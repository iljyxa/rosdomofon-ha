"""
Постоянный захват кадров с камеры Росдомофон через фоновый ffmpeg.

Зачем это нужно:
    Облачный поток RDVA поднимается по факту подключения зрителя и гаснет, когда
    зритель отключается. HA получает снимок камеры (async_camera_image) «по
    требованию», из-за чего дефолтная реализация Camera сама поднимает и гасит
    стрим на каждый запрос снимка — RDVA не успевает выдать поток стабильно, и
    снимки часто приходят рваными/пустыми (см. историю HLS-прокси и его
    зависимость от свежего токена в stream_proxy.py).

Решение:
    На каждую камеру, пока она добавлена в HA, держим постоянный ffmpeg, который
    читает HLS-поток через наш авторизованный прокси (токен подставляет прокси,
    поэтому долгоживущий процесс переживает ротацию токена) и хранит в памяти
    последний свежий кадр (JPEG). Camera.async_camera_image() отдаёт этот кадр
    вместо запроса «по требованию».
"""

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

from homeassistant.components.camera import async_get_stream_source
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Маркеры начала (SOI) и конца (EOI) JPEG — по ним режем MJPEG-поток из ffmpeg.
_JPEG_SOI = b"\xff\xd8"
_JPEG_EOI = b"\xff\xd9"

# Частота выдачи кадров. Снимку камеры чаще не нужно, а декодирование дешевле.
_OUTPUT_FPS = 1
# Пауза перед перезапуском ffmpeg после обрыва/падения.
_RESTART_DELAY = 5
# Кадр считается свежим не дольше этого времени; иначе поток завис или умер.
_FRAME_TTL = 15.0


class StreamGrabber:
    """Постоянный ffmpeg-читатель одной камеры с буфером последнего кадра."""

    def __init__(self, hass: HomeAssistant, camera_entity_id: str) -> None:
        self._hass = hass
        self._camera = camera_entity_id
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None
        self._closing = False
        self._latest: bytes | None = None
        self._latest_at: float = 0.0

    @property
    def camera(self) -> str:
        """entity_id камеры, которую читает grabber."""
        return self._camera

    def latest_frame(self) -> bytes | None:
        """Последний свежий кадр (JPEG) или None, если поток не готов/завис."""
        if self._latest is None:
            return None
        if self._hass.loop.time() - self._latest_at > _FRAME_TTL:
            return None
        return self._latest

    def start(self) -> None:
        """Запускает фоновую задачу-супервизор (идемпотентно)."""
        if self._task is not None and not self._task.done():
            return
        self._closing = False
        self._task = self._hass.async_create_background_task(
            self._run(), f"rosdomofon_grabber_{self._camera}"
        )

    async def async_stop(self) -> None:
        """Останавливает ffmpeg и фоновую задачу."""
        self._closing = True
        await self._terminate_proc()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._latest = None

    # -- Внутреннее -------------------------------------------------------

    async def _terminate_proc(self) -> None:
        """Аккуратно завершает процесс ffmpeg (terminate, при зависании kill)."""
        proc = self._proc
        self._proc = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _resolve_source(self) -> str | None:
        """URL HLS-потока камеры через прокси, с локальным base.

        stream_source() камеры отдаёт подписанный прокси-URL с внешним base
        (get_url). Подпись прокси считается только по пути, поэтому base можно
        переписать на локальный HA — так трафик ffmpeg не ходит через внешний
        адрес.
        """
        try:
            source = await async_get_stream_source(self._hass, self._camera)
        except Exception as exc:  # noqa: BLE001 — источник может быть не готов
            _LOGGER.debug("Нет stream_source для %s: %s", self._camera, exc)
            return None
        if not source:
            return None
        split = urlsplit(source)
        if split.path.startswith("/api/rosdomofon/stream/"):
            port = getattr(self._hass.http, "server_port", 8123)
            return urlunsplit(
                ("http", f"127.0.0.1:{port}", split.path, split.query, "")
            )
        return source

    async def _run(self) -> None:
        """Супервизор: держит ffmpeg живым, перезапускает при обрыве."""
        while not self._closing:
            source = await self._resolve_source()
            if not source:
                await asyncio.sleep(_RESTART_DELAY)
                continue
            try:
                await self._pump(source)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — логируем и перезапускаем
                _LOGGER.debug("Grabber %s: ошибка чтения потока: %s", self._camera, exc)
            if not self._closing:
                await asyncio.sleep(_RESTART_DELAY)

    async def _pump(self, source: str) -> None:
        """Запускает ffmpeg и читает MJPEG из stdout, обновляя последний кадр."""
        # Локальный импорт: homeassistant.components.ffmpeg тянет за собой
        # пакет haffmpeg, который HA устанавливает сама во время реальной
        # настройки компонента (по зависимости ffmpeg в manifest.json), но
        # который не нужен ради самого факта импорта модуля — только здесь,
        # где ffmpeg реально запускается.
        from homeassistant.components.ffmpeg import get_ffmpeg_manager

        binary = get_ffmpeg_manager(self._hass).binary
        args = [
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source,
            "-an",
            "-vf",
            f"fps={_OUTPUT_FPS}",
            "-f",
            "mjpeg",
            "-q:v",
            "5",
            "pipe:1",
        ]
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        _LOGGER.debug("Grabber %s: ffmpeg запущен", self._camera)
        stdout = self._proc.stdout
        assert stdout is not None
        buf = bytearray()
        try:
            while not self._closing:
                chunk = await stdout.read(65536)
                if not chunk:
                    break  # ffmpeg завершился — выходим на перезапуск
                buf.extend(chunk)
                self._extract_frames(buf)
        finally:
            await self._terminate_proc()

    def _extract_frames(self, buf: bytearray) -> None:
        """Вырезает завершённые JPEG из буфера и сохраняет последний."""
        while True:
            start = buf.find(_JPEG_SOI)
            if start == -1:
                # Мусор без начала кадра — оставляем хвост на случай разрыва SOI.
                if len(buf) > 2:
                    del buf[:-1]
                return
            if start > 0:
                del buf[:start]
            end = buf.find(_JPEG_EOI, 2)
            if end == -1:
                return  # кадр ещё не дочитан
            end += 2
            self._latest = bytes(buf[:end])
            self._latest_at = self._hass.loop.time()
            del buf[:end]
