"""Тесты для token_manager.py интеграции rosdomofon."""

import time

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rosdomofon.const import DOMAIN
from custom_components.rosdomofon.token_manager import TokenManager

pytestmark = pytest.mark.asyncio

TOKEN_URL = "https://rdba.rosdomofon.com/authserver-service/oauth/token"


def _entry(token_data: dict) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"phone": "79991234567", "token_data": token_data},
        unique_id="79991234567",
        title="Росдомофон (79991234567)",
    )
    return entry


def _fresh_token_data() -> dict:
    return {
        "access_token": "current_access_token",
        "refresh_token": "current_refresh_token",
        "expires_in": 3600,
        "timestamp": int(time.time()),
    }


def _expired_token_data() -> dict:
    return {
        "access_token": "old_access_token",
        "refresh_token": "old_refresh_token",
        "expires_in": 3600,
        "timestamp": int(time.time()) - 7200,
    }


async def test_ensure_valid_token_skips_refresh_when_not_expired(
    hass: HomeAssistant, aioclient_mock
):
    """Если токен ещё не истёк, обновление не запрашивается."""
    entry = _entry(_fresh_token_data())
    entry.add_to_hass(hass)
    manager = TokenManager(hass, entry)

    assert await manager.ensure_valid_token() is True
    assert manager.access_token == "current_access_token"
    assert len(aioclient_mock.mock_calls) == 0


async def test_ensure_valid_token_refreshes_expired_token(
    hass: HomeAssistant, aioclient_mock
):
    """Истёкший токен обновляется через aiohttp, результат сохраняется в entry."""
    aioclient_mock.post(
        TOKEN_URL,
        json={
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
            "token_type": "Bearer",
        },
        status=200,
    )
    entry = _entry(_expired_token_data())
    entry.add_to_hass(hass)
    manager = TokenManager(hass, entry)

    assert await manager.ensure_valid_token() is True
    assert manager.access_token == "new_access_token"
    # Обновлённые данные должны попасть в config entry, чтобы пережить перезапуск.
    assert entry.data["token_data"]["access_token"] == "new_access_token"
    assert entry.data["token_data"]["refresh_token"] == "new_refresh_token"
    assert "timestamp" in entry.data["token_data"]

    call = aioclient_mock.mock_calls[0]
    sent_data = call[2]
    assert sent_data["grant_type"] == "refresh_token"
    assert sent_data["refresh_token"] == "old_refresh_token"


async def test_ensure_valid_token_refresh_http_error(
    hass: HomeAssistant, aioclient_mock
):
    """Ошибка сервера (не 200) при обновлении токена не приводит к падению."""
    aioclient_mock.post(TOKEN_URL, status=401)
    entry = _entry(_expired_token_data())
    entry.add_to_hass(hass)
    manager = TokenManager(hass, entry)

    assert await manager.ensure_valid_token() is False
    # Старый токен не должен быть потерян при неудачном обновлении.
    assert manager.access_token == "old_access_token"


async def test_ensure_valid_token_refresh_network_error(
    hass: HomeAssistant, aioclient_mock
):
    """Сетевая ошибка при обновлении токена не приводит к падению."""
    aioclient_mock.post(TOKEN_URL, exc=TimeoutError)
    entry = _entry(_expired_token_data())
    entry.add_to_hass(hass)
    manager = TokenManager(hass, entry)

    assert await manager.ensure_valid_token() is False
    assert manager.access_token == "old_access_token"
