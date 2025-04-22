from homeassistant.components.camera import Camera
from homeassistant.components.ffmpeg import async_get_image
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from custom_components.rosdomofon.token_manager import TokenManager
import logging
import requests
from typing import Optional, List
from custom_components.rosdomofon.const import *

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
        hass: HomeAssistant,
        entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
) -> None:
    """Настройка камер с автоматическим обновлением токена."""
    token_manager = TokenManager(hass, entry)

    if not await token_manager.ensure_valid_token():
        _LOGGER.error("Не удалось обновить токен, пропускаем настройку камер")
        return

    try:
        # Получаем список камер через синхронный запрос
        cameras = await hass.async_add_executor_job(
            _fetch_cameras_sync, token_manager.access_token
        )

        entities = []
        for camera in cameras:
            # Получаем RTSP-ссылку для каждой камеры
            rtsp_url = await hass.async_add_executor_job(
                _get_rtsp_url_sync, camera["id"], token_manager.access_token
            )

            if rtsp_url:
                entities.append(RosdomofonCamera(
                    hass=hass,
                    token_manager=token_manager,
                    camera_id=camera["id"],
                    name=camera.get("name", f"Камера {camera['id']}"),
                    rtsp_url=rtsp_url
                ))

        if entities:
            async_add_entities(entities, update_before_add=True)
            _LOGGER.info(f"Успешно добавлено {len(entities)} камер")
        else:
            _LOGGER.warning("Не найдено доступных камер")

    except Exception as e:
        _LOGGER.error(f"Ошибка при настройке камер: {e}", exc_info=True)


def _fetch_cameras_sync(access_token: str) -> List[dict]:
    """Синхронный запрос списка камер."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(
            CAMERAS_LIST_URL,
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        _LOGGER.error(f"Ошибка получения списка камер: {e}")
        raise
    except ValueError as e:
        _LOGGER.error(f"Ошибка парсинга JSON: {e}")
        raise


def _get_rtsp_url_sync(camera_id: str, access_token: str) -> Optional[str]:
    """Синхронный запрос RTSP-ссылки."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    try:
        response = requests.get(
            CAMERA_RTSP_URL.format(camera_id=camera_id),
            headers=headers,
            timeout=10
        )
        response.raise_for_status()
        return response.json().get("uri")
    except requests.exceptions.RequestException as e:
        _LOGGER.error(f"Ошибка получения RTSP для камеры {camera_id}: {e}")
        return None
    except ValueError as e:
        _LOGGER.error(f"Ошибка парсинга JSON для камеры {camera_id}: {e}")
        return None


class RosdomofonCamera(Camera):
    """Представление камеры Росдомофон в Home Assistant."""

    def __init__(
            self,
            hass: HomeAssistant,
            token_manager: TokenManager,
            camera_id: str,
            name: str,
            rtsp_url: str
    ):
        """Инициализация камеры."""
        super().__init__()
        self.hass = hass
        self._token_manager = token_manager
        self._camera_id = camera_id
        self._attr_name = name
        self._rtsp_url = rtsp_url
        self._attr_unique_id = f"rosdomofon_cam_{camera_id}"
        self._attr_extra_state_attributes = {
            "camera_id": camera_id,
            "rtsp_url": rtsp_url
        }
        self._attr_icon = "mdi:cctv"

    async def async_camera_image(
            self,
            width: Optional[int] = None,
            height: Optional[int] = None
    ) -> Optional[bytes]:
        """Получение изображения с камеры."""
        if not await self._token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен, невозможно получить изображение")
            return None

        try:
            return await async_get_image(
                self.hass,
                self._rtsp_url,
                extra_cmd="-rtsp_transport tcp -timeout 5000000",
                width=width,
                height=height
            )
        except Exception as e:
            _LOGGER.error(f"Ошибка получения изображения с камеры {self._camera_id}: {e}")
            return None

    async def stream_source(self) -> Optional[str]:
        """Возвращает RTSP-поток."""
        if not await self._token_manager.ensure_valid_token():
            _LOGGER.error("Не удалось обновить токен, поток недоступен")
            return None

        return self._rtsp_url

    async def async_update(self) -> None:
        """Обновление данных камеры."""
        if not await self._token_manager.ensure_valid_token():
            _LOGGER.warning("Не удалось обновить токен при обновлении камеры")
            return

        try:
            new_rtsp_url = await self.hass.async_add_executor_job(
                _get_rtsp_url_sync, self._camera_id, self._token_manager.access_token
            )

            if new_rtsp_url and new_rtsp_url != self._rtsp_url:
                self._rtsp_url = new_rtsp_url
                self._attr_extra_state_attributes["rtsp_url"] = new_rtsp_url
                self.async_write_ha_state()
                _LOGGER.info(f"Обновлена RTSP-ссылка для камеры {self._camera_id}")
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления камеры {self._camera_id}: {e}")