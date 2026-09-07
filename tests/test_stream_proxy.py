"""Тесты для stream proxy (host/SSRF защиты)."""

from unittest.mock import MagicMock, AsyncMock, patch

import pytest
from aiohttp import web
from homeassistant.core import HomeAssistant

from custom_components.rosdomofon.const import DOMAIN
from custom_components.rosdomofon.stream_proxy import RosdomofonStreamProxyView


@pytest.mark.asyncio
async def test_stream_proxy_rejects_unknown_camera(hass: HomeAssistant):
    """Неизвестная камера должна вернуть 404."""
    hass.data[DOMAIN] = {
        "_camera_hosts": {},
        "entry_1": {
            "token_manager": MagicMock(
                ensure_valid_token=AsyncMock(return_value=True),
                access_token="test_token",
            )
        },
    }

    view = RosdomofonStreamProxyView(hass)
    response = await view.get(
        MagicMock(), "39167", "s.rdva68.rosdomofon.com", "live/39167.m3u8"
    )

    assert response.status == 404


@pytest.mark.asyncio
async def test_stream_proxy_rejects_invalid_host(hass: HomeAssistant):
    """Host не совпадает с ожидаемым для камеры => 403."""
    hass.data[DOMAIN] = {
        "_camera_hosts": {"39167": "s.rdva68.rosdomofon.com"},
        "entry_1": {
            "token_manager": MagicMock(
                ensure_valid_token=AsyncMock(return_value=True),
                access_token="test_token",
            )
        },
    }

    view = RosdomofonStreamProxyView(hass)
    response = await view.get(
        MagicMock(), "39167", "evil.com", "live/39167.m3u8"
    )

    assert response.status == 403


@pytest.mark.asyncio
async def test_stream_proxy_rejects_non_rosdomofon_host(hass: HomeAssistant):
    """Даже совпадающий host должен быть в домене rosdomofon.com."""
    hass.data[DOMAIN] = {
        "_camera_hosts": {"39167": "s.example.com"},
        "entry_1": {
            "token_manager": MagicMock(
                ensure_valid_token=AsyncMock(return_value=True),
                access_token="test_token",
            )
        },
    }

    view = RosdomofonStreamProxyView(hass)
    response = await view.get(
        MagicMock(), "39167", "s.example.com", "live/39167.m3u8"
    )

    assert response.status == 403


@pytest.mark.asyncio
async def test_stream_proxy_allows_expected_host(hass: HomeAssistant):
    """Корректный host для камеры должен проходить."""
    hass.data[DOMAIN] = {
        "_camera_hosts": {"39167": "s.rdva68.rosdomofon.com"},
        "entry_1": {
            "token_manager": MagicMock(
                ensure_valid_token=AsyncMock(return_value=True),
                access_token="test_token",
            )
        },
    }

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"Content-Type": "application/vnd.apple.mpegurl"}
    mock_response.text = "#EXTM3U\nsegment.ts"
    mock_response.content = b""

    with patch("requests.get", return_value=mock_response):
        view = RosdomofonStreamProxyView(hass)
        response = await view.get(
            MagicMock(), "39167", "s.rdva68.rosdomofon.com", "live/39167.m3u8"
        )

    assert isinstance(response, web.Response)
    assert response.status == 200


@pytest.mark.asyncio
async def test_stream_proxy_rewrites_playlist_urls_with_queries(hass: HomeAssistant):
    """Прокси должен сохранять query string у HLS URI."""
    view = RosdomofonStreamProxyView(hass)

    content = await view._rewrite_playlist_urls(
        '#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin?token=1"\nsegment.ts?token=2',
        "39167",
        "s.rdva68.rosdomofon.com",
        "live/39167.m3u8",
    )

    assert (
        "/api/rosdomofon/stream/39167/s.rdva68.rosdomofon.com/live/key.bin?token=1"
        in content
    )
    assert (
        "/api/rosdomofon/stream/39167/s.rdva68.rosdomofon.com/live/segment.ts?token=2"
        in content
    )


@pytest.mark.asyncio
async def test_stream_proxy_signs_rewritten_playlist_urls(hass: HomeAssistant):
    """Переписанные HLS URI должны нормализоваться и подписываться."""
    view = RosdomofonStreamProxyView(hass)

    content = await view._rewrite_playlist_urls(
        "#EXTM3U\n../dllive/39167/088307.ts",
        "39167",
        "s.rdva68.rosdomofon.com",
        "dllive/39167/index.m3u8",
    )

    assert "/api/rosdomofon/stream/39167/s.rdva68.rosdomofon.com/dllive/39167/088307.ts" in content
    assert "../" not in content
    assert "sig=" in content


def test_sign_and_validate_roundtrip(hass: HomeAssistant):
    """Путь, подписанный sign_proxy_path, должен успешно проходить проверку."""
    from custom_components.rosdomofon.stream_proxy import (
        _validate_proxy_request,
        sign_proxy_path,
    )

    path = "/api/rosdomofon/stream/39167/s.rdva68.rosdomofon.com/live/39167.m3u8"
    signed = sign_proxy_path(hass, path)
    sig = signed.split("sig=", 1)[1]

    request = MagicMock()
    request.get.return_value = False
    request.query = {"sig": sig}
    request.path = path

    assert _validate_proxy_request(hass, request) is True


def test_validate_rejects_missing_or_wrong_signature(hass: HomeAssistant):
    """Отсутствующая или неверная подпись отклоняется."""
    from custom_components.rosdomofon.stream_proxy import _validate_proxy_request

    path = "/api/rosdomofon/stream/39167/s.rdva68.rosdomofon.com/live/39167.m3u8"

    no_sig = MagicMock(path=path, query={})
    no_sig.get.return_value = False
    assert _validate_proxy_request(hass, no_sig) is False

    wrong_sig = MagicMock(path=path, query={"sig": "deadbeef"})
    wrong_sig.get.return_value = False
    assert _validate_proxy_request(hass, wrong_sig) is False
