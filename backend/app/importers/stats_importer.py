import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from app.config import Settings
from app.utils.files import sha256_file, write_json_atomic
from app.importers.import_status import parse_source_datetime, preview_import_status, record_import_status
from app.importers.models import ImportRunError
from app.importers.providers.vct_snapshot import inspect_snapshot_file, read_source_timestamp
from app.importers.stats_models import (MapStatsDTO, MatchMapOperationDTO,
                                        PlayerDTO, PlayerMapStatOperationDTO,
                                        PlayerMapStatsDTO, SourceMatchStatsDTO,
                                        TeamMapStatsDTO)
from app.importers.providers.vct_stats import VctStatsProvider
from app.importers.write_plan import _json_safe, _normalized_definition, calculate_plan_digest, verify_plan_integrity

STATS_PROVIDER = "vct-reference"
CANONICAL_MATCH_PROVIDER = "liquipedia"
PLACEHOLDER_MAP_NAMES = {
    "", "tbd", "tba", "unknown", "unpicked", "map x", "-", "?"
}
CLUTCH_FIELDS = ("clutch_1v1", "clutch_1v2", "clutch_1v3", "clutch_1v4",
                 "clutch_1v5")
OVERALL_REQUIRED_FIELDS = ("rating_all", "acs_all", "adr_all", "kast_all",
                           "kills_all", "deaths_all", "assists_all", "fk_all",
                           "fd_all")
SPLIT_REQUIRED_COUNT_FIELDS = ("kills_atk", "deaths_atk", "assists_atk",
                               "fk_atk", "fd_atk", "kills_def", "deaths_def",
                               "assists_def", "fk_def", "fd_def")
PRESERVE_NULL_PATHS = {
    "players": {"realName", "role", "imageUrl", "nationality"},
    "match_maps": {
        "startedAt", "completedAt", "pickedByTeamId", "startingSide.teamA",
        "startingSide.teamB"
    },
    "player_map_stats": {"overall.clutches", "attack", "defense"},
    "team_map_stats": {
        "attack.kills", "attack.deaths", "attack.firstKills",
        "attack.firstDeaths", "defense.kills", "defense.deaths",
        "defense.firstKills", "defense.firstDeaths"
    }
}
INDEX_OPERATIONS = [{
    "collection": "players",
    "keys": [("slug", 1)],
    "unique": True,
    "name": "players_slug_unique"
}, {
    "collection": "players",
    "keys": [("handle", 1)],
    "name": "players_handle"
}, {
    "collection": "players",
    "keys": [("currentTeamId", 1)],
    "name": "players_current_team"
}, {
    "collection": "players",
    "keys": [("role", 1)],
    "name": "players_role"
}, {
    "collection": "match_maps",
    "keys": [("matchId", 1), ("mapNumber", 1)],
    "unique": True,
    "name": "match_maps_match_number_unique"
}, {
    "collection": "match_maps",
    "keys": [("source.provider", 1), ("externalMapId", 1)],
    "unique": True,
    "name": "match_maps_source_external_unique",
    "partialFilterExpression": {
        "source.provider": {
            "$type": "string",
            "$gt": ""
        },
        "externalMapId": {
            "$type": "string",
            "$gt": ""
        }
    }
}, {
    "collection": "match_maps",
    "keys": [("tournamentId", 1)],
    "name": "match_maps_tournament"
}, {
    "collection": "match_maps",
    "keys": [("mapName", 1)],
    "name": "match_maps_map_name"
}, {
    "collection": "match_maps",
    "keys": [("teamAId", 1)],
    "name": "match_maps_team_a"
}, {
    "collection": "match_maps",
    "keys": [("teamBId", 1)],
    "name": "match_maps_team_b"
}, {
    "collection": "player_map_stats",
    "keys": [("matchMapId", 1), ("playerId", 1)],
    "unique": True,
    "name": "player_map_stats_map_player_unique"
}, {
    "collection": "player_map_stats",
    "keys": [("tournamentId", 1), ("playerId", 1)],
    "name": "player_map_stats_tournament_player"
}, {
    "collection": "player_map_stats",
    "keys": [("tournamentId", 1), ("teamId", 1)],
    "name": "player_map_stats_tournament_team"
}, {
    "collection": "player_map_stats",
    "keys": [("tournamentId", 1), ("mapName", 1)],
    "name": "player_map_stats_tournament_map"
}, {
    "collection": "player_map_stats",
    "keys": [("playerId", 1), ("mapName", 1)],
    "name": "player_map_stats_player_map"
}, {
    "collection": "player_map_stats",
    "keys": [("playerId", 1), ("agent", 1)],
    "name": "player_map_stats_player_agent"
}, {
    "collection": "player_map_stats",
    "keys": [("matchId", 1), ("playerId", 1)],
    "name": "player_map_stats_match_player"
}, {
    "collection": "team_map_stats",
    "keys": [("matchMapId", 1), ("teamId", 1)],
    "unique": True,
    "name": "team_map_stats_map_team_unique"
}, {
    "collection": "team_map_stats",
    "keys": [("tournamentId", 1), ("teamId", 1)],
    "name": "team_map_stats_tournament_team"
}, {
    "collection": "team_map_stats",
    "keys": [("teamId", 1), ("mapName", 1)],
    "name": "team_map_stats_team_map"
}, {
    "collection": "team_map_stats",
    "keys": [("matchId", 1), ("teamId", 1)],
    "name": "team_map_stats_match_team"
}, {
    "collection": "team_map_stats",
    "keys": [("tournamentId", 1), ("mapName", 1)],
    "name": "team_map_stats_tournament_map"
}]


def _iso_utc(value: datetime) -> str:
  if value.tzinfo is None or value.utcoffset() is None:
    value = value.replace(tzinfo=timezone.utc)

  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _team_slug_from_url(value: str | None) -> str | None:
  if not value:
    return None

  parsed = urlparse(value if "://" in value else
                    f"https://vct-reference.com/{value.lstrip('/')}")
  slug = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]).strip()

  return slug or None


def _team_index_to_canonical(team_index: int, reversed_order: bool) -> int:
  return 1 - team_index if reversed_order else team_index


def _normalize_side(value: str) -> str | None:
  normalized = value.strip().casefold()

  return {
      "attack": "attack",
      "atk": "attack",
      "offense": "attack",
      "defense": "defense",
      "def": "defense",
      "defence": "defense"
  }.get(normalized)


def _other_side(value: str) -> str:
  return "defense" if value == "attack" else "attack"


def _player_slug(player: PlayerDTO) -> str | None:
  return _team_slug_from_url(player.url)


def _finite_nonnegative(value, label: str, errors: list[str]) -> None:
  if value is None:
    errors.append(f"{label} is missing.")

    return

  if isinstance(value, bool) or not isinstance(value,
                                               (int, float)) or value < 0:
    errors.append(f"{label} must be a non-negative number.")
  elif isinstance(value, float) and (value != value or value
                                     in (float("inf"), float("-inf"))):
    errors.append(f"{label} must be finite.")


def _validate_map(map_stats: MapStatsDTO) -> tuple[list[str], dict]:
  errors = list(map_stats.source_errors)
  game_id = map_stats.game_id or "(missing game_id)"

  if not map_stats.game_id or not map_stats.game_id.isdigit():
    errors.append(f"Map {game_id} has no numeric stable game_id.")

  if not map_stats.map_name or map_stats.map_name.strip().casefold(
  ) in PLACEHOLDER_MAP_NAMES:
    errors.append(f"Map {game_id} has an empty or placeholder map_name.")

  if map_stats.score0 is None or map_stats.score1 is None:
    errors.append(f"Map {game_id} is missing a score.")
  elif map_stats.score0 == map_stats.score1:
    errors.append(f"Map {game_id} has a tied score.")

  if len(map_stats.player_rows) != 10:
    errors.append(
        f"Map {game_id} must have exactly 10 Player Map rows; found {len(map_stats.player_rows)}."
    )

  player_ids = [row.external_player_id for row in map_stats.player_rows]

  if len(player_ids) != len(set(player_ids)):
    errors.append(f"Map {game_id} contains duplicate player_id rows.")

  team_counts = {
      team_idx: sum(row.team_idx == team_idx for row in map_stats.player_rows)
      for team_idx in (0, 1)
  }

  if team_counts != {0: 5, 1: 5}:
    errors.append(f"Map {game_id} must have five players on each source team.")

  player_slugs: dict[str, PlayerMapStatsDTO] = {}

  for row in map_stats.player_rows:
    label = f"Player {row.external_player_id or '(missing player_id)'} on Map {game_id}"
    player = row.player

    if not row.external_player_id or row.external_player_id != player.external_id:
      errors.append(f"{label} has an invalid player identity.")

    if not player.name or not player.name.strip():
      errors.append(f"{label} has no player name.")

    slug = _player_slug(player)

    if not slug:
      errors.append(f"{label} has no stable player URL or slug.")
    elif slug in player_slugs and player_slugs[
        slug].external_player_id != row.external_player_id:
      errors.append(
          f"Player slug '{slug}' maps to multiple DuckDB player IDs.")
    else:
      player_slugs[slug] = row

    if not row.agents or len(row.agents) != 1 or not row.agents[0].strip():
      errors.append(f"{label} must have exactly one non-empty Agent.")

    for field in OVERALL_REQUIRED_FIELDS:
      _finite_nonnegative(getattr(row, field), f"{label} {field}", errors)

    for field in SPLIT_REQUIRED_COUNT_FIELDS:
      _finite_nonnegative(getattr(row, field), f"{label} {field}", errors)

    for field in ("rating_atk", "acs_atk", "adr_atk", "kast_atk", "hs_pct_atk",
                  "rating_def", "acs_def", "adr_def", "kast_def",
                  "hs_pct_def"):
      value = getattr(row, field)

      if value is not None:
        _finite_nonnegative(value, f"{label} {field}", errors)

    for field in CLUTCH_FIELDS:
      value = getattr(row, field)

      if value is not None:
        _finite_nonnegative(value, f"{label} {field}", errors)

  expected_rounds = (map_stats.score0 or 0) + (map_stats.score1 or 0)

  if len(map_stats.rounds) != expected_rounds:
    errors.append(
        f"Map {game_id} round row count {len(map_stats.rounds)} does not match its score total {expected_rounds}."
    )

  round_numbers = [round_row.round_num for round_row in map_stats.rounds]

  if len(round_numbers) != len(set(round_numbers)):
    errors.append(f"Map {game_id} contains duplicate round numbers.")

  normalized_rounds = []

  for round_row in map_stats.rounds:
    side = _normalize_side(round_row.side)

    if side is None:
      errors.append(
          f"Map {game_id} round {round_row.round_num} has an unrecognized side."
      )

    normalized_rounds.append((round_row, side))

  if map_stats.score0 is not None and map_stats.score1 is not None:
    round_wins = {
        team_idx: sum(row.winner_team == team_idx for row in map_stats.rounds)
        for team_idx in (0, 1)
    }

    if round_wins != {0: map_stats.score0, 1: map_stats.score1}:
      errors.append(
          f"Map {game_id} round winners do not match its source team scores.")

  side_rounds = {
      team_idx: {
          "attack": {
              "played": 0,
              "won": 0
          },
          "defense": {
              "played": 0,
              "won": 0
          }
      }
      for team_idx in (0, 1)
  }

  for round_row, side in normalized_rounds:
    if side is None:
      continue

    for team_idx in (0, 1):
      team_side = side if team_idx == round_row.winner_team else _other_side(
          side)
      side_rounds[team_idx][team_side]["played"] += 1

      if team_idx == round_row.winner_team:
        side_rounds[team_idx][team_side]["won"] += 1

  if map_stats.score0 is not None and map_stats.score1 is not None:
    for team_idx, score in ((0, map_stats.score0), (1, map_stats.score1)):
      side_played = sum(item["played"]
                        for item in side_rounds[team_idx].values())
      side_won = sum(item["won"] for item in side_rounds[team_idx].values())

      if side_played != expected_rounds:
        errors.append(
            f"Map {game_id} Team {team_idx} attack/defense rounds do not total the map round count."
        )

      if side_won != score:
        errors.append(
            f"Map {game_id} Team {team_idx} attack/defense round wins do not match its map score."
        )

  map_result = {
      "gameId": map_stats.game_id,
      "roundsPlayed": expected_rounds,
      "sideRounds": side_rounds,
      "startingSide": None,
      "winnerSourceTeam": None,
      "playerSlugs": {
          slug: row
          for slug, row in player_slugs.items()
      }
  }

  if normalized_rounds:
    first_round, first_side = min(normalized_rounds,
                                  key=lambda item: item[0].round_num)

    if first_side is not None:
      map_result["startingSide"] = {
          first_round.winner_team: first_side,
          1 - first_round.winner_team: _other_side(first_side)
      }

  if (map_stats.score0 is not None and map_stats.score1 is not None
      and map_stats.score0 != map_stats.score1):
    map_result[
        "winnerSourceTeam"] = 0 if map_stats.score0 > map_stats.score1 else 1

  return errors, map_result


