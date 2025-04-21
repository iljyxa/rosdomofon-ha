import logging

import requests
from homeassistant.components.camera import Camera
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import *

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Настройка камер с использованием FFmpeg."""
    token_data = entry.data["token_data"]

    try:
        cameras = await hass.async_add_executor_job(
            _fetch_cameras, token_data["access_token"]
        )
    except Exception as e:
        _LOGGER.error(f"Ошибка получения камер: {e}")
        return

    entities = []

    for camera in cameras:
        try:
            rtsp_url = await hass.async_add_executor_job(
                _get_rtsp_url, camera["id"], token_data["access_token"]
            )
            if rtsp_url:
                entities.append(
                    RosdomofonCamera(
                        hass=hass,
                        camera_id=camera["id"],
                        name=camera.get("name", f"Камера {camera['id']}"),
                        rtsp_url=rtsp_url,
                        token=token_data["access_token"]
                    )
                )
        except Exception as e:
            _LOGGER.error(f"Ошибка создания камеры {camera.get('id')}: {e}")

    if entities:
        async_add_entities(entities)


def _fetch_cameras(token):
    """Получение списка камер."""
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        CAMERAS_LIST_URL,
        headers=headers,
        timeout=10
    )
    return response.json() if response.status_code == 200 else []


def _get_rtsp_url(camera_id, token):
    """Получение RTSP ссылки."""
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(
        CAMERA_RTSP_URL.format(camera_id=camera_id),
        headers=headers,
        timeout=10
    )
    return response.json().get("uri") if response.status_code == 200 else None


class RosdomofonCamera(Camera):
    """Камера с FFmpeg трансляцией."""


    def __init__(self, hass: HomeAssistant, camera_id: str, name: str, rtsp_url: str, token: str):
        """Инициализация камеры."""
        super().__init__()
        self.hass = hass
        self._camera_id = camera_id
        self._name = name
        self._rtsp_url = rtsp_url
        self._token = token
        self._attr_unique_id = f"rosdomofon_{camera_id}"
        self._attr_extra_state_attributes = {
            "rtsp_url": rtsp_url,
            "camera_id": camera_id,
            "token": token[:8] + "..."  # Для безопасности
        }


    @property
    def name(self):
        """Возвращает имя камеры."""
        return self._name

    async def async_camera_image(self, width=None, height=None):
        """Улучшенный метод с обработкой ошибок RTSP."""
        try:
            _LOGGER.debug(f"Попытка получить изображение с {self._rtsp_url}")
            return await async_get_image(
                self.hass,
                self._rtsp_url,
                extra_cmd="-rtsp_transport tcp -timeout 5000000",
                width=width,
                height=height
            )
        except Exception as e:
            _LOGGER.warning(f"Ошибка получения изображения: {str(e)}")
            return None


    async def stream_source(self) -> str | None:
        """Возвращает источник для стриминга."""
        return self._rtsp_url