from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

BACKEND_DIRECTORY = Path(__file__).resolve().parent
API_HOST = "127.0.0.1"
API_PORT = 8591
HEALTH_URL = f"http://{API_HOST}:{API_PORT}/api/health"
SERVICE_NAME = "VCT Analytics API"
RUN_DIRECTORY = BACKEND_DIRECTORY / "var" / "run"
API_LOCK_PATH = RUN_DIRECTORY / "api-process.lock"
LOG_DIRECTORY = BACKEND_DIRECTORY / "var" / "logs"
STOP_FLAG = RUN_DIRECTORY / "api.stop"
STDOUT_LOG = LOG_DIRECTORY / "api-stdout.log"
STDERR_LOG = LOG_DIRECTORY / "api-stderr.log"
API_STARTUP_GRACE_SECONDS = 15.0
API_STARTUP_TIMEOUT_SECONDS = 25.0
API_STOP_TIMEOUT_SECONDS = 20.0


@dataclass(frozen=True)
class HealthResult:
  reachable: bool
  identity_matches: bool
  healthy: bool


@dataclass(frozen=True)
class ApiState:
  name: str
  health: HealthResult
  port_open: bool | None
  lock_held: bool | None
  bind_available: bool | None
  lock_owner: "ProcessIdentity | None"
  lock_owner_candidates: tuple["ProcessIdentity", ...]
  owner_inspection_available: bool
  stop_requested: bool


@dataclass(frozen=True)
class ProcessIdentity:
  process_id: int
  created_at: str | None
  executable_path: str | None
  working_directory: str | None
  command_matches: bool | None
  lock_start_matches: bool | None
  identity_verified: bool
  verified: bool
  verification_source: str | None


def _filetime_ticks(value) -> int:
  return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _filetime_iso(value) -> str:
  timestamp = _filetime_ticks(value) / 10_000_000 - 11_644_473_600

  return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
      "+00:00", "Z")


def _restart_manager_lock_owners() -> list[tuple[int, int]] | None:
  if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
    return None

  from ctypes import wintypes

  class RmUniqueProcess(ctypes.Structure):
    _fields_ = [("dwProcessId", wintypes.DWORD),
                ("ProcessStartTime", wintypes.FILETIME)]

  class RmProcessInfo(ctypes.Structure):
    _fields_ = [("Process", RmUniqueProcess),
                ("strAppName", wintypes.WCHAR * 256),
                ("strServiceShortName", wintypes.WCHAR * 64),
                ("ApplicationType", ctypes.c_int),
                ("AppStatus", wintypes.ULONG), ("TSSessionId", wintypes.DWORD),
                ("bRestartable", wintypes.BOOL)]

  try:
    restart_manager = ctypes.WinDLL("Rstrtmgr.dll", use_last_error=True)
    start_session = restart_manager.RmStartSession
    start_session.argtypes = [
        ctypes.POINTER(wintypes.DWORD), wintypes.DWORD, wintypes.LPWSTR
    ]
    start_session.restype = wintypes.DWORD
    register_resources = restart_manager.RmRegisterResources
    register_resources.argtypes = [
        wintypes.DWORD, wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR), wintypes.UINT, ctypes.c_void_p,
        wintypes.UINT, ctypes.c_void_p
    ]
    register_resources.restype = wintypes.DWORD
    get_list = restart_manager.RmGetList
    get_list.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(RmProcessInfo),
        ctypes.POINTER(wintypes.DWORD)
    ]
    get_list.restype = wintypes.DWORD
    end_session = restart_manager.RmEndSession
    end_session.argtypes = [wintypes.DWORD]
    end_session.restype = wintypes.DWORD
  except (AttributeError, OSError):
    return None

  session = wintypes.DWORD()
  session_key = ctypes.create_unicode_buffer(33)

  if start_session(ctypes.byref(session), 0, session_key) != 0:
    return None

  try:
    paths = (wintypes.LPCWSTR * 1)(str(API_LOCK_PATH))

    if register_resources(session, 1, paths, 0, None, 0, None) != 0:
      return None

    needed = wintypes.UINT()
    count = wintypes.UINT()
    reboot_reasons = wintypes.DWORD()
    result = get_list(session, ctypes.byref(needed), ctypes.byref(count), None,
                      ctypes.byref(reboot_reasons))

    if result == 0:
      return []

    if result != 234 or needed.value <= 0:
      return None

    processes = (RmProcessInfo * needed.value)()
    count.value = needed.value
    result = get_list(session, ctypes.byref(needed), ctypes.byref(count),
                      processes, ctypes.byref(reboot_reasons))

    if result != 0:
      return None

    return [(int(processes[index].Process.dwProcessId),
             _filetime_ticks(processes[index].Process.ProcessStartTime))
            for index in range(count.value)]
  except (OSError, ValueError):
    return None
  finally:
    end_session(session)


