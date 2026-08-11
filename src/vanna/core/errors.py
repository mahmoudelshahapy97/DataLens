"""
Exception classes for the Vanna Agents framework.

This module defines all custom exceptions used throughout the framework.
"""


class AgentError(Exception):
    """Base exception for agent framework."""

    pass


class UserFacingError(Exception):
    """An exception whose message was written for the person who asked.

    The agent catches everything and replaces it with "an unexpected error
    occurred", which is right for a stack trace and wrong for a refusal. A quota
    wall is not a malfunction: the user needs to be told they hit a limit and
    when it resets, and a generic apology instead leaves them retrying something
    that cannot succeed.

    Subclass this only when ``str(exc)`` is safe to show verbatim -- no SQL, no
    connection strings, no internal identifiers.
    """

    pass


class ToolExecutionError(AgentError):
    """Error during tool execution."""

    pass


class ToolNotFoundError(AgentError):
    """Tool not found in registry."""

    pass


class PermissionError(AgentError):
    """User lacks required permissions."""

    pass


class ConversationNotFoundError(AgentError):
    """Conversation not found."""

    pass


class LlmServiceError(AgentError):
    """Error communicating with LLM service."""

    pass


class ValidationError(AgentError):
    """Data validation error."""

    pass


# ----------------------------------------------------------------------
# Phased errors
# ----------------------------------------------------------------------
#
# "It failed" is not a diagnosis. A text-to-SQL request passes through half a
# dozen stages, each failing for entirely different reasons and needing
# entirely different fixes:
#
#   retrieval failed        -> the catalog is stale or the store is unreachable
#   policy check failed     -> the model wrote something unsafe
#   planning failed         -> the SQL references something that isn't there
#   execution failed        -> the database said no
#
# Tagging every error with the phase that produced it turns a two-hour log hunt
# into a two-minute one, and makes "which stage is failing most this week?" a
# groupable metric rather than a guess.

from enum import Enum
from typing import Any, Dict, Optional


class ErrorPhase(str, Enum):
    """Which stage of request handling produced an error."""

    # -- Serving a request ---------------------------------------------
    USER_RESOLUTION = "user_resolution"
    RETRIEVAL = "retrieval"
    PROMPT_ASSEMBLY = "prompt_assembly"
    LLM_REQUEST = "llm_request"
    TOOL_EXECUTION = "tool_execution"
    ACCESS_CONTROL = "access_control"
    """Resolving session properties, or applying row/column-level rules.
    Distinct from USER_RESOLUTION: we know who they are, and are deciding what
    they may read."""
    SQL_POLICY_CHECK = "sql_policy_check"
    SEMANTIC_COMPILE = "semantic_compile"
    """Lowering semantic SQL to dialect SQL. Distinct from SQL_PLANNING, which
    is the database's own planner."""
    SQL_PLANNING = "sql_planning"
    SQL_EXECUTION = "sql_execution"
    RESPONSE_GENERATION = "response_generation"
    VISUALIZATION = "visualization"
    STORAGE = "storage"

    # -- Operating the deployment --------------------------------------
    PROFILE_RESOLUTION = "profile_resolution"
    """Resolving a connection profile, its ``${VAR}`` placeholders, or its
    ``.env`` cascade."""
    PROJECT_LOAD = "project_load"
    """Reading or validating a project directory and its manifest."""
    INDEX_BUILD = "index_build"
    SKILL_DELIVERY = "skill_delivery"
    EVALUATION = "evaluation"


class ErrorCode(str, Enum):
    """Stable identifier for a failure kind.

    Branch and alert on these, never on message text -- messages get reworded,
    codes do not.
    """

    # User-caused: the request was understood and refused.
    INVALID_REQUEST = "invalid_request"
    PERMISSION_DENIED = "permission_denied"
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    POLICY_VIOLATION = "policy_violation"

    # Data-caused: the request was valid but the data layer disagreed.
    INVALID_SQL = "invalid_sql"
    OBJECT_NOT_FOUND = "object_not_found"
    QUERY_TIMEOUT = "query_timeout"
    RESULT_TOO_LARGE = "result_too_large"

    # Definition-caused: the request and the data were fine, what we were told
    # about the data was not.
    INVALID_MANIFEST = "invalid_manifest"
    """A semantic manifest that does not describe a coherent model."""
    COMPILATION_FAILED = "compilation_failed"
    """Valid-looking semantic SQL that could not be lowered to dialect SQL."""
    INVALID_PROJECT = "invalid_project"
    """A project directory that is missing, malformed, or inconsistent."""

    # System-caused: our problem.
    LLM_UNAVAILABLE = "llm_unavailable"
    DATABASE_UNAVAILABLE = "database_unavailable"
    STORE_UNAVAILABLE = "store_unavailable"
    DEPENDENCY_MISSING = "dependency_missing"
    """An optional extra is needed for this path. Its own code because the fix
    is a single pip install, which makes it the most actionable class we have --
    folding it into MISCONFIGURED would bury that."""
    MISCONFIGURED = "misconfigured"
    """Settings that cannot work together, or a required setting absent."""
    INTERNAL_ERROR = "internal_error"
    NOT_IMPLEMENTED = "not_implemented"


