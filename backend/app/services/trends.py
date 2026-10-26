from collections import defaultdict
from typing import Any

from pymongo.database import Database

from app.api.errors import APIError
from app.api.responses import public_identifier, utc_datetime_string
from app.services.analytics import (aggregate_player_stats,
                                    aggregate_team_map_stats,
                                    match_map_result_for_team,
                                    match_result_for_team, match_team_ids,
                                    select_player_stat, utc_datetime)
from app.services.common import find_tournament_id

PLAYER_TREND_FIELDS = {
    "rating": "rating",
    "acs": "acs",
    "kd": "kd",
    "kast": "kast",
    "adr": "adr",
    "kpr": "kpr",
    "apr": "apr",
    "fkpr": "fkpr",
    "fdpr": "fdpr",
    "hs": "headshotPercentage"
}
PLAYER_TREND_METRICS = set(PLAYER_TREND_FIELDS)
TEAM_MAP_METRICS = {
    "roundWinPercentage", "roundDifferential", "attackRoundWinPercentage",
    "defenseRoundWinPercentage", "kd", "firstKillPercentage"
}
TEAM_PLAYER_METRICS = {"avgAcs", "avgRating", "kast", "adr"}
TEAM_TREND_METRICS = TEAM_MAP_METRICS | TEAM_PLAYER_METRICS
TREND_INTERVALS = {"map", "match"}
TREND_RANGES = {"all", "last5", "last10"}
TREND_SIDES = {"overall", "attack", "defense"}

