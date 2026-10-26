from datetime import datetime, timezone
import re

IMPORT_TYPES = ("schedule", "stats", "assets")
ANALYTICAL_IMPORT_TYPES = ("schedule", "stats")
MANUAL_UPDATE_PHASES = {
    "getting_source", "reconciling", "applying_schedule",
    "checking_stats_source", "downloading_stats_source",
    "validating_stats_source", "stats_source_unchanged",
    "stats_source_updated", "stats_source_failed", "verifying_stats",
    "final_validation"
}
SOURCE_HASH_FIELDS = {
    "duckdbSha256", "duckdbSchemaFingerprint",
    "canonicalScheduleMappingDigest", "liquipediaCacheSha256"
}
SOURCE_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$", re.IGNORECASE)


def parse_source_datetime(value: str | datetime | None) -> datetime | None:
  if isinstance(value, datetime):
    parsed = value
  elif isinstance(value, str) and value.strip():
    normalized = value.strip()

    if normalized.endswith("Z"):
      normalized = normalized[:-1] + "+00:00"

    try:
      parsed = datetime.fromisoformat(normalized)
    except ValueError:
      return None
  else:
    return None

  if parsed.tzinfo is None or parsed.utcoffset() is None:
    return None

  return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
  if value is None:
    return None

  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def expected_status(skipped: int, failed: int = 0) -> str:
  if failed:
    return "failed"

  return "partial" if skipped else "success"


def preview_import_status(import_type: str,
                          source_data_as_of: str | datetime | None,
                          plan_digest: str | None,
                          skipped: int,
                          current: dict | None = None) -> dict:
  source_time = parse_source_datetime(source_data_as_of)
  status = expected_status(skipped)
  prior = current or {}
  prior_imports = prior.get("imports")
  prior_imports = prior_imports if isinstance(prior_imports, dict) else {}
  source_times = []

  if import_type in ANALYTICAL_IMPORT_TYPES and source_time is not None:
    source_times.append(source_time)

  for import_name in ANALYTICAL_IMPORT_TYPES:
    entry = prior_imports.get(import_name)

    if isinstance(entry, dict):
      source_times.append(parse_source_datetime(entry.get("dataAsOf")))

  source_times = [value for value in source_times if value is not None]
  data_as_of = max(source_times) if source_times else None

  if import_type == "assets" and data_as_of is None:
    data_as_of = parse_source_datetime(prior.get("dataAsOf"))

  imported_at = {}
  import_details = {}

  for import_name in IMPORT_TYPES:
    value = prior.get(f"{import_name}ImportedAt")
    imported_at[import_name] = iso_utc(parse_source_datetime(value))
    entry = prior_imports.get(import_name)
    entry = entry if isinstance(entry, dict) else {}
    is_current = import_name == import_type
    import_details[import_name] = {
        "status":
        status if is_current else entry.get("status"),
        "lastSuccessfulAt":
        "set-on-successful-apply" if is_current else iso_utc(
            parse_source_datetime(entry.get("lastSuccessfulAt"))),
        "dataAsOf":
        iso_utc(source_time) if is_current else iso_utc(
            parse_source_datetime(entry.get("dataAsOf"))),
        "planDigest":
        plan_digest if is_current else entry.get("planDigest")
    }

  prior_digests = prior.get("planDigests")
  prior_digests = prior_digests if isinstance(prior_digests, dict) else {}
  plan_digests = {name: prior_digests.get(name) for name in IMPORT_TYPES}
  plan_digests[import_type] = plan_digest
  fields = {
      "lastImportedAt":
      "set-on-successful-apply",
      "nextImportAt":
      None,
      "status":
      status,
      "dataAsOf":
      iso_utc(data_as_of),
      "planDigest":
      plan_digest,
      "scheduleImportedAt":
      "set-on-successful-apply"
      if import_type == "schedule" else imported_at["schedule"],
      "statsImportedAt":
      "set-on-successful-apply"
      if import_type == "stats" else imported_at["stats"],
      "assetsImportedAt":
      "set-on-successful-apply"
      if import_type == "assets" else imported_at["assets"],
      "planDigests":
      plan_digests,
      "imports":
      import_details
  }

  return {
      "importType": import_type,
      "status": status,
      "sourceDataAsOf": iso_utc(source_time),
      "planDigest": plan_digest,
      "afterSuccessfulApply": fields
  }


