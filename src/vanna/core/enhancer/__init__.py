"""
LLM context enhancement system for adding context to prompts and messages.

This module provides interfaces for enriching LLM system prompts and messages
with additional context before LLM calls (e.g., from memory, RAG, documentation).
"""

from .base import LlmContextEnhancer
from .budget import (
    AssemblyResult,
    BudgetPolicy,
    Section,
    assemble,
    estimate_tokens,
)
from .default import DefaultLlmContextEnhancer
from .retrieval import RetrievalContextEnhancer

__all__ = [
    "LlmContextEnhancer",
    "DefaultLlmContextEnhancer",
    "RetrievalContextEnhancer",
    "BudgetPolicy",
    "Section",
    "AssemblyResult",
    "assemble",
    "estimate_tokens",
]
