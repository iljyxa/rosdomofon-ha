"""
Платформа кнопок (button) для интеграции Росдомофон.

Для каждого замка создаётся кнопка «Поделиться»,
которая генерирует временную гостевую ссылку для открытия.
"""

import logging

import requests
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    LOCKS_LIST_URL,
    SHARE_LINK_DEFAULT_TTL_HOURS,
)
from .share import ExternalURLNotAvailable, ShareLinkManager

_LOGGER = logging.getLogger(__name__)

# Соответствие типа устройства -> название для кнопки
_DEVICE_NAMES: dict[int, str] = {
    1: "Дверь подъезда",
    2: "Шлагбаум",
    3: "Ворота",
    4: "Калитка",
}


# ---------------------------------------------------------------------------
# Настройка платформы
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Создаёт кнопки «Поделиться» для каждого замка."""
    data = hass.data[DOMAIN][entry.entry_id]
    token_manager = data["token_manager"]
    share_manager: ShareLinkManager = data["share_manager"]

    if not await token_manager.ensure_valid_token():
        _LOGGER.error("Не удалось обновить токен, пропускаем настройку кнопок")
        return

    try:
        keys = await hass.async_add_executor_job(
            _fetch_keys, token_manager.access_token
        )
    except Exception as exc:
        _LOGGER.error("Ошибка получения ключей для кнопок: %s", exc)
        return

    entities = [
        RosdomofonShareButton(
            share_manager=share_manager,
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


class RosdomofonShareButton(ButtonEntity):
    """Кнопка «Поделиться» для замка Росдомофон."""

    def __init__(
        self,
        share_manager: ShareLinkManager,
        adapter_id: str,
        relay: int,
        device_type: int,
    ) -> None:
        self._share_manager = share_manager
        self._adapter_id = adapter_id
        self._relay = relay

        device_name = _DEVICE_NAMES.get(device_type, f"Замок {device_type}")
        self._lock_entity_id = f"lock.rosdomofon_{adapter_id}_{relay}"

        self._attr_name = f"Поделиться: {device_name}"
        self._attr_icon = "mdi:share-variant"
        self._attr_unique_id = f"rosdomofon_share_{adapter_id}_{relay}"

    async def async_press(self) -> None:
        """Нажатие кнопки: генерация гостевой ссылки или уведомление об ошибке."""
        # Проверка внешнего URL перед генерацией
        try:
            url = self._share_manager.generate(
                self._lock_entity_id,
                SHARE_LINK_DEFAULT_TTL_HOURS,
            )
        except ExternalURLNotAvailable:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass,
                "Невозможно создать гостевую ссылку.\n\n"
                "В Home Assistant не настроен доступ извне. "
                "Настройте **External URL** в разделе "
                "Настройки → Система → Сеть, "
                "либо подключите **Home Assistant Cloud (Nabu Casa)**.",
                title="Росдомофон: внешний доступ не настроен ⚠️",
                notification_id="rosdomofon_no_external_url",
            )
            _LOGGER.warning("Внешний URL не настроен, ссылку создать невозможно")
            return

        ttl = int(SHARE_LINK_DEFAULT_TTL_HOURS)
        from homeassistant.components import persistent_notification

        persistent_notification.async_create(
            self.hass,
            f"Ссылка для открытия **{self._attr_name.replace('Поделиться: ', '')}** "
            f"(действительна {ttl} ч):\n\n"
            f"`{url}`\n\n"
            f"Скопируйте ссылку и отправьте гостю.",
            title="Росдомофон: гостевая ссылка 🔗",
            notification_id=f"rosdomofon_share_{self._lock_entity_id}",
        )
        _LOGGER.info("Создана гостевая ссылка для %s", self._lock_entity_id)


# ---------------------------------------------------------------------------
# Синхронный HTTP-запрос (повториспользуется тот же API что и в lock.py)
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
