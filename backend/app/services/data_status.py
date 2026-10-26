from datetime import datetime

from pymongo.database import Database

from app.api.responses import utc_datetime_string
from app.importers.import_status import parse_source_datetime

IMPORT_TYPES = ("schedule", "stats", "assets")
VALID_STATUSES = {"success", "partial", "failed"}
STATS_IMPORT_STATUSES = {
    "pending", "planning", "ready", "unchanged", "applied", "partial",
    "failed", "blocked"
}


def _import_info(import_status: dict, import_type: str) -> dict:
  imports = import_status.get("imports")
  imports = imports if isinstance(imports, dict) else {}
  detail = imports.get(import_type)
  detail = detail if isinstance(detail, dict) else {}
  status = detail.get("status")

  return {
      "lastSuccessfulAt":
      utc_datetime_string(detail.get("lastSuccessfulAt")),
      "status":
      status if isinstance(status, str) and status in VALID_STATUSES else None
  }


def get_data_status(database: Database, tournament_slug: str) -> dict:
  tournament = database["tournaments"].find_one({"slug": tournament_slug}, {
      "importStatus": 1,
      "_id": 0
  })
  import_status = tournament.get("importStatus") if isinstance(
      tournament, dict) else None
  import_status = import_status if isinstance(import_status, dict) else {}
  status = import_status.get("status")
  failure_codes = {
      "SOURCE_UNAVAILABLE", "STATS_SOURCE_FAILED", "PLAN_BLOCKED",
      "PREFLIGHT_BLOCKED", "WRITE_LIMIT_EXCEEDED", "APPLY_FAILED",
      "POST_APPLY_FAILED", "LOCK_BUSY", "INTERNAL_ERROR"
  }
  failure_code = import_status.get("lastFailureCode")
  last_run_status = import_status.get("lastRunStatus")
  failure_at = import_status.get("lastFailureAt")
  manual = import_status.get("manualUpdate")
  manual = manual if isinstance(manual, dict) else {}
  summary = manual.get("summary")
  summary = summary if isinstance(summary, dict) else {}
  imports = import_status.get("imports")
  imports = imports if isinstance(imports, dict) else {}
  stats = imports.get("stats")
  stats = stats if isinstance(stats, dict) else {}
  source_identity = stats.get("sourceIdentity")
  source_identity = source_identity if isinstance(source_identity,
                                                  dict) else {}
  required_identity_fields = ("duckdbSha256", "duckdbSchemaFingerprint",
                              "canonicalScheduleMappingDigest")
  document_counts = stats.get("statsDocumentCounts")
  document_counts = document_counts if isinstance(document_counts,
                                                  dict) else {}
  has_complete_stats_metadata = bool(
      stats.get("lastSuccessfulAt") and all(
          isinstance(source_identity.get(field), str)
          and len(source_identity[field]) == 64
          for field in required_identity_fields)
      and stats.get("statsPostflightStatus") == "passed"
      and stats.get("statsImportStatus") in {"applied", "unchanged"}
      and isinstance(stats.get("statsLinkedMatches"), int)
      and not isinstance(stats.get("statsLinkedMatches"), bool) and all(
          isinstance(document_counts.get(field), int)
          and not isinstance(document_counts.get(field), bool)
          for field in ("players", "matchMaps", "playerMapStats",
                        "teamMapStats")) and stats.get("status") == "success")
  raw_stats_import_status = stats.get("statsImportStatus")

  if raw_stats_import_status not in STATS_IMPORT_STATUSES:
    raw_stats_import_status = (
        "failed" if stats.get("status") == "failed" else
        "partial" if stats.get("status") == "partial" else "pending")

  latest_summary_status = summary.get("statsImportStatus")

  if latest_summary_status in STATS_IMPORT_STATUSES and manual.get(
      "status") in VALID_STATUSES:
    raw_stats_import_status = latest_summary_status

  raw_snapshot_status = summary.get("statsSnapshotStatus",
                                    stats.get("statsSnapshotStatus"))
  stats_snapshot_status = (raw_snapshot_status if raw_snapshot_status in {
      "updated", "unchanged", "unavailable", "invalid", "failed"
  } else None)
  linked_matches = summary.get("statsLinkedMatches")

  if not isinstance(linked_matches, int) or isinstance(
      linked_matches, bool) or linked_matches < 0:
    linked_matches = summary.get("statsAvailable")

  if not isinstance(linked_matches, int) or isinstance(
      linked_matches, bool) or linked_matches < 0:
    linked_matches = stats.get("statsLinkedMatches")

  linked_matches = (linked_matches if isinstance(linked_matches, int)
                    and not isinstance(linked_matches, bool)
                    and linked_matches >= 0 else None)
  pending_matches = summary.get("statsPendingMatches")

  if (not isinstance(pending_matches, int)
      or isinstance(pending_matches, bool) or pending_matches < 0):
    schedule_total = summary.get("scheduleTotal")
    pending_matches = (max(0, schedule_total -
                           linked_matches) if isinstance(schedule_total, int)
                       and linked_matches is not None else None)

  if (not isinstance(pending_matches, int)
      or isinstance(pending_matches, bool) or pending_matches < 0):
    pending_matches = None

  manual_status = manual.get("status")
  manual_finished_at = (parse_source_datetime(manual.get("finishedAt"))
                        if isinstance(manual.get("finishedAt"),
                                      (str, datetime)) else None)
  parsed_failure_at = parse_source_datetime(failure_at) if isinstance(
      failure_at, (str, datetime)) else None
  manual_supersedes_failure = bool(
      manual_status in VALID_STATUSES and manual_finished_at is not None and
      (parsed_failure_at is None or manual_finished_at >= parsed_failure_at))
  safe_failure_code = (failure_code if isinstance(failure_code, str)
                       and failure_code in failure_codes else None)
  latest_status = (manual_status if manual_status in VALID_STATUSES
                   and manual_finished_at is not None else status)
  latest_run_status = latest_status if latest_status in VALID_STATUSES else last_run_status

  return {
      "dataAsOf":
      utc_datetime_string(import_status.get("dataAsOf")),
      "lastImportedAt":
      utc_datetime_string(import_status.get("lastImportedAt")),
      "nextImportAt":
      None,
      "status":
      latest_status if isinstance(latest_status, str)
      and latest_status in VALID_STATUSES else None,
      "schedulerStatus":
      "disabled",
      "lastAttemptAt":
      utc_datetime_string(import_status.get("lastAttemptAt")),
      "lastSuccessfulAt":
      utc_datetime_string(import_status.get("lastSuccessfulAt")),
      "lastFailureAt":
      None if manual_supersedes_failure else utc_datetime_string(failure_at),
      "lastFailureCode":
      None if manual_supersedes_failure else safe_failure_code,
      "historicalFailure": {
          "at": utc_datetime_string(failure_at),
          "code": safe_failure_code
      } if failure_at is not None or safe_failure_code is not None else None,
      "lastRunStatus":
      latest_run_status if isinstance(latest_run_status, str)
      and latest_run_status in VALID_STATUSES else None,
      "imports": {
          name: _import_info(import_status, name)
          for name in IMPORT_TYPES
      },
      "statsSnapshotStatus":
      stats_snapshot_status,
      "statsImportStatus":
      raw_stats_import_status,
      "statsPlanRequired":
      (not has_complete_stats_metadata or raw_stats_import_status
       in {"pending", "planning", "ready", "partial", "failed", "blocked"}),
      "statsLinkedMatches":
      linked_matches,
      "statsPendingMatches":
      pending_matches
  }
