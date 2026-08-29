"""Тесты для config_flow.py интеграции rosdomofon."""

import pytest
from homeassistant import config_entries, data_entry_flow
from homeassistant.core import HomeAssistant

from custom_components.rosdomofon.const import DOMAIN

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Делает custom_components/rosdomofon видимым для config flow в тестах."""
    yield


def _token_response(refresh_token="new_refresh_token", access_token="new_access_token"):
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_in": 3600,
        "token_type": "Bearer",
    }


async def _start_flow(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


# --- Шаг выбора способа входа ---


async def test_user_step_shows_menu(hass: HomeAssistant):
    """Первый шаг должен предложить выбор между SMS и refresh_token."""
    result = await _start_flow(hass)

    assert result["type"] == data_entry_flow.FlowResultType.MENU
    assert result["step_id"] == "user"
    assert set(result["menu_options"]) == {"phone", "token"}


# --- Способ 1: номер телефона + SMS ---


async def test_phone_sms_flow_success(hass: HomeAssistant, aioclient_mock):
    """Полный успешный проход: номер -> SMS -> код -> запись создана."""
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/abonents-service/api/v1/abonents/79991234567/sms",
        status=200,
    )
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        json=_token_response(),
        status=200,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "phone"}
    )
    assert result["step_id"] == "phone"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "+7 (999) 123-45-67"}
    )
    assert result["step_id"] == "sms"
    assert result["description_placeholders"]["phone"] == "79991234567"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sms_code": "1234"}
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["title"] == "Росдомофон (79991234567)"
    assert result["data"]["phone"] == "79991234567"
    assert result["data"]["token_data"]["access_token"] == "new_access_token"
    assert "timestamp" in result["data"]["token_data"]


async def test_phone_step_invalid_length(hass: HomeAssistant, aioclient_mock):
    """Некорректная длина номера должна вернуть ошибку без запроса SMS."""
    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "phone"}
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "123"}
    )

    assert result["step_id"] == "phone"
    assert result["errors"]["phone"] == "invalid_phone_length"
    assert len(aioclient_mock.mock_calls) == 0


async def test_phone_step_sms_request_failed(hass: HomeAssistant, aioclient_mock):
    """Ошибка отправки SMS (не 200) должна вернуть base error."""
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/abonents-service/api/v1/abonents/79991234567/sms",
        status=500,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "phone"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "79991234567"}
    )

    assert result["step_id"] == "phone"
    assert result["errors"]["base"] == "sms_failed"


async def test_sms_step_invalid_code(hass: HomeAssistant, aioclient_mock):
    """Неверный код должен вернуть ошибку invalid_code."""
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/abonents-service/api/v1/abonents/79991234567/sms",
        status=200,
    )
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        status=400,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "phone"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"phone": "79991234567"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"sms_code": "0000"}
    )

    assert result["step_id"] == "sms"
    assert result["errors"]["base"] == "invalid_code"


# --- Способ 2: готовый refresh_token ---


async def test_token_step_success(hass: HomeAssistant, aioclient_mock):
    """Успешный обмен refresh_token на access_token создаёт запись без номера."""
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        json=_token_response(),
        status=200,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    assert result["step_id"] == "token"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "existing_refresh_token"}
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    # Номер телефона неизвестен — заголовок различает записи по хвосту токена.
    assert result["title"] == "Росдомофон (токен …oken)"
    assert result["data"]["phone"] is None
    assert result["data"]["token_data"]["access_token"] == "new_access_token"
    assert result["data"]["token_data"]["refresh_token"] == "new_refresh_token"
    assert "timestamp" in result["data"]["token_data"]

    # Запрос должен уйти именно с grant_type=refresh_token и переданным токеном
    call = aioclient_mock.mock_calls[0]
    sent_data = call[2]
    assert sent_data["grant_type"] == "refresh_token"
    assert sent_data["refresh_token"] == "existing_refresh_token"


async def test_token_step_response_without_new_refresh_token(hass: HomeAssistant, aioclient_mock):
    """Если сервер не вернул новый refresh_token, сохраняем введённый пользователем.

    OAuth2-сервер может не отдавать refresh_token в ответе, если он не
    изменился — без этой подстраховки TokenManager не смог бы обновлять
    access_token в дальнейшем (falls back on the submitted refresh_token).
    """
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        json={"access_token": "new_access_token", "expires_in": 3600},
        status=200,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "existing_refresh_token"}
    )

    assert result["type"] == data_entry_flow.FlowResultType.CREATE_ENTRY
    assert result["data"]["token_data"]["refresh_token"] == "existing_refresh_token"


async def test_token_step_response_without_expires_in(hass: HomeAssistant, aioclient_mock):
    """Ответ 200 без expires_in считается ошибкой, а не пустым успехом.

    Без expires_in TokenManager._is_expired() упал бы с KeyError сразу
    при настройке записи.
    """
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        json={"access_token": "new_access_token", "refresh_token": "new_refresh_token"},
        status=200,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "some_token"}
    )

    assert result["step_id"] == "token"
    assert result["errors"]["base"] == "invalid_refresh_token"


async def test_token_step_response_without_access_token(hass: HomeAssistant, aioclient_mock):
    """Ответ 200 без access_token считается ошибкой, а не пустым успехом."""
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        json={},
        status=200,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "some_token"}
    )

    assert result["step_id"] == "token"
    assert result["errors"]["base"] == "invalid_refresh_token"


async def test_token_step_invalid_refresh_token(hass: HomeAssistant, aioclient_mock):
    """Ошибка обмена (например 401) должна вернуть invalid_refresh_token."""
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        status=401,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "bad_token"}
    )

    assert result["step_id"] == "token"
    assert result["errors"]["base"] == "invalid_refresh_token"


async def test_token_step_network_error(hass: HomeAssistant, aioclient_mock):
    """Сетевая ошибка при обмене токена не должна приводить к падению."""
    aioclient_mock.post(
        "https://rdba.rosdomofon.com/authserver-service/oauth/token",
        exc=TimeoutError,
    )

    result = await _start_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "token"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"refresh_token": "some_token"}
    )

    assert result["step_id"] == "token"
    assert result["errors"]["base"] == "invalid_refresh_token"
