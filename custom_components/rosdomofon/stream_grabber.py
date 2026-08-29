"""
Снимок с камеры Росдомофон по требованию — короткий одноразовый ffmpeg.

Зачем это так:
    Camera.async_camera_image() запрашивается HA «по требованию» — для
    карточек на дашборде, уведомлений, сервиса camera.snapshot. Более ранняя
    версия этого модуля держала ffmpeg открытым непрерывно, пока камера
    добавлена в HA, чтобы не зависеть от «раскачки» облачного потока RDVA на
    каждый запрос. На практике это подняло простойную нагрузку CPU в разы
    (замерено на реальной установке: 1–5% → 35–40% для одной камеры) —
    потому что декодирование видео идёт непрерывно вне зависимости от того,
    что из декодированного реально нужно (fps=1 на выходе фильтрует кадры
    уже ПОСЛЕ декодирования, экономии на самом декодировании не даёт).

Решение:
    Снимок захватывается коротким запуском ffmpeg только по факту реального
    запроса (`-frames:v 1` — декодировать ровно до первого кадра и выйти),
    результат кэшируется на несколько секунд, чтобы не плодить параллельные
    запуски при пачке запросов подряд (несколько карточек на дашборде,
    история и т.п.). Никакого фонового процесса, пока снимок явно не
    запрошен.
"""

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

from homeassistant.components.camera import async_get_stream_source
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Кэшированный кадр отдаём без повторного запуска ffmpeg, если он не старше
# этого — гасит всплески параллельных запросов снимка (несколько карточек,
# история и т.п.) без ощутимой потери свежести.
_CACHE_FRESH_SECONDS = 8.0
# Если свежий захват не удался, но в кэше есть кадр не старше этого — лучше
# отдать устаревший снимок, чем ничего (иконка «нет данных» в интерфейсе).
_CACHE_STALE_MAX_SECONDS = 120.0
# Общий бюджет на один запуск ffmpeg. Удачный захват обычно занимает
# секунду-две; таймаут — в первую очередь защита от зависшего upstream-потока.
_CAPTURE_TIMEOUT = 12.0


