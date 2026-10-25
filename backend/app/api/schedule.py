from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pymongo.database import Database

from app.api.dependencies import get_database
from app.api.errors import APIError
from app.api.responses import success_object, utc_datetime_string
from app.api.schemas import APIResponse, ScheduleMatch
from app.config import get_settings
from app.services.schedule import SCHEDULE_STATUS_FILTERS, list_schedule

router = APIRouter()


def _utc_datetime(value: datetime | None) -> datetime | None:
  if value is None:
    return None

  if value.tzinfo is None or value.utcoffset() is None:
    raise APIError(400, "INVALID_PARAMETER",
                   "Datetime parameters must include a timezone.")

  return value.astimezone(timezone.utc)


@router.get("/schedule", response_model=APIResponse[list[ScheduleMatch]])
def get_schedule(status: str = Query(default="all", max_length=32),
                 stage: str | None = Query(default=None, max_length=128),
                 team: str | None = Query(default=None, max_length=128),
                 from_: datetime | None = Query(default=None, alias="from"),
                 to: datetime | None = Query(default=None),
                 database: Database = Depends(get_database)):
  if status not in SCHEDULE_STATUS_FILTERS:
    raise APIError(400, "INVALID_FILTER",
                   f"Unsupported schedule status: {status}")

  from_at = _utc_datetime(from_)
  to_at = _utc_datetime(to)

  if from_at is not None and to_at is not None and from_at > to_at:
    raise APIError(
        400, "INVALID_PARAMETER",
        "The from datetime must be before or equal to the to datetime.")

  matches = list_schedule(database,
                          get_settings().tournament_slug, status,
                          stage.strip() if stage and stage.strip() else None,
                          team.strip() if team else None, from_at, to_at)

  for match in matches:
    match["scheduledAt"] = utc_datetime_string(match.get("scheduledAt"))

  return success_object(matches, {"total": len(matches)})
