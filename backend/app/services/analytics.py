from datetime import datetime, timezone
from math import isfinite
from typing import Any, TypeGuard

PLAYER_COUNT_FIELDS = ("kills", "deaths", "assists", "firstKills",
                       "firstDeaths", "roundsPlayed")
PLAYER_WEIGHTED_FIELDS = {
    "rating": 2,
    "acs": 1,
    "kast": 1,
    "adr": 1,
    "headshotPercentage": 1
}


def _value_at(document: dict[str, Any], path: str) -> Any:
  value: Any = document

  for part in path.split("."):
    if not isinstance(value, dict):
      return None

    value = value.get(part)

  return value


def _valid_count(value: Any) -> TypeGuard[int]:
  return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _valid_number(value: Any) -> TypeGuard[int | float]:
  return isinstance(
      value, (int, float)) and not isinstance(value, bool) and isfinite(value)


def sum_count(rows: list[dict[str, Any]], path: str) -> int:
  return sum(value for row in rows
             if _valid_count(value := _value_at(row, path)))


def ratio(numerator: int | float,
          denominator: int | float,
          scale: int = 1) -> float | None:
  if not _valid_number(numerator) or not _valid_number(
      denominator) or denominator <= 0:
    return None

  value = numerator / denominator * scale

  return value if isfinite(value) else None


def ratio_for_rows(rows: list[dict[str, Any]],
                   numerator_path: str,
                   denominator_path: str,
                   scale: int = 1) -> float | None:
  if not rows:
    return None

  if any(not _valid_count(_value_at(row, path)) for row in rows
         for path in (numerator_path, denominator_path)):
    return None

  return ratio(sum_count(rows, numerator_path),
               sum_count(rows, denominator_path), scale)


def weighted_average(rows: list[dict[str, Any]], metric: str,
                     precision: int) -> float | None:
  numerator = 0.0
  denominator = 0

  for row in rows:
    value = row.get(metric)
    rounds = row.get("roundsPlayed")

    if value is None or not _valid_number(value) or not _valid_count(rounds):
      continue

    numerator += value * rounds
    denominator += rounds

  if denominator == 0 or not isfinite(numerator):
    return None

  return round(numerator / denominator, precision)


def select_player_stat(row: dict[str, Any],
                       side: str) -> dict[str, Any] | None:
  if side == "overall":
    metrics = row.get("overall")
    rounds_played = row.get("roundsPlayed")
  else:
    metrics = row.get(side)
    rounds_played = metrics.get("roundsPlayed") if isinstance(metrics,
                                                              dict) else None

  if not isinstance(metrics, dict):
    return None

  selected = {
      field: metrics.get(field)
      for field in (*PLAYER_COUNT_FIELDS, *PLAYER_WEIGHTED_FIELDS)
  }
  selected.update({
      "roundsPlayed": rounds_played,
      "playerId": row.get("playerId"),
      "teamId": row.get("teamId"),
      "matchId": row.get("matchId"),
      "matchMapId": row.get("matchMapId"),
      "mapName": row.get("mapName"),
      "agent": row.get("agent")
  })

  return selected


def aggregate_player_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
  counts = {field: sum_count(rows, field) for field in PLAYER_COUNT_FIELDS}
  map_ids = {
      row.get("matchMapId")
      for row in rows if row.get("matchMapId") is not None
  }
  stats: dict[str, Any] = {"mapsPlayed": len(map_ids), **counts}

  for metric, precision in PLAYER_WEIGHTED_FIELDS.items():
    stats[metric] = weighted_average(rows, metric, precision)

  stats["kd"] = _rounded_ratio_for_rows(rows, "kills", "deaths", 2)
  stats["kpr"] = _rounded_ratio_for_rows(rows, "kills", "roundsPlayed", 2)
  stats["apr"] = _rounded_ratio_for_rows(rows, "assists", "roundsPlayed", 2)
  stats["fkfd"] = _rounded_ratio_for_rows(rows, "firstKills", "firstDeaths", 2)
  stats["fkpr"] = _rounded_ratio_for_rows(rows, "firstKills", "roundsPlayed",
                                          2)
  stats["fdpr"] = _rounded_ratio_for_rows(rows, "firstDeaths", "roundsPlayed",
                                          2)

  return stats


def _rounded_ratio_for_rows(rows: list[dict[str, Any]],
                            numerator_path: str,
                            denominator_path: str,
                            precision: int,
                            scale: int = 1) -> float | None:
  value = ratio_for_rows(rows, numerator_path, denominator_path, scale)

  return round(value, precision) if value is not None else None


def _unique_map_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  unique_rows = {}

  for row in rows:
    map_id = row.get("matchMapId")

    if map_id is not None:
      unique_rows.setdefault(map_id, row)

  return list(unique_rows.values())


