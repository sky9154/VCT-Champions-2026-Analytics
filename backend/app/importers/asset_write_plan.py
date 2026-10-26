import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, overload

from app.config import Settings
from app.utils.files import sha256_file, write_json_atomic
from app.assets import (MIME_EXTENSIONS, TEAM_LOGO_API_PREFIX,
                        is_manually_managed_team_logo,
                        manually_managed_team_logos, public_team_logo_url,
                        store_team_logo, valid_team_slug)
from app.importers.asset_models import AssetImageDTO, TeamAssetOperationDTO, TeamAssetReferenceDTO
from app.importers.models import ImportRunError
from app.importers.import_status import expected_status, preview_import_status, record_import_status
from app.importers.providers.liquipedia_assets import (
    TEAM_IDENTITY_ALIASES, extract_team_logo_candidates, normalize_identity,
    read_event_parse_cache, select_team_logo, validate_image_url)
from app.importers.providers.liquipedia import LiquipediaProvider
from app.importers.write_plan import calculate_plan_digest, _validate_plan, load_write_plan, verify_plan_integrity


@overload
def _iso_utc(value: datetime) -> str:
  ...


@overload
def _iso_utc(value: None) -> None:
  ...


def _iso_utc(value: datetime | None) -> str | None:
  if value is None:
    return None

  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value: str | None) -> datetime | None:
  if not isinstance(value, str) or not value.strip():
    return None

  normalized = value.strip()

  if normalized.endswith("Z"):
    normalized = normalized[:-1] + "+00:00"

  try:
    parsed = datetime.fromisoformat(normalized)
  except ValueError:
    return None

  if parsed.tzinfo is None or parsed.utcoffset() is None:
    return None

  return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> dict:
  payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))

  if not isinstance(payload, dict):
    raise ValueError("A source Write Plan must contain a JSON object.")

  return payload


def _latest_schedule_plan(settings: Settings) -> tuple[Path, dict]:
  candidates = []

  for path in settings.report_directory.glob("import-plan-*.json"):
    try:
      plan = _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
      continue

    if (plan.get("eventSlug") == settings.tournament_slug
        and isinstance(plan.get("generatedAt"), str)):
      candidates.append((plan["generatedAt"], path.resolve(), plan))

  for _, path, plan in sorted(candidates,
                              key=lambda item: item[0],
                              reverse=True):
    if (not verify_plan_integrity(plan, path)
        and not plan.get("blockingErrors") and not _validate_plan(plan)):
      return path, plan

  raise ValueError(
      "No intact, non-blocking Schedule Write Plan exists for the configured Tournament."
  )


def _team_references(schedule_plan: dict,
                     blocking_errors: list[str]) -> list[dict]:
  references = []
  seen_slugs = set()
  seen_external_ids = set()

  for operation in schedule_plan.get("teamOperations", []):
    document = operation.get("document") or {}
    source = document.get("source") or {}
    reference = {
        "slug": document.get("slug"),
        "name": document.get("name"),
        "sourceProvider": source.get("provider"),
        "sourceExternalId": source.get("externalId")
    }

    if not all(
        isinstance(value, str) and value.strip()
        for value in reference.values()):
      blocking_errors.append(
          "A canonical Team reference is missing its slug, name, provider, or external ID."
      )
      continue

    external_identity = (reference["sourceProvider"],
                         reference["sourceExternalId"])

    if reference[
        "slug"] in seen_slugs or external_identity in seen_external_ids:
      blocking_errors.append(
          f"Duplicate canonical Team identity in Schedule Write Plan: {reference['slug']}."
      )
      continue

    seen_slugs.add(reference["slug"])
    seen_external_ids.add(external_identity)
    references.append(reference)

  return references


def _team_candidates_by_slug(
    team_references: list[dict],
    candidates: dict[str,
                     list[dict]]) -> tuple[dict[str, list[dict]], list[dict]]:
  by_slug = {reference["slug"]: [] for reference in team_references}
  unresolved = []
  identity_to_slugs: dict[str, set[str]] = {}

  for reference in team_references:
    for identity in (reference["name"], reference["slug"]):
      identity_to_slugs.setdefault(normalize_identity(identity),
                                   set()).add(reference["slug"])

  for alias, canonical_slug in TEAM_IDENTITY_ALIASES.items():
    if canonical_slug in by_slug:
      identity_to_slugs.setdefault(normalize_identity(alias),
                                   set()).add(canonical_slug)

  for identity_key, image_candidates in candidates.items():
    target_slugs = identity_to_slugs.get(identity_key, set())

    if len(target_slugs) != 1:
      unresolved.append({
          "sourceIdentity":
          identity_key,
          "slugs":
          sorted(target_slugs),
          "reason":
          "team-identity-ambiguous"
          if target_slugs else "team-identity-unmatched"
      })
      continue

    target_slug = next(iter(target_slugs))
    by_slug[target_slug].extend(image_candidates)

  return by_slug, unresolved


