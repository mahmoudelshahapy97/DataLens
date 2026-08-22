"""
Local integration.

This module provides built-in local implementations.
"""

from .audit import LoggingAuditLogger
from .file_system import LocalFileSystem
from .grants import MemoryGrantStore
from .values import MemoryValueStore
from .storage import MemoryConversationStore
from .file_system_conversation_store import FileSystemConversationStore
from .schema_catalog import LocalSchemaCatalog
from .knowledge import LocalExampleStore, LocalInstructionStore
from .markdown_knowledge import (
    MarkdownExampleStore,
    MarkdownInstructionStore,
    slugify,
)

__all__ = [
    "MemoryConversationStore",
    "FileSystemConversationStore",
    "LocalFileSystem",
    "LoggingAuditLogger",
    "MemoryGrantStore",
    "MemoryValueStore",
    "LocalSchemaCatalog",
    "LocalExampleStore",
    "LocalInstructionStore",
    "MarkdownExampleStore",
    "MarkdownInstructionStore",
    "slugify",
]
