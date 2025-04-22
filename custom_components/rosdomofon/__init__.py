import logging

from custom_components.rosdomofon.token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)


def __init__(self, device_type: int):
    self._attr_icon = {
        1: "mdi:door-closed",  # Дверь
        2: "mdi:gate",  # Шлагбаум
        3: "mdi:garage",  # Ворота
        4: "mdi:fence"  # Калитка
    }.get(device_type, "mdi:lock")


async def async_setup_entry(hass, entry):
    """Настройка интеграции."""
    # Инициализируем TokenManager
    token_manager = TokenManager(hass, entry)
    if not await token_manager.ensure_valid_token():
        _LOGGER.error("Не удалось обновить токен при старте")
        return False

    hass.data.setdefault("rosdomofon", {})
    hass.data["rosdomofon"][entry.entry_id] = {
        "token_manager": token_manager
    }

    # Настраиваем платформы
    await hass.config_entries.async_forward_entry_setups(entry, ["camera", "lock"])
    return True


async def _async_update_data():
    """Метод для обновления всех данных интеграции."""
    # Здесь можно реализовать проверку новых/удаленных устройств
    return {}
