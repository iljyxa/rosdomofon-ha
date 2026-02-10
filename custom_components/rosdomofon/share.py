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
        _LOGGER.info("Обработка гостевой ссылки %s для %s", webhook_id, entity_id)

        # Проверяем, что сущность существует и получаем её имя для отображения
        state = hass.states.get(entity_id)
        if state is None:
            _LOGGER.error("Сущность %s не найдена", entity_id)
            return web.Response(
                text=_html_page(
                    "Ошибка",
                    "Замок не найден. Возможно, интеграция была перенастроена.",
                    success=False,
                ),
                content_type="text/html",
                status=404,
            )

        display_name = state.name or "Замок"

        # Если это первый заход (GET) — показываем страницу с кнопкой
        if request.method == hdrs.METH_GET:
            now = time.time()
            remaining = max(0, link.expires_at - now)
            remaining_hours = int(remaining // 3600)
            remaining_minutes = int((remaining % 3600) // 60)

            return web.Response(
                text=_html_page_with_button(
                    display_name,
                    remaining_hours,
                    remaining_minutes,
                ),
                content_type="text/html",
            )

        # Далее считаем, что это POST с попыткой открыть замок
        try:
            await hass.services.async_call(
                "lock",
                "unlock",
                {"entity_id": entity_id},
                blocking=True,
            )
        except Exception as exc:
            _LOGGER.error("Ошибка открытия %s: %s", entity_id, exc)
            return web.json_response(
                {
                    "status": "error",
                    "error": str(exc),
                    "title": "Ошибка",
                    "message": f"Не удалось открыть {display_name}. Попробуйте позже.",
                },
                status=500,
            )

        return web.json_response(
            {
                "status": "ok",
                "title": f"{display_name} открыта",
                "message": f"{display_name} успешно открыта.",
            }
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


def _html_page_with_button(
    display_name: str,
    remaining_hours: int,
    remaining_minutes: int,
) -> str:
    """Страница с кнопкой открытия замка и таймером действия ключа."""

    # Мягкий градиент от синевато-голубого к фиолетовому
    gradient_start = "#8fb7ff"  # светлый сине-голубой
    gradient_end = "#c7a4ff"    # мягкий фиолетовый
    accent_color = "#7b5cff"    # фиолетовый для кнопки и акцентов
    text_color = "#ffffff"

    return f"""\
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Гостевой доступ</title>
  <style>
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      justify-content: center;
      align-items: center;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: linear-gradient(160deg, {gradient_start}, {gradient_end});
      color: {text_color};
    }}
    .card {{
      background: rgba(255, 255, 255, 0.08);
      border-radius: 24px;
      padding: 32px 24px 28px;
      width: 100%;
      max-width: 420px;
      box-shadow: 0 18px 45px rgba(0, 0, 0, 0.25);
      backdrop-filter: blur(18px);
      text-align: center;
    }}
    .title {{
      font-size: 1.15rem;
      font-weight: 600;
      margin-bottom: 8px;
    }}
    .subtitle {{
      font-size: 0.9rem;
      opacity: 0.9;
      margin-bottom: 24px;
    }}
    .timer {{
      font-size: 0.85rem;
      opacity: 0.95;
      margin-bottom: 28px;
    }}
    .timer span {{
      font-weight: 600;
    }}
    .button-wrapper {{
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 10px;
    }}
    .circle-button {{
      position: relative;
      width: 180px;
      height: 180px;
      border-radius: 50%;
      border: none;
      background: #fff;
      color: {accent_color};
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      box-shadow: 0 12px 30px rgba(0,0,0,0.20);
      transition: transform 0.12s ease, box-shadow 0.12s ease, background 0.15s ease;
    }}
    .circle-button:active {{
      transform: scale(0.97);
      box-shadow: 0 8px 22px rgba(0,0,0,0.24);
    }}
    .circle-button.disabled {{
      cursor: default;
      opacity: 0.85;
      box-shadow: 0 6px 16px rgba(0,0,0,0.15);
    }}
    .icon {{
      font-size: 44px;
      margin-bottom: 8px;
    }}
    .label {{
      font-size: 1.05rem;
      font-weight: 700;
      letter-spacing: 0.08em;
    }}
    .status-ok {{ color: #1EB980; }}
    .status-error {{ color: #FF5252; }}
    .status-progress {{ color: {accent_color}; }}
    .error-text {{
      margin-top: 4px;
      min-height: 1.2em;
      font-size: 0.85rem;
      color: #FFE8E8;
    }}
    .hint {{
      margin-top: 18px;
      font-size: 0.8rem;
      opacity: 0.85;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="title">Вам предоставили временный ключ для открытия {display_name}</div>
    <div class="timer">Ключ действует: <span>{remaining_hours}ч {remaining_minutes}м</span></div>

    <div class="button-wrapper">
      <button class="circle-button" id="open-btn">
        <div class="icon" id="btn-icon">🔓</div>
        <div class="label status-progress" id="btn-label">ОТКРЫТЬ</div>
      </button>
      <div class="error-text" id="error-text"></div>
    </div>

    <div class="hint">Не закрывайте страницу, пока дверь открывается.</div>
  </div>

  <script>
    const btn = document.getElementById('open-btn');
    const icon = document.getElementById('btn-icon');
    const label = document.getElementById('btn-label');
    const errorText = document.getElementById('error-text');

    let resetTimeout = null;

    function setStateIdle() {{
      btn.classList.remove('disabled');
      icon.textContent = '🔓';
      label.textContent = 'ОТКРЫТЬ';
      label.className = 'label status-progress';
      errorText.textContent = '';
    }}

    function setStateProgress() {{
      btn.classList.add('disabled');
      icon.textContent = '⏳';
      label.textContent = 'ОТКРЫВАЕМ...';
      label.className = 'label status-progress';
      errorText.textContent = '';
    }}

    function setStateOk() {{
      btn.classList.add('disabled');
      icon.textContent = '✅';
      label.textContent = 'ОТКРЫТО';
      label.className = 'label status-ok';
    }}

    function setStateError(message) {{
      btn.classList.remove('disabled');
      icon.textContent = '❌';
      label.textContent = 'ОШИБКА';
      label.className = 'label status-error';
      errorText.textContent = message || 'Произошла ошибка при открытии.';
    }}

    async function handleClick() {{
      if (btn.classList.contains('disabled')) {{
        return;
      }}
      window.clearTimeout(resetTimeout);
      setStateProgress();

      try {{
        const resp = await fetch(window.location.href, {{ method: 'POST' }});
        const data = await resp.json();

        if (resp.ok && data.status === 'ok') {{
          setStateOk();
          resetTimeout = window.setTimeout(setStateIdle, 5000);
        }} else {{
          const msg = data && data.message ? data.message : 'Не удалось открыть дверь.';
          setStateError(msg);
        }}
      }} catch (err) {{
        setStateError('Ошибка соединения. Попробуйте ещё раз.');
      }}
    }}

    btn.addEventListener('click', handleClick);
  </script>
</body>
</html>"""