MATCH_PROJECTION = {
    "externalMatchId": 1,
    "stage": 1,
    "scheduledAt": 1,
    "status": 1,
    "teams": 1,
    "score": 1,
    "winnerTeamId": 1
}
MATCH_MAP_PROJECTION = {
    "externalMapId": 1,
    "mapNumber": 1,
    "mapName": 1,
    "matchId": 1,
    "status": 1,
    "teamAId": 1,
    "teamBId": 1,
    "score": 1,
    "winnerTeamId": 1
}
TEAM_MAP_STATS_PROJECTION = {
    "matchId": 1,
    "matchMapId": 1,
    "teamId": 1,
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
PLAYER_MAP_STATS_PROJECTION = {
    "matchId": 1,
    "matchMapId": 1,
    "playerId": 1,
    "teamId": 1,
    "mapName": 1,
    "agent": 1,
    "roundsPlayed": 1,
    "overall": 1,
    "attack": 1,
    "defense": 1
}


def _empty_trend(metric: str, interval: str,
                 filters: dict[str, Any]) -> dict[str, Any]:
  return {
      "metric": metric,
      "interval": interval,
      "filters": filters,
      "average": None,
      "points": [],
      "coverage": {
          "pointCount": 0,
          "availableCount": 0
      }
  }


def _coverage(records: list[dict[str, Any]]) -> dict[str, int]:
  return {
      "pointCount":
      len(records),
      "availableCount":
      sum(1 for record in records
          if record["point"].get("dataAvailability") == "available")
  }


def _opponent_id(match: dict[str, Any], team_id: Any) -> Any | None:
  team_a_id, team_b_id = match_team_ids(match)

  if team_id == team_a_id:
    return team_b_id

  if team_id == team_b_id:
    return team_a_id

  return None


def _load_completed_matches(database: Database,
                            tournament_id: Any,
                            match_ids: list[Any] | None = None,
                            team_id: Any | None = None,
                            stage: str | None = None) -> list[dict[str, Any]]:
  if match_ids is not None and not match_ids:
    return []

  query: dict[str, Any] = {
      "tournamentId": tournament_id,
      "status": "completed"
  }

  if match_ids is not None:
    query["_id"] = {"$in": match_ids}

  if team_id is not None:
    query["$or"] = [{"teams.teamAId": team_id}, {"teams.teamBId": team_id}]

  if stage:
    query["stage"] = stage

  return [
      match for match in database["matches"].find(query, MATCH_PROJECTION)
      if isinstance(match.get("externalMatchId"), str)
      and match["externalMatchId"].strip()
  ]


def _load_completed_maps(
    database: Database, tournament_id: Any, matches: list[dict[str, Any]]
) -> tuple[dict[Any, dict[str, Any]], dict[Any, list[dict[str, Any]]]]:
  match_ids = [
      match["_id"] for match in matches if match.get("_id") is not None
  ]

  if not match_ids:
    return {}, {}

  maps = list(database["match_maps"].find(
      {
          "tournamentId": tournament_id,
          "matchId": {
              "$in": match_ids
          },
          "status": "completed"
      }, MATCH_MAP_PROJECTION).sort([("matchId", 1), ("mapNumber", 1),
                                     ("externalMapId", 1), ("_id", 1)]))
  match_by_id = {
      match["_id"]: match
      for match in matches if match.get("_id") is not None
  }
  maps_by_id: dict[Any, dict[str, Any]] = {}
  maps_by_match: dict[Any, list[dict[str, Any]]] = defaultdict(list)

  for match_map in maps:
    match_id = match_map.get("matchId")
    map_id = match_map.get("_id")
    external_map_id = match_map.get("externalMapId")

    if (map_id is None or match_id not in match_by_id
        or not isinstance(external_map_id, str)
        or not external_map_id.strip()):
      continue

    maps_by_id[map_id] = match_map
    maps_by_match[match_id].append(match_map)

  return maps_by_id, dict(maps_by_match)


def _load_teams(database: Database,
                matches: list[dict[str, Any]]) -> dict[Any, dict[str, Any]]:
  team_ids = {
      team_id
      for match in matches
      for team_id in match_team_ids(match) if team_id is not None
  }

  if not team_ids:
    return {}

  return {
      team["_id"]: team
      for team in database["teams"].find({"_id": {
          "$in": list(team_ids)
      }}, {
          "slug": 1,
          "name": 1,
          "shortName": 1
      })
  }


def _opponent_payload(
    team_id: Any | None,
    teams_by_id: dict[Any, dict[str, Any]]) -> dict[str, Any] | None:
  team = teams_by_id.get(team_id)

  if team is None:
    return None

  return {
      "slug": public_identifier(team.get("slug")),
      "name": team.get("name"),
      "shortName": team.get("shortName")
  }


def _point_record(
    point: dict[str, Any], scheduled_at: Any, map_number: Any,
    external_match_id: Any, external_map_id: Any,
    source_rows: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
  return {
      "point":
      point,
      "scheduledAt":
      scheduled_at,
      "mapNumber":
      map_number if isinstance(map_number, int) else None,
      "externalIdentity": (public_identifier(external_match_id),
                           public_identifier(external_map_id)),
      "sourceRows":
      source_rows
  }


def _point_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
  scheduled_at = utc_datetime(record.get("scheduledAt"))
  external_match_id, external_map_id = record["externalIdentity"]
  map_number = record.get("mapNumber")

  return (scheduled_at
          is None, scheduled_at.timestamp() if scheduled_at is not None else 0,
          map_number is None, map_number if map_number is not None else 0,
          external_match_id.casefold() if external_match_id else "",
          external_map_id.casefold() if external_map_id else "",
          external_match_id or "", external_map_id or "")


def _apply_range(records: list[dict[str, Any]],
                 range_value: str) -> list[dict[str, Any]]:
  records.sort(key=_point_sort_key)

  if range_value == "last5":
    return records[-5:]

  if range_value == "last10":
    return records[-10:]

  return records


def _select_player_rows(rows: list[dict[str, Any]],
                        side: str) -> list[dict[str, Any]]:
  return [
      selected for row in rows
      if (selected := select_player_stat(row, side)) is not None
  ]


def _player_trend_value(metric: str,
                        rows: list[dict[str, Any]]) -> float | int | None:
  return aggregate_player_stats(rows).get(
      PLAYER_TREND_FIELDS[metric]) if rows else None


def _player_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
  stats = aggregate_player_stats(rows)

  return {
      field: stats[field]
      for field in ("roundsPlayed", "rating", "acs", "kills", "deaths",
                    "assists", "kd", "kast", "adr")
  }


def _team_trend_value(metric: str, team_rows: list[dict[str, Any]],
                      player_rows: list[dict[str, Any]]) -> float | int | None:
  if metric in TEAM_MAP_METRICS:
    return aggregate_team_map_stats(team_rows).get(
        metric) if team_rows else None

  player_field = {
      "avgAcs": "acs",
      "avgRating": "rating",
      "kast": "kast",
      "adr": "adr"
  }[metric]

  return aggregate_player_stats(player_rows).get(
      player_field) if player_rows else None


def _round_differential_average(records: list[dict[str, Any]]) -> float | None:
  point_differentials = []

  for record in records:
    team_rows = record["sourceRows"]["team"]
    point_differential = aggregate_team_map_stats(team_rows).get(
        "roundDifferential")

    if point_differential is not None:
      point_differentials.append(point_differential)

  if not point_differentials:
    return None

  return round(sum(point_differentials) / len(point_differentials), 1)


def team_trend(database: Database, tournament_slug: str, slug: str,
               metric: str, interval: str, map_name: str | None,
               stage: str | None, opponent_slug: str | None,
               range_value: str) -> dict[str, Any]:
  tournament_id = find_tournament_id(database, tournament_slug)
  team = database["teams"].find_one({"slug": slug}, {
      "_id": 1,
      "slug": 1,
      "name": 1
  })

  if tournament_id is None or team is None:
    raise APIError(404, "NOT_FOUND", "Team not found.")

  team_id = team["_id"]
  in_event = database["matches"].find_one(
      {
          "tournamentId": tournament_id,
          "$or": [{
              "teams.teamAId": team_id
          }, {
              "teams.teamBId": team_id
          }]
      }, {"_id": 1})

  if in_event is None:
    raise APIError(404, "NOT_FOUND", "Team not found.")

  filters = {
      "map": map_name,
      "stage": stage,
      "opponent": opponent_slug,
      "range": range_value
  }
  opponent_id = None

  if opponent_slug:
    opponent = database["teams"].find_one({"slug": opponent_slug}, {"_id": 1})

    if opponent is None:
      return _empty_trend(metric, interval, filters)

    opponent_id = opponent.get("_id")

  matches = _load_completed_matches(database,
                                    tournament_id,
                                    team_id=team_id,
                                    stage=stage)

  if opponent_id is not None:
    matches = [
        match for match in matches
        if _opponent_id(match, team_id) == opponent_id
    ]

  matches = [
      match for match in matches if match.get("_id") is not None
      and public_identifier(match.get("externalMatchId")) is not None
  ]
  matches_by_id = {match["_id"]: match for match in matches}
  all_maps_by_id, _ = _load_completed_maps(database, tournament_id, matches)
  maps_by_id = {
      map_id: row
      for map_id, row in all_maps_by_id.items()
      if not map_name or row.get("mapName") == map_name
  }
  maps_by_match: dict[Any, list[dict[str, Any]]] = defaultdict(list)

  for match_map in maps_by_id.values():
    maps_by_match[match_map["matchId"]].append(match_map)

  for rows in maps_by_match.values():
    rows.sort(
        key=lambda row: (row.get("mapNumber") is None, row.get("mapNumber") or
                         0, row.get("externalMapId") or ""))

  match_ids = list(matches_by_id)
  map_ids = list(maps_by_id)
  team_rows = []

  if map_ids:
    team_rows = list(database["team_map_stats"].find(
        {
            "tournamentId": tournament_id,
            "teamId": team_id,
            "matchId": {
                "$in": match_ids
            },
            "matchMapId": {
                "$in": map_ids
            }
        }, TEAM_MAP_STATS_PROJECTION).sort([("matchId", 1), ("matchMapId", 1),
                                            ("_id", 1)]))

  team_rows_by_map: dict[Any, list[dict[str, Any]]] = defaultdict(list)

  for row in team_rows:
    match_map = maps_by_id.get(row.get("matchMapId"))

    if (row.get("matchId") in matches_by_id and match_map
        and match_map.get("matchId") == row.get("matchId")):
      team_rows_by_map[row["matchMapId"]].append(row)

  player_rows_by_map: dict[Any, list[dict[str, Any]]] = defaultdict(list)

  if metric in TEAM_PLAYER_METRICS and map_ids:
    raw_player_rows = database["player_map_stats"].find(
        {
            "tournamentId": tournament_id,
            "teamId": team_id,
            "matchId": {
                "$in": match_ids
            },
            "matchMapId": {
                "$in": map_ids
            }
        }, PLAYER_MAP_STATS_PROJECTION).sort([("matchId", 1),
                                              ("matchMapId", 1), ("_id", 1)])

    for row in raw_player_rows:
      match_map = maps_by_id.get(row.get("matchMapId"))

      if (row.get("matchId") in matches_by_id and match_map
          and match_map.get("matchId") == row.get("matchId")):
        for selected in _select_player_rows([row], "overall"):
          player_rows_by_map[row["matchMapId"]].append(selected)

  teams_by_id = _load_teams(database, matches)
  map_records = []

  for map_id, match_map in maps_by_id.items():
    match = matches_by_id.get(match_map.get("matchId"))

    if match is None:
      continue

    opponent_id_for_point = _opponent_id(match, team_id)
    map_team_rows = team_rows_by_map.get(map_id, [])
    map_player_rows = player_rows_by_map.get(map_id, [])
    opponent_id_from_map, map_result = match_map_result_for_team(
        match, match_map, team_id, canonical_winner_only=True)
    relevant_rows = map_team_rows if metric in TEAM_MAP_METRICS else map_player_rows

    if not relevant_rows:
      continue

    map_records.append(
        _point_record(
            {
                "timestamp": utc_datetime_string(match.get("scheduledAt")),
                "matchId": public_identifier(match.get("externalMatchId")),
                "matchMapId": public_identifier(
                    match_map.get("externalMapId")),
                "opponent": _opponent_payload(opponent_id_for_point,
                                              teams_by_id),
                "map": match_map.get("mapName"),
                "value": _team_trend_value(metric, map_team_rows,
                                           map_player_rows),
                "dataAvailability": "available",
                "result": map_result if opponent_id_from_map is not None else {
                    "win": None,
                    "ownScore": None,
                    "opponentScore": None
                }
            }, match.get("scheduledAt"), match_map.get("mapNumber"),
            match.get("externalMatchId"), match_map.get("externalMapId"), {
                "team": map_team_rows,
                "player": map_player_rows
            }))

  if interval == "map":
    records = map_records
  else:
    records = []

    for match_id, match in matches_by_id.items():
      match_maps = maps_by_match.get(match_id, [])
      aggregate_team_rows = [
          row for match_map in match_maps
          for row in team_rows_by_map.get(match_map.get("_id"), [])
      ]
      aggregate_player_rows = [
          row for match_map in match_maps
          for row in player_rows_by_map.get(match_map.get("_id"), [])
      ]
      opponent_id_for_point = _opponent_id(match, team_id)
      availability_rows = aggregate_team_rows if metric in TEAM_MAP_METRICS else aggregate_player_rows
      records.append(
          _point_record(
              {
                  "timestamp":
                  utc_datetime_string(match.get("scheduledAt")),
                  "matchId":
                  public_identifier(match.get("externalMatchId")),
                  "matchMapId":
                  None,
                  "opponent":
                  _opponent_payload(opponent_id_for_point, teams_by_id),
                  "map":
                  None,
                  "value":
                  _team_trend_value(metric, aggregate_team_rows,
                                    aggregate_player_rows),
                  "dataAvailability":
                  "available" if availability_rows else "pending",
                  "result":
                  match_result_for_team(
                      match, team_id, canonical_winner_only=True)[1]
              }, match.get("scheduledAt"), None, match.get("externalMatchId"),
              None, {
                  "team": aggregate_team_rows,
                  "player": aggregate_player_rows
              }))

  records = _apply_range(records, range_value)

  if metric == "roundDifferential":
    average = _round_differential_average(records)
  else:
    all_team_rows = [
        row for record in records for row in record["sourceRows"]["team"]
    ]
    all_player_rows = [
        row for record in records for row in record["sourceRows"]["player"]
    ]
    average = _team_trend_value(metric, all_team_rows, all_player_rows)

  return {
      "metric": metric,
      "interval": interval,
      "filters": filters,
      "average": average,
      "points": [record["point"] for record in records],
      "coverage": _coverage(records)
  }


def player_trend(database: Database, tournament_slug: str, slug: str,
                 metric: str, interval: str, map_name: str | None,
                 agent: str | None, stage: str | None, side: str,
                 opponent_slug: str | None,
                 range_value: str) -> dict[str, Any]:
  tournament_id = find_tournament_id(database, tournament_slug)
  player = database["players"].find_one({"slug": slug}, {
      "_id": 1,
      "currentTeamId": 1
  })

  if tournament_id is None or player is None:
    raise APIError(404, "NOT_FOUND", "Player not found.")

  raw_rows = list(database["player_map_stats"].find(
      {
          "tournamentId": tournament_id,
          "playerId": player["_id"]
      }, PLAYER_MAP_STATS_PROJECTION).sort([("matchId", 1), ("matchMapId", 1),
                                            ("_id", 1)]))
  filters = {
      "map": map_name,
      "agent": agent,
      "stage": stage,
      "side": side,
      "opponent": opponent_slug,
      "range": range_value
  }
  opponent_id = None

  if opponent_slug:
    opponent = database["teams"].find_one({"slug": opponent_slug}, {"_id": 1})

    if opponent is None:
      return _empty_trend(metric, interval, filters)

    opponent_id = opponent.get("_id")

  player_team_ids = {
      team_id
      for team_id in
      [player.get("currentTeamId"), *(row.get("teamId") for row in raw_rows)]
      if team_id is not None
  }

  if player_team_ids:
    matches_by_identity = {}

    for team_id in player_team_ids:
      for match in _load_completed_matches(database,
                                           tournament_id,
                                           team_id=team_id,
                                           stage=stage):
        matches_by_identity[match.get("_id")] = match

    matches = list(matches_by_identity.values())
  else:
    candidate_match_ids = list({
        row.get("matchId")
        for row in raw_rows if row.get("matchId") is not None
    })
    matches = _load_completed_matches(database,
                                      tournament_id,
                                      candidate_match_ids,
                                      stage=stage)

  matches_by_id = {
      match["_id"]: match
      for match in matches if match.get("_id") is not None
  }
  maps_by_id, _ = _load_completed_maps(database, tournament_id, matches)
  selected_rows = []
  stats_available_match_ids: set[Any] = set()

  for row in raw_rows:
    match_id = row.get("matchId")
    match_map = maps_by_id.get(row.get("matchMapId"))
    match = matches_by_id.get(match_id)
    map_value = row.get("mapName")

    if (match is None or match_map is None
        or match_map.get("matchId") != match_id
        or not isinstance(map_value, str) or not map_value.strip()
        or map_value != match_map.get("mapName")
        or row.get("teamId") not in match_team_ids(match)):
      continue

    selected = select_player_stat(row, side)

    if selected is None:
      continue

    stats_available_match_ids.add(match_id)

    if map_name and map_value != map_name:
      continue

    if agent and row.get("agent") != agent:
      continue

    if opponent_id is not None and _opponent_id(
        match, row.get("teamId")) != opponent_id:
      continue

    selected_rows.append(selected)

  teams_by_id = _load_teams(database, matches)
  rows_by_map: dict[Any, list[dict[str, Any]]] = defaultdict(list)
  rows_by_match: dict[Any, list[dict[str, Any]]] = defaultdict(list)

  for row in selected_rows:
    rows_by_map[row.get("matchMapId")].append(row)
    rows_by_match[row.get("matchId")].append(row)

  records = []

  if interval == "map":
    for map_id, rows in rows_by_map.items():
      match_map = maps_by_id.get(map_id)
      match = matches_by_id.get(rows[0].get("matchId")) if rows else None

      if match_map is None or match is None:
        continue

      team_ids = {
          row.get("teamId")
          for row in rows if row.get("teamId") is not None
      }
      team_id = next(iter(team_ids)) if len(team_ids) == 1 else None
      opponent_id_for_point = _opponent_id(
          match, team_id) if team_id is not None else None
      _, map_result = match_map_result_for_team(
          match, match_map, team_id,
          canonical_winner_only=True) if team_id is not None else (
              None, {
                  "win": None,
                  "ownScore": None,
                  "opponentScore": None
              })
      stats = aggregate_player_stats(rows)
      agent_values = {row.get("agent") for row in rows}
      agent_value = next(
          iter(agent_values)) if len(agent_values) == 1 else None
      records.append(
          _point_record(
              {
                  "timestamp": utc_datetime_string(match.get("scheduledAt")),
                  "matchId": public_identifier(match.get("externalMatchId")),
                  "matchMapId": public_identifier(
                      match_map.get("externalMapId")),
                  "opponent": _opponent_payload(opponent_id_for_point,
                                                teams_by_id),
                  "map": match_map.get("mapName"),
                  "agent": public_identifier(agent_value),
                  "value": stats.get(PLAYER_TREND_FIELDS[metric]),
                  "dataAvailability": "available",
                  "result": map_result,
                  "metadata": _player_metadata(rows)
              }, match.get("scheduledAt"), match_map.get("mapNumber"),
              match.get("externalMatchId"), match_map.get("externalMapId"),
              {"player": rows}))
  else:
    for match_id, match in matches_by_id.items():
      teams_in_match = set(match_team_ids(match))
      linked_team_ids = teams_in_match.intersection(player_team_ids)

      if len(linked_team_ids) != 1:
        continue

      team_id = next(iter(linked_team_ids))
      opponent_id_for_point = _opponent_id(
          match, team_id) if team_id is not None else None

      if opponent_id is not None and opponent_id_for_point != opponent_id:
        continue

      rows = rows_by_match.get(match_id, [])

      if match_id in stats_available_match_ids and not rows:
        continue

      result = match_result_for_team(
          match, team_id,
          canonical_winner_only=True)[1] if team_id is not None else {
              "win": None,
              "ownScore": None,
              "opponentScore": None
          }
      stats = aggregate_player_stats(rows) if rows else None
      agent_values = {row.get("agent") for row in rows}
      agent_value = next(
          iter(agent_values)) if len(agent_values) == 1 else None
      records.append(
          _point_record(
              {
                  "timestamp": utc_datetime_string(match.get("scheduledAt")),
                  "matchId": public_identifier(match.get("externalMatchId")),
                  "matchMapId": None,
                  "opponent": _opponent_payload(opponent_id_for_point,
                                                teams_by_id),
                  "map": None,
                  "agent": public_identifier(agent_value),
                  "value":
                  stats.get(PLAYER_TREND_FIELDS[metric]) if stats else None,
                  "dataAvailability": "available" if stats else "pending",
                  "result": result,
                  "metadata": _player_metadata(rows) if rows else None
              }, match.get("scheduledAt"), None, match.get("externalMatchId"),
              None, {"player": rows}))

  records = _apply_range(records, range_value)
  source_rows = [
      row for record in records for row in record["sourceRows"]["player"]
  ]
  average = _player_trend_value(metric, source_rows) if source_rows else None

  return {
      "metric": metric,
      "interval": interval,
      "filters": filters,
      "average": average,
      "points": [record["point"] for record in records],
      "coverage": _coverage(records)
  }
