from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    # Регистрируем кнопку и камеру (если нужно)
    hass.async_create_task(
        hass.config_entries.async_forward_entry_setup(entry, "button")
    )
    hass.async_create_task(
        hass.config_entries.async_forward_entry_setup(entry, "camera")
    )

    return True