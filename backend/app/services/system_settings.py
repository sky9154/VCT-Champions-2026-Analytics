from __future__ import annotations

import os
import re
import stat
import tempfile
from io import StringIO
from pathlib import Path
from threading import Lock
from typing import Any

from dotenv import dotenv_values
from pydantic import ValidationError

from app.api.errors import APIError
from app.config import BACKEND_DIRECTORY, Settings

ENV_FILE = BACKEND_DIRECTORY / ".env"
_WRITE_LOCK = Lock()
_MANAGED_SETTINGS = {
    "liquipediaRequestIntervalSeconds": {
        "env": "LIQUIPEDIA_REQUEST_INTERVAL_SECONDS",
        "field": "liquipedia_request_interval_seconds",
        "label": "Liquipedia 請求間隔",
        "description": "控制向 Liquipedia 發出請求的最短間隔。",
        "minimum": 2,
        "maximum": 60,
        "step": 1,
        "unit": "秒"
    },
    "liquipediaParseIntervalSeconds": {
        "env": "LIQUIPEDIA_PARSE_INTERVAL_SECONDS",
        "field": "liquipedia_parse_interval_seconds",
        "label": "Liquipedia 解析間隔",
        "description": "控制重複解析相同頁面的最短間隔。",
        "minimum": 30,
        "maximum": 3600,
        "step": 1,
        "unit": "秒"
    },
    "liquipediaCacheTtlSeconds": {
        "env": "LIQUIPEDIA_CACHE_TTL_SECONDS",
        "field": "liquipedia_cache_ttl_seconds",
        "label": "Liquipedia 快取有效時間",
        "description": "設定來源快取可重用的時間；設為 0 時，不限制快取有效時間。",
        "minimum": 0,
        "maximum": 86400,
        "step": 60,
        "unit": "秒"
    },
    "matchTimeToleranceMinutes": {
        "env": "MATCH_TIME_TOLERANCE_MINUTES",
        "field": "match_time_tolerance_minutes",
        "label": "賽程時間比對容許差距",
        "description": "設定比對不同來源賽程時間時可接受的差距。",
        "minimum": 0,
        "maximum": 180,
        "step": 1,
        "unit": "分鐘"
    }
}
_SECRET_KEYS = ("MONGODB_URI", "LIQUIPEDIA_USER_AGENT")


def _read_env() -> tuple[str, dict[str, Any], bool]:
  if not ENV_FILE.exists():
    return "", {}, False

  raw_content = ENV_FILE.read_bytes()
  has_bom = raw_content.startswith(b"\xef\xbb\xbf")
  content = raw_content.decode("utf-8-sig")
  parsed = dotenv_values(stream=StringIO(content), interpolate=False)

  return content, parsed, has_bom


def _candidate_settings(settings: Settings, env_values: dict[str,
                                                             Any]) -> Settings:
  values = settings.model_dump()

  for spec in _MANAGED_SETTINGS.values():
    env_key = spec["env"]

    if env_key in os.environ:
      values[spec["field"]] = os.environ[env_key]
    elif env_values.get(env_key) is not None:
      values[spec["field"]] = env_values[env_key]

  return Settings.model_validate(values)


def get_system_settings(settings: Settings) -> dict[str, Any]:
  try:
    _, env_values, _ = _read_env()
    candidate = _candidate_settings(settings, env_values)
    configuration_valid = True
  except (OSError, ValidationError, ValueError):
    env_values = {}
    candidate = settings
    configuration_valid = False

  items = []
  pending_backend_restart = False

  for key, spec in _MANAGED_SETTINGS.items():
    value = getattr(candidate, spec["field"])
    running_value = getattr(settings, spec["field"])
    pending = configuration_valid and value != running_value
    pending_backend_restart = pending_backend_restart or pending
    items.append({
        "key":
        spec["env"],
        "name":
        spec["label"],
        "description":
        spec["description"],
        "type":
        "number",
        "value":
        value,
        "minimum":
        spec["minimum"],
        "maximum":
        spec["maximum"],
        "step":
        spec["step"],
        "unit":
        spec["unit"],
        "configured":
        spec["env"] in os.environ or env_values.get(spec["env"]) is not None,
        "requiresRestart":
        "backend",
        "pendingRestart":
        pending
    })

  secrets = {}

  for key in _SECRET_KEYS:
    secrets[key] = {
        "configured": bool(os.environ.get(key) or env_values.get(key))
    }

  return {
      "frontendPort": 5577,
      "backendPort": 8591,
      "settings": items,
      "secrets": secrets,
      "configurationValid": configuration_valid,
      "pendingBackendRestart": pending_backend_restart,
      "pendingFrontendRestart": False,
      "schedulerEnabled": settings.auto_update_enabled
  }


