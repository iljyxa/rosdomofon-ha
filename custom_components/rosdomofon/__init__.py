"""
Интеграция Росдомофон для Home Assistant.

Обеспечивает управление замками (двери, шлагбаумы, ворота, калитки)
через облачный API Росдомофон.
"""

import logging

from .const import DOMAIN
from .token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["lock"]


async def async_setup_entry(hass, entry) -> bool:
    """Настройка интеграции при добавлении config entry."""
    token_manager = TokenManager(hass, entry)

    if not await token_manager.ensure_valid_token():
        _LOGGER.error("Не удалось обновить токен при старте")
        return False

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "token_manager": token_manager,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Выгрузка интеграции при удалении config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)

    return unload_ok
