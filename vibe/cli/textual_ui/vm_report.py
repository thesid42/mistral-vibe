from __future__ import annotations

from pathlib import Path

from vibe.core.vibevm.models import ContextPage, PageState, VMSnapshot, VMStats

_THOUSAND = 1_000
_STATE_ORDER: dict[PageState, int] = {
    PageState.PINNED: 0,
    PageState.HOT: 1,
    PageState.COLD: 2,
}


def format_vm_report(
    snapshot: VMSnapshot, *, budget: int | None, context_tokens: int | None
) -> str:
    """Render a VibeVM snapshot as GFM markdown for ``UserCommandMessage``."""
    heading = _format_heading(context_tokens, budget)
    if not snapshot.pages:
        return f"{heading}\n\nNo pages tracked yet for this session.\n"
    return (
        f"{heading}\n\n{_format_table(snapshot.pages)}\n\n"
        f"{_format_stats_line(snapshot.stats)}\n"
    )


def _format_heading(context_tokens: int | None, budget: int | None) -> str:
    if context_tokens is None:
        return "## VibeVM"
    in_view = f"## VibeVM — {_format_k(context_tokens)} in view"
    if budget is None:
        return in_view
    return f"{in_view} / {_format_k(budget)} budget"


def _format_k(tokens: int) -> str:
    if tokens < _THOUSAND:
        return str(tokens)
    value = f"{tokens / _THOUSAND:.1f}"
    if value.endswith(".0"):
        value = value[:-2]
    return f"{value}k"


def _format_table(pages: list[ContextPage]) -> str:
    rows = [
        "| Page | Type | State | Tokens | Acc | Source |",
        "|------|------|-------|--------|-----|--------|",
    ]
    for page in sorted(pages, key=_sort_key):
        rows.append(
            f"| {page.id} | {page.page_type} | {page.state.value.upper()} | "
            f"{page.token_count:,} | {page.access_count:,} | "
            f"{_format_source(page.source_path)} |"
        )
    return "\n".join(rows)


def _sort_key(page: ContextPage) -> tuple[int, str]:
    return (_STATE_ORDER.get(page.state, len(_STATE_ORDER)), page.id)


def _format_source(source_path: str | None) -> str:
    if not source_path:
        return "—"
    basename = Path(source_path).name or source_path
    return basename.replace("|", "\\|")


def _format_stats_line(stats: VMStats) -> str:
    return (
        f"**Stats:** evictions {stats.evictions:,} · "
        f"page faults {stats.page_faults:,} · "
        f"stale recalls {stats.stale_recalls:,} · "
        f"tokens evicted {stats.tokens_evicted:,}"
    )
