import hashlib
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import httpx
from bs4 import BeautifulSoup, Tag

from app.config import Settings
from app.importers.models import ImportRunError, LiquipediaSnapshot, MatchDTO, TeamDTO
from app.importers.providers.liquipedia_assets import validate_image_url
from app.importers.validation import normalize_status
from app.utils.dates import parse_utc_datetime
from app.utils.files import write_json_atomic
from app.utils.retry import RetryLimitExceeded, request_with_retry

LOGGER = logging.getLogger(__name__)
PARSER_NAME = "beautifulsoup4"
MATCH_SELECTORS = ".bracket-game, .bracket-match, .matchlist-row, .matchlist-match, .match-row, .match-card, [data-match-id], [data-matchid]"
TEAM_SELECTORS = ".team-template-text, .team-template-name, .teamname, .team-name, [data-team-name], [data-team]"
SCORE_SELECTORS = ".bracket-score, .match-score, .matchlist-score, .score"
PLACEHOLDER_TEAMS = {"bye", "tba", "tbd", "unknown"}
PROJECT_USER_AGENT_MARKER = "vct analytics"
USER_AGENT_CONTACT_PATTERN = re.compile(
    r"\b[^\s()<>@]+@[^\s()<>@]+\.[^\s()<>@]+\b")


def _iso_utc(value: datetime) -> str:
  return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_from_tag(tag: Tag) -> datetime | None:
  candidates = [tag]
  candidates.extend(tag.select("time, [data-timestamp], .timer-object"))

  for candidate in candidates:
    raw_value = candidate.get("datetime") or candidate.get("data-timestamp")

    if not raw_value:
      continue

    value = str(raw_value).strip()

    if value.isdigit():
      try:
        return datetime.fromtimestamp(int(value), timezone.utc)
      except (OverflowError, OSError, ValueError):
        continue

    parsed = parse_utc_datetime(value)

    if parsed is not None:
      return parsed

  text = tag.get_text(" ", strip=True)
  match = re.search(
      r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})\b",
      text)

  return parse_utc_datetime(match.group(0)) if match else None


def _team_names(tag: Tag) -> list[str | None]:
  opponents = tag.select(".match-info-header-opponent")

  if opponents:
    left_opponent = next(
        (opponent
         for opponent in opponents if "match-info-header-opponent-left" in
         opponent.get_attribute_list("class")), None)
    right_opponent = next(
        (opponent
         for opponent in opponents if "match-info-header-opponent-left" not in
         opponent.get_attribute_list("class")), None)

    if right_opponent is left_opponent:
      right_opponent = None

    if right_opponent is None and left_opponent is not None:
      right_opponent = next(
          (opponent
           for opponent in opponents if opponent is not left_opponent), None)

    ordered_opponents = [left_opponent, right_opponent
                         ] if left_opponent is not None else opponents[:2]
    names = []

    for opponent in ordered_opponents:
      if opponent is None:
        names.append(None)
        continue

      team_name = None
      team_element = opponent.select_one("[data-team-name]")

      if team_element is not None:
        team_name = team_element.get("data-team-name")

      if not team_name:
        name_link = opponent.select_one(".name a[title]")
        team_name = name_link.get("title") if name_link is not None else None

      if not team_name:
        name_element = opponent.select_one(".name")
        team_name = name_element.get_text(
            " ", strip=True) if name_element is not None else None

      names.append(
          re.sub(r"\s+", " ", str(team_name)).strip() if team_name else None)

    while len(names) < 2:
      names.append(None)

    return names

  names = []

  for element in tag.select(TEAM_SELECTORS):
    value = element.get("data-team-name") or element.get(
        "data-team") or element.get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", str(value)).strip()

    if value and value not in names:
      names.append(value)

  if len(names) >= 2:
    return names[:2]

  for anchor in tag.select('a[href*="/team/"]'):
    value = anchor.get("title") or anchor.get_text(" ", strip=True)
    value = re.sub(r"\s+", " ", str(value)).strip()

    if value and value not in names:
      names.append(value)

  return names[:2]


