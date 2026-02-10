"""Тесты для платформы lock."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.core import HomeAssistant
from homeassistant.const import STATE_LOCKED, STATE_UNLOCKED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rosdomofon.const import DOMAIN


@pytest.mark.asyncio
async def test_lock_setup(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест настройки замков."""
    mock_config_entry.add_to_hass(hass)
    
    # Подготовка hass.data
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }
    
    with patch("custom_components.rosdomofon.lock._fetch_keys", return_value=mock_locks_data):
        from custom_components.rosdomofon.lock import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        # Проверяем, что созданы все замки
        assert len(entities) == 3
        assert entities[0]._attr_unique_id == "rosdomofon_12345_1"
        assert entities[1]._attr_unique_id == "rosdomofon_12345_2"
        assert entities[2]._attr_unique_id == "rosdomofon_67890_1"


@pytest.mark.asyncio
async def test_lock_unlock(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест открытия замка."""
    mock_config_entry.add_to_hass(hass)
    
    mock_token_manager = MagicMock(
        ensure_valid_token=AsyncMock(return_value=True),
        access_token="test_token"
    )
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": mock_token_manager
        }
    }
    
    with patch("custom_components.rosdomofon.lock._fetch_keys", return_value=mock_locks_data), \
         patch("custom_components.rosdomofon.lock._activate_key", return_value=True) as mock_activate:
        
        from custom_components.rosdomofon.lock import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        lock_entity = entities[0]
        lock_entity.hass = hass
        
        # Начальное состояние - закрыт
        assert lock_entity.is_locked is True
        
        # Открываем замок
        await lock_entity.async_unlock()
        
        # Проверяем, что замок открыт
        assert lock_entity.is_locked is False
        mock_activate.assert_called_once_with("test_token", "12345", 1)
        
        # Проверяем, что таймер автоблокировки запущен
        assert lock_entity._unlock_timer is not None


@pytest.mark.asyncio
async def test_lock_auto_lock(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест автоматической блокировки."""
    mock_config_entry.add_to_hass(hass)
    
    mock_token_manager = MagicMock(
        ensure_valid_token=AsyncMock(return_value=True),
        access_token="test_token"
    )
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": mock_token_manager
        }
    }
    
    with patch("custom_components.rosdomofon.lock._fetch_keys", return_value=mock_locks_data), \
         patch("custom_components.rosdomofon.lock._activate_key", return_value=True):
        
        from custom_components.rosdomofon.lock import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        lock_entity = entities[0]
        lock_entity.hass = hass
        
        # Открываем замок
        await lock_entity.async_unlock()
        assert lock_entity.is_locked is False
        
        # Вручную вызываем callback автоблокировки
        lock_entity._async_auto_lock(None)
        
        # Проверяем, что замок закрылся
        assert lock_entity.is_locked is True
        assert lock_entity._unlock_timer is None


@pytest.mark.asyncio
async def test_lock_unlock_failure(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест неудачного открытия замка."""
    mock_config_entry.add_to_hass(hass)
    
    mock_token_manager = MagicMock(
        ensure_valid_token=AsyncMock(return_value=True),
        access_token="test_token"
    )
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": mock_token_manager
        }
    }
    
    with patch("custom_components.rosdomofon.lock._fetch_keys", return_value=mock_locks_data), \
         patch("custom_components.rosdomofon.lock._activate_key", return_value=False) as mock_activate:
        
        from custom_components.rosdomofon.lock import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        lock_entity = entities[0]
        lock_entity.hass = hass
        
        # Пытаемся открыть замок
        await lock_entity.async_unlock()
        
        # Замок должен остаться закрытым
        assert lock_entity.is_locked is True
        assert lock_entity._unlock_timer is None
        mock_activate.assert_called_once()


@pytest.mark.asyncio
async def test_lock_token_refresh_failure(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест открытия замка при неудаче обновления токена."""
    mock_config_entry.add_to_hass(hass)
    
    mock_token_manager = MagicMock(
        ensure_valid_token=AsyncMock(return_value=False),
        access_token="test_token"
    )
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": mock_token_manager
        }
    }
    
    with patch("custom_components.rosdomofon.lock._fetch_keys", return_value=mock_locks_data):
        from custom_components.rosdomofon.lock import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        lock_entity = entities[0]
        lock_entity.hass = hass
        
        # Пытаемся открыть замок
        await lock_entity.async_unlock()
        
        # Замок должен остаться закрытым
        assert lock_entity.is_locked is True


@pytest.mark.asyncio
async def test_lock_device_names(hass: HomeAssistant, mock_config_entry, mock_locks_data):
    """Тест корректных названий для разных типов устройств."""
    mock_config_entry.add_to_hass(hass)
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }
    
    with patch("custom_components.rosdomofon.lock._fetch_keys", return_value=mock_locks_data):
        from custom_components.rosdomofon.lock import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        # Проверяем названия
        assert entities[0]._attr_name == "Дверь подъезда"
        assert entities[1]._attr_name == "Шлагбаум"
        assert entities[2]._attr_name == "Ворота"
        
        # Проверяем иконки
        assert entities[0]._attr_icon == "mdi:door-closed"
        assert entities[1]._attr_icon == "mdi:gate"
        assert entities[2]._attr_icon == "mdi:garage"
