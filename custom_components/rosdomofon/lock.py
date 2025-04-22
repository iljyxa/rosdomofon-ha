import logging
from typing import Any
from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
import requests

from .const import *
from .token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    """Настройка замков с автоматическим обновлением токена."""
    token_manager = TokenManager(hass, entry)
    if not await token_manager.ensure_valid_token():
        return

    try:
        keys = await hass.async_add_executor_job(
            lambda: _fetch_keys(token_manager.access_token)
        )
    except Exception as e:
        _LOGGER.error(f"Ошибка получения ключей: {e}")
        return

    entities = []
    for key in keys:
        entities.append(RosdomofonLock(
            hass=hass,
            token_manager=token_manager,
            adapter_id=key["adapterId"],
            relay=key["relay"],
            device_type=key["type"]
        ))

    async_add_entities(entities)


def _fetch_keys(access_token: str) -> list[dict]:
    """Получение списка ключей."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    response = requests.get(
        LOCKS_LIST,
        headers=headers,
        timeout=10
    )
    response.raise_for_status()
    return response.json()


class RosdomofonLock(LockEntity):
    def __init__(self, hass, token_manager, adapter_id, relay, device_type):
        self.hass = hass
        self._token_manager = token_manager
        self._adapter_id = adapter_id
        self._relay = relay
        self._attr_name = self._get_device_name(device_type)
        self._attr_icon = self._get_device_icon(device_type)
        self._attr_unique_id = f"rosdomofon_{adapter_id}_{relay}"

    async def async_unlock(self, **kwargs):
        """Открытие с автоматическим обновлением токена."""
        if not await self._token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен, открытие невозможно")
            return

        try:
            success = await self.hass.async_add_executor_job(
                lambda: _activate_key(
                    self._token_manager.access_token,
                    self._adapter_id,
                    self._relay
                )
            )
            if success:
                self._attr_is_locked = False
                self.async_write_ha_state()
        except Exception as e:
            _LOGGER.error(f"Ошибка при открытии: {e}")

    def _get_device_name(self, device_type):
        names = {
            1: "Дверь подъезда",
            2: "Шлагбаум",
            3: "Ворота",
            4: "Калитка"
        }
        return names.get(device_type, f"Устройство {self._adapter_id}")

    def _get_device_icon(self, device_type):
        icons = {
            1: "mdi:door-closed",
            2: "mdi:gate",
            3: "mdi:garage",
            4: "mdi:fence"
        }
        return icons.get(device_type, "mdi:lock")


def _activate_key(access_token: str, adapter_id: str, relay: int) -> bool:
    """Активация ключа для открытия."""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"
    }
    data = {"rele": relay}

    response = requests.post(
        LOCK_UNLOCK.format(adapter_id=adapter_id),
        headers=headers,
        json=data,
        timeout=10
    )

    if response.status_code == 200:
        _LOGGER.debug(f"Успешная активация {adapter_id}/{relay}")
        return True

    _LOGGER.error(
        f"Ошибка активации {adapter_id}/{relay}: "
        f"{response.status_code} {response.text}"
    )
    return False