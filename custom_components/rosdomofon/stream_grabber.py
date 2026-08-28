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
# Подписанный URL плейлиста, который ffmpeg держит открытым, живёт 5 минут
# (жёстко в camera.py: HA async_sign_path требует expiration позиционным
# аргументом без дефолта, поэтому _sign_path_compat всегда попадает в
# fallback timedelta(minutes=5)). ffmpeg переопрашивает этот же URL для
# live-плейлиста всё время жизни процесса — если не пересоздать сессию
# заранее, через 5 минут прокси начнёт отвечать 401 и это выглядело бы как
# обрыв потока. Ротируем сессию проактивно, с запасом.
_MAX_SESSION_SECONDS = 240
# Кадр без EOI дольше этого объёма считаем битым/десинхронизированным и
# сбрасываем буфер целиком — иначе при потерянном EOI буфер рос бы
# неограниченно вплоть до перезапуска всего процесса ffmpeg.
_MAX_PENDING_FRAME_BYTES = 5 * 1024 * 1024


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
        # Ключ последней залогированной на уровне warning проблемы (или None).
        # Супервизор ретраит каждые несколько секунд бесконечно — без этого
        # одна и та же причина спамила бы лог вечно на warning; вместо этого
        # предупреждаем один раз при появлении/смене проблемы, дальше — debug.
        self._last_issue: str | None = None

    def _log_issue(self, key: str, message: str, *args) -> None:
        """Warning при первом появлении/смене проблемы, иначе debug (не спамим)."""
        if self._last_issue != key:
            _LOGGER.warning(message, *args)
            self._last_issue = key
        else:
            _LOGGER.debug(message, *args)

    def _clear_issue(self) -> None:
        """Сбрасывает признак проблемы, когда чтение потока снова заработало."""
        if self._last_issue is not None:
            _LOGGER.info("Grabber %s: поток восстановлен", self._camera)
            self._last_issue = None

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
            # мгновенно (см. _log_ffmpeg_exit — "Error reading HTTP response:
            # End of file"). Схему выбираем по тому же признаку, каким сама HA
            # решает поднимать SSL-контекст (homeassistant/components/http).
            scheme = "https" if getattr(self._hass.http, "ssl_certificate", None) else "http"
            resolved = urlunsplit(
                (scheme, f"127.0.0.1:{port}", split.path, split.query, "")
            )
        else:
            resolved = source
        _LOGGER.debug("Grabber %s: источник для ffmpeg: %s", self._camera, resolved)
        return resolved

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
            except FileNotFoundError as exc:
                self._log_issue(
                    "ffmpeg_binary_missing",
                    "Grabber %s: не удалось запустить ffmpeg (%s) — проверьте, "
                    "что бинарник ffmpeg установлен и доступен на хосте Home Assistant",
                    self._camera,
                    exc,
                )
            except Exception as exc:  # noqa: BLE001 — логируем и перезапускаем
                self._log_issue(
                    "pump_error", "Grabber %s: ошибка чтения потока: %s", self._camera, exc
                )
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
            stderr=asyncio.subprocess.PIPE,
        )
        _LOGGER.debug("Grabber %s: ffmpeg запущен", self._camera)
        stdout = self._proc.stdout
        assert stdout is not None
        buf = bytearray()
        started_at = self._hass.loop.time()
        planned_rotation = False
        try:
            while not self._closing:
                if self._hass.loop.time() - started_at > _MAX_SESSION_SECONDS:
                    # Проактивная ротация до истечения подписи URL (см.
                    # _MAX_SESSION_SECONDS) — это ожидаемое пересоздание
                    # процесса, а не сбой, поэтому не логируем как ошибку.
                    planned_rotation = True
                    break
                chunk = await stdout.read(65536)
                if not chunk:
                    break  # ffmpeg завершился — выходим на перезапуск
                buf.extend(chunk)
                self._extract_frames(buf)
        finally:
            if not self._closing and not planned_rotation:
                # ffmpeg завершился сам (не мы его остановили и не плановая
                # ротация) — до сих пор это было видно только по факту
                # молчания (нет кадров, нет ошибки: -loglevel error + DEVNULL
                # для stderr выбрасывали единственную подсказку о причине).
                # Читаем накопленный stderr, пока процесс ещё не убит
                # _terminate_proc().
                await self._log_ffmpeg_exit()
            await self._terminate_proc()
        if planned_rotation:
            _LOGGER.debug(
                "Grabber %s: плановая ротация ffmpeg (обновление подписи URL)",
                self._camera,
            )

    async def _log_ffmpeg_exit(self) -> None:
        """Логирует причину неожиданного завершения ffmpeg (код + stderr)."""
        proc = self._proc
        if proc is None:
            return
        # stdout уже закрылся, но это не гарантирует, что asyncio успел
        # обработать завершение процесса и заполнить returncode — ждём явно,
        # иначе он может по гонке всё ещё оставаться None в логе.
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        stderr_text = ""
        if proc.stderr is not None:
            try:
                data = await asyncio.wait_for(proc.stderr.read(4096), timeout=2)
                stderr_text = data.decode(errors="replace").strip()
            except Exception:  # noqa: BLE001 — диагностика не должна падать сама
                pass
        self._log_issue(
            "ffmpeg_exited",
            "Grabber %s: ffmpeg неожиданно завершился (код %s)%s",
            self._camera,
            proc.returncode,
            f" — {stderr_text}" if stderr_text else " (stderr пуст)",
        )

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

            # Если раньше EOI встречается ещё один SOI — текущий кадр битый
            # или недописанный (в валидных JPEG-данных байты EOI 0xFFD9 не
            # могут появиться внутри scan-секции из-за byte-stuffing, так что
            # такое сочетание — признак десинхронизации, не совпадение).
            # Отбрасываем всё до второго SOI и пробуем снова с него — иначе
            # EOI второго кадра склеился бы с началом первого в один битый
            # снимок вместо ожидаемого "кадр ещё не дочитан".
            next_start = buf.find(_JPEG_SOI, len(_JPEG_SOI))
            end = buf.find(_JPEG_EOI, len(_JPEG_SOI))
            if next_start != -1 and (end == -1 or next_start < end):
                del buf[:next_start]
                continue

            if end == -1:
                if len(buf) > _MAX_PENDING_FRAME_BYTES:
                    # EOI так и не нашёлся на разумном объёме — кадр битый,
                    # а не просто "ещё не дочитан". Без этого буфер рос бы
                    # неограниченно вплоть до следующего перезапуска ffmpeg.
                    self._log_issue(
                        "frame_too_large",
                        "Grabber %s: не найден конец JPEG-кадра за %d МБ, "
                        "сбрасываю буфер",
                        self._camera,
                        _MAX_PENDING_FRAME_BYTES // (1024 * 1024),
                    )
                    buf.clear()
                return  # кадр ещё не дочитан

            end += 2
            self._latest = bytes(buf[:end])
            self._latest_at = self._hass.loop.time()
            del buf[:end]
            self._clear_issue()
