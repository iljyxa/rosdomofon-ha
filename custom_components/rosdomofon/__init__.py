"""
Интеграция Росдомофон для Home Assistant.

Обеспечивает управление замками (двери, шлагбаумы, ворота, калитки)
через облачный API Росдомофон.
Поддерживает генерацию временных гостевых ссылок для открытия дверей.
"""

import logging

import voluptuous as vol
from homeassistant.components import persistent_notification
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, SHARE_LINK_DEFAULT_TTL_HOURS
from .share import ExternalURLNotAvailable, ShareLinkManager
from .stream_proxy import setup_stream_proxy
from .token_manager import TokenManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[str] = ["lock", "button", "camera"]

# Схема сервиса генерации гостевой ссылки
SERVICE_GENERATE_LINK = "generate_share_link"
SERVICE_GENERATE_LINK_SCHEMA = vol.Schema({
    vol.Required("entity_id"): cv.entity_id,
    vol.Optional("ttl_hours", default=SHARE_LINK_DEFAULT_TTL_HOURS): vol.All(
        vol.Coerce(float), vol.Range(min=0.5, max=168)
    ),
})


async def async_setup_entry(hass, entry) -> bool:
    """Настройка интеграции при добавлении config entry."""
    token_manager = TokenManager(hass, entry)

    if not await token_manager.ensure_valid_token():
        _LOGGER.error("Не удалось обновить токен при старте")
        raise ConfigEntryNotReady("Не удалось обновить токен при старте")

    share_manager = ShareLinkManager(hass)

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "token_manager": token_manager,
        "share_manager": share_manager,
    }

    # Регистрируем прокси для HLS потоков (один раз на домен)
    if "_stream_proxy_registered" not in hass.data[DOMAIN]:
        setup_stream_proxy(hass)
        hass.data[DOMAIN]["_stream_proxy_registered"] = True

    # Регистрируем сервис генерации ссылки (один раз на домен)
    if not hass.services.has_service(DOMAIN, SERVICE_GENERATE_LINK):
        async def handle_generate_link(call):
            """Обработчик сервиса rosdomofon.generate_share_link."""
            entity_id = call.data["entity_id"]
            ttl_hours = call.data.get("ttl_hours", SHARE_LINK_DEFAULT_TTL_HOURS)

            # Находим share_manager для любого активного entry
            mgr = None
            for _eid, data in hass.data.get(DOMAIN, {}).items():
                if isinstance(data, dict) and "share_manager" in data:
                    mgr = data["share_manager"]
                    break

            if mgr is None:
                _LOGGER.error("Интеграция не настроена")
                return

            try:
                url = mgr.generate(entity_id, ttl_hours)
            except ExternalURLNotAvailable:
                persistent_notification.async_create(
                    hass,
                    "Невозможно создать гостевую ссылку: "
                    "в Home Assistant не настроен внешний доступ. "
                    "Настройте External URL или подключите Home Assistant Cloud (Nabu Casa).",
                    title="Росдомофон: внешний доступ не настроен",
                    notification_id="rosdomofon_no_external_url",
                )
                return

            ttl_text = f"{int(ttl_hours)} ч" if ttl_hours == int(ttl_hours) else f"{ttl_hours} ч"
            persistent_notification.async_create(
                hass,
                f"Ссылка для открытия **{entity_id}** "
                f"(действительна {ttl_text}):\n\n"
                f"`{url}`\n\n"
                f"Скопируйте и отправьте гостю.",
                title="Росдомофон: гостевая ссылка создана 🔗",
                notification_id=f"rosdomofon_share_{entity_id}",
            )

        hass.services.async_register(
            DOMAIN,
            SERVICE_GENERATE_LINK,
            handle_generate_link,
            schema=SERVICE_GENERATE_LINK_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    """Выгрузка интеграции при удалении config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, {})
        share_manager = data.get("share_manager")
        if share_manager:
            share_manager.revoke_all()

        # Если больше нет активных entry, удаляем сервис
        if not hass.data.get(DOMAIN):
            hass.services.async_remove(DOMAIN, SERVICE_GENERATE_LINK)

    return unload_ok
