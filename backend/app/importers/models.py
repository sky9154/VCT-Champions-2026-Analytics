from datetime import datetime, timezone
from typing import Literal, overload

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


def _to_camel(value: str) -> str:
  first, *rest = value.split("_")

  return first + "".join(part.capitalize() for part in rest)


@overload
def _normalize_utc(value: datetime) -> datetime:
  ...


@overload
def _normalize_utc(value: None) -> None:
  ...


def _normalize_utc(value: datetime | None) -> datetime | None:
  if value is None:
    return None

  if value.tzinfo is None or value.utcoffset() is None:
    raise ValueError("Datetime values must include a timezone.")

  return value.astimezone(timezone.utc)


class DTOModel(BaseModel):
  model_config = ConfigDict(alias_generator=_to_camel,
                            populate_by_name=True,
                            extra="forbid")

  @field_serializer("*", when_used="json")
  def _serialize_utc_datetime(self, value):
    if isinstance(value, datetime):
      normalized = _normalize_utc(value)

      return normalized.isoformat().replace("+00:00", "Z")

    return value


class SourceReference(DTOModel):
  provider: str
  external_id: str | None = None
  url: str | None = None


class TeamDTO(DTOModel):
  name: str | None
  external_id: str | None = None


class TeamReference(DTOModel):
  name: str | None
  provider: str | None = None
  external_id: str | None = None
  source_url: str | None = None


class MatchDTO(DTOModel):
  provider: str
  external_id: str | None
  event_slug: str
  event_name: str | None
  event_external_id: str | None = None
  team_a: TeamDTO
  team_b: TeamDTO
  scheduled_at: datetime | None
  stage: str | None
  group_name: str | None
  round_name: str | None
  status: str | None
  score_a: int | None
  score_b: int | None
  best_of: int | None
  winner: str | None = None
  forfeit_result: bool = False
  source_url: str | None
  source_timestamp: datetime | None = None
  stats_available: bool = False
  stats_map_count: int = 0
  stats_player_rows: int = 0
  warnings: list[str] = Field(default_factory=list)

  @field_validator("scheduled_at", "source_timestamp")
  @classmethod
  def require_utc_datetime(cls, value: datetime | None) -> datetime | None:
    return _normalize_utc(value)


class ProviderSnapshot(DTOModel):
  provider: str
  source_url: str
  fetched_at: datetime | None
  cache_status: str
  revision: str | None = None
  source_timestamp: datetime | None = None
  content_sha256: str | None = None
  warnings: list[str] = Field(default_factory=list)

  @field_validator("fetched_at", "source_timestamp")
  @classmethod
  def require_utc_datetime(cls, value: datetime | None) -> datetime | None:
    return _normalize_utc(value)


class LiquipediaSnapshot(ProviderSnapshot):
  page_title: str


class DuckDbSnapshot(ProviderSnapshot):
  database_path: str
  read_only: bool = True


class MatchReference(DTOModel):
  provider: str
  external_id: str | None
  source: SourceReference | None = None
  event_slug: str
  event_external_id: str | None = None
  teams: list[TeamReference]
  scheduled_at: datetime | None
  stage: str | None
  group_name: str | None
  round_name: str | None
  status: str | None
  score_a: int | None
  score_b: int | None
  best_of: int | None
  winner: str | None = None
  forfeit_result: bool = False
  source_url: str | None

  @field_validator("scheduled_at")
  @classmethod
  def require_utc_datetime(cls, value: datetime | None) -> datetime | None:
    return _normalize_utc(value)


class FieldDifference(DTOModel):
  field: str
  liquipedia: str | int | float | bool | None | list[str | int | float | bool
                                                     | None]
  duckdb: str | int | float | bool | None | list[str | int | float | bool
                                                 | None]
  severity: Literal["conflict", "warning", "liquipedia_newer", "incomplete",
                    "ambiguous"]
  priority: Literal["high", "normal"] | None = None


class ValueComparison(DTOModel):
  liquipedia: str | int | list[int | None] | None
  duckdb: str | int | list[int | None] | None


class TeamOrderComparison(DTOModel):
  liquipedia: list[TeamReference] | None
  duckdb: list[TeamReference] | None
  team_order_reversed: bool


class ExternalIdComparison(DTOModel):
  liquipedia: str | None
  duckdb: str | None
  liquipedia_event: str | None
  duckdb_event: str | None
  liquipedia_teams: list[str | None] | None
  duckdb_teams: list[str | None] | None


class StatsAvailability(DTOModel):
  available: bool
  complete_maps: int
  complete_player_rows: int


class MatchReconciliation(DTOModel):
  status: Literal["matched", "liquipedia_newer", "duckdb_stats_available",
                  "conflict", "incomplete", "unmatched", "ambiguous"]
  schedule_status: Literal["ready", "warning", "blocked", "not_applicable"]
  stats_link_status: Literal["available", "matched", "conflict", "ambiguous",
                             "incomplete", "unmatched"]
  schedule_blocking_reasons: list[str] | None = None
  liquipedia_identity: MatchReference | None
  duckdb_identity: MatchReference | None
  teams: TeamOrderComparison
  time: ValueComparison
  stage: ValueComparison
  round: ValueComparison
  group: ValueComparison
  best_of: ValueComparison
  scores: ValueComparison
  winner: ValueComparison
  differences: list[FieldDifference]
  source_urls: ValueComparison
  external_ids: ExternalIdComparison
  stats_availability: StatsAvailability
  suggested_action: str
  candidate_duckdb_identities: list[MatchReference] | None = None


class ReconciliationSummary(DTOModel):
  liquipedia_matches: int
  duckdb_matches: int
  matched: int
  liquipedia_newer: int
  stats_available: int
  incomplete: int
  conflicts: int
  ambiguous: int
  unmatched: int
  schedule_ready: int
  schedule_warning: int
  schedule_blocked: int
  stats_matched: int
  stats_conflict: int
  stats_unavailable: int
  stats_ambiguous: int
  stats_incomplete: int
  stats_unmatched: int


class EventReference(DTOModel):
  slug: str
  liquipedia_page: str


class SourceSummary(DTOModel):
  provider: str
  url: str
  status: str
  fetched_at: datetime | None = None
  cache_status: str
  revision: str | None = None
  source_timestamp: datetime | None = None
  content_sha256: str | None = None
  attribution: str | None = None
  database_path: str | None = None
  read_only: bool | None = None

  @field_validator("fetched_at", "source_timestamp")
  @classmethod
  def require_utc_datetime(cls, value: datetime | None) -> datetime | None:
    return _normalize_utc(value)


class ReportMessage(DTOModel):
  provider: str | None = None
  event: str | None = None
  match_identity: str | None = None
  message: str
  source_url: str | None = None


class ReconciliationReport(DTOModel):
  generated_at: datetime
  event: EventReference
  sources: dict[str, SourceSummary]
  summary: ReconciliationSummary
  matches: list[MatchReconciliation]
  warnings: list[ReportMessage]
  errors: list[ReportMessage]

  @field_validator("generated_at")
  @classmethod
  def require_utc_datetime(cls, value: datetime) -> datetime:
    return _normalize_utc(value)


class ImportRunError(Exception):
  pass
