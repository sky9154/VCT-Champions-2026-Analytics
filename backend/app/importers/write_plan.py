import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from app.config import Settings
from app.utils.files import sha256_file, write_json_atomic
from app.importers.import_status import expected_status, preview_import_status, record_import_status

MATCH_STATUSES = {"scheduled", "live", "completed", "cancelled", "postponed"}
TEAM_CANONICAL_NAMES = {"xlgesports": "Xi Lai Gaming", "xlg": "Xi Lai Gaming"}
TEAM_SLUGS = {
    "100thieves": "100-thieves",
    "edwardgaming": "edward-gaming",
    "futesports": "fut-esports",
    "g2esports": "g2-esports",
    "globalesports": "global-esports",
    "jdgaming": "jdg-esports",
    "karminecorp": "karmine-corp",
    "loud": "loud",
    "nrg": "nrg",
    "nongshimredforce": "nongshim-redforce",
    "paperrex": "paper-rex",
    "t1": "t1",
    "tyloo": "tyloo",
    "teamliquid": "team-liquid",
    "teamvitality": "team-vitality",
    "xilaigaming": "xi-lai-gaming"
}
TEAM_SHORT_NAMES = {
    "100thieves": "100T",
    "edwardgaming": "EDG",
    "futesports": "FUT",
    "g2esports": "G2",
    "globalesports": "GE",
    "jdgaming": "JDG",
    "karminecorp": "KC",
    "loud": "LOUD",
    "nrg": "NRG",
    "nongshimredforce": "NS",
    "paperrex": "PRX",
    "t1": "T1",
    "tyloo": "TYL",
    "teamliquid": "TL",
    "teamvitality": "VIT",
    "xilaigaming": "XLG"
}
INDEX_OPERATIONS = [{
    "collection": "tournaments",
    "keys": [("slug", 1)],
    "unique": True,
    "name": "tournaments_slug_unique"
}, {
    "collection": "tournaments",
    "keys": [("year", 1)],
    "name": "tournaments_year"
}, {
    "collection": "tournaments",
    "keys": [("status", 1)],
    "name": "tournaments_status"
}, {
    "collection": "teams",
    "keys": [("slug", 1)],
    "unique": True,
    "name": "teams_slug_unique"
}, {
    "collection": "teams",
    "keys": [("name", 1)],
    "name": "teams_name"
}, {
    "collection": "teams",
    "keys": [("region", 1)],
    "name": "teams_region"
}, {
    "collection": "matches",
    "keys": [("tournamentId", 1), ("scheduledAt", 1)],
    "name": "matches_tournament_scheduled"
}, {
    "collection": "matches",
    "keys": [("tournamentId", 1), ("status", 1)],
    "name": "matches_tournament_status"
}, {
    "collection": "matches",
    "keys": [("teams.teamAId", 1)],
    "name": "matches_team_a"
}, {
    "collection": "matches",
    "keys": [("teams.teamBId", 1)],
    "name": "matches_team_b"
}, {
    "collection": "matches",
    "keys": [("externalMatchId", 1)],
    "name": "matches_external_id"
}, {
    "collection": "matches",
    "keys": [("source.provider", 1), ("externalMatchId", 1)],
    "unique": True,
    "name": "matches_source_external_unique",
    "partialFilterExpression": {
        "source.provider": {
            "$type": "string",
            "$gt": ""
        },
        "externalMatchId": {
            "$type": "string",
            "$gt": ""
        }
    }
}]
MUTABLE_PLAN_FIELDS = {
    "execution", "executionMode", "writePlanPath", "planDigest"
}
PRESERVE_MISSING_VALUE_PATHS = {
    "tournaments": {
        "currentStage", "location.city", "location.country",
        "source.externalId", "source.url"
    },
    "teams": {"logoUrl", "source.externalId", "source.url"},
    "matches": {
        "group", "round", "stage", "bestOf", "status", "startedAt",
        "completedAt", "winnerTeamId", "score.teamA", "score.teamB",
        "source.url"
    }
}


def _iso_utc(value: datetime) -> str:
  if value.tzinfo is None or value.utcoffset() is None:
    value = value.replace(tzinfo=timezone.utc)

  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
  if not value:
    return None

  normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
  parsed = datetime.fromisoformat(normalized)

  if parsed.tzinfo is None or parsed.utcoffset() is None:
    return None

  return parsed.astimezone(timezone.utc)


def _name_key(value: str) -> str:
  return re.sub(r"[^a-z0-9]", "", value.casefold())


def _canonical_team_name(value: str) -> str:
  return TEAM_CANONICAL_NAMES.get(_name_key(value), value.strip())


def _canonical_team_key(value: str | None) -> str:
  if not isinstance(value, str):
    raise ValueError("Canonical Team name must be a string.")

  return _name_key(_canonical_team_name(value))


def _is_placeholder_team(value: str | None) -> bool:
  return not isinstance(
      value, str) or not value.strip() or value.strip().casefold() in {
          "bye", "tba", "tbd", "unknown"
      }


def _index_operations_for_plan() -> list[dict]:
  return [{
      **{
          key: value
          for key, value in operation.items() if key != "keys"
      }, "keys": [[field, direction] for field, direction in operation["keys"]]
  } for operation in INDEX_OPERATIONS]


def calculate_plan_digest(plan: dict) -> str:
  payload = {
      key: value
      for key, value in plan.items() if key not in MUTABLE_PLAN_FIELDS
  }
  encoded = json.dumps(payload,
                       ensure_ascii=False,
                       sort_keys=True,
                       separators=(",", ":"),
                       allow_nan=False)

  return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def verify_plan_integrity(plan: dict, plan_path: Path) -> list[str]:
  errors = []
  stored_digest = plan.get("planDigest")

  if not isinstance(stored_digest, str) or not stored_digest:
    errors.append(
        "Write Plan payload digest is missing; regenerate and review a new Write Plan."
    )
  elif calculate_plan_digest(plan) != stored_digest:
    errors.append(
        "Write Plan payload changed after review; regenerate and review a new Write Plan."
    )

  source_digests = plan.get("sourceDigests") or {}
  cache_identity = plan.get("liquipediaCacheIdentity") or {}
  sources = [("reconciliation report", plan.get("reconciliationReportPath"),
              source_digests.get("reconciliationReportSha256"))]

  if cache_identity.get("status") != "live-no-cache":
    sources.insert(1, ("Liquipedia cache", cache_identity.get("path"),
                       source_digests.get("liquipediaCacheSha256")))

  changed_sources = []

  for label, raw_path, expected_digest in sources:
    if not raw_path or not expected_digest:
      changed_sources.append(f"{label} (digest or path missing)")
      continue

    try:
      actual_digest = sha256_file(Path(raw_path).expanduser().resolve())
    except OSError:
      changed_sources.append(f"{label} (file missing or unreadable)")
      continue

    if actual_digest.casefold() != expected_digest.casefold():
      changed_sources.append(label)

  if changed_sources:
    errors.append("Source digest mismatch: " + ", ".join(changed_sources) +
                  ". Regenerate and review a new Write Plan.")

  cache_digest = (plan.get("liquipediaCacheIdentity") or {}).get("cacheSha256")
  duckdb_digest = (plan.get("duckdb") or {}).get("sha256")

  if cache_digest != source_digests.get("liquipediaCacheSha256"):
    errors.append(
        "Write Plan Liquipedia cache digest fields disagree; regenerate and review a new Write Plan."
    )

  if duckdb_digest != source_digests.get("duckdbSha256"):
    errors.append(
        "Write Plan DuckDB digest fields disagree; regenerate and review a new Write Plan."
    )

  try:
    report_path = Path(plan.get("reconciliationReportPath")
                       or "").expanduser().resolve()
    report_payload = json.loads(report_path.read_text(encoding="utf-8"))
    report_content_digest = ((report_payload.get("sources")
                              or {}).get("liquipedia")
                             or {}).get("contentSha256")
  except (OSError, UnicodeError, json.JSONDecodeError, AttributeError,
          TypeError):
    report_content_digest = None

  expected_content_digest = source_digests.get("liquipediaContentSha256")

  if ((expected_content_digest is not None and
       (not isinstance(expected_content_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_content_digest) is None
        or expected_content_digest != report_content_digest))
      or (cache_identity.get("status") == "live-no-cache"
          and expected_content_digest is None)):
    errors.append(
        "Write Plan Liquipedia source content digest is missing or inconsistent; regenerate and review a new Write Plan."
    )

  if (cache_identity.get("status") == "live-no-cache"
      and (cache_identity.get("path") is not None
           or cache_identity.get("cacheSha256") is not None)):
    errors.append(
        "Live-no-cache Write Plan unexpectedly references a Liquipedia cache file."
    )

  if not plan_path.is_file():
    errors.append("Specified Write Plan file is missing or unreadable.")

  return errors


