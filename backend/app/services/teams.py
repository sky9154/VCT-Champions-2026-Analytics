from typing import Any

from pymongo.database import Database

from app.api.errors import APIError
from app.api.responses import public_identifier, utc_datetime_string
from app.assets import public_team_logo_url
from app.services.analytics import match_result_for_team, team_summary, utc_datetime
from app.services.common import find_tournament_id, referenced_team_ids
from app.config import get_settings

MATCH_PROJECTION = {
    "externalMatchId": 1,
    "stage": 1,
    "status": 1,
    "scheduledAt": 1,
    "teams": 1,
    "score": 1,
    "winnerTeamId": 1
}
TEAM_MAP_STATS_PROJECTION = {
    "teamId": 1,
    "matchId": 1,
    "matchMapId": 1,
    "mapName": 1,
    "roundsPlayed": 1,
    "roundsWon": 1,
    "roundsLost": 1,
    "kills": 1,
    "deaths": 1,
    "assists": 1,
    "firstKills": 1,
    "firstDeaths": 1,
    "attack": 1,
    "defense": 1
}


def _find_tournament_matches(
    database: Database,
    tournament_id: Any,
    stage: str | None = None,
    team_id: Any | None = None) -> list[dict[str, Any]]:
  query: dict[str, Any] = {"tournamentId": tournament_id}

  if stage:
    query["stage"] = stage

  if team_id is not None:
    query["$or"] = [{"teams.teamAId": team_id}, {"teams.teamBId": team_id}]

  return list(database["matches"].find(query, MATCH_PROJECTION))


def _completed_map_ids(database: Database, tournament_id: Any,
                       match_ids: list[Any]) -> set[Any]:
  if not match_ids:
    return set()

  return {
      match_map["_id"]
      for match_map in database["match_maps"].find(
          {
              "tournamentId": tournament_id,
              "matchId": {
                  "$in": match_ids
              },
              "status": "completed"
          }, {"_id": 1}) if match_map.get("_id") is not None
  }


def _find_team_map_stats(database: Database, tournament_id: Any,
                         team_ids: list[Any],
                         match_ids: list[Any]) -> list[dict[str, Any]]:
  if not team_ids or not match_ids:
    return []

  completed_map_ids = _completed_map_ids(database, tournament_id, match_ids)

  if not completed_map_ids:
    return []

  query = {
      "tournamentId": tournament_id,
      "teamId": {
          "$in": team_ids
      },
      "matchId": {
          "$in": match_ids
      },
      "matchMapId": {
          "$in": list(completed_map_ids)
      }
  }

  return list(database["team_map_stats"].find(query,
                                              TEAM_MAP_STATS_PROJECTION).sort([
                                                  ("matchMapId", 1), ("_id", 1)
                                              ]))


def _team_payload(team: dict[str, Any]) -> dict[str, Any]:
  return {
      "slug":
      public_identifier(team.get("slug")),
      "name":
      team.get("name"),
      "shortName":
      team.get("shortName"),
      "region":
      team.get("region"),
      "logoUrl":
      public_team_logo_url(get_settings(), public_identifier(team.get("slug")))
  }


def _sort_ranked_teams(items: list[dict[str, Any]], sort: str,
                       order: str) -> list[dict[str, Any]]:

  def tie_key(item: dict[str, Any]) -> tuple[int, str, str]:
    name = item["team"].get("name")
    slug = item["team"].get("slug")

    return (-item["stats"]["mapsPlayed"],
            name.casefold() if isinstance(name, str) else "",
            slug.casefold() if isinstance(slug, str) else "")

  items.sort(key=tie_key)
  valid = [item for item in items if item["stats"].get(sort) is not None]
  missing = [item for item in items if item["stats"].get(sort) is None]
  valid.sort(key=lambda item: item["stats"][sort], reverse=order == "desc")

  return valid + missing