def record_import_status(database,
                         tournament_slug: str,
                         import_type: str,
                         status: str,
                         source_data_as_of: str | datetime | None,
                         plan_digest: str | None,
                         source_identity: dict | None = None,
                         stats_verification: dict | None = None) -> None:
  if import_type not in IMPORT_TYPES or status not in {
      "success", "partial", "failed"
  }:
    raise ValueError("Invalid import status identity.")

  tournament = database["tournaments"].find_one({"slug": tournament_slug},
                                                {"importStatus": 1})

  if tournament is None:
    raise RuntimeError("Tournament import status target is missing.")

  current = tournament.get("importStatus")
  current = current if isinstance(current, dict) else {}
  imports = current.get("imports")
  imports = dict(imports) if isinstance(imports, dict) else {}
  plan_digests = current.get("planDigests")
  plan_digests = dict(plan_digests) if isinstance(plan_digests, dict) else {}

  for name in IMPORT_TYPES:
    plan_digests.setdefault(name, None)
    imported_field = f"{name}ImportedAt"
    current.setdefault(imported_field, None)
    entry = imports.get(name)
    entry = dict(entry) if isinstance(entry, dict) else {}
    entry.setdefault("status", None)
    entry.setdefault("lastSuccessfulAt", None)
    entry.setdefault("dataAsOf", None)
    entry.setdefault("planDigest", None)
    imports[name] = entry

  current["planDigests"] = plan_digests
  current["nextImportAt"] = current.get("nextImportAt") if current.get(
      "schedulerStatus") == "active" else None

  current.setdefault("dataAsOf", None)
  current.setdefault("lastImportedAt", None)
  previous_type = imports.get(import_type)
  previous_type = dict(previous_type) if isinstance(previous_type,
                                                    dict) else {}
  imported_at = datetime.now(timezone.utc)

  if status == "failed":
    previous_type["status"] = "failed"

    if import_type == "stats":
      previous_type["statsImportStatus"] = "failed"
      previous_type["statsPostflightStatus"] = "failed"

    imports[import_type] = previous_type
    updated = {
        **current, "status": "failed",
        "planDigests": plan_digests,
        "imports": imports
    }
  else:
    source_time = parse_source_datetime(source_data_as_of)
    previous_type.update({
        "status": status,
        "lastSuccessfulAt": imported_at,
        "dataAsOf": source_time,
        "planDigest": plan_digest
    })

    if source_identity is not None:
      safe_identity: dict[str, str | int] = {
          key: value.casefold()
          for key, value in source_identity.items()
          if key in SOURCE_HASH_FIELDS and isinstance(value, str)
          and SOURCE_HASH_PATTERN.fullmatch(value)
      }
      liquipedia_count = source_identity.get("liquipediaMatchCount")

      if isinstance(liquipedia_count, int) and liquipedia_count >= 0:
        safe_identity["liquipediaMatchCount"] = liquipedia_count

      verified_match_count = source_identity.get("verifiedMatchCount")

      if isinstance(verified_match_count, int) and verified_match_count >= 0:
        safe_identity["verifiedMatchCount"] = verified_match_count

      duckdb_match_count = source_identity.get("duckdbMatchCount")

      if isinstance(duckdb_match_count, int) and duckdb_match_count >= 0:
        safe_identity["duckdbMatchCount"] = duckdb_match_count

      if safe_identity:
        previous_type["sourceIdentity"] = safe_identity

    if import_type == "stats" and stats_verification is not None:
      identity = previous_type.get("sourceIdentity")
      identity = dict(identity) if isinstance(identity, dict) else {}

      for key in ("duckdbSha256", "duckdbSchemaFingerprint",
                  "canonicalScheduleMappingDigest"):
        value = stats_verification.get(key)

        if isinstance(value, str) and SOURCE_HASH_PATTERN.fullmatch(value):
          identity[key] = value.casefold()

      if identity:
        previous_type["sourceIdentity"] = identity

      import_status_value = stats_verification.get("statsImportStatus")

      if import_type == "stats" and import_status_value in {
          "unchanged", "applied", "partial"
      }:
        previous_type["statsImportStatus"] = import_status_value
        previous_type[
            "statsPostflightStatus"] = "passed" if stats_verification.get(
                "postflightStatus") == "passed" else None
        previous_type["sourceTimestamp"] = parse_source_datetime(
            stats_verification.get("sourceTimestamp"))
        linked_matches = stats_verification.get("statsLinkedMatches")

        if (isinstance(linked_matches, int)
            and not isinstance(linked_matches, bool) and linked_matches >= 0):
          previous_type["statsLinkedMatches"] = linked_matches

        raw_counts = stats_verification.get("statsDocumentCounts")
        allowed_count_fields = ("players", "matchMaps", "playerMapStats",
                                "teamMapStats")

        if isinstance(raw_counts, dict):
          counts = {
              field: raw_counts[field]
              for field in allowed_count_fields
              if isinstance(raw_counts.get(field), int) and not isinstance(
                  raw_counts.get(field), bool) and raw_counts[field] >= 0
          }

          if len(counts) == len(allowed_count_fields):
            previous_type["statsDocumentCounts"] = counts

    imports[import_type] = previous_type

    source_times = [
        parse_source_datetime((imports.get(name) or {}).get("dataAsOf"))
        for name in ANALYTICAL_IMPORT_TYPES
    ]
    source_times = [value for value in source_times if value is not None]
    latest_source_time = max(source_times) if source_times else None

    if import_type == "assets" and latest_source_time is None:
      latest_source_time = parse_source_datetime(current.get("dataAsOf"))

    plan_digests[import_type] = plan_digest
    imported_field = f"{import_type}ImportedAt"
    updated = {
        **current, "status":
        status,
        "dataAsOf":
        latest_source_time,
        "lastImportedAt":
        imported_at,
        "nextImportAt":
        current.get("nextImportAt")
        if current.get("schedulerStatus") == "active" else None,
        imported_field:
        imported_at,
        "planDigests":
        plan_digests,
        "imports":
        imports
    }

  result = database["tournaments"].update_one(
      {"_id": tournament["_id"]}, {"$set": {
          "importStatus": updated
      }},
      upsert=False)

  if result.matched_count != 1:
    raise RuntimeError("Tournament import status target changed during Apply.")


