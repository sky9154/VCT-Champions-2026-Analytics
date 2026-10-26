import ctypes
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


class ImportLockBusy(RuntimeError):
  pass


def _iso_utc(value: datetime) -> str:
  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_time(value) -> datetime | None:
  if not isinstance(value, str):
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


def _windows_process_identity(process_id: int) -> str | None:
  if os.name != "nt":
    return None

  class FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_uint32), ("high", ctypes.c_uint32)]

  kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
  kernel32.OpenProcess.argtypes = [
      ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32
  ]
  kernel32.OpenProcess.restype = ctypes.c_void_p
  kernel32.GetProcessTimes.argtypes = [
      ctypes.c_void_p,
      ctypes.POINTER(FileTime),
      ctypes.POINTER(FileTime),
      ctypes.POINTER(FileTime),
      ctypes.POINTER(FileTime)
  ]
  kernel32.GetProcessTimes.restype = ctypes.c_int
  kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
  handle = kernel32.OpenProcess(0x1000, 0, process_id)

  if not handle:
    return None

  try:
    created = FileTime()
    exited = FileTime()
    kernel = FileTime()
    user = FileTime()

    if not kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                    ctypes.byref(exited), ctypes.byref(kernel),
                                    ctypes.byref(user)):
      return None

    return f"{created.high:08x}{created.low:08x}"
  finally:
    kernel32.CloseHandle(handle)


def _process_identity(process_id: int) -> str | None:
  windows_identity = _windows_process_identity(process_id)

  if windows_identity is not None:
    return f"windows:{windows_identity}"

  if os.name != "nt":
    try:
      fields = Path(f"/proc/{process_id}/stat").read_text(
          encoding="utf-8").split()

      return f"proc:{fields[21]}"
    except (OSError, IndexError):
      return None

  return None


def _process_exists(process_id: int,
                    expected_identity: str | None) -> bool | None:
  actual_identity = _process_identity(process_id)

  if actual_identity is not None:
    return expected_identity is None or actual_identity == expected_identity

  try:
    os.kill(process_id, 0)
  except ProcessLookupError:
    return False
  except PermissionError:
    return None
  except OSError:
    return None

  return None


def is_import_process_locked(lock_directory: Path) -> bool:
  lock_path = lock_directory / "import-process.lock"

  if not lock_path.exists():
    return False

  try:
    with lock_path.open("r+b") as lock_file:
      try:
        if os.name == "nt":
          import msvcrt

          lock_file.seek(0)
          msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
          msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
          import fcntl

          fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
          fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
      except (OSError, BlockingIOError):
        return True
  except OSError:
    return True

  return False


def read_import_process_started_at(lock_directory: Path) -> datetime | None:
  lock_path = lock_directory / "import-process.lock"

  try:
    contents = lock_path.read_bytes()[1:].decode("utf-8").strip()
    metadata = json.loads(contents)
  except (OSError, UnicodeError, json.JSONDecodeError):
    return None

  if not isinstance(metadata, dict):
    return None

  return _parse_time(metadata.get("createdAt"))


class ImportProcessLock:

  def __init__(self,
               lock_directory: Path,
               stale_after_minutes: int,
               lock_name: str = "import-process.lock"):
    self.path = lock_directory / lock_name
    self.stale_after = timedelta(minutes=stale_after_minutes)
    self.file = None
    self.acquired = False

  def _read_metadata(self) -> dict | None:
    if self.file is None:
      raise ImportLockBusy("Import lock file is not open.")

    try:
      self.file.seek(1)
      contents = self.file.read().decode("utf-8").strip()
    except (OSError, UnicodeError):
      raise ImportLockBusy(
          "Import lock metadata cannot be read; refusing to proceed.")

    if not contents:
      return None

    try:
      payload = json.loads(contents)
    except json.JSONDecodeError as error:
      raise ImportLockBusy(
          "Import lock metadata is invalid; refusing to proceed.") from error

    return payload if isinstance(payload, dict) else None

  def _check_stale_metadata(self, metadata: dict | None) -> None:
    if not metadata:
      return

    process_id = metadata.get("processId")
    started_at = _parse_time(metadata.get("createdAt"))

    if not isinstance(process_id, int) or started_at is None:
      raise ImportLockBusy(
          "Import lock owner cannot be verified; refusing to proceed.")

    exists = _process_exists(process_id, metadata.get("processIdentity"))

    if exists is True or exists is None:
      raise ImportLockBusy("Another importer owns the process lock.")

    if datetime.now(timezone.utc) - started_at < self.stale_after:
      raise ImportLockBusy(
          "A stale import lock has not reached its cleanup timeout.")

  def acquire(self) -> "ImportProcessLock":
    self.path.parent.mkdir(parents=True, exist_ok=True)
    self.file = self.path.open("a+b")
    self.file.seek(0, os.SEEK_END)

    if self.file.tell() == 0:
      self.file.write(b" ")
      self.file.flush()

    try:
      self.file.seek(1)
      prior_metadata = self._read_metadata()
      self._check_stale_metadata(prior_metadata)
    except ImportLockBusy:
      self.file.close()
      self.file = None
      raise

    try:
      if os.name == "nt":
        import msvcrt

        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
      else:
        import fcntl

        fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError) as error:
      self.file.close()
      self.file = None
      raise ImportLockBusy(
          "Another importer owns the process lock.") from error

    self.acquired = True
    created_at = datetime.now(timezone.utc)
    metadata = {
        "processId": os.getpid(),
        "processIdentity": _process_identity(os.getpid()),
        "createdAt": _iso_utc(created_at)
    }

    self.file.seek(0)
    self.file.truncate()
    self.file.write(b" ")
    self.file.write(
        json.dumps(metadata, separators=(",", ":")).encode("utf-8"))
    self.file.flush()
    os.fsync(self.file.fileno())

    return self

  def release(self) -> None:
    if not self.acquired or self.file is None:
      return

    try:
      try:
        self.file.seek(0)
        self.file.truncate()
        self.file.flush()
        os.fsync(self.file.fileno())
      except OSError:
        pass

      if os.name == "nt":
        import msvcrt

        self.file.seek(0)
        msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
      else:
        import fcntl

        fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
    finally:
      self.file.close()
      self.file = None
      self.acquired = False

  def __enter__(self) -> "ImportProcessLock":
    return self.acquire()

  def __exit__(self, exception_type, exception, traceback) -> None:
    self.release()
