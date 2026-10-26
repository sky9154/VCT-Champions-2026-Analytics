import re

from app.importers.models import MatchDTO, MatchReference, SourceReference, TeamReference
from app.importers.validation import normalize_round, normalize_stage, normalize_status, normalize_team_name

VERIFIED_CROSS_SOURCE_ID_MAP: dict[str, str] = {}


def _team_names(match: MatchDTO) -> tuple[str | None, str | None]:
  return normalize_team_name(match.team_a.name), normalize_team_name(
      match.team_b.name)


def _known_teams(match: MatchDTO) -> list[str]:
  return [name for name in _team_names(match) if name is not None]


def _team_order(match_a: MatchDTO, match_b: MatchDTO) -> tuple[bool, bool]:
  a_names = _team_names(match_a)
  b_names = _team_names(match_b)

  if None in a_names or None in b_names:
    return False, False

  if a_names == b_names:
    return True, False

  if a_names == (b_names[1], b_names[0]):
    return True, True

  return False, False


def _same_event(match_a: MatchDTO, match_b: MatchDTO) -> bool:
  return match_a.event_slug.casefold() == match_b.event_slug.casefold()


def _stage_round_match(match_a: MatchDTO, match_b: MatchDTO) -> bool:
  stage_a = normalize_stage(match_a.stage)
  stage_b = normalize_stage(match_b.stage)
  round_a = normalize_round(match_a.round_name)
  round_b = normalize_round(match_b.round_name)

  return bool(stage_a and stage_b and round_a and round_b
              and stage_a == stage_b and round_a == round_b)


def _candidate_matches(liquipedia_match: MatchDTO,
                       duckdb_matches: list[MatchDTO]) -> list[int]:
  available = [
      index for index, match in enumerate(duckdb_matches)
      if _same_event(liquipedia_match, match)
  ]

  same_provider_ids = [
      index for index in available
      if liquipedia_match.provider == duckdb_matches[index].provider
      and liquipedia_match.external_id is not None
      and liquipedia_match.external_id == duckdb_matches[index].external_id
  ]

  if same_provider_ids:
    return same_provider_ids

  mapped_vct_id = VERIFIED_CROSS_SOURCE_ID_MAP.get(liquipedia_match.external_id
                                                   or "")

  if mapped_vct_id is not None:
    mapped_ids = [
        index for index in available
        if duckdb_matches[index].external_id == mapped_vct_id
    ]

    if mapped_ids:
      return mapped_ids

  complete_team_candidates = []

  for index in available:
    team_match, _ = _team_order(liquipedia_match, duckdb_matches[index])

    if not team_match:
      continue

    if _stage_round_match(liquipedia_match, duckdb_matches[index]):
      complete_team_candidates.append(index)

  if complete_team_candidates:
    exact_candidates = [
        index for index in complete_team_candidates
        if _same_group_and_best_of(liquipedia_match, duckdb_matches[index])
    ]

    if exact_candidates:
      return exact_candidates

    return complete_team_candidates

  return []


def _append_difference(differences: list[dict],
                       field: str,
                       liq_value,
                       duck_value,
                       severity: str = "conflict") -> None:
  differences.append({
      "field": field,
      "liquipedia": liq_value,
      "duckdb": duck_value,
      "severity": severity
  })


def _normalize_group(value: str | None) -> str | None:
  if not value:
    return None

  normalized = re.sub(r"[^a-z0-9]+", "", value.casefold())

  if normalized.startswith("group"):
    normalized = normalized[len("group"):]

  return normalized or None


def _same_group_and_best_of(match_a: MatchDTO, match_b: MatchDTO) -> bool:
  group_a = _normalize_group(match_a.group_name)
  group_b = _normalize_group(match_b.group_name)

  return bool(group_a and group_b and group_a == group_b
              and match_a.best_of is not None and match_b.best_of is not None
              and match_a.best_of == match_b.best_of)