def record_stats_import_state(database, tournament_slug: str,
                              state: str) -> bool:
  if state not in {"pending", "blocked", "failed"}:
    raise ValueError("Invalid Stats import attempt state.")

  postflight_status = "failed" if state == "failed" else "not-completed"
  result = database["tournaments"].update_one({"slug": tournament_slug}, {
      "$set": {
          "importStatus.imports.stats.statsImportStatus": state,
          "importStatus.imports.stats.statsPostflightStatus": postflight_status
      }
  },
                                              upsert=False)

  if result.matched_count != 1:
    raise RuntimeError(
        "Tournament Stats import status target is missing or changed.")

  return result.modified_count > 0


def record_scheduler_state(database,
                           tournament_slug: str,
                           scheduler_status: str,
                           next_import_at: datetime | None,
                           last_attempt_at: datetime | None = None,
                           last_successful_at: datetime | None = None,
                           last_failure_at: datetime | None = None,
                           last_failure_code: str | None = None,
                           last_run_status: str | None = None,
                           heartbeat_at: datetime | None = None) -> None:
  if scheduler_status not in {"active", "disabled", "stale"}:
    raise ValueError("Invalid scheduler status.")

  if last_run_status not in {None, "success", "partial", "failed"}:
    raise ValueError("Invalid scheduler run status.")

  safe_codes = {
      None, "SOURCE_UNAVAILABLE", "STATS_SOURCE_FAILED", "PLAN_BLOCKED",
      "PREFLIGHT_BLOCKED", "WRITE_LIMIT_EXCEEDED", "APPLY_FAILED",
      "POST_APPLY_FAILED", "LOCK_BUSY", "INTERNAL_ERROR"
  }

  if last_failure_code not in safe_codes:
    raise ValueError("Invalid scheduler failure code.")

  update = {
      "importStatus.schedulerStatus":
      scheduler_status,
      "importStatus.nextImportAt":
      next_import_at if scheduler_status == "active" else None
  }
  optional_values = {
      "importStatus.lastAttemptAt": last_attempt_at,
      "importStatus.lastSuccessfulAt": last_successful_at,
      "importStatus.lastFailureAt": last_failure_at,
      "importStatus.lastFailureCode": last_failure_code,
      "importStatus.lastRunStatus": last_run_status,
      "importStatus.schedulerHeartbeatAt": heartbeat_at
  }

  for field, value in optional_values.items():
    if value is not None:
      update[field] = value

  result = database["tournaments"].update_one({"slug": tournament_slug},
                                              {"$set": update},
                                              upsert=False)

  if result.matched_count != 1:
    raise RuntimeError(
        "Tournament scheduler status target is missing or changed.")


def record_manual_update_status(database,
                                tournament_slug: str,
                                job_id: str,
                                status: str,
                                finished_at: datetime,
                                failure_message: str | None = None,
                                summary: dict | None = None) -> None:
  if status not in {"busy", "success", "partial", "failed"}:
    raise ValueError("Invalid manual update status.")

  result = database["tournaments"].update_one(
      {
          "slug": tournament_slug,
          "importStatus.manualUpdate.jobId": job_id
      }, {
          "$set": {
              "importStatus.manualUpdate.status": status,
              "importStatus.manualUpdate.finishedAt": finished_at,
              "importStatus.manualUpdate.failureMessage": failure_message,
              "importStatus.manualUpdate.summary": summary,
              "importStatus.manualUpdate.phase": None
          }
      },
      upsert=False)

  if result.matched_count != 1:
    raise RuntimeError(
        "Manual update status target changed before completion.")


def record_manual_update_phase(database, tournament_slug: str, job_id: str,
                               phase: str) -> None:
  if phase not in MANUAL_UPDATE_PHASES:
    raise ValueError("Invalid manual update phase.")

  result = database["tournaments"].update_one(
      {
          "slug": tournament_slug,
          "importStatus.manualUpdate.jobId": job_id,
          "importStatus.manualUpdate.status": "running"
      }, {"$set": {
          "importStatus.manualUpdate.phase": phase
      }},
      upsert=False)

  if result.matched_count != 1:
    raise RuntimeError(
        "Manual update phase target changed before progress was recorded.")