def _read_remote_bytes(kernel32, process, address: int,
                       length: int) -> bytes | None:
  if address <= 0 or length <= 0 or length > 65_536:
    return None

  buffer = ctypes.create_string_buffer(length)
  bytes_read = ctypes.c_size_t()
  succeeded = kernel32.ReadProcessMemory(process, ctypes.c_void_p(address),
                                         buffer, length,
                                         ctypes.byref(bytes_read))

  if not succeeded or bytes_read.value != length:
    return None

  return buffer.raw[:length]


def _remote_unicode_string(kernel32, process, address: int,
                           pointer_size: int) -> str | None:
  header = _read_remote_bytes(kernel32, process, address, pointer_size * 2)

  if header is None:
    return None

  length = struct.unpack_from("<H", header)[0]
  string_address = int.from_bytes(header[pointer_size:pointer_size * 2],
                                  "little")

  if length == 0 or length % 2:
    return None

  contents = _read_remote_bytes(kernel32, process, string_address, length)

  if contents is None:
    return None

  try:
    return contents.decode("utf-16-le")
  except UnicodeDecodeError:
    return None


def _process_identity_details(
    process_id: int, lock_started_ticks: int | None) -> ProcessIdentity:
  if os.name != "nt":
    return ProcessIdentity(process_id, None, None, None, None, None, False,
                           False, None)

  from ctypes import wintypes

  class ProcessBasicInformation(ctypes.Structure):
    _fields_ = [("Reserved1", ctypes.c_void_p),
                ("PebBaseAddress", ctypes.c_void_p),
                ("Reserved2", ctypes.c_void_p * 2),
                ("UniqueProcessId", ctypes.c_void_p),
                ("InheritedFromUniqueProcessId", ctypes.c_void_p)]

  kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
  kernel32.OpenProcess.argtypes = [
      wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
  ]
  kernel32.OpenProcess.restype = wintypes.HANDLE
  kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
  kernel32.CloseHandle.restype = wintypes.BOOL
  kernel32.GetProcessTimes.argtypes = [
      wintypes.HANDLE,
      ctypes.POINTER(wintypes.FILETIME),
      ctypes.POINTER(wintypes.FILETIME),
      ctypes.POINTER(wintypes.FILETIME),
      ctypes.POINTER(wintypes.FILETIME)
  ]
  kernel32.GetProcessTimes.restype = wintypes.BOOL
  kernel32.QueryFullProcessImageNameW.argtypes = [
      wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
      ctypes.POINTER(wintypes.DWORD)
  ]
  kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
  kernel32.ReadProcessMemory.argtypes = [
      wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
      ctypes.POINTER(ctypes.c_size_t)
  ]
  kernel32.ReadProcessMemory.restype = wintypes.BOOL
  process = kernel32.OpenProcess(0x0410, False, process_id)

  if not process:
    return ProcessIdentity(process_id, None, None, None, None, None, False,
                           False, None)

  executable_path = None
  working_directory = None
  command_matches = None
  created_at = None
  lock_start_matches = None

  try:
    creation_time = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()

    if kernel32.GetProcessTimes(process, ctypes.byref(creation_time),
                                ctypes.byref(exit_time),
                                ctypes.byref(kernel_time),
                                ctypes.byref(user_time)):
      creation_ticks = _filetime_ticks(creation_time)
      created_at = _filetime_iso(creation_time)

      if lock_started_ticks is not None:
        lock_start_matches = abs(creation_ticks -
                                 lock_started_ticks) <= 10_000_000

    image_buffer = ctypes.create_unicode_buffer(32_768)
    image_length = wintypes.DWORD(len(image_buffer))

    if kernel32.QueryFullProcessImageNameW(process, 0, image_buffer,
                                           ctypes.byref(image_length)):
      executable_path = image_buffer.value

    ntdll = ctypes.WinDLL("ntdll.dll")
    query_process = ntdll.NtQueryInformationProcess
    query_process.argtypes = [
        wintypes.HANDLE, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG,
        ctypes.POINTER(wintypes.ULONG)
    ]
    query_process.restype = wintypes.LONG
    basic_information = ProcessBasicInformation()
    returned_length = wintypes.ULONG()
    status = query_process(process, 0, ctypes.byref(basic_information),
                           ctypes.sizeof(basic_information),
                           ctypes.byref(returned_length))

    if status >= 0 and basic_information.PebBaseAddress:
      pointer_size = ctypes.sizeof(ctypes.c_void_p)
      peb_parameters_offset = 0x20 if pointer_size == 8 else 0x10
      parameters_bytes = _read_remote_bytes(
          kernel32, process,
          int(basic_information.PebBaseAddress) + peb_parameters_offset,
          pointer_size)

      if parameters_bytes is not None:
        parameters = int.from_bytes(parameters_bytes, "little")
        command_offset = 0x70 if pointer_size == 8 else 0x40
        directory_offset = 0x38 if pointer_size == 8 else 0x24
        command_line = _remote_unicode_string(kernel32, process,
                                              parameters + command_offset,
                                              pointer_size)
        working_directory = _remote_unicode_string(
            kernel32, process, parameters + directory_offset, pointer_size)

        if command_line is not None:
          normalized_command = command_line.casefold().replace("\\", "/")
          command_matches = bool(
              re.search(r"(?:^|\s)-m\s+app\.cli\.serve(?:\s|$)",
                        normalized_command)
              or ("manage.py" in normalized_command and re.search(
                  r"manage\.py[\"']?\s+run(?:\s|$)", normalized_command)))
  except (OSError, AttributeError, ValueError):
    pass
  finally:
    kernel32.CloseHandle(process)

  expected_executable = Path(sys.prefix) / "python.exe"
  executable_matches = (executable_path is not None and os.path.normcase(
      os.path.abspath(executable_path)) == os.path.normcase(
          os.path.abspath(expected_executable)))
  working_directory_matches = (
      working_directory is not None
      and os.path.normcase(os.path.abspath(working_directory))
      == os.path.normcase(os.path.abspath(BACKEND_DIRECTORY)))
  identity_verified = bool(created_at is not None and executable_matches
                           and working_directory_matches
                           and command_matches is True)
  verified = identity_verified and lock_start_matches is True

  return ProcessIdentity(process_id, created_at, executable_path,
                         working_directory, command_matches,
                         lock_start_matches, identity_verified, verified,
                         "restart-manager" if verified else None)