def _read_duckdb_metadata(database_path: Path, event_slug: str,
                          team_ids: set[str]) -> tuple[dict, dict[str, dict]]:
  connection = duckdb.connect(str(database_path), read_only=True)

  try:
    event_row = connection.execute(
        """
      SELECT MIN(utc_timestamp), MAX(utc_timestamp), CAST(MAX(event_id) AS VARCHAR),
        MAX(event), MAX(event_url)
      FROM matches
      WHERE event_slug = ?
      """, [event_slug]).fetchone()
    team_rows = []

    if team_ids:
      ids = sorted({int(team_id) for team_id in team_ids})
      placeholders = ", ".join("?" for _ in ids)
      team_rows = connection.execute(
          f"SELECT team_id, team_name, team_url, country FROM teams WHERE team_id IN ({placeholders})",
          ids).fetchall()
  finally:
    connection.close()

  event = {
      "startDate":
      _iso_utc(event_row[0]) if event_row and event_row[0] else None,
      "endDate":
      _iso_utc(event_row[1]) if event_row and event_row[1] else None,
      "externalId": event_row[2] if event_row else None,
      "name": event_row[3] if event_row else None,
      "url": event_row[4] if event_row else None
  }
  teams = {
      str(row[0]): {
          "externalId": str(row[0]),
          "name": row[1],
          "url": row[2],
          "country": row[3]
      }
      for row in team_rows
  }

  return event, teams


def _liquipedia_cache_identity(settings: Settings, page_title: str,
                               fetched_at: str | None) -> dict:
  identity = {
      "apiUrl": settings.liquipedia_api_url,
      "pageTitle": page_title,
      "action": "parse",
      "prop": "text|sections",
      "formatVersion": "2"
  }
  cache_key = hashlib.sha256(
      json.dumps(identity, sort_keys=True).encode("utf-8")).hexdigest()
  cache_path = settings.cache_directory / "liquipedia" / f"{cache_key}.json"

  return {
      "requestIdentity": identity,
      "cacheFile": cache_path.name,
      "path": str(cache_path.resolve()),
      "cacheSha256": sha256_file(cache_path) if cache_path.is_file() else None,
      "fetchedAt": fetched_at,
      "status": "cache-offline"
  }


def _participant_team_keys(report: dict) -> set[str]:
  return {
      _canonical_team_key(team.get("name"))
      for match in report.get("matches", [])
      for team in ((match.get("liquipediaIdentity") or {}).get("teams") or [])
      if isinstance(team, dict) and not _is_placeholder_team(team.get("name"))
  }


def _duckdb_participant_ids(report: dict,
                            participant_keys: set[str]) -> set[str]:
  result = set()

  for match in report.get("matches", []):
    identity = match.get("duckdbIdentity") or {}

    for team in identity.get("teams") or []:
      if (not isinstance(team, dict) or not team.get("externalId")
          or _is_placeholder_team(team.get("name"))):
        continue

      if _canonical_team_key(team.get("name")) in participant_keys:
        result.add(str(team["externalId"]))

  return result


def _make_tournament(report: dict, generated_at: datetime) -> dict:
  event = report.get("event") or {}
  event_slug = event.get("slug")
  page = event.get("liquipediaPage")

  if not isinstance(event_slug, str) or not event_slug.strip():
    raise ValueError("Liquipedia event slug is missing.")

  year_match = re.search(r"(?:^|\D)(20\d{2})(?:\D|$)", event_slug)
  year = int(year_match.group(1)) if year_match else generated_at.year
  name = event.get("name")

  if not isinstance(name, str) or not name.strip():
    tail = (page or event_slug).rstrip("/").split("/")[-1].replace("_", " ")
    name = f"VALORANT {tail} {year}".strip()

  canonical_matches = [
      match for match in report.get("matches", [])
      if match.get("liquipediaIdentity")
  ]
  schedule_statuses = [
      match["liquipediaIdentity"].get("status") for match in canonical_matches
  ]

  if schedule_statuses and all(status == "completed"
                               for status in schedule_statuses):
    status = "completed"
  elif any(status in {"scheduled", "live"} for status in schedule_statuses):
    status = "ongoing" if any(item == "completed" for item in schedule_statuses
                              ) or generated_at.year == year else "upcoming"
  else:
    status = None

  future_stages = []
  past_stages = []

  for match in canonical_matches:
    identity = match["liquipediaIdentity"]
    scheduled_at = _parse_utc(identity.get("scheduledAt"))

    if scheduled_at and identity.get("stage"):
      destination = future_stages if scheduled_at >= generated_at else past_stages
      destination.append((scheduled_at, identity["stage"]))

  current_stage = min(future_stages)[1] if future_stages else (
      max(past_stages)[1] if past_stages else None)
  event_url = f"https://liquipedia.net/valorant/{page}" if isinstance(
      page, str) and page else None

  return {
      "slug":
      event_slug,
      "name":
      name,
      "shortName":
      f"Champions {year}" if "champions" in event_slug.casefold() else name,
      "year":
      year,
      "location": {
          "city": None,
          "country": None
      },
      "startDate":
      None,
      "endDate":
      None,
      "status":
      status,
      "currentStage":
      current_stage,
      "source": {
          "provider": "liquipedia",
          "externalId": None,
          "url": event_url
      }
  }


