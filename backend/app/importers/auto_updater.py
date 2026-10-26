import json
import os
from threading import Event, Thread
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.utils.files import write_json_atomic
from app.importers.import_status import (parse_source_datetime,
                                         record_manual_update_phase,
                                         record_manual_update_status,
                                         record_stats_import_state,
                                         record_scheduler_state)
from app.importers.models import ImportRunError
from app.importers.process_lock import ImportLockBusy, ImportProcessLock
from app.importers.runner import run_reconciliation
from app.importers.providers.vct_snapshot import inspect_snapshot_file, refresh_snapshot, read_source_timestamp
from app.importers.stats_importer import (apply_stats_plan, build_stats_plan,
                                          preflight_stats_plan,
                                          record_stats_import_success,
                                          write_stats_plan_file)
from app.importers.write_plan import (apply_write_plan, build_write_plan,
                                      preflight_write_plan, write_plan_file)

ERROR_CODES = {
    "LOCK_BUSY":
    "Another import process is already running; this updater exited without connecting to MongoDB.",
    "SOURCE_UNAVAILABLE": "A required data source could not be read.",
    "STATS_SOURCE_FAILED":
    "The statistics source could not be refreshed; the previous snapshot was preserved.",
    "PLAN_BLOCKED": "The generated plan contains blocking validation errors.",
    "PREFLIGHT_BLOCKED":
    "Read-only preflight found a conflict or failed validation.",
    "WRITE_LIMIT_EXCEEDED":
    "The planned changes exceed the configured write limit.",
    "APPLY_FAILED": "The Apply operation did not complete successfully.",
    "POST_APPLY_FAILED":
    "Post-Apply verification did not confirm the applied data.",
    "INTERNAL_ERROR": "The updater stopped after an internal error."
}


def _iso_utc(value: datetime) -> str:
  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _new_report(mode: str) -> dict:
  started_at = datetime.now(timezone.utc)

  return {
      "runId": str(uuid.uuid4()),
      "mode": mode,
      "startedAt": _iso_utc(started_at),
      "finishedAt": None,
      "status": "running",
      "lock": {
          "acquired": False,
          "released": False
      },
      "scheduler": {
          "status": "disabled",
          "nextImportAt": None,
          "metadataWrites": 0
      },
      "stages": {},
      "mongodb": {
          "connected": False,
          "writes": 0,
          "applyStarted": False
      },
      "errors": [],
      "warnings": []
  }


def _finish_report(settings: Settings, report: dict, status: str) -> Path:
  finished_at = datetime.now(timezone.utc)
  report["finishedAt"] = _iso_utc(finished_at)
  report["status"] = status
  path = settings.report_directory / f"auto-update-run-{finished_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  report["reportPath"] = str(path.resolve())
  write_json_atomic(path, report, allow_nan=False)

  return path


def _add_error(report: dict, stage: str, code: str) -> None:
  report["errors"].append({
      "stage":
      stage,
      "code":
      code,
      "message":
      ERROR_CODES.get(code, ERROR_CODES["INTERNAL_ERROR"])
  })


def _prior_source_match_count(settings: Settings, import_status: dict | None,
                              identity_field: str,
                              summary_field: str) -> int | None:
  status_imports = (import_status or {}).get("imports")
  status_imports = status_imports if isinstance(status_imports, dict) else {}
  schedule = status_imports.get("schedule")
  schedule = schedule if isinstance(schedule, dict) else {}
  source_identity = schedule.get("sourceIdentity")
  source_identity = source_identity if isinstance(source_identity,
                                                  dict) else {}
  status_count = source_identity.get(identity_field)

  if isinstance(status_count, int) and status_count > 0:
    return status_count

  candidates = sorted(settings.report_directory.glob("reconciliation-*.json"),
                      reverse=True)

  for path in candidates:
    try:
      payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
      continue

    if (payload.get("event") or {}).get("slug") != settings.tournament_slug:
      continue

    if ((payload.get("sources") or {}).get("liquipedia")
        or {}).get("status") == "failed":
      continue

    count = (payload.get("summary") or {}).get(summary_field)

    if isinstance(count, int) and count > 0:
      return count

  return None


def _prior_liquipedia_match_count(settings: Settings,
                                  import_status: dict | None = None
                                  ) -> int | None:
  return _prior_source_match_count(settings, import_status,
                                   "liquipediaMatchCount", "liquipediaMatches")


def _prior_duckdb_match_count(settings: Settings,
                              import_status: dict | None = None) -> int | None:
  return _prior_source_match_count(settings, import_status, "duckdbMatchCount",
                                   "duckdbMatches")


def _read_import_status(settings: Settings) -> tuple[dict, bool]:
  from app.database import create_mongodb_client

  client = None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    tournament = client[settings.mongodb_database]["tournaments"].find_one(
        {"slug": settings.tournament_slug}, {
            "importStatus": 1,
            "_id": 0
        })
    status = tournament.get("importStatus") if isinstance(tournament,
                                                          dict) else {}

    return (status if isinstance(status, dict) else {}), True
  except Exception:
    return {}, False
  finally:
    if client is not None:
      client.close()


def _find_applied_plan(
    settings: Settings, import_status: dict,
    import_type: Literal["stats", "schedule"]) -> dict | None:
  digest = (import_status.get("planDigests") or {}).get(import_type)

  if not isinstance(digest, str) or not digest:
    return None

  pattern = "stats-import-plan-*.json" if import_type == "stats" else "import-plan-*.json"

  for path in sorted(settings.report_directory.glob(pattern), reverse=True):
    try:
      plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
      continue

    if isinstance(plan, dict) and plan.get("planDigest") == digest:
      return plan

  return None


def _last_applied_duckdb_hash(settings: Settings,
                              import_status: dict) -> str | None:
  imports = import_status.get("imports")
  imports = imports if isinstance(imports, dict) else {}
  stats = imports.get("stats")
  stats = stats if isinstance(stats, dict) else {}
  identity = stats.get("sourceIdentity")
  identity = identity if isinstance(identity, dict) else {}
  digest = identity.get("duckdbSha256")

  if isinstance(digest, str) and len(digest) == 64:
    return digest.casefold()

  plan = _find_applied_plan(settings, import_status, "stats")
  candidate = ((plan or {}).get("sourceDigests") or {}).get("duckdbSha256")

  return candidate.casefold() if isinstance(
      candidate, str) and len(candidate) == 64 else None


def _last_applied_stats_counts(settings: Settings,
                               import_status: dict) -> dict | None:
  plan = _find_applied_plan(settings, import_status, "stats")
  counts = (plan or {}).get("plannedCounts")

  if not isinstance(counts, dict):
    return None

  return {
      field: counts[field]
      for field in ("players", "matchMaps", "playerMapStats", "teamMapStats")
      if type(counts.get(field)) is int and counts[field] >= 0
  }


def _last_applied_schedule_match_count(settings: Settings,
                                       import_status: dict) -> int | None:
  imports = import_status.get("imports")
  imports = imports if isinstance(imports, dict) else {}
  schedule = imports.get("schedule")
  schedule = schedule if isinstance(schedule, dict) else {}
  identity = schedule.get("sourceIdentity")
  identity = identity if isinstance(identity, dict) else {}
  count = identity.get("verifiedMatchCount")

  if type(count) is int and count > 0:
    return count

  plan = _find_applied_plan(settings, import_status, "schedule")
  count = ((plan or {}).get("entityCounts") or {}).get("matchUpserts")

  return count if type(count) is int and count > 0 else None


