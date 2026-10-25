from fastapi import APIRouter, Depends, Query
from pymongo.database import Database

from app.api.dependencies import get_database
from app.api.errors import APIError
from app.api.responses import success_object
from app.api.schemas import APIResponse, PlayerDetailData, PlayerRankingItem, PlayerTrendData
from app.config import get_settings
from app.services.players import PLAYER_SORT_FIELDS, get_player_detail, list_players
from app.services.trends import (PLAYER_TREND_METRICS, TREND_INTERVALS,
                                 TREND_RANGES, TREND_SIDES, player_trend)

router = APIRouter()


@router.get("/players", response_model=APIResponse[list[PlayerRankingItem]])
def get_players(team: str | None = Query(default=None, max_length=128),
                role: str | None = Query(default=None, max_length=64),
                agent: str | None = Query(default=None, max_length=64),
                map_name: str | None = Query(default=None,
                                             alias="map",
                                             max_length=64),
                stage: str | None = Query(default=None, max_length=128),
                side: str = Query(default="overall", max_length=16),
                min_rounds: int = Query(default=0,
                                        alias="minRounds",
                                        ge=0,
                                        le=5000),
                sort: str = Query(default="rating", max_length=32),
                order: str = Query(default="desc", max_length=8),
                database: Database = Depends(get_database)):
  if side not in TREND_SIDES:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported player side: {side}.")

  if sort not in PLAYER_SORT_FIELDS:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported player sort: {sort}.")

  if order not in {"asc", "desc"}:
    raise APIError(400, "INVALID_PARAMETER", "Order must be asc or desc.")

  filters = {
      "team": team.strip() if team and team.strip() else None,
      "role": role.strip() if role and role.strip() else None,
      "agent": agent.strip() if agent and agent.strip() else None,
      "map": map_name.strip() if map_name and map_name.strip() else None,
      "stage": stage.strip() if stage and stage.strip() else None,
      "side": side
  }
  data, total = list_players(database,
                             get_settings().tournament_slug, filters["team"],
                             filters["role"], filters["agent"], filters["map"],
                             filters["stage"], side, min_rounds, sort, order)

  return success_object(
      data, {
          "filters": filters,
          "sort": sort,
          "order": order,
          "minRounds": min_rounds,
          "total": total
      })


@router.get("/players/{slug}", response_model=APIResponse[PlayerDetailData])
def get_player(slug: str, database: Database = Depends(get_database)):
  if not slug.strip() or len(slug) > 128:
    raise APIError(400, "INVALID_PARAMETER", "Invalid player slug.")

  data = get_player_detail(database,
                           get_settings().tournament_slug, slug.strip())

  return success_object(data)


@router.get("/players/{slug}/trend",
            response_model=APIResponse[PlayerTrendData])
def get_player_trend(slug: str,
                     metric: str = Query(..., max_length=64),
                     interval: str = Query(default="map", max_length=16),
                     map_name: str | None = Query(default=None,
                                                  alias="map",
                                                  max_length=64),
                     agent: str | None = Query(default=None, max_length=64),
                     stage: str | None = Query(default=None, max_length=128),
                     side: str = Query(default="overall", max_length=16),
                     opponent: str | None = Query(default=None,
                                                  max_length=128),
                     range_value: str = Query(default="all",
                                              alias="range",
                                              max_length=16),
                     database: Database = Depends(get_database)):
  clean_slug = slug.strip()

  if not clean_slug or len(clean_slug) > 128:
    raise APIError(400, "INVALID_PARAMETER", "Invalid player slug.")

  if metric not in PLAYER_TREND_METRICS:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported player trend metric: {metric}.")

  if interval not in TREND_INTERVALS:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported trend interval: {interval}.")

  if side not in TREND_SIDES:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported trend side: {side}.")

  if range_value not in TREND_RANGES:
    raise APIError(400, "INVALID_PARAMETER",
                   f"Unsupported trend range: {range_value}.")

  clean_map = map_name.strip() if map_name and map_name.strip() else None
  clean_agent = agent.strip() if agent and agent.strip() else None
  clean_stage = stage.strip() if stage and stage.strip() else None
  clean_opponent = opponent.strip() if opponent and opponent.strip() else None
  data = player_trend(database,
                      get_settings().tournament_slug, clean_slug, metric,
                      interval, clean_map, clean_agent, clean_stage, side,
                      clean_opponent, range_value)

  return success_object(data)
