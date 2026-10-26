from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import duckdb
import httpx

from app.config import Settings
from app.utils.files import sha256_file, write_json_atomic
from app.importers.models import ImportRunError

LOGGER = logging.getLogger(__name__)
SNAPSHOT_URL = "https://vct-reference.com/dataset/vct.duckdb"
ALLOWED_HOSTS = {"vct-reference.com"}
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 30.0
MAX_ATTEMPTS = 3
MAX_REDIRECTS = 4
BACKUP_PATTERN = re.compile(
    r"^vct-reference-(\d{8}T\d{12}Z)-([0-9a-f]{12})\.duckdb$")
ALLOWED_CONTENT_TYPES = {
    "application/octet-stream", "application/vnd.duckdb",
    "application/x-duckdb"
}


@dataclass(frozen=True)
class ValidatedSnapshot:
  status: str
  changed: bool
  replaced: bool
  content_sha256: str
  size_bytes: int
  source_timestamp: datetime | None
  etag: str | None
  last_modified: str | None
  schema_fingerprint: str
  table_counts: dict[str, int]
  backup_name: str | None = None
  head_status: int | None = None
  content_type: str | None = None
  content_length: int | None = None
  redirect_hosts: tuple[str, ...] = ()


def _utc_string(value: datetime | None) -> str | None:
  if value is None:
    return None

  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_http_time(value: str | None) -> datetime | None:
  if not value:
    return None

  try:
    parsed = parsedate_to_datetime(value)
  except (TypeError, ValueError, OverflowError):
    return None

  if parsed.tzinfo is None or parsed.utcoffset() is None:
    return None

  return parsed.astimezone(timezone.utc)


def read_source_metadata(database_path: Path) -> dict:
  metadata_path = database_path.parent / "source-metadata.json"

  try:
    value = json.loads(metadata_path.read_text(encoding="utf-8"))
  except (OSError, UnicodeError, json.JSONDecodeError):
    return {}

  return value if isinstance(value, dict) else {}


def read_source_timestamp(database_path: Path) -> datetime | None:
  value = read_source_metadata(database_path).get("sourceTimestamp")

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


def _ensure_official_url(url: str) -> None:
  parsed = urlparse(url)

  if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
      or parsed.port not in {None, 443} or parsed.username is not None
      or parsed.password is not None):
    raise ImportRunError(
        "VCT Reference snapshot source redirected outside the approved HTTPS host."
    )


def _request(client: httpx.Client,
             method: str,
             url: str,
             redirect_hosts: list[str] | None = None) -> httpx.Response:
  current_url = url

  for _ in range(MAX_REDIRECTS + 1):
    _ensure_official_url(current_url)
    request = client.build_request(method, current_url)
    response = client.send(request, stream=(method == "GET"))

    if response.status_code not in {301, 302, 303, 307, 308}:
      return response

    location = response.headers.get("location")
    response.close()

    if not location:
      raise ImportRunError(
          "VCT Reference snapshot returned an invalid redirect.")

    current_url = urljoin(current_url, location)

    if redirect_hosts is not None:
      target_host = urlparse(current_url).hostname

      if target_host:
        redirect_hosts.append(target_host)

  raise ImportRunError("VCT Reference snapshot exceeded the redirect limit.")


def _request_with_retry(
    client: httpx.Client,
    method: str,
    url: str,
    redirect_hosts: list[str] | None = None) -> httpx.Response:
  last_error: Exception | None = None

  for attempt in range(MAX_ATTEMPTS):
    try:
      response = _request(client, method, url, redirect_hosts)

      if response.status_code not in {429, 500, 502, 503, 504}:
        return response

      retry_after = response.headers.get("retry-after")
      response.close()
      delay = min(
          float(retry_after),
          5.0) if retry_after and retry_after.isdigit() else 0.5 * (attempt +
                                                                    1)

      if attempt + 1 < MAX_ATTEMPTS:
        time.sleep(delay)
    except (httpx.TimeoutException, httpx.NetworkError) as error:
      last_error = error

      if attempt + 1 < MAX_ATTEMPTS:
        time.sleep(0.5 * (attempt + 1))

  if last_error is not None:
    raise ImportRunError(
        "VCT Reference snapshot request failed after limited retries."
    ) from last_error

  raise ImportRunError(
      "VCT Reference snapshot request failed after limited retries.")


