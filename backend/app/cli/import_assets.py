import argparse
import sys
from pathlib import Path

from pydantic import ValidationError

from app.config import get_settings
from app.importers.asset_write_plan import (apply_asset_plan, build_asset_plan,
                                            load_asset_plan,
                                            preflight_asset_plan,
                                            write_asset_coverage_report,
                                            write_asset_plan_file)
from app.importers.process_lock import ImportLockBusy, ImportProcessLock
from app.utils.logging import configure_logging


def _build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      description=
      "Prepare, preflight, or apply a reviewed Liquipedia Team logo Asset Write Plan."
  )
  mode = parser.add_mutually_exclusive_group(required=True)
  mode.add_argument(
      "--offline",
      action="store_true",
      help="Read existing Liquipedia caches only and create a dry-run plan.")
  mode.add_argument(
      "--prepare",
      action="store_true",
      help=
      "Download and validate Team logos from cached Liquipedia image references; do not connect to MongoDB."
  )
  mode.add_argument(
      "--preflight-plan",
      type=Path,
      help="Read-only MongoDB preflight for the specified Asset Write Plan.")
  mode.add_argument(
      "--post-apply-preflight-plan",
      type=Path,
      help=
      "Read-only verification that the specified Asset Write Plan was applied and is now unchanged."
  )
  mode.add_argument(
      "--apply-plan",
      type=Path,
      help="Apply exactly the specified reviewed Asset Write Plan.")
  parser.add_argument("--verbose", action="store_true")

  return parser


def _print_plan_summary(plan: dict, path: Path, coverage_path: Path) -> None:
  entities = plan.get("entityCounts", {})
  planned = plan.get("plannedCounts", {})
  counts = entities.get("teams", {})

  print(
      f"Team logos: total={counts.get('total', 0)}, resolved={counts.get('resolved', 0)}, "
      f"missing={counts.get('missing', 0)}, ambiguous={counts.get('ambiguous', 0)}"
  )
  print(
      f"Player portrait operations: {plan.get('playerPortraitOperations', 0)} (out of scope)"
  )
  print(f"Planned create: {planned.get('create', 0)}")
  print(f"Planned update candidates: {planned.get('update', 0)}")
  print("Planned unchanged: unknown until MongoDB Preflight")
  print(f"Planned skip: {planned.get('skip', 0)}")

  local_files = plan.get("localFiles", {})

  print(
      f"Local image files: verified={local_files.get('verified', 0)}, created={local_files.get('created', 0)}, "
      f"reused={local_files.get('reused', 0)}, failed={local_files.get('failed', 0)}"
  )
  print(f"Rejected URL: {len(plan.get('rejectedUrls', []))}")
  print(f"Cache sources: {len(plan.get('cacheSources', []))}")
  print(
      f"Cache SHA-256: {plan.get('sourceDigests', {}).get('liquipediaCacheSha256') if plan.get('sourceDigests') else plan.get('liquipediaCacheIdentity', {}).get('cacheSha256')}"
  )
  print(f"Plan SHA-256: {plan.get('planDigest')}")
  print("MongoDB connected: no")
  print("MongoDB writes: 0")
  print(f"Asset Write Plan path: {path}")
  print(f"Coverage report path: {coverage_path}")
  print(f"Warnings: {len(plan.get('warnings', []))}")
  print(f"Blocking errors: {len(plan.get('blockingErrors', []))}")

  for warning in plan.get("warnings", []):
    print(f"Warning: {warning}")

  for error in plan.get("blockingErrors", []):
    print(f"Blocking error: {error}")


