"""Тесты для stream_grabber (постоянный ffmpeg-читатель потока камеры)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rosdomofon.stream_grabber import StreamGrabber, _FRAME_TTL


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
