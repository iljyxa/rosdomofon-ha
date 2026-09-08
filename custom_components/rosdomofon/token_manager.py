"""
Менеджер токенов для интеграции Росдомофон.

Отвечает за хранение access/refresh токенов,
проверку их срока действия и автоматическое обновление.
"""

import logging
import time

from .const import CLIENT_ID, GRANT_TYPE_REFRESH
from .oauth_client import async_request_oauth_token

_LOGGER = logging.getLogger(__name__)

# Запас (в секундах) до истечения токена, при котором запускается refresh
_EXPIRY_MARGIN = 60


class TokenManager:
    """Управляет жизненным циклом OAuth-токенов Росдомофон."""

    def __init__(self, hass, entry):
        self.hass = hass
        self.entry = entry
        self._tok = entry.data["token_data"]

    # --- Публичный интерфейс ---

    @property
    def access_token(self) -> str:
        """Текущий access token."""
        return self._tok["access_token"]

    async def ensure_valid_token(self) -> bool:
        """Проверяет срок действия и обновляет токен при необходимости."""
        if not self._is_expired():
            return True

        _LOGGER.debug("Токен истёк, попытка обновления...")
        if await self._refresh():
            return True

        _LOGGER.error("Не удалось обновить токен, требуется повторная авторизация")
        return False

    # --- Внутренняя логика ---

    def _is_expired(self) -> bool:
        """Проверяет, истёк ли токен (с запасом _EXPIRY_MARGIN сек)."""
        if "timestamp" not in self._tok:
            return True
        elapsed = time.time() - self._tok["timestamp"]
        return elapsed > (self._tok["expires_in"] - _EXPIRY_MARGIN)

    async def _refresh(self) -> bool:
        """Обновляет токен через refresh_token и сохраняет в config entry."""
        try:
            payload = {
                "grant_type": GRANT_TYPE_REFRESH,
                "client_id": CLIENT_ID,
                "refresh_token": self._tok["refresh_token"],
            }
            new_tok = await async_request_oauth_token(self.hass, payload, "refresh_token")
            if new_tok is None:
                return False

            new_tok["timestamp"] = int(time.time())
            self._tok = new_tok

            self.hass.config_entries.async_update_entry(
                self.entry,
                data={**self.entry.data, "token_data": self._tok},
            )
            return True
        except Exception as exc:
            _LOGGER.error("Ошибка обновления токена: %s", exc)
            return False
