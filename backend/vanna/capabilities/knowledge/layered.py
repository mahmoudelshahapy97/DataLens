"""A deployment-wide baseline of rules, layered over each workspace's own.

Two kinds of rule reach the prompt and they are owned by different people. A
workspace's rules are its own: its analysts write them, edit them and delete
them. The baseline belongs to whoever runs the deployment -- "never reference a
column that is not in the schema" is not a business preference, and a workspace
that can delete it can quietly disable the thing keeping its answers honest.

**The baseline is never copied into a workspace.** That is the design decision
everything else follows from. Copying is the obvious implementation and it is
wrong in two ways: editing a platform rule would leave a stale duplicate in
every workspace that had already taken it, and "a tenant deleted a platform
rule" would become a thing a guard has to prevent rather than a thing that
cannot happen. Here the baseline lives outside tenant storage entirely and is
merged at read time, so a workspace has nothing to delete.

What a workspace *can* do, when the rule says so, is switch one off. That is
deliberately per-rule and closed by default -- see :class:`LayeredInstructionStore`.
"""

from __future__ import annotations

import logging
from typing import (
    TYPE_CHECKING,
    Callable,
    List,
    Optional,
    Protocol,
    Sequence,
    Set,
)

from .base import InstructionStore
from .models import Instruction, InstructionScope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from vanna.core.tool import ToolContext

logger = logging.getLogger(__name__)


class LockedInstructionError(PermissionError):
    """A workspace tried to change a rule the platform owns.

    Deliberately not ``return False``. The stores signal "no such rule" that
    way and the routes render it as a 404, which here would be a lie: the rule
    exists, it applies, and it is being enforced. Callers map this to 409.
    """

    def __init__(self, instruction_id: str, reason: str = "locked") -> None:
        self.instruction_id = instruction_id
        self.reason = reason
        super().__init__(
            f"instruction {instruction_id!r} is owned by the platform ({reason})"
        )


class BaselineOverrideStore(Protocol):
    """Records which baseline rules one workspace has switched off.

    Only the exceptions are stored. Absence means "on", which is what lets a
    newly shipped baseline rule apply everywhere the moment it deploys instead
    of needing a row written per workspace first.
    """

    async def disabled_ids(self, context: "ToolContext") -> Set[str]:
        ...

    async def set_disabled(
        self, context: "ToolContext", baseline_id: str, disabled: bool
    ) -> None:
        ...


class MemoryBaselineOverrideStore:
    """In-process overrides. For tests and single-process demos.

    Scoped per tenant like every other store here, because an override that
    leaked between workspaces would silently switch a safety rule off for
    somebody who never asked.
    """

    def __init__(self) -> None:
        self._by_tenant: dict = {}

    @staticmethod
    def _tenant(context: "ToolContext") -> str:
        from vanna.capabilities.agent_memory.scoping import tenant_scope

        return tenant_scope(context)

    async def disabled_ids(self, context: "ToolContext") -> Set[str]:
        return set(self._by_tenant.get(self._tenant(context), set()))

    async def set_disabled(
        self, context: "ToolContext", baseline_id: str, disabled: bool
    ) -> None:
        held = self._by_tenant.setdefault(self._tenant(context), set())
        if disabled:
            held.add(baseline_id)
        else:
            held.discard(baseline_id)


