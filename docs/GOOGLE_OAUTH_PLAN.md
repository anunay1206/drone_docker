# Google OAuth SSO + Login Landing Page — Implementation Plan

**Goal:** Require Google sign-in before anyone can use the frontend, and log
*which Google user* triggered every API call — **without changing anything on
the Airflow side** (no new DAG conf keys, no DAG code changes, no changes to the
request the backend sends to Airflow).

**Key insight that makes the constraint trivial to satisfy:** user identity is
needed only for *authentication + audit logging*, which both live entirely in
the FastAPI backend and the frontend. The pipeline/Airflow layer never needs to
know who the user is. `app/services/airflow_client.py` keeps sending the exact
same `conf` it sends today. So the Airflow contract is untouched by design — we
just don't propagate identity past the API boundary.

---

## 1. Current state (what exists today)

- **Auth:** `app/api/deps.py :: require_api_key()` — a single optional shared
  `X-API-Key` header. On success it returns the string `"default"`, which is
  used everywhere as `user_id`. There is no concept of individual users.
- **User model:** `Project.user_id` column already exists (`app/db/models.py`),
  defaulting to `"default"`. Multi-tenant plumbing is half-there but unused.
- **Frontend:** one static `frontend/index.html` (nginx). API key is typed into
  a password field (`#key`) and attached by the `headers()` helper (line ~446).
  No login screen, no gate — the whole UI is immediately usable.
- **Airflow:** backend triggers DAGs via `airflow_client.trigger_dag(dag_id, conf)`.
  `conf` today carries only pipeline params (project id, run number, model, etc).

## 2. Target state

1. Frontend shows a **landing / login page** first. "Sign in with Google" is the
   only action. The existing pipeline UI is hidden until a valid session exists.
2. Backend gets a **Google OIDC login flow** + **session** (signed cookie or
   JWT). Every protected endpoint resolves the real user (email) instead of the
   hardcoded `"default"`.
3. An **audit log** records `(timestamp, user_email, method, path, project_id,
   status_code)` for every API call — persisted to the DB (new `audit_log`
   table) and/or structured stdout logs.
4. Airflow calls are **byte-for-byte unchanged.**

---

## 3. Recommended architecture

**Auth style: OIDC Authorization Code flow, session handled by the backend.**

Google is the Identity Provider. The FastAPI backend is the OAuth *client* and
also issues the app session. The frontend never handles Google tokens directly —
it just gets a session cookie. This is the standard, safe SSO pattern and keeps
all secrets server-side.

```
Browser (index.html)
  │  1. hits app, no session  ─────────────►  redirected to /auth/login
  │
Backend  /auth/login  ── 302 ──►  Google consent screen
  │
Google  ── redirect w/ ?code ──►  Backend /auth/callback
  │       2. backend exchanges code → id_token (server-to-server)
  │       3. verify id_token, extract email + domain
  │       4. set signed session cookie (HttpOnly, Secure, SameSite=Lax)
  │  302 ►  back to frontend "/"
  │
Browser now has session cookie
  │  every API call sends cookie automatically
Backend  require_user()  ── reads cookie ──►  real user email
         audit middleware  ── logs (user, method, path, status)
         pipeline / airflow_client  ── UNCHANGED conf ──►  Airflow
```

### Library choice
Use **Authlib** (`authlib` + `itsdangerous` for cookie signing), or
`fastapi-sso`. Authlib is the most common, well-maintained option and integrates
with Starlette's `SessionMiddleware`.

```
# requirements-api.txt  (add)
authlib
itsdangerous
```

### Restrict to your org (optional but recommended)
Google returns the `hd` (hosted domain) claim for Workspace accounts. Gate on
it, e.g. only allow `@iitd.ac.in`, so random Google accounts can't sign in.

---

## 4. Backend changes (all in `code/app/`)

### 4.1 New settings — `app/core/settings.py`
```python
# ── Google OAuth (SSO) ──────────────────────────────────────────────
google_client_id: str | None = None
google_client_secret: str | None = None
oauth_redirect_url: str = ""          # e.g. https://api.example.com/auth/callback
session_secret: str = "change-me"     # signs the session cookie
allowed_hd: str | None = None         # e.g. "iitd.ac.in" — restrict to a domain
auth_enabled: bool = False            # master switch; keep False for local dev
```
All read from env with the existing `TCP_` prefix, so `.env` / `.env.example`
get new entries. No secrets in code.

### 4.2 New router — `app/api/v1/auth.py`
Endpoints:
- `GET /auth/login` → redirect to Google.
- `GET /auth/callback` → exchange code, verify `id_token`, check `hd`, create
  session cookie, redirect to the frontend.
- `POST /auth/logout` → clear the cookie.
- `GET /auth/me` → return `{email, name, picture}` for the current session (the
  frontend uses this to decide login vs app view).

Register it in `app/api/v1/router.py` (and add `SessionMiddleware` in
`app/main.py`).

### 4.3 Replace the identity dependency — `app/api/deps.py`
Add `require_user()` that reads the session cookie and returns the email. Make
the existing `get_project` / endpoints depend on it instead of
`require_api_key`. Behaviour:
- `auth_enabled = False` → fall back to today's behaviour (`"default"`), so local
  dev and existing tests keep working.
