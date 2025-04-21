from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
import requests
from .const import *

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Добавляем все камеры."""
    token = entry.data["token_data"]["access_token"]
    cameras = await hass.async_add_executor_job(_fetch_cameras, token)
    entities = [RosdomofonCamera(camera, token) for camera in cameras]
    async_add_entities(entities)

def _fetch_cameras(token):
    """Получаем список камер и их RTSP-ссылки."""
    headers = {"Authorization": f"Bearer {token}"}
    cameras_list = requests.get(CAMERAS_LIST_URL, headers=headers).json()
    return [
        {
            "id": cam["id"],
            "name": cam.get("name", f"Камера {cam['id']}"),
            "rtsp_url": requests.get(
                CAMERA_RTSP_URL.format(camera_id=cam["id"]), headers=headers
            ).json().get("uri"),
        }
        for cam in cameras_list
    ]

class RosdomofonCamera(Camera):
    def __init__(self, camera_data, token):
        super().__init__()
        self._id = camera_data["id"]
        self._name = camera_data["name"]
        self._rtsp_url = camera_data["rtsp_url"]
        self._token = token

    @property
    def name(self):
        return self._name

    async def stream_source(self):
        return self._rtsp_url