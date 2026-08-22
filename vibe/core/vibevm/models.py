from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class PageState(StrEnum):
    HOT = "hot"  # content present in the model view
    COLD = "cold"  # stubbed out of the view; full content in store
    PINNED = "pinned"  # never evictable


class ContextPage(BaseModel):
    id: str  # "P001", "P002" — per-session counter, stable
    session_id: str
    tool_call_id: (
        str  # binding key to the history tool message (stable across restarts)
    )
    page_type: str  # tool name: "read_file", "bash", "grep", "recall_context", ...
    content: str  # exact original tool-result text
    summary: str  # one-line heuristic summary (for stubs + FTS)
    source_path: str | None = None  # file path for file-backed pages (read_file)
    source_hash: str | None = None  # sha256 of the SOURCE FILE at page creation
    token_count: int  # approx_token_count(content)
    importance: float = 0.5
    access_count: int = 0
    created_seq: int  # index of the tool message in history at registration
    created_at: str  # iso timestamp
    last_accessed: str
    state: PageState = PageState.HOT


class VMStats(BaseModel):
    evictions: int = 0
    page_faults: int = 0  # recalls that hit a COLD page
    hits: int = 0  # recall queries that found >=1 page
    misses: int = 0  # recall queries that found nothing
    stale_recalls: int = 0
    tokens_evicted: int = 0  # cumulative tokens moved out of view
    context_budget: int = (
        0  # last configured budget, for /vm when live config is unavailable
    )


class VMSnapshot(BaseModel):
    pages: list[ContextPage]
    stats: VMStats
    db_path: str


class RecallOutcome(BaseModel):
    status: str
    page_id: str | None = None
    page_type: str | None = None
    source_path: str | None = None
    content: str | None = None
    note: str | None = None
    other_matches: list[str] = Field(default_factory=list)
