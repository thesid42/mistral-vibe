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
because project config requires the folder to be trusted. Do **trust the folder**
when prompted anyway: the fixture config also disables `web_fetch`/`web_search`/
`task`, which shrinks every model call and keeps the model on-script — that part
only applies from the trusted project config.)

**This demo is validated on the current (modest) API key**: the full three-beat
run fits its rate limits when beats are spaced by ~a minute of narration. If a
response ever pauses mid-beat, keep talking — Vibe retries rate-limited calls
automatically and recovers on its own; do not restart it.

## Beat 1 — pressure and eviction (~90s)

> **Prompt:** "Tests are failing and users report login sessions never expire.
> Read the files server.log and auth.py here in the current directory (use
> relative paths), run pytest test_auth.py, and tell me what's going on."

Vibe reads the log (≈11k tokens on its own — already over the 10k budget),
reads `auth.py`, runs pytest. Once the model moves past the log, VibeVM pages
it out mid-investigation. Run `/vm`: pages flipping **COLD**, evictions
counted, tokens-evicted climbing. Talking point: *"nothing was summarized —
every byte is still on disk in the page store."*

Keep the prompt lean (no docs/api.md here) — fewer model calls means the
per-minute rate limit never gets a vote. Narrate for ~a minute before the next
beat; rate-limit windows reset per minute.

## Beat 2 — page fault (~45s)

Chat a few more turns (e.g. "which test asserts the 401?" / "explain the fix").
Then:

> **Prompt:** "Earlier in server.log there was a database error with an errno —
> quote me the exact log line."

The line (`errno=104 connection reset by peer`, buried at 09:xx in a COLD page)
is gone from context. The model should call **`recall_context`** on its own
(system prompt + stub text prefer recall over re-read/grep) and return the
line **verbatim**. `/vm` again: page fault counted. Talking point: *"compaction
would have given you 'there were some database errors'; VibeVM gives you the
evidence back, byte for byte."*

## Beat 3 — stale page / coherence (~45s)

Fix the bug live (or let Vibe fix it): in `auth.py`, change the buggy check in
`refresh_session` to `if claims.get("exp", 0) <= now: return 401, None`. Then:

> **Prompt:** "Recall what auth.py looked like — has the refresh check changed?"

Two coherence mechanisms fire, and both are showable: the moment the next
model call happens, VibeVM re-hashes the sources behind resident pages and
**auto-invalidates** the now-stale auth.py page (run `/vm`: it flipped COLD on
its own — write-invalidation, like a CPU cache). The model then page-faults it
back and gets **stale_refreshed** with old/new hashes and the *current* file —
no rotten memory, whether the page was resident or paged out, whether you or
Vibe edited the file. Close on `/vm` totals: evictions, faults, tokens
evicted.

Talking point: *"the context can't lie about a file — resident or paged out."*

> **Closing line:** "Programs stopped needing to fit in RAM decades ago.
> Agents shouldn't have to fit in a context window either."

## Rehearsal notes (learned from live runs)

- **All three beats validated end-to-end against the live API** after the
  efficiency work (windowed page faults + age-based eviction + full=true cap):
  the whole three-beat run costs ~180K prompt tokens — less than beat 1 alone
  cost before (~194K). Beat 1 is ~98K over 5 model calls (~11-12K per call on
  the 10K budget); beat 2 quotes the buried `errno=104` line **verbatim** via
  one excerpted page fault; beat 3 returns `stale_refreshed` with the current
  file after a live edit.
- **API rate limits remain the top stage risk.** Back-to-back beats can trip
  HTTP 429 and each model call then stalls in retry-backoff (looks like a
  hang — check `~/.vibe/logs/vibe.log` for `rate_limited`). Narrating ~a
  minute between beats is usually enough; a key with real tokens-per-minute
  headroom removes the risk entirely.
- **Scripted (non-TTY) runs must close stdin**: `vibe` blocks forever in
  `get_prompt_from_stdin` if the caller holds stdin open — append
  `</dev/null` to any `vibe -p ...` in scripts. The interactive stage demo is
  unaffected (stdin is a TTY). Upstream quirk, not a VibeVM behavior.
- Kill stray `vibe`/`python` processes between rehearsals — an orphaned
  session quietly burns your rate limit.
- Beat 3 verified with the natural prompt and zero hints: search ranks
  filename matches first (a query naming auth.py beats the log's term
  frequency), the coherence pass auto-stubs an edited file's resident page,
  and the recall came back `stale_refreshed` in a single call.

## Reset between rehearsals

```bash
git checkout -- demo/authapp/auth.py
```

Also start a fresh Vibe session (`/new`) so the page store starts empty, and
`git checkout -- demo/authapp` if Vibe edited the fixture.
