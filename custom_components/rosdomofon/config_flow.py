"""
Поток настройки (Config Flow) интеграции Росдомофон.

Поддерживаются два способа входа:
1. По номеру телефона: пользователь вводит номер РФ, сервис отправляет SMS,
   затем пользователь вводит код из SMS и интеграция получает OAuth-токен.
2. По refresh_token: пользователь вводит готовый refresh_token (полученный,
   например, из мобильного приложения), интеграция обменивает его на
   access_token через oauth/token. Полезно, когда провайдер домофона
   отключил подтверждение по SMS (звонок, MAX и т.д.), которое config flow
   пока не умеет проходить напрямую.
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
    GRANT_TYPE_REFRESH,
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

    # --- Шаг 0: Выбор способа входа ---

    async def async_step_user(self, user_input=None):
        """Предлагает выбрать способ входа: по SMS или по готовому refresh_token."""
        return self.async_show_menu(
            step_id="user",
            menu_options=["phone", "token"],
        )

    # --- Шаг 1а: Ввод номера телефона ---

    async def async_step_phone(self, user_input=None):
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
            step_id="phone",
            data_schema=vol.Schema({
                vol.Required("phone"): str,
            }),
            description_placeholders={
                "note": "+7 (XXX) XXX-XX-XX, можно вводить в свободном формате — пробелы и символы будут удалены автоматически",
            },
            errors=errors,
        )

    # --- Шаг 1б: Ввод готового refresh_token ---

    async def async_step_token(self, user_input=None):
        """Запрос refresh_token и обмен его на access_token."""
        errors: dict[str, str] = {}

        if user_input is not None:
            refresh_token = user_input["refresh_token"].strip()
            self._tok = await self._exchange_refresh_token(refresh_token)
            # Помимо ответа с ошибкой (None) отбраковываем и «пустой» успех
            # без обязательных полей — TokenManager не сможет с ним работать
            # (access_token нужен для запросов, expires_in — чтобы понять,
            # когда токен истекает).
            if self._tok and self._tok.get("access_token") and "expires_in" in self._tok:
                self._tok["timestamp"] = int(time.time())
                return self._create_entry()
            errors["base"] = "invalid_refresh_token"

        return self.async_show_form(
            step_id="token",
            data_schema=vol.Schema({
                vol.Required("refresh_token"): str,
            }),
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
        if self._phone:
            title = f"Росдомофон ({self._phone})"
        else:
            # Номер телефона неизвестен (вход по refresh_token) — добавляем
            # хвост access_token, чтобы несколько таких записей можно было
            # различить в списке интеграций.
            token_suffix = (self._tok or {}).get("access_token", "")[-4:]
            title = f"Росдомофон (токен …{token_suffix})" if token_suffix else "Росдомофон (по токену)"
        return self.async_create_entry(
            title=title,
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

    async def _request_token(self, payload: dict, log_context: str) -> dict | None:
        """Отправляет запрос на oauth/token и возвращает разобранный ответ."""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            async with session.post(
                TOKEN_REQUEST_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=_REQUEST_TIMEOUT,
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

    async def _get_token(self, phone: str, sms_code: str) -> dict | None:
        """Получает OAuth-токен по номеру телефона и SMS-коду."""
        payload = {
            "grant_type": GRANT_TYPE_MOBILE,
            "client_id": CLIENT_ID,
            "phone": phone,
            "sms_code": sms_code,
            "company": COMPANY_NAME,
        }
        return await self._request_token(payload, "SMS")

    async def _exchange_refresh_token(self, refresh_token: str) -> dict | None:
        """Обменивает готовый refresh_token на access_token."""
        payload = {
            "grant_type": GRANT_TYPE_REFRESH,
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        }
        tok = await self._request_token(payload, "refresh_token")
        if tok is not None and not tok.get("refresh_token"):
            # OAuth2-сервер может не вернуть новый refresh_token в ответе,
            # если он не изменился — сохраняем тот, что ввёл пользователь,
            # чтобы TokenManager мог обновлять токен и дальше.
            tok["refresh_token"] = refresh_token
        return tok
