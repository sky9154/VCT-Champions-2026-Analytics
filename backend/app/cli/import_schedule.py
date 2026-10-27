import argparse
import sys
from pathlib import Path
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.importers.runner import run_reconciliation
from app.importers.process_lock import ImportLockBusy, ImportProcessLock
from app.importers.write_plan import (apply_write_plan, build_write_plan,
                                      load_write_plan, preflight_write_plan,
                                      write_plan_file)
from app.utils.logging import configure_logging

GENERATION_ARGUMENTS = ("event", "liquipedia_page", "duckdb_path",
                        "output_directory", "cache_directory")


def _build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      description=
      "Create, preflight, or apply a reviewed schedule/results Write Plan.")
  mode = parser.add_mutually_exclusive_group(required=True)
  mode.add_argument(
      "--offline",
      action="store_true",
      help="Read existing sources and create a new dry-run Write Plan.")
  mode.add_argument("--apply-plan",
                    type=Path,
                    help="Apply exactly the specified reviewed Write Plan.")
  mode.add_argument(
      "--preflight-plan",
      type=Path,
      help="Read-only MongoDB preflight for the specified Write Plan.")

  parser.add_argument("--event")
  parser.add_argument("--liquipedia-page")
  parser.add_argument("--duckdb-path", type=Path)
  parser.add_argument("--output-directory", type=Path)
  parser.add_argument("--cache-directory", type=Path)
  parser.add_argument("--verbose", action="store_true")

  return parser


def _resolve_path(settings: Settings, value: Path | None,
                  default: Path) -> Path:
  return settings.resolve_backend_path(value if value is not None else default)


def _print_plan_summary(plan: dict, path: Path) -> None:
  planned = plan.get("plannedCounts", {})
  entities = plan.get("entityCounts", {})

  print(f"Event: {plan.get('eventSlug')}")
  print(f"Tournament operations: {entities.get('tournamentUpserts', 0)}")
  print(f"Team operations: {entities.get('teamUpserts', 0)}")
  print(f"Match operations: {entities.get('matchUpserts', 0)}")
  print(f"Skipped matches: {entities.get('matchSkips', 0)}")

  schedule_counts = plan.get("scheduleCounts", {})
  stats_coverage = plan.get("statsCoverage", {})

  print(f"TBD matches: {schedule_counts.get('tbdMatches', 0)}")
  print(f"Stats available: {stats_coverage.get('available', 0)}")
  print(f"Stats unavailable: {stats_coverage.get('unavailable', 0)}")
  print(f"Stats ambiguous: {stats_coverage.get('ambiguous', 0)}")
  print(f"Schedule warnings: {schedule_counts.get('warning', 0)}")
  print(f"Planned: {planned.get('upsert', 0)} upserts")
  print(f"Created: unknown; updated: unknown; unchanged: unknown")
  print(f"Skipped: {planned.get('skip', 0)}")
  print("Failed: 0")
  print("MongoDB connected: no")
  print("MongoDB writes: 0")
  print(f"Write Plan path: {path}")
  print(f"Blocking errors: {len(plan.get('blockingErrors', []))}")


def _print_preflight_summary(report: dict, report_path: Path) -> None:
  for field in ("plannedCreate", "plannedUpdate", "plannedUnchanged",
                "skipped"):
    print(f"{field}: {report.get(field)}")

  print(f"Index conflicts: {len(report.get('indexConflicts', []))}")
  print(f"Duplicate key risks: {len(report.get('duplicateKeyRisks', []))}")
  print(
      f"Completed match downgrade risks: {len(report.get('completedMatchDowngradeRisks', []))}"
  )
  print(
      f"Canonical reference risks: {len(report.get('canonicalReferenceRisks', []))}"
  )
  print(
      f"Match operations skipped for individual risks: {len({item.get('externalMatchId') for item in report.get('matchOperationRisks', []) if item.get('externalMatchId')})}"
  )
  print(
      f"Ready for Apply: {'yes' if report.get('readyForApply') is True else 'no'}"
  )

  print(f"Estimated write units: {report.get('estimatedWriteUnits')}")
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
  print(
      f"Planned: {result.get('created', 0) + result.get('updated', 0) + result.get('unchanged', 0)}"
  )

  for field in ("created", "updated", "unchanged", "skipped", "failed"):
    print(f"{field.capitalize()}: {result.get(field, 0)}")

  print(f"MongoDB connected: {'yes' if result.get('connected') else 'no'}")
  print(f"MongoDB writes: {result.get('writes', 0)}")
  print(
      f"Import status: {result.get('importStatus', 'failed' if result.get('failed') or result.get('errors') else 'not-applied')}"
  )
  print(f"Write Plan path: {plan_path}")

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
  is_plan_mode = args.apply_plan is not None or args.preflight_plan is not None

  if is_plan_mode and any(
      getattr(args, name) is not None for name in GENERATION_ARGUMENTS):
    parser.error(
        "--apply-plan/--preflight-plan cannot be combined with parameters for creating a new plan."
    )

  if args.offline:
    event_slug = args.event or "valorant-champions-2026"
    liquipedia_page = args.liquipedia_page or settings.liquipedia_page_title
    database_path = _resolve_path(settings, args.duckdb_path,
                                  settings.reference_database_path)
    output_directory = _resolve_path(settings, args.output_directory,
                                     settings.report_directory)
    cache_directory = _resolve_path(settings, args.cache_directory,
                                    settings.cache_directory)

    try:
      report, reconciliation_path, _ = run_reconciliation(
          settings=settings,
          event_slug=event_slug,
          liquipedia_page=liquipedia_page,
          database_path=database_path,
          output_directory=output_directory,
          cache_directory=cache_directory,
          refresh=False,
          offline=True)

      plan = build_write_plan(settings, report, reconciliation_path,
                              database_path)
      path = write_plan_file(plan, output_directory)
    except (OSError, ValueError) as error:
      print(f"Offline plan generation failed ({type(error).__name__}).",
            file=sys.stderr)

      return 2

    _print_plan_summary(plan, path)

    return 1 if plan.get("blockingErrors") else 0

  selected_plan = args.apply_plan or args.preflight_plan

  if selected_plan is None:
    parser.error("A Plan path is required.")

  plan_path = settings.resolve_backend_path(selected_plan)

  try:
    plan = load_write_plan(plan_path)
  except ValueError as error:
    print(f"{error} MongoDB connected: no; MongoDB writes: 0.",
          file=sys.stderr)

    return 2

  if args.preflight_plan is not None:
    report, report_path = preflight_write_plan(plan, settings, plan_path)
    _print_preflight_summary(report, report_path)

    return 1 if report.get("errors") or report.get(
        "readyForApply") is not True else 0

  try:
    with ImportProcessLock(settings.runtime_directory,
                           settings.auto_update_lock_stale_minutes):
      result = apply_write_plan(
          plan,
          settings,
          plan_path,
          max_writes=settings.auto_update_max_schedule_writes)
  except ImportLockBusy:
    print(
        "Apply skipped because another importer owns the process lock. MongoDB connected: no; MongoDB writes: 0.",
        file=sys.stderr)

    return 2

  _print_apply_summary(result, plan_path)

  return 1 if result.get("errors") or result.get("failed") else 0


if __name__ == "__main__":
  raise SystemExit(main())
