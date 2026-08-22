# VibeVM Live Demo Script

Fixture: `demo/authapp` — a tiny auth service with a planted bug (`refresh_session`
checks `iat` instead of `exp`, so expired refresh tokens are accepted → one failing test)
plus a 76KB `server.log` and a fat API doc to create real context pressure.

## Setup (once, before going on stage)

```bash
cd demo/authapp
```

Launch Vibe with a deliberately tiny context budget so the VM has to work
(env vars override config, no trust prompt involved):

```bash
VIBE_VIBEVM__ENABLED=true VIBE_VIBEVM__CONTEXT_BUDGET=10000 VIBE_VIBEVM__MIN_PAGE_TOKENS=150 vibe
```

(Equivalent `.vibe/config.toml` is in the fixture; env vars are the reliable path
because project config requires the folder to be trusted.)

## Beat 1 — pressure and eviction (~90s)

> **Prompt:** "Tests are failing and users report login sessions never expire.
> Read server.log and docs/api.md, run the tests, look at auth.py, and tell me
> what's going on."

Vibe reads the log (≈19k tokens), the docs, runs pytest, reads `auth.py` — the
view blows through the 10k budget. Run `/vm`: pages flipping **COLD**, evictions
counted, tokens-evicted climbing. Talking point: *"nothing was summarized —
every byte is still on disk in the page store."*

## Beat 2 — page fault (~45s)

Chat a few more turns (e.g. "which test asserts the 401?" / "explain the fix").
Then:

> **Prompt:** "Earlier in server.log there was a database error with an errno —
> quote me the exact log line."

The line (`errno=104 connection reset by peer`, buried at 09:xx in a COLD page)
is gone from context. Watch the model call **`recall_context`** and return the
line **verbatim**. `/vm` again: page fault counted. Talking point: *"compaction
would have given you 'there were some database errors'; VibeVM gives you the
evidence back, byte for byte."*

## Beat 3 — stale page / coherence (~45s)

Fix the bug live (or let Vibe fix it): in `auth.py`, change the buggy check in
`refresh_session` to `if claims.get("exp", 0) <= now: return 401, None`. Then:

> **Prompt:** "Recall what auth.py looked like — has the refresh check changed?"

`recall_context` detects the sha256 mismatch, reports **stale_refreshed** with
old/new hashes, and returns the *current* file — no rotten memory. Close on
`/vm` totals: evictions, faults, tokens evicted.

> **Closing line:** "Programs stopped needing to fit in RAM decades ago.
> Agents shouldn't have to fit in a context window either."

## Rehearsal notes (learned from live runs)

- **API rate limits are the #1 stage risk.** Every continued turn resends the
  full history (~35K tokens by beat 3); a low-tier key hits HTTP 429 and each
  model call stalls minutes in retry-backoff (looks like a hang — check
  `~/.vibe/logs/vibe.log` for `rate_limited`). Use a key with generous
  tokens-per-minute headroom on demo day.
- Beats 1 and 2 were validated end-to-end against the live API: the model
  diagnoses the bug, and after eviction quotes the buried
  `errno=104 connection reset by peer` log line **verbatim** via a single
  `recall_context` page fault.
- Kill stray `vibe`/`python` processes between rehearsals — an orphaned
  session holds its lease and quietly burns your rate limit.

## Reset between rehearsals

```bash
git checkout -- demo/authapp/auth.py
```

Also start a fresh Vibe session (`/new`) so the page store starts empty, and
`git checkout -- demo/authapp` if Vibe edited the fixture.
