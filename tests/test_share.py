"""Тесты для share link manager."""

import pytest
import time
from unittest.mock import patch, MagicMock, AsyncMock
from aiohttp import web
from homeassistant.core import HomeAssistant

from custom_components.rosdomofon.share import ShareLinkManager, ExternalURLNotAvailable


@pytest.mark.asyncio
async def test_share_manager_generate_link(hass: HomeAssistant):
    """Тест генерации гостевой ссылки."""
    manager = ShareLinkManager(hass)
    
    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register") as mock_register, \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:
        
        # Не запускаем настоящий планировщик hass, возвращаем просто мок
        mock_call_later.return_value = MagicMock()
        
        entity_id = "lock.rosdomofon_12345_1"
        ttl_hours = 12
        
        url = manager.generate(entity_id, ttl_hours)
        
        # Проверяем, что URL сгенерирован
        assert url.startswith("https://example.com/api/webhook/")
        assert "rosdomofon_share_" in url
        
        # Проверяем, что webhook зарегистрирован
        mock_register.assert_called_once()
        
        # Проверяем, что ссылка добавлена в manager
        assert len(manager._links) == 1


@pytest.mark.asyncio
async def test_share_manager_generate_no_external_url(hass: HomeAssistant):
    """Тест генерации ссылки без внешнего URL."""
    manager = ShareLinkManager(hass)
    
    with patch.object(manager, "get_external_url", return_value=None):
        entity_id = "lock.rosdomofon_12345_1"
        
        with pytest.raises(ExternalURLNotAvailable):
            manager.generate(entity_id, 12)


@pytest.mark.asyncio
async def test_share_manager_revoke_link(hass: HomeAssistant):
    """Тест отзыва гостевой ссылки."""
    manager = ShareLinkManager(hass)
    
    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"), \
         patch("custom_components.rosdomofon.share.webhook.async_unregister") as mock_unregister, \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:
        
        mock_call_later.return_value = MagicMock()
        
        # Генерируем ссылку
        url = manager.generate("lock.rosdomofon_12345_1", 12)
        webhook_id = url.split("/")[-1]
        
        assert len(manager._links) == 1
        
        # Отзываем ссылку
        manager.revoke(webhook_id)
        
        # Проверяем, что ссылка удалена
        assert len(manager._links) == 0
        mock_unregister.assert_called_once_with(hass, webhook_id)


@pytest.mark.asyncio
async def test_share_manager_revoke_all(hass: HomeAssistant):
    """Тест отзыва всех гостевых ссылок."""
    manager = ShareLinkManager(hass)
    
    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"), \
         patch("custom_components.rosdomofon.share.webhook.async_unregister") as mock_unregister, \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:
        
        mock_call_later.return_value = MagicMock()
        
        # Генерируем несколько ссылок
        manager.generate("lock.rosdomofon_12345_1", 12)
        manager.generate("lock.rosdomofon_12345_2", 12)
        manager.generate("lock.rosdomofon_67890_1", 12)
        
        assert len(manager._links) == 3
        
        # Отзываем все ссылки
        manager.revoke_all()
        
        # Проверяем, что все ссылки удалены
        assert len(manager._links) == 0
        assert mock_unregister.call_count == 3


@pytest.mark.asyncio
async def test_share_link_expiration():
    """Тест истечения срока действия ссылки (чисто синхронная логика)."""
    from custom_components.rosdomofon.share import ShareLink
    
    # Создаём ссылку с коротким TTL
    link = ShareLink(
        webhook_id="test_webhook",
        entity_id="lock.test",
        ttl_hours=0.001  # ~3.6 секунды
    )
    
    # Сразу после создания ссылка действительна
    assert link.is_expired is False
    
    # Ждём истечения в обычном потоке, вне event loop Home Assistant
    time.sleep(4)
    
    # Теперь ссылка должна быть недействительной
    assert link.is_expired is True


@pytest.mark.asyncio
async def test_webhook_handler_success(hass: HomeAssistant):
    """Тест успешной обработки webhook запроса."""
    manager = ShareLinkManager(hass)
    
    # Добавляем mock состояние
    mock_state = MagicMock()
    mock_state.name = "Дверь подъезда"
    hass.states.get = MagicMock(return_value=mock_state)
    hass.services.async_call = AsyncMock()
    
    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"):
        
        request = MagicMock()
        request.app = {"hass": hass}
        request.match_info = {"webhook_id": "test_webhook"}
        
        response = await manager.webhook_handler(request)
        
        # Проверяем, что сервис открытия замка вызван
        hass.services.async_call.assert_awaited_once_with(
            "lock",
            "unlock",
            {"entity_id": "lock.rosdomofon_12345_1"},
            blocking=True,
        )
        
        assert response.status == 200


@pytest.mark.asyncio
async def test_webhook_handler_expired_link(hass: HomeAssistant):
    """Тест обработки истёкшей ссылки."""
    manager = ShareLinkManager(hass)
    
    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"), \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:
        
        mock_call_later.return_value = MagicMock()
        
        # Генерируем ссылку с коротким TTL
        url = manager.generate("lock.rosdomofon_12345_1", 0.001)
        webhook_id = url.split("/")[-1]
        
        # Помечаем ссылку как истёкшую
        link = manager._links[webhook_id]
        link.expires_at = link.expires_at.replace(year=2000)
        
        request = MagicMock()
        request.app = {"hass": hass}
        request.match_info = {"webhook_id": webhook_id}
        
        response = await manager.webhook_handler(request)
        
        assert response.status == 410


@pytest.mark.asyncio
async def test_webhook_handler_entity_not_found(hass: HomeAssistant):
    """Тест обработки webhook когда сущность не найдена."""
    manager = ShareLinkManager(hass)
    
    # Mock: сущность не найдена
    hass.states.get = MagicMock(return_value=None)
    
    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"):
        
        request = MagicMock()
        request.app = {"hass": hass}
        request.match_info = {"webhook_id": "test_webhook"}
        
        response = await manager.webhook_handler(request)
        
        assert response.status == 404