def inspect_snapshot_file(path: Path) -> dict:
  resolved = path.expanduser().resolve()
  schema_fingerprint, table_counts = _validate_database(resolved)

  return {
      "sha256": sha256_file(resolved),
      "sizeBytes": resolved.stat().st_size,
      "schemaFingerprint": schema_fingerprint,
      "tableCounts": table_counts
  }


def _schema_fingerprint(connection: duckdb.DuckDBPyConnection) -> str:
  rows = connection.execute("""
    SELECT table_name, column_name, data_type
    FROM information_schema.columns
    WHERE table_schema = 'main'
    ORDER BY table_name, ordinal_position
    """).fetchall()
  serialized = "\n".join("|".join(str(value) for value in row) for row in rows)

  return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _validate_database(path: Path) -> tuple[str, dict[str, int]]:
  connection = None

  try:
    connection = duckdb.connect(str(path), read_only=True)
    from app.importers.providers.vct_reference import _validate_schema as validate_schedule_schema
    from app.importers.providers.vct_stats import _validate_schema as validate_stats_schema

    validate_schedule_schema(connection)
    validate_stats_schema(connection)
    table_counts = {
        table:
        int(
            connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchall()[0]
            [0])
        for table in ("matches", "maps", "rounds", "player_map", "players",
                      "teams")
    }

    if not all(
        table_counts.get(name, 0) > 0
        for name in ("matches", "maps", "rounds", "player_map", "players",
                     "teams")):
      raise ImportRunError(
          "VCT Reference snapshot is missing required baseline data.")

    return _schema_fingerprint(connection), table_counts
  except (duckdb.Error, OSError) as error:
    raise ImportRunError(
        "VCT Reference snapshot failed read-only DuckDB validation."
    ) from error
  finally:
    if connection is not None:
      connection.close()


def _atomic_copy(source: Path, target: Path) -> None:
  temporary = None

  try:
    with tempfile.NamedTemporaryFile(mode="wb",
                                     dir=target.parent,
                                     prefix=f".{target.name}.",
                                     suffix=".tmp",
                                     delete=False) as output:
      temporary = Path(output.name)

      with source.open("rb") as input_file:
        shutil.copyfileobj(input_file, output, 1024 * 1024)

      output.flush()
      os.fsync(output.fileno())

    os.replace(temporary, target)
  finally:
    if temporary is not None and temporary.exists():
      temporary.unlink()


def _make_backup(database_path: Path, backup_directory: Path, digest: str,
                 now: datetime) -> Path:
  backup_directory.mkdir(parents=True, exist_ok=True)
  name = f"vct-reference-{now.strftime('%Y%m%dT%H%M%S%fZ')}-{digest[:12]}.duckdb"
  backup_path = backup_directory / name
  _atomic_copy(database_path, backup_path)

  if (backup_path.stat().st_size != database_path.stat().st_size
      or sha256_file(backup_path) != digest):
    backup_path.unlink(missing_ok=True)
    raise ImportRunError(
        "VCT Reference backup verification failed; the current database was not replaced."
    )

  return backup_path


def _prune_backups(backup_directory: Path, keep: int = 3) -> None:
  candidates = []

  try:
    for path in backup_directory.iterdir():
      match = BACKUP_PATTERN.fullmatch(path.name)

      if match and path.is_file():
        candidates.append((match.group(1), path))
  except OSError:
    LOGGER.warning(
        "VCT Reference backup retention cleanup could not list its managed files."
    )

    return

  candidates.sort(reverse=True)

  for _, path in candidates[keep:]:
    try:
      path.unlink()
    except OSError:
      LOGGER.warning(
          "VCT Reference backup retention cleanup could not remove an expired managed backup."
      )


