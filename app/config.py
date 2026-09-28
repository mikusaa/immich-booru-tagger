"""Validated configuration; importing the CLI never requires credentials."""
import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

StringList = Annotated[list[str], NoDecode]
LEGACY_LANGUAGE_KEYS = ("ENGLISH_TAGS_ENABLED", "TRANSLATIONS_ENABLED")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", validate_assignment=True,
                                      hide_input_in_errors=True)

    immich_base_url: str
    immich_api_key: str = Field(default="", repr=False)
    immich_api_keys: StringList = Field(default_factory=list, repr=False)
    immich_libraries: Annotated[dict[str, str], NoDecode] = Field(default_factory=dict, repr=False)
    immich_include_library_ids: StringList = Field(default_factory=list)
    immich_include_album_ids: StringList = Field(default_factory=list)
    immich_exclude_library_ids: StringList = Field(default_factory=list)
    search_api: Literal["auto", "structured", "legacy"] = "auto"

    confidence_threshold: float = Field(default=0.35, ge=0, le=1)
    general_threshold: float | None = Field(default=None, ge=0, le=1)
    character_threshold: float = Field(default=0.90, ge=0, le=1)
    batch_size: int = Field(default=250, ge=1, le=1000)
    processed_tag_name: str = "auto:processed"
    failure_timeout: int = Field(default=3, ge=0)
    tagging_model: Literal["wd14", "deepdanbooru"] = "wd14"
    model_repo: str = "SmilingWolf/wd-swinv2-tagger-v3"
    model_cache_dir: Path = Path("models")
    deepdanbooru_project_dir: Path | None = None
    state_dir: Path = Path("state")
    tag_language_mode: Literal["bilingual", "chinese", "english"] = "bilingual"
    translation_file: Path = Path(__file__).resolve().parent.parent / "data/tag_translations.json"
    translation_overrides: Path | None = None
    cleanup_admin_api_key: str = Field(default="", repr=False)
    cleanup_queue_timeout: float = Field(default=300, gt=0)

    max_retries: int = Field(default=3, ge=0)
    retry_delay: float = Field(default=1, gt=0)
    request_timeout: float = Field(default=30, gt=0)
    tag_cache_ttl: int = Field(default=300, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_progress_interval_seconds: int = Field(default=10, ge=1)
    log_slow_operation_seconds: int = Field(default=60, ge=1)
    resume_on_startup: bool = True
    health_port: int = Field(default=8000, ge=1, le=65535)
    enable_scheduler: bool = True
    cron_schedule: str = "0 2 * * *"
    timezone: str = "Asia/Shanghai"
    run_on_startup: bool = False

    @field_validator("immich_api_keys", "immich_include_library_ids",
                     "immich_include_album_ids", "immich_exclude_library_ids", mode="before")
    @classmethod
    def parse_lists(cls, value):
        if isinstance(value, str):
            value = value.strip()
            value = json.loads(value) if value.startswith("[") else value.split(",")
        return list(dict.fromkeys(v.strip() for v in (value or []) if v.strip()))

    @field_validator("immich_libraries", mode="before")
    @classmethod
    def parse_accounts(cls, value):
        if isinstance(value, str):
            return json.loads(value) if value.strip() else {}
        return value or {}

    @field_validator("immich_include_library_ids", "immich_include_album_ids",
                     "immich_exclude_library_ids")
    @classmethod
    def validate_ids(cls, values):
        return list(dict.fromkeys(str(UUID(value)) for value in values))

    @field_validator("immich_base_url")
    @classmethod
    def validate_url(cls, value):
        value = value.rstrip("/")
        if not value.startswith(("http://", "https://")):
            raise ValueError("IMMICH_BASE_URL must start with http:// or https://")
        if value.endswith("/api"):
            raise ValueError("IMMICH_BASE_URL must not include /api")
        return value

    @field_validator("processed_tag_name")
    @classmethod
    def validate_marker(cls, value):
        if not value.strip() or any(c in value for c in "\n\r\t"):
            raise ValueError("PROCESSED_TAG_NAME must be a nonempty tag")
        return value.strip()

    @model_validator(mode="after")
    def validate_credentials(self):
        choices = sum(bool(v) for v in (self.immich_api_key, self.immich_api_keys, self.immich_libraries))
        if choices != 1:
            raise ValueError("Set exactly one of IMMICH_API_KEY, IMMICH_API_KEYS, IMMICH_LIBRARIES")
        if self.immich_libraries and any(not n.strip() or not k.strip() for n, k in self.immich_libraries.items()):
            raise ValueError("Account names and API keys must not be empty")
        return self

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings):
        # Use the configured sources, including custom env files and _env_file=None.
        # Inspect names only: never put credentials or removed values in errors.
        def reject_legacy():
            keys = {str(key).upper() for source in (env_settings.env_vars, dotenv_settings.env_vars,
                                                   init_settings.init_kwargs) for key in source}
            legacy = sorted(keys.intersection(LEGACY_LANGUAGE_KEYS))
            if legacy:
                raise ValueError("已移除旧语言配置 %s，请删除并改用 TAG_LANGUAGE_MODE=bilingual/chinese/english"
                                 % ", ".join(legacy))
            return {}
        return reject_legacy, init_settings, env_settings, dotenv_settings, file_secret_settings

    @property
    def english_tags_enabled(self):
        return self.tag_language_mode in ("bilingual", "english")

    @property
    def translations_enabled(self):
        return self.tag_language_mode in ("bilingual", "chinese")

    @property
    def effective_general_threshold(self):
        return self.general_threshold if self.general_threshold is not None else self.confidence_threshold

    def get_library_config(self):
        if self.immich_libraries:
            return [{"name": name, "api_key": key} for name, key in self.immich_libraries.items()]
        keys = self.immich_api_keys or [self.immich_api_key]
        return [{"name": f"User_{i + 1}", "api_key": key} for i, key in enumerate(keys)]


@lru_cache
def get_settings() -> Settings:
    return Settings()