def _reliable_match_identity(liquipedia_match: MatchDTO,
                             duckdb_match: MatchDTO,
                             liquipedia_id_is_unique: bool,
                             duckdb_id_is_unique: bool) -> bool:
  team_match, _ = _team_order(liquipedia_match, duckdb_match)
  lp_teams = _team_names(liquipedia_match)
  db_teams = _team_names(duckdb_match)
  lp_stage, db_stage = normalize_stage(
      liquipedia_match.stage), normalize_stage(duckdb_match.stage)
  lp_group, db_group = _normalize_group(
      liquipedia_match.group_name), _normalize_group(duckdb_match.group_name)
  lp_round, db_round = normalize_round(
      liquipedia_match.round_name), normalize_round(duckdb_match.round_name)

  return bool(
      _same_event(liquipedia_match, duckdb_match)
      and not (liquipedia_match.event_external_id
               and duckdb_match.event_external_id
               and liquipedia_match.event_external_id
               != duckdb_match.event_external_id) and team_match
      and all(name is not None for name in lp_teams + db_teams)
      and lp_teams[0] != lp_teams[1] and lp_stage and lp_stage == db_stage
      and lp_group and lp_group == db_group and lp_round
      and lp_round == db_round and liquipedia_match.best_of is not None
      and liquipedia_match.best_of == duckdb_match.best_of
      and isinstance(liquipedia_match.external_id, str)
      and bool(liquipedia_match.external_id.strip())
      and isinstance(duckdb_match.external_id, str)
      and bool(duckdb_match.external_id.strip()) and liquipedia_id_is_unique
      and duckdb_id_is_unique)


def _reliable_schedule_identity(liquipedia_match: MatchDTO,
                                duckdb_match: MatchDTO,
                                liquipedia_id_is_unique: bool,
                                duckdb_id_is_unique: bool) -> bool:
  same_provider_identity = (liquipedia_match.provider == duckdb_match.provider
                            and isinstance(liquipedia_match.external_id, str)
                            and bool(liquipedia_match.external_id.strip())
                            and liquipedia_match.external_id
                            == duckdb_match.external_id)
  compatible_event_identity = not (
      liquipedia_match.event_external_id and duckdb_match.event_external_id
      and liquipedia_match.event_external_id != duckdb_match.event_external_id)

  return bool(
      _same_event(liquipedia_match, duckdb_match) and compatible_event_identity
      and liquipedia_id_is_unique and duckdb_id_is_unique
      and (same_provider_identity or _reliable_match_identity(
          liquipedia_match, duckdb_match, liquipedia_id_is_unique,
          duckdb_id_is_unique)))


def _completed_score_is_legal(match: MatchDTO) -> bool:
  if normalize_status(match.status) != "completed":
    return False

  if match.forfeit_result and match.winner in {
      match.team_a.name, match.team_b.name
  }:
    if match.score_a is None or match.score_b is None:
      return True

    score_winner = match.team_a.name if match.score_a > match.score_b else match.team_b.name if match.score_b > match.score_a else None

    return score_winner == match.winner

  if not isinstance(match.best_of, int) or match.best_of < 1:
    return False

  if not isinstance(match.score_a, int) or not isinstance(match.score_b, int):
    return False

  if match.score_a < 0 or match.score_b < 0 or match.score_a == match.score_b:
    return False

  wins_required = match.best_of // 2 + 1
  winner_score, loser_score = sorted((match.score_a, match.score_b),
                                     reverse=True)
  score_winner = match.team_a.name if match.score_a > match.score_b else match.team_b.name

  return winner_score == wins_required and loser_score < wins_required and (
      match.winner is None or match.winner == score_winner)


