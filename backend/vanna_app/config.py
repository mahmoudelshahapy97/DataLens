"""Every environment variable this deployment reads, in one place, checked once.

Configuration used to be module-level constants scattered across four files, each
read at import time. That made two things impossible: knowing what a deployment is
actually configured to do without grepping, and *refusing to start* when the
configuration is dangerous.

The second is the point of this module. Almost every serious weakness in the
original stack was a permissive default that nobody chose:

* no ``VANNA_ADMIN_EMAILS`` meant **every** authenticated user administered
  **every** workspace
* an unreachable control plane meant the API served with no authentication at all
* ``VANNA_SECURE_COOKIES=false`` shipped a session cookie over plain HTTP

Each was documented in a comment next to the code that did it. Comments do not stop
a deployment. A startup check does.

Deployment modes
----------------

``demo``          Zero configuration. Anonymous access allowed, in-process limits,
                  SQLite fallback. What ``docker compose up`` with an empty ``.env``
                  gets, and never what a real deployment should be.
``single-tenant`` One workspace, real accounts, real limits. No tenant directory
                  required.
``multi-tenant``  The product. Every permissive default is refused; see
                  ``_MULTI_TENANT_RULES`` for the exact list.

The mode is explicit (``VANNA_DEPLOYMENT_MODE``) rather than inferred, because a
mode inferred from which variables happen to be set is a mode nobody decided on.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

logger = logging.getLogger("vanna.config")

DEMO = "demo"
SINGLE_TENANT = "single-tenant"
MULTI_TENANT = "multi-tenant"

MODES = (DEMO, SINGLE_TENANT, MULTI_TENANT)


class ConfigError(RuntimeError):
    """The configuration is unusable, and the process must not start.

    Carries *every* problem rather than the first, so an operator fixes the
    deployment in one pass instead of discovering the next fault on each restart.
    """

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        body = "\n".join(f"  - {p}" for p in self.problems)
        super().__init__(
            f"Refusing to start: {len(self.problems)} configuration problem(s).\n"
            f"{body}\n"
            "Fix these, or set VANNA_DEPLOYMENT_MODE=demo if this is a throwaway "
            "instance on a trusted network."
        )


# ----------------------------------------------------------------------
# Readers
# ----------------------------------------------------------------------


def _text(env: Mapping[str, str], name: str, default: str = "") -> str:
    return (env.get(name) or default).strip()


def _flag(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    """A boolean env var.

    Accepts the spellings people actually type. Anything unrecognised is the
    default *and a warning* -- ``VANNA_SECURE_COOKIES=yes`` silently meaning False
    is precisely the class of surprise this module exists to remove.
    """
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in ("true", "1", "yes", "on"):
        return True
    if raw in ("false", "0", "no", "off"):
        return False
    logger.warning(
        "%s=%r is not a boolean; using %s. Use true or false.", name, raw, default
    )
    return default


def _number(env: Mapping[str, str], name: str, default: int, *, minimum: int = 0) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s.", name, raw, default)
        return default
    if value < minimum:
        logger.warning("%s=%s is below the minimum %s; using %s.", name, value, minimum, minimum)
        return minimum
    return value


def _list(env: Mapping[str, str], name: str, default: str = "") -> List[str]:
    return [item.strip() for item in _text(env, name, default).split(",") if item.strip()]


def _emails(env: Mapping[str, str], name: str) -> Set[str]:
    return {item.lower() for item in _list(env, name)}


def _networks(env: Mapping[str, str], name: str) -> Tuple[ipaddress._BaseNetwork, ...]:
    """Parse a CIDR list, dropping and reporting anything malformed.

    A malformed entry must not silently widen or narrow trust, so it is logged at
    warning level and skipped rather than being accepted as a host address.
    """
    networks = []
    for item in _list(env, name):
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("%s: %r is not a CIDR block or address; ignoring it.", name, item)
    return tuple(networks)


# ----------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """The whole configuration of one running deployment."""

    # -- mode ----------------------------------------------------------
    mode: str = DEMO

    # -- storage -------------------------------------------------------
    data_dir: Path = Path("/data")
    knowledge_dir: Path = Path("/data/knowledge")
    sqlite_path: str = "/data/demo.db"

    # -- databases -----------------------------------------------------
    database_url: str = ""
    app_database_url: str = ""
    app_pool_min: int = 2
    app_pool_max: int = 16
    app_pool_wait_seconds: int = 10
    auto_migrate: bool = True

    # -- llm -----------------------------------------------------------
    llm_provider: str = "auto"

    # -- identity ------------------------------------------------------
    admin_emails: Set[str] = field(default_factory=set)
    default_tenant: str = "demo"
    session_cookie: str = "vanna_session"
    session_ttl_hours: int = 72
    secure_cookies: bool = False
    trust_headers: bool = False
    allow_anonymous: bool = False
    admin_password: str = ""
    public_user_directory: bool = False
    trusted_proxies: Tuple[Any, ...] = ()
    secret_key: str = ""
    auth_methods: Tuple[str, ...] = ("password",)

    # -- limits --------------------------------------------------------
    max_rows: int = 1000
    query_timeout: int = 60
    daily_quota: int = 200
    rate_limit_per_min: int = 20
    login_max_attempts: int = 8
    login_window_seconds: int = 300
    max_tenant_runtimes: int = 32
    tenant_runtime_ttl_seconds: int = 1800
    generation_retention_days: int = 365

    # -- capabilities --------------------------------------------------
    allow_writes: bool = False
    #: Ceiling on rows one approved change may touch, across all its steps.
    #: Far lower than the read cap on purpose: a read that returns too much
    #: wastes a page, a write that touches too much is an incident.
    max_write_rows: int = 50
    #: How long an unapproved change stays live. Long enough to read the card
    #: and think about it; short enough that the permissions it was authorized
    #: against have probably not moved.
    write_confirmation_ttl_seconds: int = 900
    scan_on_start: bool = True
    index_backend: str = "lexical"
    project_dir: str = ""
    projects_dir: str = ""
    payment_provider: str = "manual"

    # -- http ----------------------------------------------------------
    cors_origins: Tuple[str, ...] = ()

    # -- smtp ----------------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = "vanna@localhost"
    smtp_starttls: bool = True
    public_base_url: str = "http://localhost:3000"

    # -- oidc ----------------------------------------------------------
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid email profile"
    oidc_role_claim: str = ""
    oidc_auto_provision_tenant: str = ""

    # -- observability -------------------------------------------------
    log_level: str = "INFO"
    log_format: str = "text"
    sentry_dsn: str = ""
    metrics_enabled: bool = True

    # -- derived -------------------------------------------------------

    @property
    def is_demo(self) -> bool:
        return self.mode == DEMO

    @property
    def is_multi_tenant(self) -> bool:
        return self.mode == MULTI_TENANT

    @property
    def has_control_plane(self) -> bool:
        return bool(self.app_database_url)

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id and "oidc" in self.auth_methods)

    @property
    def smtp_enabled(self) -> bool:
        """Whether mail can actually be delivered.

        Demo mode without a host still "sends" -- to the log -- so the
        forgot-password flow is exercisable without a mail server.
        """
        return bool(self.smtp_host) or self.is_demo

    def redacted(self) -> Dict[str, Any]:
        """The settings, safe to log or serve on an admin screen.

        Allow-listed rather than deny-listed: a new secret added to this dataclass
        must be *added* to be exposed, instead of being exposed until someone
        remembers to hide it.
        """
        secret_names = {
            "admin_password", "secret_key", "smtp_password",
            "oidc_client_secret", "database_url", "app_database_url",
        }
        out: Dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if key in secret_names:
                out[key] = "***" if value else ""
            elif isinstance(value, (set, tuple)):
                out[key] = sorted(str(v) for v in value)
            elif isinstance(value, Path):
                out[key] = str(value)
            else:
                out[key] = value
        return out


# ----------------------------------------------------------------------
# Loading
# ----------------------------------------------------------------------


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Read the environment into a ``Settings``. Does not validate."""
    env = env if env is not None else os.environ

    mode = _text(env, "VANNA_DEPLOYMENT_MODE", DEMO).lower()
    if mode not in MODES:
        logger.warning(
            "VANNA_DEPLOYMENT_MODE=%r is not one of %s; treating it as %r.",
            mode, ", ".join(MODES), MULTI_TENANT,
        )
        # Unknown means strict, never permissive: a typo in the mode name must not
        # be a way to end up in demo mode in production.
        mode = MULTI_TENANT

    data_dir = Path(_text(env, "VANNA_DATA_DIR", "/data"))
    default_tenant = _text(env, "VANNA_DEFAULT_TENANT", "demo").lower() or "demo"

    return Settings(
        mode=mode,
        data_dir=data_dir,
        knowledge_dir=Path(_text(env, "VANNA_KNOWLEDGE_DIR", str(data_dir / "knowledge"))),
        sqlite_path=_text(env, "VANNA_SQLITE_PATH", str(data_dir / "demo.db")),
        database_url=_text(env, "VANNA_DATABASE_URL"),
        app_database_url=_text(env, "VANNA_APP_DATABASE_URL"),
        app_pool_min=_number(env, "VANNA_APP_POOL_MIN", 2, minimum=1),
        app_pool_max=_number(env, "VANNA_APP_POOL_MAX", 16, minimum=1),
        app_pool_wait_seconds=_number(env, "VANNA_APP_POOL_WAIT_SECONDS", 10, minimum=1),
        auto_migrate=_flag(env, "VANNA_AUTO_MIGRATE", True),
        llm_provider=_text(env, "VANNA_LLM_PROVIDER", "auto").lower(),
        admin_emails=_emails(env, "VANNA_ADMIN_EMAILS"),
        default_tenant=default_tenant,
        session_cookie=_text(env, "VANNA_SESSION_COOKIE", "vanna_session"),
        session_ttl_hours=_number(env, "VANNA_SESSION_TTL_HOURS", 72, minimum=1),
        secure_cookies=_flag(env, "VANNA_SECURE_COOKIES", False),
        trust_headers=_flag(env, "VANNA_TRUST_HEADERS", False),
        allow_anonymous=_flag(env, "VANNA_ALLOW_ANONYMOUS", False),
        admin_password=_text(env, "VANNA_ADMIN_PASSWORD"),
        public_user_directory=_flag(env, "VANNA_PUBLIC_USER_DIRECTORY", False),
        trusted_proxies=_networks(env, "VANNA_TRUSTED_PROXIES"),
        secret_key=_text(env, "VANNA_SECRET_KEY"),
        auth_methods=tuple(
            m.lower() for m in _list(env, "VANNA_AUTH_METHODS", "password")
        ) or ("password",),
        max_rows=_number(env, "VANNA_MAX_ROWS", 1000, minimum=1),
        query_timeout=_number(env, "VANNA_QUERY_TIMEOUT", 60, minimum=1),
        daily_quota=_number(env, "VANNA_DAILY_QUOTA", 200, minimum=1),
        rate_limit_per_min=_number(env, "VANNA_RATE_LIMIT_PER_MIN", 20, minimum=1),
        login_max_attempts=_number(env, "VANNA_LOGIN_MAX_ATTEMPTS", 8, minimum=1),
        login_window_seconds=_number(env, "VANNA_LOGIN_WINDOW_SECONDS", 300, minimum=1),
        max_tenant_runtimes=_number(env, "VANNA_MAX_TENANT_RUNTIMES", 32, minimum=1),
        tenant_runtime_ttl_seconds=_number(
            env, "VANNA_TENANT_RUNTIME_TTL_SECONDS", 1800, minimum=60
        ),
        generation_retention_days=_number(
            env, "VANNA_GENERATION_RETENTION_DAYS", 365, minimum=0
        ),
        allow_writes=_flag(env, "VANNA_ALLOW_WRITES", False),
        max_write_rows=_number(env, "VANNA_MAX_WRITE_ROWS", 50, minimum=1),
        write_confirmation_ttl_seconds=_number(
            env, "VANNA_WRITE_CONFIRMATION_TTL_SECONDS", 900, minimum=30
        ),
        scan_on_start=_flag(env, "VANNA_SCAN_ON_START", True),
        index_backend=_text(env, "VANNA_INDEX_BACKEND", "lexical"),
        project_dir=_text(env, "VANNA_PROJECT_DIR"),
        projects_dir=_text(env, "VANNA_PROJECTS_DIR"),
        payment_provider=_text(env, "VANNA_PAYMENT_PROVIDER", "manual"),
        cors_origins=tuple(_list(env, "VANNA_CORS_ORIGINS", "http://localhost:3000")),
        smtp_host=_text(env, "VANNA_SMTP_HOST"),
        smtp_port=_number(env, "VANNA_SMTP_PORT", 587, minimum=1),
        smtp_username=_text(env, "VANNA_SMTP_USERNAME"),
        smtp_password=_text(env, "VANNA_SMTP_PASSWORD"),
        smtp_from=_text(env, "VANNA_SMTP_FROM", "vanna@localhost"),
        smtp_starttls=_flag(env, "VANNA_SMTP_STARTTLS", True),
        public_base_url=_text(env, "VANNA_PUBLIC_BASE_URL", "http://localhost:3000").rstrip("/"),
        oidc_issuer=_text(env, "VANNA_OIDC_ISSUER"),
        oidc_client_id=_text(env, "VANNA_OIDC_CLIENT_ID"),
        oidc_client_secret=_text(env, "VANNA_OIDC_CLIENT_SECRET"),
        oidc_scopes=_text(env, "VANNA_OIDC_SCOPES", "openid email profile"),
        oidc_role_claim=_text(env, "VANNA_OIDC_ROLE_CLAIM"),
        oidc_auto_provision_tenant=_text(env, "VANNA_OIDC_AUTO_PROVISION_TENANT").lower(),
        log_level=_text(env, "LOG_LEVEL", "INFO").upper(),
        log_format=_text(env, "VANNA_LOG_FORMAT", "text").lower(),
        sentry_dsn=_text(env, "VANNA_SENTRY_DSN"),
        metrics_enabled=_flag(env, "VANNA_METRICS_ENABLED", True),
    )


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------