def _source_counts(report: dict) -> dict:
  summary = report.get("summary") or {}

  return {
      "liquipediaMatches": summary.get("liquipediaMatches"),
      "duckdbMatches": summary.get("duckdbMatches"),
      "matched": summary.get("matched"),
      "duckdbStatsAvailable": summary.get("statsAvailable"),
      "conflicts": summary.get("conflicts"),
      "ambiguous": summary.get("ambiguous"),
      "incomplete": summary.get("incomplete"),
      "unmatched": summary.get("unmatched"),
      "statsUnavailable": summary.get("statsUnavailable"),
      "statsAmbiguous": summary.get("statsAmbiguous"),
      "statsIncomplete": summary.get("statsIncomplete"),
      "statsUnmatched": summary.get("statsUnmatched"),
      "participantsUnresolved": _unresolved_participant_count(report),
      "scheduleReady": summary.get("scheduleReady"),
      "scheduleWarning": summary.get("scheduleWarning"),
      "scheduleBlocked": summary.get("scheduleBlocked"),
      "statsMatched": summary.get("statsMatched"),
      "statsConflict": summary.get("statsConflict")
  }


def _unresolved_participant_count(report: dict) -> int:
  unresolved = 0
  placeholders = {"bye", "tba", "tbd", "unknown"}

  for match in report.get("matches", []):
    identity = match.get("liquipediaIdentity") or {}

    if not identity:
      continue

    teams = identity.get("teams") or []
    names = [team.get("name") for team in teams]

    if len(names) != 2 or any(not isinstance(name, str) or not name.strip()
                              or name.strip().casefold() in placeholders
                              for name in names):
      unresolved += 1

  return unresolved


def _manual_update_summary(report: dict) -> dict[str, int | str | bool | None]:
  schedule = (report.get("stages") or {}).get("schedule") or {}
  schedule_apply = schedule.get("apply") or {}
  stats = (report.get("stages") or {}).get("stats") or {}
  stats_source = (report.get("stages") or {}).get("statsSource") or {}
  stats_reconciliation = (report.get("stages")
                          or {}).get("statsReconciliation") or {}
  stats_apply = stats.get("apply") or {}
  stats_postflight = stats.get("postflight") or {}
  source = schedule.get("sourceCounts") or {}
  stats_coverage_source = ((stats_reconciliation.get("sourceCounts") or {})
                           if stats_reconciliation.get("status") == "ready"
                           else source)

  def count(value) -> int:
    return value if isinstance(
        value, int) and not isinstance(value, bool) and value >= 0 else 0

  schedule_total = count(source.get("liquipediaMatches"))
  stats_available = count(stats_coverage_source.get("duckdbStatsAvailable"))
  schedule_created = count((schedule_apply.get("matchCounts")
                            or {}).get("created"))
  schedule_updated = count((schedule_apply.get("matchCounts")
                            or {}).get("updated"))
  schedule_unchanged = count((schedule_apply.get("matchCounts")
                              or {}).get("unchanged"))
  stats_import_status = stats.get("statsImportStatus")

  if stats_import_status not in {
      "pending", "planning", "ready", "unchanged", "applied", "partial",
      "failed", "blocked"
  }:
    stats_import_status = None

  linked_matches = stats.get("statsLinkedMatches")

  if not isinstance(linked_matches, int) or isinstance(
      linked_matches, bool) or linked_matches < 0:
    linked_matches = count(stats_coverage_source.get("duckdbStatsAvailable"))

  stats_pending_matches = stats.get("statsPendingMatches")

  if (not isinstance(stats_pending_matches, int)
      or isinstance(stats_pending_matches, bool) or stats_pending_matches < 0):
    stats_pending_matches = max(0, schedule_total - linked_matches)

  return {
      "scheduleTotal":
      schedule_total,
      "scheduleSynced":
      schedule_created + schedule_updated + schedule_unchanged,
      "statsAvailable":
      stats_available,
      "statsUnavailable":
      max(0, schedule_total - stats_available),
      "scheduleCreated":
      schedule_created,
      "scheduleUpdated":
      schedule_updated,
      "scheduleUnchanged":
      schedule_unchanged,
      "statsUpdated":
      count(stats_apply.get("created")) + count(stats_apply.get("updated")),
      "statsCreated":
      count(stats_apply.get("created")),
      "statsUnchanged":
      count(
          stats_apply.get(
              "unchanged",
              stats_postflight.get("plannedUnchanged",
                                   (stats.get("preflight")
                                    or {}).get("plannedUnchanged")))),
      "statsSkipped":
      count(stats_apply.get("skipped", stats.get("skippedCount", 0))),
      "statsSnapshotStatus":
      stats_source.get("status") if stats_source.get("status")
      in {"updated", "unchanged", "failed"} else None,
      "statsImportStatus":
      stats_import_status,
      "statsPlanRequired":
      stats.get("statsPlanRequired") if isinstance(
          stats.get("statsPlanRequired"), bool) else None,
      "statsLinkedMatches":
      linked_matches,
      "statsPendingMatches":
      stats_pending_matches,
      "conflicts":
      count(source.get("conflicts")),
      "ambiguous":
      count(source.get("ambiguous")),
      "incomplete":
      count(source.get("incomplete")),
      "unmatched":
      count(source.get("unmatched")),
      "participantsUnresolved":
      count(source.get("participantsUnresolved"))
  }


def _check_schedule_source(report: dict, prior_liquipedia_count: int | None,
                           prior_duckdb_count: int | None,
                           settings: Settings) -> str | None:
  summary = report.get("summary") or {}
  liquipedia_errors = [
      error for error in report.get("errors", [])
      if error.get("provider") == "liquipedia"
  ]

  if liquipedia_errors:
    return "SOURCE_UNAVAILABLE"

  current_count = summary.get("liquipediaMatches")

  if not isinstance(current_count, int) or current_count <= 0:
    return "SOURCE_UNAVAILABLE"

  if prior_liquipedia_count is not None:
    drop = (prior_liquipedia_count -
            current_count) / prior_liquipedia_count * 100

    if drop > settings.auto_update_max_source_decrease_percent:
      return "SOURCE_UNAVAILABLE"

  return None


def _estimate_write_units(preflight: dict) -> int | None:
  value = preflight.get("estimatedWriteUnits")

  if isinstance(value, int) and value >= 0:
    return value

  create = preflight.get("plannedCreate")
  update = preflight.get("plannedUpdate")

  if isinstance(create, int) and isinstance(update, int):
    return create + update + 1

  return None


def _schedule_gate(plan: dict, preflight: dict,
                   settings: Settings) -> str | None:
  if plan.get("eventSlug") != settings.tournament_slug or plan.get(
      "blockingErrors"):
    return "PLAN_BLOCKED"

  operations = [
      *plan.get("teamOperations", []), *plan.get("matchOperations", [])
  ]

  if any(
      item.get("operation") not in {"upsert", "skip"} for item in operations):
    return "PLAN_BLOCKED"

  if any(
      item.get("operation") == "upsert"
      and item.get("scheduleStatus") not in {"ready", "warning"}
      for item in plan.get("matchOperations", [])):
    return "PLAN_BLOCKED"

  if (preflight.get("mongodbConnected") is not True
      or preflight.get("mongodbWrites") != 0
      or preflight.get("planDigest") != plan.get("planDigest")
      or Path(preflight.get("writePlanPath", "")).resolve() != Path(
          plan.get("writePlanPath", "")).resolve() or preflight.get("errors")
      or preflight.get("indexConflicts") or preflight.get("duplicateKeyRisks")
      or preflight.get("readyForApply") is not True
      or any(not risk.get("externalMatchId")
             for risk in preflight.get("canonicalReferenceRisks", []))):
    return "PREFLIGHT_BLOCKED"

  estimated = _estimate_write_units(preflight)

  if estimated is None or estimated > settings.auto_update_max_schedule_writes:
    return "WRITE_LIMIT_EXCEEDED"

  return None


