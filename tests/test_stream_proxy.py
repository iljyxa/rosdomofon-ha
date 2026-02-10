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
