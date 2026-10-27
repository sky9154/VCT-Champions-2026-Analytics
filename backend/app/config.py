from functools import lru_cache
from pathlib import Path
import re
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
EXPECTED_LIQUIPEDIA_API_URL = "https://liquipedia.net/valorant/api.php"
VCT_REFERENCE_DATABASE_RELATIVE_PATH = Path(
    "var/data/vct-reference/vct.duckdb")


class Settings(BaseSettings):
  model_config = SettingsConfigDict(env_file=BACKEND_DIRECTORY / ".env",
                                    env_file_encoding="utf-8",
                                    extra="ignore")

  app_name: str = "VCT Analytics API"
  app_env: str = "development"
  app_host: str = "127.0.0.1"
  app_port: int = Field(default=8591, gt=0, lt=65536)

  mongodb_uri: str = "mongodb://localhost:27017"
  mongodb_database: str = "vct_analytics"
  tournament_slug: str = "valorant-champions-2026"

  liquipedia_api_url: str = EXPECTED_LIQUIPEDIA_API_URL
  liquipedia_page_title: str = "VCT/2026/Champions"
  liquipedia_user_agent: str = ""
  liquipedia_request_interval_seconds: float = Field(default=2, ge=2)
  liquipedia_parse_interval_seconds: float = Field(default=30, ge=30)
  liquipedia_cache_ttl_seconds: int = Field(default=1800, ge=0)

  import_cache_directory: Path = Path("./var/cache")
  import_report_directory: Path = Path("./var/reports")
  import_asset_directory: Path = Path("./var/assets")
  match_time_tolerance_minutes: int = Field(default=15, ge=0)

  auto_update_enabled: bool = False
  auto_update_mode: Literal["interval", "daily_times"] = "interval"
  auto_update_interval_minutes: int = Field(default=30, ge=1, le=1440)
  auto_update_daily_times: str = ""
  auto_update_timezone: str = "Asia/Taipei"
  auto_update_run_on_start: bool = False
  auto_update_max_schedule_writes: int = Field(default=100, gt=0)
  auto_update_max_stats_writes: int = Field(default=1000, gt=0)
  auto_update_max_asset_writes: int = Field(default=50, gt=0)
  auto_update_max_source_decrease_percent: int = Field(default=25, ge=1, le=99)
  auto_update_lock_stale_minutes: int = Field(default=60, ge=1)
  auto_update_backoff_max_minutes: int = Field(default=30, ge=1)

  @field_validator("auto_update_daily_times")
  @classmethod
  def validate_auto_update_daily_times(cls, value: str) -> str:
    normalized = value.strip()

    if not normalized:
      return ""

    times = []

    for item in normalized.split(","):
      candidate = item

      if not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", candidate):
        raise ValueError(
            "AUTO_UPDATE_DAILY_TIMES must contain strict HH:mm values.")

      times.append(candidate)

    return ",".join(sorted(set(times)))

  @field_validator("auto_update_timezone")
  @classmethod
  def validate_auto_update_timezone(cls, value: str) -> str:
    normalized = value.strip()

    try:
      ZoneInfo(normalized)
    except (ZoneInfoNotFoundError, ValueError):
      raise ValueError(
          "AUTO_UPDATE_TIMEZONE must be a valid IANA time zone.") from None

    return normalized

  @model_validator(mode="after")
  def validate_auto_update_schedule(self) -> "Settings":
    if self.auto_update_mode == "daily_times" and not self.auto_update_daily_times:
      raise ValueError(
          "AUTO_UPDATE_DAILY_TIMES is required in daily_times mode.")

    return self

  @property
  def parsed_auto_update_daily_times(self) -> tuple[str, ...]:
    return tuple(self.auto_update_daily_times.split(
        ",")) if self.auto_update_daily_times else ()

  @property
  def auto_update_timezone_info(self) -> ZoneInfo:
    return ZoneInfo(self.auto_update_timezone)

  @field_validator("liquipedia_api_url")
  @classmethod
  def validate_liquipedia_api_url(cls, value: str) -> str:
    if value.rstrip("/") != EXPECTED_LIQUIPEDIA_API_URL:
      raise ValueError(
          "Liquipedia access must use the official MediaWiki API URL.")

    return EXPECTED_LIQUIPEDIA_API_URL

  @field_validator("liquipedia_user_agent")
  @classmethod
  def strip_user_agent(cls, value: str) -> str:
    return value.strip()

  def resolve_backend_path(self, path: Path) -> Path:
    expanded_path = path.expanduser()

    if expanded_path.is_absolute():
      return expanded_path.resolve()

    return (BACKEND_DIRECTORY / expanded_path).resolve()

  @property
  def reference_database_path(self) -> Path:
    return self.resolve_backend_path(VCT_REFERENCE_DATABASE_RELATIVE_PATH)

  @property
  def reference_metadata_path(self) -> Path:
    return self.reference_database_path.parent / "source-metadata.json"

  @property
  def reference_backup_directory(self) -> Path:
    return (BACKEND_DIRECTORY / "var" / "backups" / "vct-reference").resolve()

  @property
  def cache_directory(self) -> Path:
    return self.resolve_backend_path(self.import_cache_directory)

  @property
  def report_directory(self) -> Path:
    return self.resolve_backend_path(self.import_report_directory)

  @property
  def asset_directory(self) -> Path:
    return self.resolve_backend_path(self.import_asset_directory)

  @property
  def runtime_directory(self) -> Path:
    return self.report_directory.parent / "run"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
  return Settings()
