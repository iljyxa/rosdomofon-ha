"""
Платформа замков (lock) для интеграции Росдомофон.

Поддерживаемые типы устройств:
  1 - Дверь подъезда
  2 - Шлагбаум
  3 - Ворота
  4 - Калитка
"""

import logging
from typing import Any

import requests
from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, LOCKS_LIST_URL, LOCK_UNLOCK_URL

_LOGGER = logging.getLogger(__name__)

# Соответствие типа устройства -> название
_DEVICE_NAMES: dict[int, str] = {
    1: "Дверь подъезда",
    2: "Шлагбаум",
    3: "Ворота",
    4: "Калитка",
}

# Соответствие типа устройства -> иконка MDI
_DEVICE_ICONS: dict[int, str] = {
    1: "mdi:door-closed",
    2: "mdi:gate",
    3: "mdi:garage",
    4: "mdi:fence",
}

# Время автоматической блокировки после открытия (секунды)
_AUTO_LOCK_DELAY = 5.0


# ---------------------------------------------------------------------------
# Настройка платформы
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Настройка замков из config entry."""
    token_manager = hass.data[DOMAIN][entry.entry_id]["token_manager"]

    if not await token_manager.ensure_valid_token():
        _LOGGER.error("Не удалось обновить токен, пропускаем настройку замков")
        return

    try:
        keys = await hass.async_add_executor_job(
            _fetch_keys, token_manager.access_token
        )
    except requests.RequestException as exc:
        _LOGGER.error("Ошибка получения ключей (сетевая ошибка): %s", exc)
        return
    except ValueError as exc:
        _LOGGER.error("Некорректный ответ API при получении ключей: %s", exc)
        return
    except Exception as exc:  # safety net
        _LOGGER.exception("Неожиданная ошибка при получении ключей: %s", exc)
        return

    entities = [
        RosdomofonLock(
            token_manager=token_manager,
            adapter_id=key["adapterId"],
            relay=key["relay"],
            device_type=key["type"],
        )
        for key in keys
    ]
    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


class RosdomofonLock(LockEntity):
    """Замок Росдомофон с автоматической блокировкой после открытия."""

    def __init__(
        self,
        token_manager,
        adapter_id: str,
        relay: int,
        device_type: int,
    ) -> None:
        self._token_manager = token_manager
        self._adapter_id = adapter_id
        self._relay = relay

        self._attr_name = _DEVICE_NAMES.get(device_type, f"Замок {device_type}")
        self._attr_icon = _DEVICE_ICONS.get(device_type, "mdi:lock")
        self._attr_unique_id = f"rosdomofon_{adapter_id}_{relay}"
        self._attr_is_locked = True
        self._unlock_timer = None

    async def async_unlock(self, **kwargs: Any) -> None:
        """Открыть замок. Автоматически закроется через _AUTO_LOCK_DELAY сек."""
        if not await self._token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен")
            return

        try:
            success = await self.hass.async_add_executor_job(
                _activate_key,
                self._token_manager.access_token,
                self._adapter_id,
                self._relay,
            )
        except Exception as exc:
            _LOGGER.error("Ошибка при открытии %s: %s", self.name, exc)
            return

        if not success:
            _LOGGER.error("Не удалось открыть %s", self.name)
            return

        self._attr_is_locked = False
        self.async_write_ha_state()
        _LOGGER.info("%s открыт", self.name)

        if self._unlock_timer is not None:
            self._unlock_timer()
            self._unlock_timer = None

        self._unlock_timer = async_call_later(
            self.hass, _AUTO_LOCK_DELAY, self._async_auto_lock
        )

    @callback
    def _async_auto_lock(self, _now) -> None:
        """Автоматическая блокировка замка по таймеру."""
        self._attr_is_locked = True
        self._unlock_timer = None
        self.async_write_ha_state()
        _LOGGER.info("%s автоматически закрыт", self.name)

    async def async_will_remove_from_hass(self) -> None:
        """Отменяет таймер автоблокировки при удалении сущности."""
        if self._unlock_timer is not None:
            self._unlock_timer()
            self._unlock_timer = None


# ---------------------------------------------------------------------------
# Синхронные HTTP-запросы (executor)
# ---------------------------------------------------------------------------


def _fetch_keys(access_token: str) -> list[dict]:
    """Получает список ключей (замков) абонента."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    response = requests.get(LOCKS_LIST_URL, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def _activate_key(access_token: str, adapter_id: str, relay: int) -> bool:
    """Активирует ключ (открывает замок)."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    response = requests.post(
        LOCK_UNLOCK_URL.format(adapter_id=adapter_id),
        headers=headers,
        json={"rele": relay},
        timeout=10,
    )
    if response.status_code == 200:
        _LOGGER.debug("Успешная активация %s/%d", adapter_id, relay)
        return True

    _LOGGER.error(
        "Ошибка активации: %d %s", response.status_code, response.text
    )
    return False
