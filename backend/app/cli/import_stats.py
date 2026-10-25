import argparse
import sys
from pathlib import Path
from pydantic import ValidationError

from app.config import get_settings
from app.importers.models import ImportRunError
from app.importers.process_lock import ImportLockBusy, ImportProcessLock
from app.importers.stats_importer import (apply_stats_plan, build_stats_plan,
                                          load_stats_plan,
                                          preflight_stats_plan,
                                          record_stats_import_success,
                                          write_stats_plan_file)
from app.utils.logging import configure_logging


def _build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      description="Create, preflight, or apply a reviewed VCT stats Write Plan."
  )
  mode = parser.add_mutually_exclusive_group(required=True)
  mode.add_argument(
      "--offline",
      action="store_true",
      help="Read existing sources and create a dry-run Stats Write Plan.")
  mode.add_argument(
      "--preflight-plan",
      type=Path,
      help="Read-only MongoDB preflight for a specified Stats Write Plan.")
  mode.add_argument(
      "--apply-plan",
      type=Path,
      help="Apply exactly the specified reviewed Stats Write Plan.")
  parser.add_argument("--verbose", action="store_true")

  return parser


def _print_offline_summary(plan: dict, path: Path) -> None:
  counts = plan.get("plannedCounts", {})
  reversed_order = plan.get("teamOrderReversed", {})

  print(f"Event: {plan.get('eventSlug')}")
  print(f"Verified Match mappings: {len(plan.get('matchMappings', []))}")
  print(f"Match Maps: {counts.get('matchMaps', 0)}")
  print(f"Players: {counts.get('players', 0)}")
  print(f"Player Map Stats: {counts.get('playerMapStats', 0)}")
  print(f"Team Map Stats: {counts.get('teamMapStats', 0)}")
  print(f"teamOrderReversed matches: {reversed_order.get('count', 0)}")
  print(f"Blocked Matches: {len(plan.get('blockedMatchExternalIds', []))}")
  print(f"Warnings: {len(plan.get('warnings', []))}")
  print(f"Blocking errors: {len(plan.get('blockingErrors', []))}")
  print("MongoDB connected: no")
  print("MongoDB writes: 0")
  print(f"Stats Write Plan path: {path}")

  for warning in plan.get("warnings", []):
    print(f"Warning: {warning}")

  for error in plan.get("blockingErrors", []):
    print(f"Blocking error: {error}")


def _print_preflight_summary(report: dict, report_path: Path) -> None:
  for field in ("plannedCreate", "plannedUpdate", "plannedUnchanged",
                "skipped"):
    print(f"{field}: {report.get(field)}")

  for collection, counts in (report.get("byCollection") or {}).items():
    print(
        f"{collection}: create={counts.get('create', 0)}, "
        f"update={counts.get('update', 0)}, unchanged={counts.get('unchanged', 0)}"
    )

  print(
      f"Canonical completed Matches: {report.get('completedCanonicalMatches', 0)}"
  )
  print(f"Index conflicts: {len(report.get('indexConflicts', []))}")
  print(f"Duplicate key risks: {len(report.get('duplicateKeyRisks', []))}")
  print(
      f"Map identity conflicts: {len(report.get('mapIdentityConflicts', []))}")
  print(
      f"Missing canonical references: {len(report.get('missingCanonicalReferences', []))}"
  )
  print(
      f"Documents checked for null-preserving merge: {(report.get('nullPreservingMerge') or {}).get('existingDocumentsChecked', 0)}"
  )

  planned_status = report.get("plannedImportStatus") or {}

  if planned_status:
    print(f"Planned import status: {planned_status.get('status')}")
    print(f"Planned dataAsOf: {planned_status.get('sourceDataAsOf')}")
    print(f"Planned plan digest: {planned_status.get('planDigest')}")

  print(
      f"MongoDB connected: {'yes' if report.get('mongodbConnected') else 'no'}"
  )
  print(f"MongoDB writes: {report.get('mongodbWrites', 0)}")
  print(f"Preflight report path: {report_path}")

  for error in report.get("errors", []):
    print(f"Preflight error: {error}")


def _print_apply_summary(result: dict, plan_path: Path) -> None:
  for field in ("created", "updated", "unchanged", "skipped", "failed"):
    print(f"{field.capitalize()}: {result.get(field, 0)}")

  for collection, counts in result.get("byCollection", {}).items():
    print(
        f"{collection}: created={counts.get('created', 0)}, "
        f"updated={counts.get('updated', 0)}, unchanged={counts.get('unchanged', 0)}"
    )

  print(
      f"MongoDB connected: {'yes' if result.get('mongodbConnected') else 'no'}"
  )
  print(f"MongoDB writes: {result.get('mongodbWrites', 0)}")
  print(
      f"Import status: {result.get('importStatus', 'failed' if result.get('failed') or result.get('errors') else 'not-applied')}"
  )
  print(f"Stats Write Plan path: {plan_path}")

  for error in result.get("errors", []):
    print(f"Apply error: {error}")


def main() -> int:
  parser = _build_parser()
  args = parser.parse_args()

  try:
    settings = get_settings()
  except (ValidationError, ValueError):
    print("Configuration error; sensitive settings were not displayed.",
          file=sys.stderr)

    return 2

  configure_logging(args.verbose)

  if args.offline:
    try:
      plan = build_stats_plan(settings)
      plan_path = write_stats_plan_file(plan, settings.report_directory)
    except (ImportRunError, OSError, ValueError) as error:
      print(
          f"Offline Stats plan generation failed ({type(error).__name__}): {error}",
          file=sys.stderr)

      return 2

    _print_offline_summary(plan, plan_path)

    return 1 if plan.get("blockingErrors") else 0

  plan_path = settings.resolve_backend_path(args.preflight_plan
                                            or args.apply_plan)

  try:
    plan = load_stats_plan(plan_path)
  except ImportRunError as error:
    print(f"{error} MongoDB connected: no; MongoDB writes: 0.",
          file=sys.stderr)

    return 2

  if args.preflight_plan is not None:
    report, report_path = preflight_stats_plan(plan, settings, plan_path)
    _print_preflight_summary(report, report_path)

    return 1 if report.get("errors") else 0

  try:
    with ImportProcessLock(settings.runtime_directory,
                           settings.auto_update_lock_stale_minutes):
      result = apply_stats_plan(plan, settings, plan_path)

      if not result.get("errors") and not result.get("failed"):
        postflight, postflight_path = preflight_stats_plan(
            plan, settings, plan_path)
        result["postflightReportPath"] = str(postflight_path)
        import_result = "partial" if result.get(
            "skipped", 0) else ("applied" if result.get("created", 0)
                                or result.get("updated", 0) else "unchanged")
        recorded = record_stats_import_success(plan, settings, postflight,
                                               import_result)
        result["mongodbConnected"] = result.get(
            "mongodbConnected") or recorded.get("mongodbConnected") is True
        result["mongodbWrites"] = result.get(
            "mongodbWrites", 0) + recorded.get("mongodbWrites", 0)

        if not recorded.get("recorded"):
          result["failed"] = result.get("failed", 0) + 1
          result.setdefault("errors", []).append(
              "Stats Postflight or success metadata verification did not complete."
          )
          result["importStatus"] = "failed"
        else:
          result["importStatus"] = import_result
  except ImportLockBusy:
    print(
        "Apply skipped because another importer owns the process lock. MongoDB connected: no; MongoDB writes: 0.",
        file=sys.stderr)

    return 2

  _print_apply_summary(result, plan_path)

  return 1 if result.get("errors") or result.get("failed") else 0


if __name__ == "__main__":
  raise SystemExit(main())
