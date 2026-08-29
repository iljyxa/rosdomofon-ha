"""Тесты для stream_grabber (снимок камеры коротким ffmpeg по требованию)."""

import asyncio
import logging
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.rosdomofon.stream_grabber import (
    StreamGrabber,
    _CACHE_FRESH_SECONDS,
    _CACHE_STALE_MAX_SECONDS,
    _CAPTURE_TIMEOUT,
)

_LOGGER_NAME = "custom_components.rosdomofon.stream_grabber"


def _make_grabber(now: float = 1000.0) -> tuple[StreamGrabber, MagicMock]:
    """Создаёт StreamGrabber с моковым hass и управляемым временем."""
    hass = MagicMock()
    hass.loop.time.return_value = now
    return StreamGrabber(hass, "camera.test"), hass


# -- Резолв источника (переписывание base на локальный HA) -----------------


@pytest.mark.asyncio
async def test_resolve_source_rewrites_proxy_base_to_localhost():
    """URL нашего же прокси переписывается на локальный base (127.0.0.1)."""
    grabber, hass = _make_grabber()
    hass.http.server_port = 8123
    hass.http.ssl_certificate = None  # HA слушает plain HTTP
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
async def test_resolve_source_uses_https_when_ha_terminates_tls():
    """HA слушает TLS напрямую (ssl_certificate настроен) — локальный base на https.

    Реальный кейс: DuckDNS + Let's Encrypt без отдельного reverse-proxy — HA
    сама принимает только TLS-соединения, plain HTTP на тот же порт рвётся
    мгновенно (ffmpeg: "Error reading HTTP response: End of file").
    """
    grabber, hass = _make_grabber()
    hass.http.server_port = 8123
    hass.http.ssl_certificate = "/config/certs/fullchain.pem"
    proxied = (
        "https://ha.example.duckdns.org/api/rosdomofon/stream/39167/"
        "s.rdva.rosdomofon.com/live/39167.m3u8?authSig=abc"
    )

    with patch(
        "custom_components.rosdomofon.stream_grabber.async_get_stream_source",
        AsyncMock(return_value=proxied),
    ):
        source = await grabber._resolve_source()

    assert source == (
        "https://127.0.0.1:8123/api/rosdomofon/stream/39167/"
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

    warnings = [
        r for r in caplog.records if r.name == _LOGGER_NAME and r.levelno == logging.WARNING
    ]
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


# -- Кэш (async_get_frame) ---------------------------------------------------


@pytest.mark.asyncio
async def test_get_frame_returns_fresh_cache_without_capturing():
    """Свежий кэш отдаётся сразу, без запуска ffmpeg."""
    grabber, _hass = _make_grabber(now=1000.0)
    grabber._latest = b"cached-frame"
    grabber._latest_at = 1000.0 - (_CACHE_FRESH_SECONDS - 1)

    with patch.object(grabber, "_capture_one_frame", AsyncMock()) as mock_capture:
        frame = await grabber.async_get_frame()

    assert frame == b"cached-frame"
    mock_capture.assert_not_called()


@pytest.mark.asyncio
async def test_get_frame_captures_when_cache_is_stale():
    """Устаревший кэш не годится — запускается новый захват."""
    grabber, _hass = _make_grabber(now=1000.0)
    grabber._latest = b"old-frame"
    grabber._latest_at = 1000.0 - (_CACHE_FRESH_SECONDS + 1)

    with patch.object(
        grabber, "_capture_one_frame", AsyncMock(return_value=b"new-frame")
    ) as mock_capture:
        frame = await grabber.async_get_frame()

    assert frame == b"new-frame"
    mock_capture.assert_awaited_once()
    assert grabber._latest == b"new-frame"


@pytest.mark.asyncio
async def test_get_frame_falls_back_to_stale_cache_on_capture_failure():
    """Захват не удался, но кэш не совсем протух — отдаём его, а не None."""
    grabber, _hass = _make_grabber(now=1000.0)
    grabber._latest = b"stale-but-usable"
    grabber._latest_at = 1000.0 - (_CACHE_STALE_MAX_SECONDS - 1)

    with patch.object(grabber, "_capture_one_frame", AsyncMock(return_value=None)):
        frame = await grabber.async_get_frame()

    assert frame == b"stale-but-usable"


@pytest.mark.asyncio
async def test_get_frame_returns_none_when_capture_fails_and_cache_too_old():
    """Захват не удался, а кэша либо нет, либо он слишком старый — None."""
    grabber, _hass = _make_grabber(now=1000.0)
    grabber._latest = b"way-too-old"
    grabber._latest_at = 1000.0 - (_CACHE_STALE_MAX_SECONDS + 1)

    with patch.object(grabber, "_capture_one_frame", AsyncMock(return_value=None)):
        frame = await grabber.async_get_frame()

    assert frame is None


@pytest.mark.asyncio
async def test_get_frame_serializes_concurrent_requests_into_one_capture():
    """Несколько параллельных запросов снимка не плодят несколько ffmpeg."""
    grabber, _hass = _make_grabber(now=1000.0)
    capture_started = 0

    async def fake_capture():
        nonlocal capture_started
        capture_started += 1
        return b"frame"

    with patch.object(grabber, "_capture_one_frame", fake_capture):
        results = await asyncio.gather(
            grabber.async_get_frame(),
            grabber.async_get_frame(),
            grabber.async_get_frame(),
        )

    assert results == [b"frame", b"frame", b"frame"]
    assert capture_started == 1


# -- _capture_one_frame -------------------------------------------------------


@pytest.mark.asyncio
async def test_capture_one_frame_returns_none_without_source():
    """Нет источника — ffmpeg вообще не запускается."""
    grabber, _hass = _make_grabber()

    with patch.object(grabber, "_resolve_source", AsyncMock(return_value=None)), \
         patch("asyncio.create_subprocess_exec") as mock_exec:
        frame = await grabber._capture_one_frame()

    assert frame is None
    mock_exec.assert_not_called()


@pytest.mark.asyncio
async def test_capture_one_frame_success():
    """Успешный захват — возвращает stdout ffmpeg как есть."""
    grabber, _hass = _make_grabber()

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"jpeg-bytes", b""))

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    fake_ffmpeg_module = types.SimpleNamespace(
        get_ffmpeg_manager=lambda _hass: MagicMock(binary="ffmpeg")
    )

    with patch.object(grabber, "_resolve_source", AsyncMock(return_value="http://127.0.0.1:8123/x")), \
         patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec), \
         patch.dict(sys.modules, {"homeassistant.components.ffmpeg": fake_ffmpeg_module}):
        frame = await grabber._capture_one_frame()

    assert frame == b"jpeg-bytes"


