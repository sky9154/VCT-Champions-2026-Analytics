from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

BACKEND_DIRECTORY = Path(__file__).resolve().parents[2]
RUNTIME_DIRECTORY = BACKEND_DIRECTORY / "var" / "run"
API_LOCK_PATH = RUNTIME_DIRECTORY / "api-process.lock"
API_STOP_PATH = RUNTIME_DIRECTORY / "api.stop"
API_HOST = "127.0.0.1"
API_PORT = 8591


def validate_environment() -> None:
  expected_name = "vct-analytics"
  prefix = Path(sys.prefix).resolve()
  prefix_matches = prefix.name.casefold() == expected_name
  default_name_matches = os.environ.get("CONDA_DEFAULT_ENV",
                                        "").casefold() == expected_name
  conda_prefix = os.environ.get("CONDA_PREFIX")
  conda_prefix_matches = False

  if conda_prefix:
    try:
      conda_prefix_matches = Path(conda_prefix).resolve() == prefix
    except OSError:
      conda_prefix_matches = False

  if not prefix_matches and not (default_name_matches and conda_prefix_matches):
    raise RuntimeError("請先啟用 VCT-Analytics Python 環境。")


class ApiProcessLock:
  """An OS-owned singleton lock; the lock file contains no process metadata."""

  def __init__(self, path: Path = API_LOCK_PATH):
    self.path = path
    self._file = None

  def acquire(self) -> bool:
    self.path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
    lock_file = os.fdopen(descriptor, "r+b", buffering=0)

    try:
      if os.fstat(descriptor).st_size == 0:
        lock_file.write(b"\0")

      lock_file.seek(0)

      if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
    except (OSError, BlockingIOError):
      lock_file.close()

      return False

    self._file = lock_file

    return True

  def release(self) -> None:
    lock_file = self._file
    self._file = None

    if lock_file is None:
      return

    try:
      lock_file.seek(0)

      if os.name == "nt":
        import msvcrt

        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
      lock_file.close()


def is_api_lock_held() -> bool | None:
  if not API_LOCK_PATH.exists():
    return False

  probe = ApiProcessLock()

  try:
    acquired = probe.acquire()
  except OSError:
    return None

  if not acquired:
    return True

  probe.release()

  return False


def _read_stop_token() -> str | None:
  try:
    token = API_STOP_PATH.read_text(encoding="utf-8").strip()
  except OSError:
    return None

  return token or None


def _remove_stop_token(token: str | None) -> None:
  if not token:
    return

  try:
    if API_STOP_PATH.read_text(encoding="utf-8").strip() == token:
      API_STOP_PATH.unlink(missing_ok=True)
  except OSError:
    pass


def main() -> int:
  try:
    validate_environment()
  except RuntimeError as error:
    print(str(error), file=sys.stderr)

    return 2

  os.chdir(BACKEND_DIRECTORY)
  backend_text = str(BACKEND_DIRECTORY)

  if backend_text not in sys.path:
    sys.path.insert(0, backend_text)

  lock = ApiProcessLock()

  if not lock.acquire():
    print("VCT Analytics API 已有程序持有執行鎖。", file=sys.stderr)

    return 3

  stop_seen = threading.Event()
  watcher_done = threading.Event()
  stop_token: list[str | None] = [None]
  server = None

  try:
    from uvicorn import Config, Server

    configuration = Config("app.main:app",
                           host=API_HOST,
                           port=API_PORT,
                           workers=1,
                           reload=False)
    server = Server(configuration)

    def watch_stop_request() -> None:
      while not watcher_done.wait(0.25):
        token = _read_stop_token()

        if token is not None:
          stop_token[0] = token
          stop_seen.set()
          server.should_exit = True

          return

    watcher = threading.Thread(target=watch_stop_request,
                               name="api-stop-watch",
                               daemon=True)

    watcher.start()
    print("VCT Analytics API 啟動於 http://127.0.0.1:8591")

    server.run()

    return 0
  except KeyboardInterrupt:
    return 0
  except Exception:
    import traceback

    traceback.print_exc(file=sys.stderr)

    return 1
  finally:
    watcher_done.set()

    if stop_seen.is_set():
      _remove_stop_token(stop_token[0])

    lock.release()


if __name__ == "__main__":
  raise SystemExit(main())
