"""Тесты для платформы camera."""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from homeassistant.core import HomeAssistant

from custom_components.rosdomofon.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_camera_setup(hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details):
    """Тест настройки камер."""
    mock_config_entry.add_to_hass(hass)
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }
    
    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details):
        
        from custom_components.rosdomofon.camera import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        # Проверяем, что созданы камеры
        assert len(entities) == 2
        assert entities[0]._attr_unique_id == "rosdomofon_camera_39167"
        assert entities[0]._attr_name == "Камера подъезд"


async def test_camera_stream_source(hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details):
    """Тест получения stream source для камеры."""
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
    
    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details), \
         patch("custom_components.rosdomofon.camera.get_url", return_value="https://ha.example.com"):
        
        from custom_components.rosdomofon.camera import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        camera_entity = entities[0]
        camera_entity.hass = hass
        
        # Получаем stream source
        stream_source = await camera_entity.stream_source()
        
        # Проверяем, что URL проксируется
        assert stream_source is not None
        assert "https://ha.example.com/api/rosdomofon/stream/39167" in stream_source
        assert "rdva68.rosdomofon.com" in stream_source


async def test_camera_stream_source_token_failure(hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details):
    """Тест получения stream source при неудаче обновления токена."""
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
    
    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details):
        
        from custom_components.rosdomofon.camera import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        camera_entity = entities[0]
        camera_entity.hass = hass
        mock_token_manager.ensure_valid_token = AsyncMock(return_value=False)
        
        # Получаем stream source
        stream_source = await camera_entity.stream_source()
        
        # При неудаче обновления токена должен вернуть None
        assert stream_source is None


async def test_camera_setup_no_cameras(hass: HomeAssistant, mock_config_entry):
    """Тест настройки когда камер нет."""
    mock_config_entry.add_to_hass(hass)
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }
    
    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=[]):
        from custom_components.rosdomofon.camera import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        # Проверяем, что камеры не были созданы
        assert len(entities) == 0


async def test_camera_setup_invalid_camera_id(hass: HomeAssistant, mock_config_entry):
    """Тест настройки с некорректным camera_id."""
    mock_config_entry.add_to_hass(hass)
    
    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }
    
    # Камера без ID
    invalid_camera_data = [{"name": "Камера"}]
    
    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=invalid_camera_data):
        from custom_components.rosdomofon.camera import async_setup_entry
        
        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))
        
        # Проверяем, что камеры не были созданы
        assert len(entities) == 0


async def test_camera_image_returns_none(hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details):
    """Без запущенного читателя потока (сущность не добавлена в hass) — None."""
    mock_config_entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }

    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details):

        from custom_components.rosdomofon.camera import async_setup_entry

        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))

        camera_entity = entities[0]
        camera_entity.hass = hass

        # async_added_to_hass() не вызывался — _grabber ещё не создан
        image = await camera_entity.async_camera_image()

        assert image is None


async def test_camera_added_to_hass_creates_grabber(
    hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details
):
    """При добавлении сущности создаётся снимальщик кадра (без фонового процесса)."""
    mock_config_entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }

    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details), \
         patch("custom_components.rosdomofon.camera.StreamGrabber") as mock_grabber_cls:

        from custom_components.rosdomofon.camera import async_setup_entry

        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))

        camera_entity = entities[0]
        camera_entity.hass = hass
        camera_entity.entity_id = "camera.rosdomofon_camera_39167"

        mock_grabber_instance = mock_grabber_cls.return_value
        await camera_entity.async_added_to_hass()

        mock_grabber_cls.assert_called_once_with(hass, "camera.rosdomofon_camera_39167")
        assert camera_entity._grabber is mock_grabber_instance


async def test_camera_will_remove_from_hass_clears_grabber(
    hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details
):
    """При удалении сущности снимальщик отвязывается."""
    mock_config_entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }

    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details):

        from custom_components.rosdomofon.camera import async_setup_entry

        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))

        camera_entity = entities[0]
        camera_entity.hass = hass
        camera_entity._grabber = MagicMock()

        await camera_entity.async_will_remove_from_hass()

        assert camera_entity._grabber is None


async def test_camera_will_remove_from_hass_without_grabber_does_not_raise(
    hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details
):
    """Удаление сущности без созданного снимальщика (не дошло до added_to_hass)."""
    mock_config_entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }

    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details):

        from custom_components.rosdomofon.camera import async_setup_entry

        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))

        camera_entity = entities[0]
        camera_entity.hass = hass

        await camera_entity.async_will_remove_from_hass()  # не должно падать


async def test_camera_image_returns_frame_from_grabber(
    hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details
):
    """async_camera_image отдаёт кадр, полученный от снимальщика."""
    mock_config_entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }

    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details):

        from custom_components.rosdomofon.camera import async_setup_entry

        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))

        camera_entity = entities[0]
        camera_entity.hass = hass
        camera_entity._grabber = MagicMock(async_get_frame=AsyncMock(return_value=b"jpeg-bytes"))

        image = await camera_entity.async_camera_image()

        assert image == b"jpeg-bytes"


async def test_camera_image_none_when_grabber_capture_fails(
    hass: HomeAssistant, mock_config_entry, mock_cameras_data, mock_camera_details
):
    """Снимальщик создан, но захват не удался (и кэша нет) — None."""
    mock_config_entry.add_to_hass(hass)

    hass.data[DOMAIN] = {
        mock_config_entry.entry_id: {
            "token_manager": MagicMock(ensure_valid_token=AsyncMock(return_value=True), access_token="test_token")
        }
    }

    with patch("custom_components.rosdomofon.camera._fetch_cameras", return_value=mock_cameras_data), \
         patch("custom_components.rosdomofon.camera._fetch_camera_details", return_value=mock_camera_details):

        from custom_components.rosdomofon.camera import async_setup_entry

        entities = []
        await async_setup_entry(hass, mock_config_entry, lambda e: entities.extend(e))

        camera_entity = entities[0]
        camera_entity.hass = hass
        camera_entity._grabber = MagicMock(async_get_frame=AsyncMock(return_value=None))

        image = await camera_entity.async_camera_image()

        assert image is None
