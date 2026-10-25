from datetime import datetime, timezone
from typing import Any


def success_object(data: dict[str, Any] | list[dict[str, Any]],
                   meta: dict[str, Any] | None = None) -> dict[str, Any]:
  return {"data": data, "meta": meta or {}}


def public_identifier(value: Any) -> str | None:
  if not isinstance(value, str) or not value.strip():
    return None

  return value.strip()


def utc_datetime_string(value: Any) -> str | None:
  if not isinstance(value, datetime):
    return None

  if value.tzinfo is None or value.utcoffset() is None:
    value = value.replace(tzinfo=timezone.utc)

  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