def _compare_matches(
    liquipedia_match: MatchDTO,
    duckdb_match: MatchDTO,
    tolerance_minutes: int,
    reliable_identity: bool = False) -> tuple[str, list[dict], bool]:
  differences: list[dict] = []
  mismatch_severity = "warning" if reliable_identity else "conflict"
  team_match, reversed_order = _team_order(liquipedia_match, duckdb_match)
  lp_known_teams = _known_teams(liquipedia_match)
  db_known_teams = _known_teams(duckdb_match)
  liquipedia_schedule_is_newer = False

  if len(lp_known_teams) == 2 and len(db_known_teams) == 2 and not team_match:
    _append_difference(differences, "teams", lp_known_teams, db_known_teams,
                       mismatch_severity)
  elif len(lp_known_teams) > len(db_known_teams):
    _append_difference(differences, "teams", lp_known_teams, db_known_teams,
                       "warning" if reliable_identity else "liquipedia_newer")
  elif len(db_known_teams) > len(lp_known_teams):
    _append_difference(differences, "teams", lp_known_teams, db_known_teams,
                       "warning" if reliable_identity else "incomplete")

  if liquipedia_match.scheduled_at is not None and duckdb_match.scheduled_at is not None:
    delta_minutes = abs((liquipedia_match.scheduled_at -
                         duckdb_match.scheduled_at).total_seconds()) / 60

    if delta_minutes > tolerance_minutes:
      liquipedia_schedule_is_newer = reliable_identity
      _append_difference(differences, "scheduledAt",
                         liquipedia_match.scheduled_at.isoformat(),
                         duckdb_match.scheduled_at.isoformat(),
                         "warning" if reliable_identity else "conflict")
    elif delta_minutes > 0:
      _append_difference(differences, "scheduledAt",
                         liquipedia_match.scheduled_at.isoformat(),
                         duckdb_match.scheduled_at.isoformat(), "warning")
  elif liquipedia_match.scheduled_at is not None and duckdb_match.scheduled_at is None:
    _append_difference(differences, "scheduledAt",
                       liquipedia_match.scheduled_at.isoformat(), None,
                       "liquipedia_newer")
  elif duckdb_match.scheduled_at is not None and liquipedia_match.scheduled_at is None:
    _append_difference(differences, "scheduledAt", None,
                       duckdb_match.scheduled_at.isoformat(), "incomplete")

  lp_stage = normalize_stage(liquipedia_match.stage)
  db_stage = normalize_stage(duckdb_match.stage)

  if lp_stage and db_stage and lp_stage != db_stage:
    _append_difference(differences, "stage", lp_stage, db_stage,
                       mismatch_severity)
  elif lp_stage and not db_stage:
    _append_difference(differences, "stage", lp_stage, None, mismatch_severity)
  elif lp_stage is None or db_stage is None:
    _append_difference(differences, "stage", lp_stage, db_stage)

  lp_round = normalize_round(liquipedia_match.round_name)
  db_round = normalize_round(duckdb_match.round_name)

  if lp_round and db_round and lp_round != db_round:
    _append_difference(differences, "round", liquipedia_match.round_name,
                       duckdb_match.round_name, mismatch_severity)
  elif lp_round and not db_round and "playoffs" not in {lp_stage, db_stage}:
    _append_difference(differences, "round", lp_round, None, mismatch_severity)
  elif lp_round is None or db_round is None:
    _append_difference(differences, "round", lp_round, db_round)

  lp_group = _normalize_group(liquipedia_match.group_name)
  db_group = _normalize_group(duckdb_match.group_name)

  if lp_group and db_group and lp_group != db_group:
    _append_difference(differences, "group", lp_group, db_group,
                       mismatch_severity)
  elif lp_group and not db_group:
    _append_difference(differences, "group", lp_group, None, mismatch_severity)
  elif lp_group is None or db_group is None:
    _append_difference(differences, "group", lp_group, db_group)

  if (liquipedia_match.best_of is not None and duckdb_match.best_of is not None
      and liquipedia_match.best_of != duckdb_match.best_of):
    _append_difference(differences, "bestOf", liquipedia_match.best_of,
                       duckdb_match.best_of, mismatch_severity)
  elif liquipedia_match.best_of is not None and duckdb_match.best_of is None:
    _append_difference(differences, "bestOf", liquipedia_match.best_of, None,
                       mismatch_severity)
  elif liquipedia_match.best_of is None or duckdb_match.best_of is None:
    _append_difference(differences, "bestOf", liquipedia_match.best_of,
                       duckdb_match.best_of)

  lp_status = normalize_status(liquipedia_match.status)
  db_status = normalize_status(duckdb_match.status)
  db_status_is_placeholder = db_status in {None, "tbd"}
  lp_result_override = (reliable_identity and lp_status == "completed"
                        and db_status == "scheduled"
                        and _completed_score_is_legal(liquipedia_match)
                        and duckdb_match.score_a is None
                        and duckdb_match.score_b is None)

  if lp_status and not db_status_is_placeholder and lp_status != db_status:
    if lp_result_override:
      _append_difference(differences, "status", lp_status, db_status,
                         "liquipedia_newer")
    else:
      _append_difference(differences, "status", lp_status, db_status,
                         mismatch_severity)
  elif lp_status and db_status_is_placeholder:
    _append_difference(differences, "status", lp_status, None,
                       "liquipedia_newer")

  db_score_a, db_score_b = duckdb_match.score_a, duckdb_match.score_b

  if reversed_order:
    db_score_a, db_score_b = db_score_b, db_score_a

  lp_has_score = liquipedia_match.score_a is not None and liquipedia_match.score_b is not None
  db_has_score = db_score_a is not None and db_score_b is not None

  if lp_status == "completed" and not _completed_score_is_legal(
      liquipedia_match):
    _append_difference(differences, "score",
                       [liquipedia_match.score_a, liquipedia_match.score_b],
                       [db_score_a, db_score_b])
  elif db_status == "completed" and not db_has_score:
    _append_difference(differences, "score",
                       [liquipedia_match.score_a, liquipedia_match.score_b],
                       [db_score_a, db_score_b], mismatch_severity)
  elif lp_has_score and db_has_score:
    if (liquipedia_match.score_a, liquipedia_match.score_b) != (db_score_a,
                                                                db_score_b):
      _append_difference(differences, "score",
                         [liquipedia_match.score_a, liquipedia_match.score_b],
                         [db_score_a, db_score_b], mismatch_severity)
  elif lp_has_score and not db_has_score and lp_result_override:
    _append_difference(differences, "score",
                       [liquipedia_match.score_a, liquipedia_match.score_b],
                       [db_score_a, db_score_b], "liquipedia_newer")
  elif lp_has_score != db_has_score:
    _append_difference(differences, "score",
                       [liquipedia_match.score_a, liquipedia_match.score_b],
                       [db_score_a, db_score_b], mismatch_severity)

  if not reliable_identity:
    _append_difference(differences, "identity", "reliable canonical identity",
                       "unstable or non-unique candidate")

  severities = {difference["severity"] for difference in differences}

  if "conflict" in severities:
    status = "conflict"
  elif "liquipedia_newer" in severities or liquipedia_schedule_is_newer:
    status = "liquipedia_newer"
  elif "warning" in severities:
    status = "liquipedia_newer"
  elif len(lp_known_teams
           ) < 2 or lp_status is None or liquipedia_match.scheduled_at is None:
    status = "incomplete"
  elif lp_status == "completed" and (not lp_has_score or not db_has_score):
    status = "incomplete"
  elif differences and "incomplete" in severities:
    status = "incomplete"
  elif lp_status == "completed" and duckdb_match.stats_available:
    status = "duckdb_stats_available"
  else:
    status = "matched"

  return status, differences, reversed_order


