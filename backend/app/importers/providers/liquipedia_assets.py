import hashlib
import json
import re
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from bs4 import BeautifulSoup

from app.config import Settings
from app.utils.files import sha256_file
from app.utils.dates import parse_utc_datetime

ALLOWED_IMAGE_HOSTS = {"liquipedia.net"}
PLACEHOLDER_NAMES = {"", "bye", "tba", "tbd", "unknown"}
TEAM_IDENTITY_ALIASES = {
    "nsredforce": "nongshim-redforce",
    "xlg": "xi-lai-gaming",
    "xlgesports": "xi-lai-gaming"
}


def normalize_identity(value: str) -> str:
  normalized = value.replace("_", " ").casefold()

  return re.sub(r"[^a-z0-9]", "", normalized)


def event_parse_cache_path(settings: Settings, page_title: str) -> Path:
  identity = {
      "apiUrl": settings.liquipedia_api_url,
      "pageTitle": page_title,
      "action": "parse",
      "prop": "text|sections",
      "formatVersion": "2"
  }
  key = hashlib.sha256(json.dumps(identity,
                                  sort_keys=True).encode("utf-8")).hexdigest()

  return settings.cache_directory / "liquipedia" / f"{key}.json"


def read_event_parse_cache(settings: Settings,
                           page_title: str) -> tuple[dict, dict]:
  cache_path = event_parse_cache_path(settings, page_title)
  envelope = json.loads(cache_path.read_text(encoding="utf-8"))
  fetched_at = parse_utc_datetime(envelope.get("fetchedAt"))
  response = envelope.get("response")
  parse_data = response.get("parse") if isinstance(response, dict) else None
  html = parse_data.get("text") if isinstance(parse_data, dict) else None

  if fetched_at is None or not isinstance(html, str):
    raise ValueError(
        "Liquipedia event cache is missing a valid timestamp or parsed page.")

  source = {
      "kind": "event-parse",
      "pageTitle": page_title,
      "path": str(cache_path.resolve()),
      "cacheFile": cache_path.name,
      "cacheSha256": sha256_file(cache_path),
      "fetchedAt": fetched_at.isoformat().replace("+00:00", "Z")
  }

  return {"html": html, "fetchedAt": fetched_at}, source


def validate_image_url(
    raw_url: str,
    base_url: str = "https://liquipedia.net") -> tuple[str | None, str | None]:
  try:
    absolute_url = urljoin(base_url, raw_url.strip())
    parsed = urlparse(absolute_url)
    port = parsed.port
  except (AttributeError, TypeError, ValueError):
    return None, "malformed-url"

  if parsed.scheme.casefold() != "https":
    return None, "scheme-not-https"

  if parsed.hostname is None or parsed.hostname.casefold(
  ) not in ALLOWED_IMAGE_HOSTS:
    return None, "host-not-allowed"

  if parsed.username is not None or parsed.password is not None or port not in {
      None, 443
  }:
    return None, "unsafe-authority"

  if not parsed.path or parsed.path == "/":
    return None, "missing-image-path"

  return absolute_url, None


def _source_image_name(raw_url: str) -> str:
  path = [unquote(part) for part in urlparse(raw_url).path.split("/") if part]

  if "thumb" in path:
    thumb_index = path.index("thumb")

    if len(path) > thumb_index + 3:
      return path[thumb_index + 3]

  return path[-1] if path else ""


def _page_url(href: str) -> str | None:
  try:
    parsed = urlparse(urljoin("https://liquipedia.net", href))
    port = parsed.port
  except (TypeError, ValueError):
    return None

  if (parsed.scheme.casefold() != "https" or parsed.hostname is None
      or parsed.hostname.casefold() != "liquipedia.net"
      or parsed.username is not None or parsed.password is not None
      or port not in {None, 443} or not parsed.path.startswith("/valorant/")):
    return None

  return parsed.geturl()


def extract_team_logo_candidates(
    html: str) -> tuple[dict[str, list[dict]], list[dict]]:
  soup = BeautifulSoup(html, "html.parser")
  candidates: dict[str, list[dict]] = {}
  rejected: list[dict] = []

  for opponent in soup.select(
      ".brkts-match-info-popup .match-info-header-opponent"):
    opponent_name = opponent.select_one("[data-team-name]")
    identity_name = str(opponent_name.get("data-team-name",
                                          "")).strip() if opponent_name else ""
    identity_links: dict[tuple[str, str], list] = {}

    for anchor in opponent.select("a[href]"):
      href = str(anchor.get("href", ""))
      page_url = _page_url(href)
      title = str(anchor.get("title", "")).strip()

      if page_url and title and "Match:" not in title and anchor.select_one(
          "img[src]") is not None:
        identity_links.setdefault((title, page_url), []).append(anchor)

    if len(identity_links) != 1:
      if identity_name and identity_name.casefold() not in PLACEHOLDER_NAMES:
        rejected.append({
            "sourceName": identity_name,
            "reason": "team-page-reference-ambiguous"
        })

      continue

    (page_title, source_page), anchors = next(iter(identity_links.items()))

    if page_title.casefold() in PLACEHOLDER_NAMES:
      continue

    entity_name = identity_name or page_title
    identity_key = normalize_identity(entity_name)

    if not identity_key:
      continue

    for image in [
        image for anchor in anchors for image in anchor.select("img[src]")
    ]:
      raw_url = str(image.get("src", "")).strip()

      if not raw_url:
        continue

      image_url, reason = validate_image_url(raw_url)
      image_name = _source_image_name(raw_url)

      if reason:
        rejected.append({
            "sourceName": entity_name,
            "sourcePage": source_page,
            "url": raw_url,
            "reason": reason
        })
        continue

      if re.search(r"(?:default|placeholder|no[_ -]?image|unknown)",
                   image_name, re.IGNORECASE):
        rejected.append({
            "sourceName": entity_name,
            "sourcePage": source_page,
            "url": image_url,
            "reason": "placeholder-image"
        })
        continue

      candidates.setdefault(identity_key, []).append({
          "liquipediaName": page_title,
          "sourcePage": source_page,
          "url": image_url,
          "imageName": image_name
      })

  return candidates, rejected


def select_team_logo(candidates: list[dict]) -> tuple[dict | None, str | None]:
  unique = {}

  for candidate in candidates:
    unique[(candidate["url"], candidate["imageName"],
            candidate["sourcePage"])] = candidate

  values = list(unique.values())

  if not values:
    return None, None

  allmode = [
      item for item in values
      if re.search(r"[_ -]allmode\.", item["imageName"], re.IGNORECASE)
  ]

  if len(allmode) == 1:
    return allmode[0], None

  if len(allmode) > 1:
    return None, "multiple-allmode-images"

  variants: dict[str, dict[str, list[dict]]] = {}

  for item in values:
    match = re.search(r"([_ -])(lightmode|darkmode)\.", item["imageName"],
                      re.IGNORECASE)

    if match:
      base_name = (item["imageName"][:match.start()] +
                   item["imageName"][match.end() - 1:]).casefold()

      (variants.setdefault(base_name,
                           {}).setdefault(match.group(2).casefold(),
                                          []).append(item))

  if len(variants) == 1:
    variant_set = next(iter(variants.values()))

    if (len(variant_set.get("lightmode", [])) == 1
        and len(variant_set.get("darkmode", [])) <= 1):
      return variant_set["lightmode"][0], None

    if (len(variant_set.get("lightmode", [])) == 0
        and len(variant_set.get("darkmode", [])) == 1):
      return variant_set["darkmode"][0], None

  if len(values) == 1:
    return values[0], None

  return None, "multiple-team-logo-images"