#: Metadata keys never returned to a client. They carry query text, which
#: embeds both schema structure and filter values -- the two things an error
#: message is not allowed to disclose. They are still carried, because for a
#: compiler bug they are the entire diagnosis; ``to_dict()`` drops them on the
#: way out and logs keep them.
SENSITIVE_METADATA_KEYS = frozenset(
    {"sql", "semantic_sql", "dialect_sql", "connection_string", "traceback"}
)


class VannaError(AgentError):
    """An error that knows which stage produced it and why.

    ``message`` is user-safe: it must never carry query text, connection
    strings, or data values, because it is logged and often shown. Put
    anything sensitive in ``metadata`` and redact at the boundary.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        phase: Optional[ErrorPhase] = None,
        metadata: Optional[Dict[str, Any]] = None,
        hint: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        self.code = code
        self.phase = phase
        self.metadata = metadata or {}
        self.hint = hint
        super().__init__(message)
        if cause is not None:
            self.__cause__ = cause

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        phase: Optional[ErrorPhase] = None,
        code: Optional[ErrorCode] = None,
        metadata: Optional[Dict[str, Any]] = None,
        hint: Optional[str] = None,
    ) -> "VannaError":
        """Wrap an arbitrary exception, classifying and sanitising it.

        Sanitisation happens *here* rather than at the call sites, because a
        call site that forgets is a call site that writes the failing query into
        a log. Route every driver exception through this method and there is no
        path that skips it.

        When ``code`` is omitted the message is classified with the same
        machinery ``SqlRepairStrategy`` uses, so an error reported over HTTP and
        the same error handled by the retry loop agree on what went wrong.
        """
        if isinstance(exc, VannaError):
            return exc

        # Imported here, not at module scope: `core.recovery.sql` imports
        # ErrorCode from this module, and a top-level import would close the
        # cycle.
        from .recovery.sql import (
            classify_sql_error,
            error_code_for_sql_kind,
            sanitize_error,
        )

        raw = str(exc)
        return cls(
            code or error_code_for_sql_kind(classify_sql_error(raw)),
            sanitize_error(raw),
            phase=phase,
            metadata=metadata,
            hint=hint,
            cause=exc,
        )

    @property
    def is_user_error(self) -> bool:
        """True when the caller can fix this by asking differently.

        Drives the retry decision and the alerting threshold: user errors are
        expected background noise, system errors are a page.
        """
        return self.code in {
            ErrorCode.INVALID_REQUEST,
            ErrorCode.PERMISSION_DENIED,
            ErrorCode.QUOTA_EXCEEDED,
            ErrorCode.RATE_LIMITED,
            ErrorCode.POLICY_VIOLATION,
            ErrorCode.INVALID_SQL,
            ErrorCode.OBJECT_NOT_FOUND,
        }

    @property
    def retryable(self) -> bool:
        """Whether re-issuing a *corrected* request could plausibly succeed.

        Narrower than :attr:`is_user_error`: a permission denial is the caller's
        problem but no rewrite fixes it, and retrying one repeatedly looks like
        an access probe to whoever reads the audit log.
        """
        return self.code in {
            ErrorCode.INVALID_SQL,
            ErrorCode.OBJECT_NOT_FOUND,
            ErrorCode.COMPILATION_FAILED,
            ErrorCode.RESULT_TOO_LARGE,
        }

    def to_dict(self, *, redact: bool = True) -> Dict[str, Any]:
        """Structured form for logs, telemetry, and clients.

        ``redact=True`` (the default) is what every outward-facing transport --
        HTTP, SSE, MCP -- should use; it drops the metadata keys that carry
        query text. Pass ``redact=False`` for logs and explicit debug commands,
        where the reader is already trusted with the schema.

        Uses the raw message rather than ``str(self)`` -- the latter prefixes
        the phase for human readability, which would duplicate the dedicated
        ``phase`` field and make the two disagree if either is filtered.
        """
        metadata = self.metadata
        if redact:
            metadata = {
                k: v for k, v in metadata.items() if k not in SENSITIVE_METADATA_KEYS
            }

        return {
            "code": self.code.value,
            "phase": self.phase.value if self.phase else None,
            "message": self.args[0] if self.args else "",
            "user_error": self.is_user_error,
            "retryable": self.retryable,
            **({"hint": self.hint} if self.hint else {}),
            **({"metadata": metadata} if metadata else {}),
        }

    def __str__(self) -> str:
        base = super().__str__()
        if self.phase:
            return f"[{self.code.value}] {base} phase={self.phase.value}"
        return f"[{self.code.value}] {base}"


def error_envelope(
    exc: BaseException, *, phase: Optional[ErrorPhase] = None
) -> Dict[str, Any]:
    """Redacted ``{"error": {...}}`` payload for any exception.

    The one-liner every transport calls, so an unexpected exception reaches the
    client in the same shape as a deliberate one.
    """
    return {"error": VannaError.from_exception(exc, phase=phase).to_dict()}
