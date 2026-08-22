"""
Vanna Agents - A modular framework for building LLM agents.

This package provides a flexible framework for creating conversational AI agents
with tool execution, conversation management, and user scoping.
"""

#: The library's version, and the only place it is written down.
#:
#: It used to be here *and* in pyproject.toml, where it disagreed -- this said
#: 0.1.0 while the package published 2.0.2, and `vanna --version` reported the
#: packaged one because click read it from the installed distribution's metadata.
#: There is no distribution and no metadata now, so this string is what
#: `python -m vanna --version` prints.
#:
#: Deliberately *not* the same number as ``vanna_app.__version__``, which versions
#: the deployed application and is what /health and /docs report. The library and
#: the thing built on it are released on their own schedules. frontend/package.json
#: carries a third for the bundle banner; nothing syncs across the three.
__version__ = "2.0.2"

# Import core framework components
from .core import (
    # Interfaces
    Agent,
    ConversationStore,
    LlmService,
    SystemPromptBuilder,
    Tool,
    UserService,
    T,
    # Models
    Conversation,
    LlmMessage,
    LlmRequest,
    LlmResponse,
    LlmStreamChunk,
    Message,
    ToolCall,
    ToolContext,
    ToolResult,
    ToolSchema,
    User,
    # UI Components
    UiComponent,
    SimpleComponent,
    SimpleComponentType,
    SimpleTextComponent,
    SimpleImageComponent,
    SimpleLinkComponent,
    # Rich Components
    ArtifactComponent,
    BadgeComponent,
    CardComponent,
    DataFrameComponent,
    IconTextComponent,
    LogViewerComponent,
    NotificationComponent,
    ProgressBarComponent,
    ProgressDisplayComponent,
    RichTextComponent,
    StatusCardComponent,
    TaskListComponent,
    # Core implementations
    Agent,
    AgentConfig,
    DefaultSystemPromptBuilder,
    DefaultWorkflowHandler,
    ToolRegistry,
    # Evaluation
    Evaluator,
    TestCase,
    ExpectedOutcome,
    AgentResult,
    EvaluationResult,
    TestCaseResult,
    AgentVariant,
    EvaluationRunner,
    TrajectoryEvaluator,
    OutputEvaluator,
    LLMAsJudgeEvaluator,
    EfficiencyEvaluator,
    EvaluationReport,
    ComparisonReport,
    EvaluationDataset,
    # Exceptions
    AgentError,
    ConversationNotFoundError,
    LlmServiceError,
    PermissionError,
    ToolExecutionError,
    ToolNotFoundError,
    ValidationError,
)

# Import basic implementations
from .integrations import MemoryConversationStore, MockLlmService

# Main exports
__all__ = [
    # Version
    "__version__",
    # Core interfaces
    "Agent",
    "Tool",
    "LlmService",
    "ConversationStore",
    "UserService",
    "SystemPromptBuilder",
    "T",
    # Models
    "User",
    "Message",
    "Conversation",
    "ToolCall",
    "ToolResult",
    "ToolContext",
    "ToolSchema",
    "LlmMessage",
    "LlmRequest",
    "LlmResponse",
    "LlmStreamChunk",
    # UI Components
    "UiComponent",
    "SimpleComponent",
    "SimpleComponentType",
    "SimpleTextComponent",
    "SimpleImageComponent",
    "SimpleLinkComponent",
    # Rich Components
    "ArtifactComponent",
    "BadgeComponent",
    "CardComponent",
    "DataFrameComponent",
    "IconTextComponent",
    "LogViewerComponent",
    "NotificationComponent",
    "ProgressBarComponent",
    "ProgressDisplayComponent",
    "RichTextComponent",
    "StatusCardComponent",
    "TaskListComponent",
    # Core implementations
    "Agent",
    "AgentConfig",
    "ToolRegistry",
    "DefaultSystemPromptBuilder",
    "DefaultWorkflowHandler",
    # Evaluation
    "Evaluator",
    "TestCase",
    "ExpectedOutcome",
    "AgentResult",
    "EvaluationResult",
    "TestCaseResult",
    "AgentVariant",
    "EvaluationRunner",
    "TrajectoryEvaluator",
    "OutputEvaluator",
    "LLMAsJudgeEvaluator",
    "EfficiencyEvaluator",
    "EvaluationReport",
    "ComparisonReport",
    "EvaluationDataset",
    # Basic implementations
    "MemoryConversationStore",
    "MockLlmService",
    # Server components
    "VannaFlaskServer",
    "VannaFastAPIServer",
    "ChatHandler",
    "ChatRequest",
    "ChatStreamChunk",
    "ExampleAgentLoader",
    # Exceptions
    "AgentError",
    "ToolExecutionError",
    "ToolNotFoundError",
    "PermissionError",
    "ConversationNotFoundError",
    "LlmServiceError",
    "ValidationError",
]
