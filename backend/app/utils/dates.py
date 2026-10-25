from datetime import datetime, timezone, tzinfo


def parse_utc_datetime(
    value: str | datetime | None,
    source_timezone: tzinfo | None = None) -> datetime | None:
  if value is None:
    return None

  if isinstance(value, datetime):
    parsed = value
  else:
    normalized = value.strip()

    if not normalized:
      return None

    if normalized.endswith("Z"):
      normalized = f"{normalized[:-1]}+00:00"

    try:
      parsed = datetime.fromisoformat(normalized)
    except ValueError:
      return None

  if parsed.tzinfo is None:
    if source_timezone is None:
      return None

    parsed = parsed.replace(tzinfo=source_timezone)

  return parsed.astimezone(timezone.utc)
