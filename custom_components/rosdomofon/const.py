"""Константы интеграции Росдомофон."""

DOMAIN = "rosdomofon"

# Базовый URL API Росдомофон
BASE_URL = "https://rdba.rosdomofon.com"

# Эндпоинты авторизации
SMS_REQUEST_URL = f"{BASE_URL}/abonents-service/api/v1/abonents/{{phone}}/sms"
TOKEN_REQUEST_URL = f"{BASE_URL}/authserver-service/oauth/token"

# Эндпоинты замков
LOCKS_LIST_URL = f"{BASE_URL}/abonents-service/api/v2/abonents/keys"
LOCK_UNLOCK_URL = f"{BASE_URL}/rdas-service/api/v1/rdas/{{adapter_id}}/activate_key"

# Параметры OAuth
GRANT_TYPE_MOBILE = "mobile"
GRANT_TYPE_REFRESH = "refresh_token"
CLIENT_ID = "abonent"
COMPANY_NAME = ""

# Валидация номера телефона РФ (11 цифр, начинается с 7)
PHONE_LENGTH = 11
PHONE_PREFIX = "7"