def _format_team(match: MatchDTO, team) -> dict:
  return TeamReference(name=team.name,
                       provider=match.provider,
                       external_id=team.external_id,
                       source_url=match.source_url).model_dump(mode="json",
                                                               by_alias=True)


def _format_match_identity(match: MatchDTO | None) -> dict | None:
  if match is None:
    return None

  return MatchReference(
      provider=match.provider,
      external_id=match.external_id,
      source=SourceReference(provider=match.provider,
                             external_id=match.external_id,
                             url=match.source_url),
      event_slug=match.event_slug,
      event_external_id=match.event_external_id,
      teams=[
          TeamReference.model_validate(_format_team(match, team))
          for team in (match.team_a, match.team_b)
      ],
      scheduled_at=match.scheduled_at,
      stage=match.stage,
      group_name=match.group_name,
      round_name=match.round_name,
      status=match.status,
      score_a=match.score_a,
      score_b=match.score_b,
      best_of=match.best_of,
      winner=match.winner
      if normalize_status(match.status) == "completed" else None,
      forfeit_result=match.forfeit_result
      if normalize_status(match.status) == "completed" else False,
      source_url=match.source_url).model_dump(mode="json", by_alias=True)


def _suggested_action(status: str) -> str:
  actions = {
      "matched":
      "Use Liquipedia for canonical Schedule fields and DuckDB only for verified detailed statistics.",
      "liquipedia_newer":
      "Use Liquipedia for the newer schedule or bracket fields; leave DuckDB stats unchanged.",
      "duckdb_stats_available":
      "The completed result is verified and DuckDB detailed stats are available for a later import phase.",
      "conflict":
      "Keep the Liquipedia Schedule; exclude DuckDB statistics until the conflicting identity is resolved.",
      "incomplete":
      "Keep the Liquipedia Schedule; wait for complete identity fields before linking DuckDB statistics.",
      "unmatched":
      "Keep the Liquipedia Schedule; no verified DuckDB statistics match is available.",
      "ambiguous":
      "Keep the Liquipedia Schedule; do not choose among ambiguous DuckDB statistics candidates."
  }

  return actions[status]


