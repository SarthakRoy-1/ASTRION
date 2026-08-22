"""Application configuration, read from the environment (Phase 5).

One `Settings` object is built at startup and threaded through the app. Two
properties matter more than the field list:

- **No secret is ever hard-coded, and none is ever echoed back.** The API key
  lives in the environment. `Settings` exposes only whether one is present
  (`has_provider_credentials`), never the value, and `/health` reports the
  provider *mode*, not its configuration.

- **Provider selection is explicit, never inferred.** `LLM_PROVIDER` must say
  `deterministic` or `real`. A missing key while `real` is requested is a
  startup failure, not a quiet downgrade — see
  `ProviderConfigurationError`. Guessing here would mean an operator could
  believe they are running a live model when they are not.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.backend.core.errors import ConfigurationError, ProviderConfigurationError

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "processed" / "parcelpilot.db"


class ProviderMode(StrEnum):
    """Which `PlanningProvider` implementation backs the agent."""

    DETERMINISTIC = "deterministic"
    REAL = "real"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _database_path(raw: str | None) -> Path:
    """Accept either a plain path or the `sqlite:///` URL form in `.env`."""
    if not raw:
        return DEFAULT_DB_PATH
    if raw.startswith("sqlite:///"):
        raw = raw[len("sqlite:///") :]
    elif raw.startswith("sqlite://"):
        raw = raw[len("sqlite://") :]
    path = Path(raw)
    return path if path.is_absolute() else (REPO_ROOT / path).resolve()


class Settings(BaseModel):
    """Resolved runtime configuration. Frozen: nothing reconfigures mid-run."""

    model_config = ConfigDict(frozen=True)

    app_env: str = "development"
    provider_mode: ProviderMode = ProviderMode.DETERMINISTIC

    # Provider credentials. Present only when the real provider is selected;
    # never serialised into a response — see `public_summary`.
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_model: str = "gpt-4o"
    openai_base_url: str | None = None

    agent_max_tool_steps: int = 8
    agent_request_timeout_seconds: float = 60.0
    llm_temperature: float = 0.0

    database_path: Path = DEFAULT_DB_PATH
    cors_allow_origins: tuple[str, ...] = ("http://localhost:3000",)
    enable_state_changing_actions: bool = True

    @property
    def has_provider_credentials(self) -> bool:
        return bool(self.openai_api_key)

    @property
    def uses_real_provider(self) -> bool:
        return self.provider_mode is ProviderMode.REAL

    def validate_provider(self) -> None:
        """Fail loudly when `real` was asked for and cannot be honoured.

        Called at startup so the process refuses to serve rather than serving
        a different agent than the operator configured.
        """
        if not self.uses_real_provider:
            return
        if not self.has_provider_credentials:
            raise ProviderConfigurationError(
                "LLM_PROVIDER=real requires OPENAI_API_KEY to be set. "
                "Set it in the environment, or use LLM_PROVIDER=deterministic "
                "to run without a model."
            )
        if not self.openai_model:
            raise ProviderConfigurationError(
                "LLM_PROVIDER=real requires OPENAI_MODEL to name a chat model."
            )

    def public_summary(self) -> dict:
        """Configuration safe to expose on `/health`. No secrets, no paths."""
        return {
            "app_env": self.app_env,
            "provider_mode": self.provider_mode.value,
            "model": self.openai_model if self.uses_real_provider else None,
            "max_tool_steps": self.agent_max_tool_steps,
            "state_changing_actions_enabled": self.enable_state_changing_actions,
        }


def load_settings(*, env_file: Path | str | None = None) -> Settings:
    """Build `Settings` from the process environment (and optionally a .env).

    Values already present in the environment always win over the file, so a
    deployment's real configuration cannot be shadowed by a checked-out file.
    """
    if env_file is not None:
        _load_env_file(Path(env_file))

    raw_mode = (_env("LLM_PROVIDER", ProviderMode.DETERMINISTIC.value) or "").lower()
    try:
        mode = ProviderMode(raw_mode)
    except ValueError as exc:
        options = ", ".join(m.value for m in ProviderMode)
        raise ConfigurationError(
            f"LLM_PROVIDER must be one of: {options}; got {raw_mode!r}"
        ) from exc

    origins = _env("CORS_ALLOW_ORIGINS", "http://localhost:3000") or ""
    api_key = _env("OPENAI_API_KEY")
    # The shipped template carries a placeholder so the file is runnable; a
    # placeholder is not a credential and must not read as one.
    if api_key and api_key.startswith("sk-replace-me"):
        api_key = None

    return Settings(
        app_env=_env("APP_ENV", "development") or "development",
        provider_mode=mode,
        openai_api_key=api_key,
        openai_model=_env("OPENAI_MODEL", "gpt-4o") or "gpt-4o",
        openai_base_url=_env("OPENAI_BASE_URL"),
        agent_max_tool_steps=_env_int("AGENT_MAX_TOOL_STEPS", 8),
        agent_request_timeout_seconds=_env_float("AGENT_REQUEST_TIMEOUT_SECONDS", 60.0),
        llm_temperature=_env_float("LLM_TEMPERATURE", 0.0),
        database_path=_database_path(_env("DATABASE_URL")),
        cors_allow_origins=tuple(o.strip() for o in origins.split(",") if o.strip()),
        enable_state_changing_actions=_env_bool("ENABLE_STATE_CHANGING_ACTIONS", True),
    )


def _load_env_file(path: Path) -> None:
    """Populate os.environ from a .env file without overriding real values."""
    if not path.exists():
        return
    try:
        from dotenv import load_dotenv
    except ImportError:  # optional convenience only
        return
    load_dotenv(path, override=False)


DEFAULT_ENV_FILE = REPO_ROOT / ".env"