def _setting_values_from_patch(patch: dict[str, Any]) -> dict[str, Any]:
  values = {}

  for key, value in patch.items():
    spec = _MANAGED_SETTINGS[key]

    if value is None or isinstance(value, bool):
      raise APIError(422, "INVALID_SETTING", "設定內容無效，請確認欄位與允許範圍。")

    values[spec["field"]] = value

  return values


def _format_env_value(value: Any) -> str:
  if isinstance(value, float) and value.is_integer():
    return str(int(value))

  return str(value)


def _replace_managed_values(content: str, new_values: dict[str, Any]) -> str:
  newline = "\r\n" if "\r\n" in content else "\n"
  lines = content.splitlines(keepends=True)
  seen: set[str] = set()

  for index, line in enumerate(lines):
    for key, spec in _MANAGED_SETTINGS.items():
      env_key = spec["env"]
      match = re.match(
          rf"^(\s*(?:export\s+)?{re.escape(env_key)}\s*=\s*)(.*?)(\r?\n)?$",
          line)

      if not match or key not in new_values:
        continue

      current_value = match.group(2)
      comment = re.search(r"\s+#.*$", current_value)
      trailing_space = re.search(r"\s*$", current_value)
      suffix = comment.group(0) if comment else trailing_space.group(
          0) if trailing_space else ""
      ending = match.group(3) or ""
      lines[
          index] = f"{match.group(1)}{_format_env_value(new_values[key])}{suffix}{ending}"
      seen.add(key)

  missing = [key for key in new_values if key not in seen]

  if not missing:
    return "".join(lines)

  rendered = "".join(lines)

  if rendered and not rendered.endswith(("\n", "\r")):
    rendered += newline

  for key in missing:
    rendered += f"{_MANAGED_SETTINGS[key]['env']}={_format_env_value(new_values[key])}{newline}"

  return rendered


def _atomic_write(content: str, *, has_bom: bool) -> None:
  ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
  previous_mode = None

  try:
    previous_mode = stat.S_IMODE(ENV_FILE.stat().st_mode)
  except OSError:
    pass

  descriptor, temporary_name = tempfile.mkstemp(prefix=".env-",
                                                suffix=".tmp",
                                                dir=ENV_FILE.parent)
  temporary_path = Path(temporary_name)

  try:
    with os.fdopen(descriptor, "w", encoding="utf-8",
                   newline="") as temporary_file:
      if has_bom:
        temporary_file.write("\ufeff")

      temporary_file.write(content)
      temporary_file.flush()
      os.fsync(temporary_file.fileno())

    if previous_mode is not None:
      os.chmod(temporary_path, previous_mode)

    os.replace(temporary_path, ENV_FILE)
  finally:
    temporary_path.unlink(missing_ok=True)


def update_system_settings(settings: Settings,
                           patch: dict[str, Any]) -> dict[str, Any]:
  if not patch:
    raise APIError(422, "EMPTY_SETTINGS_PATCH", "請先修改至少一項系統設定。")

  mapped_values = _setting_values_from_patch(patch)

  for key in patch:
    env_key = _MANAGED_SETTINGS[key]["env"]

    if env_key in os.environ:
      raise APIError(409, "SETTING_OVERRIDDEN_BY_PROCESS",
                     "此設定由 API 服務的啟動環境管理，請在該環境調整。")

  candidate_values = settings.model_dump()
  candidate_values.update(mapped_values)

  try:
    Settings.model_validate(candidate_values)
  except (ValidationError, ValueError):
    raise APIError(422, "INVALID_SETTING", "設定未通過檢查，請確認數值範圍。") from None

  with _WRITE_LOCK:
    try:
      content, _, has_bom = _read_env()
      updated_content = _replace_managed_values(content, patch)
      updated_env_values = dotenv_values(stream=StringIO(updated_content),
                                         interpolate=False)
      _candidate_settings(settings, updated_env_values)
    except (ValidationError, ValueError, TypeError):
      raise APIError(422, "INVALID_SETTING", "設定未通過檢查，請確認所有設定值。") from None

    try:
      _atomic_write(updated_content, has_bom=has_bom)
    except (OSError, UnicodeError):
      raise APIError(503, "SETTINGS_WRITE_UNAVAILABLE",
                     "無法安全儲存設定，原設定未變更。") from None

  return get_system_settings(settings)