def _build_team_operations(team_references: list[dict],
                           candidates_by_slug: dict[str, list[dict]],
                           skipped: list[dict], rejected_urls: list[dict],
                           ambiguous_slugs: set[str], settings: Settings,
                           provider: LiquipediaProvider | None) -> list[dict]:
  operations = []

  for reference in team_references:
    if is_manually_managed_team_logo(settings, reference["slug"]):
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": "manual-logo-protected"
      })
      continue

    if reference["slug"] in ambiguous_slugs:
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": "team-identity-ambiguous"
      })
      continue

    selected, reason = select_team_logo(
        candidates_by_slug.get(reference["slug"], []))

    if selected is None:
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": reason or "team-logo-not-found"
      })
      continue

    validated_url, rejected_reason = validate_image_url(selected["url"])

    if rejected_reason or validated_url is None:
      rejected_urls.append({
          "entityType": "team",
          "slug": reference["slug"],
          "url": selected["url"],
          "reason": rejected_reason
      })
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": "team-logo-url-rejected"
      })
      continue

    if provider is None:
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": "image-download-required"
      })
      continue

    try:
      data, mime_type, content_sha256, downloaded_at = provider.download_team_logo(
          validated_url, reference["slug"])
      asset = store_team_logo(settings, reference["slug"], data, mime_type,
                              _iso_utc(downloaded_at))
    except (OSError, ValueError, ImportRunError) as error:
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": "image-download-or-validation-failed"
      })
      continue

    logo_url = public_team_logo_url(settings, reference["slug"])

    if logo_url is None:
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": "stored-image-version-unavailable"
      })
      continue

    logo = AssetImageDTO(source_url=validated_url,
                         source_identity=selected["liquipediaName"],
                         logo_url=logo_url,
                         source_page=selected["sourcePage"],
                         image_name=selected["imageName"],
                         mime_type=mime_type,
                         storage_identity=asset["storageIdentity"],
                         content_sha256=content_sha256,
                         size_bytes=len(data),
                         retrieved_at=_iso_utc(downloaded_at),
                         file_reused=asset["fileReused"])
    operation = TeamAssetOperationDTO(reference=TeamAssetReferenceDTO(
        slug=reference["slug"],
        name=reference["name"],
        source_provider=reference["sourceProvider"],
        source_external_id=reference["sourceExternalId"]),
                                      logo=logo)

    operations.append(operation.model_dump(mode="json", by_alias=True))

  return operations


def _validate_plan_sources(schedule_path: Path | None, schedule_plan: dict,
                           cache_source: dict | None,
                           errors: list[str]) -> dict:
  try:
    schedule_sha = sha256_file(
        schedule_path) if schedule_path is not None else None
  except OSError:
    schedule_sha = None
    errors.append("Canonical Schedule Write Plan is missing or unreadable.")

  cache_sha = cache_source.get("cacheSha256") if cache_source else None
  planned_cache_sha = (schedule_plan.get("liquipediaCacheIdentity")
                       or {}).get("cacheSha256")

  if cache_sha is None:
    errors.append("Liquipedia event cache SHA-256 is unavailable.")
  elif cache_sha != planned_cache_sha:
    errors.append(
        "Liquipedia event cache does not match the source Schedule Write Plan."
    )

  return {
      "schedulePlanSha256": schedule_sha,
      "liquipediaCacheSha256": cache_sha
  }


