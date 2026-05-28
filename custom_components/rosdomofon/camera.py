"""
Платформа камер (camera) для интеграции Росдомофон.

Поддерживает воспроизведение HLS потоков с авторизацией по bearer токену.
"""

import inspect
import logging
from datetime import timedelta
import re
from typing import Any

import requests
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

try:
    from homeassistant.components.http import async_sign_path as _ha_async_sign_path
except ImportError:
    try:
        from homeassistant.components.http.auth import async_sign_path as _ha_async_sign_path
    except ImportError:
        _ha_async_sign_path = None
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .const import CAMERAS_LIST_URL, CAMERA_DETAILS_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def _sign_path_compat(hass: HomeAssistant, path: str) -> str:
    """Sign path across HA versions."""
    if _ha_async_sign_path is None:
        _LOGGER.warning("Signed-path helper unavailable; stream proxy URL will be unsigned.")
        return path
    if "http.auth" not in hass.data:
        return path
    try:
        result = _ha_async_sign_path(hass, path)
    except TypeError:
        result = _ha_async_sign_path(hass, path, timedelta(minutes=5))
    except Exception as exc:
        _LOGGER.warning("Failed to sign path: %s", exc)
        return path
    if inspect.isawaitable(result):
        try:
            return await result
        except Exception as exc:
            _LOGGER.warning("Failed to sign path: %s", exc)
            return path
    return result


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
            camera_details = None
            rdva_uri = camera_data.get("rdvaUri", "")
            if not rdva_uri:
                camera_details = await hass.async_add_executor_job(
                    _fetch_camera_details, token_manager.access_token, camera_id
                )
                rdva_uri = (camera_details or {}).get("rdva", {}).get("uri", "")

            if rdva_uri:
                camera_payload = {**(camera_details or {}), **camera_data}
                stream_host = _rdva_uri_to_stream_host(rdva_uri)
                camera_hosts[str(camera_id)] = stream_host
                entities.append(
                    RosdomofonCamera(
                        token_manager=token_manager,
                        camera_id=camera_id,
                        camera_name=camera_payload.get("name", f"Камера {camera_id}"),
                        rdva_uri=rdva_uri,
                        stream_host=stream_host,
                        camera_data=camera_payload,
                    )
                )
            else:
                _LOGGER.warning("rdvaUri не найден для камеры %s", camera_id)
        except Exception as exc:
            _LOGGER.error("Ошибка обработки камеры %s: %s", camera_id, exc)
            continue

    if entities:
        async_add_entities(entities)
        _LOGGER.info("Добавлено камер: %d", len(entities))
    else:
        _LOGGER.warning("Не удалось добавить ни одной камеры")


class RosdomofonCamera(Camera):
    """Камера Росдомофон с поддержкой HLS потока."""

    def __init__(
        self,
        token_manager,
        camera_id: str,
        camera_name: str,
        rdva_uri: str,
        stream_host: str,
        camera_data: dict,
    ) -> None:
        super().__init__()
        self._token_manager = token_manager
        self._camera_id = camera_id
        self._camera_name = camera_name
        self._rdva_uri = rdva_uri
        self._camera_data = camera_data
        self._stream_source = f"https://{stream_host}/live/{camera_id}.m3u8"
        self._attr_name = camera_name
        self._attr_unique_id = f"rosdomofon_camera_{camera_id}"
        self._attr_supported_features = CameraEntityFeature.STREAM
        self._attr_brand = "Росдомофон"
        self._attr_model = camera_data.get("model", "Unknown")

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return None

    async def stream_source(self) -> str | None:
        """Возвращает URL HLS потока через прокси с авторизацией."""
        if not self._stream_source:
            return None
        if not await self._token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен для камеры %s", self.name)
            return None

        m = re.match(r"(https?://)([^/]+)/(.*)", self._stream_source)
        if not m:
            _LOGGER.error("Некорректный stream_source: %s", self._stream_source)
            return None

        _, host, path = m.groups()
        try:
            base_url = get_url(self.hass)
        except Exception as exc:
            _LOGGER.error("Не удалось получить base_url Home Assistant: %s", exc)
            return None

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
        return {
            "camera_id": self._camera_id,
            "stream_url": self._stream_source,
            "rdva_uri": self._rdva_uri,
            "rtsp_url": self._camera_data.get("rtspUrl", ""),
        }


def _rdva_uri_to_stream_host(rdva_uri: str) -> str:
    """Преобразует rdva URI из API в hostname HLS-сервера."""
    host = re.sub(r"^https?://", "", rdva_uri).strip("/")
    if not host.startswith("s."):
        host = f"s.{host}"
    return host


def _fetch_cameras(access_token: str) -> list[dict]:
    """Получает список камер."""
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
    response = requests.get(
        CAMERA_DETAILS_URL.format(camera_id=camera_id), headers=headers, timeout=10
    )

    if response.status_code == 200:
        return response.json()

    _LOGGER.error(
        "Ошибка получения деталей камеры %s: %d %s",
        camera_id,
        response.status_code,
        response.text,
    )
    return None