@pytest.mark.asyncio
async def test_capture_one_frame_adds_tls_verify_for_local_https_source():
    """HTTPS на 127.0.0.1/localhost — добавляется -tls_verify 0."""
    grabber, _hass = _make_grabber()
    captured = {}

    fake_proc = MagicMock()
    fake_proc.returncode = 0
    fake_proc.communicate = AsyncMock(return_value=(b"jpeg-bytes", b""))

    async def fake_create_subprocess_exec(*args, **_kwargs):
        captured["args"] = args
        return fake_proc

    fake_ffmpeg_module = types.SimpleNamespace(
        get_ffmpeg_manager=lambda _hass: MagicMock(binary="ffmpeg")
    )

    with patch.object(
        grabber, "_resolve_source", AsyncMock(return_value="https://127.0.0.1:8123/x")
    ), patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec), patch.dict(
        sys.modules, {"homeassistant.components.ffmpeg": fake_ffmpeg_module}
    ):
        await grabber._capture_one_frame()

    assert "-tls_verify" in captured["args"]


@pytest.mark.asyncio
async def test_capture_one_frame_ffmpeg_component_not_ready(caplog):
    """ValueError из get_ffmpeg_manager (ffmpeg-компонент не поднят) — понятный warning."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    def raise_value_error(_hass):
        raise ValueError("ffmpeg component not initialized")

    fake_ffmpeg_module = types.SimpleNamespace(get_ffmpeg_manager=raise_value_error)

    with patch.object(
        grabber, "_resolve_source", AsyncMock(return_value="http://127.0.0.1:8123/x")
    ), patch.dict(sys.modules, {"homeassistant.components.ffmpeg": fake_ffmpeg_module}):
        frame = await grabber._capture_one_frame()

    assert frame is None
    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(warnings) == 1
    assert "dependencies" in warnings[0].message


@pytest.mark.asyncio
async def test_capture_one_frame_binary_missing(caplog):
    """FileNotFoundError при запуске ffmpeg — понятный warning про бинарник на хосте."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        raise FileNotFoundError("ffmpeg")

    fake_ffmpeg_module = types.SimpleNamespace(
        get_ffmpeg_manager=lambda _hass: MagicMock(binary="ffmpeg")
    )

    with patch.object(
        grabber, "_resolve_source", AsyncMock(return_value="http://127.0.0.1:8123/x")
    ), patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec), patch.dict(
        sys.modules, {"homeassistant.components.ffmpeg": fake_ffmpeg_module}
    ):
        frame = await grabber._capture_one_frame()

    assert frame is None
    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(warnings) == 1
    assert "бинарник" in warnings[0].message