def _system_process_ids() -> list[int] | None:
  if os.name != "nt" or not hasattr(ctypes, "WinDLL"):
    return None

  from ctypes import wintypes

  class ProcessEntry32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]

  try:
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot
    snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    snapshot.restype = wintypes.HANDLE
    first = kernel32.Process32FirstW
    first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    first.restype = wintypes.BOOL
    next_process = kernel32.Process32NextW
    next_process.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    next_process.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
  except (AttributeError, OSError):
    return None

  process_snapshot = snapshot(0x00000002, 0)

  if not process_snapshot or process_snapshot == ctypes.c_void_p(-1).value:
    return None

  process_ids = []

  try:
    entry = ProcessEntry32W()
    entry.dwSize = ctypes.sizeof(entry)

    if not first(process_snapshot, ctypes.byref(entry)):
      return None

    while True:
      if entry.szExeFile.casefold() in {"python.exe", "pythonw.exe"}:
        process_ids.append(int(entry.th32ProcessID))

      entry.dwSize = ctypes.sizeof(entry)

      if not next_process(process_snapshot, ctypes.byref(entry)):
        break
  finally:
    kernel32.CloseHandle(process_snapshot)

  return process_ids


def _api_lock_processes() -> tuple[bool, tuple[ProcessIdentity, ...]]:
  owners = _restart_manager_lock_owners()

  if owners is not None:
    return True, tuple(
        _process_identity_details(process_id, started)
        for process_id, started in owners)

  process_ids = _system_process_ids()

  if process_ids is None:
    return False, ()

  candidates = tuple(
      process for process in (_process_identity_details(process_id, None)
                              for process_id in process_ids)
      if process.identity_verified)

  if len(candidates) == 1:
    candidates = (replace(
        candidates[0],
        verified=True,
        verification_source="unique backend service process"), )

  return True, candidates


