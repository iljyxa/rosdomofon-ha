"""Тесты для stream_grabber (постоянный ffmpeg-читатель потока камеры)."""

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rosdomofon.stream_grabber import StreamGrabber, _FRAME_TTL

_LOGGER_NAME = "custom_components.rosdomofon.stream_grabber"


def _make_grabber(now: float = 1000.0) -> tuple[StreamGrabber, MagicMock]:
    """Создаёт StreamGrabber с моковым hass и управляемым временем."""
    hass = MagicMock()
    hass.loop.time.return_value = now
    return StreamGrabber(hass, "camera.test"), hass


# -- Извлечение JPEG-кадров из потока ffmpeg -------------------------------


def test_extract_frames_single_jpeg():
    """Один завершённый JPEG в буфере становится последним кадром."""
    grabber, _hass = _make_grabber()
    frame = b"\xff\xd8" + b"payload" + b"\xff\xd9"
    buf = bytearray(frame)

    grabber._extract_frames(buf)

    assert grabber.latest_frame() == frame
    assert bytes(buf) == b""


def test_extract_frames_keeps_latest_of_several():
    """Несколько кадров в одном чанке — сохраняется последний."""
    grabber, _hass = _make_grabber()
    frame1 = b"\xff\xd8" + b"one" + b"\xff\xd9"
    frame2 = b"\xff\xd8" + b"two" + b"\xff\xd9"
    buf = bytearray(frame1 + frame2)

    grabber._extract_frames(buf)

    assert grabber.latest_frame() == frame2


def test_extract_frames_incomplete_frame_not_emitted():
    """Недочитанный кадр (нет EOI) не становится latest, буфер не режется."""
    grabber, _hass = _make_grabber()
    buf = bytearray(b"\xff\xd8" + b"partial")

    grabber._extract_frames(buf)

    assert grabber.latest_frame() is None
    assert bytes(buf) == b"\xff\xd8partial"


def test_extract_frames_across_chunks():
    """Кадр, разбитый на два чанка — как реально приходит из stdout ffmpeg."""
    grabber, _hass = _make_grabber()
    frame = b"\xff\xd8" + b"x" * 20 + b"\xff\xd9"

    buf = bytearray(frame[:5])
    grabber._extract_frames(buf)
    assert grabber.latest_frame() is None

    buf.extend(frame[5:])
    grabber._extract_frames(buf)
    assert grabber.latest_frame() == frame


def test_extract_frames_discards_garbage_before_soi():
    """Мусор без SOI перед кадром не ломает извлечение следующего кадра."""
    grabber, _hass = _make_grabber()
    frame = b"\xff\xd8" + b"payload" + b"\xff\xd9"
    buf = bytearray(b"garbage-without-marker" + frame)

    grabber._extract_frames(buf)

    assert grabber.latest_frame() == frame


# -- Свежесть кадра (TTL) ----------------------------------------------------


def test_latest_frame_none_before_any_frame():
    """Пока не было ни одного кадра, latest_frame() возвращает None."""
    grabber, _hass = _make_grabber()
    assert grabber.latest_frame() is None


def test_latest_frame_expires_after_ttl():
    """Кадр становится «протухшим», если давно не обновлялся (поток завис)."""
    grabber, hass = _make_grabber(now=1000.0)
    buf = bytearray(b"\xff\xd8" + b"payload" + b"\xff\xd9")
    grabber._extract_frames(buf)
    assert grabber.latest_frame() is not None

    hass.loop.time.return_value = 1000.0 + _FRAME_TTL + 1
    assert grabber.latest_frame() is None


# -- Резолв источника (переписывание base на локальный HA) -----------------


@pytest.mark.asyncio
async def test_resolve_source_rewrites_proxy_base_to_localhost():
    """URL нашего же прокси переписывается на локальный base (127.0.0.1)."""
    grabber, hass = _make_grabber()
    hass.http.server_port = 8123
    proxied = (
        "https://ha.example.com/api/rosdomofon/stream/39167/"
        "s.rdva.rosdomofon.com/live/39167.m3u8?authSig=abc"
    )

    with patch(
        "custom_components.rosdomofon.stream_grabber.async_get_stream_source",
        AsyncMock(return_value=proxied),
    ):
        source = await grabber._resolve_source()

    assert source == (
        "http://127.0.0.1:8123/api/rosdomofon/stream/39167/"
        "s.rdva.rosdomofon.com/live/39167.m3u8?authSig=abc"
    )


@pytest.mark.asyncio
async def test_resolve_source_leaves_foreign_url_untouched():
    """URL, не совпадающий с нашим прокси, не переписывается."""
    grabber, _hass = _make_grabber()
    direct = "https://s.rdva.rosdomofon.com/live/39167.m3u8"

    with patch(
        "custom_components.rosdomofon.stream_grabber.async_get_stream_source",
        AsyncMock(return_value=direct),
    ):
        source = await grabber._resolve_source()

    assert source == direct


