"""
Поток настройки (Config Flow) интеграции Росдомофон.

Шаг 1 - пользователь вводит номер телефона РФ, сервис отправляет SMS.
Шаг 2 - пользователь вводит код из SMS, интеграция получает OAuth-токен.
"""

import logging
import re
import time

import aiohttp
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import aiohttp_client

from .const import (
    CLIENT_ID,
    COMPANY_NAME,
    DOMAIN,
    GRANT_TYPE_MOBILE,
    PHONE_LENGTH,
    PHONE_PREFIX,
    SMS_REQUEST_URL,
    TOKEN_REQUEST_URL,
)

_LOGGER = logging.getLogger(__name__)

# Таймаут для HTTP-запросов к API
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _normalize_phone(raw_phone: str) -> str:
    """Приводит номер телефона к строгому формату (11 цифр, начиная с 7).

    Удаляет пробелы, тире, скобки, плюс.
    Заменяет ведущую 8 на 7.
    """
    digits = re.sub(r"\D", "", raw_phone)
    if digits.startswith("8") and len(digits) == PHONE_LENGTH:
        digits = PHONE_PREFIX + digits[1:]
    return digits


def _validate_phone(phone: str) -> str | None:
    """Возвращает код ошибки или None если номер корректен."""
    if len(phone) != PHONE_LENGTH:
        return "invalid_phone_length"
    if not phone.startswith(PHONE_PREFIX):
        return "invalid_phone_prefix"
    return None


class RosdomofonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Поток настройки интеграции Росдомофон."""

    VERSION = 1

    def __init__(self):
        self._phone: str | None = None
        self._tok: dict | None = None

    # --- Шаг 1: Ввод номера телефона ---

    async def async_step_user(self, user_input=None):
        """Запрос номера телефона и отправка SMS."""
        errors: dict[str, str] = {}

        if user_input is not None:
            phone = _normalize_phone(user_input["phone"])
            error = _validate_phone(phone)

            if error:
                errors["phone"] = error
            elif await self._request_sms(phone):
                self._phone = phone
                return await self.async_step_sms()
            else:
                errors["base"] = "sms_failed"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("phone"): str,
            }),
            description_placeholders={
                "note": "+7 (XXX) XXX-XX-XX, можно вводить в свободном формате — пробелы и символы будут удалены автоматически",
            },
            errors=errors,
        )

    # --- Шаг 2: Ввод SMS-кода ---

    async def async_step_sms(self, user_input=None):
        """Запрос кода из SMS и получение токена."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._tok = await self._get_token(
                self._phone, user_input["sms_code"]
            )
            if self._tok:
                self._tok["timestamp"] = int(time.time())
                return self._create_entry()
            errors["base"] = "invalid_code"

        return self.async_show_form(
            step_id="sms",
            data_schema=vol.Schema({
                vol.Required("sms_code"): str,
            }),
            description_placeholders={"phone": self._phone},
            errors=errors,
        )

    # --- Создание config entry ---

    def _create_entry(self):
        """Создаёт config entry с данными авторизации."""
        return self.async_create_entry(
            title=f"Росдомофон ({self._phone})",
            data={
                "phone": self._phone,
                "token_data": self._tok,
            },
        )

    # --- HTTP-запросы к API ---

    async def _request_sms(self, phone: str) -> bool:
        """Отправляет запрос на SMS-код для указанного номера."""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            async with session.post(
                SMS_REQUEST_URL.format(phone=phone),
                headers={"Content-Type": "application/json"},
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    _LOGGER.debug("SMS отправлено успешно")
                    return True
                _LOGGER.error("Ошибка отправки SMS: %d", resp.status)
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.error("Ошибка запроса SMS: %s", exc)
        return False

    async def _get_token(self, phone: str, sms_code: str) -> dict | None:
        """Получает OAuth-токен по номеру телефона и SMS-коду."""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            payload = {
                "grant_type": GRANT_TYPE_MOBILE,
                "client_id": CLIENT_ID,
                "phone": phone,
                "sms_code": sms_code,
                "company": COMPANY_NAME,
            }
            async with session.post(
                TOKEN_REQUEST_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                if resp.status == 200:
                    _LOGGER.debug("Токен получен успешно")
                    return await resp.json()
                _LOGGER.error(
                    "Ошибка получения токена: %d %s",
                    resp.status,
                    await resp.text(),
                )
        except (aiohttp.ClientError, TimeoutError) as exc:
            _LOGGER.error("Ошибка запроса токена: %s", exc)
        return None
