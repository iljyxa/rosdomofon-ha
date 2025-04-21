import voluptuous as vol
import requests
import logging
from homeassistant import config_entries
from .const import *

_LOGGER = logging.getLogger(__name__)


class RosdomofonConfigFlow(config_entries.ConfigFlow, domain="rosdomofon"):
    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    def __init__(self):
        self._phone = None
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
            data_schema=vol.Schema({vol.Required("phone"): str}),
            description_placeholders={
                "note": "Номер должен быть зарегистрирован в Росдомофоне."
            },
            errors=errors,
        )

    async def async_step_sms(self, user_input=None):
        """Шаг 2: Ввод SMS-кода."""
        errors = {}
        if user_input:
            sms_code = user_input["sms_code"]
            self._token_data = await self._get_token(self._phone, sms_code)
            if self._token_data:
                return await self._setup_cameras()
            errors["base"] = "invalid_code"

        return self.async_show_form(
            step_id="sms",
            data_schema=vol.Schema({vol.Required("sms_code"): str}),
            description_placeholders={"phone": self._phone},
            errors=errors,
        )

    async def _setup_cameras(self):
        """Добавляем все камеры."""
        cameras = await self._get_cameras(self._token_data["access_token"])
        if not cameras:
            return self.async_abort(reason="no_cameras")

        return self.async_create_entry(
            title=f"Росдомофон ({self._phone})",
            data={
                "phone": self._phone,
                "token_data": self._token_data,  # Весь объект токена!
            },
        )

    async def _request_sms(self, phone):
        """Запрос SMS-кода."""
        try:
            url = SMS_REQUEST_URL.format(phone=phone)
            response = await self.hass.async_add_executor_job(
                requests.post, url, {"headers": {"Content-Type": "application/json"}}
            )
            return response.status_code == 200
        except Exception as e:
            _LOGGER.error(f"Ошибка SMS: {e}")
            return False

    async def _get_token(self, phone, sms_code):
        """Получение токена."""
        data = {
            "grant_type": GRANT_TYPE_MOBILE,
            "client_id": CLIENT_ID,
            "phone": phone,
            "sms_code": sms_code,
        }
        try:
            response = await self.hass.async_add_executor_job(
                requests.post, TOKEN_URL, {"data": data}
            )
            return response.json() if response.status_code == 200 else None
        except Exception as e:
            _LOGGER.error(f"Ошибка токена: {e}")
            return None

    async def _get_cameras(self, token):
        """Получение списка камер."""
        try:
            headers = {"Authorization": f"Bearer {token}"}
            response = await self.hass.async_add_executor_job(
                requests.get, CAMERAS_LIST_URL, {"headers": headers}
            )
            return response.json() if response.status_code == 200 else []
        except Exception as e:
            _LOGGER.error(f"Ошибка камер: {e}")
            return []