def _verified_lock_owner(
    processes: tuple[ProcessIdentity, ...]) -> ProcessIdentity | None:
  verified = [process for process in processes if process.verified]

  return verified[0] if len(verified) == 1 else None


def _health_result() -> HealthResult:
  request = urllib.request.Request(HEALTH_URL,
                                   headers={"Accept": "application/json"})
  opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

  try:
    with opener.open(request, timeout=1.0) as response:
      payload = json.loads(response.read().decode("utf-8"))
      data = payload.get("data", {}) if isinstance(payload, dict) else {}
      identity = isinstance(data, dict) and data.get("service") == SERVICE_NAME

      return HealthResult(True, identity, identity
                          and data.get("status") == "ok")
  except urllib.error.HTTPError as error:
    try:
      payload = json.loads(error.read().decode("utf-8"))
      data = payload.get("data", {}) if isinstance(payload, dict) else {}
      identity = isinstance(data, dict) and data.get("service") == SERVICE_NAME

      return HealthResult(True, identity, False)
    except (OSError, UnicodeError, json.JSONDecodeError):
      return HealthResult(True, False, False)
  except (OSError, TimeoutError, UnicodeError, json.JSONDecodeError):
    return HealthResult(False, False, False)


def _port_open() -> bool | None:
  probe = None

  try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    probe.connect((API_HOST, API_PORT))

    return True
  except OSError as error:
    refusal_codes = {errno.ECONNREFUSED, 10061}

    if error.errno in refusal_codes or getattr(error, "winerror",
                                               None) in refusal_codes:
      return False

    return None
  finally:
    if probe is not None:
      probe.close()


def _exclusive_bind_available() -> bool | None:
  probe = None

  try:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive_option = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)

    if exclusive_option is not None:
      probe.setsockopt(socket.SOL_SOCKET, exclusive_option, 1)
    elif os.name == "nt":
      return None
    else:
      probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)

    probe.bind((API_HOST, API_PORT))

    return True
  except OSError as error:
    address_in_use_codes = {errno.EADDRINUSE, 10048}

    if (error.errno in address_in_use_codes
        or getattr(error, "winerror", None) in address_in_use_codes):
      return False

    return None
  finally:
    if probe is not None:
      probe.close()


def _lock_held() -> bool | None:
  backend_text = str(BACKEND_DIRECTORY)

  if backend_text not in sys.path:
    sys.path.insert(0, backend_text)

  try:
    from app.cli.serve import is_api_lock_held

    return is_api_lock_held()
  except OSError:
    return None


def inspect_api() -> ApiState:
  health = _health_result()
  port_open = _port_open()
  lock_held = _lock_held()
  bind_available = _exclusive_bind_available()

  if bind_available is True:
    port_open = False
  elif bind_available is False:
    port_open = True

  stop_requested = STOP_FLAG.is_file()
  owner_candidates: tuple[ProcessIdentity, ...] = ()
  owner_inspection_available = True

  if lock_held is True:
    owner_inspection_available, owner_candidates = _api_lock_processes()

  lock_owner = _verified_lock_owner(owner_candidates)

  if health.identity_matches:
    state = "running" if health.healthy and lock_held is True else "unhealthy"
  elif stop_requested and lock_held is True:
    state = "stopping"
  elif (bind_available is False and lock_held is True
        and lock_owner is not None and not health.reachable):
    created_at = lock_owner.created_at

    if created_at is None:
      state = "unknown"
    else:
      started_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
      elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
      state = "starting" if elapsed <= API_STARTUP_GRACE_SECONDS else "unhealthy"
  elif bind_available is False:
    state = "port-occupied"
  elif bind_available is None or lock_held is None:
    state = "unknown"
  elif health.reachable:
    state = "unhealthy"
  elif lock_held is False and bind_available is True:
    state = "stopped"
  elif lock_held is True and bind_available is True:
    timed_processes = [
        process for process in owner_candidates
        if process.created_at is not None
        and process.lock_start_matches is not False
    ]

    created_at = timed_processes[0].created_at if len(
        timed_processes) == 1 else None

    if created_at is None:
      state = "unknown"
    else:
      started_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
      elapsed = (datetime.now(timezone.utc) - started_at).total_seconds()
      state = "starting" if elapsed <= API_STARTUP_GRACE_SECONDS else "unhealthy"
  else:
    state = "unknown"

  return ApiState(state, health, port_open, lock_held, bind_available,
                  lock_owner, owner_candidates, owner_inspection_available,
                  stop_requested)