def _stats_gate(plan: dict, preflight: dict, settings: Settings) -> str | None:
  if plan.get("eventSlug") != settings.tournament_slug or plan.get(
      "blockingErrors"):
    return "PLAN_BLOCKED"

  if (preflight.get("mongodbConnected") is not True
      or preflight.get("mongodbWrites") != 0
      or preflight.get("planDigest") != plan.get("planDigest")
      or Path(preflight.get("statsWritePlanPath", "")).resolve() != Path(
          plan.get("writePlanPath", "")).resolve() or preflight.get("errors")
      or preflight.get("indexConflicts") or preflight.get("duplicateKeyRisks")
      or preflight.get("mapIdentityConflicts")
      or preflight.get("missingCanonicalReferences")):
    return "PREFLIGHT_BLOCKED"

  estimated = _estimate_write_units(preflight)

  if estimated is None or estimated > settings.auto_update_max_stats_writes:
    return "WRITE_LIMIT_EXCEEDED"

  return None


def _stats_metadata_is_current(import_status: dict, plan: dict,
                               preflight: dict,
                               schedule_changed: bool) -> bool:
  if schedule_changed:
    return False

  manual = import_status.get("manualUpdate")
  manual = manual if isinstance(manual, dict) else {}
  prior_summary = manual.get("summary")
  prior_summary = prior_summary if isinstance(prior_summary, dict) else {}

  if prior_summary.get("statsImportStatus") in {
      "pending", "planning", "ready", "partial", "failed", "blocked"
  }:
    return False

  imports = import_status.get("imports")
  imports = imports if isinstance(imports, dict) else {}
  stats = imports.get("stats")
  stats = stats if isinstance(stats, dict) else {}
  source_identity = stats.get("sourceIdentity")
  source_identity = source_identity if isinstance(source_identity,
                                                  dict) else {}
  source_digests = plan.get("sourceDigests") or {}
  planned_counts = plan.get("plannedCounts") or {}
  saved_counts = stats.get("statsDocumentCounts")
  saved_counts = saved_counts if isinstance(saved_counts, dict) else {}
  required_hashes = ("duckdbSha256", "duckdbSchemaFingerprint",
                     "canonicalScheduleMappingDigest")

  if any(
      source_identity.get(field) != source_digests.get(field)
      for field in required_hashes):
    return False

  if (not stats.get("lastSuccessfulAt") or stats.get("status") != "success"
      or stats.get("statsImportStatus") not in {"applied", "unchanged"}
      or stats.get("statsPostflightStatus") != "passed"
      or stats.get("statsLinkedMatches")
      != planned_counts.get("verifiedMatches")):
    return False

  for field in ("players", "matchMaps", "playerMapStats", "teamMapStats"):
    if saved_counts.get(field) != planned_counts.get(field):
      return False

  by_collection = preflight.get("byCollection")
  by_collection = by_collection if isinstance(by_collection, dict) else {}

  return (preflight.get("plannedCreate") == 0
          and preflight.get("plannedUpdate") == 0 and all(
              isinstance(collection_summary := by_collection.get(name), dict)
              and collection_summary.get("create") == 0
              and collection_summary.get("update") == 0 and
              collection_summary.get("unchanged") == planned_counts.get(field)
              for name, field in (("players", "players"),
                                  ("match_maps", "matchMaps"),
                                  ("player_map_stats", "playerMapStats"),
                                  ("team_map_stats", "teamMapStats"))))


def _record_apply_result(report: dict,
                         result: dict,
                         connected_key: str = "mongodbConnected") -> None:
  report["mongodb"]["connected"] = report["mongodb"][
      "connected"] or result.get(connected_key) is True
  report["mongodb"]["writes"] += int(
      result.get("writes", result.get("mongodbWrites", 0)) or 0)
  report["mongodb"]["applyStarted"] = report["mongodb"]["applyStarted"] or int(
      result.get("writes", result.get("mongodbWrites", 0)) or 0) > 0


def _post_schedule_ok(report: dict, plan: dict) -> bool:
  return (report.get("mongodbConnected") is True
          and report.get("mongodbWrites") == 0
          and report.get("planDigest") == plan.get("planDigest")
          and report.get("plannedCreate") == 0
          and report.get("plannedUpdate") == 0 and not report.get("errors")
          and not report.get("indexConflicts")
          and not report.get("duplicateKeyRisks")
          and not any(not risk.get("externalMatchId")
                      for risk in report.get("canonicalReferenceRisks", [])))


def _post_stats_ok(report: dict, plan: dict) -> bool:
  expected_matches = (plan.get("plannedCounts") or {}).get("verifiedMatches")
  expected_counts = plan.get("plannedCounts") or {}
  collection_counts = report.get("byCollection")
  collection_counts = collection_counts if isinstance(collection_counts,
                                                      dict) else {}
  all_documents_unchanged = all(
      isinstance(collection_summary := collection_counts.get(collection), dict)
      and collection_summary.get("create") == 0
      and collection_summary.get("update") == 0
      and collection_summary.get("unchanged") == expected_counts.get(field)
      for collection, field in (("players", "players"), ("match_maps",
                                                         "matchMaps"),
                                ("player_map_stats", "playerMapStats"),
                                ("team_map_stats", "teamMapStats")))

  return (report.get("mongodbConnected") is True
          and report.get("mongodbWrites") == 0
          and report.get("planDigest") == plan.get("planDigest")
          and report.get("plannedCreate") == 0
          and report.get("plannedUpdate") == 0
          and report.get("completedCanonicalMatches") == expected_matches
          and all_documents_unchanged and not report.get("errors")
          and not report.get("indexConflicts")
          and not report.get("duplicateKeyRisks")
          and not report.get("mapIdentityConflicts")
          and not report.get("missingCanonicalReferences"))


def _record_scheduler_metadata(settings: Settings,
                               status: str,
                               next_import_at: datetime | None,
                               last_attempt_at: datetime | None = None,
                               last_successful_at: datetime | None = None,
                               last_failure_at: datetime | None = None,
                               last_failure_code: str | None = None,
                               last_run_status: str | None = None,
                               heartbeat_at: datetime | None = None) -> bool:
  from app.database import create_mongodb_client

  client = None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    record_scheduler_state(client[settings.mongodb_database],
                           settings.tournament_slug, status, next_import_at,
                           last_attempt_at, last_successful_at,
                           last_failure_at, last_failure_code, last_run_status,
                           heartbeat_at)

    return True
  except Exception:
    return False
  finally:
    if client is not None:
      client.close()


def _record_manual_update_result(settings: Settings, job_id: str, status: str,
                                 report: dict) -> None:
  from app.database import create_mongodb_client

  client = None
  failure_message = "資料更新未完成，請稍後重試。" if status == "failed" else None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    record_manual_update_status(client[settings.mongodb_database],
                                settings.tournament_slug, job_id, status,
                                datetime.now(timezone.utc), failure_message,
                                _manual_update_summary(report))
  except Exception:
    return
  finally:
    if client is not None:
      client.close()


def _manual_update_job_id() -> str | None:
  value = os.environ.get("VCT_ANALYTICS_MANUAL_UPDATE_JOB_ID", "")

  try:
    return str(uuid.UUID(value))
  except (ValueError, AttributeError):
    return None


