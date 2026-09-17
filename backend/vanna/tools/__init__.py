"""Built-in tool implementations."""

from .propose_write import (
    ConfirmWriteArgs,
    ConfirmWriteTool,
    ProposeWriteTool,
    create_write_tools,
)
from vanna.integrations.plotly import PlotlyChartGenerator
from .column_values import CheckColumnValuesTool, ColumnValuesArgs
from .run_sql import RunSqlTool
from .schema import (
    GetTableSchemaTool,
    SearchTablesTool,
    create_schema_tools,
)
from .system_time import TIME_FUNCTION_NAMES, SystemTimeArgs, SystemTimeTool
from .validate_sql import ValidateSqlArgs, ValidateSqlTool
from .dashboard import (
    ListDashboardsTool,
    SaveDashboardTool,
    create_dashboard_tools,
)
from .visualize_data import VisualizeDataTool
from .calculator import CalculatorArgs, CalculatorTool
from .knowledge import SearchKnowledgeArgs, SearchKnowledgeTool
from .query_history import SearchQueryHistoryArgs, SearchQueryHistoryTool
from .value_dictionary import ListKnownValuesArgs, ListKnownValuesTool
from .core_columns import CheckCoreColumnsArgs, CheckCoreColumnsTool

__all__ = [
    # SQL
    "RunSqlTool",
    "CheckColumnValuesTool",
    "ValidateSqlTool",
    "ValidateSqlArgs",
    # Schema catalog access
    "SearchTablesTool",
    "GetTableSchemaTool",
    "create_schema_tools",
    "ColumnValuesArgs",
    "CheckCoreColumnsArgs",
    "CheckCoreColumnsTool",
    # Determinism / time
    "SystemTimeTool",
    "SystemTimeArgs",
    "TIME_FUNCTION_NAMES",
    # Visualization
    "PlotlyChartGenerator",
    "VisualizeDataTool",
    "SaveDashboardTool",
    "ListDashboardsTool",
    "create_dashboard_tools",
    "ConfirmWriteArgs",
    "ConfirmWriteTool",
    "ProposeWriteTool",
    "create_write_tools",
    # Additional tools over existing services
    "CalculatorArgs",
    "CalculatorTool",
    "SearchKnowledgeArgs",
    "SearchKnowledgeTool",
    "SearchQueryHistoryArgs",
    "SearchQueryHistoryTool",
    "ListKnownValuesArgs",
    "ListKnownValuesTool",
]
