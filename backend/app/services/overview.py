from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from app.api.errors import APIError
from app.api.responses import public_identifier, utc_datetime_string
from app.services.players import list_players
from app.services.schedule import list_schedule
from app.services.teams import list_teams
from app.services.analytics import utc_datetime


def _scheduled_key(match: dict[str, Any],
                   descending: bool) -> tuple[bool, float, bool, str, str]:
  scheduled_at = utc_datetime(match.get("scheduledAt"))
  external_id = public_identifier(match.get("id"))
  timestamp = scheduled_at.timestamp() if scheduled_at is not None else 0

  return (scheduled_at
          is None, -timestamp if descending else timestamp, external_id
          is None, external_id.casefold() if external_id else "", external_id
          or "")


def _schedule_response(match: dict[str, Any]) -> dict[str, Any]:
  return {
      **match, "scheduledAt": utc_datetime_string(match.get("scheduledAt"))
  }


def get_overview(database: Database, tournament_slug: str) -> dict[str, Any]:
  now = datetime.now(timezone.utc)
  tournament = database["tournaments"].find_one({"slug": tournament_slug}, {
      "slug": 1,
      "name": 1,
      "status": 1,
      "currentStage": 1,
      "startDate": 1,
      "endDate": 1
  })

  if tournament is None:
    raise APIError(404, "NOT_FOUND", "Tournament not found.")

  matches = list_schedule(database, tournament_slug, "all", None, None, None,
                          None)
  matches_completed = sum(
      match.get("status") == "completed" for match in matches)

  upcoming = [
      match for match in matches
      if match.get("status") == "scheduled" and (scheduled_at := utc_datetime(
          match.get("scheduledAt"))) is not None and scheduled_at >= now
  ]

  upcoming.sort(key=lambda match: _scheduled_key(match, descending=False))

  recent = [match for match in matches if match.get("status") == "completed"]
  recent.sort(key=lambda match: _scheduled_key(match, descending=True))

  top_players, _ = list_players(database, tournament_slug, None, None, None,
                                None, None, "overall", 0, "rating", "desc")
  top_teams, _ = list_teams(database, tournament_slug, None, None,
                            "mapWinPercentage", "desc")

  return {
      "tournament": {
          "slug": public_identifier(tournament.get("slug")) or tournament_slug,
          "name": tournament.get("name"),
          "status": tournament.get("status"),
          "currentStage": tournament.get("currentStage"),
          "startDate": utc_datetime_string(tournament.get("startDate")),
          "endDate": utc_datetime_string(tournament.get("endDate"))
      },
      "progress": {
          "matchesCompleted": matches_completed,
          "matchesTotal": len(matches)
      },
      "upcomingMatches": [_schedule_response(match) for match in upcoming[:5]],
      "recentResults": [_schedule_response(match) for match in recent[:5]],
      "topPlayers": top_players[:5],
      "topTeams": top_teams[:5]
  }