def _winner(match: MatchDTO | None,
            reversed_order: bool = False) -> str | None:
  if match is None or normalize_status(match.status) != "completed":
    return None

  if match.winner in {match.team_a.name, match.team_b.name}:
    return match.winner

  if (match is None or match.score_a is None or match.score_b is None
      or match.score_a == match.score_b):
    return None

  winner_index = 0 if match.score_a > match.score_b else 1

  if reversed_order:
    winner_index = 1 - winner_index

  return match.team_a.name if winner_index == 0 else match.team_b.name


def _report_match(status: str,
                  liquipedia_match: MatchDTO | None,
                  duckdb_match: MatchDTO | None,
                  differences: list[dict],
                  reversed_order: bool = False) -> dict:
  lp = liquipedia_match
  db = duckdb_match
  both_completed = (lp is not None and db is not None
                    and normalize_status(lp.status) == "completed"
                    and normalize_status(db.status) == "completed")
  lp_score = [lp.score_a, lp.score_b] if lp else None
  db_score = [db.score_a, db.score_b] if db else None

  if db is not None and reversed_order:
    db_score = [db.score_b, db.score_a]

  rendered_differences = []

  for difference in differences:
    item = dict(difference)

    if lp is not None and db is not None:
      item["severity"] = "warning"

      if item.get("field") in {"score", "winner"}:
        item["priority"] = "high" if both_completed else "normal"

    rendered_differences.append(item)

  lp_winner = _winner(lp)
  db_winner = _winner(db, reversed_order)

  if (lp is not None and db is not None and lp_winner != db_winner
      and (lp_winner is not None or db_winner is not None)):
    rendered_differences.append({
        "field":
        "winner",
        "liquipedia":
        lp_winner,
        "duckdb":
        db_winner,
        "severity":
        "warning",
        "priority":
        "high" if both_completed else "normal"
    })

  safe_status_warnings = {
      "SOURCE_STATUS_NORMALIZED_TO_ONGOING", "RESULT_PENDING_SOURCE_UPDATE"
  }

  if lp is not None:
    for code in dict.fromkeys(code for code in lp.warnings
                              if code in safe_status_warnings):
      rendered_differences.append({
          "field": "sourceStatus",
          "liquipedia": code,
          "duckdb": None,
          "severity": "warning",
          "priority": "normal"
      })

  return {
      "status": status,
      "scheduleStatus": "not_applicable" if lp is None else "ready",
      "statsLinkStatus": "unmatched" if lp is None else status,
      "liquipediaIdentity": _format_match_identity(lp),
      "duckdbIdentity": _format_match_identity(db),
      "teams": {
          "liquipedia":
          [_format_team(lp, lp.team_a),
           _format_team(lp, lp.team_b)] if lp else None,
          "duckdb": [_format_team(db, db.team_a),
                     _format_team(db, db.team_b)] if db else None,
          "teamOrderReversed":
          reversed_order
      },
      "time": {
          "liquipedia":
          lp.scheduled_at.isoformat().replace("+00:00", "Z")
          if lp and lp.scheduled_at else None,
          "duckdb":
          db.scheduled_at.isoformat().replace("+00:00", "Z")
          if db and db.scheduled_at else None
      },
      "stage": {
          "liquipedia": lp.stage if lp else None,
          "duckdb": db.stage if db else None
      },
      "round": {
          "liquipedia": lp.round_name if lp else None,
          "duckdb": db.round_name if db else None
      },
      "group": {
          "liquipedia": lp.group_name if lp else None,
          "duckdb": db.group_name if db else None
      },
      "bestOf": {
          "liquipedia": lp.best_of if lp else None,
          "duckdb": db.best_of if db else None
      },
      "scores": {
          "liquipedia": lp_score,
          "duckdb": db_score
      },
      "winner": {
          "liquipedia": _winner(lp),
          "duckdb": _winner(db, reversed_order)
      },
      "differences": rendered_differences,
      "sourceUrls": {
          "liquipedia": lp.source_url if lp else None,
          "duckdb": db.source_url if db else None
      },
      "externalIds": {
          "liquipedia":
          lp.external_id if lp else None,
          "duckdb":
          db.external_id if db else None,
          "liquipediaEvent":
          lp.event_external_id if lp else None,
          "duckdbEvent":
          db.event_external_id if db else None,
          "liquipediaTeams":
          [lp.team_a.external_id, lp.team_b.external_id] if lp else None,
          "duckdbTeams":
          [db.team_a.external_id, db.team_b.external_id] if db else None
      },
      "statsAvailability": {
          "available": db.stats_available if db else False,
          "completeMaps": db.stats_map_count if db else 0,
          "completePlayerRows": db.stats_player_rows if db else 0
      },
      "suggestedAction": _suggested_action(status)
  }


