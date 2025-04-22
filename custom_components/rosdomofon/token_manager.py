import logging
import time
from typing import Optional, Dict
from custom_components.rosdomofon.const import *
import requests

_LOGGER = logging.getLogger(__name__)


class TokenManager:
    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self._token_data = entry.data["token_data"]

    async def ensure_valid_token(self):
        """Проверяет и обновляет токен при необходимости."""
        if self._is_token_expired():
            _LOGGER.debug("Токен истек, пытаемся обновить...")
            if not await self._refresh_token():
                _LOGGER.error("Не удалось обновить токен, требуется повторная авторизация")
                return False
        return True

    def _is_token_expired(self) -> bool:
        """Проверяет, истек ли срок действия токена."""
        if 'timestamp' not in self._token_data:
            return True

        return (time.time() - self._token_data['timestamp']) > (
                self._token_data['expires_in'] - 60)  # Обновляем за 60 сек до истечения

    async def _refresh_token(self) -> bool:
        """Обновляет токен с использованием refresh_token."""
        try:
            new_token = await self.hass.async_add_executor_job(self._request_token_refresh)

            if new_token:
                self._token_data = new_token
                self._token_data['timestamp'] = int(time.time())
                self.hass.config_entries.async_update_entry(
                    self.entry,
                    data={
                        **self.entry.data,
                        "token_data": self._token_data
                    }
                )
                return True
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления токена: {e}")
        return False

    def _request_token_refresh(self) -> Optional[Dict]:
        """Синхронный запрос на обновление токена."""
        data = {
            "grant_type": "refresh_token",
            "client_id": "abonent",
            "refresh_token": self._token_data['refresh_token']
        }

        response = requests.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10
        )

        if response.status_code == 200:
            _LOGGER.debug("Токен успешно обновлен")
            return response.json()

        _LOGGER.error(f"Ошибка обновления токена: {response.status_code} {response.text}")

        return None

    @property
    def access_token(self):
        """Возвращает текущий access token."""
        return self._token_data['access_token']