@pytest.mark.asyncio
async def test_resolve_source_returns_none_when_unavailable():
    """Если stream_source ещё не готов (None или исключение) — None, без падения."""
    grabber, _hass = _make_grabber()

    with patch(
        "custom_components.rosdomofon.stream_grabber.async_get_stream_source",
        AsyncMock(return_value=None),
    ):
        assert await grabber._resolve_source() is None

    with patch(
        "custom_components.rosdomofon.stream_grabber.async_get_stream_source",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        assert await grabber._resolve_source() is None


# -- start()/async_stop() ----------------------------------------------------


def test_start_is_idempotent():
    """Повторный start() при уже запущенной задаче не создаёт вторую."""
    grabber, hass = _make_grabber()
    task = MagicMock(done=MagicMock(return_value=False))

    def _create_background_task(coro, _name):
        # MagicMock не запускает переданную корутину (_run()) — закрываем
        # её явно, иначе она утечёт как "never awaited" до сборщика мусора.
        coro.close()
        return task

    hass.async_create_background_task.side_effect = _create_background_task

    grabber.start()
    grabber.start()

    hass.async_create_background_task.assert_called_once()


@pytest.mark.asyncio
async def test_async_stop_without_start_does_not_raise():
    """async_stop() до старта (например, повторное удаление сущности) безопасен."""
    grabber, _hass = _make_grabber()
    await grabber.async_stop()
    assert grabber.latest_frame() is None


# -- Видимость проблем в логе (warn once, потом debug; сброс при восстановлении) --


def test_log_issue_warns_once_then_downgrades_to_debug(caplog):
    """Одна и та же причина логируется на warning только при первом появлении."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    grabber._log_issue("no_source", "проблема: %s", "test")
    grabber._log_issue("no_source", "проблема: %s", "test")

    records = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert [r.levelno for r in records] == [logging.WARNING, logging.DEBUG]


def test_log_issue_warns_again_when_reason_changes(caplog):
    """Смена причины проблемы — новый warning, а не подавляется предыдущей."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.DEBUG, logger=_LOGGER_NAME)

    grabber._log_issue("no_source", "нет источника")
    grabber._log_issue("ffmpeg_binary_missing", "нет ffmpeg")

    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING]
    assert len(warnings) == 2


def test_clear_issue_logs_recovery_only_if_issue_was_set(caplog):
    """clear_issue логирует восстановление только если проблема реально была."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.INFO, logger=_LOGGER_NAME)

    grabber._clear_issue()
    assert not [r for r in caplog.records if r.name == _LOGGER_NAME]

    grabber._last_issue = "no_source"
    grabber._clear_issue()

    assert grabber._last_issue is None
    assert any(r.name == _LOGGER_NAME for r in caplog.records)


def test_extract_frames_clears_issue_on_successful_frame():
    """Успешное извлечение кадра сбрасывает признак предыдущей проблемы."""
    grabber, _hass = _make_grabber()
    grabber._last_issue = "pump_error"

    buf = bytearray(b"\xff\xd8" + b"payload" + b"\xff\xd9")
    grabber._extract_frames(buf)

    assert grabber._last_issue is None


@pytest.mark.asyncio
async def test_run_gives_actionable_message_when_ffmpeg_component_not_ready(caplog):
    """ValueError из get_ffmpeg_manager (ffmpeg-компонент не поднят) — понятный warning."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def fake_pump(_source):
        grabber._closing = True  # прерываем супервизор после первой попытки
        raise ValueError("ffmpeg component not initialized")

    with patch.object(grabber, "_resolve_source", AsyncMock(return_value="http://x/y")), \
         patch.object(grabber, "_pump", fake_pump), \
         patch("asyncio.sleep", AsyncMock()):
        await grabber._run()

    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(warnings) == 1
    assert "dependencies" in warnings[0].message


@pytest.mark.asyncio
async def test_run_gives_actionable_message_when_ffmpeg_binary_missing(caplog):
    """FileNotFoundError при запуске ffmpeg — понятный warning про бинарник на хосте."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def fake_pump(_source):
        grabber._closing = True
        raise FileNotFoundError("ffmpeg")

    with patch.object(grabber, "_resolve_source", AsyncMock(return_value="http://x/y")), \
         patch.object(grabber, "_pump", fake_pump), \
         patch("asyncio.sleep", AsyncMock()):
        await grabber._run()

    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(warnings) == 1
    assert "бинарник" in warnings[0].message


@pytest.mark.asyncio
async def test_log_ffmpeg_exit_includes_stderr_and_return_code(caplog):
    """При тихом завершении ffmpeg (без исключения) в лог попадает stderr.

    Раньше stderr уходил в DEVNULL и такое завершение было полностью
    неразличимо от нормальной работы — единственная причина, по которой это
    сейчас можно диагностировать без правки кода на лету.
    """
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.stderr = MagicMock()
    fake_proc.stderr.read = AsyncMock(return_value=b"Server returned 404 Not Found\n")
    grabber._proc = fake_proc

    await grabber._log_ffmpeg_exit()

    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(warnings) == 1
    assert "404" in warnings[0].message
    assert "1" in warnings[0].message


@pytest.mark.asyncio
async def test_log_ffmpeg_exit_without_proc_does_not_raise():
    """_log_ffmpeg_exit() без активного процесса (гонка при остановке) безопасен."""
    grabber, _hass = _make_grabber()
    grabber._proc = None
    await grabber._log_ffmpeg_exit()  # не должно падать
