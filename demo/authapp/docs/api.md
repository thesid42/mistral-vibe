# Auth Service HTTP API

Internal reference for the demo auth service. All endpoints are JSON over HTTP.
Errors follow RFC 7807 problem+json. Times are Unix epoch seconds unless noted.

Token model: short-lived HS256 access tokens (15 min) plus rotating refresh
tokens (60 min). Refresh rotation invalidates the previous refresh token on
first use. Sessions idle for more than 30 minutes are excluded from active
counts and reaped by the janitor.

## POST /api/login

Authenticate with credentials and receive a token pair.

Parameters:

- username (string, required)
- password (string, required)
- device_name (string, optional)

Responses:

- 200 with TokenPair body
- 401 when credentials are invalid
- 429 when rate-limited

Notes:

- Clients MUST treat unknown fields in the /api/login response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 1).
- Clients MUST treat unknown fields in the /api/login response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 2).
- Clients MUST treat unknown fields in the /api/login response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 3).
- Clients MUST treat unknown fields in the /api/login response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 4).
- Clients MUST treat unknown fields in the /api/login response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 5).
- Clients MUST treat unknown fields in the /api/login response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 6).

## POST /api/refresh

Exchange a refresh token for a new token pair.

Parameters:

- refresh_token (string, required)

Responses:

- 200 with a fresh TokenPair; the used refresh token becomes invalid
- 401 when the refresh token is expired, revoked, or unknown
- 400 when the body is malformed

Notes:

- Clients MUST treat unknown fields in the /api/refresh response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 1).
- Clients MUST treat unknown fields in the /api/refresh response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 2).
- Clients MUST treat unknown fields in the /api/refresh response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 3).
- Clients MUST treat unknown fields in the /api/refresh response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 4).
- Clients MUST treat unknown fields in the /api/refresh response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 5).
- Clients MUST treat unknown fields in the /api/refresh response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 6).

## POST /api/logout

Revoke the current session.

Parameters:

- refresh_token (string, required)

Responses:

- 204 on success
- 404 when the session is unknown

Notes:

- Clients MUST treat unknown fields in the /api/logout response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 1).
- Clients MUST treat unknown fields in the /api/logout response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 2).
- Clients MUST treat unknown fields in the /api/logout response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 3).
- Clients MUST treat unknown fields in the /api/logout response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 4).
- Clients MUST treat unknown fields in the /api/logout response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 5).
- Clients MUST treat unknown fields in the /api/logout response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 6).

## GET /api/me

Return the authenticated user profile.

Parameters:

- Authorization: Bearer <access_token> header

Responses:

- 200 with the profile
- 401 when the access token is missing/expired

Notes:

- Clients MUST treat unknown fields in the /api/me response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 1).
- Clients MUST treat unknown fields in the /api/me response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 2).
- Clients MUST treat unknown fields in the /api/me response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 3).
- Clients MUST treat unknown fields in the /api/me response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 4).
- Clients MUST treat unknown fields in the /api/me response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 5).
- Clients MUST treat unknown fields in the /api/me response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 6).

## GET /api/sessions

List the caller's active sessions.

Parameters:

- Authorization header
- include_idle (bool query, default false)

Responses:

- 200 with an array of session records

Notes:

- Clients MUST treat unknown fields in the /api/sessions response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 1).
- Clients MUST treat unknown fields in the /api/sessions response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 2).
- Clients MUST treat unknown fields in the /api/sessions response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 3).
- Clients MUST treat unknown fields in the /api/sessions response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 4).
- Clients MUST treat unknown fields in the /api/sessions response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 5).
- Clients MUST treat unknown fields in the /api/sessions response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 6).

## DELETE /api/sessions/{id}

Revoke one session by id.

Parameters:

- Authorization header
- id (path)

Responses:

- 204 on success
- 403 when the session belongs to another user

Notes:

- Clients MUST treat unknown fields in the /api/sessions/{id} response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 1).
- Clients MUST treat unknown fields in the /api/sessions/{id} response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 2).
- Clients MUST treat unknown fields in the /api/sessions/{id} response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 3).
- Clients MUST treat unknown fields in the /api/sessions/{id} response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 4).
- Clients MUST treat unknown fields in the /api/sessions/{id} response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 5).
- Clients MUST treat unknown fields in the /api/sessions/{id} response as forward-compatible extensions and ignore them; servers MAY add fields without a version bump (note 6).

## Operational FAQ

**Q1. What happens when replica lag exceeds the freshness budget during scenario 1?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q2. What happens when replica lag exceeds the freshness budget during scenario 2?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q3. What happens when replica lag exceeds the freshness budget during scenario 3?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q4. What happens when replica lag exceeds the freshness budget during scenario 4?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q5. What happens when replica lag exceeds the freshness budget during scenario 5?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q6. What happens when replica lag exceeds the freshness budget during scenario 6?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q7. What happens when replica lag exceeds the freshness budget during scenario 7?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q8. What happens when replica lag exceeds the freshness budget during scenario 8?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q9. What happens when replica lag exceeds the freshness budget during scenario 9?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q10. What happens when replica lag exceeds the freshness budget during scenario 10?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q11. What happens when replica lag exceeds the freshness budget during scenario 11?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q12. What happens when replica lag exceeds the freshness budget during scenario 12?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q13. What happens when replica lag exceeds the freshness budget during scenario 13?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q14. What happens when replica lag exceeds the freshness budget during scenario 14?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q15. What happens when replica lag exceeds the freshness budget during scenario 15?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q16. What happens when replica lag exceeds the freshness budget during scenario 16?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q17. What happens when replica lag exceeds the freshness budget during scenario 17?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q18. What happens when replica lag exceeds the freshness budget during scenario 18?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q19. What happens when replica lag exceeds the freshness budget during scenario 19?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q20. What happens when replica lag exceeds the freshness budget during scenario 20?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q21. What happens when replica lag exceeds the freshness budget during scenario 21?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q22. What happens when replica lag exceeds the freshness budget during scenario 22?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q23. What happens when replica lag exceeds the freshness budget during scenario 23?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q24. What happens when replica lag exceeds the freshness budget during scenario 24?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q25. What happens when replica lag exceeds the freshness budget during scenario 25?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q26. What happens when replica lag exceeds the freshness budget during scenario 26?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q27. What happens when replica lag exceeds the freshness budget during scenario 27?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.

**Q28. What happens when replica lag exceeds the freshness budget during scenario 28?** Reads fall back to the primary, latency SLOs widen by 50ms, and the incident playbook in runbooks/authsvc.md section 4 applies. Alert `authsvc_replica_lag` pages after 5 sustained minutes.