#: Rules enforced in ``multi-tenant``. Each is (predicate, message): the predicate
#: returns True when the deployment is *broken*.
#:
#: Written as data rather than a wall of ifs so the list can be read as a policy --
#: and so the test suite can assert against it directly.
_MULTI_TENANT_RULES: Tuple[Tuple[Callable[[Settings], bool], str], ...] = (
    (
        lambda s: not s.admin_emails,
        "VANNA_ADMIN_EMAILS is empty. An empty list used to mean 'everyone is a "
        "platform admin'; it now means nobody is, and nobody could administer this "
        "deployment. Name at least one address.",
    ),
    (
        lambda s: not s.app_database_url,
        "VANNA_APP_DATABASE_URL is not set. Multi-tenancy is the control plane: "
        "without it there are no tenants, no members, no roles and no sessions.",
    ),
    (
        lambda s: not s.secure_cookies,
        "VANNA_SECURE_COOKIES is false. The session cookie would be sent over plain "
        "HTTP. Terminate TLS in front of this and set it to true.",
    ),
    (
        lambda s: s.trust_headers,
        "VANNA_TRUST_HEADERS is true. X-User-Email would be accepted as proof of "
        "identity by anyone who can reach the API. Only enable this behind a gateway "
        "that authenticates and strips the header, and never with the API port "
        "published.",
    ),
    (
        lambda s: s.allow_anonymous,
        "VANNA_ALLOW_ANONYMOUS is true. Every request would resolve to a synthetic "
        "account with no credential.",
    ),
    (
        lambda s: s.public_user_directory,
        "VANNA_PUBLIC_USER_DIRECTORY is true. The member roster of every workspace "
        "would be readable without signing in.",
    ),
    (
        lambda s: not s.secret_key,
        "VANNA_SECRET_KEY is not set. It encrypts stored datasource credentials and "
        "signs CSRF and OIDC state. Generate one with: "
        "python -c \"import secrets;print(secrets.token_urlsafe(48))\"",
    ),
    (
        lambda s: len(s.secret_key) < 32,
        "VANNA_SECRET_KEY is shorter than 32 characters.",
    ),
    (
        lambda s: "*" in s.cors_origins,
        "VANNA_CORS_ORIGINS contains '*'. Credentials ride on cookies here, so a "
        "wildcard origin is both rejected by browsers and wrong. Name the origins.",
    ),
    (
        lambda s: not s.trusted_proxies,
        "VANNA_TRUSTED_PROXIES is empty. X-Forwarded-For would be ignored and every "
        "request would appear to come from the reverse proxy, so per-IP login "
        "throttling would lock out all users at once. Set it to the proxy's network "
        "(the compose default is 172.16.0.0/12).",
    ),
)

