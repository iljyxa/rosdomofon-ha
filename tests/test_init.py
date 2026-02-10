"""Тесты для __init__.py интеграции rosdomofon."""

import pytest
from unittest.mock import patch, MagicMock
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.rosdomofon.const import DOMAIN

pytestmark = pytest.mark.asyncio


async def test_setup_entry_success(hass: HomeAssistant, mock_config_entry):
    """Тест успешной настройки интеграции."""
    mock_config_entry.add_to_hass(hass)

    with patch("custom_components.rosdomofon.TokenManager") as mock_token_manager, \
         patch("custom_components.rosdomofon.setup_stream_proxy") as mock_setup_proxy, \
         patch.object(hass.config_entries, "async_forward_entry_setups") as mock_forward:

        mock_tm_instance = MagicMock()
        mock_tm_instance.ensure_valid_token.return_value = True
        mock_tm_instance.access_token = "test_token"
        mock_token_manager.return_value = mock_tm_instance

        from custom_components.rosdomofon import async_setup_entry
        result = await async_setup_entry(hass, mock_config_entry)

        assert result is True
        assert DOMAIN in hass.data
        assert mock_config_entry.entry_id in hass.data[DOMAIN]
        assert "token_manager" in hass.data[DOMAIN][mock_config_entry.entry_id]
        assert "share_manager" in hass.data[DOMAIN][mock_config_entry.entry_id]

        mock_setup_proxy.assert_called_once()
        mock_forward.assert_called_once()


async def test_setup_entry_token_failure(hass: HomeAssistant, mock_config_entry):
    """Тест неудачной настройки из-за проблем с токеном."""
    mock_config_entry.add_to_hass(hass)

    with patch("custom_components.rosdomofon.TokenManager") as mock_token_manager:
        mock_tm_instance = MagicMock()
        mock_tm_instance.ensure_valid_token.return_value = False
        mock_token_manager.return_value = mock_tm_instance

        from custom_components.rosdomofon import async_setup_entry

        with pytest.raises(ConfigEntryNotReady):
            await async_setup_entry(hass, mock_config_entry)


async def test_setup_entry_proxy_registered_once(hass: HomeAssistant, mock_config_entry):
    """Тест, что прокси регистрируется только один раз."""
    mock_config_entry.add_to_hass(hass)

    mock_config_entry2 = MockConfigEntry(
        domain=DOMAIN,
        data={
            "phone": "+79999999999",
            "access_token": "test_token_2",
            "refresh_token": "test_refresh_2",
        },
        unique_id="+79999999999",
    )
    mock_config_entry2.add_to_hass(hass)

    with patch("custom_components.rosdomofon.TokenManager") as mock_token_manager, \
         patch("custom_components.rosdomofon.setup_stream_proxy") as mock_setup_proxy, \
         patch.object(hass.config_entries, "async_forward_entry_setups"):

        mock_tm_instance = MagicMock()
        mock_tm_instance.ensure_valid_token.return_value = True
        mock_tm_instance.access_token = "test_token"
        mock_token_manager.return_value = mock_tm_instance

        from custom_components.rosdomofon import async_setup_entry

        await async_setup_entry(hass, mock_config_entry)
        assert mock_setup_proxy.call_count == 1

        await async_setup_entry(hass, mock_config_entry2)
        assert mock_setup_proxy.call_count == 1


async def test_unload_entry(hass: HomeAssistant, mock_config_entry):
    """Тест выгрузки интеграции."""
    mock_config_entry.add_to_hass(hass)

    with patch("custom_components.rosdomofon.TokenManager") as mock_token_manager, \
         patch("custom_components.rosdomofon.setup_stream_proxy"), \
         patch.object(hass.config_entries, "async_forward_entry_setups"), \
         patch.object(hass.config_entries, "async_unload_platforms", return_value=True):

        mock_tm_instance = MagicMock()
        mock_tm_instance.ensure_valid_token.return_value = True
        mock_tm_instance.access_token = "test_token"
        mock_token_manager.return_value = mock_tm_instance

        from custom_components.rosdomofon import async_setup_entry, async_unload_entry

        await async_setup_entry(hass, mock_config_entry)

        mock_share_manager = MagicMock()
        hass.data[DOMAIN][mock_config_entry.entry_id]["share_manager"] = mock_share_manager

        result = await async_unload_entry(hass, mock_config_entry)

        assert result is True
        mock_share_manager.revoke_all.assert_called_once()
        assert mock_config_entry.entry_id not in hass.data[DOMAIN]


async def test_service_generate_share_link(hass: HomeAssistant, mock_config_entry):
    """Тест сервиса генерации гостевых ссылок."""
    mock_config_entry.add_to_hass(hass)

    with patch("custom_components.rosdomofon.TokenManager") as mock_token_manager, \
         patch("custom_components.rosdomofon.setup_stream_proxy"), \
         patch.object(hass.config_entries, "async_forward_entry_setups"):

        mock_tm_instance = MagicMock()
        mock_tm_instance.ensure_valid_token.return_value = True
        mock_tm_instance.access_token = "test_token"
        mock_token_manager.return_value = mock_tm_instance

        from custom_components.rosdomofon import async_setup_entry

        await async_setup_entry(hass, mock_config_entry)

        mock_share_manager = MagicMock()
        mock_share_manager.generate.return_value = "https://example.com/share/link"
        hass.data[DOMAIN][mock_config_entry.entry_id]["share_manager"] = mock_share_manager

        assert hass.services.has_service(DOMAIN, "generate_share_link")

        await hass.services.async_call(
            DOMAIN,
            "generate_share_link",
            {"entity_id": "lock.rosdomofon_12345_1", "ttl_hours": 24},
            blocking=True,
        )

        mock_share_manager.generate.assert_called_once_with(
            "lock.rosdomofon_12345_1", 24
        )