def _make_team_operations(
    source_teams: dict[str, dict], liquipedia_matches: list[dict],
    blocking_errors: list[str],
    warnings: list[str]) -> tuple[list[dict], dict[str, str], dict[str, str]]:
  operations = []
  slugs_by_external_id = {}
  slugs_by_name = {}
  owner_by_slug = {}
  participants: dict[str, str] = {}

  for match in liquipedia_matches:
    identity = match.get("liquipediaIdentity") or {}

    for team in identity.get("teams") or []:
      name = team.get("name")

      if not _is_placeholder_team(name):
        canonical_name = _canonical_team_name(name)
        participants[_canonical_team_key(canonical_name)] = canonical_name

  source_by_name: dict[str, tuple[str, dict]] = {}
  ambiguous_source_names: set[str] = set()

  for external_id, source in source_teams.items():
    source_name = source.get("name")

    if _is_placeholder_team(source_name):
      continue

    key = _canonical_team_key(source_name)

    if key not in participants:
      continue

    if key in ambiguous_source_names:
      continue

    existing = source_by_name.get(key)

    if existing is not None and existing[0] != external_id:
      warnings.append(
          f"DuckDB has multiple Team identities for canonical Liquipedia Team '{participants[key]}'; schedule uses its explicit Team slug."
      )
      source_by_name.pop(key, None)
      ambiguous_source_names.add(key)
      continue

    source_by_name[key] = (external_id, source)

  for key, name in sorted(participants.items()):
    source_entry = source_by_name.get(key)
    external_id, source = source_entry if source_entry else (None, {})
    slug = TEAM_SLUGS.get(key)

    if not slug:
      warnings.append(
          f"Liquipedia Team '{name}' has no explicit canonical slug mapping; its match participants will remain unresolved."
      )
      continue

    owner = owner_by_slug.get(slug)

    if owner is not None and owner != key:
      warnings.append(
          f"Liquipedia Team '{name}' could not be safely assigned canonical slug '{slug}'."
      )
      continue

    owner_by_slug[slug] = key
    slugs_by_name[key] = slug

    if external_id:
      slugs_by_external_id[external_id] = slug

    document = {
        "slug": slug,
        "name": name,
        "shortName": TEAM_SHORT_NAMES.get(key, name),
        "region": None,
        "logoUrl": None
    }

    operations.append({
        "operation": "upsert",
        "key": {
            "slug": slug
        },
        "sourceExternalId": external_id,
        "document": document
    })

  return operations, slugs_by_external_id, slugs_by_name


def _match_document_template(match: dict, team_slugs_by_name: dict[str, str],
                             tournament_slug: str,
                             errors: list[str]) -> dict | None:
  identity = match.get("liquipediaIdentity") or {}
  match_id = identity.get("externalId")
  source_teams = identity.get("teams") or []
  names = [
      team.get("name") if isinstance(team, dict) else None
      for team in source_teams
  ]
  status = identity.get("status")
  scheduled_at = identity.get("scheduledAt")
  missing = list(match.get("scheduleBlockingReasons") or [])

  if not isinstance(match_id, str) or not match_id.strip():
    missing.append("externalMatchId")

  parsed_scheduled_at = _parse_utc(scheduled_at)

  if parsed_scheduled_at is None:
    missing.append("scheduledAt")

  if identity.get("eventSlug") != tournament_slug:
    missing.append("event identity")

  if not tournament_slug:
    missing.append("Tournament")

  stage = identity.get("stage")
  best_of = identity.get("bestOf")

  if status is not None and status not in MATCH_STATUSES:
    missing.append("unsupported status")

  if best_of is not None and (not isinstance(best_of, int) or best_of < 1):
    missing.append("invalid bestOf")

  resolved_team_slugs = []

  for name in names[:2]:
    if _is_placeholder_team(name):
      resolved_team_slugs.append(None)
      continue

    canonical_key = _canonical_team_key(name)
    resolved_slug = team_slugs_by_name.get(canonical_key)

    if not resolved_slug:
      resolved_team_slugs.append(None)
      continue

    resolved_team_slugs.append(resolved_slug)

  if len(names) != 2:
    missing.append("Liquipedia participants must contain exactly two slots")

  if (len(resolved_team_slugs) == 2 and resolved_team_slugs[0]
      and resolved_team_slugs[1]
      and resolved_team_slugs[0] == resolved_team_slugs[1]):
    missing.append("both participants resolve to the same Team")

  scores = [identity.get("scoreA"), identity.get("scoreB")]

  if (scores[0] is None) != (scores[1] is None):
    missing.append("series score has only one side")
  elif scores[0] is not None and (
      not isinstance(scores[0], int) or not isinstance(scores[1], int)
      or scores[0] < 0 or scores[1] < 0 or
      (status == "completed" and scores[0] == scores[1])):
    missing.append("series score is contradictory")
  elif status == "scheduled" and scores[0] is not None:
    missing.append("scheduled status conflicts with a series score")
  elif status == "completed":
    source_winner = identity.get("winner")
    winner_key = _canonical_team_key(source_winner) if isinstance(
        source_winner, str) else None
    explicit_winner_slug = team_slugs_by_name.get(
        winner_key) if winner_key else None
    forfeit_result = identity.get("forfeitResult") is True

    if source_winner is not None and explicit_winner_slug not in resolved_team_slugs:
      missing.append(
          "completed winner does not match either Liquipedia participant")

    if scores[0] is None:
      if not (forfeit_result and explicit_winner_slug):
        missing.append("completed Match has no verified series result")
    elif (isinstance(best_of, int) and isinstance(scores[0], int)
          and isinstance(scores[1], int) and scores[0] != scores[1]):
      wins_required = best_of // 2 + 1
      winner_score, loser_score = sorted((scores[0], scores[1]), reverse=True)
      score_winner_slug = resolved_team_slugs[0] if scores[0] > scores[
          1] else resolved_team_slugs[1]

      if forfeit_result:
        if not explicit_winner_slug or explicit_winner_slug != score_winner_slug:
          missing.append("forfeit winner contradicts its series score")
      elif winner_score != wins_required or loser_score >= wins_required:
        missing.append("completed score contradicts bestOf")
      elif explicit_winner_slug and explicit_winner_slug != score_winner_slug:
        missing.append("completed winner contradicts its series score")
    elif scores[0] is not None and not forfeit_result:
      missing.append("completed result cannot be verified without bestOf")
    elif isinstance(scores[0], int) and isinstance(scores[1],
                                                   int) and forfeit_result:
      score_winner_slug = resolved_team_slugs[0] if scores[0] > scores[
          1] else resolved_team_slugs[1]

      if not explicit_winner_slug or explicit_winner_slug != score_winner_slug:
        missing.append("forfeit winner contradicts its series score")

  if missing:
    errors.extend(missing)

    return None

  winner_team_slug = None

  if status == "completed":
    source_winner = identity.get("winner")
    winner_key = _canonical_team_key(source_winner) if isinstance(
        source_winner, str) else None
    winner_team_slug = team_slugs_by_name.get(
        winner_key) if winner_key else None

    if (winner_team_slug is None and isinstance(scores[0], int)
        and isinstance(scores[1], int) and scores[0] != scores[1]):
      winner_team_slug = resolved_team_slugs[0] if scores[0] > scores[
          1] else resolved_team_slugs[1]

  return {
      "tournamentSlug":
      tournament_slug,
      "externalMatchId":
      match_id,
      "stage":
      stage,
      "group":
      identity.get("groupName"),
      "round":
      identity.get("roundName"),
      "bestOf":
      best_of,
      "scheduledAt":
      _iso_utc(parsed_scheduled_at)
      if parsed_scheduled_at is not None else None,
      "status":
      status,
      "teams": {
          "teamASlug": resolved_team_slugs[0],
          "teamBSlug": resolved_team_slugs[1]
      },
      "score": {
          "teamA": scores[0],
          "teamB": scores[1]
      },
      "winnerTeamSlug":
      winner_team_slug,
      "forfeitResult":
      identity.get("forfeitResult") is True,
      "source": {
          "provider": "liquipedia",
          "url": identity.get("sourceUrl")
      }
  }