def _print_preflight_summary(report: dict, report_path: Path) -> None:
  counts = report.get("teams") or {}

  print(
      f"Team logos: total={counts.get('total', 0)}, resolved={counts.get('resolved', 0)}, "
      f"missing={counts.get('missing', 0)}, ambiguous={counts.get('ambiguous', 0)}"
  )
  print(
      f"Player portrait operations: {report.get('playerOperations', 0)} (out of scope)"
  )
  local_files = report.get("localFileVerification") or {}
  print(
      f"Local files: verified={local_files.get('verified')}/{local_files.get('total')}, "
      f"missing={local_files.get('missing')}, mismatched={local_files.get('mismatched')}"
  )

  for field in ("plannedCreate", "plannedUpdate", "plannedUnchanged",
                "skipped"):
    print(f"{field}: {report.get(field)}")

  print(f"Rejected URL: {len(report.get('rejectedUrls', []))}")
  print(f"Index conflicts: {len(report.get('indexConflicts', []))}")
  print(f"Duplicate key risks: {len(report.get('duplicateKeyRisks', []))}")
  print(
      f"Canonical reference risks: {len(report.get('canonicalReferenceRisks', []))}"
  )
  print(
      f"MongoDB connected: {'yes' if report.get('mongodbConnected') else 'no'}"
  )
  print(f"MongoDB writes: {report.get('mongodbWrites', 0)}")
  print(
      f"Post-apply verified: {(report.get('postApplyVerification') or {}).get('verified', False)}"
  )

  planned_status = report.get("plannedImportStatus") or {}

  if planned_status:
    print(f"Planned import status: {planned_status.get('status')}")

    after_apply = planned_status.get("afterSuccessfulApply") or {}

    print(f"Asset source snapshot: {planned_status.get('sourceDataAsOf')}")
    print(f"Planned analytical dataAsOf: {after_apply.get('dataAsOf')}")
    print(f"Planned nextImportAt: {after_apply.get('nextImportAt')}")
    print(
        f"Planned per-type import timestamps: {after_apply.get('scheduleImportedAt')}, {after_apply.get('statsImportedAt')}, {after_apply.get('assetsImportedAt')}"
    )
    print(f"Planned plan digest: {planned_status.get('planDigest')}")

  print(f"Preflight report path: {report_path}")

  for warning in report.get("warnings", []):
    print(f"Warning: {warning}")

  for blocking_error in report.get("blockingErrors", []):
    print(f"Blocking error: {blocking_error}")

  for error in report.get("errors", []):
    print(f"Preflight error: {error}")


def _print_apply_summary(result: dict, plan_path: Path) -> None:
  for field in ("created", "updated", "unchanged", "skipped", "failed"):
    print(f"{field.capitalize()}: {result.get(field, 0)}")

  print(
      f"Import status: {result.get('status', 'failed' if result.get('failed') or result.get('errors') else 'not-applied')}"
  )
  print(
      f"MongoDB connected: {'yes' if result.get('mongodbConnected') else 'no'}"
  )
  print(f"MongoDB writes: {result.get('mongodbWrites', 0)}")
  print(f"Asset Write Plan path: {plan_path}")

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
      plan = build_asset_plan(settings, download_assets=False)
      plan_path = write_asset_plan_file(plan, settings.report_directory)
      coverage_path = write_asset_coverage_report(plan, plan_path,
                                                  settings.report_directory)
    except (OSError, ValueError) as error:
      print(
          f"Asset plan generation failed ({type(error).__name__}). MongoDB connected: no; MongoDB writes: 0.",
          file=sys.stderr)

      return 2

    _print_plan_summary(plan, plan_path, coverage_path)

    return 1 if plan.get("blockingErrors") else 0

  if args.prepare:
    try:
      with ImportProcessLock(settings.runtime_directory,
                             settings.auto_update_lock_stale_minutes):
        plan = build_asset_plan(settings, download_assets=True)
        plan_path = write_asset_plan_file(plan, settings.report_directory)
        coverage_path = write_asset_coverage_report(plan, plan_path,
                                                    settings.report_directory)
    except ImportLockBusy:
      print(
          "Asset preparation skipped because another importer owns the process lock. MongoDB connected: no; MongoDB writes: 0.",
          file=sys.stderr)

      return 2
    except (OSError, ValueError) as error:
      print(
          f"Asset preparation failed ({type(error).__name__}). MongoDB connected: no; MongoDB writes: 0.",
          file=sys.stderr)

      return 2

    _print_plan_summary(plan, plan_path, coverage_path)

    return 1 if plan.get("blockingErrors") else 0

  plan_path = settings.resolve_backend_path(args.preflight_plan
                                            or args.post_apply_preflight_plan
                                            or args.apply_plan)

  try:
    plan = load_asset_plan(plan_path)
  except ValueError as error:
    print(f"{error} MongoDB connected: no; MongoDB writes: 0.",
          file=sys.stderr)

    return 2

  if args.preflight_plan is not None or args.post_apply_preflight_plan is not None:
    report, report_path = preflight_asset_plan(
        plan,
        settings,
        plan_path,
        post_apply=args.post_apply_preflight_plan is not None)

    _print_preflight_summary(report, report_path)

    return 1 if report.get("errors") else 0

  try:
    with ImportProcessLock(settings.runtime_directory,
                           settings.auto_update_lock_stale_minutes):
      result = apply_asset_plan(plan, settings, plan_path)
  except ImportLockBusy:
    print(
        "Apply skipped because another importer owns the process lock. MongoDB connected: no; MongoDB writes: 0.",
        file=sys.stderr)

    return 2

  _print_apply_summary(result, plan_path)

  return 1 if result.get("errors") or result.get("failed") else 0


if __name__ == "__main__":
  raise SystemExit(main())
