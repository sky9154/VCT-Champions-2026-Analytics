from fastapi import APIRouter, Depends, Query
from pymongo.database import Database

from app.api.dependencies import get_database
from app.api.errors import APIError
from app.api.responses import success_object
from app.api.schemas import APIResponse, TeamDetailData, TeamRankingItem, TeamTrendData
from app.config import get_settings
from app.services.teams import list_teams, get_team_detail
from app.services.trends import TEAM_TREND_METRICS, TREND_INTERVALS, TREND_RANGES, team_trend

router = APIRouter()

TEAM_SORT_FIELDS = {
    "matchesPlayed", "matchesWon", "matchWinPercentage", "mapsPlayed",
    "mapsWon", "mapWinPercentage", "roundWinPercentage",
    "attackRoundWinPercentage", "defenseRoundWinPercentage",
    "roundDifferential", "kd", "firstKillPercentage"
}


@router.get("/teams", response_model=APIResponse[list[TeamRankingItem]])

def get_teams(stage: str | None = Query(default=None, max_length=128),
              region: str | None = Query(default=None, max_length=32),
              sort: str = Query(default="mapWinPercentage", max_length=64),
              order: str = Query(default="desc", max_length=8),
              database: Database = Depends(get_database)):
  if sort not in TEAM_SORT_FIELDS:
    raise APIError(400, "INVALID_PARAMETER", f"Unsupported team sort: {sort}.")

  if order not in {"asc", "desc"}:
    raise APIError(400, "INVALID_PARAMETER", "Order must be asc or desc.")

  clean_stage = stage.strip() if stage and stage.strip() else None
  clean_region = region.strip() if region and region.strip() else None
  data, total = list_teams(database,
                           get_settings().tournament_slug, clean_stage,
                           clean_region, sort, order)

  return success_object(data, {"sort": sort, "order": order, "total": total})


@router.get("/teams/{slug}", response_model=APIResponse[TeamDetailData])
def get_team(
    slug: str,
    database: Database = Depends(get_database),
):
  if not slug.strip() or len(slug) > 128:
    raise APIError(400, "INVALID_PARAMETER", "Invalid team slug.")

  data = get_team_detail(database,
                         get_settings().tournament_slug, slug.strip())

  return success_object(data)


@router.get("/teams/{slug}/trend", response_model=APIResponse[TeamTrendData])
def get_team_trend(slug: str,
                   metric: str = Query(..., max_length=64),
                   interval: str = Query(default="match", max_length=16),
                   map_name: str | None = Query(default=None,
                                                alias="map",
                                                max_length=64),
                   stage: str | None = Query(default=None, max_length=128),
                   opponent: str | None = Query(default=None, max_length=128),
                   range_value: str = Query(default="all",
                                            alias="range",
                                            max_length=16),
                   database: Database = Depends(get_database)):
  clean_slug = slug.strip()

  if not clean_slug or len(clean_slug) > 128:
    raise APIError(400, "INVALID_PARAMETER", "Invalid team slug.")

  if metric not in TEAM_TREND_METRICS:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported team trend metric: {metric}.")

  if interval not in TREND_INTERVALS:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported trend interval: {interval}.")

  if range_value not in TREND_RANGES:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported trend range: {range_value}.")

  clean_map = map_name.strip() if map_name and map_name.strip() else None
  clean_stage = stage.strip() if stage and stage.strip() else None
  clean_opponent = opponent.strip() if opponent and opponent.strip() else None
  data = team_trend(database,
                    get_settings().tournament_slug, clean_slug, metric,
                    interval, clean_map, clean_stage, clean_opponent,
                    range_value)

  return success_object(data)
