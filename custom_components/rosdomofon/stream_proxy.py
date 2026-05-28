"""
Прокси для HLS-потоков Росдомофон с авторизацией.

Перехватывает запросы к HLS и добавляет заголовок Authorization.
"""

import inspect
import logging
import posixpath
import re
from datetime import timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.http import KEY_AUTHENTICATED

try:
    from homeassistant.components.http import async_sign_path as _ha_async_sign_path
except ImportError:
    try:
        from homeassistant.components.http.auth import async_sign_path as _ha_async_sign_path
    except ImportError:
        _ha_async_sign_path = None

try:
    from homeassistant.components.http.auth import (
        async_validate_signed_request as _ha_async_validate_signed_request,
    )
except ImportError:
    _ha_async_validate_signed_request = None

try:
    from homeassistant.components.http import (
        async_validate_signed_path as _ha_async_validate_signed_path,
    )
except ImportError:
    try:
        from homeassistant.components.http.auth import (
            async_validate_signed_path as _ha_async_validate_signed_path,
        )
    except ImportError:
        _ha_async_validate_signed_path = None

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_HLS_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')


async def _sign_path_compat(hass: HomeAssistant, path: str) -> str:
    """Sign path across HA versions."""
    if _ha_async_sign_path is None:
        _LOGGER.warning("Signed-path helper unavailable; stream proxy URL will be unsigned.")
        return path
    if "http.auth" not in hass.data:
        return path

    try:
        result = _ha_async_sign_path(hass, path, timedelta(minutes=5))
    except TypeError:
        result = _ha_async_sign_path(hass, path)
    except Exception as exc:
        _LOGGER.warning("Failed to sign path: %s", exc)
        return path

    if inspect.isawaitable(result):
        try:
            return await result
        except Exception as exc:
            _LOGGER.warning("Failed to sign path: %s", exc)
            return path
    return result


async def _validate_signed_request_compat(hass: HomeAssistant, request: web.Request) -> bool:
    """Validate signed request across HA versions."""
    if request.get(KEY_AUTHENTICATED):
        return True
    if "http.auth" not in hass.data:
        return True

    if _ha_async_validate_signed_request is not None:
        try:
            result = _ha_async_validate_signed_request(request)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:
            _LOGGER.warning("Signed-request validation failed: %s", exc)
            return False

    if _ha_async_validate_signed_path is not None:
        try:
            result = _ha_async_validate_signed_path(hass, request.path_qs)
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:
            _LOGGER.warning("Signed-path validation failed: %s", exc)
            return False

    _LOGGER.warning("Signed-path validation is unavailable; rejecting stream proxy request.")
    return False


