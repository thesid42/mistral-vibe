from __future__ import annotations

from vibe.cli.commands import CommandRegistry
from vibe.cli.textual_ui.vm_report import format_vm_report
from vibe.core.vibevm.models import ContextPage, PageState, VMSnapshot, VMStats

_NOW = "2026-01-01T00:00:00+00:00"


def _make_page(
    *,
    page_id: str = "P001",
    page_type: str = "read_file",
    token_count: int = 100,
    access_count: int = 0,
    source_path: str | None = None,
    state: PageState = PageState.HOT,
    created_seq: int = 0,
) -> ContextPage:
    return ContextPage(
        id=page_id,
        session_id="sess-1",
        tool_call_id=f"call-{page_id}",
        page_type=page_type,
        content="",
        summary="a summary",
        source_path=source_path,
        source_hash=None,
        token_count=token_count,
        importance=0.5,
        access_count=access_count,
        created_seq=created_seq,
        created_at=_NOW,
        last_accessed=_NOW,
        state=state,
    )


def _body_rows(report: str) -> list[str]:
    return [
        line
        for line in report.splitlines()
        if line.startswith("|") and "Page" not in line and "---" not in line
    ]


class TestVmCommandRegistration:
    def test_vm_command_parses(self) -> None:
        registry = CommandRegistry()

        result = registry.parse_command("/vm")

        assert result is not None
        cmd_name, cmd, cmd_args = result
        assert cmd_name == "vm"
        assert cmd.handler == "_show_vm"
        assert cmd.side_channel is True
        assert cmd_args == ""

    def test_vm_command_registered_in_help_text(self) -> None:
        registry = CommandRegistry()
        assert "/vm" in registry.get_help_text()


class TestFormatVmReport:
    def test_empty_store_shows_friendly_message(self) -> None:
        snapshot = VMSnapshot(pages=[], stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        assert "No pages tracked yet" in report

    def test_table_contains_page_ids_states_and_tokens(self) -> None:
        pages = [
            _make_page(
                page_id="P003",
                page_type="read_file",
                token_count=5821,
                access_count=2,
                state=PageState.HOT,
                created_seq=2,
                source_path="/repo/auth.py",
            ),
            _make_page(
                page_id="P002",
                page_type="bash",
                token_count=6210,
                access_count=0,
                state=PageState.COLD,
                created_seq=1,
            ),
        ]
        snapshot = VMSnapshot(pages=pages, stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        assert "| P003 | read_file | HOT | 5,821 | 2 | auth.py |" in report
        assert "| P002 | bash | COLD | 6,210 | 0 | — |" in report

    def test_sort_order_pinned_then_hot_then_cold_by_page_id(self) -> None:
        pages = [
            _make_page(page_id="P010", state=PageState.COLD),
            _make_page(page_id="P001", state=PageState.PINNED),
            _make_page(page_id="P005", state=PageState.HOT),
            _make_page(page_id="P002", state=PageState.PINNED),
        ]
        snapshot = VMSnapshot(pages=pages, stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        ids_in_order = [row.split("|")[1].strip() for row in _body_rows(report)]
        assert ids_in_order == ["P001", "P002", "P005", "P010"]

    def test_pipe_in_source_path_is_escaped_and_shown_as_basename(self) -> None:
        pages = [_make_page(page_id="P001", source_path="dir/weird|name.py")]
        snapshot = VMSnapshot(pages=pages, stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        assert "| weird\\|name.py |" in report

    def test_missing_source_path_renders_em_dash(self) -> None:
        pages = [_make_page(source_path=None)]
        snapshot = VMSnapshot(pages=pages, stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        rows = _body_rows(report)
        assert len(rows) == 1
        assert rows[0].split("|")[6].strip() == "—"

    def test_stats_line_contents(self) -> None:
        pages = [_make_page()]
        stats = VMStats(
            evictions=3,
            page_faults=2,
            hits=9,
            misses=1,
            stale_recalls=1,
            tokens_evicted=41822,
        )
        snapshot = VMSnapshot(pages=pages, stats=stats, db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        assert (
            "**Stats:** evictions 3 · page faults 2 · stale recalls 1 · "
            "tokens evicted 41,822" in report
        )

    def test_heading_shows_in_view_and_budget_when_both_available(self) -> None:
        snapshot = VMSnapshot(pages=[_make_page()], stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=20_000, context_tokens=14_200)

        assert report.splitlines()[0] == "## VibeVM — 14.2k in view / 20k budget"

    def test_heading_omits_budget_when_unavailable(self) -> None:
        snapshot = VMSnapshot(pages=[_make_page()], stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=14_200)

        first_line = report.splitlines()[0]
        assert first_line == "## VibeVM — 14.2k in view"
        assert "budget" not in first_line

    def test_heading_plain_when_context_tokens_unavailable(self) -> None:
        snapshot = VMSnapshot(pages=[_make_page()], stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        assert report.splitlines()[0] == "## VibeVM"

    def test_budget_falls_back_to_snapshot_stats_when_not_provided(self) -> None:
        stats = VMStats(context_budget=20_000)
        snapshot = VMSnapshot(pages=[_make_page()], stats=stats, db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=14_200)

        assert report.splitlines()[0] == "## VibeVM — 14.2k in view / 20k budget"

    def test_budget_fallback_ignores_zero_context_budget_stat(self) -> None:
        stats = VMStats(context_budget=0)
        snapshot = VMSnapshot(pages=[_make_page()], stats=stats, db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=14_200)

        first_line = report.splitlines()[0]
        assert first_line == "## VibeVM — 14.2k in view"
        assert "budget" not in first_line

    def test_explicit_budget_takes_precedence_over_snapshot_stats(self) -> None:
        stats = VMStats(context_budget=999_000)
        snapshot = VMSnapshot(pages=[_make_page()], stats=stats, db_path="x.db")

        report = format_vm_report(snapshot, budget=20_000, context_tokens=14_200)

        assert report.splitlines()[0] == "## VibeVM — 14.2k in view / 20k budget"

    def test_page_content_is_never_rendered(self) -> None:
        page = _make_page().model_copy(update={"content": "TOP-SECRET-CONTENT"})
        snapshot = VMSnapshot(pages=[page], stats=VMStats(), db_path="x.db")

        report = format_vm_report(snapshot, budget=None, context_tokens=None)

        assert "TOP-SECRET-CONTENT" not in report