def build_write_plan(settings: Settings, report: dict,
                     reconciliation_path: Path, database_path: Path) -> dict:
  generated_at = datetime.now(timezone.utc)
  run_id = str(uuid.uuid4())
  blocking_errors = []
  warnings = []
  participants = _participant_team_keys(report)
  duckdb_ids = _duckdb_participant_ids(report, participants)

  event_source = {}
  source_teams = {}

  try:
    event_source, source_teams = _read_duckdb_metadata(database_path,
                                                       report["event"]["slug"],
                                                       duckdb_ids)
  except (duckdb.Error, OSError, ValueError) as error:
    warnings.append(
        f"DuckDB Team enrichment was unavailable ({type(error).__name__}); Liquipedia Schedule remains eligible."
    )

  tournament_document = None

  try:
    tournament_document = _make_tournament(report, generated_at)
  except (KeyError, ValueError, TypeError) as error:
    blocking_errors.append(f"Tournament payload is incomplete: {error}")

  team_operations, _, team_slugs = _make_team_operations(
      source_teams, report.get("matches", []), blocking_errors, warnings)
  match_operations = []
  liquipedia_id_counts: dict[str, int] = {}

  for item in report.get("matches", []):
    identity = item.get("liquipediaIdentity") or {}
    external_id = identity.get("externalId")

    if isinstance(external_id, str) and external_id.strip():
      liquipedia_id_counts[external_id] = liquipedia_id_counts.get(
          external_id, 0) + 1

  for match in report.get("matches", []):
    status = match.get("status")
    identity = match.get("liquipediaIdentity") or {}
    match_id = identity.get("externalId") or ((match.get("duckdbIdentity")
                                               or {}).get("externalId"))

    if not identity:
      match_operations.append({
          "operation":
          "skip",
          "reconciliationStatus":
          status,
          "scheduleStatus":
          "not_applicable",
          "statsLinkStatus":
          match.get("statsLinkStatus", "unmatched"),
          "liquipediaExternalMatchId":
          identity.get("externalId"),
          "duckdbExternalMatchId": (match.get("duckdbIdentity")
                                    or {}).get("externalId"),
          "skipReason":
          "DuckDB-only record has no Liquipedia canonical Schedule entry."
      })
      continue

    blocking_reasons = list(match.get("scheduleBlockingReasons") or [])

    if liquipedia_id_counts.get(identity.get("externalId") or "") != 1:
      if "Liquipedia external match ID is missing or duplicated." not in blocking_reasons:
        blocking_reasons.append(
            "Liquipedia external match ID is missing or duplicated.")

    if match.get("scheduleStatus") == "blocked" and not blocking_reasons:
      blocking_reasons.append(
          "Liquipedia source validation blocked this Match.")

    if blocking_reasons:
      match_operations.append({
          "operation":
          "skip",
          "reconciliationStatus":
          status,
          "scheduleStatus":
          "blocked",
          "statsLinkStatus":
          match.get("statsLinkStatus"),
          "liquipediaExternalMatchId":
          identity.get("externalId"),
          "externalMatchId":
          identity.get("externalId"),
          "blockingReasons":
          blocking_reasons,
          "skipReason":
          "; ".join(blocking_reasons)
      })
      continue

    match_errors: list[str] = []
    template = _match_document_template(match, team_slugs,
                                        report["event"]["slug"], match_errors)

    if template is None:
      match_operations.append({
          "operation":
          "skip",
          "reconciliationStatus":
          status,
          "scheduleStatus":
          "blocked",
          "statsLinkStatus":
          match.get("statsLinkStatus"),
          "externalMatchId":
          match_id,
          "blockingReasons":
          match_errors,
          "skipReason":
          "; ".join(match_errors)
          or "Liquipedia Schedule payload validation failed."
      })
      continue

    match_operations.append({
        "operation":
        "upsert",
        "reconciliationStatus":
        status,
        "scheduleStatus":
        match.get("scheduleStatus", "ready"),
        "statsLinkStatus":
        match.get("statsLinkStatus", "unmatched"),
        "externalMatchId":
        template["externalMatchId"],
        "documentTemplate":
        template
    })

    for difference in match.get("differences") or []:
      if difference.get("field") not in {"candidateMatches", "identity"}:
        warnings.append(
            f"Match {template['externalMatchId']} warning: {difference.get('field')} differs "
            f"(Liquipedia={json.dumps(difference.get('liquipedia'), ensure_ascii=False)}, "
            f"DuckDB={json.dumps(difference.get('duckdb'), ensure_ascii=False)}; "
            f"priority={difference.get('priority', 'normal')}).")

  for error in report.get("errors", []):
    if error.get("provider") == "liquipedia":
      blocking_errors.append(
          "Liquipedia source error; inspect the reconciliation report.")
    else:
      warnings.append(
          "DuckDB Stats source error; canonical Liquipedia Schedule remains available."
      )

  if not tournament_document:
    blocking_errors.append(
        "Tournament document is unavailable; accepted matches cannot be applied."
    )

  match_upserts = sum(operation["operation"] == "upsert"
                      for operation in match_operations)
  match_skips = sum(operation["operation"] == "skip"
                    and operation.get("scheduleStatus") == "blocked"
                    for operation in match_operations)
  non_schedule_rows = sum(
      operation.get("scheduleStatus") == "not_applicable"
      for operation in match_operations)
  tbd_matches = sum(
      operation.get("operation") == "upsert" and any(
          value is None
          for value in (operation.get("documentTemplate", {}).get(
              "teams", {}).get("teamASlug"),
                        operation.get("documentTemplate", {}).get(
                            "teams", {}).get("teamBSlug")))
      for operation in match_operations)
  planned_ready = sum(
      operation.get("operation") == "upsert"
      and operation.get("scheduleStatus") == "ready"
      for operation in match_operations)
  planned_warning = sum(
      operation.get("operation") == "upsert"
      and operation.get("scheduleStatus") == "warning"
      for operation in match_operations)
  upsert_count = (1 if tournament_document else
                  0) + len(team_operations) + match_upserts
  source_counts = dict(report.get("summary", {}))
  liquipedia_source = report.get("sources", {}).get("liquipedia", {})
  cache = _liquipedia_cache_identity(settings,
                                     report["event"]["liquipediaPage"],
                                     liquipedia_source.get("fetchedAt"))
  cache["status"] = liquipedia_source.get("cacheStatus") or cache["status"]

  if liquipedia_source.get("cacheStatus") == "live-no-cache":
    cache.update({
        "cacheFile": None,
        "path": None,
        "cacheSha256": None,
        "status": "live-no-cache"
    })
  elif cache["cacheSha256"] is None:
    blocking_errors.append(
        f"Offline Liquipedia cache file '{cache['cacheFile']}' is missing.")

  reconciliation_sha256 = sha256_file(
      reconciliation_path) if reconciliation_path.is_file() else None

  if reconciliation_sha256 is None:
    blocking_errors.append(
        "Reconciliation report file is missing or unreadable.")

  try:
    duckdb_sha256 = sha256_file(
        database_path) if database_path.is_file() else None
  except OSError:
    duckdb_sha256 = None

  if duckdb_sha256 is None:
    warnings.append(
        "DuckDB Stats source identity is unavailable; Schedule operations are independent of DuckDB."
    )

  plan = {
      "runId": run_id,
      "generatedAt": _iso_utc(generated_at),
      "eventSlug": report["event"]["slug"],
      "reconciliationReportPath": str(reconciliation_path.resolve()),
      "liquipediaCacheIdentity": cache,
      "duckdb": {
          "path": str(database_path.resolve()),
          "sha256": duckdb_sha256,
          "readOnly": True
      },
      "sourceDigests": {
          "reconciliationReportSha256": reconciliation_sha256,
          "liquipediaCacheSha256": cache["cacheSha256"],
          "liquipediaContentSha256": liquipedia_source.get("contentSha256"),
          "duckdbSha256": duckdb_sha256
      },
      "executionMode": "dry-run",
      "tournamentOperation": {
          "operation": "upsert" if tournament_document else "skip",
          "key": {
              "slug": report["event"]["slug"]
          },
          "document": tournament_document
      },
      "indexOperations": _index_operations_for_plan(),
      "teamOperations": team_operations,
      "matchOperations": match_operations,
      "reconciliationStatusCounts": source_counts,
      "scheduleCounts": {
          "ready": planned_ready,
          "warning": planned_warning,
          "blocked": match_skips,
          "plannedUpserts": match_upserts,
          "nonCanonicalDuckdbOnly": non_schedule_rows,
          "tbdMatches": tbd_matches
      },
      "statsCoverage": {
          "available": source_counts.get("statsAvailable", 0),
          "unavailable": source_counts.get("statsUnavailable", 0),
          "ambiguous": source_counts.get("statsAmbiguous", 0),
          "incomplete": source_counts.get("statsIncomplete", 0),
          "unmatched": source_counts.get("statsUnmatched", 0),
          "conflict": source_counts.get("statsConflict", 0),
          "matchedWithoutStats": source_counts.get("statsMatched", 0)
      },
      "plannedCounts": {
          "create":
          None,
          "update":
          None,
          "unchanged":
          None,
          "skip":
          match_skips,
          "upsert":
          upsert_count,
          "createUpdateUnchangedNote":
          "These outcomes require a MongoDB read; dry-run does not connect, so they remain unknown."
      },
      "entityCounts": {
          "tournamentUpserts": 1 if tournament_document else 0,
          "teamUpserts": len(team_operations),
          "matchUpserts": match_upserts,
          "matchSkips": match_skips,
          "duckdbOnlyIgnored": non_schedule_rows
      },
      "warnings": warnings,
      "blockingErrors": blocking_errors
  }

  return plan


