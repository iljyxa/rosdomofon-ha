import logging
import time

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client

from custom_components.rosdomofon.const import *

_LOGGER = logging.getLogger(__name__)


class RosdomofonConfigFlow(config_entries.ConfigFlow, domain="rosdomofon"):
    """Config flow с поддержкой обновления токенов."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        self._phone = None
        self._sms_code = None
        self._token_data = None

    async def async_step_user(self, user_input=None):
        """Шаг 1: Ввод телефона."""
        errors = {}
        if user_input:
            self._phone = user_input["phone"]
            if await self._request_sms(self._phone):
                return await self.async_step_sms()
            errors["base"] = "sms_failed"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("phone"): str
            }),
            description_placeholders={
                "note": "Номер должен быть зарегистрирован в Росдомофоне"
            },
            errors=errors
        )

    async def async_step_sms(self, user_input=None):
        """Шаг 2: Ввод SMS и получение токена."""
        errors = {}
        if user_input:
            self._sms_code = user_input["sms_code"]
            self._token_data = await self._get_token(self._phone, self._sms_code)

            if self._token_data:
                # Добавляем timestamp при первом получении токена
                self._token_data['timestamp'] = int(time.time())
                return await self._async_create_entry()

            errors["base"] = "invalid_code"

        return self.async_show_form(
            step_id="sms",
            data_schema=vol.Schema({
                vol.Required("sms_code"): str
            }),
            description_placeholders={
                "phone": self._phone
            },
            errors=errors
        )

    async def _async_create_entry(self):
        """Создаем entry с токенами."""
        return self.async_create_entry(
            title=f"Росдомофон ({self._phone})",
            data={
                "phone": self._phone,
                "token_data": self._token_data
            }
        )

    async def _request_sms(self, phone: str) -> bool:
        """Запрос SMS с обработкой ошибок."""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            async with session.post(
                    SMS_REQUEST_URL.format(phone=phone),
                    headers={"Content-Type": "application/json"},
                    timeout=10
            ) as response:
                if response.status == 200:
                    _LOGGER.debug("SMS отправлено успешно")
                    return True
                _LOGGER.error(f"Ошибка SMS: {response.status}")
        except Exception as e:
            _LOGGER.error(f"Ошибка запроса SMS: {e}")
        return False

    async def _get_token(self, phone: str, sms_code: str) -> dict | None:
        """Получение токена с обработкой ошибок."""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            data = {
                "grant_type": "mobile",
                "client_id": "abonent",
                "phone": phone,
                "sms_code": sms_code
            }

            async with session.post(
                    TOKEN_URL,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=10
            ) as response:
                if response.status == 200:
                    token_data = await response.json()
                    _LOGGER.debug("Токен получен успешно")
                    return token_data
                _LOGGER.error(f"Ошибка токена: {response.status}")
        except Exception as e:
            _LOGGER.error(f"Ошибка получения токена: {e}")
        return None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Опционально: настройка периодического обновления."""
        return RosdomofonOptionsFlowHandler(config_entry)


class RosdomofonOptionsFlowHandler(config_entries.OptionsFlow):
    """Обработчик опций для настройки обновления."""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        """Настройка параметров обновления."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Optional(
                    "update_interval",
                    default=5,
                    description="Интервал обновления (минуты)"
                ): int
            })
        )
