# VibeVM — virtual memory for Vibe's context window

> Programs stopped needing to fit entirely in RAM decades ago.
> Agents shouldn't have to fit entirely in a context window either.

Vibe's answer to a full context window is compaction: summarize the history and
carry on. Compaction is **lossy compression** — once "`FAILED test_refresh_token:
expected 401, got 200`" becomes "there were test failures", the evidence is gone
and no amount of asking brings it back.

VibeVM treats the context window like **RAM** and everything the agent has seen
like **virtual memory**:

```
              VIBE AGENT
                  │
                  ▼
          ┌──────────────┐
          │  Context RAM │  ← token budget
          └──────┬───────┘
                 │ VibeVM
        ┌────────┼────────┐
       HOT     COLD     recall
        │        │         │
    in prompt  SQLite   page fault
```

- Large tool results (file reads, test runs, greps, logs) become **pages**,
  mirrored into a per-session **SQLite + FTS5 page store**.
- Under budget pressure, low-value pages are **evicted**: their content in the
  outgoing prompt is replaced by a three-line stub — *"page P004, pytest output,
  ~4.3k tokens, call `recall_context` to restore"*. **Nothing is deleted.**
- When the model needs old detail, it calls **`recall_context`** — a **page
  fault**. The exact original bytes come back, found by full-text search or
  page id.
- File-backed pages carry a **sha256 of the source file**. If the file changed
  since paging, recall returns the *current* content flagged `stale_refreshed`
  instead of serving rotten memory — cache invalidation, not just caching.
- `/vm` shows the page table: hot/cold pages, token counts, evictions, page
  faults, stale refreshes, tokens saved.

**Compaction is lossy compression. VibeVM is hierarchical storage.**

## How it works (design highlights)

- **History is never mutated.** VibeVM is a pure *view transformation* applied
  in `AgentLoop._messages_for_backend`, strictly downstream of compaction's own
  `select_model_context` cut — the same "hide, don't delete" philosophy Vibe
  already uses, extended to individual tool results. Stubbing swaps a tool
  message's *content* while keeping the message in place, so the Mistral API's
  tool-call/tool-result pairing invariants always hold, and `/rewind`, session
  resume, and the on-disk transcript are untouched.
- **Eviction is scored**, not chronological:
  `0.45·recency + 0.25·access-frequency + 0.30·type-importance`, with a
  high/low watermark (evict down to `budget × evict_target_ratio`) and a
  protected window of recent work that never pages out.
- **Resume-safe.** Pages bind to history by `tool_call_id` (stable on disk), so
  a `--resume`d session reconciles its page table automatically; `/new` and
  `/clear` rotate the session id and start a fresh store.
- **Compaction stays as the fallback.** With a VibeVM budget well below
  `auto_compact_threshold`, compaction simply never needs to fire.

## Try it

```bash
VIBE_VIBEVM__ENABLED=true VIBE_VIBEVM__CONTEXT_BUDGET=10000 vibe
```

or in `.vibe/config.toml` / `~/.vibe/config.toml`:

```toml
[vibevm]
enabled = true          # off by default — zero behavior change unless opted in
context_budget = 100000 # token ceiling for the model view
evict_target_ratio = 0.8
min_page_tokens = 400   # only tool results at least this large become pages
protect_recent_turns = 2
```

A ready-made pressure-cooker demo lives in [`demo/`](demo/DEMO.md): a small
auth service with a planted bug, a 76KB server log, and a three-beat stage
script — eviction, page fault, stale page — under a deliberately tiny 10k
budget.

## What's where

| Piece | Path |
|---|---|
| Page model, stats | `vibe/core/vibevm/models.py` |
| SQLite + FTS5 store | `vibe/core/vibevm/store.py` |
| Pager (register / evict / stub / recall) | `vibe/core/vibevm/pager.py` |
| Page-fault tool | `vibe/core/tools/builtins/recall_context.py` |
| Agent-loop integration | `vibe/core/agent_loop/_loop.py` (`_messages_for_backend`) |
| `/vm` page-table command | `vibe/cli/commands.py`, `vibe/cli/textual_ui/vm_report.py` |
| Config | `[vibevm]` in `vibe/core/config/models.py` |
| Page stores on disk | `~/.vibe/vm/<session_id>.db` |

Measured, not just claimed: live ablation numbers (VibeVM vs full context vs
forced compaction) are in [BENCHMARKS.md](BENCHMARKS.md).

Built for the Mistral hackathon (Track 2) on top of `mistral-vibe` v2.24.3.