def _download(
    client: httpx.Client, temporary_path: Path, redirect_hosts: list[str]
) -> tuple[str, int, str | None, str | None, datetime | None, str, int | None]:
  last_error: Exception | None = None

  for attempt in range(MAX_ATTEMPTS):
    response = None

    try:
      response = _request_with_retry(client, "GET", SNAPSHOT_URL,
                                     redirect_hosts)

      if response.status_code != 200:
        raise ImportRunError(
            "VCT Reference snapshot download returned an unsuccessful status.")

      content_type = response.headers.get("content-type",
                                          "").split(";",
                                                    1)[0].strip().casefold()

      if content_type not in ALLOWED_CONTENT_TYPES:
        raise ImportRunError(
            "VCT Reference snapshot has an unsupported content type.")

      raw_length = response.headers.get("content-length")
      content_length = None

      if raw_length:
        if not raw_length.isdigit():
          raise ImportRunError(
              "VCT Reference snapshot returned an invalid content length.")

        content_length = int(raw_length)

        if content_length > MAX_SNAPSHOT_BYTES:
          raise ImportRunError(
              "VCT Reference snapshot exceeds the configured size limit.")

      digest = hashlib.sha256()
      size = 0
      prefix = bytearray()

      with temporary_path.open("wb") as output:
        for chunk in response.iter_bytes(1024 * 1024):
          if not chunk:
            continue

          size += len(chunk)

          if size > MAX_SNAPSHOT_BYTES:
            raise ImportRunError(
                "VCT Reference snapshot exceeds the configured size limit.")

          if len(prefix) < 512:
            prefix.extend(chunk[:512 - len(prefix)])

          digest.update(chunk)
          output.write(chunk)

        output.flush()
        os.fsync(output.fileno())

      if size == 0:
        raise ImportRunError("VCT Reference snapshot is empty.")

      normalized_prefix = bytes(prefix).lstrip(b"\xef\xbb\xbf \t\r\n").lower()

      if normalized_prefix.startswith(
          (b"<!doctype html", b"<html", b"{", b"[")):
        raise ImportRunError(
            "VCT Reference snapshot response is not a DuckDB file.")

      return (digest.hexdigest(), size, response.headers.get("etag"),
              response.headers.get("last-modified"),
              _parse_http_time(response.headers.get("last-modified")),
              content_type, content_length)
    except (httpx.TimeoutException, httpx.NetworkError) as error:
      last_error = error
      temporary_path.unlink(missing_ok=True)

      if attempt + 1 < MAX_ATTEMPTS:
        time.sleep(0.5 * (attempt + 1))
    finally:
      if response is not None:
        response.close()

  raise ImportRunError(
      "VCT Reference snapshot download failed after limited retries."
  ) from last_error


def _metadata_payload(*, settings: Settings, digest: str, size: int,
                      schema_fingerprint: str, table_counts: dict[str, int],
                      downloaded_at: datetime | None, etag: str | None,
                      last_modified: str | None,
                      source_timestamp: datetime | None,
                      provenance: str) -> dict:
  try:
    storage_identity = settings.reference_database_path.relative_to(
        settings.resolve_backend_path(Path("."))).as_posix()
  except ValueError:
    storage_identity = "configured-reference-database"

  return {
      "provider":
      "vct-reference",
      "sourceUrl":
      SNAPSHOT_URL,
      "storageIdentity":
      storage_identity,
      "version":
      etag,
      "publishedAt":
      None,
      "sourceTimestamp":
      _utc_string(source_timestamp),
      "downloadedAt":
      _utc_string(downloaded_at),
      "migratedAt":
      _utc_string(datetime.now(timezone.utc))
      if provenance == "migrated-existing-local-snapshot" else None,
      "etag":
      etag,
      "lastModified":
      last_modified,
      "contentSha256":
      digest,
      "sizeBytes":
      size,
      "schemaFingerprint":
      schema_fingerprint,
      "tableCounts":
      table_counts,
      "validationStatus":
      "passed",
      "provenance":
      provenance
  }


