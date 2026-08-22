"""Generate bulky-but-realistic demo assets: server.log and docs/api.md."""

import random
from pathlib import Path

DEMO = Path(__file__).resolve().parent / "authapp"
random.seed(42)

USERS = [f"user-{i:03d}" for i in range(1, 40)]
IPS = [f"10.4.{random.randint(0, 9)}.{random.randint(2, 250)}" for _ in range(30)]
PATHS = ["/api/login", "/api/refresh", "/api/logout", "/api/me", "/api/sessions", "/health"]

lines = []
t = 0


def stamp() -> str:
    global t
    t += random.randint(1, 4)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = random.randint(0, 999)
    return f"2026-08-21T{9 + h:02d}:{m:02d}:{s:02d}.{ms:03d}Z"


for i in range(360):
    ts = stamp()
    user = random.choice(USERS)
    ip = random.choice(IPS)
    path = random.choice(PATHS)
    kind = random.random()
    if i == 120:
        lines.append(f"{ts} ERROR gunicorn.worker pid=4117 worker timeout, killing worker")
        lines.append(
            f"{ts} ERROR authsvc.db conn=pg-7 query failed after 30000ms: "
            "OperationalError errno=104 connection reset by peer "
            "(host=db-primary.internal port=5432 user=authsvc)"
        )
        lines.append(f"{ts} WARN  authsvc.db conn=pg-7 retrying with backoff attempt=1/5 delay=200ms")
        continue
    if i == 260:
        lines.append(
            f"{ts} WARN  authsvc.refresh token exp={1_755_003_600} now={1_755_007_205} "
            f"user={user} accepted_expired_refresh=true status=200 "
            "-- SHOULD have been 401, ticket AUTH-2214"
        )
        continue
    if kind < 0.62:
        ms_taken = random.randint(3, 180)
        status = 200 if path != "/api/refresh" or random.random() < 0.9 else 401
        lines.append(
            f'{ts} INFO  authsvc.http {ip} "POST {path}" status={status} '
            f"user={user} took={ms_taken}ms bytes={random.randint(180, 2400)}"
        )
    elif kind < 0.78:
        lines.append(
            f"{ts} DEBUG authsvc.session touch user={user} sessions_active={random.randint(3, 41)} "
            f"idle_reaped={random.randint(0, 3)} timeout_min=30"
        )
    elif kind < 0.88:
        lines.append(
            f"{ts} DEBUG authsvc.token issue user={user} typ=access ttl=900 alg=HS256 "
            f"kid=demo-1 iat_skew_ms={random.randint(-40, 40)}"
        )
    elif kind < 0.95:
        lines.append(
            f"{ts} WARN  authsvc.http {ip} rate-limit near threshold user={user} "
            f"window=60s count={random.randint(50, 59)}/60"
        )
    else:
        lines.append(
            f"{ts} INFO  authsvc.audit event=logout user={user} reason=user_request "
            f"session_age_s={random.randint(60, 3400)}"
        )

(DEMO / "server.log").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

head = """# Auth Service HTTP API

Internal reference for the demo auth service. All endpoints are JSON over HTTP.
Errors follow RFC 7807 problem+json. Times are Unix epoch seconds unless noted.

Token model: short-lived HS256 access tokens (15 min) plus rotating refresh
tokens (60 min). Refresh rotation invalidates the previous refresh token on
first use. Sessions idle for more than 30 minutes are excluded from active
counts and reaped by the janitor.

"""

sections = []
endpoints = [
    ("POST", "/api/login", "Authenticate with credentials and receive a token pair",
     ["username (string, required)", "password (string, required)", "device_name (string, optional)"],
     ["200 with TokenPair body", "401 when credentials are invalid", "429 when rate-limited"]),
    ("POST", "/api/refresh", "Exchange a refresh token for a new token pair",
     ["refresh_token (string, required)"],
     ["200 with a fresh TokenPair; the used refresh token becomes invalid",
      "401 when the refresh token is expired, revoked, or unknown",
      "400 when the body is malformed"]),
    ("POST", "/api/logout", "Revoke the current session",
     ["refresh_token (string, required)"],
     ["204 on success", "404 when the session is unknown"]),
    ("GET", "/api/me", "Return the authenticated user profile",
     ["Authorization: Bearer <access_token> header"],
     ["200 with the profile", "401 when the access token is missing/expired"]),
    ("GET", "/api/sessions", "List the caller's active sessions",
     ["Authorization header", "include_idle (bool query, default false)"],
     ["200 with an array of session records"]),
    ("DELETE", "/api/sessions/{id}", "Revoke one session by id",
     ["Authorization header", "id (path)"],
     ["204 on success", "403 when the session belongs to another user"]),
]
for method, path, desc, params, responses in endpoints:
    block = [f"## {method} {path}", "", desc + ".", "", "Parameters:", ""]
    block += [f"- {p}" for p in params]
    block += ["", "Responses:", ""]
    block += [f"- {r}" for r in responses]
    block += ["", "Notes:", ""]
    for n in range(6):
        block.append(
            f"- Clients MUST treat unknown fields in the {path} response as forward-compatible "
            f"extensions and ignore them; servers MAY add fields without a version bump (note {n + 1})."
        )
    block.append("")
    sections.append("\n".join(block))

tail_faq = ["## Operational FAQ", ""]
for n in range(28):
    tail_faq.append(
        f"**Q{n + 1}. What happens when replica lag exceeds the freshness budget during scenario {n + 1}?** "
        "Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in "
        "runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes."
    )
    tail_faq.append("")

docs_dir = DEMO / "docs"
docs_dir.mkdir(exist_ok=True)
(docs_dir / "api.md").write_text(head + "\n".join(sections) + "\n" + "\n".join(tail_faq), encoding="utf-8", newline="\n")

for f in ["server.log", "docs/api.md"]:
    p = DEMO / f
    print(f, p.stat().st_size, "bytes")
