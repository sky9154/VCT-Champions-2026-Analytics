import re
import unicodedata

from app.importers.models import MatchDTO

TEAM_NAME_ALIASES = {
    "100t": "100thieves",
    "edg": "edwardgaming",
    "g2": "g2esports",
    "jdg": "jdgaming",
    "kc": "karminecorp",
    "prx": "paperrex",
    "tl": "teamliquid",
    "xlg": "xilaigaming",
    "xlgesports": "xilaigaming"
}
STAGE_ALIASES = {
    "group": "group-stage",
    "groupstage": "group-stage",
    "group-stage": "group-stage",
    "playoff": "playoffs",
    "playoffs": "playoffs",
    "knockout": "playoffs"
}
STATUS_ALIASES = {
    "complete": "completed",
    "completed": "completed",
    "finished": "completed",
    "final": "completed",
    "live": "live",
    "ongoing": "live",
    "scheduled": "scheduled",
    "upcoming": "scheduled",
    "postponed": "postponed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "tbd": "tbd",
    "to be determined": "tbd"
}
ROUND_ALIASES = {
    "opening": "opening",
    "opening match": "opening",
    "opening matches": "opening",
    "winner": "winners",
    "winners": "winners",
    "winner s": "winners",
    "winner match": "winners",
    "winners match": "winners",
    "winner s match": "winners",
    "elimination": "elimination",
    "elimination match": "elimination",
    "decider": "decider",
    "decider match": "decider",
    "upper quarterfinal": "upper-quarterfinals",
    "upper quarterfinals": "upper-quarterfinals",
    "upper bracket quarterfinal": "upper-quarterfinals",
    "upper bracket quarterfinals": "upper-quarterfinals",
    "ub quarterfinals": "upper-quarterfinals",
    "ubqf": "upper-quarterfinals",
    "upper semifinal": "upper-semifinals",
    "upper semifinals": "upper-semifinals",
    "upper bracket semifinal": "upper-semifinals",
    "upper bracket semifinals": "upper-semifinals",
    "ub semifinals": "upper-semifinals",
    "ubsf": "upper-semifinals",
    "upper final": "upper-final",
    "upper bracket final": "upper-final",
    "ub final": "upper-final",
    "ubf": "upper-final",
    "lower round 1": "lower-round-1",
    "lower bracket round 1": "lower-round-1",
    "lb round 1": "lower-round-1",
    "lbr1": "lower-round-1",
    "lower round 2": "lower-round-2",
    "lower bracket round 2": "lower-round-2",
    "lower quarterfinal": "lower-round-2",
    "lower quarterfinals": "lower-round-2",
    "lower bracket quarterfinal": "lower-round-2",
    "lower bracket quarterfinals": "lower-round-2",
    "lb round 2": "lower-round-2",
    "lbr2": "lower-round-2",
    "lower round 3": "lower-round-3",
    "lower bracket round 3": "lower-round-3",
    "lower semifinal": "lower-round-3",
    "lower semifinals": "lower-round-3",
    "lower bracket semifinal": "lower-round-3",
    "lower bracket semifinals": "lower-round-3",
    "lb round 3": "lower-round-3",
    "lbr3": "lower-round-3",
    "lower final": "lower-final",
    "lower bracket final": "lower-final",
    "lb final": "lower-final",
    "lbf": "lower-final",
    "grand final": "grand-final"
}


def _normalize_words(value: str | None) -> str | None:
  if value is None:
    return None

  decomposed = unicodedata.normalize("NFKD", value).casefold()
  ascii_text = "".join(char for char in decomposed
                       if not unicodedata.combining(char))
  normalized = re.sub(r"[^a-z0-9]+", " ", ascii_text).strip()

  return normalized or None


def normalize_team_name(value: str | None) -> str | None:
  normalized = _normalize_words(value)

  if normalized is None:
    return None

  compact = normalized.replace(" ", "")

  return TEAM_NAME_ALIASES.get(compact, compact)


def normalize_stage(value: str | None) -> str | None:
  normalized = _normalize_words(value)

  if normalized is None:
    return None

  compact = normalized.replace(" ", "-")

  return STAGE_ALIASES.get(compact.replace("-", ""),
                           STAGE_ALIASES.get(compact, compact))


def normalize_round(value: str | None) -> str | None:
  if value is None:
    return None

  without_group = re.sub(r"\(\s*(?:group\s+)?[a-z]\s*\)",
                         " ",
                         value,
                         flags=re.IGNORECASE)
  normalized = _normalize_words(without_group)

  if normalized is None:
    return None

  return ROUND_ALIASES.get(normalized, normalized.replace(" ", "-"))


def normalize_status(value: str | None) -> str | None:
  normalized = _normalize_words(value)

  if normalized is None:
    return None

  return STATUS_ALIASES.get(normalized, normalized.replace(" ", "-"))


def validate_match(match: MatchDTO) -> list[str]:
  warnings = list(dict.fromkeys(match.warnings))

  if match.team_a.name is None and match.team_b.name is None:
    warnings.append("Both participants are missing or unresolved.")

  if match.scheduled_at is None:
    warnings.append("Scheduled time is missing or has no explicit timezone.")

  if match.stage is None:
    warnings.append("Stage is missing.")

  if match.round_name is None:
    warnings.append("Round is missing.")

  if match.status is None:
    warnings.append("Match status is missing or unresolved.")

  if (match.score_a is None) != (match.score_b is None):
    warnings.append("Only one series score is present.")

  if any(score is not None and score < 0
         for score in (match.score_a, match.score_b)):
    warnings.append("Series scores cannot be negative.")

  if (match.status == "completed"
      and (match.score_a is None or match.score_b is None)
      and not (match.forfeit_result
               and match.winner in {match.team_a.name, match.team_b.name})):
    warnings.append("Completed match is missing a series score.")

  return list(dict.fromkeys(warnings))
