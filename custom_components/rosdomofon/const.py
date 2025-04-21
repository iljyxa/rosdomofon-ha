BASE_URL = "https://rdba.rosdomofon.com"
SMS_REQUEST_URL = f"{BASE_URL}/abonents-service/api/v1/abonents/{{phone}}/sms"
TOKEN_URL = f"{BASE_URL}/authserver-service/oauth/token"
CAMERAS_LIST_URL = f"{BASE_URL}/abonents-service/api/v3/abonents/cameras"
CAMERA_RTSP_URL = f"{BASE_URL}/cameras-service/api/v1/cameras/{{camera_id}}"

GRANT_TYPE_MOBILE = "mobile"
GRANT_TYPE_REFRESH = "refresh_token"
CLIENT_ID = "abonent"