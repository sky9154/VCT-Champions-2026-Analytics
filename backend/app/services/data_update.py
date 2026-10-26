from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
from threading import Thread
import uuid

from pymongo import ReturnDocument
from pymongo.database import Database

from app.config import BACKEND_DIRECTORY, Settings
from app.database import create_mongodb_client
from app.importers.import_status import record_manual_update_status, parse_source_datetime
from app.importers.process_lock import is_import_process_locked, read_import_process_started_at
from app.api.errors import APIError
from app.api.responses import utc_datetime_string

MANUAL_UPDATE_FAILURE_MESSAGE = "資料更新未完成，已保留上一次成功資料。"
MANUAL_UPDATE_LAUNCH_GRACE = timedelta(seconds=60)
MANUAL_UPDATE_SUMMARY_FIELDS = ("scheduleTotal", "scheduleSynced",
                                "statsAvailable", "statsUnavailable",
                                "scheduleCreated", "scheduleUpdated",
                                "scheduleUnchanged", "statsUpdated",
                                "statsCreated", "statsUnchanged",
                                "statsSkipped", "statsSnapshotStatus",
                                "statsImportStatus", "statsPlanRequired",
                                "statsLinkedMatches", "statsPendingMatches",
                                "conflicts", "ambiguous", "incomplete",
                                "unmatched", "participantsUnresolved")
MANUAL_UPDATE_PHASES = {
    "getting_source", "reconciling", "applying_schedule",
    "checking_stats_source", "downloading_stats_source",
    "validating_stats_source", "stats_source_unchanged",
    "stats_source_updated", "stats_source_failed", "verifying_stats",
    "final_validation"
}


def _status_record(database: Database, tournament_slug: str) -> dict:
  tournament = database["tournaments"].find_one({"slug": tournament_slug}, {
      "importStatus": 1,
      "_id": 0
  })
  status = tournament.get("importStatus") if isinstance(tournament,
                                                        dict) else None

  return status if isinstance(status, dict) else {}


def _finish_manual_update(settings: Settings, job_id: str, status: str,
                          finished_at: datetime) -> None:
  client = None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    record_manual_update_status(
        client[settings.mongodb_database], settings.tournament_slug, job_id,
        status, finished_at,
        MANUAL_UPDATE_FAILURE_MESSAGE if status == "failed" else None)
  except Exception:
    return
  finally:
    if client is not None:
      client.close()


def _recover_child_exit(settings: Settings, job_id: str) -> None:
  client = None

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    database = client[settings.mongodb_database]
    status = _status_record(database, settings.tournament_slug)
    manual = status.get("manualUpdate")

    if (isinstance(manual, dict) and manual.get("jobId") == job_id
        and manual.get("status") == "running"):
      record_manual_update_status(database, settings.tournament_slug, job_id,
                                  "failed", datetime.now(timezone.utc),
                                  MANUAL_UPDATE_FAILURE_MESSAGE)
  except Exception:
    return
  finally:
    if client is not None:
      client.close()


def _watch_child(process: subprocess.Popen, settings: Settings,
                 job_id: str) -> None:
  try:
    process.wait()
    _recover_child_exit(settings, job_id)
  except Exception:
    return


def get_data_update_status(database: Database, settings: Settings) -> dict:
  import_status = _status_record(database, settings.tournament_slug)
  manual = import_status.get("manualUpdate")
  manual = manual if isinstance(manual, dict) else {}
  stored_status = manual.get("status")
  stored_status = stored_status if stored_status in {
      "running", "success", "partial", "failed", "busy"
  } else "idle"
  started_at = manual.get("startedAt")
  finished_at = manual.get("finishedAt")
  job_id = manual.get("jobId") if isinstance(manual.get("jobId"),
                                             str) else None
  lock_is_held = is_import_process_locked(settings.runtime_directory)

  if lock_is_held:
    if stored_status == "running":
      status = "running"
    else:
      status = "busy"
      job_id = None
      started_at = read_import_process_started_at(settings.runtime_directory)

    finished_at = None
  elif stored_status == "running":
    parsed_started_at = parse_source_datetime(started_at) if isinstance(
        started_at, (str, datetime)) else None

    if (parsed_started_at is not None
        and datetime.now(timezone.utc) - parsed_started_at
        <= MANUAL_UPDATE_LAUNCH_GRACE):
      status = "running"
      finished_at = None
    else:
      status = "failed"
  elif stored_status == "busy":
    status = "idle"
    job_id = None
    started_at = None
    finished_at = None
  else:
    status = stored_status

  phase = manual.get("phase")
  phase = phase if phase in MANUAL_UPDATE_PHASES and status in {
      "running", "busy"
  } else None

  return {
      "jobId": job_id,
      "status": status,
      "startedAt": utc_datetime_string(started_at),
      "finishedAt": utc_datetime_string(finished_at),
      "failureMessage":
      MANUAL_UPDATE_FAILURE_MESSAGE if status == "failed" else None,
      "phase": phase,
      "dataAsOf": utc_datetime_string(import_status.get("dataAsOf")),
      "lastImportedAt":
      utc_datetime_string(import_status.get("lastImportedAt")),
      "summary": _safe_manual_summary(manual.get("summary"))
  }