def _nullable_sum(rows: list[PlayerMapStatsDTO], field: str) -> int | None:
  values = [getattr(row, field) for row in rows]

  if any(value is None for value in values):
    return None

  return sum(values)


def _player_split(row: PlayerMapStatsDTO, prefix: str,
                  rounds_played: int) -> dict:
  suffix = "atk" if prefix == "attack" else "def"

  return {
      "roundsPlayed": rounds_played,
      "rating": getattr(row, f"rating_{suffix}"),
      "acs": getattr(row, f"acs_{suffix}"),
      "kills": getattr(row, f"kills_{suffix}"),
      "deaths": getattr(row, f"deaths_{suffix}"),
      "assists": getattr(row, f"assists_{suffix}"),
      "kast": getattr(row, f"kast_{suffix}"),
      "adr": getattr(row, f"adr_{suffix}"),
      "headshotPercentage": getattr(row, f"hs_pct_{suffix}"),
      "firstKills": getattr(row, f"fk_{suffix}"),
      "firstDeaths": getattr(row, f"fd_{suffix}")
  }


def _player_stat_document(source_match: SourceMatchStatsDTO,
                          map_stats: MapStatsDTO, map_result: dict,
                          row: PlayerMapStatsDTO, reversed_order: bool,
                          canonical_team_slugs: list[str],
                          tournament_slug: str) -> dict:
  if not row.agents:
    raise ImportRunError("Player Map Stats has no verified agent.")

  canonical_team_idx = _team_index_to_canonical(row.team_idx, reversed_order)
  current_team_slug = canonical_team_slugs[canonical_team_idx]
  opponent_team_slug = canonical_team_slugs[1 - canonical_team_idx]
  side_rounds = map_result["sideRounds"][row.team_idx]
  clutch_values = [getattr(row, field) for field in CLUTCH_FIELDS]
  clutches = sum(clutch_values) if map_stats.performance_available and all(
      value is not None for value in clutch_values) else None

  return {
      "tournamentSlug": tournament_slug,
      "externalMatchId": None,
      "duckdbMatchId": source_match.match_id,
      "externalMapId": map_stats.game_id,
      "playerSlug": _player_slug(row.player),
      "teamSlug": current_team_slug,
      "opponentTeamSlug": opponent_team_slug,
      "mapName": map_stats.map_name,
      "agent": row.agents[0],
      "roundsPlayed": map_result["roundsPlayed"],
      "overall": {
          "rating": row.rating_all,
          "acs": row.acs_all,
          "kills": row.kills_all,
          "deaths": row.deaths_all,
          "assists": row.assists_all,
          "kast": row.kast_all,
          "adr": row.adr_all,
          "headshotPercentage": row.hs_pct_all,
          "firstKills": row.fk_all,
          "firstDeaths": row.fd_all,
          "clutches": clutches
      },
      "attack": _player_split(row, "attack", side_rounds["attack"]["played"]),
      "defense": _player_split(row, "defense",
                               side_rounds["defense"]["played"]),
      "source": {
          "provider": STATS_PROVIDER,
          "url": source_match.source_url
      }
  }


def _team_split(rows: list[PlayerMapStatsDTO], stat_prefix: str,
                round_totals: dict) -> dict:
  suffix = "atk" if stat_prefix == "attack" else "def"

  return {
      "roundsPlayed": round_totals["played"],
      "roundsWon": round_totals["won"],
      "kills": _nullable_sum(rows, f"kills_{suffix}"),
      "deaths": _nullable_sum(rows, f"deaths_{suffix}"),
      "firstKills": _nullable_sum(rows, f"fk_{suffix}"),
      "firstDeaths": _nullable_sum(rows, f"fd_{suffix}")
  }


def _team_stat_documents(source_match: SourceMatchStatsDTO,
                         map_stats: MapStatsDTO, map_result: dict,
                         canonical_team_slugs: list[str], reversed_order: bool,
                         tournament_slug: str,
                         external_match_id: str) -> list[TeamMapStatsDTO]:
  total_rounds = map_result["roundsPlayed"]
  raw_scores = {0: map_stats.score0 or 0, 1: map_stats.score1 or 0}
  team_documents = []

  for raw_team_idx in (0, 1):
    canonical_team_idx = _team_index_to_canonical(raw_team_idx, reversed_order)
    team_slug = canonical_team_slugs[canonical_team_idx]
    opponent_slug = canonical_team_slugs[1 - canonical_team_idx]
    players = [
        row for row in map_stats.player_rows if row.team_idx == raw_team_idx
    ]
    rounds_won = raw_scores[raw_team_idx]
    document = {
        "tournamentSlug":
        tournament_slug,
        "externalMatchId":
        None,
        "duckdbMatchId":
        source_match.match_id,
        "externalMapId":
        map_stats.game_id,
        "teamSlug":
        team_slug,
        "opponentTeamSlug":
        opponent_slug,
        "mapName":
        map_stats.map_name,
        "roundsPlayed":
        total_rounds,
        "roundsWon":
        rounds_won,
        "roundsLost":
        total_rounds - rounds_won,
        "kills":
        sum(row.kills_all or 0 for row in players),
        "deaths":
        sum(row.deaths_all or 0 for row in players),
        "assists":
        sum(row.assists_all or 0 for row in players),
        "firstKills":
        sum(row.fk_all or 0 for row in players),
        "firstDeaths":
        sum(row.fd_all or 0 for row in players),
        "attack":
        _team_split(players, "attack",
                    map_result["sideRounds"][raw_team_idx]["attack"]),
        "defense":
        _team_split(players, "defense",
                    map_result["sideRounds"][raw_team_idx]["defense"]),
        "source": {
            "provider": STATS_PROVIDER,
            "url": source_match.source_url
        }
    }

    team_documents.append(
        TeamMapStatsDTO(external_match_id=external_match_id,
                        external_map_id=map_stats.game_id or "",
                        team_slug=team_slug,
                        opponent_team_slug=opponent_slug,
                        document_template=document))

  return team_documents


def _build_indexes() -> list[dict]:
  return _index_operations_for_plan()


def _index_operations_for_plan() -> list[dict]:
  return [{
      **{
          key: value
          for key, value in operation.items() if key != "keys"
      }, "keys": [[field, direction] for field, direction in operation["keys"]]
  } for operation in INDEX_OPERATIONS]


