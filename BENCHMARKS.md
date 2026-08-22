# VibeVM Ablation Benchmarks — 2026-08-22

Live ablation of VibeVM against Vibe's alternatives, run on the demo fixture
with the author's own API key. Raw outputs and per-condition JSON are preserved
on the benchmarking machine (session scratchpad `bench/`: `vm_s*.txt`,
`full_s*.txt`, `compact2_s1.txt`, `result_*.json`, plus the three isolated
`home-*` trees with full session logs and the VM page-store db).

## Setup

- Code: fork `thesid42/mistral-vibe`, branch `vibevm` (VM ran at `606d44e`; the
  `4e4a42d` delta — optional query on page_id recalls — was never exercised by
  the VM run's calls, so results are comparable).
- Isolated per-condition workspaces: git worktree `mv-bench` + a separate
  `VIBE_HOME` per condition (own sessions, logs, page stores; fixture's
  `.vibe/` removed so all conditions run identical env-var config and identical
  tool sets).
- Task: the 3-step demo chain on `demo/authapp`, identical prompts:
  - **S1** investigate (read 42KB `server.log` + `auth.py`, run pytest, explain the bug)
  - **S2** (continuation) quote the exact `errno=104` log line verbatim
  - **S3** (continuation) after `auth.py` is edited on disk: "has the refresh check changed?"
- Model: `mistral-vibe-cli-latest` (medium 3.5), user's own API key, live.
- Conditions:
  1. **VM @ 10K** — VibeVM enabled, `context_budget=10000`, `min_page_tokens=150`
  2. **Full context** — VibeVM off, no effective limit (auto-compact at default 200K, never reached)
  3. **Compaction @ 10K** — VibeVM off, auto-compaction pinned to a 10K threshold
     (same memory limit as condition 1 — the apples-to-apples comparison)

## Results

| | VM @ 10K | Full context | Compaction @ 10K |
|---|---|---|---|
| Completed the chain | **✓ 3/3 steps** | ✓ 3/3 steps | **✗ killed 8 min into S1** |
| Prompt tokens (total) | **160,544** | 227,140 | **445,511 — S1 alone, unfinished** |
| Completion tokens | 1,889 | 4,344 | 7,083 |
| Model calls | 10 | 7 | 12 (partial) |
| Auto-compactions | 0 | 0 | **5** |
| End context size | **13,685** | 40,757 | 35,331 (still over) |
| S1: bug diagnosed | ✓ | ✓ | — (not reached) |
| S2: exact `errno=104` line verbatim | ✓ via one excerpted page fault | ✓ (line still sat in context) | — |
| S3: answers from disk truth after edit | **✓ `stale_refreshed`, "the bug has been fixed"** | **✗ ROTTEN — quoted the old `iat` code as current** | — |
| VM stats | 2 evictions, 3 faults, 1 stale recall, 12.9K tokens evicted | — | — |

## Findings

1. **Cost.** At the same task, VibeVM used **29% fewer prompt tokens than
   unlimited context** (160K vs 227K) while keeping the end context 3× smaller
   (13.7K vs 40.8K — bounded growth vs monotone growth).
2. **Same-limit comparison is a rout.** With the identical 10K memory limit,
   compaction entered a thrash loop — compact → model re-reads what the summary
   destroyed → context refills → compact again, **five cycles** — burning 2.8×
   the tokens of VibeVM's *entire finished chain* without completing even step
   1, and still sitting at 35K context. VibeVM under the same limit finished
   everything with 2 evictions and 3 cheap page faults.
3. **Coherence.** The full-context baseline gave a confidently wrong answer in
   S3: asked whether the edited file changed, it quoted its stale in-context
   copy (the old `iat` check + BUG comment) as the current state, with zero
   tool calls. VibeVM auto-invalidated the resident page on the next call,
   forced a page fault, and answered from the disk truth (`stale_refreshed`).
   *"The context can't lie about a file"* is a measured differentiator, not
   just a design claim.
4. **Exactness.** S2's verbatim quote succeeded under VM via a ~300-token
   excerpted page fault. The full baseline also succeeded — by hauling the
   whole 11.6K-token log in every one of its calls. Compaction, by mechanism,
   destroys that line's verbatim form (summaries), and its run never got far
   enough to try re-reading it from disk.

## Added scenario: VibeVM AND compaction both pinned at 10K

A fourth condition probing the worst-case pairing: VibeVM enabled
(budget 10000) *and* auto-compaction pinned to the same 10000 threshold
(session `session_20260822_232635_b2a21e4d`, home `bench/home-hybrid`).

| | VM + compaction @ 10K |
|---|---|
| S1 (investigate) | ✗ turn-capped at 14 calls; **207,198** prompt tokens; **2 compactions** fired |
| S2 (exact errno line) | ✓ verbatim — but via **file re-read + grep**, not a page fault |
| End context / totals | 6,722 ctx; 248,379 prompt tokens incl. S2; VM: 18 evictions, 5 faults |

What the transcript shows: VibeVM's protected working set necessarily rides
above 10K mid-investigation, so compaction keeps firing anyway — and each
firing cuts the view *including the stubs*, so the model re-reads its files
and the store accumulates duplicate pages (three separate 11.6K copies of the
log). The two mechanisms fight when given the same limit.

Three honest conclusions:

1. **Configuration guidance, now measured**: VibeVM's budget must sit well
   below the compaction threshold. The shipped defaults do exactly that
   (10K budget vs 200K threshold) — which is why the VM-only condition logged
   zero compactions.
2. Even in this hostile pairing, VibeVM damped the damage versus
   compaction-alone: 207K tokens reaching deep into the task (it even
   attempted the fix) with 5 working page faults across compaction
   boundaries, versus 445K without finishing step one.
3. The post-compaction exact quote succeeded because the *source file still
   existed on disk* — the model grepped it rather than recalling (compaction
   had wiped the stubs that would have steered it to recall). For file-backed
   evidence, disk is an alternate recovery path; for ephemeral evidence (test
   runs, command output), only the page store survives compaction — the
   VM-only condition's quote did come through a true page fault.

## Methodology — exact inputs

Every condition ran the identical three prompts, via `uv run vibe` in
`mv-bench/demo/authapp` (worktree of the fork), programmatic mode,
`--auto-approve --output text --trust </dev/null`, caps `--max-price 0.80
--max-turns 14` (S1) and `--max-price 0.50 --max-turns 40` (S2/S3):

- **S1** (`-p`): "Tests are failing and users report login sessions never
  expire. Read the files server.log and auth.py here in the current directory
  (use relative paths), run pytest test_auth.py, and tell me what's going on."
- **S2** (`-c`): "Earlier in server.log there was a database error with an
  errno — quote me the exact log line."
- **S3** (`-c`, after `auth.py`'s buggy line was replaced on disk by a scripted
  patch — `iat` check → `exp` check): "Recall what auth.py looked like — has
  the refresh check changed?"

Per-condition environment (each with its own `VIBE_HOME`, so separate
sessions, logs, page stores, and config):

| Condition | VIBE_HOME | Env / config | Session id |
|---|---|---|---|
| VM @ 10K | `bench/home-vm` | `VIBE_VIBEVM__ENABLED=true`, `CONTEXT_BUDGET=10000`, `MIN_PAGE_TOKENS=150` | `session_20260822_221054_f25a3440` |
| Full context | `bench/home-compact` (1st session) | `VIBE_VIBEVM__ENABLED=false` (threshold override silently no-op → default 200K, never reached) | `session_20260822_221400_85198bae` |
| Compaction @ 10K | `bench/home-compact` (2nd session) | `VIBE_VIBEVM__ENABLED=false` + user-config-pinned model `bench-compact` with `auto_compact_threshold=10000` | `session_20260822_222229_41d52c0e` |

All three used the same underlying model `mistral-vibe-cli-latest`
(Mistral Medium 3.5); the compaction condition's `bench-compact` alias is the
same model name with only the threshold changed (verified in the session's own
config dump).

## How each number is derived (provenance)

- **Prompt/completion tokens, end context**: read from each session's
  `meta.json` → `stats.session_prompt_tokens` / `session_completion_tokens` /
  `context_tokens`. These are written by **Vibe's own session logger**, which
  accumulates the **Mistral-API-reported usage** of every call — not estimated
  or computed by the benchmark harness.
- **Model calls / compactions / tool calls**: recounted directly from each
  session's `messages.jsonl` (assistant-role messages; messages carrying
  `context_boundary` — the envelope Vibe appends on every compaction; tool_call
  entries).
