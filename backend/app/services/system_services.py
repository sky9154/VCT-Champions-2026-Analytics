from urllib.error import URLError
from urllib.request import urlopen

FRONTEND_PORT = 5577
BACKEND_PORT = 8591
BACKEND_HEALTH_IDENTITY = "VCT Analytics API"


def _frontend_is_reachable() -> bool:
  try:
    with urlopen(f"http://127.0.0.1:{FRONTEND_PORT}/",
                 timeout=0.8) as response:
      return 200 <= response.status < 400
  except (OSError, URLError, TimeoutError):
    return False


def get_system_services() -> dict[str, object]:
  frontend_reachable = _frontend_is_reachable()

  return {
      "frontend": {
          "status": "running" if frontend_reachable else "unavailable",
          "port": FRONTEND_PORT,
          "reachable": frontend_reachable,
          "healthIdentity": None
      },
      "backend": {
          "status": "running",
          "port": BACKEND_PORT,
          "reachable": True,
          "healthIdentity": BACKEND_HEALTH_IDENTITY
      },
      "managementMode": "separate",
      "managementMessage": "網頁服務與 API 服務由不同程序執行；套用需要重新啟動的設定時，請手動重啟對應服務。"
  }