def _map_outcome(row: dict[str, Any]) -> str | None:
  rounds_won = row.get("roundsWon")
  rounds_lost = row.get("roundsLost")

  if not _valid_count(rounds_won) or not _valid_count(
      rounds_lost) or rounds_won == rounds_lost:
    return None

  return "won" if rounds_won > rounds_lost else "lost"


def _side_percentage(rows: list[dict[str, Any]], side: str) -> float | None:
  side_rows = [
      value for row in rows if isinstance(value := row.get(side), dict)
  ]

  if len(side_rows) != len(rows):
    return None

  value = ratio_for_rows(side_rows, "roundsWon", "roundsPlayed", 100)

  return round(value, 1) if value is not None else None


def aggregate_team_map_stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
  unique_rows = _unique_map_rows(rows)
  outcomes = [_map_outcome(row) for row in unique_rows]
  maps_won = outcomes.count("won")
  maps_lost = outcomes.count("lost")
  maps_played = len(unique_rows)
  map_percentage = None

  if maps_played and all(outcome is not None for outcome in outcomes):
    value = ratio(maps_won, maps_played, 100)
    map_percentage = round(value, 1) if value is not None else None

  round_differential = None

  if unique_rows and all(
      _valid_count(_value_at(row, field)) for row in unique_rows
      for field in ("roundsWon", "roundsLost")):
    round_differential = sum_count(unique_rows, "roundsWon") - sum_count(
        unique_rows, "roundsLost")

  return {
      "mapsPlayed":
      maps_played,
      "mapsWon":
      maps_won,
      "mapsLost":
      maps_lost,
      "mapWinPercentage":
      map_percentage,
      "roundsPlayed":
      sum_count(unique_rows, "roundsPlayed"),
      "roundsWon":
      sum_count(unique_rows, "roundsWon"),
      "roundsLost":
      sum_count(unique_rows, "roundsLost"),
      "roundWinPercentage":
      _rounded_ratio_for_rows(unique_rows, "roundsWon", "roundsPlayed", 1,
                              100),
      "attackRoundWinPercentage":
      _side_percentage(unique_rows, "attack"),
      "defenseRoundWinPercentage":
      _side_percentage(unique_rows, "defense"),
      "roundDifferential":
      round_differential,
      "kills":
      sum_count(unique_rows, "kills"),
      "deaths":
      sum_count(unique_rows, "deaths"),
      "assists":
      sum_count(unique_rows, "assists"),
      "firstKills":
      sum_count(unique_rows, "firstKills"),
      "firstDeaths":
      sum_count(unique_rows, "firstDeaths"),
      "kd":
      _rounded_ratio_for_rows(unique_rows, "kills", "deaths", 2),
      "firstKillPercentage":
      _first_kill_percentage(unique_rows)
  }


def _first_kill_percentage(rows: list[dict[str, Any]]) -> float | None:
  if (not rows or any(not _valid_count(row.get(field)) for row in rows
                      for field in ("firstKills", "firstDeaths"))):
    return None

  kills = sum_count(rows, "firstKills")
  deaths = sum_count(rows, "firstDeaths")
  value = ratio(kills, kills + deaths, 100)

  return round(value, 1) if value is not None else None


def team_summary(team_id: Any, matches: list[dict[str, Any]],
                 team_map_rows: list[dict[str, Any]]) -> dict[str, Any]:
  valid_matches = []
  matches_won = 0

  for match in matches:
    if match.get("status") != "completed":
      continue

    team_a_id, team_b_id = match_team_ids(match)
    winner_id = match_winner_id(match)

    if team_id not in (team_a_id, team_b_id) or winner_id is None:
      continue

    valid_matches.append(match)
    matches_won += winner_id == team_id

  maps = aggregate_team_map_stats(team_map_rows)
  matches_played = len(valid_matches)

  return {
      "matchesPlayed": matches_played,
      "matchesWon": matches_won,
      "matchesLost": matches_played - matches_won,
      "matchWinPercentage": _rounded_ratio(matches_won, matches_played, 1,
                                           100),
      **maps
  }


def _rounded_ratio(numerator: int,
                   denominator: int,
                   precision: int,
                   scale: int = 1) -> float | None:
  value = ratio(numerator, denominator, scale)

  return round(value, precision) if value is not None else None


def match_team_ids(match: dict[str, Any]) -> tuple[Any | None, Any | None]:
  teams = match.get("teams")

  if not isinstance(teams, dict):
    return None, None

  team_a_id = teams.get("teamAId")
  team_b_id = teams.get("teamBId")

  if team_a_id is None or team_b_id is None or team_a_id == team_b_id:
    return None, None

  return team_a_id, team_b_id