def write_plan_file(plan: dict, output_directory: Path) -> Path:
  created_at = _parse_utc(plan["generatedAt"])

  if created_at is None:
    raise ValueError("Write Plan generatedAt must include a time zone.")

  filename = f"import-plan-{created_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  path = output_directory / filename
  plan["writePlanPath"] = str(path.resolve())
  plan["planDigest"] = calculate_plan_digest(plan)
  write_json_atomic(path, plan)

  return path


def _validate_plan(plan: dict) -> list[str]:
  errors = list(plan.get("blockingErrors", []))
  tournament = (plan.get("tournamentOperation") or {}).get("document")

  if not tournament or not all(
      tournament.get(field) for field in ("slug", "name")):
    errors.append(
        "Apply validation failed: tournament payload is missing required fields."
    )
  elif plan["tournamentOperation"].get("operation") != "upsert":
    errors.append(
        "Apply validation failed: tournament operation is not an upsert.")
  elif tournament.get("status") not in {
      None, "upcoming", "ongoing", "completed"
  }:
    errors.append("Apply validation failed: tournament status is invalid.")
  elif any(
      tournament.get(field) is not None
      and _parse_utc(tournament.get(field)) is None
      for field in ("startDate", "endDate")):
    errors.append(
        "Apply validation failed: tournament dates are not timezone-aware UTC values."
    )

  if plan.get("indexOperations") != _index_operations_for_plan():
    errors.append(
        "Apply validation failed: planned MongoDB index definitions are missing or unsupported."
    )

  team_slugs = set()

  for operation in plan.get("teamOperations", []):
    document = operation.get("document") or {}

    if operation.get("operation") != "upsert":
      errors.append(
          "Apply validation failed: a Team operation is not an upsert.")

    if not all(document.get(field) for field in ("slug", "name", "shortName")):
      errors.append(
          "Apply validation failed: Team payload is missing required fields.")

    if document.get("region") not in {
        None, "americas", "emea", "pacific", "china", "other"
    }:
      errors.append(
          f"Apply validation failed: Team '{document.get('slug')}' has an invalid region."
      )

    if document.get("slug") in team_slugs:
      errors.append(
          f"Apply validation failed: duplicate Team slug '{document.get('slug')}'."
      )

    team_slugs.add(document.get("slug"))

  match_ids = set()

  for operation in plan.get("matchOperations", []):
    if operation.get("operation") != "upsert":
      if operation.get("scheduleStatus"
                       ) == "blocked" and not operation.get("blockingReasons"):
        errors.append(
            f"Apply validation failed: blocked Schedule match {operation.get('externalMatchId')} has no recorded reason."
        )

      continue

    if operation.get("scheduleStatus") not in {"ready", "warning"}:
      errors.append(
          f"Apply validation failed: Schedule match {operation.get('externalMatchId')} is not ready or warning-only."
      )
      continue

    template = operation.get("documentTemplate") or {}
    errors.extend(
        _validate_match_template(template, operation.get("externalMatchId")))
    match_id = template.get("externalMatchId")

    if match_id != operation.get("externalMatchId"):
      errors.append(
          f"Apply validation failed: match operation identity does not match its payload ({match_id})."
      )

    team_refs = template.get("teams") or {}

    for team_slug in (team_refs.get("teamASlug"), team_refs.get("teamBSlug")):
      if team_slug is None:
        continue

      if team_slug not in team_slugs:
        errors.append(
            f"Apply validation failed: match {match_id} references an unplanned Team '{team_slug}'."
        )

    if match_id in match_ids:
      errors.append(
          f"Apply validation failed: duplicate Liquipedia match ID '{match_id}'."
      )

    match_ids.add(match_id)

  return errors


