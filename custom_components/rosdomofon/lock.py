import logging
from typing import Any
from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from .token_manager import TokenManager
from .const import *
import requests

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """Настройка замков."""
    token_manager = TokenManager(hass, entry)
    if not await token_manager.ensure_valid_token():
        return

    try:
        keys = await hass.async_add_executor_job(
            _fetch_keys, token_manager.access_token
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
            device_type=key["type"],
            lock_name=_get_device_name(key["type"]),
            icon=_get_device_icon(key["type"])
        ))

    async_add_entities(entities)


class RosdomofonLock(LockEntity):
    """Умный замок с автоматическим закрытием."""

    def __init__(
            self,
            hass: HomeAssistant,
            token_manager: TokenManager,
            adapter_id: str,
            relay: int,
            device_type: int,
            lock_name: str,
            icon: str
    ):
        self.hass = hass
        self._token_manager = token_manager
        self._adapter_id = adapter_id
        self._relay = relay
        self._attr_name = lock_name
        self._attr_icon = icon
        self._attr_unique_id = f"rosdomofon_{adapter_id}_{relay}"
        self._attr_is_locked = True  # По умолчанию замок закрыт
        self._unlock_timer = None

    async def async_unlock(self, **kwargs: Any) -> None:
        """Открыть замок с автоматическим закрытием через 5 секунд."""
        if not await self._token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен")
            return

        try:
            success = await self.hass.async_add_executor_job(
                _activate_key,
                self._token_manager.access_token,
                self._adapter_id,
                self._relay
            )

            if success:
                self._attr_is_locked = False
                self.async_write_ha_state()
                _LOGGER.info(f"{self.name} открыт")

                # Запланировать автоматическое закрытие через 5 секунд
                self._unlock_timer = async_call_later(
                    self.hass,
                    5.0,  # 5 секунд
                    self._async_lock_callback
                )
            else:
                _LOGGER.error(f"Не удалось открыть {self.name}")

        except Exception as e:
            _LOGGER.error(f"Ошибка при открытии {self.name}: {e}")

    @callback
    def _async_lock_callback(self, _):
        """Автоматическое закрытие замка."""
        self._attr_is_locked = True
        self._unlock_timer = None
        self.async_write_ha_state()
        _LOGGER.info(f"{self.name} автоматически закрыт")

    async def async_will_remove_from_hass(self):
        """Отменить таймер при удалении entity."""
        if self._unlock_timer:
            self._unlock_timer()
            self._unlock_timer = None


def _get_device_name(device_type: int) -> str:
    """Возвращает название устройства по типу."""
    return {
        1: "Дверь подъезда",
        2: "Шлагбаум",
        3: "Ворота",
        4: "Калитка"
    }.get(device_type, f"Замок {device_type}")


def _get_device_icon(device_type: int) -> str:
    """Возвращает иконку устройства по типу."""
    return {
        1: "mdi:door-closed",
        2: "mdi:gate",
        3: "mdi:garage",
        4: "mdi:fence"
    }.get(device_type, "mdi:lock")


def _fetch_keys(access_token: str) -> list:
    """Синхронный запрос списка ключей."""
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

    _LOGGER.error(f"Ошибка активации: {response.status_code} {response.text}")
    return False