def _print_status(state: ApiState) -> None:
  messages = {
      "running": "API 正常執行中：http://127.0.0.1:8591",
      "stopped": "API 已停止。",
      "starting": "API 正在啟動中。",
      "stopping": "API 正在協作式停止中。",
      "unhealthy": "API 持有執行鎖但未正常提供服務；可嘗試協作式停止。",
      "port-occupied": "連接埠 8591 已由其他服務使用，未啟動 API。",
      "unknown": "API 狀態無法安全確認；未採取操作。"
  }

  print(messages[state.name])

  if state.name in {"starting", "stopping", "unhealthy", "unknown"}:
    if not state.owner_inspection_available:
      print("持鎖程序資訊：系統未能安全讀取。")
    elif state.lock_owner is not None:
      owner = state.lock_owner
      print(
          "持鎖 API 程序已核對："
          f"PID={owner.process_id}, executable={owner.executable_path}, "
          f"startedAt={owner.created_at}, backendDirectory=matched, command=matched。"
      )
    elif state.lock_held is True:
      print("持鎖程序未能唯一核對；不會向未確認的程序送出停止要求。")


def _validate_environment() -> None:
  from app.cli.serve import validate_environment

  validate_environment()


def _remove_stop_flag_if_owned(token: str) -> None:
  try:
    if STOP_FLAG.read_text(encoding="utf-8").strip() == token:
      STOP_FLAG.unlink(missing_ok=True)
  except OSError:
    pass


def _write_stop_flag() -> str:
  RUN_DIRECTORY.mkdir(parents=True, exist_ok=True)
  token = uuid.uuid4().hex
  temporary_path = RUN_DIRECTORY / f"api.stop.{token}.tmp"

  try:
    with temporary_path.open("x", encoding="utf-8", newline="\n") as stop_file:
      stop_file.write(token)
      stop_file.flush()
      os.fsync(stop_file.fileno())

    os.replace(temporary_path, STOP_FLAG)
  finally:
    temporary_path.unlink(missing_ok=True)

  return token


def _print_lock_processes(state: ApiState) -> None:
  for process in state.lock_owner_candidates:
    print(
        "程序診斷："
        f"PID={process.process_id}, createdAt={process.created_at}, "
        f"executable={process.executable_path}, workingDirectory={process.working_directory}, "
        f"commandMatches={process.command_matches}, lockStartMatches={process.lock_start_matches}, "
        f"verified={process.verified}")


def _request_stop(timeout_seconds: float = API_STOP_TIMEOUT_SECONDS) -> bool:
  state = inspect_api()

  if state.name == "stopped":
    if STOP_FLAG.exists():
      try:
        STOP_FLAG.unlink()
      except OSError:
        print("API 已停止，但無法清除殘留的 stop flag。", file=sys.stderr)

        return False

    print("API 已停止。")

    return True

  if state.name == "stopping" and state.stop_requested:
    token = None
  elif state.name == "running":
    if state.lock_held is not True or not state.health.identity_matches:
      _print_status(state)

      return False

    token = _write_stop_flag()
  elif (state.name in {"starting", "unhealthy"}
        and state.bind_available is True and state.lock_held is True
        and state.lock_owner is not None):
    token = _write_stop_flag()
  else:
    _print_status(state)

    return False

  deadline = time.monotonic() + timeout_seconds

  try:
    while time.monotonic() < deadline:
      state = inspect_api()

      if state.name == "stopped":
        if token is not None:
          _remove_stop_flag_if_owned(token)

        print("API 已正常停止。")

        return True

      if state.name == "port-occupied":
        print("停止期間連接埠被其他服務占用；未採取其他停止方式。", file=sys.stderr)

        return False

      time.sleep(0.25)
  finally:
    if token is not None:
      _remove_stop_flag_if_owned(token)

  print("API 未在期限內釋放 OS lock；未強制停止，也未刪除 lock。", file=sys.stderr)
  _print_lock_processes(state)

  return False