def build_asset_plan(settings: Settings,
                     download_assets: bool = False) -> dict:
  generated_at = datetime.now(timezone.utc)
  blocking_errors: list[str] = []
  warnings: list[str] = []
  skipped: list[dict] = []
  rejected_urls: list[dict] = []

  if not download_assets:
    blocking_errors.append(
        "Offline mode does not download image bytes; use --prepare to create an applicable Team logo plan."
    )

  try:
    schedule_path, schedule_plan = _latest_schedule_plan(settings)
  except (OSError, ValueError) as error:
    schedule_path = None
    schedule_plan = {}
    blocking_errors.append(str(error))

  team_references = _team_references(schedule_plan, blocking_errors)
  event_page_title = ((schedule_plan.get("liquipediaCacheIdentity")
                       or {}).get("requestIdentity") or {}).get("pageTitle")

  if not isinstance(event_page_title, str) or not event_page_title:
    event_page_title = settings.liquipedia_page_title

  cache_source = None
  event_data = None

  try:
    event_data, cache_source = read_event_parse_cache(settings,
                                                      event_page_title)
  except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
    blocking_errors.append(
        f"Liquipedia event cache is unavailable ({type(error).__name__}).")

  team_operations = []
  provider = None

  if event_data is not None:
    candidates, rejected = extract_team_logo_candidates(event_data["html"])
    rejected_urls.extend({"entityType": "team", **item} for item in rejected)
    candidates_by_slug, unresolved = _team_candidates_by_slug(
        team_references, candidates)
    ambiguous_slugs = {
        slug
        for item in unresolved
        if item.get("reason") == "team-identity-ambiguous"
        for slug in item.get("slugs", [])
    }

    warnings.extend(
        f"Unmatched Liquipedia team image identity '{item['sourceIdentity']}' was skipped."
        for item in unresolved)

    if download_assets:
      provider = LiquipediaProvider(settings, settings.cache_directory)
      provider._current_event = settings.tournament_slug

    try:
      team_operations = _build_team_operations(team_references,
                                               candidates_by_slug, skipped,
                                               rejected_urls, ambiguous_slugs,
                                               settings, provider)
    finally:
      if provider is not None:
        provider.close()
  else:
    for reference in team_references:
      skipped.append({
          "entityType": "team",
          "slug": reference["slug"],
          "name": reference["name"],
          "reason": "event-cache-unavailable"
      })

  url_to_slugs: dict[str, list[str]] = {}

  for operation in team_operations:
    url_to_slugs.setdefault(operation["logo"]["sourceUrl"],
                            []).append(operation["reference"]["slug"])

  collisions = {
      url: slugs
      for url, slugs in url_to_slugs.items() if len(slugs) > 1
  }

  if collisions:
    collision_slugs = {slug for slugs in collisions.values() for slug in slugs}

    for url, slugs in collisions.items():
      warnings.append(
          "A Liquipedia team logo URL matched multiple canonical identities and was skipped."
      )
      rejected_urls.append({
          "entityType": "team",
          "slugs": sorted(slugs),
          "url": url,
          "reason": "duplicate-asset-identity"
      })

    team_operations = [
        operation for operation in team_operations
        if operation["reference"]["slug"] not in collision_slugs
    ]

    for slug in sorted(collision_slugs):
      skipped.append({
          "entityType": "team",
          "slug": slug,
          "reason": "duplicate-asset-url"
      })

  resolved_slugs = {
      operation["reference"]["slug"]
      for operation in team_operations
  }
  ambiguous_slugs = {
      item.get("slug")
      for item in skipped if item.get("entityType") == "team" and any(
          marker in str(item.get("reason"))
          for marker in ("ambiguous", "multiple", "duplicate-asset-url"))
  }
  source_digests = _validate_plan_sources(schedule_path, schedule_plan,
                                          cache_source, blocking_errors)
  cache_sources = [cache_source] if cache_source else []
  data_as_of_values = [
      _parse_time(item.get("fetchedAt")) for item in cache_sources
  ]
  data_as_of_values = [
      value for value in data_as_of_values if value is not None
  ]
  data_as_of = max(data_as_of_values) if data_as_of_values else None
  source_digests["cacheSources"] = [{
      "cacheFile": item["cacheFile"],
      "sha256": item["cacheSha256"]
  } for item in cache_sources]
  team_counts = {
      "total":
      len(team_references),
      "resolved":
      len(resolved_slugs),
      "missing":
      max(0,
          len(team_references) - len(resolved_slugs) - len(ambiguous_slugs)),
      "ambiguous":
      len(ambiguous_slugs)
  }
  operation_count = len(team_operations)
  plan = {
      "schemaVersion": 3,
      "runId": str(uuid.uuid4()),
      "generatedAt": _iso_utc(generated_at),
      "eventSlug": settings.tournament_slug,
      "executionMode":
      "local-asset-prepare" if download_assets else "offline-cache-only",
      "scope": {
          "collection": "teams",
          "field": "logoUrl",
          "excluded": ["players.imageUrl"]
      },
      "tournamentIdentity": {
          "slug": settings.tournament_slug,
          "schedulePlanDigest": schedule_plan.get("planDigest")
      },
      "dataAsOf": _iso_utc(data_as_of),
      "schedulePlan": {
          "path": str(schedule_path) if schedule_path else None,
          "sha256": source_digests.get("schedulePlanSha256"),
          "planDigest": schedule_plan.get("planDigest")
      },
      "liquipediaCacheIdentity": {
          "pageTitle": event_page_title,
          "path": cache_source.get("path") if cache_source else None,
          "cacheSha256": source_digests.get("liquipediaCacheSha256")
      },
      "sourceDigests": source_digests,
      "cacheSources": cache_sources,
      "teamReferences": team_references,
      "teamOperations": team_operations,
      "playerPortraitOperations": 0,
      "skips": skipped,
      "rejectedUrls": rejected_urls,
      "entityCounts": {
          "teams": team_counts
      },
      "coverage": {
          "teamLogos": team_counts,
          "playerPortraits": {
              "inScope": False,
              "operations": 0
          }
      },
      "plannedCounts": {
          "create": 0,
          "update": operation_count,
          "unchanged": None,
          "skip": len(skipped),
          "assetOperations": operation_count
      },
      "localFiles": {
          "created":
          sum(1 for operation in team_operations
              if not operation["logo"].get("fileReused")),
          "reused":
          sum(1 for operation in team_operations
              if operation["logo"].get("fileReused")),
          "verified":
          len(team_operations),
          "failed":
          len(skipped),
          "playerOperations":
          0
      },
      "warnings": warnings,
      "blockingErrors": blocking_errors,
      "execution": None
  }
  plan["planDigest"] = calculate_plan_digest(plan)

  return plan


def write_asset_plan_file(plan: dict, output_directory: Path) -> Path:
  generated_at = _parse_time(plan.get("generatedAt")) or datetime.now(
      timezone.utc)

  output_directory.mkdir(parents=True, exist_ok=True)
  path = output_directory / f"asset-import-plan-{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  plan["writePlanPath"] = str(path.resolve())
  plan["planDigest"] = calculate_plan_digest(plan)
  write_json_atomic(path, plan, allow_nan=False)

  return path