def _clean_team_name(value: str | None) -> str | None:
  if value is None:
    return None

  cleaned = re.sub(r"\s+", " ", value).strip()

  if not cleaned:
    return None

  if cleaned.casefold() in PLACEHOLDER_TEAMS:
    return None

  if re.match(r"^(winner|loser)\b", cleaned, re.IGNORECASE):
    return None

  return cleaned


def _scores(tag: Tag) -> tuple[int | None, int | None]:
  primary_scores = tag.select(".match-info-header-scoreholder-score")

  if primary_scores:
    values = [element.get_text(" ", strip=True) for element in primary_scores]

    if len(values) == 2 and all(
        re.fullmatch(r"\d+", value) for value in values):
      return int(values[0]), int(values[1])

    return None, None

  values = []

  for element in tag.select(SCORE_SELECTORS):
    if _is_in_map_or_veto(element, tag):
      continue

    text = element.get_text(" ", strip=True)

    if re.fullmatch(r"\d+", text):
      values.append(int(text))

  if len(values) == 2:
    return values[0], values[1]

  score_text = " ".join(
      element.get_text(" ", strip=True)
      for element in tag.select(SCORE_SELECTORS)
      if not _is_in_map_or_veto(element, tag))
  match = re.search(r"(?<!\d)(\d+)\s*[-–:]\s*(\d+)(?!\d)", score_text)

  if match:
    return int(match.group(1)), int(match.group(2))

  return None, None


def _is_in_map_or_veto(element: Tag, boundary: Tag) -> bool:
  for parent in (element, *element.parents):
    if parent is boundary:
      break

    classes = " ".join(parent.get_attribute_list("class")).casefold()

    if "map" in classes or "veto" in classes:
      return True

  return False


def _ancestor_with_class(tag: Tag, class_name: str) -> Tag | None:
  for candidate in (tag, *tag.parents):
    if class_name in candidate.get_attribute_list("class"):
      return candidate

  return None


def _associated_match_container(tag: Tag) -> Tag | None:
  for candidate in tag.parents:
    classes = candidate.get_attribute_list("class")

    if "brkts-matchlist-match" in classes or "brkts-match" in classes:
      return candidate

  return None


def _match_external_id(tag: Tag) -> str | None:
  candidates = [tag]
  outer_match = _associated_match_container(tag)

  if outer_match is not None:
    candidates.append(outer_match)

  for candidate in candidates:
    for attribute in ("data-match-id", "data-matchid", "data-game-id",
                      "data-gameid"):
      value = candidate.get(attribute)

      if value:
        return str(value)

    for anchor in candidate.select('a[href*="Match:"]'):
      match = re.search(r"Match:([^/?#]+)", str(anchor.get("href", "")),
                        re.IGNORECASE)

      if match:
        return f"Match:{match.group(1)}"

  return None


def _stage_and_round(tag: Tag) -> tuple[str | None, str | None, str | None]:
  match_list = _ancestor_with_class(tag, "brkts-matchlist")

  if match_list is not None:
    title = match_list.select_one(".brkts-matchlist-title")
    title_text = title.get_text(" ", strip=True) if title else ""
    group_match = re.search(r"\bgroup\s+([A-D])\b", title_text, re.IGNORECASE)
    group_name = group_match.group(1).upper() if group_match else None
    round_name = None

    for header in tag.find_all_previous(class_="brkts-matchlist-header"):
      if _ancestor_with_class(header, "brkts-matchlist") is match_list:
        round_name = header.get_text(" ", strip=True)
        break

    stage = "group-stage" if group_name is not None else None

    return stage, group_name, round_name

  if _ancestor_with_class(tag, "brkts-bracket") is not None:
    return "playoffs", None, None

  heading = tag.find_previous(["h2", "h3", "h4"])
  heading_text = heading.get_text(" ", strip=True) if heading else ""
  stage = (value if isinstance(value := tag.get("data-stage"), str) and value
           else None)
  round_name = (value if isinstance(value := tag.get("data-round"), str)
                and value else None)
  group_name = (value if isinstance(value := tag.get("data-group"), str)
                and value else None)

  if round_name is None:
    round_node = tag.select_one(
        ".bracket-header, .bracket-round, .matchlist-header, .matchlist-title")
    round_name = round_node.get_text(" ", strip=True) if round_node else None

  context = " ".join(value for value in (heading_text, str(round_name or ""))
                     if value)
  group_match = re.search(r"\bgroup\s+([A-D])\b|\(([A-D])\)", context,
                          re.IGNORECASE)

  if group_name is None and group_match:
    group_name = (group_match.group(1) or group_match.group(2)).upper()

  if stage is None:
    if re.search(r"\b(group|groups)\b", context, re.IGNORECASE):
      stage = "group-stage"
    elif re.search(r"\b(playoffs?|bracket|elimination)\b", context,
                   re.IGNORECASE):
      stage = "playoffs"

  return stage, group_name, round_name