def _team_stats_for_output(summary: dict[str, Any]) -> dict[str, Any]:
  fields = ("matchesPlayed", "matchesWon", "matchesLost", "matchWinPercentage",
            "mapsPlayed", "mapsWon", "mapsLost", "mapWinPercentage",
            "roundsPlayed", "roundsWon", "roundsLost", "roundWinPercentage",
            "attackRoundWinPercentage", "defenseRoundWinPercentage",
            "roundDifferential", "kd", "firstKillPercentage")

  return {field: summary[field] for field in fields}


def list_teams(database: Database, tournament_slug: str, stage: str | None,
               region: str | None, sort: str,
               order: str) -> tuple[list[dict[str, Any]], int]:
  tournament_id = find_tournament_id(database, tournament_slug)

  if tournament_id is None:
    return [], 0

  matches = _find_tournament_matches(database, tournament_id, stage)
  team_ids = {
      team_id
      for match in matches
      for team_id in referenced_team_ids(match)
  }

  if not team_ids:
    return [], 0

  team_query: dict[str, Any] = {"_id": {"$in": list(team_ids)}}

  if region:
    team_query["region"] = region

  teams = list(database["teams"].find(team_query, {
      "slug": 1,
      "name": 1,
      "shortName": 1,
      "region": 1,
      "logoUrl": 1
  }))

  if not teams:
    return [], 0

  match_ids = [
      match["_id"] for match in matches
      if match.get("_id") is not None and match.get("status") == "completed"
  ]
  result_team_ids = [team["_id"] for team in teams]
  team_map_rows = _find_team_map_stats(database, tournament_id,
                                       result_team_ids, match_ids)
  stats_by_team: dict[Any, list[dict[str, Any]]] = {
      team_id: []
      for team_id in result_team_ids
  }

  for row in team_map_rows:
    stats_by_team.setdefault(row.get("teamId"), []).append(row)

  items = []

  for team in teams:
    summary = team_summary(team["_id"], matches,
                           stats_by_team.get(team["_id"], []))
    items.append({
        "team": _team_payload(team),
        "stats": _team_stats_for_output(summary)
    })

  items = _sort_ranked_teams(items, sort, order)

  return [{
      "rank": index,
      **item
  } for index, item in enumerate(items, start=1)], len(items)


def _recent_match_sort_key(
    match: dict[str, Any]) -> tuple[bool, float | str, bool, str, str]:
  scheduled_at = utc_datetime(match.get("scheduledAt"))
  external_id = public_identifier(match.get("externalMatchId"))

  return (scheduled_at is None,
          -(scheduled_at.timestamp()) if scheduled_at is not None else "",
          external_id is None, external_id.casefold() if external_id else "",
          external_id or "")


def _recent_matches(database: Database, matches: list[dict[str, Any]],
                    team: dict[str, Any]) -> list[dict[str, Any]]:
  team_id = team["_id"]
  recent_matches = [
      match for match in matches
      if match.get("status") in {"completed", "live"}
  ]
  recent_matches.sort(key=_recent_match_sort_key)
  selected = recent_matches[:5]
  opponent_ids = {
      opponent_id
      for match in selected
      for opponent_id, _ in [match_result_for_team(match, team_id)]
      if opponent_id is not None and opponent_id != team_id
  }
  opponents_by_id = {
      opponent["_id"]: opponent
      for opponent in database["teams"].find(
          {"_id": {
              "$in": list(opponent_ids)
          }}, {
              "slug": 1,
              "name": 1
          })
  } if opponent_ids else {}

  result = []

  for match in selected:
    opponent_id, match_result = match_result_for_team(match, team_id)
    status = match.get("status")

    if status == "live":
      match_result["win"] = None

    opponent = opponents_by_id.get(opponent_id)
    result.append({
        "matchId": public_identifier(match.get("externalMatchId")),
        "scheduledAt": utc_datetime_string(match.get("scheduledAt")),
        "status": status,
        "opponent": {
            "slug":
            public_identifier(opponent.get("slug")),
            "name":
            opponent.get("name"),
            "logoUrl":
            public_team_logo_url(get_settings(),
                                 public_identifier(opponent.get("slug")))
        } if opponent else None,
        "result": match_result
    })

  return result


