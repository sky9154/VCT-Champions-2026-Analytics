import argparse
import sys
from contextlib import nullcontext
from pathlib import Path
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.importers.runner import run_reconciliation
from app.importers.process_lock import ImportLockBusy, ImportProcessLock
from app.utils.logging import configure_logging


def _build_parser(settings: Settings) -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      description="Run a read-only Liquipedia and VCT Reference reconciliation."
  )

  parser.add_argument("--event", default="valorant-champions-2026")
  parser.add_argument("--liquipedia-page",
                      default=settings.liquipedia_page_title)
  parser.add_argument("--duckdb-path",
                      type=Path,
                      default=settings.reference_database_path)
  parser.add_argument("--output-directory",
                      type=Path,
                      default=settings.report_directory)
  parser.add_argument("--cache-directory",
                      type=Path,
                      default=settings.cache_directory)
  parser.add_argument("--refresh", action="store_true")
  parser.add_argument("--offline", action="store_true")
  parser.add_argument("--verbose", action="store_true")

  return parser


def _print_summary(report: dict, report_path: Path, exit_code: int) -> None:
  summary = report["summary"]

  print(f"Event: {report['event']['slug']}")
  print(
      f"Liquipedia fetch/cache status: {report['sources']['liquipedia']['status']}"
  )
  print(f"DuckDB path: {report['sources']['vctReference']['databasePath']}")
  print(f"Liquipedia matches: {summary['liquipediaMatches']}")
  print(f"DuckDB matches: {summary['duckdbMatches']}")
  print(f"Matched: {summary['matched']}")
  print(f"Liquipedia newer: {summary['liquipediaNewer']}")
  print(f"Stats available: {summary['statsAvailable']}")
  print(f"Incomplete: {summary['incomplete']}")
  print(f"Conflicts: {summary['conflicts']}")
  print(f"Ambiguous: {summary['ambiguous']}")
  print(f"Unmatched: {summary['unmatched']}")
  print(f"Report path: {report_path}")

  if report["errors"]:
    for error in report["errors"]:
      print(f"Error ({error['provider']}): {error['message']}")

  if exit_code == 1:
    print("Review required: conflicts or ambiguous matches were found.")
  elif exit_code == 2:
    print("Dry run failed for at least one source; see the report errors.")


def main() -> int:
  try:
    settings = get_settings()
  except (ValidationError, ValueError) as error:
    print(f"Configuration error: {error}", file=sys.stderr)

    return 2

  args = _build_parser(settings).parse_args()
  configure_logging(args.verbose)

  try:
    process_lock = nullcontext() if args.offline else ImportProcessLock(
        settings.runtime_directory, settings.auto_update_lock_stale_minutes)

    with process_lock:
      report, report_path, exit_code = run_reconciliation(
          settings=settings,
          event_slug=args.event,
          liquipedia_page=args.liquipedia_page,
          database_path=settings.resolve_backend_path(args.duckdb_path),
          output_directory=settings.resolve_backend_path(
              args.output_directory),
          cache_directory=settings.resolve_backend_path(args.cache_directory),
          refresh=args.refresh,
          offline=args.offline)
  except ImportLockBusy:
    print(
        "Reconciliation skipped because another importer owns the process lock.",
        file=sys.stderr)

    return 2
  except (OSError, ValueError) as error:
    print(f"Dry run could not produce a report: {error}", file=sys.stderr)

    return 2

  _print_summary(report, report_path, exit_code)

  return exit_code


if __name__ == "__main__":
  raise SystemExit(main())
