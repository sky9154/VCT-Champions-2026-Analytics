from datetime import date, datetime, time, timedelta, timezone

from app.config import Settings


def next_scheduled_run(settings: Settings,
                       now: datetime | None = None) -> datetime | None:
  if not settings.auto_update_enabled:
    return None

  current = now or datetime.now(timezone.utc)

  if current.tzinfo is None or current.utcoffset() is None:
    raise ValueError("The scheduling reference time must include a time zone.")

  current_utc = current.astimezone(timezone.utc)

  if settings.auto_update_mode == "interval":
    return current_utc + timedelta(
        minutes=settings.auto_update_interval_minutes)

  local_now = current_utc.astimezone(settings.auto_update_timezone_info)

  for day_offset in range(0, 370):
    candidate_date: date = local_now.date() + timedelta(days=day_offset)

    for configured_time in settings.parsed_auto_update_daily_times:
      hour, minute = (int(part) for part in configured_time.split(":"))
      local_candidate = datetime.combine(candidate_date, time(hour, minute))
      zoned_candidate = local_candidate.replace(
          tzinfo=settings.auto_update_timezone_info, fold=0)
      candidate_utc = zoned_candidate.astimezone(timezone.utc)
      round_trip = candidate_utc.astimezone(settings.auto_update_timezone_info)

      if round_trip.replace(tzinfo=None) != local_candidate:
        continue

      if candidate_utc > current_utc:
        return candidate_utc

  raise ValueError(
      "No valid upcoming daily schedule time could be calculated.")