def _match_containers(soup: BeautifulSoup) -> list[Tag]:
  popup_containers = list(soup.select(".brkts-match-info-popup"))
  candidates = popup_containers or list(soup.select(MATCH_SELECTORS))

  if not candidates:
    candidates = [
        row for row in soup.select("tr") if len(_team_names(row)) >= 2
    ]

  candidate_ids = {id(candidate) for candidate in candidates}
  result = []

  for candidate in candidates:
    if any(id(child) in candidate_ids for child in candidate.find_all(True)):
      continue

    result.append(candidate)

  return result


def parse_liquipedia_matches(
    payload: dict,
    page_title: str,
    event_slug: str,
    source_url: str,
    retrieved_at: datetime | None = None
) -> tuple[list[MatchDTO], LiquipediaSnapshot]:
  parse_data = payload.get("parse")

  if not isinstance(parse_data, dict):
    raise ImportRunError(
        "Liquipedia action=parse response did not contain a parse object.")

  html = parse_data.get("text")

  if not isinstance(html, str) or not html.strip():
    raise ImportRunError(
        "Liquipedia action=parse response did not contain page HTML.")

  soup = BeautifulSoup(html, "html.parser")
  containers = _match_containers(soup)

  if not containers:
    raise ImportRunError(
        f"Liquipedia parser found no recognized match blocks on '{page_title}'. The page layout may have changed."
    )

  revision = str(
      parse_data.get("revid")) if parse_data.get("revid") is not None else None
  source_timestamp = parse_utc_datetime(str(
      parse_data["timestamp"])) if parse_data.get("timestamp") else None
  status_reference_time = retrieved_at or datetime.now(timezone.utc)
  matches = []

  for container in containers:
    raw_teams = _team_names(container)
    team_a_name = _clean_team_name(raw_teams[0]) if raw_teams else None
    team_b_name = _clean_team_name(
        raw_teams[1]) if len(raw_teams) > 1 else None
    score_a, score_b = _scores(container)
    scheduled_at = _timestamp_from_tag(container)
    stage, group_name, round_name = _stage_and_round(container)
    match_id = _match_external_id(container)
    lower = container.select_one(".match-info-header-scoreholder-lower")
    best_of_match = re.search(r"\b(?:Bo|Best\s+of)\s*(\d+)\b",
                              lower.get_text(" ", strip=True),
                              re.IGNORECASE) if lower else None

    if best_of_match is None and not container.select(
        ".match-info-header-scoreholder-lower"):
      best_of_match = re.search(r"\b(?:Bo|Best\s+of)\s*(\d+)\b",
                                container.get_text(" ", strip=True),
                                re.IGNORECASE)

    best_of = int(best_of_match.group(1)) if best_of_match else None
    winner = _marked_winner(container, raw_teams)
    forfeit_result = _has_explicit_forfeit(container)
    raw_status, status_warnings = _match_status(container, score_a, score_b,
                                                best_of, scheduled_at,
                                                status_reference_time, winner,
                                                forfeit_result)
    warnings = []
    warnings.extend(status_warnings)

    if team_a_name is None or team_b_name is None:
      warnings.append(
          "One or both participants are missing or unresolved in Liquipedia markup."
      )

    if scheduled_at is None:
      warnings.append("Match time was not found with an explicit timezone.")

    if stage is None:
      warnings.append(
          "Stage could not be determined from the match's own bracket or match-list block."
      )

    if stage == "group-stage" and group_name is None:
      warnings.append(
          "Group could not be determined from the match's own match-list title."
      )

    if round_name is None:
      warnings.append(
          "Round could not be determined reliably from the match's own bracket or match-list block."
      )

    if match_id is None:
      warnings.append(
          "Match detail link or stable external identifier was not found in the match container."
      )

    matches.append(
        MatchDTO(
            provider="liquipedia",
            external_id=str(match_id) if match_id is not None else None,
            event_slug=event_slug,
            event_name=None,
            team_a=TeamDTO(name=team_a_name),
            team_b=TeamDTO(name=team_b_name),
            scheduled_at=scheduled_at,
            stage=stage,
            group_name=group_name,
            round_name=round_name,
            status=normalize_status(str(raw_status)) if raw_status else None,
            score_a=score_a,
            score_b=score_b,
            best_of=best_of,
            winner=winner if raw_status == "completed" else None,
            forfeit_result=forfeit_result
            if raw_status == "completed" else False,
            source_url=source_url,
            source_timestamp=source_timestamp,
            warnings=warnings))

  fetched_at = None

  if parse_data.get("timestamp"):
    fetched_at = parse_utc_datetime(str(parse_data["timestamp"]))

  snapshot = LiquipediaSnapshot(provider="liquipedia",
                                source_url=source_url,
                                fetched_at=retrieved_at or fetched_at,
                                cache_status="payload",
                                page_title=page_title,
                                revision=revision,
                                source_timestamp=source_timestamp)

  return matches, snapshot