def refresh_snapshot(
    settings: Settings,
    *,
    dry_run: bool,
    force_download: bool = False,
    on_phase: Callable[[str], None] | None = None) -> ValidatedSnapshot:
  database_path = settings.reference_database_path
  metadata_path = settings.reference_metadata_path

  if not database_path.is_file():
    raise ImportRunError("VCT Reference local snapshot is unavailable.")

  database_path.parent.mkdir(parents=True, exist_ok=True)
  prior_metadata = read_source_metadata(database_path)
  prior_digest = sha256_file(database_path)
  prior_size = database_path.stat().st_size
  downloaded_at = datetime.now(timezone.utc)

  if on_phase is not None:
    on_phase("checking_stats_source")

  with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS,
                    follow_redirects=False) as client:
    redirect_hosts: list[str] = []
    head = _request_with_retry(client, "HEAD", SNAPSHOT_URL, redirect_hosts)

    try:
      if head.status_code not in {200, 204, 405, 501}:
        raise ImportRunError("VCT Reference snapshot metadata request failed.")

      remote_etag = head.headers.get("etag")
      remote_modified = head.headers.get("last-modified")
      remote_timestamp = _parse_http_time(remote_modified)
      raw_head_length = head.headers.get("content-length")
      head_content_length = None

      if raw_head_length:
        if not raw_head_length.isdigit():
          raise ImportRunError(
              "VCT Reference snapshot returned an invalid content length.")

        head_content_length = int(raw_head_length)

      head_content_type = head.headers.get("content-type", "").split(
          ";", 1)[0].strip().casefold() or None

      if head_content_length is not None and head_content_length > MAX_SNAPSHOT_BYTES:
        raise ImportRunError(
            "VCT Reference snapshot exceeds the configured size limit.")

      if (not force_download and remote_etag
          and remote_etag == prior_metadata.get("etag")
          and prior_metadata.get("contentSha256") == prior_digest
          and prior_metadata.get("validationStatus") == "passed"):
        current_timestamp = remote_timestamp or read_source_timestamp(
            database_path)

        if not dry_run:
          updated_metadata = dict(prior_metadata)
          updated_metadata.update({
              "version":
              remote_etag,
              "etag":
              remote_etag,
              "lastModified":
              remote_modified or prior_metadata.get("lastModified"),
              "sourceTimestamp":
              _utc_string(current_timestamp),
              "checkedAt":
              _utc_string(downloaded_at)
          })
          write_json_atomic(metadata_path,
                            updated_metadata,
                            allow_nan=False,
                            durable=True)

        if on_phase is not None:
          on_phase("stats_source_unchanged")

        table_counts = prior_metadata.get("tableCounts")
        table_counts = table_counts if isinstance(table_counts, dict) else {}

        return ValidatedSnapshot(status="unchanged",
                                 changed=False,
                                 replaced=False,
                                 content_sha256=prior_digest,
                                 size_bytes=prior_size,
                                 source_timestamp=current_timestamp,
                                 etag=remote_etag,
                                 last_modified=remote_modified,
                                 schema_fingerprint=str(
                                     prior_metadata.get("schemaFingerprint")
                                     or ""),
                                 table_counts=table_counts,
                                 head_status=head.status_code,
                                 content_type=head_content_type,
                                 content_length=head_content_length,
                                 redirect_hosts=tuple(redirect_hosts))
    finally:
      head.close()

    temporary = None

    try:
      if on_phase is not None:
        on_phase("downloading_stats_source")

      with tempfile.NamedTemporaryFile(mode="wb",
                                       dir=database_path.parent,
                                       prefix=f".{database_path.name}.",
                                       suffix=".download",
                                       delete=False) as output:
        temporary = Path(output.name)

      digest, size, get_etag, get_modified, get_timestamp, content_type, content_length = _download(
          client, temporary, redirect_hosts)
      downloaded_at = datetime.now(timezone.utc)

      if on_phase is not None:
        on_phase("validating_stats_source")

      if remote_etag and get_etag and remote_etag != get_etag:
        raise ImportRunError(
            "VCT Reference snapshot changed while it was being downloaded.")

      schema_fingerprint, table_counts = _validate_database(temporary)
      etag = get_etag or remote_etag
      last_modified = get_modified or remote_modified
      source_timestamp = get_timestamp or remote_timestamp

      if digest == prior_digest:
        if not dry_run:
          payload = _metadata_payload(
              settings=settings,
              digest=digest,
              size=size,
              schema_fingerprint=schema_fingerprint,
              table_counts=table_counts,
              downloaded_at=downloaded_at,
              etag=etag,
              last_modified=last_modified,
              source_timestamp=source_timestamp,
              provenance="official-vct-reference-download")

          write_json_atomic(metadata_path,
                            payload,
                            allow_nan=False,
                            durable=True)

        if on_phase is not None:
          on_phase("stats_source_unchanged")

        return ValidatedSnapshot(status="unchanged",
                                 changed=False,
                                 replaced=False,
                                 content_sha256=digest,
                                 size_bytes=size,
                                 source_timestamp=source_timestamp,
                                 etag=etag,
                                 last_modified=last_modified,
                                 schema_fingerprint=schema_fingerprint,
                                 table_counts=table_counts,
                                 head_status=head.status_code,
                                 content_type=content_type,
                                 content_length=content_length,
                                 redirect_hosts=tuple(redirect_hosts))

      if dry_run:
        if on_phase is not None:
          on_phase("stats_source_updated")

        return ValidatedSnapshot(status="update-available",
                                 changed=True,
                                 replaced=False,
                                 content_sha256=digest,
                                 size_bytes=size,
                                 source_timestamp=source_timestamp,
                                 etag=etag,
                                 last_modified=last_modified,
                                 schema_fingerprint=schema_fingerprint,
                                 table_counts=table_counts,
                                 head_status=head.status_code,
                                 content_type=content_type,
                                 content_length=content_length,
                                 redirect_hosts=tuple(redirect_hosts))

      backup_path = _make_backup(database_path,
                                 settings.reference_backup_directory,
                                 prior_digest, downloaded_at)
      prior_metadata_bytes = metadata_path.read_bytes(
      ) if metadata_path.exists() else None
      os.replace(temporary, database_path)
      temporary = None

      try:
        payload = _metadata_payload(
            settings=settings,
            digest=digest,
            size=size,
            schema_fingerprint=schema_fingerprint,
            table_counts=table_counts,
            downloaded_at=downloaded_at,
            etag=etag,
            last_modified=last_modified,
            source_timestamp=source_timestamp,
            provenance="official-vct-reference-download")

        write_json_atomic(metadata_path,
                          payload,
                          allow_nan=False,
                          durable=True)
      except Exception:
        rollback_temp = None

        try:
          with tempfile.NamedTemporaryFile(
              mode="wb",
              dir=database_path.parent,
              prefix=f".{database_path.name}.rollback.",
              suffix=".tmp",
              delete=False) as output:
            rollback_temp = Path(output.name)

          _atomic_copy(backup_path, rollback_temp)
          os.replace(rollback_temp, database_path)
          rollback_temp = None

          if prior_metadata_bytes is None:
            metadata_path.unlink(missing_ok=True)
          else:
            write_json_atomic(metadata_path,
                              json.loads(prior_metadata_bytes.decode("utf-8")),
                              allow_nan=False,
                              durable=True)
        finally:
          if rollback_temp is not None and rollback_temp.exists():
            rollback_temp.unlink()

        raise ImportRunError(
            "VCT Reference snapshot metadata could not be committed; prior database restored."
        ) from None

      _prune_backups(settings.reference_backup_directory)

      if on_phase is not None:
        on_phase("stats_source_updated")

      return ValidatedSnapshot(status="updated",
                               changed=True,
                               replaced=True,
                               content_sha256=digest,
                               size_bytes=size,
                               source_timestamp=source_timestamp,
                               etag=etag,
                               last_modified=last_modified,
                               schema_fingerprint=schema_fingerprint,
                               table_counts=table_counts,
                               backup_name=backup_path.name,
                               head_status=head.status_code,
                               content_type=content_type,
                               content_length=content_length,
                               redirect_hosts=tuple(redirect_hosts))
    finally:
      if temporary is not None and temporary.exists():
        temporary.unlink()
