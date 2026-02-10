"""Тесты для share link manager."""

import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from aiohttp import web
from homeassistant.core import HomeAssistant

from custom_components.rosdomofon.share import ShareLinkManager, ShareLink, ExternalURLNotAvailable


async def test_share_manager_generate_link(hass: HomeAssistant):
    """Тест генерации гостевой ссылки."""
    manager = ShareLinkManager(hass)

    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register") as mock_register, \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:

        mock_call_later.return_value = MagicMock()

        entity_id = "lock.rosdomofon_12345_1"
        ttl_hours = 12

        url = manager.generate(entity_id, ttl_hours)

        assert url.startswith("https://example.com/api/webhook/")
        assert "rosdomofon_share_" in url

        mock_register.assert_called_once()

        assert len(manager._links) == 1


async def test_share_manager_generate_no_external_url(hass: HomeAssistant):
    """Тест генерации ссылки без внешнего URL."""
    manager = ShareLinkManager(hass)

    with patch.object(manager, "get_external_url", return_value=None):
        entity_id = "lock.rosdomofon_12345_1"

        with pytest.raises(ExternalURLNotAvailable):
            manager.generate(entity_id, 12)


async def test_share_manager_revoke_link(hass: HomeAssistant):
    """Тест отзыва гостевой ссылки."""
    manager = ShareLinkManager(hass)

    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"), \
         patch("custom_components.rosdomofon.share.webhook.async_unregister") as mock_unregister, \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:

        mock_call_later.return_value = MagicMock()

        url = manager.generate("lock.rosdomofon_12345_1", 12)
        webhook_id = url.split("/")[-1]

        assert len(manager._links) == 1

        manager.revoke(webhook_id)

        assert len(manager._links) == 0
        mock_unregister.assert_called_once_with(hass, webhook_id)


async def test_share_manager_revoke_all(hass: HomeAssistant):
    """Тест отзыва всех гостевых ссылок."""
    manager = ShareLinkManager(hass)

    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"), \
         patch("custom_components.rosdomofon.share.webhook.async_unregister") as mock_unregister, \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:

        mock_call_later.return_value = MagicMock()

        manager.generate("lock.rosdomofon_12345_1", 12)
        manager.generate("lock.rosdomofon_12345_2", 12)
        manager.generate("lock.rosdomofon_67890_1", 12)

        assert len(manager._links) == 3

        manager.revoke_all()

        assert len(manager._links) == 0
        assert mock_unregister.call_count == 3


def test_share_link_expiration():
    """Тест истечения срока действия ссылки (чисто синхронная логика)."""
    link = ShareLink(
        webhook_id="test_webhook",
        entity_id="lock.test",
        created_at=time.time() - 7200,  # создана 2 часа назад
        ttl_hours=1,  # TTL 1 час
    )

    # Ссылка создана 2 часа назад, TTL 1 час — уже истекла
    assert link.is_expired is True

    # Создаём свежую ссылку
    fresh_link = ShareLink(
        webhook_id="test_webhook_2",
        entity_id="lock.test",
        ttl_hours=1,
    )

    assert fresh_link.is_expired is False


async def test_webhook_handler_success(hass: HomeAssistant):
    """Тест успешной обработки webhook запроса."""
    manager = ShareLinkManager(hass)

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

        hass.services.async_call.assert_awaited_once_with(
            "lock",
            "unlock",
            {"entity_id": "lock.rosdomofon_12345_1"},
            blocking=True,
        )

        assert response.status == 200


async def test_webhook_handler_expired_link(hass: HomeAssistant):
    """Тест обработки истёкшей ссылки."""
    manager = ShareLinkManager(hass)

    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"), \
         patch("homeassistant.helpers.event.async_call_later") as mock_call_later:

        mock_call_later.return_value = MagicMock()

        url = manager.generate("lock.rosdomofon_12345_1", 0.001)
        webhook_id = url.split("/")[-1]

        # Помечаем ссылку как истёкшую через сдвиг created_at
        link = manager._links[webhook_id]
        link.created_at = time.time() - 7200  # 2 часа назад

        request = MagicMock()
        request.app = {"hass": hass}
        request.match_info = {"webhook_id": webhook_id}

        response = await manager._handle_webhook(hass, webhook_id, request)

        assert response.status == 410


async def test_webhook_handler_entity_not_found(hass: HomeAssistant):
    """Тест обработки webhook когда сущность не найдена."""
    manager = ShareLinkManager(hass)

    hass.states.get = MagicMock(return_value=None)

    with patch.object(manager, "get_external_url", return_value="https://example.com"), \
         patch("custom_components.rosdomofon.share.webhook.async_register"):

        request = MagicMock()
        request.app = {"hass": hass}
        request.match_info = {"webhook_id": "test_webhook"}

        response = await manager.webhook_handler(request)

        assert response.status == 404