class RosdomofonStreamProxyView(HomeAssistantView):
    """HTTP View для проксирования HLS потоков с авторизацией."""

    url = "/api/rosdomofon/stream/{camera_id}/{host}/{path:.*}"
    name = "api:rosdomofon:stream_proxy"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        """Инициализация view."""
        self.hass = hass

    async def get(
        self, request: web.Request, camera_id: str, host: str, path: str = ""
    ) -> web.Response:
        """Проксирует GET запросы к HLS потоку."""
        if not await _validate_signed_request_compat(self.hass, request):
            _LOGGER.warning(
                "Неверная подпись для запроса: %s",
                request.path_qs,
            )
            return web.Response(status=401, text="Invalid signature")

        camera_hosts = self.hass.data.get(DOMAIN, {}).get("_camera_hosts", {})
        expected_host = camera_hosts.get(str(camera_id))
        if not expected_host:
            _LOGGER.error("Неизвестная камера %s", camera_id)
            return web.Response(status=404, text="Camera not found")

        if host != expected_host or not host.endswith(".rosdomofon.com"):
            _LOGGER.error("Неверный host для camera_id=%s: %s", camera_id, host)
            return web.Response(status=403, text="Invalid host")

        token_manager = None
        for data in self.hass.data.get(DOMAIN, {}).values():
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

        upstream_query = _upstream_query_string(request)
        target_url = f"https://{host}/{path}"
        if upstream_query:
            target_url = f"{target_url}?{upstream_query}"

        _LOGGER.debug(
            "Проксирование запроса для camera_id=%s: %s",
            camera_id,
            target_url,
        )

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
                _LOGGER.error(
                    "Ошибка запроса к %s: %d %s",
                    target_url,
                    response.status_code,
                    response.text,
                )
                return web.Response(
                    status=response.status_code,
                    text=f"Upstream error: {response.status_code}",
                )

            content_type = response.headers.get("Content-Type", "application/octet-stream")

            if path.endswith(".m3u8") or "mpegurl" in content_type:
                content = response.text
                _LOGGER.debug(
                    "Плейлист для camera_id=%s, path=%s:\n%s",
                    camera_id,
                    path,
                    content[:500],
                )
                content = await self._rewrite_playlist_urls(
                    content, camera_id, host, path
                )
                _LOGGER.debug(
                    "Переписанный плейлист для camera_id=%s:\n%s",
                    camera_id,
                    content[:500],
                )
                return web.Response(
                    body=content,
                    content_type="application/vnd.apple.mpegurl",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-cache"},
                )

            return web.Response(
                body=response.content,
                content_type=content_type,
                headers={
                    "Access-Control-Allow-Origin": "*",
                    "Cache-Control": "public, max-age=31536000",
                },
            )

        except requests.RequestException as exc:
            _LOGGER.error("Ошибка запроса к серверу Росдомофон: %s", exc)
            return web.Response(status=502, text=f"Proxy error: {exc}")
        except Exception as exc:
            _LOGGER.exception("Неожиданная ошибка в прокси: %s", exc)
            return web.Response(status=500, text=f"Internal error: {exc}")

    async def _rewrite_playlist_urls(
        self, playlist_content: str, camera_id: str, host: str, current_path: str
    ) -> str:
        """Переписывает URL в HLS плейлисте на прокси URL."""
        path_parts = current_path.rsplit("/", 1)
        base_path = path_parts[0] if len(path_parts) > 1 else ""

        lines = playlist_content.split("\n")
        rewritten_lines = []

        for line in lines:
            line = line.strip()
            if line.startswith("#"):
                rewritten_lines.append(
                    await self._rewrite_hls_uri_attributes(
                        line, camera_id, host, base_path
                    )
                )
                continue
            if not line:
                rewritten_lines.append(line)
                continue

            proxy_url = await self._rewrite_media_url(line, camera_id, host, base_path)
            rewritten_lines.append(proxy_url or line)

        return "\n".join(rewritten_lines)

    async def _rewrite_hls_uri_attributes(
        self, line: str, camera_id: str, host: str, base_path: str
    ) -> str:
        """Переписывает URI-атрибуты в HLS тегах, например EXT-X-KEY."""
        rewritten_line = line
        for uri in _HLS_URI_ATTR_RE.findall(line):
            proxy_url = await self._rewrite_media_url(uri, camera_id, host, base_path)
            if proxy_url:
                rewritten_line = rewritten_line.replace(
                    f'URI="{uri}"', f'URI="{proxy_url}"'
                )
        return rewritten_line

    async def _rewrite_media_url(
        self, url: str, camera_id: str, host: str, base_path: str
    ) -> str | None:
        """Возвращает подписанный proxy URL для HLS media URI."""
        parsed_url = urlsplit(url)
        query = parsed_url.query
        if parsed_url.scheme in ("http", "https"):
            if parsed_url.netloc != host:
                return None
            new_path = parsed_url.path.lstrip("/")
        elif url.startswith("/"):
            new_path = parsed_url.path.lstrip("/")
        elif base_path:
            new_path = f"{base_path}/{parsed_url.path}"
        else:
            new_path = parsed_url.path
        new_path = posixpath.normpath(new_path).lstrip("/")

        proxy_url = f"/api/rosdomofon/stream/{camera_id}/{host}/{new_path}"
        if query:
            proxy_url = f"{proxy_url}?{query}"
        return await _sign_path_compat(self.hass, proxy_url)


def setup_stream_proxy(hass: HomeAssistant) -> None:
    """Регистрирует прокси view для HLS потоков."""
    hass.http.register_view(RosdomofonStreamProxyView(hass))
    _LOGGER.info("Прокси для HLS потоков зарегистрирован")


def _upstream_query_string(request: web.Request) -> str:
    """Возвращает query string для upstream без подписи Home Assistant."""
    if not isinstance(request.query_string, str):
        return ""

    pairs = [
        (key, value)
        for key, value in parse_qsl(request.query_string, keep_blank_values=True)
        if key != "authSig"
    ]
    return urlencode(pairs, doseq=True)