class LayeredInstructionStore(InstructionStore):
    """Platform baseline rules on top of one workspace's own.

    Reads merge the two; writes only ever reach the workspace's store. A
    baseline rule cannot be added over, edited or deleted, and can be switched
    off only when the platform marked it disableable.

    **Why disabling is per-rule, and closed by default.** Treating every
    baseline rule the same is the mistake. A safety rule -- *never reference a
    column absent from the schema* -- has to be non-negotiable or "locked" is
    decoration. A stylistic one -- *prefer an explicit column list to*
    ``SELECT *`` -- genuinely conflicts with how some teams work, and refusing
    to let them turn it off produces the worse outcome rather than the stricter
    one: the admin writes a workspace rule contradicting it, and the model now
    sees two rules that disagree and picks one. :class:`Instruction` does not
    detect contradictions and its own docstring says so.

    Args:
        tenant_store: Where the workspace's own rules live.
        baseline: Called on every read, not captured once, so reloading the
            baseline reaches running workspaces without a restart.
        disableable_ids: Which baseline ids a workspace may switch off.
        overrides: Where those choices are recorded. Without one, nothing is
            disableable -- a choice that cannot be stored must not be offered.
    """

    def __init__(
        self,
        tenant_store: InstructionStore,
        *,
        baseline: Callable[[], Sequence[Instruction]],
        disableable_ids: Callable[[], Set[str]] = lambda: set(),
        overrides: Optional[BaselineOverrideStore] = None,
    ) -> None:
        self.tenant_store = tenant_store
        self._baseline = baseline
        self._disableable_ids = disableable_ids
        self.overrides = overrides

    # -- helpers -------------------------------------------------------

    def _baseline_by_id(self) -> dict:
        return {i.id: i for i in self._baseline()}

    async def _disabled(self, context: "ToolContext") -> Set[str]:
        if self.overrides is None:
            return set()
        try:
            return await self.overrides.disabled_ids(context)
        except Exception as exc:
            # A baseline rule staying on is the safe direction, so an override
            # backend that is down must not switch safety rules off.
            logger.warning("Could not read baseline overrides: %s", exc)
            return set()

    # -- reads ---------------------------------------------------------

    async def resolve(
        self,
        context: "ToolContext",
        *,
        data_source_id: Optional[str] = None,
        tables: Optional[List[str]] = None,
    ) -> List[Instruction]:
        own = await self.tenant_store.resolve(
            context, data_source_id=data_source_id, tables=tables
        )

        disabled = await self._disabled(context)
        groups = list(getattr(getattr(context, "user", None), "group_memberships", None) or [])
        baseline = [
            rule
            for rule in self._baseline()
            if rule.id not in disabled
            and rule.applies_to(
                data_source_id=data_source_id, tables=tables, user_groups=groups
            )
        ]

        merged = [*baseline, *own]
        # Same ordering the enhancer and the token budget assume: priority
        # first, then a stable tiebreak so the prompt prefix does not churn
        # between requests and defeat prompt caching.
        merged.sort(key=lambda i: (-i.priority, i.id))
        return merged

    async def list_all(self, context: "ToolContext") -> List[Instruction]:
        disabled = await self._disabled(context)
        baseline = [
            rule.model_copy(update={"enabled": rule.id not in disabled})
            for rule in self._baseline()
        ]
        own = await self.tenant_store.list_all(context)
        return [*baseline, *own]

    # -- writes --------------------------------------------------------

    async def add(
        self, context: "ToolContext", instruction: Instruction
    ) -> Instruction:
        if instruction.id in self._baseline_by_id():
            raise ValueError(
                f"{instruction.id!r} is a platform rule id; a workspace rule "
                "cannot claim it"
            )
        return await self.tenant_store.add(context, instruction)

    async def update(
        self,
        context: "ToolContext",
        instruction_id: str,
        *,
        text: Optional[str] = None,
        scope: Optional[InstructionScope] = None,
        scope_ref: Optional[str] = None,
        priority: Optional[int] = None,
        enabled: Optional[bool] = None,
    ) -> Optional[Instruction]:
        if instruction_id in self._baseline_by_id():
            raise LockedInstructionError(instruction_id, "locked")
        return await self.tenant_store.update(
            context,
            instruction_id,
            text=text,
            scope=scope,
            scope_ref=scope_ref,
            priority=priority,
            enabled=enabled,
        )

    async def set_enabled(
        self, context: "ToolContext", instruction_id: str, enabled: bool
    ) -> bool:
        if instruction_id not in self._baseline_by_id():
            return await self.tenant_store.set_enabled(
                context, instruction_id, enabled
            )

        if self.overrides is None or instruction_id not in self._disableable_ids():
            raise LockedInstructionError(instruction_id, "not_disableable")

        await self.overrides.set_disabled(context, instruction_id, not enabled)
        return True

    async def delete(self, context: "ToolContext", instruction_id: str) -> bool:
        if instruction_id in self._baseline_by_id():
            raise LockedInstructionError(instruction_id, "locked")
        return await self.tenant_store.delete(context, instruction_id)
