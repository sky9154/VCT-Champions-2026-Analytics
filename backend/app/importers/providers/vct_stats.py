from datetime import timezone
from pathlib import Path

import duckdb
from pydantic import ValidationError

from app.importers.models import ImportRunError
from app.importers.stats_models import (MapStatsDTO, PlayerDTO,
                                        PlayerMapStatsDTO, RoundStatsDTO,
                                        SourceMatchStatsDTO)

VCT_REFERENCE_BASE_URL = "https://vct-reference.com"
STAT_FIELDS = ("rating_all", "rating_atk", "rating_def", "acs_all", "acs_atk",
               "acs_def", "adr_all", "adr_atk", "adr_def", "kills_all",
               "kills_atk", "kills_def", "deaths_all", "deaths_atk",
               "deaths_def", "assists_all", "assists_atk", "assists_def",
               "fk_all", "fk_atk", "fk_def", "fd_all", "fd_atk", "fd_def",
               "kast_all", "kast_atk", "kast_def", "hs_pct_all", "hs_pct_atk",
               "hs_pct_def", "clutch_1v1", "clutch_1v2", "clutch_1v3",
               "clutch_1v4", "clutch_1v5")
REQUIRED_COLUMNS = {
    "matches": {
        "match_id", "event_slug", "listing_status", "status", "team0_id",
        "team1_id", "utc_timestamp", "url"
    },
    "maps": {
        "match_id", "game_id", "map_name", "score0", "score1",
        "picked_by_team", "performance_available", "economy_available"
    },
    "player_map":
    {"match_id", "game_id", "player_id", "team_idx", "agents", *STAT_FIELDS},
    "players": {"player_id", "player_name", "country", "player_url"},
    "rounds": {"match_id", "game_id", "round_num", "winner_team", "side"}
}


def _source_url(value: str | None) -> str | None:
  if not value:
    return None

  if value.startswith("https://"):
    return value

  return f"{VCT_REFERENCE_BASE_URL}/{value.lstrip('/')}"


def _table_columns(connection: duckdb.DuckDBPyConnection,
                   table_name: str) -> set[str]:
  return {
      row[0]
      for row in connection.execute(f'DESCRIBE "{table_name}"').fetchall()
  }


def _validate_schema(connection: duckdb.DuckDBPyConnection) -> None:
  tables = {
      row[0]
      for row in connection.execute(
          "SELECT table_name FROM information_schema.tables WHERE table_schema = 'main'"
      ).fetchall()
  }
  missing_tables = sorted(set(REQUIRED_COLUMNS) - tables)

  if missing_tables:
    raise ImportRunError(
        f"DuckDB is missing required stats tables: {', '.join(missing_tables)}."
    )

  for table_name, required_columns in REQUIRED_COLUMNS.items():
    missing_columns = sorted(required_columns -
                             _table_columns(connection, table_name))

    if missing_columns:
      raise ImportRunError(
          f"DuckDB table {table_name} is missing required stats columns: {', '.join(missing_columns)}."
      )


def _dict_rows(cursor) -> list[dict]:
  names = [column[0] for column in cursor.description]

  return [dict(zip(names, row)) for row in cursor.fetchall()]


def _validation_message(error: ValidationError, label: str) -> str:
  fields = ", ".join(".".join(str(part) for part in item["loc"])
                     for item in error.errors())

  return f"{label} failed DTO validation for fields: {fields or 'unknown'}"


