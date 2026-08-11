"""Curated knowledge: golden SQL examples and scoped business rules.

    from vanna.integrations.local import LocalExampleStore, LocalInstructionStore
    from vanna.capabilities.knowledge import Instruction, InstructionScope

    examples = LocalExampleStore("./examples.json")
    await examples.add(ctx, "revenue by region", "SELECT region, SUM(amount) ...")

    rules = LocalInstructionStore("./instructions.json")
    await rules.add(ctx, Instruction(
        text="Monetary amounts are stored in cents; divide by 100 to report dollars.",
        scope=InstructionScope.GLOBAL,
    ))
"""

from .classify import is_exploratory, is_worth_saving
from .seed import (
    SEED_TAG,
    generate_seed_examples,
    seed_example_store,
)
from .base import (
    ExampleStore,
    InstructionStore,
    extract_tables,
    validate_sql_syntax,
)
from .models import (
    Example,
    ExampleHit,
    ExampleStatus,
    Instruction,
    InstructionScope,
)

__all__ = [
    "ExampleStore",
    "InstructionStore",
    "Example",
    "ExampleHit",
    "ExampleStatus",
    "Instruction",
    "InstructionScope",
    "extract_tables",
    "is_exploratory",
    "is_worth_saving",
    "generate_seed_examples",
    "seed_example_store",
    "SEED_TAG",
    "validate_sql_syntax",
]
