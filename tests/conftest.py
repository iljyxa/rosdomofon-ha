"""
Пирятуры для тестов интеграции rosdomofon-ha.

Содержит фикстуры для настройки тестового окружения Home Assistant.
"""

import pytest
import pytest_asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rosdomofon.const import DOMAIN


@pytest_asyncio.fixture
async def hass():
    """Фикстура HomeAssistant из pytest-homeassistant-custom-component.

    Используем встроенную async-функциональную фикстуру, чтобы в тесты
    приходил готовый объект HomeAssistant, а не async_generator.
    """
    from pytest_homeassistant_custom_component.common import (  # type: ignore
        async_test_home_assistant,
    )

    hass = await async_test_home_assistant()
    try:
        yield hass
    finally:
        await hass.async_stop()


@pytest.fixture
def mock_access_token():
    """Фикстура для моковых токенов доступа."""
    return "test_access_token_12345"


@pytest.fixture
def mock_refresh_token():
    """Фикстура для моковых refresh токенов."""
    return "test_refresh_token_67890"


@pytest.fixture
def mock_phone_number():
    """Фикстура для тестового номера телефона."""
    return "+79991234567"


@pytest.fixture
def mock_config_entry(mock_phone_number, mock_access_token, mock_refresh_token):
    """Фикстура для тестового config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        data={
            "phone": mock_phone_number,
            "access_token": mock_access_token,
            "refresh_token": mock_refresh_token,
        },
        unique_id=mock_phone_number,
        title=f"Росдомофон ({mock_phone_number})",
    )


@pytest.fixture
def mock_locks_data():
    """Фикстура для моковых данных замков."""
    return [
        {
            "adapterId": "12345",
            "relay": 1,
            "type": 1,  # Дверь подъезда
            "name": "Дверь подъезда",
        },
        {
            "adapterId": "12345",
            "relay": 2,
            "type": 2,  # Шлагбаум
            "name": "Шлагбаум",
        },
        {
            "adapterId": "67890",
            "relay": 1,
            "type": 3,  # Ворота
            "name": "Ворота",
        },
    ]


@pytest.fixture
def mock_cameras_data():
    """Фикстура для моковых данных камер."""
    return [
        {
            "id": "39167",
            "name": "Камера подъезд",
            "model": "RD-101",
        },
        {
            "id": "39168",
            "name": "Камера двор",
            "model": "RD-102",
        },
    ]


@pytest.fixture
def mock_camera_details():
    """Фикстура для детальной информации о камере."""
    return {
        "id": "39167",
        "name": "Камера подъезд",
        "model": "RD-101",
        "rdva": {
            "uri": "rdva68.rosdomofon.com",
        },
    }


@pytest.fixture
def mock_requests_get():
    """Фикстура для мока requests.get."""
    with patch("requests.get") as mock_get:
        yield mock_get


@pytest.fixture
def mock_requests_post():
    """Фикстура для мока requests.post."""
    with patch("requests.post") as mock_post:
        yield mock_post


@pytest_asyncio.fixture
async def mock_token_manager(hass: HomeAssistant, mock_config_entry, mock_access_token):
    """Фикстура для мока TokenManager."""
    from custom_components.rosdomofon.token_manager import TokenManager

    with patch.object(TokenManager, "ensure_valid_token", return_value=True):
        manager = TokenManager(hass, mock_config_entry)
        manager.access_token = mock_access_token
        yield manager


@pytest_asyncio.fixture
async def mock_share_manager(hass: HomeAssistant):
    """Фикстура для мока ShareLinkManager."""
    from custom_components.rosdomofon.share import ShareLinkManager

    manager = ShareLinkManager(hass)
    with patch.object(manager, "get_external_url", return_value="https://example.com"):
        yield manager


@pytest_asyncio.fixture
async def setup_integration(hass: HomeAssistant, mock_config_entry):
    """Фикстура для настройки интеграции в тестовом окружении."""
    mock_config_entry.add_to_hass(hass)

    # Мокаем все HTTP запросы
    with patch("custom_components.rosdomofon.lock._fetch_keys", return_value=[]), \
         patch("custom_components.rosdomofon.button._fetch_keys", return_value=[]), \
         patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=[]), \
         patch("custom_components.rosdomofon.token_manager.TokenManager.ensure_valid_token", return_value=True):

        await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()

    yield mock_config_entry

    await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