def _status_marker(tag: Tag) -> str | None:
  outer_match = _associated_match_container(tag)
  scopes = [tag] + ([outer_match] if outer_match is not None else [])
  aliases = {
      "live": "live",
      "in progress": "live",
      "in-progress": "live",
      "in_progress": "live",
      "ongoing": "live",
      "completed": "completed",
      "complete": "completed",
      "finished": "completed",
      "final": "completed",
      "scheduled": "scheduled",
      "upcoming": "scheduled",
      "cancelled": "cancelled",
      "canceled": "cancelled",
      "postponed": "postponed"
  }

  for scope in scopes:
    value = scope.get("data-status")

    if value and str(value).strip().casefold() in aliases:
      return aliases[str(value).strip().casefold()]

    for class_name in scope.get_attribute_list("class"):
      normalized = str(class_name).casefold()

      for marker, status in aliases.items():
        if normalized == marker or normalized.endswith(
            (f"-{marker }", f"_{marker }")):
          return status

  for marker in tag.select(
      ".match-info-header-status, .brkts-match-status, .match-status"):
    value = marker.get_text(" ", strip=True).casefold()

    if value in aliases:
      return aliases[value]

  opponents = tag.select(".match-info-header-opponent")

  if len(opponents) == 2:
    team_markers = {
        token
        for opponent in opponents
        for token in opponent.get_attribute_list("class")
    }

    if "match-info-header-winner" in team_markers and "match-info-header-loser" in team_markers:
      return "completed"

  return None


def _marked_winner(tag: Tag, raw_teams: list[str | None]) -> str | None:
  opponents = tag.select(".match-info-header-opponent")

  if len(opponents) != 2 or len(raw_teams) < 2:
    return None

  left_index = next((index for index, opponent in enumerate(opponents)
                     if "match-info-header-opponent-left" in
                     opponent.get_attribute_list("class")), 0)
  right_index = 1 - left_index
  ordered_opponents = [opponents[left_index], opponents[right_index]]
  winners = [
      index for index, opponent in enumerate(ordered_opponents)
      if "match-info-header-winner" in opponent.get_attribute_list("class")
  ]
  losers = [
      index for index, opponent in enumerate(ordered_opponents)
      if "match-info-header-loser" in opponent.get_attribute_list("class")
  ]

  if len(winners) != 1 or len(losers) != 1 or winners[0] == losers[0]:
    return None

  return raw_teams[winners[0]]