def _start() -> int:
  state = inspect_api()

  if state.name == "running":
    print("API 已在執行中，未啟動第二個程序。")

    return 0

  if state.name == "starting":
    print("API 正在啟動中，未啟動第二個程序。")

    return 0

  if state.name != "stopped":
    _print_status(state)

    return 1

  try:
    _validate_environment()
  except RuntimeError as error:
    print(str(error), file=sys.stderr)

    return 2

  RUN_DIRECTORY.mkdir(parents=True, exist_ok=True)
  LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

  try:
    STOP_FLAG.unlink(missing_ok=True)
  except OSError:
    print("無法清除舊的停止要求；未啟動 API。", file=sys.stderr)

    return 1

  creation_flags = 0

  if os.name == "nt":
    creation_flags = subprocess.DETACHED_PROCESS

  try:
    with STDOUT_LOG.open("a", encoding="utf-8",
                         buffering=1) as stdout_log, STDERR_LOG.open(
                             "a", encoding="utf-8", buffering=1) as stderr_log:
      child = subprocess.Popen([sys.executable, "-m", "app.cli.serve"],
                               cwd=BACKEND_DIRECTORY,
                               stdin=subprocess.DEVNULL,
                               stdout=stdout_log,
                               stderr=stderr_log,
                               close_fds=True,
                               shell=False,
                               creationflags=creation_flags)
  except OSError:
    print("無法啟動 API，請確認 Python 環境及記錄檔目錄。", file=sys.stderr)

    return 1

  deadline = time.monotonic() + API_STARTUP_TIMEOUT_SECONDS

  while time.monotonic() < deadline:
    state = inspect_api()

    if state.name == "running":
      print("API 已啟動：http://127.0.0.1:8591")

      return 0

    if state.name == "port-occupied":
      _print_status(state)

      return 1

    if child.poll() is not None:
      print("API 啟動失敗；請查看 var/logs/api-stderr.log。", file=sys.stderr)

      return 1

    time.sleep(0.25)

  print("API 啟動逾時；請查看 var/logs/api-stdout.log 與 var/logs/api-stderr.log。",
        file=sys.stderr)

  return 1


class _Tee:

  def __init__(self, terminal, log_file):
    self.terminal = terminal
    self.log_file = log_file

  def write(self, value: str) -> int:
    self.terminal.write(value)
    self.log_file.write(value)

    return len(value)

  def flush(self) -> None:
    self.terminal.flush()
    self.log_file.flush()

  def isatty(self) -> bool:
    return self.terminal.isatty()

  @property
  def encoding(self):
    return self.terminal.encoding


def _run_foreground() -> int:
  LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)

  with STDOUT_LOG.open("a", encoding="utf-8",
                       buffering=1) as stdout_log, STDERR_LOG.open(
                           "a", encoding="utf-8", buffering=1) as stderr_log:
    with redirect_stdout(_Tee(sys.stdout, stdout_log)), redirect_stderr(
        _Tee(sys.stderr, stderr_log)):
      from app.cli.serve import main as serve_main

      return serve_main()


def main() -> int:
  os.chdir(BACKEND_DIRECTORY)
  parser = argparse.ArgumentParser(
      description="Manage the local VCT Analytics API.")

  parser.add_argument("command",
                      choices=("start", "stop", "restart", "status", "run"))
  arguments = parser.parse_args()

  if arguments.command == "start":
    return _start()

  if arguments.command == "stop":
    return 0 if _request_stop() else 1

  if arguments.command == "restart":
    return 0 if _request_stop() and _start() == 0 else 1

  if arguments.command == "status":
    state = inspect_api()
    _print_status(state)

    return 0 if state.name in {"running", "stopped"} else 1

  return _run_foreground()


if __name__ == "__main__":
  raise SystemExit(main())
