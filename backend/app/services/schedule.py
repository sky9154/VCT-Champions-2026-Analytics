from datetime import datetime
from typing import Any

from pymongo.database import Database

from app.api.responses import public_identifier
from app.assets import public_team_logo_url
from app.services.common import find_tournament_id
from app.config import get_settings

SCHEDULE_STATUS_FILTERS = {
    "all": None,
    "upcoming": ["scheduled"],
    "live": ["live"],
    "completed": ["completed"]
}


def _team_payload(team: dict[str, Any] | None) -> dict[str, Any] | None:
  if team is None:
    return None

  return {
      "slug":
      public_identifier(team.get("slug")),
      "name":
      team.get("name"),
      "shortName":
      team.get("shortName"),
      "logoUrl":
      public_team_logo_url(get_settings(), public_identifier(team.get("slug")))
  }


def list_schedule(database: Database, tournament_slug: str, status_filter: str,
                  stage: str | None, team_slug: str | None,
                  from_at: datetime | None,
                  to_at: datetime | None) -> list[dict[str, Any]]:
  tournament_id = find_tournament_id(database, tournament_slug)

  if tournament_id is None:
    return []

  query: dict[str, Any] = {"tournamentId": tournament_id}
  statuses = SCHEDULE_STATUS_FILTERS[status_filter]

  if statuses is not None:
    query["status"] = {"$in": statuses}

  if stage:
    query["stage"] = stage

  if team_slug:
    team = database["teams"].find_one({"slug": team_slug}, {"_id": 1})

    if team is None:
      return []

    query["$or"] = [{
        "teams.teamAId": team["_id"]
    }, {
        "teams.teamBId": team["_id"]
    }]

  if from_at is not None or to_at is not None:
    time_query: dict[str, datetime] = {}

    if from_at is not None:
      time_query["$gte"] = from_at

    if to_at is not None:
      time_query["$lte"] = to_at

    query["scheduledAt"] = time_query

  matches = list(database["matches"].find(
      query, {
          "externalMatchId": 1,
          "stage": 1,
          "group": 1,
          "round": 1,
          "scheduledAt": 1,
          "status": 1,
          "bestOf": 1,
          "teams": 1,
          "score": 1
      }).sort([("scheduledAt", 1), ("externalMatchId", 1), ("_id", 1)]))

  team_ids = {
      team_id
      for match in matches
      for team_id in (((match.get("teams") or {}).get("teamAId"),
                       (match.get("teams") or {}).get("teamBId")
                       ) if isinstance(match.get("teams"), dict) else (None,
                                                                       None))
      if team_id is not None
  }
  teams_by_id = {
      team["_id"]: team
      for team in database["teams"].find({"_id": {
          "$in": list(team_ids)
      }}, {
          "slug": 1,
          "name": 1,
          "shortName": 1,
          "logoUrl": 1
      })
  } if team_ids else {}

  result = []

  for match in matches:
    match_teams = match.get("teams") or {}
    score = match.get("score") or {}

    if not isinstance(match_teams, dict):
      match_teams = {}

    if not isinstance(score, dict):
      score = {}

    result.append({
        "id":
        public_identifier(match.get("externalMatchId")),
        "stage":
        match.get("stage"),
        "group":
        match.get("group"),
        "round":
        match.get("round"),
        "scheduledAt":
        match.get("scheduledAt"),
        "status":
        match.get("status"),
        "bestOf":
        match.get("bestOf"),
        "teamA":
        _team_payload(teams_by_id.get(match_teams.get("teamAId"))),
        "teamB":
        _team_payload(teams_by_id.get(match_teams.get("teamBId"))),
        "score": {
            "teamA": score.get("teamA"),
            "teamB": score.get("teamB")
        }
    })

  return result