def _has_explicit_forfeit(tag: Tag) -> bool:
  class_tokens = {
      str(token).casefold()
      for element in (tag, *tag.find_all(True))
      for token in element.get_attribute_list("class")
  }

  if any("forfeit" in token or "walkover" in token or "walk-over" in token
         for token in class_tokens):
    return True

  status_text = " ".join(
      element.get_text(" ", strip=True) for element in tag.select(
          ".match-info-header-status, .brkts-match-status, .match-status, .match-info-header-comment"
      ))

  return re.search(r"\b(?:forfeited?|walkover|walk-over|WO)\b|\bW\.O\.",
                   status_text, re.IGNORECASE) is not None


def _match_status(
    tag: Tag,
    score_a: int | None,
    score_b: int | None,
    best_of: int | None,
    scheduled_at: datetime | None,
    retrieved_at: datetime,
    source_winner: str | None = None,
    forfeit_result: bool = False) -> tuple[str | None, list[str]]:
  marker = _status_marker(tag)
  warnings: list[str] = []

  if marker in {"cancelled", "postponed"}:
    return marker, warnings

  started = scheduled_at is not None and scheduled_at <= retrieved_at
  has_score = isinstance(score_a, int) and isinstance(
      score_b, int) and score_a >= 0 and score_b >= 0
  reached_threshold = False
  score_is_legal = False

  if (isinstance(score_a, int) and isinstance(score_b, int) and has_score
      and score_a != score_b and isinstance(best_of, int) and best_of > 0):
    wins_required = best_of // 2 + 1
    leading_team = None

    if score_a > score_b:
      leading_team = _team_names(tag)[0]
    else:
      names = _team_names(tag)
      leading_team = names[1] if len(names) > 1 else None

    winner_score, loser_score = sorted((score_a, score_b), reverse=True)
    reached_threshold = winner_score >= wins_required
    score_is_legal = winner_score == wins_required and loser_score < wins_required

    if source_winner and leading_team and source_winner.casefold(
    ) != leading_team.casefold():
      warnings.append("RESULT_PENDING_SOURCE_UPDATE")
      score_is_legal = False

    if reached_threshold and not score_is_legal and "RESULT_PENDING_SOURCE_UPDATE" not in warnings:
      warnings.append("RESULT_PENDING_SOURCE_UPDATE")

  if forfeit_result and source_winner:
    if isinstance(score_a, int) and isinstance(
        score_b, int) and has_score and score_a != score_b:
      score_leader = _team_names(tag)[0 if score_a > score_b else 1]

      if score_leader and score_leader.casefold() == source_winner.casefold():
        return "completed", warnings

      warnings.append("RESULT_PENDING_SOURCE_UPDATE")
    elif not has_score:
      return "completed", warnings
    else:
      warnings.append("RESULT_PENDING_SOURCE_UPDATE")

  if score_is_legal:
    return "completed", warnings

  nonzero_score = isinstance(score_a, int) and isinstance(
      score_b, int) and has_score and (score_a > 0 or score_b > 0)

  if (started and
      (nonzero_score or marker in {"live", "completed"})) or marker == "live":
    if marker == "completed" and not score_is_legal:
      warnings.extend(("SOURCE_STATUS_NORMALIZED_TO_ONGOING",
                       "RESULT_PENDING_SOURCE_UPDATE"))

    return "live", list(dict.fromkeys(warnings))

  if marker == "completed":
    warnings.extend(("SOURCE_STATUS_NORMALIZED_TO_ONGOING",
                     "RESULT_PENDING_SOURCE_UPDATE"))

    return "scheduled", list(dict.fromkeys(warnings))

  if marker == "scheduled" or (scheduled_at is not None
                               and scheduled_at > retrieved_at):
    return "scheduled", warnings

  if reached_threshold or (isinstance(score_a, int) and isinstance(
      score_b, int) and has_score and score_a == score_b and
                           (score_a > 0 or score_b > 0)):
    warnings.append("RESULT_PENDING_SOURCE_UPDATE")

  return None, warnings