@pytest.mark.asyncio
async def test_capture_one_frame_timeout_kills_process(caplog):
    """Захват дольше бюджета — процесс убивается, снимок недоступен."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    fake_proc = MagicMock()
    fake_proc.kill = MagicMock()
    fake_proc.wait = AsyncMock(return_value=None)
    fake_proc.communicate = AsyncMock(return_value=(b"", b""))

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    fake_ffmpeg_module = types.SimpleNamespace(
        get_ffmpeg_manager=lambda _hass: MagicMock(binary="ffmpeg")
    )

    async def fake_wait_for(coro, timeout):
        # Настоящий asyncio.wait_for корректно закрывает/отменяет переданный
        # awaitable при таймауте — закрываем корутину сами, иначе она утечёт
        # как "never awaited" (в этой сессии тесты гоняются с -W error).
        if hasattr(coro, "close"):
            coro.close()
        raise asyncio.TimeoutError

    with patch.object(
        grabber, "_resolve_source", AsyncMock(return_value="http://127.0.0.1:8123/x")
    ), patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec), patch.dict(
        sys.modules, {"homeassistant.components.ffmpeg": fake_ffmpeg_module}
    ), patch("asyncio.wait_for", fake_wait_for):
        frame = await grabber._capture_one_frame()

    assert frame is None
    fake_proc.kill.assert_called_once()
    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(warnings) == 1
    assert str(int(_CAPTURE_TIMEOUT)) in warnings[0].message


@pytest.mark.asyncio
async def test_capture_one_frame_empty_stdout_logs_stderr(caplog):
    """ffmpeg завершился без кадра — в лог попадает код возврата и stderr."""
    grabber, _hass = _make_grabber()
    caplog.set_level(logging.WARNING, logger=_LOGGER_NAME)

    fake_proc = MagicMock()
    fake_proc.returncode = 1
    fake_proc.communicate = AsyncMock(return_value=(b"", b"Server returned 404 Not Found\n"))

    async def fake_create_subprocess_exec(*_args, **_kwargs):
        return fake_proc

    fake_ffmpeg_module = types.SimpleNamespace(
        get_ffmpeg_manager=lambda _hass: MagicMock(binary="ffmpeg")
    )

    with patch.object(
        grabber, "_resolve_source", AsyncMock(return_value="http://127.0.0.1:8123/x")
    ), patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec), patch.dict(
        sys.modules, {"homeassistant.components.ffmpeg": fake_ffmpeg_module}
    ):
        frame = await grabber._capture_one_frame()

    assert frame is None
    warnings = [r for r in caplog.records if r.name == _LOGGER_NAME]
    assert len(warnings) == 1
    assert "404" in warnings[0].message