def _record_manual_update_phase(settings: Settings,
                                phase: str,
                                enabled: bool = True) -> None:
  if not enabled:
    return

  job_id = _manual_update_job_id()

  if job_id is None:
    return

  from app.database import create_mongodb_client

  client = None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    record_manual_update_phase(client[settings.mongodb_database],
                               settings.tournament_slug, job_id, phase)
  except Exception:
    return
  finally:
    if client is not None:
      client.close()


def _run_schedule(
    settings: Settings, report: dict, dry_run: bool,
    previous_liquipedia_count: int | None, previous_duckdb_count: int | None,
    previous_verified_count: int | None
) -> tuple[dict | None, dict | None, Path | None]:
  stage = {
      "status": "failed",
      "applied": False,
      "counts": None,
      "paths": {},
      "errorCode": None
  }
  reconciliation, reconciliation_path, _ = run_reconciliation(
      settings=settings,
      event_slug=settings.tournament_slug,
      liquipedia_page=settings.liquipedia_page_title,
      database_path=settings.reference_database_path,
      output_directory=settings.report_directory,
      cache_directory=settings.cache_directory,
      refresh=True,
      offline=False,
      cache_write=not dry_run)

  _record_manual_update_phase(settings, "reconciling", enabled=not dry_run)
  stage["paths"]["reconciliationReport"] = str(reconciliation_path.resolve())
  stage["sourceCounts"] = _source_counts(reconciliation)
  stage["previousLiquipediaMatchCount"] = previous_liquipedia_count
  stage["previousDuckDBMatchCount"] = previous_duckdb_count
  source_error = _check_schedule_source(reconciliation,
                                        previous_liquipedia_count,
                                        previous_duckdb_count, settings)

  if source_error:
    stage["errorCode"] = source_error
    _add_error(report, "schedule", source_error)
    report["stages"]["schedule"] = stage

    return None, None, None

  plan = build_write_plan(settings, reconciliation, reconciliation_path,
                          settings.reference_database_path)
  plan_path = write_plan_file(plan, settings.report_directory)
  report["warnings"].extend(plan.get("warnings") or [])
  stage["paths"]["writePlan"] = str(plan_path.resolve())
  stage["planDigest"] = plan.get("planDigest")
  stage["planCounts"] = plan.get("entityCounts")
  current_verified_count = (plan.get("entityCounts") or {}).get("matchUpserts")
  stage["previousVerifiedMatchCount"] = previous_verified_count

  if not isinstance(current_verified_count,
                    int) or current_verified_count <= 0:
    stage["errorCode"] = "SOURCE_UNAVAILABLE"
    _add_error(report, "schedule", "SOURCE_UNAVAILABLE")
    report["stages"]["schedule"] = stage

    return plan, None, plan_path

  if plan.get("blockingErrors"):
    stage["errorCode"] = "PLAN_BLOCKED"
    _add_error(report, "schedule", "PLAN_BLOCKED")
    report["stages"]["schedule"] = stage

    return plan, None, plan_path

  preflight, preflight_path = preflight_write_plan(plan, settings, plan_path)
  report["mongodb"]["connected"] = report["mongodb"][
      "connected"] or preflight.get("mongodbConnected") is True
  stage["paths"]["preflight"] = str(preflight_path.resolve())
  stage["preflight"] = {
      "plannedCreate":
      preflight.get("plannedCreate"),
      "plannedUpdate":
      preflight.get("plannedUpdate"),
      "plannedUnchanged":
      preflight.get("plannedUnchanged"),
      "plannedMatchCounts":
      preflight.get("plannedMatchCounts"),
      "estimatedWriteUnits":
      preflight.get("estimatedWriteUnits"),
      "mongodbConnected":
      preflight.get("mongodbConnected"),
      "mongodbWrites":
      preflight.get("mongodbWrites"),
      "indexConflicts":
      len(preflight.get("indexConflicts", [])),
      "duplicateKeyRisks":
      len(preflight.get("duplicateKeyRisks", [])),
      "completedMatchDowngradeRisks":
      len(preflight.get("completedMatchDowngradeRisks", [])),
      "canonicalReferenceRisks":
      len(preflight.get("canonicalReferenceRisks", [])),
      "matchOperationRisks":
      len(preflight.get("matchOperationRisks", [])),
      "readyForApply":
      preflight.get("readyForApply"),
      "errors":
      len(preflight.get("errors", [])),
      "warnings":
      preflight.get("warnings", []),
      "warningCount":
      preflight.get("warningCount", 0)
  }
  gate_error = _schedule_gate(plan, preflight, settings)

  if gate_error:
    stage["status"] = "failed"
    stage["errorCode"] = gate_error
    _add_error(report, "schedule", gate_error)
    report["stages"]["schedule"] = stage

    return plan, preflight, plan_path

  if dry_run:
    stage["status"] = "partial" if preflight.get("skipped", 0) else "success"
    stage["readyForApply"] = True
    report["stages"]["schedule"] = stage

    return plan, preflight, plan_path

  _record_manual_update_phase(settings, "applying_schedule")
  report["mongodb"]["applyStarted"] = True
  applied = apply_write_plan(plan, settings, plan_path,
                             settings.auto_update_max_schedule_writes)
  _record_apply_result(report, applied, "connected")
  stage["apply"] = {
      "created":
      applied.get("created", 0),
      "updated":
      applied.get("updated", 0),
      "unchanged":
      applied.get("unchanged", 0),
      "matchCounts":
      applied.get("matchCounts", {
          "created": 0,
          "updated": 0,
          "unchanged": 0
      }),
      "skipped":
      applied.get("skipped", 0),
      "failed":
      applied.get("failed", 0),
      "writes":
      applied.get("writes", 0),
      "importStatus":
      applied.get("importStatus")
  }

  if (applied.get("errors") or applied.get("failed")
      or applied.get("writes", 0) > settings.auto_update_max_schedule_writes):
    stage["status"] = "failed"
    stage["errorCode"] = "APPLY_FAILED"
    _add_error(report, "schedule", "APPLY_FAILED")
    report["stages"]["schedule"] = stage

    return plan, preflight, plan_path

  postflight, postflight_path = preflight_write_plan(plan, settings, plan_path)
  stage["paths"]["postApplyPreflight"] = str(postflight_path.resolve())
  stage["postApplyVerified"] = _post_schedule_ok(postflight, plan)

  if not stage["postApplyVerified"]:
    stage["status"] = "failed"
    stage["errorCode"] = "POST_APPLY_FAILED"
    _add_error(report, "schedule", "POST_APPLY_FAILED")
  else:
    stage["status"] = "partial" if applied.get("skipped", 0) else "success"

  report["stages"]["schedule"] = stage

  return plan, postflight, plan_path


