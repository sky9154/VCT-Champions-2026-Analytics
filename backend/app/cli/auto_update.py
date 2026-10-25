import argparse
import sys

from pydantic import ValidationError

from app.config import get_settings
from app.importers.auto_updater import run_continuously, run_once_with_lock
from app.importers.process_lock import ImportLockBusy
from app.utils.logging import configure_logging


def _build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
      description="Run the scheduled Liquipedia and VCT data updater.")

  parser.add_argument("--once",
                      action="store_true",
                      help="Run one manually requested update cycle and exit.")
  parser.add_argument(
      "--dry-run",
      action="store_true",
      help="Fetch and preflight without applying MongoDB changes.")
  parser.add_argument("--verbose", action="store_true")

  return parser


def _print_summary(report: dict, report_path) -> None:
  print(f"Updater status: {report.get('status')}")
  print(f"Mode: {report.get('mode')}")
  print(
      f"Lock acquired: {'yes' if (report.get('lock') or {}).get('acquired') else 'no'}"
  )
  print(
      f"MongoDB connected: {'yes' if (report.get('mongodb') or {}).get('connected') else 'no'}"
  )
  print(f"MongoDB writes: {(report.get('mongodb') or {}).get('writes', 0)}")
  print(
      f"Apply started: {'yes' if (report.get('mongodb') or {}).get('applyStarted') else 'no'}"
  )

  for name, stage in (report.get("stages") or {}).items():
    print(f"{name.capitalize()} stage: {stage.get('status')}")

    for key, value in (stage.get("paths") or {}).items():
      print(f"  {key}: {value}")

    for key in ("planPath", "preflightPath", "postApplyPreflightPath",
                "coveragePath"):
      if stage.get(key):
        print(f"  {key}: {stage[key]}")

  for error in report.get("errors", []):
    print(
        f"Error {error.get('code')} ({error.get('stage')}): {error.get('message')}"
    )

  for warning in report.get("warnings", []):
    print(f"Warning: {warning}")

  print(f"Run report: {report_path}")


def main() -> int:
  parser = _build_parser()
  args = parser.parse_args()

  if args.dry_run and not args.once:
    parser.error("--dry-run is available only with --once.")

  try:
    settings = get_settings()
  except (ValidationError, ValueError):
    print("Configuration error; sensitive settings were not displayed.",
          file=sys.stderr)

    return 2

  configure_logging(args.verbose)

  if not args.once:
    return run_continuously(settings)

  try:
    report, report_path = run_once_with_lock(settings, dry_run=args.dry_run)
  except ImportLockBusy:
    print("Updater skipped because another importer owns the process lock.",
          file=sys.stderr)

    return 2

  _print_summary(report, report_path)

  return 1 if report.get("errors") else 0


if __name__ == "__main__":
  raise SystemExit(main())
