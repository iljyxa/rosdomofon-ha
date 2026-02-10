"""
Платформа камер (camera) для интеграции Росдомофон.

Поддерживает воспроизведение HLS потоков с авторизацией по bearer токену.
"""

import logging
import inspect
from datetime import timedelta
import re
from typing import Any

import requests
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

try:
    from homeassistant.components.http import async_sign_path as _ha_async_sign_path  # type: ignore
except ImportError:
    try:
        from homeassistant.components.http.auth import async_sign_path as _ha_async_sign_path  # type: ignore
    except ImportError:
        _ha_async_sign_path = None
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .const import BASE_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def _sign_path_compat(hass: HomeAssistant, path: str) -> str:
    """Sign path across HA versions. Falls back to unsigned path."""
    if _ha_async_sign_path is None:
        _LOGGER.warning(
            "Signed-path helper is unavailable in this Home Assistant version; "
            "stream proxy URL will be unsigned."
        )
        return path

    if "http.auth" not in hass.data:
        # In tests or minimal setups auth storage is not initialized.
        return path

    try:
        result = _ha_async_sign_path(hass, path)
    except TypeError:
        # Some versions require expiration arg.
        result = _ha_async_sign_path(hass, path, timedelta(minutes=5))
    except Exception as exc:
        _LOGGER.warning(
            "Failed to sign path, falling back to unsigned URL: %s",
            exc,
        )
        return path

    if inspect.isawaitable(result):
        try:
            return await result
        except Exception as exc:
            _LOGGER.warning(
                "Failed to sign path, falling back to unsigned URL: %s",
                exc,
            )
            return path
    return result

# Эндпоинты API для получения списка камер и детальной информации
# noinspection SpellCheckingInspection
CAMERAS_LIST_URL = f"{BASE_URL}/abonents-service/api/v2/abonents/cameras"
CAMERA_DETAILS_URL = f"{BASE_URL}/cameras-service/api/v1/cameras/{{camera_id}}"


# ---------------------------------------------------------------------------
# Настройка платформы
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Настройка камер из config entry."""
    token_manager = hass.data[DOMAIN][entry.entry_id]["token_manager"]

    if not await token_manager.ensure_valid_token():
        _LOGGER.error("Не удалось обновить токен, пропускаем настройку камер")
        return

    try:
        cameras = await hass.async_add_executor_job(
            _fetch_cameras, token_manager.access_token
        )
    except Exception as exc:
        _LOGGER.error("Ошибка получения списка камер: %s", exc)
        return

    if not cameras:
        _LOGGER.info("Камеры не найдены")
        return

    entities = []
    camera_hosts = hass.data.setdefault(DOMAIN, {}).setdefault("_camera_hosts", {})
    for camera_data in cameras:
        camera_id = camera_data.get("id")
        if not camera_id:
            continue

        try:
            # Получаем детальную информацию о камере для построения HLS URL
            camera_details = await hass.async_add_executor_job(
                _fetch_camera_details, token_manager.access_token, camera_id
            )
            
            if camera_details:
                rdva_data = camera_details.get("rdva", {})
                rdva_uri = rdva_data.get("uri", "")
                if rdva_uri:
                    camera_hosts[str(camera_id)] = f"s.{rdva_uri}"
                entities.append(
                    RosdomofonCamera(
                        token_manager=token_manager,
                        camera_id=camera_id,
                        camera_name=camera_data.get("name", f"Камера {camera_id}"),
                        camera_details=camera_details,
                    )
                )
        except Exception as exc:
            _LOGGER.error(
                "Ошибка получения деталей камеры %s: %s", camera_id, exc
            )
            continue

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Добавлено камер: %d", len(entities))
    else:
        _LOGGER.warning("Не удалось добавить ни одной камеры")


# ---------------------------------------------------------------------------
# Entity
# ---------------------------------------------------------------------------


# noinspection SpellCheckingInspection
class RosdomofonCamera(Camera):
    """Камера Росдомофон с поддержкой HLS потока."""

    def __init__(
        self,
        token_manager,
        camera_id: str,
        camera_name: str,
        camera_details: dict,
    ) -> None:
        super().__init__()
        self._token_manager = token_manager
        self._camera_id = camera_id
        self._camera_name = camera_name
        self._camera_details = camera_details

        # Построение HLS URL из rdva.uri
        rdva_data = camera_details.get("rdva", {})
        rdva_uri = rdva_data.get("uri", "")
        
        if rdva_uri:
            self._stream_source = f"https://s.{rdva_uri}/live/{camera_id}.m3u8"
        else:
            self._stream_source = None
            _LOGGER.warning(
                "Не удалось построить HLS URL для камеры %s", camera_id
            )

        self._attr_name = camera_name
        self._attr_unique_id = f"rosdomofon_camera_{camera_id}"
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._attr_brand = "Росдомофон"
        self._attr_model = camera_details.get("model", "Unknown")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Возвращает статичное изображение с камеры (snapshot)."""
        # Для HLS потоков можно попробовать получить первый кадр,
        # но это требует ffmpeg. Для простоты возвращаем None,
        # что заставит HA использовать stream для получения изображения.
        return None

    async def stream_source(self) -> str | None:
        """Возвращает URL HLS потока через прокси с авторизацией."""
        if not self._stream_source:
            return None

        if not await self._token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен для камеры %s", self.name)
            return None

        # self._stream_source вида: https://s.rdva68.rosdomofon.com/live/39167.m3u8
        m = re.match(r"(https?://)([^/]+)/(.*)", self._stream_source)
        if not m:
            _LOGGER.error("Некорректный stream_source: %s", self._stream_source)
            return None

        scheme, host, path = m.groups()  # scheme пока не используем

        try:
            base_url = get_url(self.hass)  # например, https://ha.ifedorov.keenetic.pro
        except Exception as exc:
            _LOGGER.error("Не удалось получить base_url Home Assistant: %s", exc)
            return None

        # path сейчас: live/39167.m3u8
        proxy_path = f"/api/rosdomofon/stream/{self._camera_id}/{host}/{path}"
        signed_path = await _sign_path_compat(self.hass, proxy_path)
        proxy_url = f"{base_url}{signed_path}"

        _LOGGER.debug(
            "Stream source для камеры %s: %s (прокси для %s)",
            self.name,
            proxy_url,
            self._stream_source,
        )

        return proxy_url

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Возвращает дополнительные атрибуты сущности."""
        return {
            "camera_id": self._camera_id,
            "stream_url": self._stream_source,
        }


# ---------------------------------------------------------------------------
# Синхронные HTTP-запросы (executor)
# ---------------------------------------------------------------------------


def _fetch_cameras(access_token: str) -> list[dict]:
    """Получает список камер абонента."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    response = requests.get(CAMERAS_LIST_URL, headers=headers, timeout=10)
    response.raise_for_status()
    return response.json()


def _fetch_camera_details(access_token: str, camera_id: str) -> dict | None:
    """Получает детальную информацию о камере."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    url = CAMERA_DETAILS_URL.format(camera_id=camera_id)
    response = requests.get(url, headers=headers, timeout=10)
    
    if response.status_code == 200:
        return response.json()
    
    _LOGGER.error(
        "Ошибка получения деталей камеры %s: %d %s",
        camera_id,
        response.status_code,
        response.text,
    )
    return None