def _validate_match_template(template: dict,
                             match_id: str | None) -> list[str]:
  errors = []

  if not template.get("tournamentSlug"):
    errors.append(f"Match {match_id} has no Tournament reference.")

  if not isinstance(match_id, str) or not match_id.strip():
    errors.append("A match has no externalMatchId.")

  if _parse_utc(template.get("scheduledAt")) is None:
    errors.append(f"Match {match_id} has no valid UTC scheduledAt.")

  if template.get("status") is not None and template.get(
      "status") not in MATCH_STATUSES:
    errors.append(f"Match {match_id} has an illegal status.")

  teams = template.get("teams") or {}

  if teams.get("teamASlug") and teams.get("teamASlug") == teams.get(
      "teamBSlug"):
    errors.append(
        f"Match {match_id} resolves both participants to the same Team.")

  if (template.get("bestOf") is not None and
      (not isinstance(template.get("bestOf"), int) or template["bestOf"] < 1)):
    errors.append(f"Match {match_id} has invalid bestOf.")

  scores = template.get("score") or {}
  score_a = scores.get("teamA")
  score_b = scores.get("teamB")

  if (score_a is None) != (score_b is None):
    errors.append(f"Match {match_id} has a partial series score.")
  elif score_a is not None and (
      not isinstance(score_a, int) or not isinstance(score_b, int)
      or score_a < 0 or score_b < 0 or
      (template.get("status") == "completed" and score_a == score_b)):
    errors.append(f"Match {match_id} has an invalid series score.")

  if template.get("status") == "scheduled" and (score_a is not None
                                                or score_b is not None):
    errors.append(
        f"Scheduled match {match_id} unexpectedly has a series score.")

  if template.get("status") == "completed":
    forfeit_result = template.get("forfeitResult") is True

    if score_a is None and (not forfeit_result
                            or not template.get("winnerTeamSlug")):
      errors.append(f"Completed match {match_id} has no verified result.")

    if (isinstance(score_a, int) and isinstance(score_b, int)
        and isinstance(template.get("bestOf"), int) and score_a != score_b):
      wins_required = template["bestOf"] // 2 + 1
      winner_score, loser_score = sorted((score_a, score_b), reverse=True)

      if forfeit_result:
        teams = template.get("teams") or {}
        expected_winner = teams.get(
            "teamASlug") if score_a > score_b else teams.get("teamBSlug")

        if not template.get("winnerTeamSlug") or template.get(
            "winnerTeamSlug") != expected_winner:
          errors.append(
              f"Forfeit result for match {match_id} contradicts its series score."
          )
      elif winner_score != wins_required or loser_score >= wins_required:
        errors.append(f"Completed match {match_id} score contradicts bestOf.")

    if template.get("winnerTeamSlug"):
      teams = template.get("teams") or {}
      expected_winner = teams.get(
          "teamASlug"
      ) if score_a is not None and score_b is not None and score_a > score_b else (
          teams.get("teamBSlug") if score_a is not None and score_b is not None
          and score_b > score_a else None)

      if expected_winner and template.get("winnerTeamSlug") != expected_winner:
        errors.append(
            f"Completed match {match_id} winner contradicts its canonical score."
        )

  return errors


def _has_value(value) -> bool:
  return value is not None and not (isinstance(value, str) and value == "")


def _merge_value(collection_name: str, path: str, current, planned):
  if isinstance(planned, dict):
    current_object = current if isinstance(current, dict) else {}
    merged = dict(current_object)

    for key, value in planned.items():
      child_path = f"{path}.{key}" if path else key
      merged[key] = _merge_value(collection_name, child_path,
                                 current_object.get(key), value)

    return merged

  policy_paths = PRESERVE_MISSING_VALUE_PATHS[collection_name]

  if (collection_name == "matches" and path == "status"
      and current == "completed" and planned != "completed"):
    return current

  if planned is None and (path in policy_paths or _has_value(current)):
    return current

  if planned == "" and isinstance(current, str) and current:
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