def reconcile_matches(liquipedia_matches: list[MatchDTO],
                      duckdb_matches: list[MatchDTO],
                      tolerance_minutes: int,
                      liquipedia_available: bool = True,
                      duckdb_available: bool = True) -> list[dict]:
  report_matches = []
  used_duckdb: set[int] = set()
  ambiguous_duckdb: set[int] = set()
  liquipedia_id_counts: dict[str, int] = {}
  duckdb_id_counts: dict[str, int] = {}

  for match in liquipedia_matches:
    if isinstance(match.external_id, str) and match.external_id.strip():
      liquipedia_id_counts[match.external_id] = liquipedia_id_counts.get(
          match.external_id, 0) + 1

  for match in duckdb_matches:
    if isinstance(match.external_id, str) and match.external_id.strip():
      duckdb_id_counts[match.external_id] = duckdb_id_counts.get(
          match.external_id, 0) + 1

  for liquipedia_match in liquipedia_matches:
    candidates = _candidate_matches(liquipedia_match, duckdb_matches)

    if len(candidates) > 1:
      ambiguous_duckdb.update(candidates)
      report_matches.append(
          _report_match("ambiguous", liquipedia_match, None, [{
              "field":
              "candidateMatches",
              "liquipedia":
              liquipedia_match.external_id,
              "duckdb":
              [duckdb_matches[index].external_id for index in candidates],
              "severity":
              "ambiguous"
          }]))
      report_matches[-1]["candidateDuckdbIdentities"] = [
          _format_match_identity(duckdb_matches[index]) for index in candidates
      ]

      continue

    if len(candidates) == 1:
      candidate_index = candidates[0]

      if candidate_index in used_duckdb:
        ambiguous_duckdb.add(candidate_index)
        report_matches.append(
            _report_match(
                "ambiguous", liquipedia_match, duckdb_matches[candidate_index],
                [{
                    "field": "identity",
                    "liquipedia": liquipedia_match.external_id,
                    "duckdb": duckdb_matches[candidate_index].external_id,
                    "severity": "ambiguous"
                }]))
        continue

      used_duckdb.add(candidate_index)
      duckdb_match = duckdb_matches[candidate_index]
      reliable_identity = _reliable_schedule_identity(
          liquipedia_match, duckdb_match,
          liquipedia_id_counts.get(liquipedia_match.external_id or "") == 1,
          duckdb_id_counts.get(duckdb_match.external_id or "") == 1)
      status, differences, reversed_order = _compare_matches(
          liquipedia_match, duckdb_match, tolerance_minutes, reliable_identity)
      row = _report_match(status, liquipedia_match, duckdb_match, differences,
                          reversed_order)
      report_matches.append(row)
      exact_identity = _reliable_match_identity(
          liquipedia_match, duckdb_match,
          liquipedia_id_counts.get(liquipedia_match.external_id or "") == 1,
          duckdb_id_counts.get(duckdb_match.external_id or "") == 1)
      lp_scores = (liquipedia_match.score_a, liquipedia_match.score_b)
      db_scores = (duckdb_match.score_a, duckdb_match.score_b)

      if reversed_order:
        db_scores = (db_scores[1], db_scores[0])

      both_completed = (
          normalize_status(liquipedia_match.status) == "completed"
          and normalize_status(duckdb_match.status) == "completed")
      score_conflict = both_completed and all(
          value is not None
          for value in lp_scores + db_scores) and lp_scores != db_scores
      lp_winner = _winner(liquipedia_match)
      db_winner = _winner(duckdb_match, reversed_order)
      winner_conflict = both_completed and lp_winner != db_winner and (
          lp_winner is not None or db_winner is not None)

      if exact_identity and (score_conflict or winner_conflict):
        row["statsLinkStatus"] = "conflict"
      elif exact_identity and duckdb_match.stats_available:
        row["statsLinkStatus"] = "available"
      elif exact_identity:
        row["statsLinkStatus"] = "matched"
      else:
        row["statsLinkStatus"] = "ambiguous"

      continue

    known_teams = _known_teams(liquipedia_match)

    if not duckdb_available:
      status = "incomplete"
      differences = [{
          "field": "duckdbSource",
          "liquipedia": liquipedia_match.external_id,
          "duckdb": None,
          "severity": "incomplete"
      }]
    else:
      status = "ambiguous" if len(known_teams) == 1 else (
          "incomplete" if not known_teams else "unmatched")
      differences = []

    report_matches.append(
        _report_match(status, liquipedia_match, None, differences))

  for index, duckdb_match in enumerate(duckdb_matches):
    if index in used_duckdb or index in ambiguous_duckdb:
      continue

    status = "unmatched" if liquipedia_available else "incomplete"
    differences = [] if liquipedia_available else [{
        "field": "liquipediaSource",
        "liquipedia": None,
        "duckdb": duckdb_match.external_id,
        "severity": "incomplete"
    }]

    report_matches.append(
        _report_match(status, None, duckdb_match, differences))

  expected_event = liquipedia_matches[
      0].event_slug if liquipedia_matches else None

  for row in report_matches:
    identity = row.get("liquipediaIdentity")

    if not isinstance(identity, dict):
      continue

    if row.get("statsLinkStatus") not in {
        "available", "matched", "conflict", "ambiguous", "incomplete",
        "unmatched"
    }:
      legacy_status = row.get("status")

      if legacy_status in {"ambiguous", "incomplete", "unmatched", "conflict"}:
        row["statsLinkStatus"] = legacy_status
      else:
        row["statsLinkStatus"] = "matched" if row.get(
            "duckdbIdentity") else "unmatched"

    match_id = identity.get("externalId")
    reasons = []

    if identity.get("provider") != "liquipedia":
      reasons.append("Match provider identity is not Liquipedia.")

    if not isinstance(match_id, str) or not match_id.strip():
      reasons.append("Liquipedia external match ID is missing.")
    elif liquipedia_id_counts.get(match_id) != 1:
      reasons.append("Liquipedia external match ID is duplicated.")

    if (not identity.get("eventSlug")
        or (expected_event and identity.get("eventSlug") != expected_event)):
      reasons.append("Liquipedia event identity is missing or inconsistent.")

    if not identity.get("scheduledAt"):
      reasons.append("Liquipedia scheduledAt is missing or invalid.")

    score_a = identity.get("scoreA")
    score_b = identity.get("scoreB")
    status = identity.get("status")

    if (score_a is None) != (score_b is None):
      reasons.append("Liquipedia series score has only one side.")
    elif score_a is not None and (
        not isinstance(score_a, int) or not isinstance(score_b, int)
        or score_a < 0 or score_b < 0 or
        (status == "completed" and score_a == score_b)):
      reasons.append("Liquipedia series score is contradictory.")
    elif status == "scheduled" and score_a is not None:
      reasons.append(
          "Liquipedia scheduled status conflicts with a series score.")
    elif (status == "completed" and isinstance(score_a, int)
          and isinstance(score_b, int) and not identity.get("forfeitResult")):
      best_of = identity.get("bestOf")

      if isinstance(best_of, int) and best_of > 0:
        wins_required = best_of // 2 + 1
        winner_score, loser_score = sorted((score_a, score_b), reverse=True)

        if winner_score != wins_required or loser_score >= wins_required:
          reasons.append("Liquipedia completed score contradicts bestOf.")

    row["scheduleBlockingReasons"] = reasons

    if reasons:
      row["scheduleStatus"] = "blocked"
    elif any(
        item.get("severity") == "warning"
        for item in row.get("differences", [])):
      row["scheduleStatus"] = "warning"
    else:
      row["scheduleStatus"] = "ready"

  return report_matches