class LiquipediaProvider:

  def __init__(self,
               settings: Settings,
               cache_directory: Path,
               client: httpx.Client | None = None,
               write_cache: bool = True):
    self.settings = settings
    self.cache_directory = cache_directory / "liquipedia"
    self.client = client or httpx.Client(
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": settings.liquipedia_user_agent
        },
        timeout=httpx.Timeout(20))
    self._owns_client = client is None
    self.write_cache = write_cache
    self._warnings: list[str] = []
    self._current_event = "unknown"

  def close(self) -> None:
    if self._owns_client:
      self.client.close()

  def _request_params(self, page_title: str) -> dict[str, str]:
    return {
        "action": "parse",
        "page": page_title,
        "prop": "text|sections",
        "format": "json",
        "formatversion": "2"
    }

  def _cache_path(self, params: dict[str, str]) -> Path:
    request_identity = {
        "apiUrl": self.settings.liquipedia_api_url,
        "pageTitle": params["page"],
        "action": params["action"],
        "prop": params["prop"],
        "formatVersion": params["formatversion"]
    }
    key = hashlib.sha256(
        json.dumps(request_identity,
                   sort_keys=True).encode("utf-8")).hexdigest()

    return self.cache_directory / f"{key}.json"

  def _read_cache(self, cache_path: Path) -> tuple[dict, datetime] | None:
    if not cache_path.is_file():
      return None

    try:
      envelope = json.loads(cache_path.read_text(encoding="utf-8"))
      fetched_at = parse_utc_datetime(envelope.get("fetchedAt"))
      response = envelope.get("response")

      if fetched_at is None or not isinstance(response, dict):
        raise ValueError("Cache envelope is incomplete.")

      return response, fetched_at
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError,
            ValueError) as error:
      self._warnings.append(
          f"Ignored an invalid Liquipedia cache entry ({type(error).__name__})."
      )

      return None

  def _rate_state(self) -> Path:
    return self.cache_directory / ".rate-limit-state.json"

  def _dry_run_rate_state(self) -> Path:
    return self.settings.runtime_directory / "liquipedia-dry-run-rate-limit.json"

  def _wait_and_record_request(self, is_parse: bool = True) -> None:
    cache_state_path = self._rate_state()
    state_paths = [cache_state_path]
    persistence_path = cache_state_path

    if not self.write_cache:
      state_paths.append(self._dry_run_rate_state())
      persistence_path = self._dry_run_rate_state()

    last_requests: list[datetime] = []
    last_parses: list[datetime] = []

    for state_path in state_paths:
      state = {}

      if not state_path.is_file():
        continue

      try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
      except (OSError, UnicodeError, json.JSONDecodeError):
        self._warnings.append(
            "Rate-limit state could not be read; enforcing a fresh request interval."
        )

      if not isinstance(state, dict):
        self._warnings.append(
            "Rate-limit state was invalid; enforcing a fresh request interval."
        )
        state = {}

      last_request = parse_utc_datetime(state.get("lastRequestAt"))
      last_parse = parse_utc_datetime(state.get("lastParseAt"))

      if last_request is not None:
        last_requests.append(last_request)

      if last_parse is not None:
        last_parses.append(last_parse)

    now = datetime.now(timezone.utc)
    last_request = max(last_requests) if last_requests else None
    last_parse = max(last_parses) if last_parses else None
    wait_seconds = 0.0

    if not self.write_cache and not last_requests and not last_parses:
      wait_seconds = max(
          self.settings.liquipedia_request_interval_seconds,
          self.settings.liquipedia_parse_interval_seconds if is_parse else 0)

    if last_request is not None:
      wait_seconds = max(
          wait_seconds, self.settings.liquipedia_request_interval_seconds -
          (now - last_request).total_seconds())

    if is_parse and last_parse is not None:
      wait_seconds = max(
          wait_seconds, self.settings.liquipedia_parse_interval_seconds -
          (now - last_parse).total_seconds())

    if wait_seconds > 0:
      LOGGER.info(
          "provider=liquipedia stage=rate_limit event=%s match_identity=page elapsed_seconds=%.1f result=waiting retry_count=0",
          self._current_event, wait_seconds)
      time.sleep(wait_seconds)

    request_at = datetime.now(timezone.utc)
    rate_state = {"lastRequestAt": _iso_utc(request_at)}

    if is_parse:
      rate_state["lastParseAt"] = _iso_utc(request_at)
    elif last_parse is not None:
      rate_state["lastParseAt"] = _iso_utc(last_parse)

    write_json_atomic(persistence_path, rate_state)

  def _validate_user_agent(self) -> None:
    user_agent = self.settings.liquipedia_user_agent

    if not user_agent:
      raise ImportRunError(
          "Live Liquipedia requests require LIQUIPEDIA_USER_AGENT with the project name and your contact email. "
          "Set it in backend/.env; do not invent contact details.")

    if (PROJECT_USER_AGENT_MARKER not in user_agent.casefold()
        or not USER_AGENT_CONTACT_PATTERN.search(user_agent)):
      raise ImportRunError(
          "LIQUIPEDIA_USER_AGENT must identify VCT Analytics and include the contact address you provided. "
          "Set it in backend/.env.")

  def _request_json(self, params: dict[str, str],
                    match_identity: str) -> tuple[dict, datetime]:
    self._validate_user_agent()

    def send_request() -> httpx.Response:
      return self.client.get(self.settings.liquipedia_api_url, params=params)

    try:
      response = request_with_retry(send_request,
                                    self._wait_and_record_request,
                                    event=self._current_event,
                                    match_identity=match_identity)
    except httpx.HTTPStatusError as error:
      status_code = error.response.status_code
      raise ImportRunError(
          f"Liquipedia API returned HTTP {status_code}.") from error
    except (httpx.TimeoutException, httpx.NetworkError,
            RetryLimitExceeded) as error:
      raise ImportRunError(
          "Liquipedia API request failed after bounded retries.") from error

    try:
      payload = response.json()
    except (json.JSONDecodeError, ValueError) as error:
      raise ImportRunError("Liquipedia API returned invalid JSON.") from error

    if not isinstance(payload, dict):
      raise ImportRunError("Liquipedia API response must be a JSON object.")

    fetched_at = datetime.now(timezone.utc)

    return payload, fetched_at

  def download_team_logo(self, raw_url: str,
                         team_slug: str) -> tuple[bytes, str, str, datetime]:
    validated_url, url_error = validate_image_url(raw_url)

    if url_error or validated_url is None or validated_url != raw_url:
      raise ImportRunError(
          "Liquipedia team logo URL failed source validation.")

    self._validate_user_agent()

    def send_request() -> httpx.Response:
      request = self.client.build_request(
          "GET",
          validated_url,
          headers={"Accept": "image/png, image/webp, image/jpeg"})

      return self.client.send(request, stream=True)

    try:
      response = request_with_retry(
          send_request,
          lambda: self._wait_and_record_request(is_parse=False),
          event=self._current_event,
          match_identity="team-logo")
    except httpx.HTTPStatusError as error:
      raise ImportRunError(
          f"Liquipedia team logo returned HTTP {error.response.status_code}."
      ) from error
    except (httpx.TimeoutException, httpx.NetworkError,
            RetryLimitExceeded) as error:
      raise ImportRunError(
          "Liquipedia team logo request failed after bounded retries."
      ) from error

    try:
      if response.status_code != 200:
        raise ImportRunError(
            f"Liquipedia team logo returned HTTP {response.status_code}.")

      mime_type = response.headers.get("Content-Type",
                                       "").split(";", 1)[0].strip().casefold()

      if mime_type not in {"image/png", "image/webp", "image/jpeg"}:
        raise ImportRunError(
            "Liquipedia team logo used an unsupported content type.")

      content_length = response.headers.get("Content-Length")

      if content_length:
        try:
          if int(content_length) <= 0 or int(content_length) > 2 * 1024 * 1024:
            raise ImportRunError(
                "Liquipedia team logo exceeded the permitted file size.")
        except ValueError as error:
          raise ImportRunError(
              "Liquipedia team logo returned an invalid content length."
          ) from error

      chunks = []
      total_size = 0
      digest = hashlib.sha256()

      for chunk in response.iter_bytes():
        if not chunk:
          continue

        total_size += len(chunk)

        if total_size > 2 * 1024 * 1024:
          raise ImportRunError(
              "Liquipedia team logo exceeded the permitted file size.")

        chunks.append(chunk)
        digest.update(chunk)

      data = b"".join(chunks)
      from app.assets import validate_image_bytes

      detected_mime, validation_error = validate_image_bytes(data, mime_type)

      if validation_error or detected_mime is None:
        raise ImportRunError("Liquipedia team logo failed image validation.")

      return data, detected_mime, digest.hexdigest(), datetime.now(
          timezone.utc)
    finally:
      response.close()

  def _fetch_payload(self, page_title: str,
                     cache_path: Path) -> tuple[dict, datetime, str]:
    params = self._request_params(page_title)
    payload, fetched_at = self._request_json(params, "page")

    if self.write_cache:
      write_json_atomic(
          cache_path, {
              "fetchedAt": _iso_utc(fetched_at),
              "request": {
                  "apiUrl": self.settings.liquipedia_api_url,
                  "parameters": params
              },
              "response": payload
          })

    return payload, fetched_at, "fetched" if self.write_cache else "live-no-cache"

  def fetch_matches(
      self,
      page_title: str,
      event_slug: str,
      refresh: bool = False,
      offline: bool = False) -> tuple[list[MatchDTO], LiquipediaSnapshot]:
    self._current_event = event_slug
    started_at = time.monotonic()

    if refresh and offline:
      raise ImportRunError("--refresh cannot be used with --offline.")

    params = self._request_params(page_title)
    cache_path = self._cache_path(params)
    cached = self._read_cache(cache_path)

    if cached is not None and offline:
      payload, fetched_at = cached
      cache_status = "cache-offline"
    elif offline:
      raise ImportRunError(
          f"Offline Liquipedia cache is missing for page '{page_title}'.")
    else:
      cached_is_fresh = (cached is not None and (datetime.now(timezone.utc) -
                                                 cached[1]).total_seconds()
                         <= self.settings.liquipedia_cache_ttl_seconds)

      if cached is not None and cached_is_fresh and not refresh:
        payload, fetched_at = cached
        cache_status = "cache"
      else:
        payload, fetched_at, cache_status = self._fetch_payload(
            page_title, cache_path)

    source_url = f"https://liquipedia.net/valorant/{quote(page_title, safe='/')}"
    matches, snapshot = parse_liquipedia_matches(payload,
                                                 page_title,
                                                 event_slug,
                                                 source_url,
                                                 retrieved_at=fetched_at)
    snapshot.fetched_at = fetched_at
    snapshot.cache_status = cache_status
    snapshot.content_sha256 = hashlib.sha256(
        json.dumps(payload,
                   ensure_ascii=False,
                   sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()

    snapshot.warnings.extend(self._warnings)
    LOGGER.info(
        "provider=liquipedia stage=parse event=%s match_identity=page elapsed_seconds=%.3f result=complete retry_count=0 match_count=%s",
        event_slug,
        time.monotonic() - started_at, len(matches))

    return matches, snapshot
