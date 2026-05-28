"""
Прокси для HLS-потоков Росдомофон с авторизацией.

Перехватывает запросы к HLS и добавляет заголовок Authorization.
"""

import logging
import inspect

import requests
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

try:
    from homeassistant.components.http import async_validate_signed_path as _ha_async_validate_signed_path
except ImportError:
    try:
        from homeassistant.components.http.auth import async_validate_signed_path as _ha_async_validate_signed_path
    except ImportError:
        _ha_async_validate_signed_path = None


async def _validate_signed_path_compat(hass: HomeAssistant, path_qs: str) -> bool:
    """Validate signed path across HA versions. Falls back to allow."""
    if _ha_async_validate_signed_path is None:
        _LOGGER.warning(
            "Signed-path validation is unavailable in this Home Assistant version; "
            "stream proxy will accept unsigned requests."
        )
        return True

    try:
        result = _ha_async_validate_signed_path(hass, path_qs)
        if inspect.isawaitable(result):
            return await result
        return result
    except Exception as exc:
        _LOGGER.warning("Signed-path validation failed, allowing request: %s", exc)
        return True


from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


class RosdomofonStreamProxyView(HomeAssistantView):
    """HTTP View для проксирования HLS потоков с авторизацией."""

    url = "/api/rosdomofon/stream/{camera_id}/{host}/{path:.*}"
    name = "api:rosdomofon:stream_proxy"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Инициализация view."""
        self.hass = hass

    async def get(self, request: web.Request, camera_id: str, host: str, path: str = "") -> web.Response:
        """Проксирует GET запросы к HLS потоку."""
        try:
            if not await _validate_signed_path_compat(self.hass, request.path_qs):
                _LOGGER.warning("Неверная подпись для запроса: %s", request.path_qs)
                return web.Response(status=401, text="Invalid signature")
        except Exception as exc:
            _LOGGER.debug("Ошибка проверки подписи: %s", exc)
            return web.Response(status=401, text="Invalid signature")

        # Проверяем валидность host (должен быть .rosdomofon.com)
        if not host or not host.endswith(".rosdomofon.com"):
            _LOGGER.error("Неверный host для camera_id=%s: %s", camera_id, host)
            return web.Response(status=403, text="Invalid host")

        # Находим token_manager
        token_manager = None
        for entry_id, data in self.hass.data.get(DOMAIN, {}).items():
            if isinstance(data, dict) and "token_manager" in data:
                token_manager = data["token_manager"]
                break

        if token_manager is None:
            _LOGGER.error("TokenManager не найден")
            return web.Response(status=500, text="Integration not configured")

        if not await token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен для проксирования")
            return web.Response(status=401, text="Token refresh failed")

        access_token = token_manager.access_token

        if not path:
            path = f"live/{camera_id}.m3u8"

        target_url = f"https://{host}/{path}"

        _LOGGER.debug("Проксирование запроса для camera_id=%s: %s", camera_id, target_url)

        try:
            response = await self.hass.async_add_executor_job(
                lambda: requests.get(
                    target_url,
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "User-Agent": "HomeAssistant/RosdomofonIntegration",
                    },
                    timeout=10,
                    stream=True,
                )
            )

            if response.status_code != 200:
                _LOGGER.error("Ошибка запроса к %s: %d %s", target_url, response.status_code, response.text)
                return web.Response(status=response.status_code, text=f"Upstream error: {response.status_code}")

            content_type = response.headers.get("Content-Type", "application/octet-stream")

            if path.endswith(".m3u8") or "mpegurl" in content_type:
                content = response.text
                _LOGGER.debug("Плейлист для camera_id=%s, path=%s:\n%s", camera_id, path, content[:500])
                content = self._rewrite_playlist_urls(content, camera_id, host, path)
                _LOGGER.debug("Переписанный плейлист для camera_id=%s:\n%s", camera_id, content[:500])
                return web.Response(
                    body=content,
                    content_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
                )

            return web.Response(
                body=response.content,
                content_type=content_type,
                headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=31536000"},
            )

        except requests.RequestException as exc:
            _LOGGER.error("Ошибка запроса к серверу Росдомофон: %s", exc)
            return web.Response(status=502, text=f"Proxy error: {exc}")
        except Exception as exc:
            _LOGGER.exception("Неожиданная ошибка в прокси: %s", exc)
            return web.Response(status=500, text=f"Internal error: {exc}")

    @staticmethod
    def _rewrite_playlist_urls(playlist_content: str, camera_id: str, host: str, current_path: str) -> str:
        """Переписывает URL в HLS плейлисте на прокси URL."""
        path_parts = current_path.rsplit("/", 1)
        base_path = path_parts[0] if len(path_parts) > 1 else ""

        lines = playlist_content.split("\n")
        rewritten_lines = []

        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                rewritten_lines.append(line)
                continue
            if line.startswith("http://") or line.startswith("https://"):
                rewritten_lines.append(line)
                continue
            if line.startswith("/"):
                new_path = line.lstrip("/")
            elif base_path:
                new_path = f"{base_path}/{line}"
            else:
                new_path = line
            proxy_url = f"/api/rosdomofon/stream/{camera_id}/{host}/{new_path}"
            rewritten_lines.append(proxy_url)

        return "\n".join(rewritten_lines)


def setup_stream_proxy(hass: HomeAssistant) -> None:
    """Регистрирует прокси view для HLS потоков."""
    hass.http.register_view(RosdomofonStreamProxyView(hass))
    _LOGGER.info("Прокси для HLS потоков зарегистрирован")