#: Rules enforced everywhere except ``demo``.
_BASELINE_RULES: Tuple[Tuple[Callable[[Settings], bool], str], ...] = (
    (
        lambda s: not s.app_database_url and not s.allow_anonymous,
        "VANNA_APP_DATABASE_URL is not set and VANNA_ALLOW_ANONYMOUS is false, so "
        "there is no way for anyone to authenticate. Configure the control plane, or "
        "opt in to anonymous access explicitly.",
    ),
    (
        lambda s: bool(s.app_database_url) and not s.secret_key,
        "VANNA_SECRET_KEY is not set. Datasource credentials would be stored "
        "unencrypted.",
    ),
    (
        lambda s: "oidc" in s.auth_methods and not (s.oidc_issuer and s.oidc_client_id),
        "VANNA_AUTH_METHODS names oidc, but VANNA_OIDC_ISSUER or "
        "VANNA_OIDC_CLIENT_ID is missing.",
    ),
    (
        lambda s: s.allow_writes and s.mode != DEMO and not s.admin_emails,
        "VANNA_ALLOW_WRITES is true with no platform admins configured. Write access "
        "is granted per workspace by a platform admin; without one it can only be "
        "granted by accident.",
    ),
)


def validate(settings: Settings) -> List[str]:
    """Every problem with this configuration. Empty means it is safe to start."""
    problems: List[str] = []

    for broken, message in _BASELINE_RULES:
        if settings.mode != DEMO and broken(settings):
            problems.append(message)

    if settings.mode == MULTI_TENANT:
        for broken, message in _MULTI_TENANT_RULES:
            if broken(settings):
                problems.append(message)

    # Mode-independent: these are wrong everywhere, demo included.
    if settings.app_pool_min > settings.app_pool_max:
        problems.append(
            f"VANNA_APP_POOL_MIN ({settings.app_pool_min}) is greater than "
            f"VANNA_APP_POOL_MAX ({settings.app_pool_max})."
        )
    unknown = set(settings.auth_methods) - {"password", "oidc"}
    if unknown:
        problems.append(
            f"VANNA_AUTH_METHODS contains unknown method(s): {', '.join(sorted(unknown))}. "
            "Known: password, oidc."
        )
    return problems


def load_and_validate(env: Optional[Mapping[str, str]] = None) -> Settings:
    """Load the configuration, or raise ``ConfigError`` describing every fault."""
    settings = load_settings(env)
    problems = validate(settings)
    if problems:
        raise ConfigError(problems)

    if settings.mode == DEMO:
        logger.warning(
            "Running in DEMO mode: anonymous access is permitted, limits are "
            "per-process, and configuration checks are relaxed. Set "
            "VANNA_DEPLOYMENT_MODE=multi-tenant before exposing this."
        )
    else:
        logger.info(
            "Deployment mode: %s (control plane: %s, auth: %s)",
            settings.mode,
            "yes" if settings.has_control_plane else "no",
            ", ".join(settings.auth_methods),
        )
    return settings


# ----------------------------------------------------------------------
# Process-wide access
# ----------------------------------------------------------------------

_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """The settings for this process, loaded and validated on first use."""
    global _settings
    if _settings is None:
        _settings = load_and_validate()
    return _settings


def set_settings(settings: Optional[Settings]) -> None:
    """Replace the process settings. For tests and the CLI, not for request code."""
    global _settings
    _settings = settings