class StreamGrabber:
    """Снимок с камеры Росдомофон: короткий ffmpeg по требованию + кэш."""

    def __init__(self, hass: HomeAssistant, camera_entity_id: str) -> None:
        self._hass = hass
        self._camera = camera_entity_id
        self._latest: bytes | None = None
        self._latest_at: float = 0.0
        # Не даёт нескольким параллельным запросам снимка (несколько карточек
        # на дашборде и т.п.) породить несколько одновременных ffmpeg —
        # только один реальный захват за раз, остальные ждут его результат.
        self._lock = asyncio.Lock()
        # Ключ последней залогированной на уровне warning проблемы (или None) —
        # чтобы одна и та же причина не спамила лог на каждый неудачный запрос.
        self._last_issue: str | None = None

    def _log_issue(self, key: str, message: str, *args) -> None:
        """Warning при первом появлении/смене проблемы, иначе debug (не спамим)."""
        if self._last_issue != key:
            _LOGGER.warning(message, *args)
            self._last_issue = key
        else:
            _LOGGER.debug(message, *args)

    def _clear_issue(self) -> None:
        """Сбрасывает признак проблемы, когда захват снова заработал."""
        if self._last_issue is not None:
            _LOGGER.info("Grabber %s: снимок снова получен успешно", self._camera)
            self._last_issue = None

    def _is_cache_fresh(self) -> bool:
        return (
            self._latest is not None
            and self._hass.loop.time() - self._latest_at < _CACHE_FRESH_SECONDS
        )

    async def async_get_frame(self) -> bytes | None:
        """Возвращает свежий снимок — из кэша или новым коротким запуском ffmpeg."""
        if self._is_cache_fresh():
            return self._latest

        async with self._lock:
            # Пока ждали лок, кэш мог обновиться другим конкурентным запросом.
            if self._is_cache_fresh():
                return self._latest

            frame = await self._capture_one_frame()
            if frame is not None:
                self._latest = frame
                self._latest_at = self._hass.loop.time()
                return frame

        # Захват не удался — лучше отдать не слишком старый кэш, чем ничего.
        if self._latest is not None and (
            self._hass.loop.time() - self._latest_at < _CACHE_STALE_MAX_SECONDS
        ):
            return self._latest
        return None

    # -- Внутреннее -------------------------------------------------------

    async def _resolve_source(self) -> str | None:
        """URL HLS-потока камеры через прокси, с локальным base.

        stream_source() камеры отдаёт подписанный прокси-URL с внешним base
        (get_url). Подпись прокси считается только по пути, поэтому base
        можно переписать на локальный HA — так трафик ffmpeg не ходит через
        внешний адрес.
        """
        try:
            source = await async_get_stream_source(self._hass, self._camera)
        except Exception as exc:  # noqa: BLE001 — источник может быть не готов
            self._log_issue(
                "no_source", "Grabber %s: нет stream_source: %s", self._camera, exc
            )
            return None
        if not source:
            self._log_issue(
                "no_source", "Grabber %s: stream_source пуст", self._camera
            )
            return None
        split = urlsplit(source)
        if split.path.startswith("/api/rosdomofon/stream/"):
            port = getattr(self._hass.http, "server_port", 8123)
            # Если HA сама слушает TLS (ssl_certificate настроен — типичный
            # случай для DuckDNS + Let's Encrypt без отдельного reverse-proxy),
            # локальный сервер не примет plain HTTP: соединение обрывается
            # мгновенно. Схему выбираем по тому же признаку, каким сама HA
            # решает поднимать SSL-контекст (homeassistant/components/http).
            scheme = "https" if getattr(self._hass.http, "ssl_certificate", None) else "http"
            resolved = urlunsplit(
                (scheme, f"127.0.0.1:{port}", split.path, split.query, "")
            )
        else:
            resolved = source
        _LOGGER.debug("Grabber %s: источник для ffmpeg: %s", self._camera, resolved)
        return resolved

    async def _capture_one_frame(self) -> bytes | None:
        """Запускает ffmpeg на ровно один кадр и возвращает его байты (или None)."""
        source = await self._resolve_source()
        if not source:
            return None

        # Локальный импорт: homeassistant.components.ffmpeg тянет за собой
        # пакет haffmpeg, который HA устанавливает сама во время реальной
        # настройки компонента (по зависимости ffmpeg в manifest.json), но
        # который не нужен ради самого факта импорта модуля — только здесь,
        # где ffmpeg реально запускается.
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        try:
            binary = get_ffmpeg_manager(self._hass).binary
        except ValueError as exc:
            # get_ffmpeg_manager() кидает именно ValueError, если компонент
            # ffmpeg не поднят — самая частая причина: "ffmpeg" не указан
            # в dependencies манифеста интеграции, либо HA не был полностью
            # перезапущен после обновления интеграции (одного reload
            # config entry для новой зависимости манифеста недостаточно).
            self._log_issue(
                "ffmpeg_not_ready",
                "Grabber %s: компонент ffmpeg не настроен в HA (%s) — "
                "проверьте, что 'ffmpeg' есть в dependencies манифеста "
                "интеграции и что HA был полностью перезапущен",
                self._camera,
                exc,
            )
            return None

        args = [
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            # По умолчанию ffmpeg ждёт до 5 с/5 МБ входных данных, чтобы
            # определить параметры потока (analyzeduration/probesize), прежде
            # чем начать декодирование — заметная часть задержки первого
            # снимка. Формат (H.264 в MPEG-TS внутри HLS-сегмента) нам и так
            # известен заранее, поэтому режем оба лимита с большим запасом
            # относительно одного сегмента, не рискуя не успеть увидеть
            # видеодорожку.
            "-fflags",
            "nobuffer",
            "-analyzeduration",
            "1000000",
            "-probesize",
            "500000",
        ]
        parsed_source = urlsplit(source)
        if parsed_source.scheme == "https" and parsed_source.hostname in (
            "127.0.0.1",
            "localhost",
        ):
            # HTTPS на локальный HA (см. _resolve_source) — сертификат выписан
            # на внешний домен, а не на 127.0.0.1/localhost, поэтому обычная
            # проверка имени хоста в сертификате всегда провалится. Это
            # заведомо наш собственный HA-инстанс (URL строим сами), а не
            # обращение к третьей стороне — отключаем verify только для этого
            # случая, а не глобально для внешних источников.
            args += ["-tls_verify", "0"]
        args += [
            "-i",
            source,
            "-an",
            "-frames:v",
            "1",
            "-f",
            "mjpeg",
            "-q:v",
            "5",
            "pipe:1",
        ]

        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self._log_issue(
                "ffmpeg_binary_missing",
                "Grabber %s: не удалось запустить ffmpeg (%s) — проверьте, "
                "что бинарник ffmpeg установлен и доступен на хосте Home Assistant",
                self._camera,
                exc,
            )
            return None

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_CAPTURE_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            self._log_issue(
                "capture_timeout",
                "Grabber %s: не удалось получить кадр за %.0f с",
                self._camera,
                _CAPTURE_TIMEOUT,
            )
            return None

        if not stdout:
            stderr_text = stderr.decode(errors="replace").strip() if stderr else ""
            self._log_issue(
                "capture_failed",
                "Grabber %s: ffmpeg не вернул кадр (код %s)%s",
                self._camera,
                proc.returncode,
                f" — {stderr_text}" if stderr_text else "",
            )
            return None

        self._clear_issue()
        return stdout
