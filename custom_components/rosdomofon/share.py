"""
Модуль гостевого доступа (Share Link) для интеграции Росдомофон.

Позволяет генерировать временные ссылки для открытия конкретного замка.
Ссылка действительна ограниченное время (TTL), после чего автоматически деактивируется.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from aiohttp import hdrs, web

from homeassistant.components import webhook
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers import network

from .const import DOMAIN, SHARE_LINK_DEFAULT_TTL_HOURS, SHARE_LINK_WEBHOOK_PREFIX

_LOGGER = logging.getLogger(__name__)


@dataclass
class ShareLink:
    """Одна временная ссылка для открытия замка."""

    webhook_id: str
    entity_id: str
    created_at: float = field(default_factory=time.time)
    ttl_hours: float = SHARE_LINK_DEFAULT_TTL_HOURS
    cancel_expiry: Any = None  # CALLBACK_TYPE — отмена таймера

    @property
    def expires_at(self) -> float:
        return self.created_at + self.ttl_hours * 3600

    @property
    def is_expired(self) -> bool:
        return time.time() > self.expires_at


class ShareLinkManager:
    """Управляет временными ссылками для открытия замков."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._links: dict[str, ShareLink] = {}  # webhook_id -> ShareLink

    # ------------------------------------------------------------------
    # Публичный интерфейс
    # ------------------------------------------------------------------

    def get_external_url(self) -> str | None:
        """Получает внешний URL Home Assistant или None."""
        try:
            return network.get_url(
                self.hass,
                allow_internal=False,
                allow_ip=True,
                prefer_external=True,
                prefer_cloud=True,
            )
        except network.NoURLAvailableError:
            return None

    def generate(self, entity_id: str, ttl_hours: float = SHARE_LINK_DEFAULT_TTL_HOURS) -> str:
        """Создаёт временную ссылку и возвращает полный URL."""
        external_url = self.get_external_url()
        if external_url is None:
            raise ExternalURLNotAvailable

        webhook_id = f"{SHARE_LINK_WEBHOOK_PREFIX}{uuid.uuid4().hex}"

        # Регистрируем webhook (доступный извне, GET + POST)
        webhook.async_register(
            self.hass,
            domain=DOMAIN,
            name=f"Share link: {entity_id}",
            webhook_id=webhook_id,
            handler=self._handle_webhook,
            local_only=False,
            allowed_methods=(hdrs.METH_GET, hdrs.METH_POST),
        )

        link = ShareLink(
            webhook_id=webhook_id,
            entity_id=entity_id,
            ttl_hours=ttl_hours,
        )

        # Таймер автоудаления
        link.cancel_expiry = async_call_later(
            self.hass,
            ttl_hours * 3600,
            self._make_expiry_callback(webhook_id),
        )

        self._links[webhook_id] = link

        full_url = f"{external_url}/api/webhook/{webhook_id}"
        _LOGGER.info(
            "Сгенерирована ссылка для %s (TTL %s ч), webhook_id=%s",
            entity_id,
            ttl_hours,
            webhook_id,
        )
        return full_url

    def revoke(self, webhook_id: str) -> None:
        """Отзывает ссылку досрочно."""
        link = self._links.pop(webhook_id, None)
        if link is None:
            return
        if link.cancel_expiry:
            link.cancel_expiry()
        try:
            webhook.async_unregister(self.hass, webhook_id)
        except ValueError:
            pass
        _LOGGER.debug("Ссылка %s отозвана", webhook_id)

    def revoke_all(self) -> None:
        """Отзывает все активные ссылки (при выгрузке интеграции)."""
        for wh_id in list(self._links):
            self.revoke(wh_id)

    # ------------------------------------------------------------------
    # Webhook handler
    # ------------------------------------------------------------------

    async def _handle_webhook(
        self, hass: HomeAssistant, webhook_id: str, request: web.Request
    ) -> web.Response:
        """Обработчик входящего запроса по ссылке."""
        link = self._links.get(webhook_id)

        if link is None or link.is_expired:
            _LOGGER.warning("Попытка использовать недействительную ссылку: %s", webhook_id)
            return web.Response(
                text=_html_page(
                    "Ссылка недействительна",
                    "Срок действия ссылки истёк или она была отозвана.",
                    success=False,
                ),
                content_type="text/html",
                status=410,
            )

        entity_id = link.entity_id
        _LOGGER.info("Открытие %s по гостевой ссылке %s", entity_id, webhook_id)

        # Вызываем сервис lock.unlock
        try:
            await hass.services.async_call(
                "lock",
                "unlock",
                {"entity_id": entity_id},
                blocking=True,
            )
        except Exception as exc:
            _LOGGER.error("Ошибка открытия %s: %s", entity_id, exc)
            return web.Response(
                text=_html_page(
                    "Ошибка",
                    "Не удалось открыть замок. Попробуйте позже.",
                    success=False,
                ),
                content_type="text/html",
                status=500,
            )

        return web.Response(
            text=_html_page(
                "Дверь открыта ✅",
                "Замок успешно открыт. Дверь автоматически закроется.",
                success=True,
            ),
            content_type="text/html",
        )

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    def _make_expiry_callback(self, webhook_id: str):
        """Создаёт callback для автоудаления ссылки по TTL."""

        @callback
        def _expire(_now) -> None:
            _LOGGER.info("Ссылка %s истекла, удаляем", webhook_id)
            self._links.pop(webhook_id, None)
            try:
                webhook.async_unregister(self.hass, webhook_id)
            except ValueError:
                pass

        return _expire


class ExternalURLNotAvailable(Exception):
    """Внешний URL Home Assistant не настроен."""


# ---------------------------------------------------------------------------
# HTML-шаблон для ответа по ссылке
# ---------------------------------------------------------------------------

def _html_page(title: str, message: str, *, success: bool = True) -> str:
    """Минимальная HTML-страница для гостя по ссылке."""
    color = "#4CAF50" if success else "#F44336"
    return f"""\
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      display: flex; justify-content: center; align-items: center;
      min-height: 100vh; margin: 0;
      background: #f5f5f5; color: #333;
    }}
    .card {{
      background: #fff; border-radius: 16px; padding: 40px 32px;
      box-shadow: 0 2px 12px rgba(0,0,0,.1); text-align: center;
      max-width: 360px; width: 90%;
    }}
    .card h1 {{ color: {color}; font-size: 1.5em; margin-bottom: 8px; }}
    .card p {{ color: #666; font-size: 1em; line-height: 1.5; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>{title}</h1>
    <p>{message}</p>
  </div>
</body>
</html>"""
