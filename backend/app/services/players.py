from collections import defaultdict
from typing import Any

from pymongo.database import Database

from app.api.errors import APIError
from app.api.responses import public_identifier, utc_datetime_string
from app.assets import public_team_logo_url
from app.config import get_settings
from app.services.analytics import (aggregate_player_stats,
                                    match_result_for_team, match_team_ids,
                                    select_player_stat, utc_datetime)
from app.services.common import find_tournament_id

PLAYER_PROJECTION = {
    "slug": 1,
    "handle": 1,
    "realName": 1,
    "role": 1,
    "currentTeamId": 1,
    "imageUrl": 1
}
PLAYER_STATS_PROJECTION = {
    "playerId": 1,
    "teamId": 1,
    "matchId": 1,
    "matchMapId": 1,
    "mapName": 1,
    "agent": 1,
    "roundsPlayed": 1,
    "overall": 1,
    "attack": 1,
    "defense": 1
}
MATCH_PROJECTION = {
    "externalMatchId": 1,
    "scheduledAt": 1,
    "status": 1,
    "teams": 1,
    "score": 1,
    "winnerTeamId": 1
}
PLAYER_SORT_FIELDS = {
    "rating": "rating",
    "acs": "acs",
    "kd": "kd",
    "kast": "kast",
    "adr": "adr",
    "kpr": "kpr",
    "apr": "apr",
    "fk": "firstKills",
    "fd": "firstDeaths",
    "fkfd": "fkfd",
    "fkpr": "fkpr",
    "fdpr": "fdpr",
    "hs": "headshotPercentage"
}


def _tournament_matches(database: Database, tournament_id: Any,
                        stage: str | None) -> list[dict[str, Any]]:
  query: dict[str, Any] = {"tournamentId": tournament_id}

  if stage:
    query["stage"] = stage

  return list(database["matches"].find(query, {
      "_id": 1,
      "status": 1,
      "stage": 1
  }))


def _player_stats_query(tournament_id: Any, match_ids: list[Any],
                        team_id: Any | None, agent: str | None,
                        map_name: str | None, side: str) -> dict[str, Any]:
  query: dict[str, Any] = {
      "tournamentId": tournament_id,
      "matchId": {
          "$in": match_ids
      },
      ("roundsPlayed" if side == "overall" else f"{side}.roundsPlayed"): {
          "$exists": True
      }
  }

  if team_id is not None:
    query["teamId"] = team_id

  if agent:
    query["agent"] = agent

  if map_name:
    query["mapName"] = map_name

  return query


def _selected_stats(rows: list[dict[str, Any]],
                    side: str) -> list[dict[str, Any]]:
  return [
      selected for row in rows
      if (selected := select_player_stat(row, side)) is not None
  ]


def _player_payload(player: dict[str, Any],
                    team: dict[str, Any] | None) -> dict[str, Any]:
  return {
      "slug": public_identifier(player.get("slug")),
      "handle": player.get("handle"),
      "role": player.get("role"),
      "team": {
          "slug": public_identifier(team.get("slug")),
          "name": team.get("name"),
          "shortName": team.get("shortName")
      } if team else None
  }


def _group_rows(rows: list[dict[str, Any]],
                field: str) -> dict[str | None, list[dict[str, Any]]]:
  grouped: dict[str | None, list[dict[str, Any]]] = defaultdict(list)

  for row in rows:
    value = row.get(field)
    key = value.strip() if isinstance(value, str) and value.strip() else None
    grouped[key].append(row)

  return grouped


def _sorted_performance(rows: list[dict[str, Any]],
                        field: str) -> list[dict[str, Any]]:
  output_field = "map" if field == "mapName" else field
  result = []

  for value, group in _group_rows(rows, field).items():
    if value is None:
      continue

    stats = aggregate_player_stats(group)
    result.append({
        output_field: value,
        "mapsPlayed": stats["mapsPlayed"],
        "roundsPlayed": stats["roundsPlayed"],
        "rating": stats["rating"],
        "acs": stats["acs"],
        "kd": stats["kd"],
        "kast": stats["kast"],
        "adr": stats["adr"]
    })

  result.sort(key=lambda item: (-item["mapsPlayed"], item[output_field].
                                casefold(), item[output_field]))

  return result


def _stable_player_sort_key(player: dict[str, Any]) -> tuple[str, str]:
  handle = player.get("handle")
  slug = public_identifier(player.get("slug"))

  return (handle.casefold() if isinstance(handle, str) else "",
          slug.casefold() if slug else "")