def _safe_manual_summary(
    value: object) -> dict[str, int | str | bool | None] | None:
  if not isinstance(value, dict):
    return None

  summary = {}

  for field in MANUAL_UPDATE_SUMMARY_FIELDS:
    if field == "statsSnapshotStatus":
      status = value.get(field)

      if status not in {
          None, "updated", "unchanged", "unavailable", "invalid", "failed"
      }:
        return None

      summary[field] = status
      continue

    if field == "statsImportStatus":
      status = value.get(field)

      if status not in {
          None, "pending", "planning", "ready", "unchanged", "applied",
          "partial", "failed", "blocked"
      }:
        return None

      summary[field] = status
      continue

    if field == "statsPlanRequired":
      flag = value.get(field)

      if flag is not None and not isinstance(flag, bool):
        return None

      summary[field] = flag
      continue

    if field in {"statsLinkedMatches", "statsPendingMatches"}:
      count = value.get(field)

      if count is not None and (not isinstance(count, int)
                                or isinstance(count, bool) or count < 0):
        return None

      summary[field] = count
      continue

    if field in {"statsCreated", "statsSkipped"} and field not in value:
      summary[field] = 0
      continue

    count = value.get(field)

    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
      return None

    summary[field] = count

  return summary


def _start_response(job_id: str | None, status: str,
                    accepted_at: datetime) -> dict:
  return {
      "jobId": job_id,
      "status": status,
      "acceptedAt": utc_datetime_string(accepted_at)
  }


def _start_child(job_id: str) -> subprocess.Popen:
  environment = os.environ.copy()
  environment["VCT_ANALYTICS_MANUAL_UPDATE_JOB_ID"] = job_id
  creation_flags = getattr(subprocess, "CREATE_NO_WINDOW",
                           0) if os.name == "nt" else 0

  return subprocess.Popen(
      [sys.executable, "-m", "app.cli.auto_update", "--once"],
      cwd=BACKEND_DIRECTORY,
      env=environment,
      stdin=subprocess.DEVNULL,
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      close_fds=True,
      shell=False,
      creationflags=creation_flags)


def start_data_update(database: Database, settings: Settings) -> dict:
  current = get_data_update_status(database, settings)
  accepted_at = datetime.now(timezone.utc)

  if current["status"] in {"running", "busy"}:
    return _start_response(current.get("jobId"), "busy", accepted_at)

  job_id = str(uuid.uuid4())
  initial_status = {
      "jobId": job_id,
      "status": "running",
      "acceptedAt": accepted_at,
      "startedAt": accepted_at,
      "finishedAt": None,
      "failureMessage": None,
      "phase": "getting_source"
  }
  claimed = database["tournaments"].find_one_and_update(
      {
          "slug": settings.tournament_slug,
          "importStatus.manualUpdate.status": {
              "$ne": "running"
          }
      }, {"$set": {
          "importStatus.manualUpdate": initial_status,
      }},
      return_document=ReturnDocument.AFTER,
      upsert=False)

  if claimed is None:
    current = get_data_update_status(database, settings)

    if current["status"] in {"running", "busy"}:
      return _start_response(current.get("jobId"), "busy", accepted_at)

    raise APIError(503, "DATA_UNAVAILABLE", "目前無法取得資料，請稍後再試。")

  try:
    process = _start_child(job_id)
  except OSError:
    _finish_manual_update(settings, job_id, "failed",
                          datetime.now(timezone.utc))
    raise APIError(503, "UPDATE_UNAVAILABLE", "目前無法啟動資料更新，請稍後再試。") from None

  monitor = Thread(target=_watch_child,
                   args=(process, settings, job_id),
                   daemon=True)
  monitor.start()

  return _start_response(job_id, "running", accepted_at)
