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
DEFAULT_DB_PATH = REPO_ROOT / "data" / "processed" / "astrion.db"


class ProviderMode(StrEnum):
    """Which `PlanningProvider` implementation backs the agent."""

    DETERMINISTIC = "deterministic"
    REAL = "real"


class AuthMode(StrEnum):
    """How a caller's identity is established.

    `SESSION` is the only mode fit to be reachable by anyone but the developer
    who started the process: a caller presents a session cookie issued by
    `POST /api/auth/login`, and the server resolves the user, organisation and
    role from its own database.

    `DEMO_HEADER` is the original assessment behaviour, kept because the demo
    and the agent test-suite depend on being able to act as a named persona
    without a login. It trusts `X-Astrion-User` completely, so it is
    **authentication in name only** — anyone who can reach the port can claim
    any identity. `Settings.validate_auth` refuses to start in this mode when
    `APP_ENV` names a production environment, and `/health` reports the mode so
    a misconfigured deployment is visible rather than merely wrong.
    """

    SESSION = "session"
    DEMO_HEADER = "demo_header"


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

    # --- authentication ------------------------------------------------------
    #: Secure by default. A deployment must opt *down* to the demo header, and
    #: cannot opt down at all when APP_ENV names production.
    auth_mode: AuthMode = AuthMode.SESSION
    session_cookie_name: str = "astrion_session"
    #: `Secure` on the session cookie. True by default so the cookie is never
    #: sent over plaintext by accident; local http development sets it False
    #: explicitly, which is a decision someone has to make rather than inherit.
    session_cookie_secure: bool = True
    session_cookie_samesite: str = "lax"
    #: Require a verified email address before a password may issue a session.
    require_verified_email: bool = True

    # --- abuse prevention ----------------------------------------------------
    #: Largest body the server will read at all, enforced before parsing. The
    #: 4000-character cap on `message` is a *schema* limit and only applies
    #: after a body has already been buffered.
    max_request_bytes: int = 256 * 1024
    rate_limit_enabled: bool = True
    #: Requests per minute per client for ordinary endpoints.
    rate_limit_per_minute: int = 120
    #: Far tighter, because each one costs a chain of model calls.
    agent_rate_limit_per_minute: int = 15
    #: Tighter still: these are the endpoints an attacker guesses against.
    auth_rate_limit_per_minute: int = 10
    #: Per-organisation ceiling on agent runs, so one tenant cannot exhaust the
    #: model budget shared with every other tenant.
    org_agent_rate_limit_per_minute: int = 60

    # --- browser security ----------------------------------------------------
    security_headers_enabled: bool = True
    #: Emitted only when the deployment is actually served over TLS; sending
    #: HSTS from a plaintext origin pins a scheme the site cannot honour.
    hsts_enabled: bool = False
    hsts_max_age_seconds: int = 63_072_000

    # --- email delivery (Resend) ---------------------------------------------
    #: Never serialised into a response; `repr=False` keeps it out of logs.
    resend_api_key: str | None = Field(default=None, repr=False)
    #: RFC 5322 "Name <address>" form, or just an address.
    email_from: str = "ASTRION <noreply@astrion.app>"
    #: Base URL for verification links — must be the frontend origin.
    #: Example: https://parcelpilot-taupe.vercel.app (Vercel legacy URL during transition)
    email_verification_url: str = "http://localhost:3000"

    @property
    def has_email_provider(self) -> bool:
        """True when Resend is configured and email can actually be sent."""
        return bool(self.resend_api_key)

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

    @property
    def is_production(self) -> bool:
        """Whether this deployment claims to be serving real users.

        Matched loosely on purpose: `prod`, `production` and `live` all mean
        the same thing to an operator, and a control that only recognises one
        spelling of production is a control that silently does not apply.
        """
        return self.app_env.strip().lower() in {"prod", "production", "live"}

    def validate_auth(self) -> None:
        """Refuse a configuration that would serve real users without a login.

        Called at startup alongside `validate_provider`. The demo header is a
        development affordance; reaching production with it enabled would mean
        every authorization control in the system rests on a request header
        anyone can set.
        """
        if self.auth_mode is AuthMode.DEMO_HEADER and self.is_production:
            raise ConfigurationError(
                "AUTH_MODE=demo_header trusts the X-Astrion-User header "
                "without any credential and must never run in production. "
                f"APP_ENV is {self.app_env!r}. Set AUTH_MODE=session."
            )
        if self.is_production and not self.session_cookie_secure:
            raise ConfigurationError(
                "SESSION_COOKIE_SECURE=false would send the session cookie over "
                "plaintext HTTP. It must stay true in production."
            )

    def validate_cors(self) -> None:
        """Reject an origin list that cannot be honoured safely.

        A wildcard origin is incompatible with cookie authentication: the
        browser refuses the combination, so a deployment configured this way
        would appear to work in a curl session and fail for every real user —
        or, worse, invite someone to "fix" it by loosening the cookie instead.
        """
        if "*" in self.cors_allow_origins:
            raise ConfigurationError(
                "CORS_ALLOW_ORIGINS=* cannot be combined with cookie "
                "authentication. List the exact origins the browser app is "
                "served from."
            )
        for origin in self.cors_allow_origins:
            if not origin.startswith(("http://", "https://")):
                raise ConfigurationError(
                    f"CORS origin {origin!r} must include a scheme, "
                    f"e.g. https://app.example.com"
                )
            if self.is_production and origin.startswith("http://"):
                raise ConfigurationError(
                    f"CORS origin {origin!r} is plaintext HTTP and is not "
                    f"permitted in production."
                )

    def public_summary(self) -> dict:
        """Configuration safe to expose on `/health`. No secrets, no paths."""
        return {
            "app_env": self.app_env,
            "provider_mode": self.provider_mode.value,
            "model": self.openai_model if self.uses_real_provider else None,
            "max_tool_steps": self.agent_max_tool_steps,
            "state_changing_actions_enabled": self.enable_state_changing_actions,
            # Reported so an operator can see from the outside that a
            # deployment is running without real authentication.
            "auth_mode": self.auth_mode.value,
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

    raw_auth = (_env("AUTH_MODE", AuthMode.SESSION.value) or "").lower()
    try:
        auth_mode = AuthMode(raw_auth)
    except ValueError as exc:
        options = ", ".join(m.value for m in AuthMode)
        raise ConfigurationError(
            f"AUTH_MODE must be one of: {options}; got {raw_auth!r}"
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
        auth_mode=auth_mode,
        session_cookie_name=_env("SESSION_COOKIE_NAME", "astrion_session")
        or "astrion_session",
        session_cookie_secure=_env_bool("SESSION_COOKIE_SECURE", True),
        session_cookie_samesite=(_env("SESSION_COOKIE_SAMESITE", "lax") or "lax").lower(),
        require_verified_email=_env_bool("REQUIRE_VERIFIED_EMAIL", True),
        max_request_bytes=_env_int("MAX_REQUEST_BYTES", 256 * 1024),
        rate_limit_enabled=_env_bool("RATE_LIMIT_ENABLED", True),
        rate_limit_per_minute=_env_int("RATE_LIMIT_PER_MINUTE", 120),
        agent_rate_limit_per_minute=_env_int("AGENT_RATE_LIMIT_PER_MINUTE", 15),
        auth_rate_limit_per_minute=_env_int("AUTH_RATE_LIMIT_PER_MINUTE", 10),
        org_agent_rate_limit_per_minute=_env_int("ORG_AGENT_RATE_LIMIT_PER_MINUTE", 60),
        security_headers_enabled=_env_bool("SECURITY_HEADERS_ENABLED", True),
        hsts_enabled=_env_bool("HSTS_ENABLED", False),
        hsts_max_age_seconds=_env_int("HSTS_MAX_AGE_SECONDS", 63_072_000),
        resend_api_key=_env("RESEND_API_KEY"),
        email_from=_env("EMAIL_FROM", "ASTRION <noreply@astrion.app>") or "ASTRION <noreply@astrion.app>",
        email_verification_url=_env("EMAIL_VERIFICATION_URL", "http://localhost:3000") or "http://localhost:3000",
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


def email_provider_for(settings: Settings):
    """Return the configured email provider (Resend or null).

    Imported here to keep email-module imports out of the cold startup path
    for deployments that do not configure email.
    """
    from app.backend.email.provider import NullEmailProvider

    if not settings.resend_api_key:
        return NullEmailProvider()

    from app.backend.email.resend_provider import ResendEmailProvider

    return ResendEmailProvider(
        api_key=settings.resend_api_key,
        from_address=settings.email_from,
    )
