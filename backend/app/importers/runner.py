import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from app.config import Settings
from app.utils.files import write_json_atomic
from app.importers.models import (ImportRunError, MatchDTO, ProviderSnapshot,
                                  ReconciliationReport)
from app.importers.providers.liquipedia import LiquipediaProvider
from app.importers.providers.vct_reference import VctReferenceProvider
from app.importers.reconciliation import reconcile_matches
from app.importers.validation import validate_match

LOGGER = logging.getLogger(__name__)


def _iso_utc(value: datetime) -> str:
  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _source_summary(snapshot: ProviderSnapshot | None, provider: str,
                    source_url: str, status: str) -> dict:
  if snapshot is None:
    source = {
        "provider": provider,
        "url": source_url,
        "status": status,
        "fetchedAt": None,
        "cacheStatus": status,
        "revision": None,
        "sourceTimestamp": None,
        "contentSha256": None
    }
  else:
    source = {
        "provider":
        snapshot.provider,
        "url":
        snapshot.source_url,
        "status":
        status,
        "fetchedAt":
        _iso_utc(snapshot.fetched_at) if snapshot.fetched_at else None,
        "cacheStatus":
        snapshot.cache_status,
        "revision":
        snapshot.revision,
        "sourceTimestamp":
        _iso_utc(snapshot.source_timestamp)
        if snapshot.source_timestamp else None,
        "contentSha256":
        snapshot.content_sha256
    }

  if provider == "liquipedia":
    source["attribution"] = "Liquipedia text content; CC BY-SA 3.0"

  return source


def _add_match_warnings(matches: list[MatchDTO], warnings: list[dict]) -> None:
  for match in matches:
    for message in validate_match(match):
      warnings.append({
          "provider": match.provider,
          "event": match.event_slug,
          "matchIdentity": match.external_id,
          "message": message,
          "sourceUrl": match.source_url
      })


def run_reconciliation(settings: Settings,
                       event_slug: str,
                       liquipedia_page: str,
                       database_path: Path,
                       output_directory: Path,
                       cache_directory: Path,
                       refresh: bool = False,
                       offline: bool = False,
                       cache_write: bool = True) -> tuple[dict, Path, int]:
  started_at = time.monotonic()
  generated_at = datetime.now(timezone.utc)
  liquipedia_matches = []
  duckdb_matches = []
  warnings: list[dict] = []
  errors: list[dict] = []
  liquipedia_snapshot = None
  duckdb_snapshot = None
  liquipedia_status = "not-run"
  duckdb_status = "not-run"
  liquipedia_url = f"https://liquipedia.net/valorant/{quote(liquipedia_page, safe='/')}"
  duckdb_url = "https://vct-reference.com"

  try:
    duckdb_matches, duckdb_snapshot = VctReferenceProvider(
        database_path).fetch_matches(event_slug)
    duckdb_status = "read-only"
  except ImportRunError as error:
    duckdb_status = "failed"
    errors.append({"provider": "vct-reference", "message": str(error)})
    LOGGER.error(
        "provider=vct-reference stage=fetch event=%s match_identity=event elapsed_seconds=%.3f result=failed retry_count=0",
        event_slug,
        time.monotonic() - started_at)

  provider = LiquipediaProvider(settings,
                                cache_directory,
                                write_cache=cache_write)

  try:
    liquipedia_matches, liquipedia_snapshot = provider.fetch_matches(
        liquipedia_page, event_slug, refresh=refresh, offline=offline)
    liquipedia_status = liquipedia_snapshot.cache_status
  except ImportRunError as error:
    liquipedia_status = "failed"
    errors.append({"provider": "liquipedia", "message": str(error)})
    LOGGER.error(
        "provider=liquipedia stage=fetch event=%s match_identity=page elapsed_seconds=%.3f result=failed retry_count=0",
        event_slug,
        time.monotonic() - started_at)
  finally:
    provider.close()

  if liquipedia_snapshot is not None:
    warnings.extend({
        "provider": "liquipedia",
        "event": event_slug,
        "message": message,
        "sourceUrl": liquipedia_snapshot.source_url
    } for message in liquipedia_snapshot.warnings)

  _add_match_warnings(liquipedia_matches, warnings)
  _add_match_warnings(duckdb_matches, warnings)

  matches = reconcile_matches(liquipedia_matches,
                              duckdb_matches,
                              settings.match_time_tolerance_minutes,
                              liquipedia_available=liquipedia_status
                              != "failed",
                              duckdb_available=duckdb_status != "failed")
  counts = Counter(match["status"] for match in matches)
  schedule_counts = Counter(match.get("scheduleStatus") for match in matches)
  stats_counts = Counter(match.get("statsLinkStatus") for match in matches)
  summary = {
      "liquipediaMatches":
      len(liquipedia_matches),
      "duckdbMatches":
      len(duckdb_matches),
      "matched":
      counts.get("matched", 0),
      "liquipediaNewer":
      counts.get("liquipedia_newer", 0),
      "statsAvailable":
      stats_counts.get("available", 0),
      "incomplete":
      counts.get("incomplete", 0),
      "conflicts":
      counts.get("conflict", 0),
      "ambiguous":
      counts.get("ambiguous", 0),
      "unmatched":
      counts.get("unmatched", 0),
      "scheduleReady":
      schedule_counts.get("ready", 0),
      "scheduleWarning":
      schedule_counts.get("warning", 0),
      "scheduleBlocked":
      schedule_counts.get("blocked", 0),
      "statsMatched":
      stats_counts.get("matched", 0),
      "statsConflict":
      stats_counts.get("conflict", 0),
      "statsUnavailable":
      max(0,
          len(liquipedia_matches) - stats_counts.get("available", 0)),
      "statsAmbiguous":
      stats_counts.get("ambiguous", 0),
      "statsIncomplete":
      stats_counts.get("incomplete", 0),
      "statsUnmatched":
      stats_counts.get("unmatched", 0)
  }
  report = {
      "generatedAt": _iso_utc(generated_at),
      "event": {
          "slug": event_slug,
          "liquipediaPage": liquipedia_page
      },
      "sources": {
          "liquipedia":
          _source_summary(liquipedia_snapshot, "liquipedia", liquipedia_url,
                          liquipedia_status),
          "vctReference": {
              **_source_summary(duckdb_snapshot, "vct-reference", duckdb_url, duckdb_status), "databasePath":
              str(database_path),
              "readOnly":
              True
          }
      },
      "summary": summary,
      "matches": matches,
      "warnings": warnings,
      "errors": errors
  }
  report = ReconciliationReport.model_validate(report).model_dump(
      mode="json", by_alias=True)

  filename = f"reconciliation-{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  report_path = output_directory / filename
  write_json_atomic(report_path, report)
  elapsed_seconds = time.monotonic() - started_at
  LOGGER.info(
      "provider=reconciler stage=report event=%s match_identity=event elapsed_seconds=%.3f result=complete retry_count=0",
      event_slug, elapsed_seconds)
  exit_code = 2 if errors else (1 if summary["scheduleBlocked"] else 0)

  return report, report_path, exit_code
