"""
Общий клиент для запросов к OAuth2-эндпоинту Росдомофон (oauth/token).

Используется как из config_flow.py (первичная авторизация: по SMS-коду и по
готовому refresh_token), так и из token_manager.py (периодическое обновление
access_token по refresh_token) — чтобы протокол запроса и обработка ошибок не
расходились между местами использования.
"""

import logging

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client

from .const import TOKEN_REQUEST_URL

_LOGGER = logging.getLogger(__name__)

# Таймаут для HTTP-запросов к API
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


async def async_request_oauth_token(
    hass: HomeAssistant, payload: dict, log_context: str
) -> dict | None:
    """Отправляет запрос на oauth/token и возвращает разобранный ответ.

    Возвращает None при сетевой ошибке, ошибке разбора JSON или ответе с
    HTTP-статусом, отличным от 200.
    """
    try:
        session = aiohttp_client.async_get_clientsession(hass)
        async with session.post(
            TOKEN_REQUEST_URL,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=REQUEST_TIMEOUT,
        ) as resp:
            if resp.status == 200:
                _LOGGER.debug("Токен получен успешно (%s)", log_context)
                return await resp.json()
            _LOGGER.error(
                "Ошибка получения токена (%s): %d %s",
                log_context,
                resp.status,
                await resp.text(),
            )
    except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
        # ValueError покрывает и ошибку разбора JSON в ответе (json.JSONDecodeError).
        _LOGGER.error("Ошибка запроса токена (%s): %s", log_context, exc)
    return None
