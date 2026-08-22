"""
Audit event models.

This module contains data models for audit logging events.
"""

import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from .._compat import StrEnum


class AuditEventType(StrEnum):
    """Types of audit events."""

    # Access control events
    TOOL_ACCESS_CHECK = "tool_access_check"
    UI_FEATURE_ACCESS_CHECK = "ui_feature_access_check"

    # Tool execution events
    TOOL_INVOCATION = "tool_invocation"
    TOOL_RESULT = "tool_result"

    # Conversation events
    MESSAGE_RECEIVED = "message_received"
    AI_RESPONSE_GENERATED = "ai_response_generated"
    CONVERSATION_CREATED = "conversation_created"

    # Security events
    ACCESS_DENIED = "access_denied"
    AUTHENTICATION_ATTEMPT = "authentication_attempt"

    # Write events. Every stage is recorded, including the ones where nothing
    # happened: a proposal that was refused and an approval that was declined
    # are exactly as interesting to whoever reads this later as one that ran.
    WRITE_PROPOSED = "write_proposed"
    WRITE_REFUSED = "write_refused"
    WRITE_APPROVED = "write_approved"
    WRITE_REJECTED = "write_rejected"
    WRITE_EXECUTED = "write_executed"
    WRITE_FAILED = "write_failed"


class AuditEvent(BaseModel):
    """Base audit event with common fields."""

    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: AuditEventType
    timestamp: datetime = Field(default_factory=datetime.utcnow)

    # User context
    user_id: str
    username: Optional[str] = None
    user_email: Optional[str] = None
    user_groups: List[str] = Field(default_factory=list)

    # Request context
    conversation_id: str
    request_id: str
    remote_addr: Optional[str] = None

    # Event-specific data
    details: Dict[str, Any] = Field(default_factory=dict)

    # Privacy/redaction markers
    contains_pii: bool = False
    redacted_fields: List[str] = Field(default_factory=list)


class ToolAccessCheckEvent(AuditEvent):
    """Audit event for tool access permission checks."""

    event_type: AuditEventType = AuditEventType.TOOL_ACCESS_CHECK
    tool_name: str
    access_granted: bool
    required_groups: List[str] = Field(default_factory=list)
    reason: Optional[str] = None


class ToolInvocationEvent(AuditEvent):
    """Audit event for actual tool executions."""

    event_type: AuditEventType = AuditEventType.TOOL_INVOCATION
    tool_call_id: str
    tool_name: str

    # Parameters with sanitization support
    parameters: Dict[str, Any] = Field(default_factory=dict)
    parameters_sanitized: bool = False

    # UI context at invocation time
    ui_features_available: List[str] = Field(default_factory=list)


class ToolResultEvent(AuditEvent):
    """Audit event for tool execution results."""

    event_type: AuditEventType = AuditEventType.TOOL_RESULT
    tool_call_id: str
    tool_name: str
    success: bool
    error: Optional[str] = None
    execution_time_ms: float = 0.0

    # Result metadata (without full content for size)
    result_size_bytes: Optional[int] = None
    ui_component_type: Optional[str] = None


class UiFeatureAccessCheckEvent(AuditEvent):
    """Audit event for UI feature access checks."""

    event_type: AuditEventType = AuditEventType.UI_FEATURE_ACCESS_CHECK
    feature_name: str
    access_granted: bool
    required_groups: List[str] = Field(default_factory=list)


class AiResponseEvent(AuditEvent):
    """Audit event for AI-generated responses."""

    event_type: AuditEventType = AuditEventType.AI_RESPONSE_GENERATED

    # Response metadata
    response_length_chars: int
    response_length_tokens: Optional[int] = None

    # Full text (optional, configurable)
    response_text: Optional[str] = None
    response_hash: str  # SHA256 for integrity verification

    # Model info
    model_name: Optional[str] = None
    temperature: Optional[float] = None

    # Tool calls in response
    tool_calls_count: int = 0
    tool_names: List[str] = Field(default_factory=list)


class WriteEvent(AuditEvent):
    """Audit event for every stage of a controlled write.

    Records the **parameterized** statement and never the bound values. The
    values in a write plan are tenant data -- a customer's address, a price, a
    name -- and an audit log is precisely the place they must not accumulate,
    because it is the store with the longest retention and the widest read
    access. `statement_preview` shows the shape; `plan_hash` proves identity;
    neither reveals content.
    """

    event_type: AuditEventType = AuditEventType.WRITE_PROPOSED

    pending_write_id: str
    operation: str
    tables: List[str] = Field(default_factory=list)

    expected_row_count: int = 0
    rows_affected: Optional[int] = None
    is_destructive: bool = False

    statement_preview: Optional[str] = None
    plan_hash: Optional[str] = None

    requested_by: Optional[str] = None
    decided_by: Optional[str] = None

    #: The refusal code, when this event is a refusal. From the closed
    #: vocabulary in `vanna.core.write.errors`, so alerts can group on it.
    refusal_code: Optional[str] = None
    error: Optional[str] = None