def _merge_managed_document(collection_name: str, current: dict | None,
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


def _upsert(collection, collection_name: str, filter_query: dict,
            document: dict, now: datetime) -> tuple[str, object]:
  current = collection.find_one(filter_query)

  if current is None:
    insert_document, _ = _merge_managed_document(collection_name, None,
                                                 document)
    result = collection.update_one(filter_query, {
        "$setOnInsert": {
            **insert_document, "createdAt": now,
            "updatedAt": now
        }
    },
                                   upsert=True)

    if result.upserted_id is not None:
      return "created", result.upserted_id

    current = collection.find_one(filter_query)

    if current is None:
      raise RuntimeError("Upsert did not create or find its target document.")

  merged, changed_fields = _merge_managed_document(collection_name, current,
                                                   document)

  if not changed_fields:
    return "unchanged", current["_id"]

  changed_values = {key: merged[key] for key in changed_fields}
  collection.update_one(filter_query,
                        {"$set": {
                            **changed_values, "updatedAt": now
                        }},
                        upsert=False)

  return "updated", current["_id"]


def _ensure_indexes(database, operations: list[dict]) -> None:
  for operation in operations:
    options = {
        key: value
        for key, value in operation.items()
        if key not in {"collection", "keys"}
    }
    keys = [tuple(key) for key in operation["keys"]]
    database[operation["collection"]].create_index(keys, **options)


def load_write_plan(path: Path) -> dict:
  try:
    payload = json.loads(
        path.expanduser().resolve().read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError) as error:
    raise ValueError(
        "Specified Write Plan is missing or invalid JSON.") from error

  if not isinstance(payload, dict):
    raise ValueError("Specified Write Plan must contain a JSON object.")

  return payload


def _json_safe(value):
  if isinstance(value, dict):
    return {str(key): _json_safe(item) for key, item in value.items()}

  if isinstance(value, (list, tuple)):
    return [_json_safe(item) for item in value]

  if isinstance(value, (str, int, float, bool)) or value is None:
    return value

  if hasattr(value, "isoformat"):
    return value.isoformat()

  return str(value)


def _normalized_definition(value):
  if isinstance(value, dict):
    return {key: _normalized_definition(value[key]) for key in sorted(value)}

  if isinstance(value, (list, tuple)):
    return [_normalized_definition(item) for item in value]

  return value


def _index_matches_plan(existing: dict, planned: dict) -> bool:
  expected_keys = [tuple(key) for key in planned["keys"]]
  actual_keys = list(existing.get("key", {}).items())

  if actual_keys != expected_keys:
    return False

  option_names = ("unique", "sparse", "expireAfterSeconds",
                  "partialFilterExpression", "collation", "wildcardProjection",
                  "hidden", "weights", "default_language", "language_override")

  for name in option_names:
    if _normalized_definition(existing.get(name)) != _normalized_definition(
        planned.get(name)):
      return False

  return True


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
  checks = [("tournaments", "slug", [{
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
            ("teams", "slug", [{
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
            ("matches", "source.provider + externalMatchId", [{
                "$match": {
                    "source.provider": {
                        "$type": "string",
                        "$gt": ""
                    },
                    "externalMatchId": {
                        "$type": "string",
                        "$gt": ""
                    }
                }
            }, {
                "$group": {
                    "_id": {
                        "provider": "$source.provider",
                        "externalMatchId": "$externalMatchId"
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
          "samples": [_json_safe(item) for item in duplicates],
          "sampleLimit": 20
      })

  return risks


def _tournament_mongo_document(document: dict) -> dict:
  result = dict(document)
  result["startDate"] = _parse_utc(result["startDate"])
  result["endDate"] = _parse_utc(result["endDate"])

  return result


def _match_mongo_document(template: dict, tournament_id,
                          team_ids_by_slug: dict[str, object]) -> dict:
  refs = template["teams"]
  winner_slug = template.get("winnerTeamSlug")

  return {
      "tournamentId": tournament_id,
      "externalMatchId": template["externalMatchId"],
      "stage": template["stage"],
      "group": template.get("group"),
      "round": template.get("round"),
      "bestOf": template["bestOf"],
      "scheduledAt": _parse_utc(template["scheduledAt"]),
      "startedAt": None,
      "completedAt": None,
      "status": template["status"],
      "teams": {
          "teamAId": team_ids_by_slug.get(refs["teamASlug"]),
          "teamBId": team_ids_by_slug.get(refs["teamBSlug"])
      },
      "score": template["score"],
      "winnerTeamId":
      team_ids_by_slug.get(winner_slug) if winner_slug else None,
      "source": template["source"]
  }


def _operation_outcome(collection_name: str, current: dict | None,
                       document: dict) -> str:
  if current is None:
    return "create"

  _, changed_fields = _merge_managed_document(collection_name, current,
                                              document)

  return "update" if changed_fields else "unchanged"


def _database_preflight(database, plan: dict) -> dict:
  collections = set(database.list_collection_names())
  index_conflicts = _index_conflicts(database, collections,
                                     plan["indexOperations"])
  planned_index_creates = _planned_index_creates(database, collections,
                                                 plan["indexOperations"])
  duplicate_risks = _duplicate_key_risks(database, collections)
  counts = {"plannedCreate": 0, "plannedUpdate": 0, "plannedUnchanged": 0}
  match_counts = {
      "plannedCreate": 0,
      "plannedUpdate": 0,
      "plannedUnchanged": 0
  }

  tournament_operation = plan["tournamentOperation"]
  tournament_filter = tournament_operation["key"]
  current_tournament = database["tournaments"].find_one(tournament_filter)
  tournament_document = _tournament_mongo_document(
      tournament_operation["document"])
  counts[
      f"planned{_operation_outcome('tournaments', current_tournament, tournament_document).capitalize()}"] += 1
  tournament_id = current_tournament.get("_id") if current_tournament else None

  team_ids_by_slug = {}
  current_teams_by_slug = {}
  planned_team_slugs = set()

  for operation in plan["teamOperations"]:
    document = operation["document"]
    planned_team_slugs.add(document["slug"])
    current = database["teams"].find_one(operation["key"])
    current_teams_by_slug[document["slug"]] = current

    if current:
      team_ids_by_slug[document["slug"]] = current["_id"]

    outcome = _operation_outcome("teams", current, document)
    counts[f"planned{outcome.capitalize()}"] += 1

  downgrade_risks = []
  canonical_reference_risks = []
  match_operation_risks = []

  for operation in plan["teamOperations"]:
    planned_team = operation["document"]
    current_team = current_teams_by_slug.get(planned_team["slug"])

    if current_team is None:
      continue

    current_source = current_team.get("source") or {}
    planned_source = planned_team.get("source") or {}

    if planned_source and (current_source.get("provider")
                           != planned_source.get("provider")
                           or str(current_source.get("externalId")) != str(
                               planned_source.get("externalId"))):
      canonical_reference_risks.append({
          "kind": "team-source-identity-changed",
          "slug": planned_team["slug"]
      })

  for operation in plan["matchOperations"]:
    if operation.get("operation") != "upsert":
      continue

    template = operation["documentTemplate"]
    match_filter = {
        "source.provider": "liquipedia",
        "externalMatchId": template["externalMatchId"]
    }
    current = database["matches"].find_one(match_filter)
    operation_risks = []

    if (current and current.get("status") == "completed"
        and template.get("status") not in {None, "completed"}):
      risk = {
          "externalMatchId": template["externalMatchId"],
          "storedStatus": "completed",
          "plannedStatus": template["status"]
      }

      downgrade_risks.append(risk)
      operation_risks.append({"kind": "completed-match-downgrade", **risk})

    refs = template["teams"]
    required_team_slugs = [
        slug for slug in (refs.get("teamASlug"), refs.get("teamBSlug")) if slug
    ]
    unresolved_team_slugs = [
        slug for slug in required_team_slugs if slug not in planned_team_slugs
        and current_teams_by_slug.get(slug) is None
    ]

    if current is not None and unresolved_team_slugs:
      risk = {
          "kind": "missing-team-reference",
          "externalMatchId": template["externalMatchId"],
          "teamSlugs": unresolved_team_slugs
      }

      canonical_reference_risks.append(risk)
      operation_risks.append(risk)
      outcome = None
    elif current is not None:
      document = _match_mongo_document(template, tournament_id,
                                       team_ids_by_slug)
      outcome = _operation_outcome("matches", current, document)
    else:
      outcome = "create"

    if operation_risks:
      match_operation_risks.extend(operation_risks)
    elif outcome is not None:
      counts[f"planned{outcome.capitalize()}"] += 1
      match_counts[f"planned{outcome.capitalize()}"] += 1

  skipped = plan["plannedCounts"]["skip"] + len({
      risk["externalMatchId"]
      for risk in match_operation_risks if risk.get("externalMatchId")
  })

  return {
      **counts, "plannedMatchCounts":
      match_counts,
      "matchOperationRisks":
      match_operation_risks,
      "canonicalReferenceRisks":
      canonical_reference_risks,
      "plannedIndexCreates":
      planned_index_creates,
      "estimatedWriteUnits":
      counts["plannedCreate"] + counts["plannedUpdate"] +
      planned_index_creates + 1,
      "skipped":
      skipped,
      "indexConflicts":
      index_conflicts,
      "duplicateKeyRisks":
      duplicate_risks,
      "completedMatchDowngradeRisks":
      downgrade_risks,
      "tournament":
      current_tournament,
      "plannedImportStatus":
      preview_import_status("schedule", (plan.get("liquipediaCacheIdentity")
                                         or {}).get("fetchedAt"),
                            plan.get("planDigest"), skipped,
                            (current_tournament or {}).get("importStatus"))
  }


def _has_preflight_blockers(result: dict) -> bool:
  global_reference_risks = [
      risk for risk in result.get("canonicalReferenceRisks", [])
      if not risk.get("externalMatchId")
  ]

  return any(
      result.get(key)
      for key in ("indexConflicts",
                  "duplicateKeyRisks")) or bool(global_reference_risks)


def preflight_write_plan(plan: dict, settings: Settings,
                         plan_path: Path) -> tuple[dict, Path]:
  generated_at = datetime.now(timezone.utc)
  report = {
      "runId": str(uuid.uuid4()),
      "generatedAt": _iso_utc(generated_at),
      "writePlanPath": str(plan_path.expanduser().resolve()),
      "planDigest": plan.get("planDigest"),
      "plannedCreate": None,
      "plannedUpdate": None,
      "plannedUnchanged": None,
      "skipped": plan.get("plannedCounts", {}).get("skip", 0),
      "indexConflicts": [],
      "duplicateKeyRisks": [],
      "completedMatchDowngradeRisks": [],
      "canonicalReferenceRisks": [],
      "matchOperationRisks": [],
      "readyForApply": None,
      "estimatedWriteUnits": None,
      "plannedImportStatus": None,
      "plannedMatchCounts": None,
      "warnings": list(plan.get("warnings") or []),
      "warningCount": len(plan.get("warnings") or []),
      "mongodbConnected": False,
      "mongodbWrites": 0,
      "errors": []
  }
  errors = verify_plan_integrity(plan, plan_path) + _validate_plan(plan)

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
                    "plannedMatchCounts", "skipped", "indexConflicts",
                    "duplicateKeyRisks", "completedMatchDowngradeRisks",
                    "canonicalReferenceRisks", "matchOperationRisks",
                    "estimatedWriteUnits", "plannedImportStatus"):
        report[field] = inspection[field]

      report["readyForApply"] = not _has_preflight_blockers(inspection)
    except Exception as error:
      report["errors"].append(
          f"Preflight failed ({type(error).__name__}); MongoDB writes remain 0."
      )
    finally:
      if client is not None:
        client.close()

  filename = f"import-preflight-{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  report_path = settings.report_directory / filename
  report["reportPath"] = str(report_path.resolve())
  write_json_atomic(report_path, report)

  return report, report_path


def apply_write_plan(plan: dict,
                     settings: Settings,
                     plan_path: Path,
                     max_writes: int | None = None) -> dict:
  errors = verify_plan_integrity(plan, plan_path) + _validate_plan(plan)
  base_counts = {
      "connected": False,
      "writes": 0,
      "created": 0,
      "updated": 0,
      "unchanged": 0,
      "skipped": plan.get("plannedCounts", {}).get("skip", 0),
      "failed": 0,
      "matchCounts": {
          "created": 0,
          "updated": 0,
          "unchanged": 0
      },
      "errors": errors,
      "preflight": None,
      "estimatedWriteUnits": None
  }

  if errors:
    return base_counts

  from app.database import create_mongodb_client

  client = None
  apply_started = False
  import_status_recorded = False

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    base_counts["connected"] = True
    database = client[settings.mongodb_database]
    inspection = _database_preflight(database, plan)
    base_counts["preflight"] = inspection

    if _has_preflight_blockers(inspection):
      base_counts["errors"].append(
          "Read-only MongoDB preflight found blocking risks; no indexes or documents were written."
      )

      return base_counts

    blocked_match_ids = {
        risk.get("externalMatchId")
        for risk in inspection.get("matchOperationRisks", [])
        if isinstance(risk.get("externalMatchId"), str)
    }
    base_counts["skipped"] = inspection["skipped"]

    if max_writes is not None and inspection[
        "estimatedWriteUnits"] > max_writes:
      base_counts["errors"].append(
          "Apply write estimate exceeds the configured safety limit; no indexes or documents were written."
      )
      base_counts["estimatedWriteUnits"] = inspection["estimatedWriteUnits"]

      return base_counts

    latest_errors = verify_plan_integrity(plan,
                                          plan_path) + _validate_plan(plan)

    if latest_errors:
      base_counts["errors"].extend(latest_errors)

      return base_counts

    base_counts["estimatedWriteUnits"] = inspection["estimatedWriteUnits"]
    apply_started = True
    _ensure_indexes(database, plan["indexOperations"])
    base_counts["writes"] += inspection["plannedIndexCreates"]
    now = datetime.now(timezone.utc)
    tournament_operation = plan["tournamentOperation"]
    tournament_result, tournament_id = _upsert(
        database["tournaments"], "tournaments", tournament_operation["key"],
        _tournament_mongo_document(tournament_operation["document"]), now)

    base_counts[tournament_result] += 1

    if tournament_result in {"created", "updated"}:
      base_counts["writes"] += 1

    team_ids_by_slug = {}

    for operation in plan["teamOperations"]:
      result, team_id = _upsert(database["teams"], "teams", operation["key"],
                                operation["document"], now)

      base_counts[result] += 1

      if result in {"created", "updated"}:
        base_counts["writes"] += 1

      team_ids_by_slug[operation["document"]["slug"]] = team_id

    match_documents = []

    for operation in plan["matchOperations"]:
      if operation.get("operation") != "upsert":
        continue

      template = operation["documentTemplate"]

      if template["externalMatchId"] in blocked_match_ids:
        continue

      document = _match_mongo_document(template, tournament_id,
                                       team_ids_by_slug)
      required_slugs = [
          slug for slug in (template["teams"].get("teamASlug"),
                            template["teams"].get("teamBSlug")) if slug
      ]

      if (not document["tournamentId"]
          or any(not team_ids_by_slug.get(slug) for slug in required_slugs)):
        base_counts["errors"].append(
            f"Match {template['externalMatchId']} failed reference validation before match writes."
        )

      match_documents.append((operation, document))

    if base_counts["errors"]:
      base_counts["failed"] += len(match_documents)
      record_import_status(database, plan["eventSlug"], "schedule", "failed",
                           None, None)
      base_counts["writes"] += 1

      return base_counts

    for operation, document in match_documents:
      result, _ = _upsert(
          database["matches"], "matches", {
              "source.provider": "liquipedia",
              "externalMatchId": operation["externalMatchId"]
          }, document, now)

      base_counts[result] += 1
      base_counts["matchCounts"][result] += 1

      if result in {"created", "updated"}:
        base_counts["writes"] += 1

    if not base_counts["errors"] and base_counts["failed"] == 0:
      import_status = expected_status(base_counts["skipped"])
      record_import_status(
          database, plan["eventSlug"], "schedule", import_status,
          (plan.get("liquipediaCacheIdentity") or {}).get("fetchedAt"),
          plan.get("planDigest"), {
              "liquipediaCacheSha256":
              (plan.get("liquipediaCacheIdentity") or {}).get("cacheSha256"),
              "liquipediaMatchCount": (plan.get("reconciliationStatusCounts")
                                       or {}).get("liquipediaMatches"),
              "duckdbMatchCount": (plan.get("reconciliationStatusCounts")
                                   or {}).get("duckdbMatches"),
              "verifiedMatchCount":
              sum(base_counts["matchCounts"].values())
          })
      base_counts["writes"] += 1
      base_counts["importStatus"] = import_status
      import_status_recorded = True
  except Exception as error:
    base_counts["failed"] += 1
    base_counts["errors"].append(
        f"Apply failed ({type(error).__name__}); MongoDB URI and credentials were not logged."
    )

    if apply_started and client is not None and not import_status_recorded:
      try:
        record_import_status(client[settings.mongodb_database],
                             settings.tournament_slug, "schedule", "failed",
                             None, None)
        base_counts["writes"] += 1
      except Exception:
        base_counts["errors"].append(
            "Failed import status could not be recorded.")
  finally:
    if client is not None:
      client.close()

  return base_counts
