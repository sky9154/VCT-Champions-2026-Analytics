import logging
import re
import time
from datetime import timezone
from pathlib import Path

import duckdb

from app.importers.models import DuckDbSnapshot, ImportRunError, MatchDTO, TeamDTO
from app.importers.providers.vct_snapshot import read_source_metadata, read_source_timestamp
from app.utils.files import sha256_file
from app.utils.dates import parse_utc_datetime

LOGGER = logging.getLogger(__name__)
EXPECTED_PLAYERS_PER_MAP = 10
VCT_REFERENCE_BASE_URL = "https://vct-reference.com"
REQUIRED_COLUMNS = {
    "matches": {
        "match_id", "event_id", "event", "event_slug", "event_url", "url",
        "utc_timestamp", "listing_status", "status", "team0_id", "team1_id",
        "score0", "score1", "format", "series_stage", "series_round"
    },
    "teams": {"team_id", "team_name", "team_url"},
    "maps": {
        "match_id", "game_id", "map_name", "score0", "score1",
        "performance_available", "economy_available"
    },
    "player_map": {
        "match_id", "game_id", "player_id", "team_idx", "rating_all",
        "acs_all", "adr_all", "kast_all", "kills_all", "deaths_all",
        "assists_all"
    }
}


def _source_url(value: str | None) -> str | None:
  if not value:
    return None

  if value.startswith("https://"):
    return value

  return f"{VCT_REFERENCE_BASE_URL}/{value.lstrip('/')}"


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
        f"VCT Reference DuckDB is missing required tables: {', '.join(missing_tables)}."
    )

  for table_name, required_columns in REQUIRED_COLUMNS.items():
    columns = {
        row[0]
        for row in connection.execute(f'DESCRIBE "{table_name}"').fetchall()
    }
    missing_columns = sorted(required_columns - columns)

    if missing_columns:
      raise ImportRunError(
          f"VCT Reference DuckDB table {table_name} is missing required columns: {', '.join(missing_columns)}."
      )


def _validate_event_identities(connection: duckdb.DuckDBPyConnection,
                               event_slug: str) -> None:
  match_quality = connection.execute(
      """
    SELECT
      COUNT(*) FILTER (WHERE match_id IS NULL OR CAST(match_id AS VARCHAR) = '') AS missing_ids,
      COUNT(*) - COUNT(DISTINCT match_id) AS duplicate_ids
    FROM matches
    WHERE event_slug = ?
    """, [event_slug]).fetchall()[0]

  if match_quality[0] or match_quality[1]:
    raise ImportRunError(
        "VCT Reference event contains missing or duplicate match IDs.")

  for table_name, identity_columns in (("maps", "match_id, game_id"),
                                       ("player_map",
                                        "match_id, game_id, player_id")):
    missing = connection.execute(
        f"""
      SELECT COUNT(*)
      FROM {table_name} source_rows
      JOIN matches event_matches USING (match_id)
      WHERE event_matches.event_slug = ?
        AND ({' OR '.join(f'{column} IS NULL' for column in identity_columns.split(', '))})
      """, [event_slug]).fetchall()[0][0]
    duplicate = connection.execute(
        f"""
      SELECT COUNT(*)
      FROM (
        SELECT {identity_columns}
        FROM {table_name} source_rows
        JOIN matches event_matches USING (match_id)
        WHERE event_matches.event_slug = ?
        GROUP BY {identity_columns}
        HAVING COUNT(*) > 1
      ) duplicate_keys
      """, [event_slug]).fetchall()[0][0]

    if missing or duplicate:
      raise ImportRunError(
          f"VCT Reference event has missing or duplicate identity keys in {table_name}."
      )


def _load_match_rows(connection: duckdb.DuckDBPyConnection, event_slug: str):
  return connection.execute(
      """
    WITH event_matches AS (
      SELECT *
      FROM matches
      WHERE event_slug = ?
    ),
    map_coverage AS (
      SELECT
        mp.match_id,
        COUNT(DISTINCT mp.game_id) FILTER (
          WHERE mp.map_name IS NOT NULL
            AND mp.map_name <> ''
            AND mp.score0 IS NOT NULL
            AND mp.score1 IS NOT NULL
        ) AS complete_map_count,
        COUNT(DISTINCT mp.game_id) FILTER (
          WHERE mp.map_name IS NOT NULL
            AND mp.map_name <> ''
            AND mp.score0 IS NOT NULL
            AND mp.score1 IS NOT NULL
        ) * ? AS expected_player_rows
      FROM maps mp
      JOIN event_matches em USING (match_id)
      GROUP BY mp.match_id
    ),
    player_coverage AS (
      SELECT
        pm.match_id,
        COUNT(*) FILTER (
          WHERE pm.rating_all IS NOT NULL
            AND pm.acs_all IS NOT NULL
            AND pm.adr_all IS NOT NULL
            AND pm.kast_all IS NOT NULL
            AND pm.kills_all IS NOT NULL
            AND pm.deaths_all IS NOT NULL
            AND pm.assists_all IS NOT NULL
        ) AS complete_player_rows
      FROM player_map pm
      JOIN event_matches em USING (match_id)
      GROUP BY pm.match_id
    )
    SELECT
      m.match_id,
      m.event_id,
      m.event,
      m.event_slug,
      m.event_url,
      m.url,
      m.utc_timestamp,
      m.listing_status,
      m.status,
      m.team0_id,
      team0.team_name,
      m.team1_id,
      team1.team_name,
      m.score0,
      m.score1,
      m.format,
      m.series_stage,
      m.series_round,
      COALESCE(map_coverage.complete_map_count, 0) AS complete_map_count,
      COALESCE(player_coverage.complete_player_rows, 0) AS complete_player_rows,
      COALESCE(map_coverage.expected_player_rows, 0) AS expected_player_rows
    FROM event_matches m
    LEFT JOIN teams team0 ON m.team0_id = team0.team_id
    LEFT JOIN teams team1 ON m.team1_id = team1.team_id
    LEFT JOIN map_coverage USING (match_id)
    LEFT JOIN player_coverage USING (match_id)
    ORDER BY m.utc_timestamp, m.match_id
    """, [event_slug, EXPECTED_PLAYERS_PER_MAP]).fetchall()


