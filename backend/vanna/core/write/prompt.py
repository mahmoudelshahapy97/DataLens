"""Telling the model what it may change, before it proposes something refused.

Without this the boundary is discoverable only by proposing a change and being
turned down. That works -- the refusals are specific and the model can re-plan --
but it spends a turn and shows the user a refusal they did not need to see. A
model that knows up front which two tables are writable simply proposes something
authorizable, or says it cannot.

Wrapping rather than replacing: a deployment already has an enhancer doing schema
and example retrieval, and writes are one more paragraph rather than a reason to
rebuild that. Delegating keeps this composable with whatever is already there.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Optional

from ..enhancer import LlmContextEnhancer, system_prompt_context
from .policy import describe_write_policy

if TYPE_CHECKING:  # pragma: no cover
    from ..user import User

logger = logging.getLogger("vanna.write.prompt")

WRITE_RULES = """
When the user asks for something to be CHANGED -- added, updated, corrected,
deleted, or a business action like ordering, booking, cancelling or refunding --
use propose_write. Answering one of those with a SELECT is a wrong answer, not a
cautious one.

Describe the change as structured steps. You cannot write SQL for a change, and
you do not need to:

- Address rows by primary key only. If you do not know the key, SELECT it first.
- Never assign a generated or computed column.
- Values are data, not expressions. To increase a number, read it and set the
  result.
- expected_row_count is a promise checked inside the transaction. State what you
  believe, not what you hope: if it is wrong the whole change is rolled back.
- To create a row and then reference it, use two steps and take the child's
  foreign key from the parent step.
- Never guess an identifier. Ask.
""".strip()


class WriteAwareEnhancer(LlmContextEnhancer):
    """Appends what this caller may change to the system prompt.

    ``service`` supplies the policy; ``inner`` is the enhancer that would
    otherwise have been used, and runs first so retrieval context comes before
    the write rules.
    """

    def __init__(self, service: Any, *, inner: Optional[LlmContextEnhancer] = None) -> None:
        self.service = service
        self.inner = inner

    async def enhance_system_prompt(
        self, system_prompt: str, user_message: str, user: "User"
    ) -> str:
        if self.inner is not None:
            system_prompt = await self.inner.enhance_system_prompt(
                system_prompt, user_message, user
            )

        try:
            policy = await self.service.build_policy(system_prompt_context(user))
        except Exception as exc:
            # A prompt without the write section still produces an answer; a
            # raised exception produces nothing. The validator remains the
            # thing that actually enforces this, so degrading here is safe.
            logger.debug("Write capability not described: %s", exc)
            return system_prompt

        if policy.is_empty:
            # Say so explicitly. Silence reads as "writes were not mentioned",
            # and the model then proposes one and gets refused.
            return (
                f"{system_prompt}\n\n## Changing data\n\n"
                "Nothing is writable here. If the user asks for something to be "
                "changed, say that you can only read this data and suggest they "
                "ask an administrator for write access."
            )

        return (
            f"{system_prompt}\n\n## Changing data\n\n{WRITE_RULES}\n\n"
            f"You may change these, and nothing else:\n\n"
            f"{describe_write_policy(policy)}"
        )

    async def enhance_user_message(
        self, message: str, user: "User", **kwargs: Any
    ) -> str:
        if self.inner is not None and hasattr(self.inner, "enhance_user_message"):
            return await self.inner.enhance_user_message(message, user, **kwargs)
        return message

    def __getattr__(self, name: str) -> Any:
        """Pass anything else through to the wrapped enhancer.

        Keeps this a drop-in for whatever was there before, so adding writes to
        a deployment does not mean auditing which enhancer methods it relied on.
        """
        inner = self.__dict__.get("inner")
        if inner is None:
            raise AttributeError(name)
        return getattr(inner, name)
