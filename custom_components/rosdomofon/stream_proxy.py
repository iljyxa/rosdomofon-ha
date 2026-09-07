"""
Прокси для HLS-потоков Росдомофон с авторизацией.

Перехватывает запросы к HLS и добавляет заголовок Authorization.
"""

import hashlib
import hmac
import logging
import posixpath
import re
import secrets
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

try:
    from homeassistant.helpers.http import KEY_AUTHENTICATED
except ImportError:
    # HA < 2024.x: KEY_AUTHENTICATED ещё не переехал в helpers.http.
    from homeassistant.components.http.const import KEY_AUTHENTICATED

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
_HLS_URI_ATTR_RE = re.compile(r'URI="([^"]+)"')

# Имя query-параметра с нашей собственной подписью прокси-URL.
_SIGN_PARAM = "sig"
# Ключ, под которым в hass.data[DOMAIN] хранится секрет подписи.
_PROXY_SECRET_KEY = "_proxy_secret"


def _get_proxy_secret(hass: HomeAssistant) -> bytes:
    """Возвращает секрет для подписи прокси-URL, создавая его при первом обращении.

    Секрет живёт в памяти в пределах запуска HA. Мы намеренно НЕ используем
    штатную подпись HA (async_sign_path): у неё есть срок жизни, а
    homeassistant.components.stream.Camera.async_create_stream() запрашивает
    stream_source() один раз и держит возвращённый URL внутри Stream на всё
    время, пока открыт live-просмотр (не ограничено по времени) — воркер
    стрима переиспользует этот же URL при каждом переподключении. Как только
    подпись истекает, все последующие переподключения получают 401, и камера
    ломается насовсем, пока Stream не будет пересоздан (обычно — перезапуском
    HA). Собственная HMAC-подпись без срока действия устраняет этот класс
    проблем целиком, а не отодвигает его по времени (см. issue #19). Заодно
    снимает зависимость от async_sign_path/async_validate_signed_* — вторая
    пара функций внутренняя и недокументированная (async_validate_signed_
    request/path в HA — вложенные функции, их нельзя импортировать напрямую
    ни на одной версии HA), а на части версий HA отсутствует вовсе.

    При перезапуске HA секрет генерируется заново, а stream_source()
    запрашивается заново для каждого активного потока — рассинхронизации
    не возникает.
    """
    domain_data = hass.data.setdefault(DOMAIN, {})
    secret = domain_data.get(_PROXY_SECRET_KEY)
    if secret is None:
        secret = secrets.token_bytes(32)
        domain_data[_PROXY_SECRET_KEY] = secret
    return secret


def _compute_signature(secret: bytes, path: str) -> str:
    """Вычисляет HMAC-SHA256 подпись пути прокси."""
    return hmac.new(secret, path.encode("utf-8"), hashlib.sha256).hexdigest()


def sign_proxy_path(hass: HomeAssistant, path: str) -> str:
    """Подписывает путь прокси собственной HMAC-подписью без срока действия.

    Подписывается только путь (без query) — подпись добавляется отдельным
    параметром `sig`. Остальные query-параметры (например, токены upstream-
    сегментов) в подпись не входят и на проверку не влияют.
    """
    split = urlsplit(path)
    signature = _compute_signature(_get_proxy_secret(hass), split.path)
    if split.query:
        new_query = f"{split.query}&{_SIGN_PARAM}={signature}"
    else:
        new_query = f"{_SIGN_PARAM}={signature}"
    return f"{split.path}?{new_query}"


def _validate_proxy_request(hass: HomeAssistant, request: web.Request) -> bool:
    """Проверяет собственную HMAC-подпись запроса к прокси."""
    # Запрос, аутентифицированный самой HA (например, long-lived токеном) — пропускаем.
    if request.get(KEY_AUTHENTICATED):
        return True
    signature = request.query.get(_SIGN_PARAM)
    if not signature:
        return False
    expected = _compute_signature(_get_proxy_secret(hass), request.path)
    return hmac.compare_digest(signature, expected)


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
        if not _validate_proxy_request(self.hass, request):
            _LOGGER.warning(
                "Подпись отсутствует или недействительна — запрос отклонён: %s",
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
        elif base_path and not parsed_url.path.startswith("../"):
            new_path = f"{base_path}/{parsed_url.path}"
        elif parsed_url.path.startswith("../"):
            # HLS-плейлисты Росдомофон иногда ссылаются на сегменты через
            # "../<camera_id>/<file>" — это не настоящая навигация вверх по
            # каталогам от текущего плейлиста, путь уже полный сам по себе.
            # Обычное posixpath-присоединение к base_path задвоило бы часть
            # пути (например "dllive/dllive/39167/..."), поэтому ведущие
            # "../" здесь просто отбрасываем, а не резолвим относительно
            # base_path.
            new_path = re.sub(r"^(\.\./)+", "", parsed_url.path)
        else:
            new_path = parsed_url.path
        new_path = posixpath.normpath(new_path).lstrip("/")

        proxy_url = f"/api/rosdomofon/stream/{camera_id}/{host}/{new_path}"
        if query:
            proxy_url = f"{proxy_url}?{query}"
        return sign_proxy_path(self.hass, proxy_url)


def setup_stream_proxy(hass: HomeAssistant) -> None:
    """Регистрирует прокси view для HLS потоков."""
    hass.http.register_view(RosdomofonStreamProxyView(hass))
    _LOGGER.info("Прокси для HLS потоков зарегистрирован")


def _upstream_query_string(request: web.Request) -> str:
    """Возвращает query string для upstream без параметров подписи прокси."""
    if not isinstance(request.query_string, str):
        return ""

    # authSig — подпись старой (штатной HA) схемы, оставлена в фильтре для
    # совместимости с уже открытыми на момент обновления живыми плеерами;
    # sig — текущая собственная подпись.
    dropped = ("authSig", _SIGN_PARAM)
    pairs = [
        (key, value)
        for key, value in parse_qsl(request.query_string, keep_blank_values=True)
        if key not in dropped
    ]
    return urlencode(pairs, doseq=True)