def write_asset_coverage_report(plan: dict, plan_path: Path,
                                output_directory: Path) -> Path:
  generated_at = datetime.now(timezone.utc)
  operations = {
      item["reference"]["slug"]: item
      for item in plan.get("teamOperations", [])
  }
  skips = {}

  for item in plan.get("skips", []):
    slug = item.get("slug")

    if slug and slug not in skips:
      skips[slug] = item

  teams = []

  for reference in plan.get("teamReferences", []):
    slug = reference["slug"]
    operation = operations.get(slug)
    skipped = skips.get(slug)
    status = "resolved" if operation else "ambiguous" if "ambiguous" in str(
        (skipped or {}).get("reason")) or "multiple" in str(
            (skipped or {}).get("reason")) else "missing"
    logo = (operation or {}).get("logo") or {}
    teams.append({
        "slug": slug,
        "name": reference["name"],
        "status": status,
        "logoUrl": logo.get("logoUrl"),
        "sourceUrl": logo.get("sourceUrl"),
        "sourceIdentity": logo.get("sourceIdentity"),
        "sourcePage": logo.get("sourcePage"),
        "imageName": logo.get("imageName"),
        "mimeType": logo.get("mimeType"),
        "sizeBytes": logo.get("sizeBytes"),
        "contentSha256": logo.get("contentSha256"),
        "storageIdentity": logo.get("storageIdentity"),
        "reason": skipped.get("reason") if skipped else None
    })

  report = {
      "generatedAt": _iso_utc(generated_at),
      "assetPlanPath": str(plan_path.resolve()),
      "planDigest": plan.get("planDigest"),
      "cacheSha256": (plan.get("liquipediaCacheIdentity")
                      or {}).get("cacheSha256"),
      "coverage": {
          "teamLogos": plan.get("coverage", {}).get("teamLogos", {}),
          "playerPortraits": {
              "inScope": False,
              "operations": 0
          }
      },
      "plannedTeamUpdates": len(operations),
      "localFiles": plan.get("localFiles", {}),
      "playerOperations": 0,
      "teams": teams,
      "mongodbConnected": False,
      "mongodbWrites": 0
  }

  output_directory.mkdir(parents=True, exist_ok=True)
  path = output_directory / f"asset-coverage-{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  write_json_atomic(path, report, allow_nan=False)

  return path


def _get_path(document: dict, path: str):
  value = document

  for part in path.split("."):
    if not isinstance(value, dict):
      return None

    value = value.get(part)

  return value


