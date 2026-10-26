import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.config import Settings

TEAM_LOGO_DIRECTORY = "team-logos"
TEAM_LOGO_API_PREFIX = "/api/assets/team-logos"
MAX_TEAM_LOGO_BYTES = 2 * 1024 * 1024
MIME_EXTENSIONS = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg"
}
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MANIFEST_NAME = "manifest.json"
MANUAL_SOURCE_KIND = "manual-local"


def valid_team_slug(value: str) -> bool:
  return bool(SLUG_PATTERN.fullmatch(value)) and ".." not in value


def image_mime_from_bytes(data: bytes) -> str | None:
  if (len(data) >= 45 and data.startswith(b"\x89PNG\r\n\x1a\n")
      and data[12:16] == b"IHDR" and data[-8:-4] == b"IEND"
      and data[-4:] == b"\xaeB`\x82"):
    return "image/png"

  if (len(data) >= 30 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
      and int.from_bytes(data[4:8], "little") == len(data) - 8
      and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}):
    return "image/webp"

  if len(data) >= 8 and data.startswith(b"\xff\xd8\xff") and data.endswith(
      b"\xff\xd9"):
    return "image/jpeg"

  return None


def validate_image_bytes(data: bytes,
                         claimed_mime: str) -> tuple[str | None, str | None]:
  if not data:
    return None, "empty-image"

  if len(data) > MAX_TEAM_LOGO_BYTES:
    return None, "image-too-large"

  if claimed_mime not in MIME_EXTENSIONS:
    return None, "unsupported-content-type"

  detected = image_mime_from_bytes(data)

  if detected != claimed_mime:
    return None, "content-type-mismatch"

  return detected, None


def _asset_root(settings: Settings) -> Path:
  return (settings.asset_directory / TEAM_LOGO_DIRECTORY).resolve()


def manifest_path(settings: Settings) -> Path:
  return _asset_root(settings) / MANIFEST_NAME


def read_manifest(settings: Settings) -> dict:
  try:
    payload = json.loads(manifest_path(settings).read_text(encoding="utf-8"))
  except (OSError, UnicodeError, json.JSONDecodeError):
    return {"schemaVersion": 1, "teams": {}}

  if (not isinstance(payload, dict)
      or payload.get("schemaVersion") not in (1, 2)
      or not isinstance(payload.get("teams"), dict)):
    return {"schemaVersion": 1, "teams": {}}

  return payload


def is_manually_managed_team_logo(settings: Settings, slug: str) -> bool:
  entry = read_manifest(settings)["teams"].get(slug)
  source = entry.get("source") if isinstance(entry, dict) else None

  return isinstance(source, dict) and source.get("kind") == MANUAL_SOURCE_KIND