class VctStatsProvider:

  def __init__(self, database_path: Path):
    self.database_path = database_path

  def fetch_matches(self,
                    match_ids: list[str]) -> dict[str, SourceMatchStatsDTO]:
    if not self.database_path.is_file():
      raise ImportRunError("VCT Reference DuckDB file does not exist.")

    requested_ids = sorted(set(match_ids))

    if not requested_ids:
      return {}

    placeholders = ", ".join("?" for _ in requested_ids)
    connection = None

    try:
      connection = duckdb.connect(str(self.database_path), read_only=True)
      _validate_schema(connection)
      match_rows = _dict_rows(
          connection.execute(
              f"""
        SELECT match_id, event_slug, listing_status, status, team0_id, team1_id,
          utc_timestamp, url
        FROM matches
        WHERE CAST(match_id AS VARCHAR) IN ({placeholders})
        """, requested_ids))
      map_rows = _dict_rows(
          connection.execute(
              f"""
        SELECT match_id, game_id, map_name, score0, score1, picked_by_team,
          performance_available, economy_available
        FROM maps
        WHERE CAST(match_id AS VARCHAR) IN ({placeholders})
        ORDER BY match_id, TRY_CAST(game_id AS INTEGER), game_id
        """, requested_ids))
      stat_select = ", ".join(f"pm.{column} AS {column}"
                              for column in STAT_FIELDS)
      player_rows = _dict_rows(
          connection.execute(
              f"""
        SELECT pm.match_id, pm.game_id, pm.player_id, pm.team_idx, pm.agents,
          {stat_select}, p.player_name, p.country, p.player_url
        FROM player_map pm
        LEFT JOIN players p ON p.player_id = pm.player_id
        WHERE CAST(pm.match_id AS VARCHAR) IN ({placeholders})
        ORDER BY pm.match_id, TRY_CAST(pm.game_id AS INTEGER), pm.game_id, pm.player_id
        """, requested_ids))
      round_rows = _dict_rows(
          connection.execute(
              f"""
        SELECT match_id, game_id, round_num, winner_team, side
        FROM rounds
        WHERE CAST(match_id AS VARCHAR) IN ({placeholders})
        ORDER BY match_id, TRY_CAST(game_id AS INTEGER), game_id, round_num
        """, requested_ids))
    except (duckdb.Error, OSError) as error:
      raise ImportRunError(
          f"DuckDB stats read failed ({type(error).__name__}).") from error
    finally:
      if connection is not None:
        connection.close()

    result: dict[str, SourceMatchStatsDTO] = {}

    for row in match_rows:
      source_id = str(row["match_id"])
      timestamp = row["utc_timestamp"]

      if timestamp is not None and (timestamp.tzinfo is None
                                    or timestamp.utcoffset() is None):
        timestamp = timestamp.replace(tzinfo=timezone.utc)

      result[source_id] = SourceMatchStatsDTO(
          match_id=source_id,
          event_slug=row["event_slug"],
          listing_status=row["listing_status"],
          status=row["status"],
          team0_id=str(row["team0_id"])
          if row["team0_id"] is not None else None,
          team1_id=str(row["team1_id"])
          if row["team1_id"] is not None else None,
          utc_timestamp=timestamp,
          source_url=_source_url(row["url"]))

    maps_by_match: dict[str, list[MapStatsDTO]] = {
        match_id: []
        for match_id in result
    }
    maps_by_key: dict[tuple[str, str | None], MapStatsDTO] = {}

    for row in map_rows:
      match_id = str(row["match_id"])
      game_id = str(row["game_id"]) if row["game_id"] is not None else None
      map_key = (match_id, game_id)
      errors = []

      if map_key in maps_by_key:
        errors.append("Duplicate game_id rows were found in maps.")
        maps_by_key[map_key].source_errors.extend(errors)
        continue

      try:
        map_dto = MapStatsDTO(
            match_id=match_id,
            game_id=game_id,
            map_name=row["map_name"],
            score0=row["score0"],
            score1=row["score1"],
            picked_by_team=row["picked_by_team"],
            performance_available=row["performance_available"],
            economy_available=row["economy_available"])
      except ValidationError as error:
        map_dto = MapStatsDTO(match_id=match_id,
                              game_id=game_id,
                              map_name=None,
                              performance_available=None,
                              economy_available=None)

        errors.append(
            _validation_message(error,
                                f"Map {game_id or '(missing game_id)'}"))

      map_dto.source_errors.extend(errors)
      maps_by_key[map_key] = map_dto

      if match_id in maps_by_match:
        maps_by_match[match_id].append(map_dto)
      else:
        continue

    for row in player_rows:
      match_id = str(row["match_id"])
      game_id = str(row["game_id"]) if row["game_id"] is not None else None
      map_dto = maps_by_key.get((match_id, game_id))

      if map_dto is None:
        if match_id in result:
          result[match_id].source_errors.append(
              f"Player Map row references missing Map {game_id or '(missing game_id)'}."
          )

        continue

      try:
        if row["player_id"] is None:
          map_dto.source_errors.append(
              "Player Map row has no Player identity.")
          continue

        player = PlayerDTO(external_id=str(row["player_id"]),
                           name=row["player_name"],
                           country=row["country"],
                           url=_source_url(row["player_url"]))
        values = {field: row[field] for field in STAT_FIELDS}
        player_map = PlayerMapStatsDTO(external_player_id=str(
            row["player_id"]),
                                       team_idx=row["team_idx"],
                                       player=player,
                                       agents=row["agents"],
                                       **values)
      except ValidationError as error:
        map_dto.source_errors.append(
            _validation_message(
                error, f"Player Map {game_id or '(missing game_id)'}"))
        continue

      map_dto.player_rows.append(player_map)

    for row in round_rows:
      match_id = str(row["match_id"])
      game_id = str(row["game_id"]) if row["game_id"] is not None else None
      map_dto = maps_by_key.get((match_id, game_id))

      if map_dto is None:
        if match_id in result:
          result[match_id].source_errors.append(
              f"Round row references missing Map {game_id or '(missing game_id)'}."
          )

        continue

      try:
        round_dto = RoundStatsDTO(round_num=row["round_num"],
                                  winner_team=row["winner_team"],
                                  side=row["side"])
      except ValidationError as error:
        map_dto.source_errors.append(
            _validation_message(
                error, f"Round on Map {game_id or '(missing game_id)'}"))
        continue

      map_dto.rounds.append(round_dto)

    for match_id, match in result.items():
      match.maps = maps_by_match.get(match_id, [])

    missing_ids = sorted(set(requested_ids) - set(result))

    for match_id in missing_ids:
      raise ImportRunError(
          f"DuckDB has no rows for reconciled match ID {match_id}.")

    return result
