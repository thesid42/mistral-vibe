from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Self

from pydantic import BaseModel, Field, model_validator

from vibe.core.tools.base import (
    BaseTool,
    BaseToolConfig,
    BaseToolState,
    InvokeContext,
    ToolPermission,
)
from vibe.core.tools.ui import ToolCallDisplay, ToolResultDisplay, ToolUIData
from vibe.core.vibevm import registry as vibevm_registry


class RecallContextArgs(BaseModel):
    query: str = Field(
        default="",
        description=(
            "What to look for in paged-out context (keywords, file names, error text). "
            "Optional when page_id is set."
        ),
    )
    page_id: str | None = Field(
        default=None,
        description="Exact page id like 'P041' from a [vibevm:paged-out] stub",
    )
    full: bool = Field(
        default=False,
        description="Return the complete page instead of focused excerpts",
    )

    @model_validator(mode="after")
    def _require_query_or_page_id(self) -> Self:
        if self.page_id is None and not self.query.strip():
            raise ValueError("Provide query and/or page_id")
        return self


class RecallContextResult(BaseModel):
    status: str
    page_id: str | None = None
    source: str | None = None
    content: str | None = None
    note: str | None = None
    other_matches: list[str] = Field(default_factory=list)


class RecallContextConfig(BaseToolConfig):
    permission: ToolPermission = ToolPermission.ALWAYS


class RecallContext(
    BaseTool[
        RecallContextArgs, RecallContextResult, RecallContextConfig, BaseToolState
    ],
    ToolUIData[RecallContextArgs, RecallContextResult],
):
    @classmethod
    def format_call_display(cls, args: RecallContextArgs) -> ToolCallDisplay:
        message = args.page_id if args.page_id else f"'{args.query}'"
        return ToolCallDisplay(
            summary=f"Recalling {message}",
            verb="Recalling",
            message=message,
            settled_verb="Recalled",
            settled_message=message,
        )

    @classmethod
    def format_result_display(cls, result: RecallContextResult) -> ToolResultDisplay:
        message = result.note or result.source or result.page_id or result.status
        return ToolResultDisplay(
            success=result.status != "unavailable", message=message
        )

    @classmethod
    def get_status_text(cls) -> str:
        return "Recalling paged-out context"

    async def run(
        self, args: RecallContextArgs, ctx: InvokeContext | None = None
    ) -> AsyncGenerator[RecallContextResult, None]:
        vm = vibevm_registry.get(ctx.session_id if ctx else None)
        if vm is None or not vm.enabled:
            yield RecallContextResult(
                status="unavailable", note="VibeVM is not enabled for this session."
            )
            return

        outcome = vm.recall(args.query, args.page_id, full=args.full)
        yield RecallContextResult(
            status=outcome.status,
            page_id=outcome.page_id,
            source=outcome.source_path,
            content=outcome.content,
            note=outcome.note,
            other_matches=outcome.other_matches,
        )
