"""
Error recovery system for handling failures gracefully.

This module provides interfaces for custom error handling, retry logic,
and fallback strategies.
"""

from .base import ErrorRecoveryStrategy
from .models import RecoveryAction, RecoveryActionType
from .sql import (
    NON_RETRYABLE,
    SqlErrorKind,
    SqlRepairStrategy,
    classify_sql_error,
    extract_identifier,
    sanitize_error,
)

__all__ = [
    "ErrorRecoveryStrategy",
    "RecoveryAction",
    "RecoveryActionType",
    "SqlRepairStrategy",
    "SqlErrorKind",
    "classify_sql_error",
    "sanitize_error",
    "extract_identifier",
    "NON_RETRYABLE",
]