def _rank_players(players: list[dict[str, Any]],
                  stats_by_player: dict[Any, dict[str, Any]], sort: str,
                  order: str) -> list[dict[str, Any]]:
  metric = PLAYER_SORT_FIELDS[sort]
  players.sort(key=lambda player: (-stats_by_player[player["_id"]][
      "roundsPlayed"], *_stable_player_sort_key(player)))
  present = [
      player for player in players
      if stats_by_player[player["_id"]].get(metric) is not None
  ]
  missing = [
      player for player in players
      if stats_by_player[player["_id"]].get(metric) is None
  ]
  present.sort(key=lambda player: stats_by_player[player["_id"]][metric],
               reverse=order == "desc")

  return present + missing


def list_players(database: Database, tournament_slug: str,
                 team_slug: str | None, role: str | None, agent: str | None,
                 map_name: str | None, stage: str | None, side: str,
                 min_rounds: int, sort: str,
                 order: str) -> tuple[list[dict[str, Any]], int]:
  tournament_id = find_tournament_id(database, tournament_slug)

  if tournament_id is None:
    return [], 0

  team_id = None

  if team_slug:
    team = database["teams"].find_one({"slug": team_slug}, {"_id": 1})

    if team is None:
      return [], 0

    team_id = team.get("_id")

  matches = _tournament_matches(database, tournament_id, stage)
  match_ids = [
      match["_id"] for match in matches if match.get("_id") is not None
  ]

  if not match_ids:
    return [], 0

  query = _player_stats_query(tournament_id, match_ids, team_id, agent,
                              map_name, side)
  player_ids = {
      player_id
      for player_id in database["player_map_stats"].distinct(
          "playerId", query) if player_id is not None
  }

  if not player_ids:
    return [], 0

  player_query: dict[str, Any] = {"_id": {"$in": list(player_ids)}}

  if role:
    player_query["role"] = role

  players = list(database["players"].find(player_query, PLAYER_PROJECTION))

  if not players:
    return [], 0

  selected_ids = [player["_id"] for player in players]
  stat_rows = list(database["player_map_stats"].find(
      {
          **query, "playerId": {
              "$in": selected_ids
          }
      }, PLAYER_STATS_PROJECTION).sort([("playerId", 1), ("matchMapId", 1),
                                        ("_id", 1)]))
  rows_by_player: dict[Any, list[dict[str, Any]]] = defaultdict(list)

  for row in _selected_stats(stat_rows, side):
    player_id = row.get("playerId")

    if player_id is not None:
      rows_by_player[player_id].append(row)

  stats_by_player = {
      player_id: aggregate_player_stats(rows)
      for player_id, rows in rows_by_player.items()
  }
  players = [
      player for player in players if player.get("_id") in stats_by_player
      and stats_by_player[player["_id"]]["roundsPlayed"] >= min_rounds
  ]

  if not players:
    return [], 0

  team_ids = {
      player.get("currentTeamId")
      for player in players if player.get("currentTeamId") is not None
  }
  teams_by_id = {
      team["_id"]: team
      for team in database["teams"].find({"_id": {
          "$in": list(team_ids)
      }}, {
          "slug": 1,
          "name": 1,
          "shortName": 1
      })
  } if team_ids else {}
  ranked = _rank_players(players, stats_by_player, sort, order)
  data = []

  for rank, player in enumerate(ranked, start=1):
    data.append({
        "rank":
        rank,
        "player":
        _player_payload(player, teams_by_id.get(player.get("currentTeamId"))),
        "stats":
        stats_by_player[player["_id"]]
    })

  return data, len(data)


def _player_detail_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
  stats = aggregate_player_stats(rows)
  fields = ("mapsPlayed", "roundsPlayed", "rating", "acs", "kills", "deaths",
            "assists", "kd", "kast", "adr", "kpr", "apr", "firstKills",
            "firstDeaths", "fkfd", "fkpr", "fdpr", "headshotPercentage")

  return {field: stats[field] for field in fields}


