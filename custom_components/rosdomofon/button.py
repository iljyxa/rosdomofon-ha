from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    async_add_entities([SmartDomofonButton(entry)])

class SmartDomofonButton(ButtonEntity):
    def __init__(self, entry):
        self._entry = entry
        self._attr_name = "Open Door"
        self._attr_unique_id = f"{entry.entry_id}_open_door"

    async def async_press(self):
        # Здесь должен быть вызов API домофона для открытия двери
        token = self._entry.data["token"]
        # requests.post("https://domofon-api/open", headers={"Authorization": token})
        self.hass.components.persistent_notification.async_create(
            "Door opened!", title="Smart Domofon"
        )