def _run_stats(settings: Settings,
               report: dict,
               dry_run: bool,
               import_status: dict,
               reconciliation_report_path: Path | None = None) -> dict:
  stage = {
      "status": "planning",
      "statsImportStatus": "planning",
      "statsPlanRequired": True,
      "applied": False,
      "planPath": None,
      "preflightPath": None,
      "errorCode": None
  }

  _record_manual_update_phase(settings, "verifying_stats", enabled=not dry_run)

  try:
    snapshot_identity = inspect_snapshot_file(settings.reference_database_path)
    current_hash = snapshot_identity["sha256"]
    current_timestamp = read_source_timestamp(settings.reference_database_path)

    if current_timestamp is not None:
      current_timestamp = current_timestamp.replace(
          microsecond=(current_timestamp.microsecond // 1000) * 1000)
  except (ImportRunError, OSError):
    stage.update({
        "status": "blocked",
        "statsImportStatus": "blocked",
        "errorCode": "SOURCE_UNAVAILABLE"
    })
    _add_error(report, "stats", "SOURCE_UNAVAILABLE")
    report["stages"]["stats"] = stage

    return stage

  last_hash = _last_applied_duckdb_hash(settings, import_status)
  stats_import = (import_status.get("imports") or {}).get("stats")
  stats_import = stats_import if isinstance(stats_import, dict) else {}
  last_timestamp = parse_source_datetime(stats_import.get("dataAsOf"))

  if last_timestamp is not None:
    last_timestamp = last_timestamp.replace(
        microsecond=(last_timestamp.microsecond // 1000) * 1000)

  timestamp_changed = ((last_timestamp is not None
                        or current_timestamp is not None)
                       and last_timestamp != current_timestamp)
  hash_changed = last_hash is None or last_hash != current_hash
  stage["duckdbChanged"] = hash_changed or timestamp_changed
  stage["duckdbHashChanged"] = hash_changed
  stage["duckdbSourceTimestampChanged"] = timestamp_changed
  stage["duckdbSourceTimestamp"] = _iso_utc(
      current_timestamp) if current_timestamp else None
  stage["duckdbSha256"] = current_hash
  stage["duckdbSchemaFingerprint"] = snapshot_identity.get("schemaFingerprint")
  stage["snapshotStatus"] = (report.get("stages", {}).get("statsSource")
                             or {}).get("status")

  try:
    plan = build_stats_plan(
        settings, reconciliation_report_path=reconciliation_report_path)
    plan_path = write_stats_plan_file(plan, settings.report_directory)
  except ImportRunError as error:
    stage.update({
        "status": "blocked",
        "statsImportStatus": "blocked",
        "errorCode": "PLAN_BLOCKED"
    })

    if "No successful Schedule Apply confirmation" in str(error):
      stage["reason"] = "no-valid-applied-schedule-plan-and-preflight"

    _add_error(report, "stats", "PLAN_BLOCKED")
    report["stages"]["stats"] = stage

    return stage
  except (OSError, ValueError):
    stage.update({
        "status": "blocked",
        "statsImportStatus": "blocked",
        "errorCode": "PLAN_BLOCKED"
    })
    _add_error(report, "stats", "PLAN_BLOCKED")
    report["stages"]["stats"] = stage

    return stage

  stage["planPath"] = str(plan_path.resolve())
  stage["planDigest"] = plan.get("planDigest")
  stage["counts"] = plan.get("plannedCounts")
  previous_counts = _last_applied_stats_counts(settings, import_status)

  if previous_counts is not None:
    current_counts = plan.get("plannedCounts") or {}
    decreases = {}

    for field, previous in previous_counts.items():
      current = current_counts.get(field)

      if not isinstance(current, int) or current < 0:
        decreases[field] = {"previous": previous, "current": current}
      elif (previous > 0 and (previous - current) / previous * 100
            > settings.auto_update_max_source_decrease_percent):
        decreases[field] = {"previous": previous, "current": current}

    stage["previousCounts"] = previous_counts

    if decreases:
      stage["sourceCountDecrease"] = decreases
      stage.update({
          "status": "blocked",
          "statsImportStatus": "blocked",
          "errorCode": "SOURCE_UNAVAILABLE"
      })
      _add_error(report, "stats", "SOURCE_UNAVAILABLE")
      report["stages"]["stats"] = stage

      return stage

  if plan.get("blockingErrors"):
    stage.update({
        "status": "blocked",
        "statsImportStatus": "blocked",
        "errorCode": "PLAN_BLOCKED"
    })
    _add_error(report, "stats", "PLAN_BLOCKED")
    report["stages"]["stats"] = stage

    return stage

  preflight, preflight_path = preflight_stats_plan(plan, settings, plan_path)
  report["mongodb"]["connected"] = report["mongodb"][
      "connected"] or preflight.get("mongodbConnected") is True
  stage["preflightPath"] = str(preflight_path.resolve())
  stage["preflight"] = {
      "plannedCreate":
      preflight.get("plannedCreate"),
      "plannedUpdate":
      preflight.get("plannedUpdate"),
      "plannedUnchanged":
      preflight.get("plannedUnchanged"),
      "estimatedWriteUnits":
      preflight.get("estimatedWriteUnits"),
      "mongodbConnected":
      preflight.get("mongodbConnected"),
      "mongodbWrites":
      preflight.get("mongodbWrites"),
      "indexConflicts":
      len(preflight.get("indexConflicts", [])),
      "duplicateKeyRisks":
      len(preflight.get("duplicateKeyRisks", [])),
      "mapIdentityConflicts":
      len(preflight.get("mapIdentityConflicts", [])),
      "missingCanonicalReferences":
      len(preflight.get("missingCanonicalReferences", [])),
      "completedCanonicalMatches":
      preflight.get("completedCanonicalMatches"),
      "errors":
      len(preflight.get("errors", []))
  }
  schedule_source_counts = (report.get("stages", {}).get("schedule")
                            or {}).get("sourceCounts") or {}
  schedule_total = schedule_source_counts.get("liquipediaMatches")
  linked_matches = plan.get("plannedCounts", {}).get("verifiedMatches")
  stage["statsLinkedMatches"] = linked_matches if isinstance(
      linked_matches, int) and linked_matches >= 0 else 0
  stage["statsPendingMatches"] = (max(
      0, schedule_total -
      stage["statsLinkedMatches"]) if isinstance(schedule_total, int)
                                  and not isinstance(schedule_total, bool)
                                  and schedule_total >= 0 else None)
  gate_error = _stats_gate(plan, preflight, settings)

  if gate_error:
    stage.update({
        "status": "blocked",
        "statsImportStatus": "blocked",
        "errorCode": gate_error
    })
    _add_error(report, "stats", gate_error)
    report["stages"]["stats"] = stage

    return stage

  schedule_preflight = (report.get("stages", {}).get("schedule")
                        or {}).get("preflight") or {}
  schedule_changed = bool(
      schedule_preflight.get("plannedCreate")
      or schedule_preflight.get("plannedUpdate")
      or schedule_preflight.get("matchOperationRisks"))
  already_current = _stats_metadata_is_current(import_status, plan, preflight,
                                               schedule_changed)

  if dry_run:
    stage.update({
        "status": "unchanged" if already_current else "ready",
        "statsImportStatus": "unchanged" if already_current else "ready",
        "statsPlanRequired": not already_current,
        "readyForApply": True,
        "alreadySynchronized": already_current
    })
    report["stages"]["stats"] = stage

    return stage

  def record_verified_success(postflight: dict, import_result: str) -> bool:
    recorded = record_stats_import_success(plan, settings, postflight,
                                           import_result)
    report["mongodb"]["connected"] = report["mongodb"][
        "connected"] or recorded.get("mongodbConnected") is True

    report["mongodb"]["writes"] += recorded.get("mongodbWrites", 0)
    stage["importStatusRecorded"] = recorded.get("recorded") is True

    return recorded.get("recorded") is True

  if preflight.get("plannedCreate") == 0 and preflight.get(
      "plannedUpdate") == 0:
    postflight, postflight_path = preflight_stats_plan(plan, settings,
                                                       plan_path)
    report["mongodb"]["connected"] = report["mongodb"][
        "connected"] or postflight.get("mongodbConnected") is True
    stage["postApplyPreflightPath"] = str(postflight_path.resolve())
    stage["postApplyVerified"] = _post_stats_ok(postflight, plan)

    if not stage["postApplyVerified"]:
      stage.update({
          "status": "failed",
          "statsImportStatus": "failed",
          "errorCode": "POST_APPLY_FAILED"
      })
      _add_error(report, "stats", "POST_APPLY_FAILED")
    elif not record_verified_success(
        postflight, "partial" if plan.get("plannedCounts", {}).get(
            "skippedMatches", 0) else "unchanged"):
      stage.update({
          "status": "failed",
          "statsImportStatus": "failed",
          "errorCode": "POST_APPLY_FAILED"
      })
      _add_error(report, "stats", "POST_APPLY_FAILED")
    else:
      stage.update({
          "status":
          "unchanged",
          "statsImportStatus":
          "partial" if plan.get("plannedCounts", {}).get("skippedMatches", 0)
          else "unchanged",
          "statsPlanRequired":
          False,
          "applied":
          False,
          "reason":
          "all-stats-documents-unchanged-import-status-verified",
          "postflight": {
              "plannedCreate": postflight.get("plannedCreate"),
              "plannedUpdate": postflight.get("plannedUpdate"),
              "plannedUnchanged": postflight.get("plannedUnchanged"),
              "mongodbConnected": postflight.get("mongodbConnected"),
              "mongodbWrites": postflight.get("mongodbWrites"),
              "errors": len(postflight.get("errors", []))
          }
      })

    report["stages"]["stats"] = stage

    return stage

  report["mongodb"]["applyStarted"] = True
  applied = apply_stats_plan(plan, settings, plan_path,
                             settings.auto_update_max_stats_writes)
  _record_apply_result(report, applied)
  stage["apply"] = {
      "created": applied.get("created", 0),
      "updated": applied.get("updated", 0),
      "unchanged": applied.get("unchanged", 0),
      "skipped": applied.get("skipped", 0),
      "failed": applied.get("failed", 0),
      "writes": applied.get("mongodbWrites", 0),
      "importStatus": applied.get("importStatus")
  }

  if (applied.get("errors") or applied.get("failed") or applied.get(
      "mongodbWrites", 0) > settings.auto_update_max_stats_writes):
    stage.update({
        "status": "failed",
        "statsImportStatus": "failed",
        "errorCode": "APPLY_FAILED"
    })
    _add_error(report, "stats", "APPLY_FAILED")
    report["stages"]["stats"] = stage

    return stage

  postflight, postflight_path = preflight_stats_plan(plan, settings, plan_path)
  stage["postApplyPreflightPath"] = str(postflight_path.resolve())
  stage["postApplyVerified"] = _post_stats_ok(postflight, plan)

  if not stage["postApplyVerified"]:
    stage.update({
        "status": "failed",
        "statsImportStatus": "failed",
        "errorCode": "POST_APPLY_FAILED"
    })
    _add_error(report, "stats", "POST_APPLY_FAILED")
  elif not record_verified_success(
      postflight, "partial" if applied.get("skipped", 0) else "applied"):
    stage.update({
        "status": "failed",
        "statsImportStatus": "failed",
        "errorCode": "POST_APPLY_FAILED"
    })
    _add_error(report, "stats", "POST_APPLY_FAILED")
  else:
    stage["status"] = "partial" if applied.get("skipped", 0) else "success"
    stage["statsImportStatus"] = "partial" if applied.get("skipped",
                                                          0) else "applied"
    stage["statsPlanRequired"] = False

  report["stages"]["stats"] = stage

  return stage


def _run_stats_reconciliation(settings: Settings, report: dict,
                              dry_run: bool) -> Path | None:
  _record_manual_update_phase(settings, "reconciling", enabled=not dry_run)

  try:
    reconciliation, reconciliation_path, _ = run_reconciliation(
        settings=settings,
        event_slug=settings.tournament_slug,
        liquipedia_page=settings.liquipedia_page_title,
        database_path=settings.reference_database_path,
        output_directory=settings.report_directory,
        cache_directory=settings.cache_directory,
        refresh=False,
        offline=True,
        cache_write=False)
  except Exception:
    reconciliation = None
    reconciliation_path = None

  if not isinstance(reconciliation, dict) or reconciliation_path is None:
    report["stages"]["statsReconciliation"] = {
        "status": "failed",
        "errorCode": "SOURCE_UNAVAILABLE"
    }
    _add_error(report, "statsReconciliation", "SOURCE_UNAVAILABLE")

    return None

  sources = reconciliation.get("sources") or {}
  lp_status = (sources.get("liquipedia") or {}).get("status")
  duckdb_status = (sources.get("vctReference") or {}).get("status")
  summary = reconciliation.get("summary") or {}

  if ((reconciliation.get("event")
       or {}).get("slug") != settings.tournament_slug or lp_status == "failed"
      or duckdb_status == "failed"
      or not isinstance(summary.get("liquipediaMatches"), int)
      or summary.get("liquipediaMatches", 0) <= 0):
    report["stages"]["statsReconciliation"] = {
        "status": "failed",
        "errorCode": "SOURCE_UNAVAILABLE",
        "sourceCounts": _source_counts(reconciliation)
    }

    _add_error(report, "statsReconciliation", "SOURCE_UNAVAILABLE")

    return None

  report["stages"]["statsReconciliation"] = {
      "status": "ready",
      "reportPath": str(reconciliation_path.resolve()),
      "sourceCounts": _source_counts(reconciliation),
      "readOnly": (sources.get("vctReference") or {}).get("readOnly") is True
  }

  return reconciliation_path


def _run_stats_source(settings: Settings, report: dict, dry_run: bool) -> bool:
  stage = {"status": "failed", "changed": False, "replaced": False}

  try:
    snapshot = refresh_snapshot(
        settings,
        dry_run=dry_run,
        force_download=dry_run,
        on_phase=lambda phase: _record_manual_update_phase(
            settings, phase, enabled=not dry_run))
  except Exception:
    stage["status"] = "failed"
    stage["snapshotStatus"] = "failed"
    stage["errorCode"] = "STATS_SOURCE_FAILED"
    _record_manual_update_phase(settings,
                                "stats_source_failed",
                                enabled=not dry_run)
    _add_error(report, "statsSource", "STATS_SOURCE_FAILED")
    report["stages"]["statsSource"] = stage

    return False

  normalized_status = "updated" if snapshot.status == "update-available" else snapshot.status
  stage.update({
      "status":
      normalized_status,
      "snapshotStatus":
      normalized_status,
      "changed":
      snapshot.changed,
      "replaced":
      snapshot.replaced,
      "sha256":
      snapshot.content_sha256,
      "sizeBytes":
      snapshot.size_bytes,
      "sourceTimestamp":
      _iso_utc(snapshot.source_timestamp)
      if snapshot.source_timestamp else None,
      "etag":
      snapshot.etag,
      "lastModified":
      snapshot.last_modified,
      "schemaFingerprint":
      snapshot.schema_fingerprint,
      "tableCounts":
      snapshot.table_counts,
      "backupName":
      snapshot.backup_name,
      "response": {
          "headStatus": snapshot.head_status,
          "contentType": snapshot.content_type,
          "contentLength": snapshot.content_length,
          "redirectHosts": list(snapshot.redirect_hosts)
      }
  })

  if snapshot.status == "update-available":
    stage["reason"] = "dry-run-valid-snapshot-not-promoted"
    report["stages"]["statsSource"] = stage

    return False

  report["stages"]["statsSource"] = stage

  return snapshot.status in {"updated", "unchanged"}


def _overall_status(report: dict) -> str:
  stages = [
      value for value in report.get("stages", {}).values()
      if isinstance(value, dict)
  ]
  failed = [item for item in stages if item.get("status") == "failed"]
  successful = [
      item for item in stages
      if item.get("status") in {"success", "partial", "ready"}
  ]
  partial = any(item.get("status") == "partial" for item in stages) or any(
      item.get("statsImportStatus") in {"blocked", "failed"}
      for item in stages)

  if failed and successful:
    return "partial"

  if failed:
    return "failed"

  return "partial" if partial else "success"


def _record_stats_attempt_status(settings: Settings,
                                 state: str) -> bool | None:
  from app.database import create_mongodb_client

  client = None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")

    return record_stats_import_state(client[settings.mongodb_database],
                                     settings.tournament_slug, state)
  except Exception:
    return False
  finally:
    if client is not None:
      client.close()


def run_update(settings: Settings,
               dry_run: bool,
               continuous: bool = False,
               scheduled_next: datetime | None = None) -> tuple[dict, Path]:
  mode = "continuous-dry-run" if continuous and dry_run else "continuous-live" if continuous else "once-dry-run" if dry_run else "once-live"
  report = _new_report(mode)
  manual_job_id = _manual_update_job_id()

  if manual_job_id and not continuous and not dry_run:
    _record_manual_update_phase(settings, "getting_source")

  import_status, status_connected = _read_import_status(settings)
  prior_count = _prior_liquipedia_match_count(settings, import_status)
  prior_duckdb_count = _prior_duckdb_match_count(settings, import_status)
  prior_verified_count = _last_applied_schedule_match_count(
      settings, import_status)
  report["mongodb"]["connected"] = status_connected

  try:
    schedule_plan, schedule_preflight, schedule_path = _run_schedule(
        settings, report, dry_run, prior_count, prior_duckdb_count,
        prior_verified_count)
  except Exception:
    report["stages"]["schedule"] = {
        "status": "failed",
        "applied": False,
        "errorCode": "INTERNAL_ERROR"
    }
    _add_error(report, "schedule", "INTERNAL_ERROR")
    schedule_plan = None
    schedule_preflight = None
    schedule_path = None

  schedule_stage = report["stages"].get("schedule") or {}
  schedule_ready_for_stats = (
      schedule_stage.get("readyForApply") is True
      if dry_run else schedule_stage.get("postApplyVerified")
      is True) and schedule_stage.get("status") in {"success", "partial"}

  if dry_run and schedule_ready_for_stats and (
      (schedule_stage.get("preflight") or {}).get("plannedCreate", 0) or
      (schedule_stage.get("preflight") or {}).get("plannedUpdate", 0) or
      (schedule_stage.get("preflight") or {}).get("matchOperationRisks")):
    report["stages"]["stats"] = {
        "status":
        "pending",
        "statsImportStatus":
        "pending",
        "statsPlanRequired":
        True,
        "statsLinkedMatches": (schedule_stage.get("sourceCounts")
                               or {}).get("duckdbStatsAvailable", 0),
        "applied":
        False,
        "reason":
        "waiting-for-schedule-apply-postflight-before-stats-link-verification"
    }
    schedule_ready_for_stats = False

  if schedule_ready_for_stats:
    try:
      source_ready = _run_stats_source(settings, report, dry_run)

      if source_ready:
        source_stage = report["stages"].get("statsSource") or {}
        stats_reconciliation_path = None

        if source_stage.get("status") == "updated":
          stats_reconciliation_path = _run_stats_reconciliation(
              settings, report, dry_run)

        if source_stage.get(
            "status") != "updated" or stats_reconciliation_path is not None:
          _run_stats(settings, report, dry_run, import_status,
                     stats_reconciliation_path)
        else:
          schedule_source = schedule_stage.get("sourceCounts") or {}
          skipped_count = schedule_source.get("duckdbStatsAvailable", 0)

          if (not isinstance(skipped_count, int)
              or isinstance(skipped_count, bool) or skipped_count < 0):
            skipped_count = 0

          report["stages"]["stats"] = {
              "status": "blocked",
              "statsImportStatus": "blocked",
              "statsPlanRequired": True,
              "applied": False,
              "reason": "post-snapshot-stats-reconciliation-failed",
              "skippedCount": skipped_count
          }
      else:
        source_stage = report["stages"].get("statsSource") or {}
        source_skipped_count = (schedule_stage.get("sourceCounts")
                                or {}).get("duckdbStatsAvailable", 0)

        if (not isinstance(source_skipped_count, int)
            or isinstance(source_skipped_count, bool)
            or source_skipped_count < 0):
          source_skipped_count = 0

        report["stages"]["stats"] = {
            "status":
            "blocked" if source_stage.get("status") == "failed" else "pending",
            "statsImportStatus":
            "blocked" if source_stage.get("status") == "failed" else "pending",
            "statsPlanRequired":
            True,
            "applied":
            False,
            "reason":
            "stats-source-refresh-failed" if source_stage.get("status")
            == "failed" else "new-snapshot-available-in-dry-run",
            "skippedCount":
            source_skipped_count
            if source_stage.get("status") == "failed" else 0
        }
    except Exception:
      report["stages"]["stats"] = {
          "status": "failed",
          "applied": False,
          "errorCode": "INTERNAL_ERROR"
      }
      _add_error(report, "stats", "INTERNAL_ERROR")
  else:
    report["stages"]["statsSource"] = {
        "status": "skipped",
        "reason": "schedule-postflight-not-verified"
    }

    if "stats" not in report["stages"]:
      report["stages"]["stats"] = {
          "status":
          "pending",
          "statsImportStatus":
          "pending",
          "statsPlanRequired":
          True,
          "applied":
          False,
          "reason":
          "waiting-for-schedule-postflight-before-stats-link-verification"
      }

  report["stages"]["assets"] = {
      "status": "skipped",
      "applied": False,
      "reason": "asset-import-requires-explicit-command"
  }

  stats_stage = report["stages"].get("stats") or {}
  stats_import_state = stats_stage.get("statsImportStatus")

  if (stats_import_state not in {"pending", "blocked", "failed"}
      and stats_stage.get("status") == "failed"):
    stats_import_state = "failed"

  if not dry_run and stats_import_state in {"pending", "blocked", "failed"}:
    status_write = _record_stats_attempt_status(settings, stats_import_state)

    if status_write is not None:
      report["mongodb"]["connected"] = True
      report["mongodb"]["writes"] += int(status_write)
    else:
      report["warnings"].append(
          "Stats attempt status could not be recorded; no successful Stats metadata was advanced."
      )

  status = _overall_status(report)

  if manual_job_id and not continuous and not dry_run:
    _record_manual_update_phase(settings,
                                "final_validation",
                                enabled=not dry_run)
    _record_manual_update_result(settings, manual_job_id, status, report)

  report["scheduler"].update({
      "status":
      "active" if continuous else "disabled",
      "nextImportAt":
      _iso_utc(scheduled_next) if continuous and scheduled_next else None
  })

  if not dry_run and continuous:
    last_failure = next((item for item in reversed(report["errors"])), None)
    has_failure = bool(report["errors"])
    metadata_updated = _record_scheduler_metadata(
        settings, "active" if continuous else "disabled",
        scheduled_next if continuous else None,
        _parse_report_time(report.get("startedAt")),
        datetime.now(timezone.utc) if not has_failure else None,
        datetime.now(timezone.utc) if has_failure else None,
        last_failure.get("code") if last_failure else None, status,
        datetime.now(timezone.utc))
    report["scheduler"]["metadataWrites"] = 1 if metadata_updated else 0

    if not metadata_updated:
      report["warnings"].append(
          "Scheduler status could not be saved because the Tournament status record is unavailable."
      )

  path = _finish_report(
      settings, report, status if not dry_run else
      ("dry-run" if status == "success" else status))

  return report, path


def _parse_report_time(value: str | None) -> datetime | None:
  return parse_source_datetime(value)


def _write_lock_skip_report(settings: Settings) -> tuple[dict, Path]:
  report = _new_report("lock-skipped")
  report["status"] = "skipped"
  report["lock"]["reason"] = "Another process owns the import lock."
  _add_error(report, "lock", "LOCK_BUSY")

  return report, _finish_report(settings, report, "skipped")


def write_disabled_run_report(settings: Settings,
                              reason: str) -> tuple[dict, Path]:
  report = _new_report("disabled")
  report["scheduler"].update({"status": "disabled", "reason": reason})

  return report, _finish_report(settings, report, "disabled")


def run_once_with_lock(
    settings: Settings,
    dry_run: bool,
    continuous: bool = False,
    scheduled_next: datetime | None = None) -> tuple[dict, Path]:
  lock = ImportProcessLock(settings.runtime_directory,
                           settings.auto_update_lock_stale_minutes)

  try:
    lock.acquire()
  except ImportLockBusy:
    report, report_path = _write_lock_skip_report(settings)
    manual_job_id = os.environ.get("VCT_ANALYTICS_MANUAL_UPDATE_JOB_ID", "")

    try:
      manual_job_id = str(uuid.UUID(manual_job_id))
    except (ValueError, AttributeError):
      manual_job_id = ""

    if manual_job_id and not continuous and not dry_run:
      _record_manual_update_result(settings, manual_job_id, "busy", report)

    return report, report_path

  try:
    report, report_path = run_update(settings,
                                     dry_run=dry_run,
                                     continuous=continuous,
                                     scheduled_next=scheduled_next)
  finally:
    lock.release()

  report["lock"]["acquired"] = True
  report["lock"]["released"] = True
  report["reportPath"] = str(report_path.resolve())
  write_json_atomic(report_path, report, allow_nan=False)

  return report, report_path


def run_continuously(settings: Settings) -> int:
  lock = ImportProcessLock(settings.runtime_directory,
                           settings.auto_update_lock_stale_minutes,
                           "updater-process.lock")

  try:
    lock.acquire()
  except ImportLockBusy:
    report = _new_report("updater-lock-skipped")
    report["status"] = "skipped"
    report["lock"]["reason"] = "Another updater process owns the updater lock."
    _add_error(report, "lock", "LOCK_BUSY")
    report_path = _finish_report(settings, report, "skipped")
    print(
        f"Updater skipped because another updater is already active. Run report: {report_path}"
    )

    return 2

  if not settings.auto_update_enabled:
    try:
      _record_scheduler_metadata(settings,
                                 "disabled",
                                 None,
                                 heartbeat_at=datetime.now(timezone.utc))
      _, path = write_disabled_run_report(settings,
                                          "AUTO_UPDATE_ENABLED is false.")
      print(f"Automatic updater is disabled. Run report: {path}")

      return 0
    finally:
      lock.release()

  stop_path = settings.runtime_directory / "updater.stop"
  print("Automatic updater started. Press Ctrl+C to stop.")
  failures = 0
  next_run = (datetime.now(timezone.utc) if settings.auto_update_run_on_start
              else _next_import_time(settings, datetime.now(timezone.utc)))
  schedule_state = {"next_import_at": next_run}
  heartbeat_stop = Event()
  heartbeat_thread = Thread(target=_scheduler_heartbeat,
                            args=(settings, heartbeat_stop, schedule_state),
                            name="updater-heartbeat",
                            daemon=True)

  try:
    stop_path.unlink(missing_ok=True)
    _record_scheduler_metadata(settings,
                               "active",
                               next_run,
                               heartbeat_at=datetime.now(timezone.utc))
    heartbeat_thread.start()

    while True:
      while not stop_path.exists():
        remaining = (next_run - datetime.now(timezone.utc)).total_seconds()

        if remaining <= 0:
          break

        time.sleep(min(remaining, 15.0))

      if stop_path.exists():
        print("Stopping automatic updater.")
        heartbeat_stop.set()
        heartbeat_thread.join()
        _record_scheduler_metadata(settings,
                                   "disabled",
                                   None,
                                   heartbeat_at=datetime.now(timezone.utc))
        break

      run_started_at = datetime.now(timezone.utc)
      next_after = _next_import_time(settings, run_started_at)
      schedule_state["next_import_at"] = next_after
      _record_scheduler_metadata(settings,
                                 "active",
                                 next_after,
                                 last_attempt_at=run_started_at,
                                 heartbeat_at=run_started_at)

      try:
        report, report_path = run_once_with_lock(settings,
                                                 dry_run=False,
                                                 continuous=True,
                                                 scheduled_next=next_after)
      except Exception:
        report = _new_report("continuous-live")
        report["status"] = "failed"
        _add_error(report, "updater", "INTERNAL_ERROR")
        report_path = _finish_report(settings, report, "failed")

      if not report.get("errors"):
        failures = 0
        next_run = _next_import_time(settings, datetime.now(timezone.utc))
      else:
        failures += 1

        if settings.auto_update_mode == "interval":
          backoff = max(
              settings.auto_update_interval_minutes,
              min(settings.auto_update_backoff_max_minutes,
                  2**min(failures, 10)))
          next_run = datetime.now(timezone.utc) + timedelta(minutes=backoff)
        else:
          next_run = _next_import_time(settings, datetime.now(timezone.utc))

      heartbeat_now = datetime.now(timezone.utc)
      _record_scheduler_metadata(
          settings,
          "active",
          next_run,
          last_failure_at=heartbeat_now if report.get("errors") else None,
          last_failure_code=report["errors"][-1].get("code")
          if report.get("errors") else None,
          last_run_status=report.get("status") if report.get("status")
          in {"success", "partial", "failed"} else "failed",
          heartbeat_at=heartbeat_now)

      if report["lock"].get("acquired"):
        report["lock"]["released"] = True

      report["scheduler"].update({
          "status": "active",
          "nextImportAt": _iso_utc(next_run)
      })
      write_json_atomic(report_path, report, allow_nan=False)
      schedule_state["next_import_at"] = next_run
      print(f"Updater run {report['status']}. Report: {report_path}")
  except KeyboardInterrupt:
    print("Stopping automatic updater.")
    heartbeat_stop.set()

    if heartbeat_thread.ident is not None:
      heartbeat_thread.join()

    _record_scheduler_metadata(settings,
                               "disabled",
                               None,
                               heartbeat_at=datetime.now(timezone.utc))
  finally:
    heartbeat_stop.set()

    if heartbeat_thread.ident is not None and heartbeat_thread.is_alive():
      heartbeat_thread.join()

    lock.release()

  return 0


def _next_import_time(settings: Settings, now: datetime) -> datetime:
  from app.importers.update_schedule import next_scheduled_run

  value = next_scheduled_run(settings, now)

  if value is None:
    raise ValueError("An enabled updater must have a scheduled next run.")

  return value


def _scheduler_heartbeat(settings: Settings, stop_event: Event,
                         schedule_state: dict) -> None:
  while not stop_event.wait(60):
    next_import_at = schedule_state.get("next_import_at")
    _record_scheduler_metadata(
        settings,
        "active",
        next_import_at if isinstance(next_import_at, datetime) else None,
        heartbeat_at=datetime.now(timezone.utc))