def get_team_detail(database: Database, tournament_slug: str,
                    slug: str) -> dict[str, Any]:
  tournament_id = find_tournament_id(database, tournament_slug)
  team = database["teams"].find_one({"slug": slug}, {
      "slug": 1,
      "name": 1,
      "shortName": 1,
      "region": 1,
      "logoUrl": 1
  })

  if tournament_id is None or team is None:
    raise APIError(404, "NOT_FOUND", "Team not found.")

  matches = _find_tournament_matches(database,
                                     tournament_id,
                                     team_id=team["_id"])

  if not matches:
    raise APIError(404, "NOT_FOUND", "Team not found.")

  match_ids = [
      match["_id"] for match in matches
      if match.get("_id") is not None and match.get("status") == "completed"
  ]
  team_stats = _find_team_map_stats(database, tournament_id, [team["_id"]],
                                    match_ids)
  summary = team_summary(team["_id"], matches, team_stats)

  player_ids = database["player_map_stats"].distinct(
      "playerId", {"tournamentId": tournament_id})
  roster = list(database["players"].find(
      {
          "_id": {
              "$in":
              [player_id for player_id in player_ids if player_id is not None]
          },
          "currentTeamId": team["_id"]
      }, {
          "slug": 1,
          "handle": 1,
          "realName": 1,
          "role": 1,
          "imageUrl": 1
      })) if player_ids else []

  roster.sort(key=lambda player: (player.get("handle").casefold(
  ) if isinstance(player.get("handle"), str) else "",
                                  public_identifier(player.get("slug")) or ""))

  map_rows_by_name: dict[str | None, list[dict[str, Any]]] = {}

  for row in team_stats:
    map_name = row.get("mapName")
    key = map_name.strip() if isinstance(map_name,
                                         str) and map_name.strip() else None
    map_rows_by_name.setdefault(key, []).append(row)

  map_performance = []

  for map_name, rows in map_rows_by_name.items():
    map_stats = _team_stats_for_output(team_summary(team["_id"], [], rows))
    map_performance.append({
        "map":
        map_name,
        "mapsPlayed":
        map_stats["mapsPlayed"],
        "mapsWon":
        map_stats["mapsWon"],
        "mapsLost":
        map_stats["mapsLost"],
        "roundsPlayed":
        map_stats["roundsPlayed"],
        "roundsWon":
        map_stats["roundsWon"],
        "roundsLost":
        map_stats["roundsLost"],
        "mapWinPercentage":
        map_stats["mapWinPercentage"],
        "roundWinPercentage":
        map_stats["roundWinPercentage"],
        "attackRoundWinPercentage":
        map_stats["attackRoundWinPercentage"],
        "defenseRoundWinPercentage":
        map_stats["defenseRoundWinPercentage"],
        "roundDifferential":
        map_stats["roundDifferential"],
        "kd":
        map_stats["kd"],
        "firstKillPercentage":
        map_stats["firstKillPercentage"]
    })

  map_performance.sort(key=lambda item: (
      -item["mapsPlayed"], item["map"] is None, item["map"].casefold()
      if isinstance(item["map"], str) else "", item["map"] or ""))

  return {
      "team":
      _team_payload(team),
      "summary":
      _team_stats_for_output(summary),
      "roster": [{
          "slug": public_identifier(player.get("slug")),
          "handle": player.get("handle"),
          "realName": player.get("realName"),
          "role": player.get("role"),
          "imageUrl": player.get("imageUrl")
      } for player in roster],
      "mapPerformance":
      map_performance,
      "recentMatches":
      _recent_matches(database, matches, team)
  }
