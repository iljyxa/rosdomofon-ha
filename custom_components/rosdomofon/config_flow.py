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

    async def _request_sms(self, phone: str) -> bool:
        """Улучшенный запрос SMS с детальным логированием."""
        try:
            url = SMS_REQUEST_URL.format(phone=phone)
            headers = {"Content-Type": "application/json"}

            _LOGGER.debug(f"Отправка запроса SMS на номер: {phone}")
            _LOGGER.debug(f"URL: {url}")
            _LOGGER.debug(f"Headers: {headers}")

            response = await self.hass.async_add_executor_job(
                lambda: requests.post(url, headers=headers, timeout=10)
            )

            _LOGGER.debug(f"Ответ сервера: {response.status_code} {response.text}")

            if response.status_code == 200:
                _LOGGER.info("SMS успешно отправлено")
                return True

            _LOGGER.error(
                f"Ошибка при запросе SMS. Код: {response.status_code}, Ответ: {response.text}"
            )
            return False

        except requests.exceptions.Timeout:
            _LOGGER.error("Таймаут при запросе SMS")
        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"Ошибка соединения: {str(e)}")
        except Exception as e:
            _LOGGER.error(f"Неожиданная ошибка: {str(e)}")

        return False

    async def _get_token(self, phone: str, sms_code: str) -> dict | None:
        """Получение токена авторизации с правильной типизацией."""
        from functools import partial

        try:
            data = {
                "grant_type": GRANT_TYPE_MOBILE,
                "client_id": CLIENT_ID,
                "phone": phone,
                "sms_code": sms_code
            }

            # Создаем частично примененную функцию
            post_request = partial(
                requests.post,
                TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10
            )

            response = await self.hass.async_add_executor_job(post_request)

            if response.status_code == 200:
                return response.json()

            _LOGGER.error(f"Ошибка получения токена. Статус: {response.status_code}")
            return None

        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"Ошибка соединения при получении токена: {e}")
            return None
        except ValueError as e:
            _LOGGER.error(f"Ошибка парсинга JSON ответа: {e}")
            return None

    async def _get_cameras(self, token: str) -> list[dict] | None:
        """Получение списка камер с правильной типизацией."""
        from functools import partial

        try:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            # Частично примененная функция для GET запроса
            get_request = partial(
                requests.get,
                CAMERAS_LIST_URL,
                headers=headers,
                timeout=10
            )

            response = await self.hass.async_add_executor_job(get_request)

            if response.status_code == 200:
                cameras = response.json()
                if not isinstance(cameras, list):
                    _LOGGER.error("Получен некорректный формат списка камер")
                    return None
                return cameras

            _LOGGER.error(f"Ошибка получения камер. Статус: {response.status_code}")
            return None

        except requests.exceptions.RequestException as e:
            _LOGGER.error(f"Ошибка соединения при получении камер: {e}")
            return None
        except ValueError as e:
            _LOGGER.error(f"Ошибка парсинга JSON ответа для камер: {e}")
            return None