def _atomic_write_bytes(path: Path, data: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary_path = None

  try:
    with tempfile.NamedTemporaryFile(dir=path.parent,
                                     prefix=f".{path.name}.",
                                     suffix=".tmp",
                                     delete=False) as file:
      temporary_path = Path(file.name)
      file.write(data)
      file.flush()
      os.fsync(file.fileno())

    os.replace(temporary_path, path)
  finally:
    if temporary_path is not None and temporary_path.exists():
      temporary_path.unlink()


def _atomic_write_json(path: Path, payload: dict) -> None:
  encoded = (json.dumps(
      payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) +
             "\n").encode("utf-8")

  _atomic_write_bytes(path, encoded)


def register_manual_team_logo_manifest(
    settings: Settings,
    canonical_slugs: list[str],
    verified_hashes: dict[str, str],
    retrieved_at: str | None = None) -> dict:
  slugs = sorted(canonical_slugs)

  if len(slugs) != 16 or len(set(slugs)) != 16 or any(not valid_team_slug(slug)
                                                      for slug in slugs):
    raise ValueError("Exactly 16 unique canonical Team slugs are required.")

  if set(verified_hashes) != set(slugs):
    raise ValueError(
        "Full-decode verification must cover exactly the canonical Team slugs."
    )

  root = _asset_root(settings)
  expected_names = {f"{slug}.png" for slug in slugs}
  asset_files = [path for path in root.iterdir() if path.name != MANIFEST_NAME]

  if any(
      path.is_symlink() or not path.is_file() or path.resolve().parent != root
      for path in asset_files):
    raise ValueError(
        "Manual Team logo directory contains a non-regular or out-of-directory file."
    )

  actual_names = {path.name for path in asset_files}

  if actual_names != expected_names:
    missing = sorted(expected_names - actual_names)
    unknown = sorted(actual_names - expected_names)
    raise ValueError(
        f"Manual Team logo files do not match canonical slugs (missing={missing}, unknown={unknown})."
    )

  registered_at = retrieved_at or utc_now_string()
  teams = {}

  for slug in slugs:
    path = root / f"{slug}.png"
    data = path.read_bytes()
    mime_type, error = validate_image_bytes(data, "image/png")
    width = int.from_bytes(data[16:20], "big") if len(data) >= 24 else 0
    height = int.from_bytes(data[20:24], "big") if len(data) >= 24 else 0

    if error or mime_type != "image/png" or width <= 0 or height <= 0:
      raise ValueError(f"Manual Team logo failed PNG validation for {slug}.")

    digest = hashlib.sha256(data).hexdigest()

    if verified_hashes[slug].casefold() != digest:
      raise ValueError(
          f"Manual Team logo changed after image decoding for {slug}.")

    teams[slug] = {
        "storageIdentity": f"{TEAM_LOGO_DIRECTORY}/{slug}.png",
        "contentSha256": digest,
        "mimeType": "image/png",
        "sizeBytes": len(data),
        "retrievedAt": registered_at,
        "source": {
            "kind": MANUAL_SOURCE_KIND,
            "managedBy": "human"
        }
    }

  payload = {"schemaVersion": 2, "teams": teams}
  _atomic_write_json(manifest_path(settings), payload)

  return payload


def manually_managed_team_logos(settings: Settings) -> set[str]:
  teams = read_manifest(settings)["teams"]

  return {
      slug
      for slug, entry in teams.items() if isinstance(slug, str)
      and isinstance(entry, dict) and isinstance(entry.get("source"), dict)
      and entry["source"].get("kind") == MANUAL_SOURCE_KIND
  }


def store_team_logo(settings: Settings, slug: str, data: bytes, mime_type: str,
                    retrieved_at: str) -> dict:
  if not valid_team_slug(slug):
    raise ValueError("Invalid canonical team slug.")

  detected_mime, error = validate_image_bytes(data, mime_type)

  if error or detected_mime is None:
    raise ValueError("Team logo failed image validation.")

  digest = hashlib.sha256(data).hexdigest()
  extension = MIME_EXTENSIONS[detected_mime]
  root = _asset_root(settings)
  target = (root / f"{slug}{extension}").resolve()

  if target.parent != root:
    raise ValueError("Invalid team logo storage identity.")

  old_manifest = read_manifest(settings)
  old_entry = old_manifest["teams"].get(slug)

  if is_manually_managed_team_logo(settings, slug):
    raise ValueError(
        "Manually maintained Team logos cannot be replaced by Liquipedia assets."
    )

  same_file = False

  if isinstance(old_entry, dict) and old_entry.get("contentSha256") == digest:
    old_identity = old_entry.get("storageIdentity")
    same_file = old_identity == f"{TEAM_LOGO_DIRECTORY}/{slug}{extension}" and target.is_file(
    )

    if same_file:
      try:
        same_file = hashlib.sha256(target.read_bytes()).hexdigest() == digest
      except OSError:
        same_file = False

  if not same_file:
    _atomic_write_bytes(target, data)

  entry = {
      "storageIdentity": f"{TEAM_LOGO_DIRECTORY}/{slug}{extension}",
      "contentSha256": digest,
      "mimeType": detected_mime,
      "sizeBytes": len(data),
      "retrievedAt": retrieved_at
  }

  if same_file and isinstance(old_entry,
                              dict) and old_entry.get("retrievedAt"):
    entry["retrievedAt"] = old_entry["retrievedAt"]

  old_manifest["teams"][slug] = entry
  _atomic_write_json(manifest_path(settings), old_manifest)

  return {**entry, "fileReused": same_file, "filePath": target}


def resolve_team_logo(settings: Settings,
                      slug: str) -> tuple[Path, dict] | None:
  if not valid_team_slug(slug):
    return None

  entry = read_manifest(settings)["teams"].get(slug)

  if not isinstance(entry, dict):
    return None

  mime_type = entry.get("mimeType")

  if mime_type not in MIME_EXTENSIONS:
    return None

  expected_identity = f"{TEAM_LOGO_DIRECTORY}/{slug}{MIME_EXTENSIONS[mime_type]}"

  if entry.get("storageIdentity") != expected_identity:
    return None

  root = _asset_root(settings)
  path = (root / f"{slug}{MIME_EXTENSIONS[mime_type]}").resolve()

  if path.parent != root or not path.is_file():
    return None

  return path, entry


def public_team_logo_url(settings: Settings, slug: object) -> str | None:
  if not isinstance(slug, str):
    return None

  resolved = resolve_team_logo(settings, slug)

  if resolved is None:
    return None

  entry = resolved[1]
  digest = entry.get("contentSha256")

  if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
    return None

  return f"{TEAM_LOGO_API_PREFIX}/{slug}?v={digest[:12]}"


def verified_team_logo(settings: Settings,
                       slug: str) -> tuple[bytes, str, str] | None:
  resolved = resolve_team_logo(settings, slug)

  if resolved is None:
    return None

  path, entry = resolved

  try:
    data = path.read_bytes()
  except OSError:
    return None

  mime_type, validation_error = validate_image_bytes(
      data, str(entry.get("mimeType", "")))
  digest = hashlib.sha256(data).hexdigest()

  if (validation_error or mime_type is None
      or mime_type != entry.get("mimeType")
      or digest != entry.get("contentSha256")
      or len(data) != entry.get("sizeBytes")):
    return None

  return data, mime_type, digest


def utc_now_string() -> str:
  return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