def _duplicate_risks(database,
                     field: str,
                     values: list[str],
                     source_provider: str | None = None) -> list[dict]:
  if not values:
    return []

  match: dict[str, Any] = {field: {"$in": values}}

  if source_provider is not None:
    match["source.provider"] = source_provider

  pipeline = [{
      "$match": match
  }, {
      "$group": {
          "_id": f"${field}",
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
  }, {
      "$limit": 50
  }]

  return [{
      "collection": "teams",
      "field": field,
      "value": row.get("_id"),
      "count": row.get("count")
  } for row in database["teams"].aggregate(pipeline)]


def _asset_metadata(asset: dict) -> dict:
  retrieved_at = _parse_time(asset.get("retrievedAt"))

  if retrieved_at is not None:
    retrieved_at = retrieved_at.replace(
        microsecond=(retrieved_at.microsecond // 1000) * 1000)

  return {
      "provider": "liquipedia",
      "sourcePage": asset["sourcePage"],
      "imageName": asset["imageName"],
      "sourceUrl": asset["sourceUrl"],
      "sourceIdentity": asset["sourceIdentity"],
      "contentSha256": asset["contentSha256"],
      "mimeType": asset["mimeType"],
      "storageIdentity": asset["storageIdentity"],
      "retrievedAt": retrieved_at
  }


def _desired_fields(operation: dict) -> dict:
  asset = operation["logo"]

  return {
      "logoUrl": asset["logoUrl"],
      "assetSources.logo": _asset_metadata(asset)
  }


def _team_identity_matches(document: dict, reference: dict) -> bool:
  source = document.get("source") or {}

  return (document.get("slug") == reference.get("slug")
          and document.get("name") == reference.get("name")
          and source.get("provider") == reference.get("sourceProvider")
          and str(source.get("externalId")) == str(
              reference.get("sourceExternalId")))


def _database_preflight(database, plan: dict) -> dict:
  tournament_matches = list(database["tournaments"].find({
      "slug":
      plan["eventSlug"]
  }).limit(2))
  tournament = tournament_matches[0] if len(tournament_matches) == 1 else None
  canonical_reference_risks = []

  if not tournament_matches:
    canonical_reference_risks.append({
        "kind": "tournament-missing",
        "slug": plan["eventSlug"]
    })
  elif len(tournament_matches) > 1:
    canonical_reference_risks.append({
        "kind": "tournament-duplicate",
        "slug": plan["eventSlug"]
    })

  references = plan.get("teamReferences", [])
  slugs = [item["slug"] for item in references]
  external_ids = [item["sourceExternalId"] for item in references]
  teams = list(database["teams"].find({"slug": {
      "$in": slugs
  }})) if slugs else []
  teams_by_slug: dict[str, list[dict]] = {}

  for team in teams:
    teams_by_slug.setdefault(team.get("slug"), []).append(team)

  for reference in references:
    matches = teams_by_slug.get(reference["slug"], [])

    if len(matches) != 1:
      canonical_reference_risks.append({
          "kind": "team-reference-missing-or-duplicate",
          "slug": reference["slug"],
          "count": len(matches)
      })
    elif not _team_identity_matches(matches[0], reference):
      canonical_reference_risks.append({
          "kind": "team-reference-changed",
          "slug": reference["slug"]
      })

  duplicate_key_risks = _duplicate_risks(database, "slug", slugs)
  providers = {item["sourceProvider"] for item in references}

  for provider in providers:
    provider_ids = [
        item["sourceExternalId"] for item in references
        if item["sourceProvider"] == provider
    ]

    duplicate_key_risks.extend(
        _duplicate_risks(database, "source.externalId", provider_ids,
                         provider))

  counts = {"plannedCreate": 0, "plannedUpdate": 0, "plannedUnchanged": 0}
  missing_logo_count = 0
  changed_logo_count = 0
  operation_documents = {}

  for operation in plan.get("teamOperations", []):
    slug = operation["reference"]["slug"]
    matches = teams_by_slug.get(slug, [])

    if len(matches) != 1:
      canonical_reference_risks.append({
          "kind": "team-operation-target-unavailable",
          "slug": slug,
          "count": len(matches)
      })
      continue

    current = matches[0]
    current_logo_url = current.get("logoUrl")

    if not isinstance(current_logo_url, str) or not current_logo_url.strip():
      missing_logo_count += 1

    if current_logo_url != operation["logo"].get("logoUrl"):
      changed_logo_count += 1

    desired = _desired_fields(operation)
    changed = {
        field: value
        for field, value in desired.items() if value is not None
        and value != "" and _get_path(current, field) != value
    }

    if changed:
      counts["plannedUpdate"] += 1
    else:
      counts["plannedUnchanged"] += 1

    operation_documents[slug] = {"document": current, "changed": changed}

  import_status = (tournament or {}).get("importStatus") or {}
  status_plan = preview_import_status("assets", plan.get("dataAsOf"),
                                      plan.get("planDigest"),
                                      len(plan.get("skips", [])),
                                      import_status)

  return {
      **counts, "estimatedWriteUnits": counts["plannedUpdate"] + 1,
      "missingLogoCount": missing_logo_count,
      "changedLogoUrlCount": changed_logo_count,
      "skipped": len(plan.get("skips", [])),
      "indexConflicts": [],
      "duplicateKeyRisks": duplicate_key_risks,
      "canonicalReferenceRisks": canonical_reference_risks,
      "plannedImportStatus": status_plan,
      "tournament": tournament,
      "teamDocumentsBySlug": teams_by_slug,
      "operationDocuments": operation_documents
  }


def _database_blockers(result: dict) -> list[str]:
  errors = []

  if result.get("indexConflicts"):
    errors.append("Asset Preflight found index conflicts.")

  if result.get("duplicateKeyRisks"):
    errors.append("Asset Preflight found duplicate canonical keys.")

  if result.get("canonicalReferenceRisks"):
    errors.append(
        "Asset Preflight found missing or changed canonical references.")

  return errors


def _validate_asset_plan(plan: dict) -> list[str]:
  errors = list(plan.get("blockingErrors", []))

  if plan.get("schemaVersion") != 3 or not plan.get("eventSlug"):
    errors.append(
        "Team-only Asset Write Plan schema or Tournament identity is invalid.")

  if (plan.get("tournamentIdentity")
      or {}).get("slug") != plan.get("eventSlug"):
    errors.append("Asset Write Plan Tournament identity fields disagree.")

  if plan.get("scope") != {
      "collection": "teams",
      "field": "logoUrl",
      "excluded": ["players.imageUrl"]
  }:
    errors.append("Asset Write Plan scope is not Team logos only.")

  if any(key in plan for key in ("playerOperations", "playerReferences",
                                 "pageImageCacheSources")):
    errors.append(
        "Player portrait data is not accepted in a Team-only Asset Write Plan."
    )

  if plan.get("playerPortraitOperations") != 0:
    errors.append(
        "Team-only Asset Write Plan must have zero Player portrait operations."
    )

  if (not isinstance(plan.get("teamOperations"), list)
      or not isinstance(plan.get("teamReferences"), list)):
    errors.append(
        "Asset Write Plan Team references or operations are invalid.")

    return errors

  if len(plan["teamReferences"]) != 16 or len(plan["teamOperations"]) != 16:
    errors.append(
        "Team-only Asset Write Plan must contain 16 canonical Teams and 16 validated logo operations."
    )

  seen_references = set()

  for reference in plan["teamReferences"]:
    if not isinstance(reference, dict) or any(
        not isinstance(reference.get(field), str)
        or not reference[field].strip()
        for field in ("slug", "name", "sourceProvider", "sourceExternalId")):
      errors.append(
          "Asset Write Plan has an incomplete canonical Team reference.")
      continue

    if reference["slug"] in seen_references:
      errors.append(
          "Asset Write Plan has duplicate canonical Team references.")

    seen_references.add(reference["slug"])

  canonical = {
      item["slug"]: item
      for item in plan["teamReferences"]
      if isinstance(item, dict) and isinstance(item.get("slug"), str)
  }

  seen_operations = set()

  for operation in plan["teamOperations"]:
    if not isinstance(operation, dict):
      errors.append("Asset Write Plan contains an invalid Team operation.")
      continue

    reference = operation.get("reference")
    reference = reference if isinstance(reference, dict) else {}
    slug = reference.get("slug")

    if operation.get("operation") != "update" or operation.get(
        "collection") != "teams":
      errors.append(
          f"Team asset operation for {slug} has an invalid operation type.")

    if not isinstance(slug,
                      str) or slug not in canonical or slug in seen_operations:
      errors.append(
          f"Team asset operation for {slug} has a missing or duplicate canonical identity."
      )
      continue

    seen_operations.add(slug)

    if any(
        reference.get(field) != canonical[slug].get(field)
        for field in ("slug", "name", "sourceProvider", "sourceExternalId")):
      errors.append(
          f"Team asset operation for {slug} differs from its canonical source identity."
      )

    logo = operation.get("logo")
    logo = logo if isinstance(logo, dict) else {}
    source_url = logo.get("sourceUrl")
    validated_url, url_error = validate_image_url(source_url) if isinstance(
        source_url, str) else (None, "missing-url")

    if url_error or validated_url != source_url:
      errors.append(
          f"Team asset operation for {slug} has a rejected image URL.")

    if not isinstance(logo.get("sourceIdentity"),
                      str) or not logo["sourceIdentity"].strip():
      errors.append(
          f"Team asset operation for {slug} has no canonical Liquipedia source identity."
      )

    content_hash = logo.get("contentSha256")
    expected_logo_url = f"{TEAM_LOGO_API_PREFIX}/{slug}?v={content_hash[:12]}" if isinstance(
        content_hash, str) else None

    if logo.get("logoUrl") != expected_logo_url:
      errors.append(
          f"Team asset operation for {slug} has an invalid internal logo URL.")

    mime_type = logo.get("mimeType")
    extension = MIME_EXTENSIONS.get(mime_type) if isinstance(mime_type,
                                                             str) else None

    if extension is None:
      errors.append(
          f"Team asset operation for {slug} has an unsupported MIME type.")
    elif logo.get("storageIdentity") != f"team-logos/{slug}{extension}":
      errors.append(
          f"Team asset operation for {slug} has an invalid local storage identity."
      )

    if (not isinstance(logo.get("contentSha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", logo["contentSha256"])):
      errors.append(
          f"Team asset operation for {slug} has an invalid content hash.")

    if not isinstance(logo.get("sizeBytes"),
                      int) or not 0 < logo["sizeBytes"] <= 2 * 1024 * 1024:
      errors.append(
          f"Team asset operation for {slug} has an invalid image size.")

    if (not logo.get("sourcePage") or not logo.get("imageName")
        or not _parse_time(logo.get("retrievedAt"))):
      errors.append(
          f"Team asset operation for {slug} has incomplete image provenance.")

    if not valid_team_slug(slug):
      errors.append(
          f"Team asset operation for {slug} has an invalid canonical slug.")

  return errors


def _verify_local_asset_files(plan: dict,
                              settings: Settings) -> tuple[dict, list[str]]:
  from app.assets import read_manifest, validate_image_bytes

  summary = {
      "total": 0,
      "verified": 0,
      "missing": 0,
      "mismatched": 0,
      "playerOperations": 0
  }
  errors = []
  manifest = read_manifest(settings).get("teams", {})
  operations = plan.get("teamOperations", [])
  summary["total"] = len(operations) if isinstance(operations, list) else 0

  for operation in operations if isinstance(operations, list) else []:
    logo = operation.get("logo") or {}
    slug = (operation.get("reference") or {}).get("slug")
    identity = logo.get("storageIdentity")
    mime_type = logo.get("mimeType")
    extension = MIME_EXTENSIONS.get(mime_type) if isinstance(mime_type,
                                                             str) else None
    expected_identity = f"team-logos/{slug}{extension}" if extension and isinstance(
        slug, str) else None

    if identity != expected_identity:
      summary["mismatched"] += 1
      errors.append(f"Local Team logo identity is invalid for {slug}.")
      continue

    root = (settings.asset_directory / "team-logos").resolve()
    file_path = (root / f"{slug}{extension}").resolve()

    if file_path.parent != root:
      summary["mismatched"] += 1
      errors.append(f"Local Team logo path is invalid for {slug}.")
      continue

    try:
      content = file_path.read_bytes()
    except OSError:
      summary["missing"] += 1
      errors.append(f"Local Team logo file is missing for {slug}.")
      continue

    digest = hashlib.sha256(content).hexdigest()
    _, validation_error = validate_image_bytes(content, str(mime_type or ""))
    manifest_entry = manifest.get(slug) if isinstance(manifest, dict) else None
    manifest_matches = (isinstance(manifest_entry, dict)
                        and manifest_entry.get("storageIdentity") == identity
                        and manifest_entry.get("contentSha256")
                        == logo.get("contentSha256")
                        and manifest_entry.get("mimeType") == mime_type
                        and manifest_entry.get("sizeBytes") == len(content))

    if (validation_error or digest != logo.get("contentSha256")
        or len(content) != logo.get("sizeBytes") or not manifest_matches):
      summary["mismatched"] += 1
      errors.append(f"Local Team logo file verification failed for {slug}.")
    else:
      summary["verified"] += 1

  return summary, errors


def verify_asset_plan_integrity(plan: dict,
                                plan_path: Path,
                                settings: Settings | None = None) -> list[str]:
  errors = []
  stored = plan.get("planDigest")

  if not isinstance(stored, str) or calculate_plan_digest(plan) != stored:
    errors.append(
        "Asset Write Plan digest is missing or has changed; regenerate and review the plan."
    )

  if not plan_path.is_file():
    errors.append("Specified Asset Write Plan file is missing.")

  schedule = plan.get("schedulePlan") or {}
  cache_identity = plan.get("liquipediaCacheIdentity") or {}
  source_files = [("Schedule Write Plan", schedule.get("path"),
                   schedule.get("sha256")),
                  ("Liquipedia event cache", cache_identity.get("path"),
                   cache_identity.get("cacheSha256"))]

  for source in plan.get("cacheSources", []):
    if source.get("kind") != "event-parse":
      errors.append(
          "Team-only Asset Write Plan contains an unsupported cache source.")

    source_files.append(("Liquipedia event cache", source.get("path"),
                         source.get("cacheSha256")))

  for label, raw_path, expected_digest in source_files:
    if not raw_path or not expected_digest:
      errors.append(f"{label} path or SHA-256 is missing.")
      continue

    try:
      actual = sha256_file(Path(raw_path).expanduser().resolve())
    except OSError:
      errors.append(f"{label} is missing or unreadable.")
      continue

    if actual.casefold() != str(expected_digest).casefold():
      errors.append(
          f"{label} SHA-256 changed; regenerate and review the plan.")

  if schedule.get("path"):
    try:
      schedule_path = Path(schedule["path"]).expanduser().resolve()
      schedule_plan = load_write_plan(schedule_path)
      errors.extend(
          f"Schedule Write Plan integrity: {error}"
          for error in verify_plan_integrity(schedule_plan, schedule_path))
      errors.extend(f"Schedule Write Plan validation: {error}"
                    for error in _validate_plan(schedule_plan))

      if schedule_plan.get("planDigest") != schedule.get("planDigest"):
        errors.append(
            "Asset Write Plan refers to a different Schedule Write Plan digest."
        )

      if plan.get("eventSlug") != schedule_plan.get("eventSlug"):
        errors.append(
            "Asset Write Plan Tournament identity differs from the Schedule Write Plan."
        )

      expected_references = _team_references(schedule_plan, [])
      actual_references = plan.get("teamReferences")
      actual_references = actual_references if isinstance(
          actual_references, list) else []
      key = lambda item: (item.get("slug", ""), item.get("sourceProvider", ""),
                          item.get("sourceExternalId", "")) if isinstance(
                              item, dict) else ("", "", "")

      if sorted(expected_references, key=key) != sorted(actual_references,
                                                        key=key):
        errors.append(
            "Asset Write Plan Team references differ from the canonical Schedule Write Plan."
        )

      scheduled_cache = schedule_plan.get("liquipediaCacheIdentity") or {}

      if scheduled_cache.get("cacheSha256") != cache_identity.get(
          "cacheSha256"):
        errors.append(
            "Asset Write Plan Liquipedia cache differs from its Schedule Write Plan."
        )
    except (OSError, ValueError) as error:
      errors.append(
          f"Schedule Write Plan could not be verified ({type(error).__name__})."
      )

  if settings is not None:
    manual_slugs = manually_managed_team_logos(settings)

    for operation in plan.get("teamOperations", []):
      slug = (operation.get("reference") or {}).get("slug")

      if slug in manual_slugs:
        errors.append(
            f"Asset Write Plan attempts to replace manually maintained Team logo {slug}."
        )

    _, local_errors = _verify_local_asset_files(plan, settings)
    errors.extend(local_errors)

  return errors + _validate_asset_plan(plan)


def _public_preflight(inspection: dict) -> dict:
  return {
      key: value
      for key, value in inspection.items() if key not in
      {"tournament", "teamDocumentsBySlug", "operationDocuments"}
  }


def preflight_asset_plan(plan: dict,
                         settings: Settings,
                         plan_path: Path,
                         post_apply: bool = False) -> tuple[dict, Path]:
  generated_at = datetime.now(timezone.utc)
  report = {
      "runId": str(uuid.uuid4()),
      "generatedAt": _iso_utc(generated_at),
      "writePlanPath": str(plan_path.expanduser().resolve()),
      "planDigest": plan.get("planDigest"),
      "sourceDigests": {
          "schedulePlanSha256": (plan.get("schedulePlan") or {}).get("sha256"),
          "liquipediaCacheSha256": (plan.get("liquipediaCacheIdentity")
                                    or {}).get("cacheSha256"),
          "cacheSources": (plan.get("sourceDigests")
                           or {}).get("cacheSources", [])
      },
      "coverage": plan.get("coverage", {}),
      "localFileVerification": {
          "total": len(plan.get("teamOperations", [])),
          "verified": None,
          "missing": None,
          "mismatched": None,
          "playerOperations": 0
      },
      "playerOperations": 0,
      "rejectedUrls": plan.get("rejectedUrls", []),
      "teams": (plan.get("entityCounts") or {}).get("teams", {}),
      "plannedCreate": None,
      "plannedUpdate": None,
      "plannedUnchanged": None,
      "estimatedWriteUnits": None,
      "missingLogoCount": None,
      "changedLogoUrlCount": None,
      "skipped": len(plan.get("skips", [])),
      "indexConflicts": [],
      "duplicateKeyRisks": [],
      "canonicalReferenceRisks": [],
      "warnings": plan.get("warnings", []),
      "blockingErrors": plan.get("blockingErrors", []),
      "plannedImportStatus": None,
      "postApplyVerification": {
          "requested": post_apply,
          "verified": False
      },
      "mongodbConnected": False,
      "mongodbWrites": 0,
      "errors": []
  }
  local_summary, _ = _verify_local_asset_files(plan, settings)
  report["localFileVerification"] = local_summary
  errors = verify_asset_plan_integrity(plan, plan_path, settings)

  if errors:
    report["errors"].extend(errors)
  else:
    from app.database import create_mongodb_client

    client = None

    try:
      client = create_mongodb_client(settings)
      client.admin.command("ping")
      report["mongodbConnected"] = True
      inspection = _database_preflight(client[settings.mongodb_database], plan)
      report.update(_public_preflight(inspection))
      blockers = _database_blockers(inspection)
      report["errors"].extend(blockers)

      if post_apply:
        status_doc = (inspection.get("tournament")
                      or {}).get("importStatus") or {}
        assets_status = ((status_doc.get("imports") or {}).get("assets") or {})
        is_recorded = ((status_doc.get("planDigests")
                        or {}).get("assets") == plan.get("planDigest")
                       and status_doc.get("assetsImportedAt") is not None
                       and assets_status.get("lastSuccessfulAt") is not None)
        is_unchanged = (inspection.get("plannedUpdate") == 0
                        and inspection.get("plannedUnchanged") == len(
                            plan.get("teamOperations", [])))
        report["postApplyVerification"] = {
            "requested": True,
            "verified": bool(is_recorded and is_unchanged and not blockers)
        }

        if not report["postApplyVerification"]["verified"]:
          report["errors"].append(
              "Post-apply preflight did not confirm the recorded plan and unchanged Team logo fields."
          )
    except Exception as error:
      report["errors"].append(
          f"Asset Preflight failed ({type(error).__name__}); MongoDB writes remain 0."
      )
    finally:
      if client is not None:
        client.close()

  filename = f"asset-import-preflight-{generated_at.strftime('%Y%m%dT%H%M%S%fZ')}.json"
  report_path = settings.report_directory / filename
  report["reportPath"] = str(report_path.resolve())
  write_json_atomic(report_path, report, allow_nan=False)

  return report, report_path


def apply_asset_plan(plan: dict,
                     settings: Settings,
                     plan_path: Path,
                     max_writes: int | None = None) -> dict:
  result = {
      "mongodbConnected": False,
      "mongodbWrites": 0,
      "created": 0,
      "updated": 0,
      "unchanged": 0,
      "skipped": len(plan.get("skips", [])),
      "playerOperations": 0,
      "failed": 0,
      "errors": verify_asset_plan_integrity(plan, plan_path, settings)
  }

  if result["errors"]:
    return result

  from app.database import create_mongodb_client

  client = None
  apply_started = False
  metadata_recorded = False

  try:
    client = create_mongodb_client(settings)
    client.admin.command("ping")
    result["mongodbConnected"] = True
    database = client[settings.mongodb_database]
    inspection = _database_preflight(database, plan)
    blockers = _database_blockers(inspection)

    if blockers:
      result["errors"].extend(blockers)

      return result

    if max_writes is not None and inspection[
        "estimatedWriteUnits"] > max_writes:
      result["errors"].append(
          "Asset Apply estimate exceeds the configured safety limit; no Team or status documents were written."
      )
      result["estimatedWriteUnits"] = inspection["estimatedWriteUnits"]

      return result

    latest_errors = verify_asset_plan_integrity(plan, plan_path, settings)

    if latest_errors:
      result["errors"].extend(latest_errors)

      return result

    apply_started = True
    now = datetime.now(timezone.utc)
    operation_documents = inspection["operationDocuments"]

    for operation in plan.get("teamOperations", []):
      slug = operation["reference"]["slug"]
      current_state = operation_documents.get(slug)

      if current_state is None:
        raise RuntimeError(
            "Team operation target changed after read-only Preflight.")

      changes = current_state["changed"]

      if not changes:
        result["unchanged"] += 1
        continue

      document = current_state["document"]
      update_values = dict(changes)
      update_values["updatedAt"] = now
      update_result = database["teams"].update_one(
          {
              "_id": document["_id"],
              "slug": slug
          }, {"$set": update_values},
          upsert=False)

      if update_result.matched_count != 1:
        raise RuntimeError("Team operation target changed during Apply.")

      result["updated"] += 1
      result["mongodbWrites"] += 1

    applied_status = expected_status(result["skipped"])
    record_import_status(
        database, plan["eventSlug"], "assets", applied_status,
        plan.get("dataAsOf"), plan.get("planDigest"), {
            "liquipediaCacheSha256":
            (plan.get("liquipediaCacheIdentity") or {}).get("cacheSha256")
        })
    metadata_recorded = True
    result["mongodbWrites"] += 1
    result["status"] = applied_status
  except Exception as error:
    result["failed"] += 1
    result["errors"].append(
        f"Asset Apply failed ({type(error).__name__}); sensitive settings were not logged."
    )

    if apply_started and client is not None and not metadata_recorded:
      try:
        record_import_status(client[settings.mongodb_database],
                             plan["eventSlug"], "assets", "failed", None, None)
        result["mongodbWrites"] += 1
      except Exception:
        result["errors"].append("Failed import status could not be recorded.")
  finally:
    if client is not None:
      client.close()

  return result


def load_asset_plan(path: Path) -> dict:
  try:
    return _read_json(path)
  except (OSError, UnicodeError, json.JSONDecodeError) as error:
    raise ValueError(
        "Specified Asset Write Plan is missing or invalid JSON.") from error