- **S2 exactness**: byte-exact substring match of the true log line
  (`...errno=104 connection reset by peer (host=db-primary.internal port=5432
  user=authsvc)`) against the captured stdout of each run.
- **S3 verdicts**: quoted from captured stdout:
  - VM: "Yes — the refresh check **has been changed**. Line 100 now correctly
    reads `if claims.get("exp", 0) <= now:` … has been fixed."
  - Full context: "**No, the refresh check has not changed. In the `auth.py` I
    read, line 100 still has the bug**" — stated while the file on disk
    contained the fix (the patch ran, scripted and verified, before the
    prompt).
- **VM internals** (evictions/faults/stale recalls): read from the condition's
  page-store SQLite `stats` table, written by VibeVM during the run.

## Independent audit (anti-fabrication check)

After the report was first written, every figure was re-derived fresh from the
raw artifacts by an independent re-read (no cached values): re-parsed the three
sessions' `meta.json` and `messages.jsonl`, recounted messages/compactions/
tools, re-ran the exact-line string match, and re-extracted the verdict
sentences. **All figures matched the table above.** The audit also confirmed
the relabeling integrity: `home-compact` contains exactly two sessions, and
each session's *embedded config dump* proves which condition it truly ran
(first: `active_model=''` with threshold 200000 → full-context; second:
`active_model='bench-compact'` with threshold 10000 → real compaction, 5
`context_boundary` envelopes present in its transcript).

Chain of custody: the harness (this session) authored only the prompts, env
vars, and the S3 file patch; Vibe's process wrote the transcripts and stats;
the model wrote the answers. Raw artifacts are preserved for re-inspection in
the session scratchpad `bench/` directory (`vm_s*.txt`, `full_s*.txt`,
`compact2_s1.txt`, `result_*.json`, plus the three `home-*` trees with full
session logs and the VM page-store db).

## Caveats (honest limits of this data)

- **N=1 per condition** — single live runs, no variance estimates; LLM
  nondeterminism applies. Treat as indicative, directionally strong.
- **Shared rate-limited key, sequential runs.** The compaction run went last
  and absorbed the most 429 backoff (83 rate-limit retries in its log), which
  inflates its wall-clock. Its *token counts and compaction count* (445K, 5
  compactions in 12 calls) are quota-independent and stand on their own.
- The compaction condition required pinning a custom model entry
  (`bench-compact`) in the bench `VIBE_HOME` config: **upstream bug found** —
  the global/env `auto_compact_threshold` override is silently ignored because
  the default config layer's `model_dump()` marks every model's threshold as
  explicitly set, so the propagation validator never applies the global. Worth
  an upstream issue.
- The full-context condition was originally launched intending to be the
  compaction condition (threshold override silently no-op per the bug above);
  since compaction never fired and the limit was never reached, it is a valid
  full-context baseline and was relabeled as such.