def _player_recent_matches(database: Database, player_rows: list[dict[str,
                                                                      Any]],
                           tournament_id: Any) -> list[dict[str, Any]]:
  rows_by_match: dict[Any, list[dict[str, Any]]] = defaultdict(list)
  team_ids_by_match: dict[Any, set[Any]] = defaultdict(set)

  for raw_row in player_rows:
    match_id = raw_row.get("matchId")

    if match_id is not None and raw_row.get("teamId") is not None:
      team_ids_by_match[match_id].add(raw_row["teamId"])

    row = select_player_stat(raw_row, "overall")

    if row is None:
      continue

    match_id = row.get("matchId")

    if match_id is not None:
      rows_by_match[match_id].append(row)

  match_ids = list(rows_by_match)

  if not match_ids:
    return []

  matches = list(database["matches"].find(
      {
          "_id": {
              "$in": match_ids
          },
          "tournamentId": tournament_id,
          "status": "completed"
      }, MATCH_PROJECTION))

  matches.sort(key=lambda match: (
      (scheduled_at := utc_datetime(match.get("scheduledAt"))
       ) is None, -scheduled_at.timestamp() if scheduled_at is not None else 0,
      public_identifier(match.get("externalMatchId")) is None,
      (public_identifier(match.get("externalMatchId")) or "").casefold(),
      public_identifier(match.get("externalMatchId")) or ""))
  selected = matches[:5]
  player_team_by_match: dict[Any, Any | None] = {}

  for match_id, team_ids in team_ids_by_match.items():
    player_team_by_match[match_id] = next(
        iter(team_ids)) if len(team_ids) == 1 else None

  opponent_ids = set()

  for match in selected:
    own_team_id = player_team_by_match.get(match.get("_id"))
    team_a_id, team_b_id = match_team_ids(match)

    if own_team_id == team_a_id:
      opponent_ids.add(team_b_id)
    elif own_team_id == team_b_id:
      opponent_ids.add(team_a_id)

  opponents_by_id = {
      team["_id"]: team
      for team in database["teams"].find({"_id": {
          "$in": list(opponent_ids)
      }}, {
          "slug": 1,
          "name": 1
      })
  } if opponent_ids else {}

  result = []

  for match in selected:
    match_id = match.get("_id")
    own_team_id = player_team_by_match.get(match_id)
    team_a_id, team_b_id = match_team_ids(match)

    if own_team_id not in (team_a_id, team_b_id):
      opponent_id = None
      match_result = {"win": None, "ownScore": None, "opponentScore": None}
    else:
      opponent_id, match_result = match_result_for_team(match, own_team_id)

    summary = aggregate_player_stats(rows_by_match[match_id])
    opponent = opponents_by_id.get(opponent_id)
    result.append({
        "matchId": public_identifier(match.get("externalMatchId")),
        "scheduledAt": utc_datetime_string(match.get("scheduledAt")),
        "status": "completed",
        "opponent": {
            "slug":
            public_identifier(opponent.get("slug")),
            "name":
            opponent.get("name"),
            "logoUrl":
            public_team_logo_url(get_settings(),
                                 public_identifier(opponent.get("slug")))
        } if opponent else None,
        "result": match_result,
        "rating": summary["rating"],
        "acs": summary["acs"],
        "kd": summary["kd"]
    })

  return result


def get_player_detail(database: Database, tournament_slug: str,
                      slug: str) -> dict[str, Any]:
  player = database["players"].find_one({"slug": slug}, PLAYER_PROJECTION)
  tournament_id = find_tournament_id(database, tournament_slug)

  if player is None or tournament_id is None:
    raise APIError(404, "NOT_FOUND", "Player not found.")

  raw_rows = list(database["player_map_stats"].find(
      {
          "tournamentId": tournament_id,
          "playerId": player["_id"]
      }, PLAYER_STATS_PROJECTION).sort([("matchId", 1), ("matchMapId", 1),
                                        ("_id", 1)]))
  rows = _selected_stats(raw_rows, "overall")

  if not rows:
    raise APIError(404, "NOT_FOUND", "Player not found.")

  current_team = database["teams"].find_one(
      {"_id": player.get("currentTeamId")}, {
          "slug": 1,
          "name": 1
      }) if player.get("currentTeamId") is not None else None
  player_payload = {
      "slug": public_identifier(player.get("slug")),
      "handle": player.get("handle"),
      "realName": player.get("realName"),
      "role": player.get("role"),
      "team": {
          "slug": public_identifier(current_team.get("slug")),
          "name": current_team.get("name")
      } if current_team else None
  }

  return {
      "player": player_payload,
      "summary": _player_detail_stats(rows),
      "mapPerformance": _sorted_performance(rows, "mapName"),
      "agentPerformance": _sorted_performance(rows, "agent"),
      "recentMatches": _player_recent_matches(database, raw_rows,
                                              tournament_id)
  }