class VctReferenceProvider:

  def __init__(self, database_path: Path):
    self.database_path = database_path

  def fetch_matches(self,
                    event_slug: str) -> tuple[list[MatchDTO], DuckDbSnapshot]:
    if not self.database_path.is_file():
      raise ImportRunError(
          f"VCT Reference DuckDB file does not exist: {self.database_path}.")

    started_at = time.monotonic()
    LOGGER.info(
        "provider=vct-reference stage=connect event=%s match_identity=event elapsed_seconds=0 result=started retry_count=0",
        event_slug)
    source_timestamp = read_source_timestamp(self.database_path)
    source_metadata = read_source_metadata(self.database_path)
    connection = None

    try:
      connection = duckdb.connect(str(self.database_path), read_only=True)
      _validate_schema(connection)
      _validate_event_identities(connection, event_slug)
      rows = _load_match_rows(connection, event_slug)
    except duckdb.Error as error:
      raise ImportRunError(
          "VCT Reference DuckDB could not be read or queried.") from error
    finally:
      if connection is not None:
        connection.close()

    if not rows:
      raise ImportRunError(
          f"VCT Reference contains no matches for event '{event_slug}'.")

    matches = []

    for row in rows:
      (match_id, event_id, event_name, row_event_slug, event_url, match_url,
       scheduled_at, listing_status, status, team_a_id, team_a_name, team_b_id,
       team_b_name, score_a, score_b, best_of, stage, round_name, map_count,
       player_rows, expected_player_rows) = row
      normalized_status = "completed" if listing_status == "Completed" and status == "final" else (
          "scheduled" if listing_status == "Upcoming" else "tbd")
      is_completed = normalized_status == "completed"
      stats_available = (is_completed and map_count > 0
                         and expected_player_rows > 0
                         and player_rows >= expected_player_rows)
      source_url = _source_url(match_url) or _source_url(event_url)
      group_match = re.search(r"\(([A-D])\)", str(
          round_name or "")) if stage == "Group Stage" else None
      match = MatchDTO(
          provider="vct-reference",
          external_id=str(match_id) if match_id is not None else None,
          event_slug=row_event_slug,
          event_name=event_name,
          team_a=TeamDTO(
              name=team_a_name,
              external_id=str(team_a_id) if team_a_id is not None else None),
          team_b=TeamDTO(
              name=team_b_name,
              external_id=str(team_b_id) if team_b_id is not None else None),
          scheduled_at=parse_utc_datetime(scheduled_at, timezone.utc),
          stage=stage,
          group_name=group_match.group(1) if group_match else None,
          round_name=round_name,
          status=normalized_status,
          score_a=score_a,
          score_b=score_b,
          best_of=int(best_of[2:]) if isinstance(best_of, str)
          and best_of.startswith("Bo") and best_of[2:].isdigit() else None,
          source_url=source_url,
          event_external_id=str(event_id) if event_id is not None else None,
          stats_available=stats_available,
          stats_map_count=map_count,
          stats_player_rows=player_rows)

      matches.append(match)

    content_sha256 = sha256_file(self.database_path)
    snapshot = DuckDbSnapshot(
        provider="vct-reference",
        source_url=VCT_REFERENCE_BASE_URL,
        fetched_at=None,
        cache_status="read-only-local",
        revision=source_metadata.get("version") if isinstance(
            source_metadata.get("version"), str) else None,
        source_timestamp=source_timestamp,
        content_sha256=content_sha256
        if isinstance(content_sha256, str) else None,
        database_path=str(self.database_path),
        read_only=True)

    LOGGER.info(
        "provider=vct-reference stage=fetch event=%s match_identity=event elapsed_seconds=%.3f result=complete retry_count=0 match_count=%s",
        event_slug,
        time.monotonic() - started_at, len(matches))

    return matches, snapshot