def _read_json(path: Path, label: str) -> dict:
  try:
    payload = json.loads(
        path.expanduser().resolve().read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as error:
    raise ImportRunError(f"{label} is missing or invalid JSON.") from error

  if not isinstance(payload, dict):
    raise ImportRunError(f"{label} must contain a JSON object.")

  return payload


def _find_applied_schedule_bundle(
    settings: Settings) -> tuple[Path, Path, dict, dict, dict]:
  candidates = sorted(
      settings.report_directory.glob("import-preflight-*.json"),
      key=lambda path: path.name,
      reverse=True)

  for preflight_path in candidates:
    try:
      preflight = _read_json(preflight_path, "Schedule Preflight report")

      if (preflight.get("mongodbConnected") is not True
          or preflight.get("mongodbWrites") != 0
          or preflight.get("plannedCreate") != 0
          or preflight.get("plannedUpdate") != 0 or preflight.get("errors")
          or preflight.get("indexConflicts")
          or preflight.get("duplicateKeyRisks")
          or preflight.get("completedMatchDowngradeRisks")
          or preflight.get("canonicalReferenceRisks")):
        continue

      schedule_path = Path(preflight["writePlanPath"]).expanduser().resolve()
      schedule_plan = _read_json(schedule_path, "Applied Schedule Write Plan")
      expected_unchanged = (schedule_plan.get("plannedCounts")
                            or {}).get("upsert")

      if (not isinstance(expected_unchanged, int)
          or preflight.get("plannedUnchanged") != expected_unchanged):
        continue

      if schedule_plan.get("planDigest") != preflight.get("planDigest"):
        continue

      integrity_errors = verify_plan_integrity(schedule_plan, schedule_path)

      if integrity_errors or schedule_plan.get("blockingErrors"):
        continue

      reconciliation_path = Path(
          schedule_plan["reconciliationReportPath"]).expanduser().resolve()
      reconciliation = _read_json(reconciliation_path,
                                  "Schedule Reconciliation report")

      return schedule_path, preflight_path.resolve(
      ), schedule_plan, preflight, reconciliation
    except (ImportRunError, KeyError, TypeError, ValueError):
      continue

  raise ImportRunError(
      "No successful Schedule Apply confirmation and matching verified Write Plan were found."
  )


def _canonical_match_mappings(schedule_plan: dict, reconciliation: dict,
                              blocking_errors: list[str]) -> list[dict]:
  if schedule_plan.get("eventSlug") != (reconciliation.get("event")
                                        or {}).get("slug"):
    blocking_errors.append(
        "Schedule Write Plan and reconciliation report refer to different events."
    )

  teams_by_external_id = {}

  for operation in schedule_plan.get("teamOperations", []):
    source_id = operation.get("sourceExternalId")
    team_slug = (operation.get("document") or {}).get("slug")

    if not source_id or not team_slug or source_id in teams_by_external_id:
      blocking_errors.append(
          "Schedule Write Plan has a missing or duplicate DuckDB Team identity."
      )
      continue

    teams_by_external_id[source_id] = team_slug

  schedule_matches = {}

  for operation in schedule_plan.get("matchOperations", []):
    if operation.get("operation") != "upsert":
      continue

    external_id = operation.get("externalMatchId")

    if not external_id or external_id in schedule_matches:
      blocking_errors.append(
          "Schedule Write Plan has a missing or duplicate Liquipedia Match identity."
      )
      continue

    schedule_matches[external_id] = operation

  mappings = []
  seen_duckdb_ids = set()

  for item in reconciliation.get("matches", []):
    link_status = item.get("statsLinkStatus")

    if link_status is None:
      link_status = "available" if item.get(
          "status") == "duckdb_stats_available" else item.get("status")

    if link_status != "available":
      continue

    lp_identity = item.get("liquipediaIdentity") or {}
    duck_identity = item.get("duckdbIdentity") or {}
    external_match_id = lp_identity.get("externalId")
    duckdb_match_id = duck_identity.get("externalId")

    if not external_match_id or not duckdb_match_id:
      blocking_errors.append(
          "A stats-eligible reconciliation row is missing one of its exact Match identities."
      )
      continue

    if duckdb_match_id in seen_duckdb_ids:
      blocking_errors.append(
          f"DuckDB Match {duckdb_match_id} is paired more than once in reconciliation."
      )
      continue

    seen_duckdb_ids.add(duckdb_match_id)
    schedule_operation = schedule_matches.get(external_match_id)

    if not schedule_operation:
      blocking_errors.append(
          f"Verified Match {external_match_id} is missing from the applied Schedule Write Plan."
      )
      continue

    template = schedule_operation.get("documentTemplate") or {}
    duckdb_team_ids = (item.get("externalIds") or {}).get("duckdbTeams") or []

    if (len(duckdb_team_ids) != 2 or not all(
        str(team_id) in teams_by_external_id for team_id in duckdb_team_ids)):
      blocking_errors.append(
          f"Verified Match {external_match_id} is missing exact DuckDB Team identities."
      )
      continue

    duckdb_team_ids = [str(team_id) for team_id in duckdb_team_ids]
    duckdb_team_slugs = [
        teams_by_external_id[team_id] for team_id in duckdb_team_ids
    ]
    reversed_order = bool((item.get("teams") or {}).get("teamOrderReversed"))
    canonical_team_slugs = duckdb_team_slugs[::
                                             -1] if reversed_order else duckdb_team_slugs
    planned_teams = template.get("teams") or {}

    if canonical_team_slugs != [
        planned_teams.get("teamASlug"),
        planned_teams.get("teamBSlug")
    ]:
      blocking_errors.append(
          f"Reconciliation team order does not match the applied canonical Match {external_match_id}."
      )
      continue

    raw_team_ids = [
        str(team.get("externalId"))
        if team.get("externalId") is not None else None
        for team in (duck_identity.get("teams") or [])
    ]

    if raw_team_ids != duckdb_team_ids:
      blocking_errors.append(
          f"Reconciliation Match {external_match_id} has inconsistent DuckDB team identity ordering."
      )
      continue

    mappings.append({
        "externalMatchId":
        external_match_id,
        "duckdbMatchId":
        duckdb_match_id,
        "reconciliationStatus":
        "duckdb_stats_available",
        "teamOrderReversed":
        reversed_order,
        "duckdbTeamExternalIds":
        duckdb_team_ids,
        "duckdbTeamSlugs":
        duckdb_team_slugs,
        "teamASlug":
        planned_teams.get("teamASlug"),
        "teamBSlug":
        planned_teams.get("teamBSlug"),
        "tournamentSlug":
        template.get("tournamentSlug"),
        "duckdbSeriesScore":
        [duck_identity.get("scoreA"),
         duck_identity.get("scoreB")],
        "expectedMapCount": (item.get("statsAvailability")
                             or {}).get("completeMaps"),
        "expectedPlayerRows": (item.get("statsAvailability")
                               or {}).get("completePlayerRows")
    })

  expected_available = (reconciliation.get("summary")
                        or {}).get("statsAvailable")

  if not isinstance(expected_available,
                    int) or expected_available != len(mappings):
    blocking_errors.append(
        "DuckDB Stats availability does not match the exact verified Match mappings."
    )

  return mappings


def _map_document_template(mapping: dict, source_match: SourceMatchStatsDTO,
                           map_stats: MapStatsDTO, map_result: dict,
                           map_number: int) -> dict:
  reversed_order = mapping["teamOrderReversed"]
  raw_scores = {0: map_stats.score0, 1: map_stats.score1}
  score_a = raw_scores[1] if reversed_order else raw_scores[0]
  score_b = raw_scores[0] if reversed_order else raw_scores[1]

  if not isinstance(score_a, int) or not isinstance(
      score_b, int) or not map_stats.map_name:
    raise ImportRunError("Match Map has no verified score or map name.")

  winner_raw = map_result["winnerSourceTeam"]
  winner_canonical = _team_index_to_canonical(winner_raw, reversed_order)
  picked_raw = map_stats.picked_by_team
  picked_canonical = _team_index_to_canonical(
      picked_raw, reversed_order) if picked_raw is not None else None
  starting_side = {"teamA": None, "teamB": None}

  for raw_team_idx, side in (map_result.get("startingSide") or {}).items():
    canonical_idx = _team_index_to_canonical(raw_team_idx, reversed_order)
    starting_side["teamA" if canonical_idx == 0 else "teamB"] = side

  return {
      "tournamentSlug":
      mapping["tournamentSlug"],
      "externalMatchId":
      mapping["externalMatchId"],
      "duckdbMatchId":
      source_match.match_id,
      "externalMapId":
      map_stats.game_id,
      "mapNumber":
      map_number,
      "mapName":
      map_stats.map_name.strip(),
      "status":
      "completed",
      "teams": {
          "teamASlug": mapping["teamASlug"],
          "teamBSlug": mapping["teamBSlug"]
      },
      "score": {
          "teamA": score_a,
          "teamB": score_b
      },
      "winnerTeamSlug":
      mapping["teamASlug"] if winner_canonical == 0 else mapping["teamBSlug"],
      "pickedByTeamSlug":
      mapping["teamASlug"] if picked_canonical == 0 else
      mapping["teamBSlug"] if picked_canonical == 1 else None,
      "startingSide":
      starting_side,
      "overtime":
      score_a > 13 or score_b > 13,
      "startedAt":
      None,
      "completedAt":
      None,
      "source": {
          "provider": STATS_PROVIDER,
          "url": source_match.source_url
      }
  }


def _validate_match_maps(
    mapping: dict, source_match: SourceMatchStatsDTO,
    blocking_errors: list[str], profile_candidates: dict[str, list[dict]]
) -> tuple[list[dict], list[dict], list[dict], int]:
  match_errors = list(source_match.source_errors)

  if source_match.event_slug != "valorant-champions-2026":
    match_errors.append("DuckDB Match belongs to a different event.")

  if source_match.listing_status != "Completed" or source_match.status != "final":
    match_errors.append("DuckDB Match is not marked Completed + final.")

  if (source_match.team0_id != mapping["duckdbTeamExternalIds"][0]
      or source_match.team1_id != mapping["duckdbTeamExternalIds"][1]):
    match_errors.append(
        "DuckDB Match team IDs no longer match the reconciliation pair.")

  if source_match.utc_timestamp is None or source_match.utc_timestamp.tzinfo is None:
    match_errors.append(
        "DuckDB Match timestamp is missing or has no UTC timezone.")

  expected_maps = mapping.get("expectedMapCount")

  if (not isinstance(expected_maps, int) or expected_maps < 1
      or len(source_match.maps) != expected_maps):
    match_errors.append(
        f"DuckDB map count differs from the reconciliation report ({len(source_match.maps)} found, {expected_maps} expected)."
    )

  if not isinstance(expected_maps, int) or mapping.get(
      "expectedPlayerRows") != expected_maps * 10:
    match_errors.append(
        "Reconciliation coverage does not confirm 10 Player Map rows for each complete Map."
    )

  parsed_maps = []
  seen_map_ids = set()

  for map_stats in source_match.maps:
    errors, map_result = _validate_map(map_stats)

    if map_stats.game_id in seen_map_ids:
      errors.append(
          f"Duplicate game_id {map_stats.game_id} appears in one DuckDB Match."
      )

    seen_map_ids.add(map_stats.game_id)

    if map_result["roundsPlayed"] > 0 and not map_result["startingSide"]:
      errors.append(
          f"Map {map_stats.game_id} has no resolvable starting side.")

    if errors:
      match_errors.extend(
          f"Map {map_stats.game_id or '(missing game_id)'}: {error}"
          for error in errors)

    parsed_maps.append((map_stats, map_result))

  try:
    parsed_maps.sort(
        key=lambda entry: (int(entry[0].game_id), entry[0].game_id or ""))
  except (TypeError, ValueError):
    match_errors.append(
        "Map game_id values cannot be sorted as stable numeric identities.")

  source_series_score = mapping.get("duckdbSeriesScore") or []

  if (len(source_series_score) == 2
      and len(parsed_maps) == len(source_match.maps)
      and not any(_validate_map(map_stats)[0]
                  for map_stats, _ in parsed_maps)):
    map_wins = {0: 0, 1: 0}

    for map_stats, map_result in parsed_maps:
      map_wins[map_result["winnerSourceTeam"]] += 1

    if map_wins != {0: source_series_score[0], 1: source_series_score[1]}:
      match_errors.append(
          "DuckDB Map winners do not reconcile to the verified series score.")

  if match_errors:
    blocking_errors.append(
        f"Match {mapping['externalMatchId']} is blocked in full; none of its maps or stats were planned: "
        + " | ".join(dict.fromkeys(match_errors)))

    return [], [], [], 0

  map_operations = []
  player_stat_operations = []
  team_stat_operations = []
  missing_clutches = 0

  if source_match.utc_timestamp is None:
    blocking_errors.extend(match_errors)

    return [], [], [], 0

  for map_number, (map_stats, map_result) in enumerate(parsed_maps, start=1):
    template = _map_document_template(mapping, source_match, map_stats,
                                      map_result, map_number)
    map_operation = MatchMapOperationDTO(
        external_match_id=mapping["externalMatchId"],
        external_map_id=map_stats.game_id,
        map_number=map_number,
        document_template=template).model_dump(mode="json", by_alias=True)

    map_operations.append({"operation": "upsert", **map_operation})

    for row in map_stats.player_rows:
      document = _player_stat_document(
          source_match, map_stats, map_result, row,
          mapping["teamOrderReversed"],
          [mapping["teamASlug"], mapping["teamBSlug"]],
          mapping["tournamentSlug"])
      document["externalMatchId"] = mapping["externalMatchId"]
      operation = PlayerMapStatOperationDTO(
          external_match_id=mapping["externalMatchId"],
          external_map_id=map_stats.game_id,
          player_slug=document["playerSlug"],
          document_template=document).model_dump(mode="json", by_alias=True)

      player_stat_operations.append({"operation": "upsert", **operation})

      if document["overall"]["clutches"] is None:
        missing_clutches += 1

      player = row.player
      player_slug = _player_slug(player)
      profile_candidates.setdefault(row.external_player_id, []).append({
          "slug":
          player_slug,
          "handle":
          player.name.strip(),
          "nationality": (player.country.strip() or None)
          if player.country is not None else None,
          "url":
          player.url,
          "teamSlug":
          document["teamSlug"],
          "matchTimestamp":
          _iso_utc(source_match.utc_timestamp),
          "duckdbMatchId":
          source_match.match_id,
          "externalMatchId":
          mapping["externalMatchId"]
      })

    for team_operation in _team_stat_documents(
        source_match, map_stats, map_result,
        [mapping["teamASlug"], mapping["teamBSlug"]],
        mapping["teamOrderReversed"], mapping["tournamentSlug"],
        mapping["externalMatchId"]):
      document = team_operation.document_template
      document["externalMatchId"] = mapping["externalMatchId"]
      team_operation.document_template = document
      team_stat_operations.append({
          "operation":
          "upsert",
          **team_operation.model_dump(mode="json", by_alias=True)
      })

  return map_operations, player_stat_operations, team_stat_operations, missing_clutches


def _player_document_templates(profile_candidates: dict[str, list[dict]],
                               blocking_errors: list[str]) -> list[dict]:
  candidates_by_slug: dict[str, str] = {}
  operations = []

  for external_player_id, candidates in sorted(profile_candidates.items()):
    if not candidates:
      continue

    stable_identity = {(item["slug"], item["handle"], item["url"])
                       for item in candidates}

    if len(stable_identity) != 1:
      blocking_errors.append(
          f"DuckDB Player {external_player_id} has inconsistent name or URL identities across verified matches."
      )
      continue

    for candidate in candidates:
      slug = candidate["slug"]
      owner = candidates_by_slug.get(slug)

      if owner is not None and owner != external_player_id:
        blocking_errors.append(
            f"Player slug '{slug}' maps to multiple DuckDB player IDs.")

      candidates_by_slug[slug] = external_player_id

    latest_timestamp = max(item["matchTimestamp"] for item in candidates)
    latest = min(
        (item
         for item in candidates if item["matchTimestamp"] == latest_timestamp),
        key=lambda item: (item["duckdbMatchId"], item["externalMatchId"]))

    operations.append({
        "operation": "upsert",
        "key": {
            "slug": latest["slug"]
        },
        "sourceExternalId": external_player_id,
        "documentTemplate": {
            "slug": latest["slug"],
            "handle": latest["handle"],
            "realName": None,
            "nationality": latest["nationality"],
            "currentTeamSlug": latest["teamSlug"],
            "role": "unknown",
            "imageUrl": None,
            "source": {
                "provider": STATS_PROVIDER,
                "externalId": external_player_id,
                "url": latest["url"]
            }
        },
        "currentTeamEvidence": {
            "scheduledAt":
            latest["matchTimestamp"],
            "duckdbMatchId":
            latest["duckdbMatchId"],
            "externalMatchId":
            latest["externalMatchId"],
            "selectionRule":
            "latest UTC match; tie-break by ascending DuckDB match_id then Liquipedia externalMatchId"
        }
    })

  return operations


def _source_hash(path: Path, label: str, errors: list[str]) -> str | None:
  try:
    return sha256_file(path)
  except OSError:
    errors.append(
        f"{label} is missing or unreadable; no source digest could be recorded."
    )

    return None


def _canonical_schedule_mapping_digest(schedule_plan: dict) -> str:
  mappings = []

  for operation in schedule_plan.get("matchOperations", []):
    if operation.get("operation") != "upsert":
      continue

    document = operation.get("documentTemplate") or {}
    mappings.append({
        "tournamentSlug": document.get("tournamentSlug"),
        "externalMatchId": document.get("externalMatchId"),
        "stage": document.get("stage"),
        "group": document.get("group"),
        "round": document.get("round"),
        "bestOf": document.get("bestOf"),
        "scheduledAt": document.get("scheduledAt"),
        "status": document.get("status"),
        "teams": document.get("teams"),
        "score": document.get("score"),
        "winnerTeamSlug": document.get("winnerTeamSlug")
    })

  mappings.sort(key=lambda item: item.get("externalMatchId") or "")
  payload = json.dumps(mappings,
                       ensure_ascii=False,
                       sort_keys=True,
                       separators=(",", ":"),
                       allow_nan=False)

  return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_stats_plan(settings: Settings,
                     database_path: Path | None = None,
                     reconciliation_report_path: Path | None = None) -> dict:
  generated_at = datetime.now(timezone.utc)
  blocking_errors: list[str] = []
  warnings: list[str] = []
  database_path = settings.resolve_backend_path(
      database_path or settings.reference_database_path)
  schedule_path, schedule_preflight_path, schedule_plan, schedule_preflight, applied_reconciliation = _find_applied_schedule_bundle(
      settings)
  reconciliation_path = (
      settings.resolve_backend_path(reconciliation_report_path)
      if reconciliation_report_path is not None else Path(
          schedule_plan["reconciliationReportPath"]).expanduser().resolve())
  reconciliation = (_read_json(reconciliation_path,
                               "Current DuckDB Stats reconciliation report")
                    if reconciliation_report_path is not None else
                    applied_reconciliation)
  refreshed_reconciliation = reconciliation_report_path is not None and reconciliation_path != Path(
      schedule_plan["reconciliationReportPath"]).expanduser().resolve()

  if refreshed_reconciliation:
    reconciliation_sources = reconciliation.get("sources") or {}
    lp_source = reconciliation_sources.get("liquipedia") or {}
    duckdb_source = reconciliation_sources.get("vctReference") or {}
    current_duckdb_hash = _source_hash(database_path, "DuckDB",
                                       blocking_errors)

    try:
      report_database_path = Path(duckdb_source.get("databasePath")
                                  or "").expanduser().resolve()
    except (OSError, TypeError, ValueError):
      report_database_path = None

    if (reconciliation.get("event")
        or {}).get("slug") != settings.tournament_slug:
      blocking_errors.append(
          "Refreshed Stats reconciliation belongs to a different Tournament.")

    if lp_source.get(
        "status") == "failed" or not lp_source.get("contentSha256"):
      blocking_errors.append(
          "Refreshed Stats reconciliation does not contain a verified Liquipedia snapshot."
      )

    if (duckdb_source.get("provider") != "vct-reference"
        or duckdb_source.get("status") == "failed"
        or duckdb_source.get("readOnly") is not True
        or report_database_path != database_path.resolve()
        or duckdb_source.get("contentSha256") != current_duckdb_hash):
      blocking_errors.append(
          "Refreshed Stats reconciliation does not identify the current read-only DuckDB snapshot."
      )

    expected_lp_content = (schedule_plan.get("sourceDigests")
                           or {}).get("liquipediaContentSha256")

    if expected_lp_content and lp_source.get(
        "contentSha256") != expected_lp_content:
      blocking_errors.append(
          "Refreshed Stats reconciliation does not use the Liquipedia snapshot verified by the Schedule Apply."
      )

  mappings = _canonical_match_mappings(schedule_plan, reconciliation,
                                       blocking_errors)
  match_ids = [mapping["duckdbMatchId"] for mapping in mappings]

  try:
    source_matches = VctStatsProvider(database_path).fetch_matches(match_ids)
  except ImportRunError as error:
    source_matches = {}
    blocking_errors.append(str(error))

  match_mappings = []
  map_operations = []
  player_stat_operations = []
  team_stat_operations = []
  profile_candidates: dict[str, list[dict]] = {}
  missing_clutches = 0
  blocked_match_ids = set()

  for mapping in mappings:
    source_match = source_matches.get(mapping["duckdbMatchId"])

    if source_match is None:
      blocking_errors.append(
          f"DuckDB Match {mapping['duckdbMatchId']} could not be read for verified Liquipedia Match {mapping['externalMatchId']}."
      )
      blocked_match_ids.add(mapping["externalMatchId"])
      match_mappings.append({
          "externalMatchId":
          mapping["externalMatchId"],
          "duckdbMatchId":
          mapping["duckdbMatchId"],
          "reconciliationStatus":
          mapping["reconciliationStatus"],
          "teamOrderReversed":
          mapping["teamOrderReversed"],
          "canonicalTeamSlugs": [mapping["teamASlug"], mapping["teamBSlug"]],
          "tournamentSlug":
          mapping["tournamentSlug"],
          "sourceTeamExternalIds":
          mapping["duckdbTeamExternalIds"],
          "expectedMapCount":
          mapping.get("expectedMapCount"),
          "mapCount":
          0
      })
      continue

    current_profile_candidates = {
        key: list(value)
        for key, value in profile_candidates.items()
    }
    before_errors = len(blocking_errors)
    match_map_ops, match_player_stats, match_team_stats, missing_match_clutches = _validate_match_maps(
        mapping, source_match, blocking_errors, current_profile_candidates)

    if len(blocking_errors) > before_errors:
      blocked_match_ids.add(mapping["externalMatchId"])
      match_mappings.append({
          "externalMatchId":
          mapping["externalMatchId"],
          "duckdbMatchId":
          mapping["duckdbMatchId"],
          "reconciliationStatus":
          mapping["reconciliationStatus"],
          "teamOrderReversed":
          mapping["teamOrderReversed"],
          "canonicalTeamSlugs": [mapping["teamASlug"], mapping["teamBSlug"]],
          "tournamentSlug":
          mapping["tournamentSlug"],
          "sourceTeamExternalIds":
          mapping["duckdbTeamExternalIds"],
          "expectedMapCount":
          mapping.get("expectedMapCount"),
          "mapCount":
          0
      })
      continue

    profile_candidates = current_profile_candidates
    map_operations.extend(match_map_ops)
    player_stat_operations.extend(match_player_stats)
    team_stat_operations.extend(match_team_stats)
    missing_clutches += missing_match_clutches
    match_mappings.append({
        "externalMatchId":
        mapping["externalMatchId"],
        "duckdbMatchId":
        mapping["duckdbMatchId"],
        "reconciliationStatus":
        mapping["reconciliationStatus"],
        "teamOrderReversed":
        mapping["teamOrderReversed"],
        "canonicalTeamSlugs": [mapping["teamASlug"], mapping["teamBSlug"]],
        "tournamentSlug":
        mapping["tournamentSlug"],
        "sourceTeamExternalIds":
        mapping["duckdbTeamExternalIds"],
        "expectedMapCount":
        mapping.get("expectedMapCount"),
        "mapCount":
        len(match_map_ops)
    })

  player_operations = _player_document_templates(profile_candidates,
                                                 blocking_errors)
  reversed_matches = [
      item["externalMatchId"] for item in match_mappings
      if item["teamOrderReversed"]
  ]

  if missing_clutches:
    warnings.append(
        f"Clutch source values are incomplete for {missing_clutches} Player Map rows; overall.clutches remains null."
    )

  unpicked_maps = [
      operation["externalMapId"] for operation in map_operations
      if operation["documentTemplate"].get("pickedByTeamSlug") is None
  ]

  if unpicked_maps:
    warnings.append(
        f"Map pick is unavailable for {len(unpicked_maps)} Maps; pickedByTeamId remains null."
    )

  if reversed_matches:
    warnings.append(
        f"Canonical Team A/Team B order was reversed for {len(reversed_matches)} verified Match(es)."
    )

  source_digests = {
      "duckdbSha256":
      _source_hash(database_path, "DuckDB", blocking_errors),
      "reconciliationReportSha256":
      _source_hash(reconciliation_path, "Reconciliation report",
                   blocking_errors),
      "schedulePlanSha256":
      _source_hash(schedule_path, "Schedule Write Plan", blocking_errors),
      "schedulePreflightSha256":
      _source_hash(schedule_preflight_path, "Schedule Preflight report",
                   blocking_errors),
      "canonicalScheduleMappingDigest":
      _canonical_schedule_mapping_digest(schedule_plan)
  }

  try:
    snapshot_identity = inspect_snapshot_file(database_path)
    source_digests["duckdbSchemaFingerprint"] = snapshot_identity[
        "schemaFingerprint"]

    if snapshot_identity["sha256"] != source_digests["duckdbSha256"]:
      blocking_errors.append(
          "DuckDB changed while the Stats Plan was being generated.")
  except (ImportRunError, OSError):
    snapshot_identity = {"schemaFingerprint": None, "tableCounts": {}}
    blocking_errors.append(
        "DuckDB could not be validated read-only for its schema fingerprint.")

  source_timestamp = read_source_timestamp(database_path)
  schedule_path_digest_errors = verify_plan_integrity(schedule_plan,
                                                      schedule_path)

  if schedule_path_digest_errors:
    blocking_errors.extend(f"Schedule plan integrity: {error}"
                           for error in schedule_path_digest_errors)

  if schedule_preflight.get("planDigest") != schedule_plan.get("planDigest"):
    blocking_errors.append(
        "Schedule Preflight report does not refer to the selected Schedule Write Plan."
    )

  if not refreshed_reconciliation:
    if source_digests["reconciliationReportSha256"] != schedule_plan.get(
        "sourceDigests", {}).get("reconciliationReportSha256"):
      blocking_errors.append(
          "Reconciliation report no longer matches the applied Schedule Write Plan."
      )

    if source_digests["duckdbSha256"] != schedule_plan.get(
        "sourceDigests", {}).get("duckdbSha256"):
      blocking_errors.append(
          "DuckDB no longer matches the applied Schedule Write Plan.")

  plan = {
      "runId":
      str(uuid.uuid4()),
      "generatedAt":
      _iso_utc(generated_at),
      "eventSlug":
      schedule_plan.get("eventSlug"),
      "executionMode":
      "offline-dry-run",
      "reconciliationMode":
      "post-snapshot-stats-linking"
      if refreshed_reconciliation else "schedule-plan-source",
      "schedulePlanIdentity": {
          "path": str(schedule_path.resolve()),
          "sha256": source_digests["schedulePlanSha256"],
          "runId": schedule_plan.get("runId"),
          "planDigest": schedule_plan.get("planDigest"),
          "preflightReportPath": str(schedule_preflight_path.resolve()),
          "preflightReportSha256": source_digests["schedulePreflightSha256"],
          "preflightMongoWrites": schedule_preflight.get("mongodbWrites"),
          "preflightPlannedUnchanged":
          schedule_preflight.get("plannedUnchanged")
      },
      "reconciliationReportPath":
      str(reconciliation_path.resolve()),
      "duckdb": {
          "path": str(database_path.resolve()),
          "sha256": source_digests["duckdbSha256"],
          "schemaFingerprint": snapshot_identity.get("schemaFingerprint"),
          "tableCounts": snapshot_identity.get("tableCounts"),
          "sourceTimestamp":
          _iso_utc(source_timestamp) if source_timestamp else None,
          "readOnly": True
      },
      "sourceFiles": {
          "schedulePlanPath": str(schedule_path.resolve()),
          "schedulePreflightReportPath": str(schedule_preflight_path.resolve())
      },
      "sourceDigests":
      source_digests,
      "matchMappings":
      match_mappings,
      "teamOrderReversed": {
          "count":
          len(reversed_matches),
          "externalMatchIds":
          reversed_matches,
          "mappingRule":
          "DuckDB team_idx 0/1 is swapped to canonical Team B/A when teamOrderReversed is true."
      },
      "playerOperations":
      player_operations,
      "matchMapOperations":
      map_operations,
      "playerMapStatOperations":
      player_stat_operations,
      "teamMapStatOperations":
      team_stat_operations,
      "indexOperations":
      _build_indexes(),
      "plannedCounts": {
          "verifiedMatches": len(match_mappings),
          "players": len(player_operations),
          "matchMaps": len(map_operations),
          "playerMapStats": len(player_stat_operations),
          "teamMapStats": len(team_stat_operations),
          "skippedMatches": len(blocked_match_ids),
          "create": None,
          "update": None,
          "unchanged": None
      },
      "blockedMatchExternalIds":
      sorted(blocked_match_ids),
      "warnings":
      warnings,
      "blockingErrors":
      blocking_errors
  }

  return plan


def load_stats_plan(path: Path) -> dict:
  return _read_json(path, "Stats Write Plan")


def _verify_source_file(label: str, raw_path: str | None, expected: str | None,
                        errors: list[str]) -> None:
  if not raw_path or not expected:
    errors.append(
        f"{label} path or SHA-256 is missing; regenerate and review a Stats Write Plan."
    )

    return

  try:
    actual = sha256_file(Path(raw_path).expanduser().resolve())
  except OSError:
    errors.append(
        f"{label} is missing or unreadable; regenerate and review a Stats Write Plan."
    )

    return

  if actual.casefold() != expected.casefold():
    errors.append(
        f"{label} SHA-256 changed; regenerate and review a Stats Write Plan.")


def verify_stats_plan_integrity(plan: dict, plan_path: Path) -> list[str]:
  errors = []
  digest = plan.get("planDigest")

  if not isinstance(digest, str) or not digest:
    errors.append(
        "Stats Write Plan digest is missing; regenerate and review a Stats Write Plan."
    )
  elif calculate_plan_digest(plan) != digest:
    errors.append(
        "Stats Write Plan payload changed; regenerate and review a Stats Write Plan."
    )

  source_digests = plan.get("sourceDigests") or {}
  files = plan.get("sourceFiles") or {}
  _verify_source_file("DuckDB", (plan.get("duckdb") or {}).get("path"),
                      source_digests.get("duckdbSha256"), errors)
  _verify_source_file("Reconciliation report",
                      plan.get("reconciliationReportPath"),
                      source_digests.get("reconciliationReportSha256"), errors)
  _verify_source_file("Schedule Write Plan", files.get("schedulePlanPath"),
                      source_digests.get("schedulePlanSha256"), errors)
  _verify_source_file("Schedule Preflight report",
                      files.get("schedulePreflightReportPath"),
                      source_digests.get("schedulePreflightSha256"), errors)
  identity = plan.get("schedulePlanIdentity") or {}
  schedule_path = Path(files.get("schedulePlanPath") or "")
  preflight_path = Path(files.get("schedulePreflightReportPath") or "")

  if (identity.get("path") != files.get("schedulePlanPath")
      or identity.get("sha256") != source_digests.get("schedulePlanSha256")):
    errors.append(
        "Stats Write Plan Schedule Plan source identity fields disagree.")

  if (identity.get("preflightReportPath")
      != files.get("schedulePreflightReportPath")
      or identity.get("preflightReportSha256")
      != source_digests.get("schedulePreflightSha256")):
    errors.append(
        "Stats Write Plan Schedule Preflight source identity fields disagree.")

  if (plan.get("duckdb")
      or {}).get("sha256") != source_digests.get("duckdbSha256"):
    errors.append("Stats Write Plan DuckDB digest fields disagree.")

  if (plan.get("duckdb") or {}).get("schemaFingerprint") != source_digests.get(
      "duckdbSchemaFingerprint"):
    errors.append(
        "Stats Write Plan DuckDB schema fingerprint fields disagree.")

  schedule_plan = {}

  if schedule_path.is_file():
    try:
      schedule_plan = _read_json(schedule_path, "Schedule Write Plan")
      errors.extend(
          f"Schedule plan integrity: {error}"
          for error in verify_plan_integrity(schedule_plan, schedule_path))

      if schedule_plan.get("planDigest") != identity.get("planDigest"):
        errors.append("Stats Write Plan Schedule Plan identity has changed.")

      if schedule_plan.get("runId") != identity.get("runId"):
        errors.append(
            "Stats Write Plan Schedule Plan run identity has changed.")

      if schedule_plan.get("eventSlug") != plan.get("eventSlug"):
        errors.append(
            "Stats Write Plan event does not match its Schedule Write Plan.")

      if _canonical_schedule_mapping_digest(
          schedule_plan) != source_digests.get(
              "canonicalScheduleMappingDigest"):
        errors.append(
            "Stats Write Plan canonical Schedule mapping digest has changed.")

      reconciliation_mode = plan.get("reconciliationMode",
                                     "schedule-plan-source")
      stats_reconciliation_path = Path(plan.get("reconciliationReportPath",
                                                "")).resolve()
      schedule_reconciliation_path = Path(
          schedule_plan.get("reconciliationReportPath", "")).resolve()

      if reconciliation_mode == "schedule-plan-source":
        if stats_reconciliation_path != schedule_reconciliation_path:
          errors.append(
              "Stats Write Plan Reconciliation source does not match its Schedule Write Plan."
          )

        if source_digests.get("duckdbSha256") != (
            schedule_plan.get("sourceDigests") or {}).get("duckdbSha256"):
          errors.append(
              "Stats Write Plan DuckDB source does not match its Schedule Write Plan."
          )
      elif reconciliation_mode == "post-snapshot-stats-linking":
        try:
          stats_reconciliation = _read_json(
              stats_reconciliation_path, "Current Stats reconciliation report")
          stats_sources = stats_reconciliation.get("sources") or {}
          lp_source = stats_sources.get("liquipedia") or {}
          duckdb_source = stats_sources.get("vctReference") or {}
          expected_lp_content = (schedule_plan.get("sourceDigests")
                                 or {}).get("liquipediaContentSha256")
          report_database_path = Path(duckdb_source.get("databasePath")
                                      or "").resolve()

          if (stats_reconciliation.get("event")
              or {}).get("slug") != plan.get("eventSlug"):
            errors.append(
                "Current Stats reconciliation belongs to a different Tournament."
            )

          if (duckdb_source.get("provider") != "vct-reference"
              or duckdb_source.get("status") == "failed"
              or duckdb_source.get("readOnly") is not True
              or report_database_path != Path(
                  (plan.get("duckdb") or {}).get("path") or "").resolve()
              or duckdb_source.get("contentSha256")
              != source_digests.get("duckdbSha256")):
            errors.append(
                "Current Stats reconciliation does not match the verified DuckDB source."
            )

          if (lp_source.get("status") == "failed"
              or not lp_source.get("contentSha256")
              or (expected_lp_content
                  and lp_source.get("contentSha256") != expected_lp_content)):
            errors.append(
                "Current Stats reconciliation does not match the Liquipedia snapshot verified by Schedule."
            )
        except (ImportRunError, OSError, TypeError, ValueError):
          errors.append(
              "Current Stats reconciliation source is missing or invalid.")
      else:
        errors.append(
            "Stats Write Plan uses an unsupported reconciliation mode.")
    except ImportRunError as error:
      errors.append(str(error))

  try:
    current_snapshot = inspect_snapshot_file(
        Path((plan.get("duckdb") or {}).get("path") or ""))

    if current_snapshot.get("schemaFingerprint") != source_digests.get(
        "duckdbSchemaFingerprint"):
      errors.append(
          "DuckDB schema fingerprint changed; regenerate and review a Stats Write Plan."
      )
  except (ImportRunError, OSError, TypeError, ValueError):
    errors.append(
        "DuckDB failed read-only schema validation; regenerate and review a Stats Write Plan."
    )

  if preflight_path.is_file():
    try:
      preflight = _read_json(preflight_path, "Schedule Preflight report")
      schedule_counts = (schedule_plan.get("plannedCounts")
                         or {}) if schedule_path.is_file() else {}
      expected_unchanged = schedule_counts.get("upsert")

      if preflight.get("mongodbConnected") is not True or preflight.get(
          "mongodbWrites") != 0:
        errors.append(
            "Schedule Preflight report does not confirm a zero-write MongoDB check."
        )

      if preflight.get("planDigest") != identity.get("planDigest"):
        errors.append(
            "Schedule Preflight report refers to a different Schedule Write Plan."
        )

      if Path(preflight.get("writePlanPath",
                            "")).resolve() != schedule_path.resolve():
        errors.append(
            "Schedule Preflight report refers to a different Schedule Write Plan file."
        )

      if (preflight.get("plannedCreate") != 0
          or preflight.get("plannedUpdate") != 0
          or not isinstance(expected_unchanged, int)
          or preflight.get("plannedUnchanged") != expected_unchanged
          or preflight.get("errors") or preflight.get("indexConflicts")
          or preflight.get("duplicateKeyRisks")
          or preflight.get("completedMatchDowngradeRisks")
          or preflight.get("canonicalReferenceRisks")):
        errors.append(
            "Schedule Preflight does not confirm every managed Schedule document is unchanged."
        )
    except ImportRunError as error:
      errors.append(str(error))

  if not plan_path.is_file():
    errors.append("Specified Stats Write Plan file is missing or unreadable.")

  return list(dict.fromkeys(errors))


def _validate_stats_plan(plan: dict) -> list[str]:
  errors = list(plan.get("blockingErrors", []))

  if plan.get("indexOperations") != _index_operations_for_plan():
    errors.append(
        "Stats plan has missing or unsupported MongoDB index definitions.")

  mappings = plan.get("matchMappings") or []
  planned_verified_matches = (plan.get("plannedCounts")
                              or {}).get("verifiedMatches")

  if (not isinstance(planned_verified_matches, int)
      or planned_verified_matches < 0
      or planned_verified_matches != len(mappings)):
    errors.append(
        "Stats plan verified Match count does not match its exact mappings.")

  mapping_by_external_id = {}

  for mapping in mappings:
    external_match_id = mapping.get("externalMatchId")

    if mapping.get("reconciliationStatus") != "duckdb_stats_available":
      errors.append(
          f"Stats plan contains an ineligible reconciliation status on Match {external_match_id}."
      )

    if (not external_match_id
        or mapping.get("teamOrderReversed") not in {True, False}
        or not mapping.get("duckdbMatchId")
        or not mapping.get("tournamentSlug")
        or len(mapping.get("canonicalTeamSlugs") or []) != 2):
      errors.append(
          "Stats plan contains a Match mapping without a stable identity or Team order."
      )

    if external_match_id in mapping_by_external_id:
      errors.append(
          f"Stats plan repeats Liquipedia Match identity {external_match_id}.")

    mapping_by_external_id[external_match_id] = mapping

  players_by_slug = {}

  for operation in plan.get("playerOperations", []):
    document = operation.get("documentTemplate") or {}
    slug = document.get("slug")

    if (operation.get("operation") != "upsert" or
        not all(document.get(field) for field in ("slug", "handle", "role"))):
      errors.append(
          f"Stats Player operation {slug or '(unknown)'} is incomplete.")

    if slug in players_by_slug:
      errors.append(f"Stats plan repeats Player slug {slug}.")

    players_by_slug[slug] = operation

  map_keys = set()
  map_numbers_by_match = {}
  map_operations_by_key = {}

  for operation in plan.get("matchMapOperations", []):
    document = operation.get("documentTemplate") or {}
    external_match_id = operation.get("externalMatchId")
    external_map_id = operation.get("externalMapId")
    key = (external_match_id, external_map_id)

    if operation.get("operation") != "upsert" or key in map_keys:
      errors.append(
          f"Stats Map operation {external_match_id}/{external_map_id} is invalid or duplicated."
      )

    map_keys.add(key)
    map_operations_by_key[key] = operation

    if external_match_id not in mapping_by_external_id or not external_map_id:
      errors.append(
          f"Stats Map operation {external_match_id}/{external_map_id} has no verified Match mapping."
      )

    if (document.get("status") != "completed"
        or document.get("externalMatchId") != external_match_id
        or document.get("externalMapId") != external_map_id):
      errors.append(
          f"Stats Map operation {external_match_id}/{external_map_id} is not a completed mapped Map."
      )

    mapping = mapping_by_external_id.get(external_match_id) or {}

    if document.get("teams") != {
        "teamASlug": mapping.get("canonicalTeamSlugs", [None, None])[0],
        "teamBSlug": mapping.get("canonicalTeamSlugs", [None, None])[1]
    }:
      errors.append(
          f"Stats Map operation {external_match_id}/{external_map_id} has unverified canonical Team references."
      )

    map_numbers_by_match.setdefault(external_match_id, set())
    map_number = operation.get("mapNumber")

    if map_number in map_numbers_by_match[external_match_id]:
      errors.append(
          f"Stats plan repeats Map number {map_number} in Match {external_match_id}."
      )

    map_numbers_by_match[external_match_id].add(map_number)

  player_stat_keys = set()
  player_stat_counts = {}

  for operation in plan.get("playerMapStatOperations", []):
    external_match_id = operation.get("externalMatchId")
    external_map_id = operation.get("externalMapId")
    document = operation.get("documentTemplate") or {}

    if (operation.get("operation") != "upsert"
        or (external_match_id, external_map_id) not in map_keys):
      errors.append(
          f"Player Map Stats operation {external_match_id}/{external_map_id} has no planned Map."
      )

    if (operation.get("playerSlug") not in players_by_slug
        or document.get("playerSlug") != operation.get("playerSlug")):
      errors.append(
          f"Player Map Stats operation {external_match_id}/{external_map_id} references an unplanned Player."
      )

    player_stat_key = (external_match_id, external_map_id,
                       operation.get("playerSlug"))

    if player_stat_key in player_stat_keys:
      errors.append(
          f"Stats plan repeats Player Map identity {player_stat_key}.")

    player_stat_keys.add(player_stat_key)
    player_stat_counts[(external_match_id,
                        external_map_id)] = player_stat_counts.get(
                            (external_match_id, external_map_id), 0) + 1

    if (document.get("externalMatchId") != external_match_id
        or document.get("externalMapId") != external_map_id):
      errors.append(
          f"Player Map Stats operation {external_match_id}/{external_map_id} has inconsistent identity fields."
      )

    mapping = mapping_by_external_id.get(external_match_id) or {}

    if (document.get("teamSlug") not in mapping.get("canonicalTeamSlugs", [])
        or document.get("opponentTeamSlug") not in mapping.get(
            "canonicalTeamSlugs", [])):
      errors.append(
          f"Player Map Stats operation {external_match_id}/{external_map_id} has an unverified Team reference."
      )

    if document.get("tournamentSlug") != mapping.get("tournamentSlug"):
      errors.append(
          f"Player Map Stats operation {external_match_id}/{external_map_id} has an unverified Tournament reference."
      )

  team_stat_keys = set()
  team_stat_counts = {}

  for operation in plan.get("teamMapStatOperations", []):
    external_match_id = operation.get("externalMatchId")
    external_map_id = operation.get("externalMapId")
    document = operation.get("documentTemplate") or {}
    key = (external_match_id, external_map_id, operation.get("teamSlug"))

    if (operation.get("operation") != "upsert"
        or (external_match_id, external_map_id) not in map_keys):
      errors.append(
          f"Team Map Stats operation {external_match_id}/{external_map_id} has no planned Map."
      )

    if key in team_stat_keys:
      errors.append(f"Stats plan repeats Team Map identity {key}.")

    team_stat_keys.add(key)
    team_stat_counts[(external_match_id,
                      external_map_id)] = team_stat_counts.get(
                          (external_match_id, external_map_id), 0) + 1

    if (document.get("externalMatchId") != external_match_id
        or document.get("externalMapId") != external_map_id):
      errors.append(
          f"Team Map Stats operation {external_match_id}/{external_map_id} has inconsistent identity fields."
      )

    mapping = mapping_by_external_id.get(external_match_id) or {}

    if (operation.get("teamSlug") not in mapping.get("canonicalTeamSlugs", [])
        or operation.get("opponentTeamSlug") not in mapping.get(
            "canonicalTeamSlugs", [])):
      errors.append(
          f"Team Map Stats operation {external_match_id}/{external_map_id} has an unverified Team reference."
      )

    if (document.get("teamSlug") != operation.get("teamSlug") or
        document.get("opponentTeamSlug") != operation.get("opponentTeamSlug")
        or document.get("tournamentSlug") != mapping.get("tournamentSlug")):
      errors.append(
          f"Team Map Stats operation {external_match_id}/{external_map_id} has unverified Team or Tournament references."
      )

    attack = document.get("attack") or {}
    defense = document.get("defense") or {}

    if (document.get("roundsWon", -1) + document.get("roundsLost", -1)
        != document.get("roundsPlayed", -2)
        or attack.get("roundsPlayed", -1) + defense.get("roundsPlayed", -1)
        != document.get("roundsPlayed", -2)
        or attack.get("roundsWon", -1) + defense.get("roundsWon", -1)
        != document.get("roundsWon", -2)):
      errors.append(
          f"Team Map Stats operation {external_match_id}/{external_map_id} has inconsistent round totals."
      )

  match_map_counts = plan.get("plannedCounts") or {}

  for key, operation in map_operations_by_key.items():
    if player_stat_counts.get(key) != 10:
      errors.append(
          f"Map {key[0]}/{key[1]} does not have exactly 10 Player Map Stats operations."
      )

    if team_stat_counts.get(key) != 2:
      errors.append(
          f"Map {key[0]}/{key[1]} does not have exactly two Team Map Stats operations."
      )

  for mapping in mappings:
    planned_map_count = len(
        map_numbers_by_match.get(mapping["externalMatchId"], set()))

    if (planned_map_count != mapping.get("mapCount")
        or planned_map_count != mapping.get("expectedMapCount")):
      errors.append(
          f"Verified Match {mapping['externalMatchId']} does not have its complete planned Map set."
      )

    match_maps = [
        operation for operation in plan.get("matchMapOperations", [])
        if operation.get("externalMatchId") == mapping["externalMatchId"]
    ]

    try:
      ordered_maps = sorted(
          match_maps,
          key=lambda operation:
          (int(operation["externalMapId"]), operation["externalMapId"]))

      if [operation.get("mapNumber") for operation in ordered_maps
          ] != list(range(1,
                          len(ordered_maps) + 1)):
        errors.append(
            f"Verified Match {mapping['externalMatchId']} mapNumber order is not numeric game_id order."
        )
    except (TypeError, ValueError, KeyError):
      errors.append(
          f"Verified Match {mapping['externalMatchId']} has a non-numeric Map identity."
      )

  if match_map_counts.get("matchMaps") != len(map_keys):
    errors.append(
        "Stats Write Plan Map operation count does not match its summary.")

  if match_map_counts.get("playerMapStats") != len(
      plan.get("playerMapStatOperations", [])):
    errors.append(
        "Stats Write Plan Player Map Stats count does not match its summary.")

  if match_map_counts.get("teamMapStats") != len(
      plan.get("teamMapStatOperations", [])):
    errors.append(
        "Stats Write Plan Team Map Stats count does not match its summary.")

  return list(dict.fromkeys(errors))


def _has_value(value) -> bool:
  return value is not None and not (isinstance(value, str) and value == "")


def _preserved_paths(collection_name: str,
                     current: dict | None,
                     planned: dict,
                     prefix: str = "") -> list[str]:
  existing = current or {}
  preserved = []

  for key, value in planned.items():
    path = f"{prefix}.{key}" if prefix else key
    old_value = existing.get(key)

    if isinstance(value, dict):
      preserved.extend(
          _preserved_paths(collection_name,
                           old_value if isinstance(old_value, dict) else {},
                           value, path))
      continue

    if value is None and _has_value(old_value):
      preserved.append(path)
    elif value == "" and isinstance(old_value, str) and old_value:
      preserved.append(path)
    elif (collection_name == "players" and path == "role"
          and value == "unknown" and old_value not in {None, "", "unknown"}):
      preserved.append(path)

  return preserved


def _merge_value(collection_name: str, path: str, current, planned):
  if isinstance(planned, dict):
    current_object = current if isinstance(current, dict) else {}
    merged = dict(current_object)

    for key, value in planned.items():
      child_path = f"{path}.{key}" if path else key
      merged[key] = _merge_value(collection_name, child_path,
                                 current_object.get(key), value)

    return merged

  preserve_paths = PRESERVE_NULL_PATHS[collection_name]

  if planned is None and (path in preserve_paths or _has_value(current)):
    return current

  if planned == "" and isinstance(current, str) and current:
    return current

  if (collection_name == "players" and path == "role" and planned == "unknown"
      and current not in {None, "", "unknown"}):
    return current

  return planned


def _managed_value_equal(current, merged, planned) -> bool:
  if isinstance(planned, dict):
    current_object = current if isinstance(current, dict) else {}
    merged_object = merged if isinstance(merged, dict) else {}

    return all(
        _managed_value_equal(current_object.get(key), merged_object.get(key),
                             value) for key, value in planned.items())

  return current == merged


def _merge_document(collection_name: str, current: dict | None,
                    planned: dict) -> tuple[dict, list[str]]:
  existing = current or {}
  merged = {}
  changed_fields = []

  for key, value in planned.items():
    merged_value = _merge_value(collection_name, key, existing.get(key), value)
    merged[key] = merged_value

    if not _managed_value_equal(existing.get(key), merged_value, value):
      changed_fields.append(key)

  return merged, changed_fields


def _operation_outcome(collection_name: str, current: dict | None,
                       planned: dict) -> str:
  if current is None:
    return "create"

  _, changed_fields = _merge_document(collection_name, current, planned)

  return "update" if changed_fields else "unchanged"


def _upsert(collection, collection_name: str, filter_query: dict,
            document: dict, now: datetime) -> tuple[str, object]:
  current = collection.find_one(filter_query)

  if current is None:
    result = collection.update_one(
        filter_query,
        {"$setOnInsert": {
            **document, "createdAt": now,
            "updatedAt": now
        }},
        upsert=True)

    if result.upserted_id is not None:
      return "created", result.upserted_id

    current = collection.find_one(filter_query)

    if current is None:
      raise RuntimeError(
          "Stats upsert did not create or find its target document.")

  merged, changed_fields = _merge_document(collection_name, current, document)

  if not changed_fields:
    return "unchanged", current["_id"]

  collection.update_one(filter_query, {
      "$set": {
          **{
              field: merged[field]
              for field in changed_fields
          }, "updatedAt": now
      }
  },
                        upsert=False)

  return "updated", current["_id"]


def _index_matches_plan(existing: dict, planned: dict) -> bool:
  expected_keys = [tuple(key) for key in planned["keys"]]

  if list(existing.get("key", {}).items()) != expected_keys:
    return False

  options = ("unique", "sparse", "expireAfterSeconds",
             "partialFilterExpression", "collation", "wildcardProjection",
             "hidden", "weights", "default_language", "language_override")

  return all(
      _normalized_definition(existing.get(name)) == _normalized_definition(
          planned.get(name)) for name in options)


def _index_conflicts(database, collections: set[str],
                     index_operations: list[dict]) -> list[dict]:
  conflicts = []

  for planned in index_operations:
    collection_name = planned["collection"]

    if collection_name not in collections:
      continue

    for existing in database[collection_name].list_indexes():
      if existing.get("name") == planned["name"] and not _index_matches_plan(
          existing, planned):
        conflicts.append({
            "collection": collection_name,
            "indexName": planned["name"],
            "planned": _json_safe(planned),
            "existing": _json_safe(existing)
        })

  return conflicts


def _planned_index_creates(database, collections: set[str],
                           index_operations: list[dict]) -> int:
  creates = 0
  index_cache = {}

  for planned in index_operations:
    collection_name = planned["collection"]

    if collection_name not in index_cache:
      index_cache[collection_name] = (list(
          database[collection_name].list_indexes()) if collection_name
                                      in collections else [])

    if not any(
        item.get("name") == planned.get("name")
        and _index_matches_plan(item, planned)
        for item in index_cache[collection_name]):
      creates += 1

  return creates


def _duplicate_key_risks(database, collections: set[str]) -> list[dict]:
  checks = [("players", "slug", [{
      "$group": {
          "_id": "$slug",
          "count": {
              "$sum": 1
          }
      }
  }, {
      "$match": {
          "count": {
              "$gt": 1
          }
      }
  }]),
            ("match_maps", "source.provider + externalMapId", [{
                "$match": {
                    "source.provider": {
                        "$type": "string",
                        "$gt": ""
                    },
                    "externalMapId": {
                        "$type": "string",
                        "$gt": ""
                    }
                }
            }, {
                "$group": {
                    "_id": {
                        "provider": "$source.provider",
                        "externalMapId": "$externalMapId"
                    },
                    "count": {
                        "$sum": 1
                    }
                }
            }, {
                "$match": {
                    "count": {
                        "$gt": 1
                    }
                }
            }]),
            ("match_maps", "matchId + mapNumber", [{
                "$group": {
                    "_id": {
                        "matchId": "$matchId",
                        "mapNumber": "$mapNumber"
                    },
                    "count": {
                        "$sum": 1
                    }
                }
            }, {
                "$match": {
                    "count": {
                        "$gt": 1
                    }
                }
            }]),
            ("player_map_stats", "matchMapId + playerId", [{
                "$group": {
                    "_id": {
                        "matchMapId": "$matchMapId",
                        "playerId": "$playerId"
                    },
                    "count": {
                        "$sum": 1
                    }
                }
            }, {
                "$match": {
                    "count": {
                        "$gt": 1
                    }
                }
            }]),
            ("team_map_stats", "matchMapId + teamId", [{
                "$group": {
                    "_id": {
                        "matchMapId": "$matchMapId",
                        "teamId": "$teamId"
                    },
                    "count": {
                        "$sum": 1
                    }
                }
            }, {
                "$match": {
                    "count": {
                        "$gt": 1
                    }
                }
            }])]
  risks = []

  for collection_name, key_name, pipeline in checks:
    if collection_name not in collections:
      continue

    duplicates = list(database[collection_name].aggregate(
        [*pipeline, {
            "$limit": 20
        }]))

    if duplicates:
      risks.append({
          "collection": collection_name,
          "key": key_name,
          "samples": [_json_safe(row) for row in duplicates]
      })

  return risks


def _map_identity_conflicts(
    database, plan: dict,
    matches_by_external_id: dict) -> tuple[list[dict], dict, dict]:
  conflicts = []
  maps_by_external_id = {}
  maps_by_internal_id = {}

  for operation in plan.get("matchMapOperations", []):
    template = operation["documentTemplate"]
    match = matches_by_external_id.get(operation["externalMatchId"])

    if not match:
      continue

    expected_internal = database["match_maps"].find_one({
        "matchId":
        match["_id"],
        "mapNumber":
        operation["mapNumber"]
    })
    expected_external = database["match_maps"].find_one({
        "source.provider":
        STATS_PROVIDER,
        "externalMapId":
        operation["externalMapId"]
    })

    if expected_internal and expected_external is None:
      conflicts.append({
          "externalMapId":
          operation["externalMapId"],
          "reason":
          "internal Map identity exists without the planned provider-scoped external identity"
      })

    if (expected_internal and expected_external
        and expected_internal["_id"] != expected_external["_id"]):
      conflicts.append({
          "externalMapId":
          operation["externalMapId"],
          "reason":
          "internal and external Map identities resolve to different documents"
      })

    existing = expected_external or expected_internal

    if (existing and (existing.get("matchId") != match["_id"]
                      or existing.get("mapNumber") != operation["mapNumber"])):
      conflicts.append({
          "externalMapId": operation["externalMapId"],
          "reason":
          "existing Map is bound to a different canonical Match or mapNumber",
          "existingMapId": str(existing.get("_id")),
          "expectedExternalMatchId": operation["externalMatchId"]
      })

    if expected_external:
      maps_by_external_id[operation["externalMapId"]] = expected_external

    if expected_internal:
      maps_by_internal_id[(operation["externalMatchId"],
                           operation["mapNumber"])] = expected_internal

  return conflicts, maps_by_external_id, maps_by_internal_id


def _resolve_mongo_references(database, plan: dict) -> tuple[dict, list[str]]:
  errors = []
  mappings = {item["externalMatchId"]: item for item in plan["matchMappings"]}
  matches_by_external_id = {}
  team_slugs = set()

  for mapping in mappings.values():
    team_slugs.update(mapping["canonicalTeamSlugs"])

  for operation in plan["playerOperations"]:
    team_slugs.add(operation["documentTemplate"]["currentTeamSlug"])

  teams_by_slug = {
      item["slug"]: item
      for item in database["teams"].find({"slug": {
          "$in": sorted(team_slugs)
      }})
  } if team_slugs else {}
  missing_teams = sorted(team_slugs - set(teams_by_slug))
  errors.extend(f"Canonical Team '{slug}' is missing from MongoDB."
                for slug in missing_teams)
  teams_by_id = {
      document["_id"]: document
      for document in teams_by_slug.values()
  }

  for mapping in mappings.values():
    match = database["matches"].find_one({
        "source.provider":
        CANONICAL_MATCH_PROVIDER,
        "externalMatchId":
        mapping["externalMatchId"]
    })

    if match is None:
      errors.append(
          f"Canonical Match {mapping['externalMatchId']} is missing from MongoDB."
      )
      continue

    matches_by_external_id[mapping["externalMatchId"]] = match

    if match.get("status") != "completed":
      errors.append(
          f"Canonical Match {mapping['externalMatchId']} is not completed in MongoDB."
      )

    if (not match.get("tournamentId")
        or not match.get("teams", {}).get("teamAId")
        or not match.get("teams", {}).get("teamBId")):
      errors.append(
          f"Canonical Match {mapping['externalMatchId']} has incomplete Tournament or Team references."
      )
      continue

    tournament = database["tournaments"].find_one(
        {"_id": match["tournamentId"]})

    if tournament is None or tournament.get("slug") != mapping.get(
        "tournamentSlug"):
      errors.append(
          f"Canonical Match {mapping['externalMatchId']} has a missing or mismatched Tournament reference."
      )

    team_a = teams_by_id.get(match["teams"]["teamAId"])
    team_b = teams_by_id.get(match["teams"]["teamBId"])

    if team_a is None or team_b is None:
      errors.append(
          f"Canonical Match {mapping['externalMatchId']} references a missing Team document."
      )
    elif [team_a.get("slug"),
          team_b.get("slug")] != mapping.get("canonicalTeamSlugs"):
      errors.append(
          f"Canonical Match {mapping['externalMatchId']} Team references disagree with reconciliation."
      )

  players_by_slug = {}

  for operation in plan["playerOperations"]:
    slug = operation["documentTemplate"]["slug"]
    current = database["players"].find_one({"slug": slug})

    if current:
      players_by_slug[slug] = current

  map_identity_conflicts, maps_by_external_id, maps_by_internal_id = _map_identity_conflicts(
      database, plan, matches_by_external_id)

  errors.extend(
      f"Map identity conflict for {item['externalMapId']}: {item['reason']}."
      for item in map_identity_conflicts)

  return {
      "mappingsByExternalId": mappings,
      "matchesByExternalId": matches_by_external_id,
      "teamsBySlug": teams_by_slug,
      "playersBySlug": players_by_slug,
      "mapsByExternalId": maps_by_external_id,
      "mapsByInternalId": maps_by_internal_id,
      "indexConflicts": map_identity_conflicts
  }, errors


def _mongo_map_document(template: dict, context: dict) -> dict | None:
  match = context["matchesByExternalId"].get(template["externalMatchId"])

  if match is None:
    return None

  team_a = context["teamsBySlug"].get(template["teams"]["teamASlug"])
  team_b = context["teamsBySlug"].get(template["teams"]["teamBSlug"])
  picked_slug = template.get("pickedByTeamSlug")
  picked_team = context["teamsBySlug"].get(
      picked_slug) if picked_slug else None

  if team_a is None or team_b is None or (picked_slug and picked_team is None):
    return None

  winner_team = context["teamsBySlug"].get(template["winnerTeamSlug"])

  if winner_team is None:
    return None

  return {
      "tournamentId": match["tournamentId"],
      "matchId": match["_id"],
      "externalMapId": template["externalMapId"],
      "mapNumber": template["mapNumber"],
      "mapName": template["mapName"],
      "status": template["status"],
      "teamAId": team_a["_id"],
      "teamBId": team_b["_id"],
      "score": template["score"],
      "winnerTeamId": winner_team["_id"],
      "pickedByTeamId": picked_team["_id"] if picked_team else None,
      "startingSide": template["startingSide"],
      "overtime": template["overtime"],
      "startedAt": template.get("startedAt"),
      "completedAt": template.get("completedAt"),
      "source": template["source"]
  }


def _mongo_player_document(template: dict, context: dict) -> dict | None:
  team = context["teamsBySlug"].get(template.get("currentTeamSlug"))

  if team is None:
    return None

  return {
      "slug": template["slug"],
      "handle": template["handle"],
      "realName": template.get("realName"),
      "nationality": template.get("nationality"),
      "currentTeamId": team["_id"],
      "role": template["role"],
      "imageUrl": template.get("imageUrl"),
      "source": template["source"]
  }


def _mongo_player_stat_document(template: dict, context: dict,
                                players_by_slug: dict,
                                maps_by_external_id: dict) -> dict | None:
  player = players_by_slug.get(template["playerSlug"])
  match = context["matchesByExternalId"].get(template["externalMatchId"])
  match_map = maps_by_external_id.get(template["externalMapId"])
  team = context["teamsBySlug"].get(template["teamSlug"])
  opponent = context["teamsBySlug"].get(template["opponentTeamSlug"])

  if not player or not match or not match_map or not team or not opponent:
    return None

  return {
      "tournamentId": match["tournamentId"],
      "matchId": match["_id"],
      "matchMapId": match_map["_id"],
      "playerId": player["_id"],
      "teamId": team["_id"],
      "opponentTeamId": opponent["_id"],
      "mapName": template["mapName"],
      "agent": template["agent"],
      "roundsPlayed": template["roundsPlayed"],
      "overall": template["overall"],
      "attack": template["attack"],
      "defense": template["defense"],
      "source": template["source"]
  }


def _mongo_team_stat_document(template: dict, context: dict,
                              maps_by_external_id: dict) -> dict | None:
  match = context["matchesByExternalId"].get(template["externalMatchId"])
  match_map = maps_by_external_id.get(template["externalMapId"])
  team = context["teamsBySlug"].get(template["teamSlug"])
  opponent = context["teamsBySlug"].get(template["opponentTeamSlug"])

  if not match or not match_map or not team or not opponent:
    return None

  return {
      "tournamentId": match["tournamentId"],
      "matchId": match["_id"],
      "matchMapId": match_map["_id"],
      "teamId": team["_id"],
      "opponentTeamId": opponent["_id"],
      "mapName": template["mapName"],
      "roundsPlayed": template["roundsPlayed"],
      "roundsWon": template["roundsWon"],
      "roundsLost": template["roundsLost"],
      "kills": template["kills"],
      "deaths": template["deaths"],
      "assists": template["assists"],
      "firstKills": template["firstKills"],
      "firstDeaths": template["firstDeaths"],
      "attack": template["attack"],
      "defense": template["defense"],
      "source": template["source"]
  }


def _ensure_indexes(database, operations: list[dict]) -> None:
  for operation in operations:
    options = {
        key: value
        for key, value in operation.items()
        if key not in {"collection", "keys"}
    }
    database[operation["collection"]].create_index(
        [tuple(key) for key in operation["keys"]], **options)


def _record_outcome(counts: dict, collection_name: str, outcome: str) -> None:
  counts[outcome] += 1
  counts["byCollection"][collection_name][outcome] += 1


def _database_preflight(database, plan: dict) -> dict:
  collections = set(database.list_collection_names())
  context, reference_errors = _resolve_mongo_references(database, plan)
  result = {
      "plannedCreate":
      0,
      "plannedUpdate":
      0,
      "plannedUnchanged":
      0,
      "byCollection": {
          "players": {
              "create": 0,
              "update": 0,
              "unchanged": 0
          },
          "match_maps": {
              "create": 0,
              "update": 0,
              "unchanged": 0
          },
          "player_map_stats": {
              "create": 0,
              "update": 0,
              "unchanged": 0
          },
          "team_map_stats": {
              "create": 0,
              "update": 0,
              "unchanged": 0
          }
      },
      "create":
      0,
      "update":
      0,
      "unchanged":
      0,
      "skipped":
      plan.get("plannedCounts", {}).get("skippedMatches", 0),
      "indexConflicts":
      _index_conflicts(database, collections, plan["indexOperations"]),
      "plannedIndexCreates":
      _planned_index_creates(database, collections, plan["indexOperations"]),
      "duplicateKeyRisks":
      _duplicate_key_risks(database, collections),
      "completedCanonicalMatches":
      len(context["matchesByExternalId"]),
      "missingCanonicalReferences":
      reference_errors,
      "mapIdentityConflicts":
      context["indexConflicts"],
      "nullPreservingMerge": {
          "existingDocumentsChecked": 0,
          "preservedNonEmptyFields": []
      }
  }
  tournament = database["tournaments"].find_one(
      {"slug": plan.get("eventSlug")})
  result["plannedImportStatus"] = preview_import_status(
      "stats", _stats_data_as_of(plan), plan.get("planDigest"),
      result["skipped"], (tournament or {}).get("importStatus"))

  if reference_errors:
    return result

  for operation in plan["playerOperations"]:
    template = operation["documentTemplate"]
    current = context["playersBySlug"].get(template["slug"])
    document = _mongo_player_document(template, context)

    if document is None:
      result["missingCanonicalReferences"].append(
          f"Player {template['slug']} has an unresolved Team reference.")
      continue

    outcome = _operation_outcome("players", current, document)
    _record_outcome(result, "players", outcome)

    if current and document is not None:
      result["nullPreservingMerge"]["existingDocumentsChecked"] += 1
      result["nullPreservingMerge"]["preservedNonEmptyFields"].extend(
          f"players[{template['slug']}].{path}"
          for path in _preserved_paths("players", current, document))

  for operation in plan["matchMapOperations"]:
    template = operation["documentTemplate"]
    match = context["matchesByExternalId"].get(operation["externalMatchId"])
    current = context["mapsByExternalId"].get(operation["externalMapId"])

    if current is None and match is not None:
      current = context["mapsByInternalId"].get(
          (operation["externalMatchId"], operation["mapNumber"]))

    document = _mongo_map_document(template, context)

    if document is None:
      result["missingCanonicalReferences"].append(
          f"Map {operation['externalMapId']} has an unresolved MongoDB reference."
      )
      continue

    outcome = _operation_outcome("match_maps", current, document)
    _record_outcome(result, "match_maps", outcome)

    if current and document is not None:
      result["nullPreservingMerge"]["existingDocumentsChecked"] += 1
      result["nullPreservingMerge"]["preservedNonEmptyFields"].extend(
          f"match_maps[{operation['externalMapId']}].{path}"
          for path in _preserved_paths("match_maps", current, document))

  for operation in plan["playerMapStatOperations"]:
    template = operation["documentTemplate"]
    player = context["playersBySlug"].get(operation["playerSlug"])
    map_document = context["mapsByExternalId"].get(operation["externalMapId"])
    player_id = player.get("_id") if player else None
    map_id = map_document.get("_id") if map_document else None
    current = database["player_map_stats"].find_one({
        "matchMapId": map_id,
        "playerId": player_id
    }) if map_id and player_id else None
    document = _mongo_player_stat_document(template, context,
                                           context["playersBySlug"],
                                           context["mapsByExternalId"])

    if document is None:
      if player_id and map_id:
        result["missingCanonicalReferences"].append(
            f"Player Map Stats for {template['playerSlug']} has an unresolved Team or Match reference."
        )
        continue

      outcome = "create"
    else:
      outcome = _operation_outcome("player_map_stats", current, document)

    _record_outcome(result, "player_map_stats", outcome)

    if current and document is not None:
      result["nullPreservingMerge"]["existingDocumentsChecked"] += 1
      result["nullPreservingMerge"]["preservedNonEmptyFields"].extend(
          f"player_map_stats[{template['externalMatchId']}/{template['externalMapId']}/{template['playerSlug']}].{path}"
          for path in _preserved_paths("player_map_stats", current, document))

  for operation in plan["teamMapStatOperations"]:
    template = operation["documentTemplate"]
    map_document = context["mapsByExternalId"].get(operation["externalMapId"])
    team = context["teamsBySlug"].get(operation["teamSlug"])
    map_id = map_document.get("_id") if map_document else None
    team_id = team.get("_id") if team else None
    current = database["team_map_stats"].find_one({
        "matchMapId": map_id,
        "teamId": team_id
    }) if map_id and team_id else None
    document = _mongo_team_stat_document(template, context,
                                         context["mapsByExternalId"])

    if document is None:
      if map_id and team_id:
        result["missingCanonicalReferences"].append(
            f"Team Map Stats for {template['teamSlug']} has an unresolved Match reference."
        )
        continue

      outcome = "create"
    else:
      outcome = _operation_outcome("team_map_stats", current, document)

    _record_outcome(result, "team_map_stats", outcome)

    if current and document is not None:
      result["nullPreservingMerge"]["existingDocumentsChecked"] += 1
      result["nullPreservingMerge"]["preservedNonEmptyFields"].extend(
          f"team_map_stats[{template['externalMatchId']}/{template['externalMapId']}/{template['teamSlug']}].{path}"
          for path in _preserved_paths("team_map_stats", current, document))

  result["plannedCreate"] = sum(values["create"]
                                for values in result["byCollection"].values())
  result["plannedUpdate"] = sum(values["update"]
                                for values in result["byCollection"].values())
  result["plannedUnchanged"] = sum(
      values["unchanged"] for values in result["byCollection"].values())
  result["estimatedWriteUnits"] = result["plannedCreate"] + result[
      "plannedUpdate"] + result["plannedIndexCreates"] + 1
  result["nullPreservingMerge"]["preservedNonEmptyFields"] = sorted(
      set(result["nullPreservingMerge"]["preservedNonEmptyFields"]))

  return result


def _stats_data_as_of(plan: dict) -> datetime | None:
  return parse_source_datetime((plan.get("duckdb")
                                or {}).get("sourceTimestamp"))


def _preflight_blockers(result: dict, plan: dict) -> list[str]:
  blockers = []
  blockers.extend(f"Missing reference: {item}"
                  for item in result.get("missingCanonicalReferences", []))
  blockers.extend(
      f"Index conflict in {item['collection']}: {item['indexName']}"
      for item in result.get("indexConflicts", []))
  blockers.extend(f"Duplicate key risk in {item['collection']}: {item['key']}"
                  for item in result.get("duplicateKeyRisks", []))
  blockers.extend(
      f"Map identity conflict for {item['externalMapId']}: {item['reason']}"
      for item in result.get("mapIdentityConflicts", []))
  expected_matches = (plan.get("plannedCounts") or {}).get("verifiedMatches")

  if (not isinstance(expected_matches, int)
      or result.get("completedCanonicalMatches") != expected_matches):
    blockers.append(
        "Stats references do not match the verified canonical Match mappings.")

  return blockers


def preflight_stats_plan(plan: dict, settings: Settings,
                         plan_path: Path) -> tuple[dict, Path]:
  generated_at = datetime.now(timezone.utc)
  report = {
      "runId": str(uuid.uuid4()),
      "generatedAt": _iso_utc(generated_at),
      "statsWritePlanPath": str(plan_path.expanduser().resolve()),
      "planDigest": plan.get("planDigest"),
      "plannedCreate": None,
      "plannedUpdate": None,
      "plannedUnchanged": None,
      "byCollection": None,
      "skipped": (plan.get("plannedCounts") or {}).get("skippedMatches", 0),
      "completedCanonicalMatches": 0,
      "indexConflicts": [],
      "plannedIndexCreates": None,
      "estimatedWriteUnits": None,
      "duplicateKeyRisks": [],
      "mapIdentityConflicts": [],
      "missingCanonicalReferences": [],
      "nullPreservingMerge": {
          "existingDocumentsChecked": 0,
          "preservedNonEmptyFields": []
      },
      "plannedImportStatus": None,
      "mongodbConnected": False,
      "mongodbWrites": 0,
      "errors": []
  }
  errors = verify_stats_plan_integrity(plan,
                                       plan_path) + _validate_stats_plan(plan)

  if errors:
    report["errors"] = errors
  else:
    from app.database import create_mongodb_client

    client = None

    try:
      client = create_mongodb_client(settings)
      client.admin.command("ping")
      report["mongodbConnected"] = True
      inspection = _database_preflight(client[settings.mongodb_database], plan)

      for field in ("plannedCreate", "plannedUpdate", "plannedUnchanged",
                    "byCollection", "skipped", "completedCanonicalMatches",
                    "indexConflicts", "duplicateKeyRisks",
                    "mapIdentityConflicts", "missingCanonicalReferences",
                    "nullPreservingMerge", "plannedIndexCreates",
                    "estimatedWriteUnits", "plannedImportStatus"):
        report[field] = inspection[field]

      report["errors"] = _preflight_blockers(inspection, plan)
    except Exception as error:
      report["errors"].append(
          f"Stats Preflight failed ({type(error).__name__}); MongoDB writes remain 0."
      )
    finally:
      if client is not None:
        client.close()

  report_path = settings.report_directory / f"stats-import-preflight-{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  report["reportPath"] = str(report_path.resolve())
  write_json_atomic(report_path, report)

  return report, report_path


def record_stats_import_success(plan: dict, settings: Settings,
                                postflight: dict, import_result: str) -> dict:
  if import_result not in {"unchanged", "applied", "partial"}:
    raise ValueError("Invalid Stats import result.")

  expected_matches = (plan.get("plannedCounts") or {}).get("verifiedMatches")
  source_digests = plan.get("sourceDigests") or {}
  counts = plan.get("plannedCounts") or {}
  digest_fields = ("duckdbSha256", "duckdbSchemaFingerprint",
                   "canonicalScheduleMappingDigest")
  expected_document_counts = {
      "players": counts.get("players"),
      "matchMaps": counts.get("matchMaps"),
      "playerMapStats": counts.get("playerMapStats"),
      "teamMapStats": counts.get("teamMapStats")
  }
  postflight_collections = postflight.get("byCollection")
  postflight_collections = postflight_collections if isinstance(
      postflight_collections, dict) else {}
  all_documents_unchanged = all(
      isinstance(collection_summary := postflight_collections.get(collection),
                 dict) and collection_summary.get("create") == 0
      and collection_summary.get("update") == 0 and collection_summary.get(
          "unchanged") == expected_document_counts.get(field)
      for collection, field in (("players", "players"), ("match_maps",
                                                         "matchMaps"),
                                ("player_map_stats", "playerMapStats"),
                                ("team_map_stats", "teamMapStats")))

  if (any(not isinstance(source_digests.get(field), str)
          or not re.fullmatch(r"[0-9a-f]{64}", source_digests[field])
          for field in digest_fields)
      or any(not isinstance(value, int) or isinstance(value, bool) or value < 0
             for value in expected_document_counts.values())
      or not isinstance(expected_matches, int) or expected_matches < 0
      or not all_documents_unchanged
      or postflight.get("mongodbConnected") is not True
      or postflight.get("mongodbWrites") != 0
      or postflight.get("planDigest") != plan.get("planDigest")
      or postflight.get("plannedCreate") != 0
      or postflight.get("plannedUpdate") != 0
      or postflight.get("completedCanonicalMatches") != expected_matches
      or postflight.get("errors") or postflight.get("indexConflicts")
      or postflight.get("duplicateKeyRisks")
      or postflight.get("mapIdentityConflicts")
      or postflight.get("missingCanonicalReferences")):
    return {
        "recorded": False,
        "mongodbConnected": postflight.get("mongodbConnected") is True,
        "mongodbWrites": 0
    }

  from app.database import create_mongodb_client

  verification = {
      "duckdbSha256":
      source_digests.get("duckdbSha256"),
      "duckdbSchemaFingerprint":
      source_digests.get("duckdbSchemaFingerprint"),
      "canonicalScheduleMappingDigest":
      source_digests.get("canonicalScheduleMappingDigest"),
      "sourceTimestamp":
      _stats_data_as_of(plan),
      "statsDocumentCounts":
      expected_document_counts,
      "statsLinkedMatches":
      expected_matches,
      "postflightStatus":
      "passed",
      "statsImportStatus":
      import_result
  }
  client = None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    record_import_status(
        client[settings.mongodb_database], plan["eventSlug"], "stats",
        "partial" if import_result == "partial" else "success",
        _stats_data_as_of(plan), plan.get("planDigest"), verification,
        verification)

    return {"recorded": True, "mongodbConnected": True, "mongodbWrites": 1}
  finally:
    if client is not None:
      client.close()


def apply_stats_plan(plan: dict,
                     settings: Settings,
                     plan_path: Path,
                     max_writes: int | None = None) -> dict:
  errors = verify_stats_plan_integrity(plan,
                                       plan_path) + _validate_stats_plan(plan)
  result = {
      "mongodbConnected": False,
      "mongodbWrites": 0,
      "created": 0,
      "updated": 0,
      "unchanged": 0,
      "skipped": (plan.get("plannedCounts") or {}).get("skippedMatches", 0),
      "failed": 0,
      "estimatedWriteUnits": None,
      "byCollection": {
          "players": {
              "created": 0,
              "updated": 0,
              "unchanged": 0
          },
          "match_maps": {
              "created": 0,
              "updated": 0,
              "unchanged": 0
          },
          "player_map_stats": {
              "created": 0,
              "updated": 0,
              "unchanged": 0
          },
          "team_map_stats": {
              "created": 0,
              "updated": 0,
              "unchanged": 0
          }
      },
      "errors": errors
  }

  if errors:
    return result

  from app.database import create_mongodb_client

  client = None
  apply_started = False
  import_status_recorded = False

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    result["mongodbConnected"] = True
    database = client[settings.mongodb_database]
    inspection = _database_preflight(database, plan)
    blockers = _preflight_blockers(inspection, plan)

    if blockers:
      result["errors"].extend(blockers)

      return result

    if max_writes is not None and inspection[
        "estimatedWriteUnits"] > max_writes:
      result["errors"].append(
          "Stats Apply estimate exceeds the configured safety limit; no indexes or documents were written."
      )
      result["estimatedWriteUnits"] = inspection["estimatedWriteUnits"]

      return result

    latest_errors = verify_stats_plan_integrity(
        plan, plan_path) + _validate_stats_plan(plan)

    if latest_errors:
      result["errors"].extend(latest_errors)

      return result

    result["estimatedWriteUnits"] = inspection["estimatedWriteUnits"]
    apply_started = True
    _ensure_indexes(database, plan["indexOperations"])
    result["mongodbWrites"] += inspection["plannedIndexCreates"]
    now = datetime.now(timezone.utc)
    initial_context, reference_errors = _resolve_mongo_references(
        database, plan)

    if reference_errors:
      result["errors"].extend(reference_errors)
      raise RuntimeError("Stats canonical reference validation failed.")

    players_by_slug = {}

    for operation in plan["playerOperations"]:
      template = operation["documentTemplate"]
      document = _mongo_player_document(template, initial_context)

      if document is None:
        result["errors"].append(
            f"Player {template['slug']} has an unresolved current Team reference."
        )
        raise RuntimeError("Stats Player reference validation failed.")

      outcome, player_id = _upsert(database["players"], "players",
                                   operation["key"], document, now)

      result[outcome] += 1
      result["byCollection"]["players"][outcome] += 1

      if outcome in {"created", "updated"}:
        result["mongodbWrites"] += 1

      players_by_slug[template["slug"]] = database["players"].find_one(
          {"_id": player_id})

    maps_by_external_id = {}

    for operation in plan["matchMapOperations"]:
      template = operation["documentTemplate"]
      document = _mongo_map_document(template, initial_context)

      if document is None:
        result["errors"].append(
            f"Map {operation['externalMapId']} has an unresolved canonical Match or Team reference."
        )
        raise RuntimeError("Stats Match Map reference validation failed.")

      outcome, map_id = _upsert(
          database["match_maps"], "match_maps", {
              "source.provider": STATS_PROVIDER,
              "externalMapId": operation["externalMapId"]
          }, document, now)

      result[outcome] += 1
      result["byCollection"]["match_maps"][outcome] += 1

      if outcome in {"created", "updated"}:
        result["mongodbWrites"] += 1

      maps_by_external_id[
          operation["externalMapId"]] = database["match_maps"].find_one(
              {"_id": map_id})

    missing_references = []
    player_stat_documents = []

    for operation in plan["playerMapStatOperations"]:
      template = operation["documentTemplate"]
      document = _mongo_player_stat_document(template, initial_context,
                                             players_by_slug,
                                             maps_by_external_id)

      if document is None:
        missing_references.append(
            f"Player Map Stats {template['externalMatchId']}/{template['externalMapId']}/{template['playerSlug']}"
        )
      else:
        player_stat_documents.append((operation, document))

    team_stat_documents = []

    for operation in plan["teamMapStatOperations"]:
      template = operation["documentTemplate"]
      document = _mongo_team_stat_document(template, initial_context,
                                           maps_by_external_id)

      if document is None:
        missing_references.append(
            f"Team Map Stats {template['externalMatchId']}/{template['externalMapId']}/{template['teamSlug']}"
        )
      else:
        team_stat_documents.append((operation, document))

    if missing_references:
      result["errors"].append(
          "All planned references must resolve before Stats writes; unresolved: "
          + ", ".join(missing_references[:20]))
      raise RuntimeError("Stats Map reference validation failed.")

    for operation, document in player_stat_documents:
      outcome, _ = _upsert(database["player_map_stats"], "player_map_stats", {
          "matchMapId": document["matchMapId"],
          "playerId": document["playerId"]
      }, document, now)

      result[outcome] += 1
      result["byCollection"]["player_map_stats"][outcome] += 1

      if outcome in {"created", "updated"}:
        result["mongodbWrites"] += 1

    for operation, document in team_stat_documents:
      outcome, _ = _upsert(database["team_map_stats"], "team_map_stats", {
          "matchMapId": document["matchMapId"],
          "teamId": document["teamId"]
      }, document, now)

      result[outcome] += 1
      result["byCollection"]["team_map_stats"][outcome] += 1

      if outcome in {"created", "updated"}:
        result["mongodbWrites"] += 1

    if not result["errors"] and result["failed"] == 0:
      result["importStatus"] = "awaiting-postflight"
  except Exception as error:
    result["failed"] += 1
    result["errors"].append(
        f"Stats Apply failed ({type(error).__name__}); MongoDB credentials were not logged."
    )

    if apply_started and client is not None and not import_status_recorded:
      try:
        record_import_status(client[settings.mongodb_database],
                             settings.tournament_slug, "stats", "failed", None,
                             None)
        result["mongodbWrites"] += 1
      except Exception:
        result["errors"].append("Failed import status could not be recorded.")
  finally:
    if client is not None:
      client.close()

  return result


def write_stats_plan_file(plan: dict, output_directory: Path) -> Path:
  generated_at = datetime.fromisoformat(plan["generatedAt"].replace(
      "Z", "+00:00"))
  path = output_directory / f"stats-import-plan-{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  plan["writePlanPath"] = str(path.resolve())
  plan["planDigest"] = calculate_plan_digest(plan)
  write_json_atomic(path, plan)

  return path
