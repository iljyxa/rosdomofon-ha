"""Тесты для платформы button."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rosdomofon.const import DOMAIN


@pytest.mark.asyncio
async def test_button_setup(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест настройки кнопок."""
    mock_config_entry.add_to_hass(hass)
    
    mock_share_manager = MagicMock()
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token"),
            "share_manager": mock_share_manager
        }
    }
    
    with patch("custom_components.rosdomofon.button._fetch_keys", return_value=mock_locks_data):
        from custom_components.rosdomofon.button import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        # Проверяем, что созданы все кнопки
        assert len(entities) == 3
        assert entities[0]._attr_unique_id == "rosdomofon_share_12345_1"
        assert entities[0]._attr_name == "Поделиться: Дверь подъезда"
        assert entities[0]._attr_icon == "mdi:share-variant"


@pytest.mark.asyncio
async def test_button_press_success(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест успешного нажатия кнопки."""
    mock_config_entry.add_to_hass(hass)
    
    mock_share_manager = MagicMock()
    mock_share_manager.generate.return_value = "https://example.com/share/test123"
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token"),
            "share_manager": mock_share_manager
        }
    }
    
    with patch("custom_components.rosdomofon.button._fetch_keys", return_value=mock_locks_data), \
         patch("homeassistant.helpers.entity_registry.async_get") as mock_registry:
        
        # Мокаем entity registry
        mock_er = MagicMock()
        mock_er.async_get_entity_id.return_value = "lock.rosdomofon_12345_1"
        mock_registry.return_value = mock_er
        
        from custom_components.rosdomofon.button import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        button_entity = entities[0]
        button_entity.hass = hass
        
        # Нажимаем кнопку
        await button_entity.async_press()
        
        # Проверяем, что была вызвана генерация ссылки
        mock_share_manager.generate.assert_called_once()


@pytest.mark.asyncio
async def test_button_press_lock_not_found(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест нажатия кнопки, когда замок не найден."""
    mock_config_entry.add_to_hass(hass)
    
    mock_share_manager = MagicMock()
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token"),
            "share_manager": mock_share_manager
        }
    }
    
    with patch("custom_components.rosdomofon.button._fetch_keys", return_value=mock_locks_data), \
         patch("homeassistant.helpers.entity_registry.async_get") as mock_registry:
        
        # Мокаем entity registry - замок не найден
        mock_er = MagicMock()
        mock_er.async_get_entity_id.return_value = None
        mock_registry.return_value = mock_er
        
        from custom_components.rosdomofon.button import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        button_entity = entities[0]
        button_entity.hass = hass
        
        # Нажимаем кнопку и ожидаем ошибку
        with pytest.raises(HomeAssistantError, match="не найден в реестре"):
            await button_entity.async_press()


@pytest.mark.asyncio
async def test_button_press_external_url_not_available(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест нажатия кнопки без внешнего URL."""
    mock_config_entry.add_to_hass(hass)
    
    from custom_components.rosdomofon.share import ExternalURLNotAvailable
    
    mock_share_manager = MagicMock()
    mock_share_manager.generate.side_effect = ExternalURLNotAvailable()
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token"),
            "share_manager": mock_share_manager
        }
    }
    
    with patch("custom_components.rosdomofon.button._fetch_keys", return_value=mock_locks_data), \
         patch("homeassistant.helpers.entity_registry.async_get") as mock_registry:
        
        mock_er = MagicMock()
        mock_er.async_get_entity_id.return_value = "lock.rosdomofon_12345_1"
        mock_registry.return_value = mock_er
        
        from custom_components.rosdomofon.button import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        button_entity = entities[0]
        button_entity.hass = hass
        
        # Нажимаем кнопку и ожидаем ошибку
        with pytest.raises(HomeAssistantError, match="не настроен доступ извне"):
            await button_entity.async_press()


@pytest.mark.asyncio
async def test_button_setup_token_failure(hass: HomeAssistant, mock_config_entry):
    """Тест настройки кнопок при неудаче обновления токена."""
    mock_config_entry.add_to_hass(hass)
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=False)),
            "share_manager": MagicMock()
        }
    }
    
    from custom_components.rosdomofon.button import async_setup_entry
    
    entities = []
    await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
    
    # Проверяем, что кнопки не были созданы
    assert len(entities) == 0