- `auth_enabled = True` → no valid session ⇒ `401 UNAUTHENTICATED`.

```python
def require_user(request: Request) -> str:
    if not settings.auth_enabled:
        return "default"
    user = request.session.get("user")   # {"email": ...} set at callback
    if not user:
        raise HTTPException(401, {"code": "UNAUTHENTICATED",
                                  "message": "Google sign-in required"})
    return user["email"]
```
Now `Project.user_id` naturally becomes the signed-in email — the column already
exists, so per-user project isolation comes almost for free.

### 4.4 Audit logging — the actual "who triggered what" requirement
Two layers, use either or both:

**(a) Global middleware in `app/main.py`** — catches *every* request:
```python
@app.middleware("http")
async def audit(request, call_next):
    resp = await call_next(request)
    user = request.session.get("user", {}).get("email", "anonymous")
    log.info("API_CALL user=%s method=%s path=%s status=%s",
             user, request.method, request.url.path, resp.status_code)
    return resp
```

**(b) Persisted table** for queryable audit trail — new model
`AuditLog(id, user_email, method, path, project_id, status_code, created_at)` in
`app/db/models.py`, written from the middleware (or from `require_user`). Pick
this if you want to *report* on usage later, not just grep logs.

> The service-token path (`require_service_token`, used by the compute
> callbacks `/analyze`, `/finalize`) stays as-is — those are machine-to-machine
> calls from Airflow, not human users, and must remain callable without a Google
> session.

## 5. Frontend changes — `frontend/index.html`

Minimal, no framework needed:

1. **On load**, call `GET /auth/me`.
   - `200` → render the existing pipeline UI (current content).
   - `401` → render the **landing page**: product blurb + a single
     "Sign in with Google" button linking to `/auth/login`.
2. Wrap the current app markup in a container hidden until `/auth/me` succeeds.
3. Add a small header showing the signed-in email + a "Sign out" button
   (`POST /auth/logout`).
4. **Send the cookie:** change the `fetch` wrapper (line ~449) to include
   `credentials: "include"` so the session cookie rides along. The old
   `X-API-Key` field can be removed (or kept as a dev fallback).

CORS note: `app/main.py` currently sets `allow_origins=["*"]`. Cookies require a
concrete origin + `allow_credentials=True`. So for the cookie flow, set
`allow_origins=[<frontend origin>]` and `allow_credentials=True`. (If frontend
and API are served same-origin behind one reverse proxy, this is a non-issue.)

## 6. Google Cloud setup (one-time, console)

1. Create an **OAuth 2.0 Client ID** (type: Web application) in Google Cloud
   Console → APIs & Services → Credentials.
2. Authorized redirect URI = your `oauth_redirect_url`
   (e.g. `https://api.example.com/auth/callback`, plus a localhost one for dev).
3. Configure the OAuth consent screen (Internal if Workspace-only).
4. Put the client id/secret into `.env` (never commit them).

---

## 7. The Airflow constraint — how it's honored

Nothing in this plan touches:
- `airflow/dags/drone_analyze_dag.py`, `drone_finalize_dag.py`
- The `conf` payload in `app/services/airflow_client.py` / `run_dispatch.py`
- Any Airflow REST request shape

Identity is resolved and logged **before** dispatch, at the FastAPI boundary,
and deliberately **not** forwarded into `conf`. The DAGs keep receiving the
exact same input, so the other team sees zero change.

**If you ever *did* want per-user info inside Airflow** (you don't, per the
constraint) the options would be: (a) add an optional `conf["triggered_by"]`
field — but that changes the request, so **rejected**; (b) correlate after the
fact by `dag_run_id` — the backend already gets the `dag_run_id` back from
`trigger_dag`, so store `dag_run_id → user_email` in the audit table. **Option
(b) is the recommended way to answer "which user caused this DAG run" with zero
Airflow changes.**

---

## 8. Suggested build order (incremental, each step shippable)

1. Add settings + `requirements-api.txt` deps + `SessionMiddleware`.
2. Build `auth.py` router + Google flow; test `/auth/login` → `/auth/me` in a
   browser. (`auth_enabled=False` default keeps everything else working.)
3. Swap endpoints to `require_user`; verify `Project.user_id` now = email.
4. Add audit middleware + `AuditLog` table + `dag_run_id → user` correlation.
5. Frontend: `/auth/me` gate + landing page + `credentials:"include"` +
   sign-out.
6. Tighten CORS, set `auth_enabled=True`, deploy behind HTTPS.
7. Update `CODEBASE_MAP.md` (new `auth.py` row, new `AuditLog` model, new
   endpoints) and run `python update_codebase_map.py`.

## 9. Effort / risk

- **New files:** `app/api/v1/auth.py` (~120 lines). **Edited:** `settings.py`,
  `deps.py`, `main.py`, `router.py`, `db/models.py`, `frontend/index.html`,
  `.env(.example)`, `requirements-api.txt`.
- **Risk:** low. Additive; gated behind `auth_enabled`; Airflow untouched.
- **Main gotchas:** cookie + CORS/SameSite when API and UI are different origins
  (put them behind one proxy to avoid it); keep the service-token compute
  endpoints exempt from the Google gate.