def valid_match_score(match: dict[str, Any]) -> tuple[int, int] | None:
  score = match.get("score")

  if not isinstance(score, dict):
    return None

  team_a = score.get("teamA")
  team_b = score.get("teamB")

  if not _valid_count(team_a) or not _valid_count(team_b):
    return None

  return team_a, team_b


def match_winner_id(match: dict[str, Any]) -> Any | None:
  team_a_id, team_b_id = match_team_ids(match)

  if team_a_id is None or team_b_id is None:
    return None

  winner_id = match.get("winnerTeamId")

  if winner_id is not None:
    return winner_id if winner_id in (team_a_id, team_b_id) else None

  score = valid_match_score(match)

  if score is None or score[0] == score[1]:
    return None

  return team_a_id if score[0] > score[1] else team_b_id


def match_result_for_team(
    match: dict[str, Any],
    team_id: Any,
    canonical_winner_only: bool = False) -> tuple[Any | None, dict[str, Any]]:
  team_a_id, team_b_id = match_team_ids(match)

  if team_id not in (team_a_id, team_b_id) or team_a_id is None:
    return None, {"win": None, "ownScore": None, "opponentScore": None}

  opponent_id = team_b_id if team_id == team_a_id else team_a_id
  result = _result_for_team_ids(team_a_id, team_b_id, team_id,
                                match.get("score"), match.get("winnerTeamId"))

  if canonical_winner_only:
    winner_id = match.get("winnerTeamId")
    scores = valid_match_score(match)

    if winner_id not in (team_a_id, team_b_id):
      result["win"] = None
    elif scores is not None:
      score_winner = team_a_id if scores[0] > scores[
          1] else team_b_id if scores[1] > scores[0] else None

      if score_winner is None or score_winner != winner_id:
        result["win"] = None

  return opponent_id, result


def _result_for_team_ids(team_a_id: Any, team_b_id: Any, team_id: Any,
                         score: Any, winner_id: Any) -> dict[str, Any]:
  if (team_a_id is None or team_b_id is None or team_a_id == team_b_id
      or team_id not in (team_a_id, team_b_id)):
    return {"win": None, "ownScore": None, "opponentScore": None}

  scores = valid_match_score({"score": score}) if isinstance(score,
                                                             dict) else None
  own_score = opponent_score = None

  if scores is not None:
    own_score, opponent_score = scores if team_id == team_a_id else (scores[1],
                                                                     scores[0])

  if winner_id is not None:
    winner_id = winner_id if winner_id in (team_a_id, team_b_id) else None
  elif scores is not None and scores[0] != scores[1]:
    winner_id = team_a_id if scores[0] > scores[1] else team_b_id

  return {
      "win": team_id == winner_id if winner_id is not None else None,
      "ownScore": own_score,
      "opponentScore": opponent_score
  }


def match_map_result_for_team(
    match: dict[str, Any],
    match_map: dict[str, Any],
    team_id: Any,
    canonical_winner_only: bool = False) -> tuple[Any | None, dict[str, Any]]:
  match_team_a, match_team_b = match_team_ids(match)
  map_team_a = match_map.get("teamAId")
  map_team_b = match_map.get("teamBId")

  if (match_team_a is None or match_team_b is None or map_team_a is None
      or map_team_b is None or map_team_a == map_team_b
      or {match_team_a, match_team_b} != {map_team_a, map_team_b}):
    return None, {"win": None, "ownScore": None, "opponentScore": None}

  opponent_id = match_team_b if team_id == match_team_a else match_team_a if team_id == match_team_b else None

  if opponent_id is None:
    return None, {"win": None, "ownScore": None, "opponentScore": None}

  result = _result_for_team_ids(map_team_a, map_team_b, team_id,
                                match_map.get("score"),
                                match_map.get("winnerTeamId"))

  if canonical_winner_only and match_map.get("winnerTeamId") not in (
      map_team_a, map_team_b):
    result["win"] = None

  map_score = valid_match_score({"score":
                                 match_map.get("score")}) if isinstance(
                                     match_map.get("score"), dict) else None

  if result["win"] is not None and map_score is not None:
    if map_score[0] == map_score[1]:
      result["win"] = None
    else:
      score_winner = map_team_a if map_score[0] > map_score[1] else map_team_b

      if (match_map.get("winnerTeamId") is not None
          and match_map.get("winnerTeamId") != score_winner):
        result["win"] = None

  return opponent_id, result


def utc_datetime(value: Any) -> datetime | None:
  if not isinstance(value, datetime):
    return None

  if value.tzinfo is None or value.utcoffset() is None:
    value = value.replace(tzinfo=timezone.utc)

  return value.astimezone(timezone.utc)
