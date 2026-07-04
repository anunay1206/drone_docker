# OAuth via Google Identity Services (GIS) — adopting the other team's flow

Assessment of `OAuth_Implementation_Details.md` (the Biocautic/cem flow) and how
much drops into ours. Landing page chosen: `frontend/landing_samples/landing_I_grid.html`.

## TL;DR

Their model is a **client-side GIS token flow** with **header-based audit**, not
a server-side redirect/session flow. It is **lighter** than the Authlib plan in
`docs/GOOGLE_OAUTH_PLAN.md`: no backend OAuth dependency, no redirect routes, no
session cookie, no `SessionMiddleware`, no CORS/SameSite rework. ~80% of it drops
straight into our stack. The one real decision is **security** (see §4).

## 1. Their flow vs ours

| | Their flow (GIS token model) | My earlier plan (Authlib) |
|---|---|---|
| Where login happens | Frontend popup (`google.accounts.oauth2.initTokenClient`) | Backend redirect `/auth/login` → Google → `/auth/callback` |
| Token storage | In memory only (no cookie/localStorage) | Signed session cookie |
| Backend role | Reads `X-User-Email` / `X-User-Id` headers, **does not verify** | Verifies `id_token`, owns session |
| Identity to backend | Custom headers per request | Session cookie per request |
| Audit | Append to `activity-YYYY-MM-DD.jsonl` | AuditLog table / middleware |
| Backend deps added | none | authlib, itsdangerous |
| Effort | low | medium |

## 2. What drops in directly (reuse ~as-is)

- **GIS script + token client.** `<script src="https://accounts.google.com/gsi/client" async defer>`
  + `initTokenClient({client_id, scope, callback})`. (This is a browser-loaded
  Google SDK, not a bundled lib — it loads client-side, fine behind nginx/Docker.
  It IS an external script from `accounts.google.com`, required for Google login.)
- **`_fetchUserInfo()`** → `GET https://www.googleapis.com/oauth2/v3/userinfo`
  with the bearer token → `email` + `sub` (stable id).
- **`authHeaders()`** → inject `X-User-Email` + `X-User-Id` on backend calls.
- **Silent refresh** (`ensureValidToken`, `prompt:''`) — only needed if we call
  Google APIs directly (Drive/Picker). For pure login+audit we can skip it; the
  token just needs to be alive long enough to read userinfo once.
- **Backend `_user` dependency** (their §F) → maps almost 1:1 onto our
  `app/api/deps.py`.

## 3. Parts WE touch

### Frontend (single-file, no build system — inline JS)
1. **`index.html`** (post-login app):
   - Add the `gsi/client` script tag.
   - Add an inline `AuthService` (init token client, request token, fetch
     userinfo, hold `userEmail`/`userId` in memory).
   - **`headers()`** (currently returns `{}` after we removed X-API-Key): inject
     `X-User-Email` + `X-User-Id`. Drop the now-unneeded `credentials:"include"`
     (that was for the cookie model we're not using).
   - Gate: on load, if not authenticated → show the landing; on login success →
     reveal the pipeline UI + call `loadMyProjects()`.
2. **`landing_I_grid.html`**: its button is currently `<a href="/auth/login">`
   (redirect-model). Change to a JS trigger calling `requestAccessToken()`. Either
   merge the landing markup into `index.html` (one file, toggle views) or keep it
   separate and, on success, navigate to the app. **Merge is simpler** for the
   in-memory token (a full-page nav would drop the in-memory token; so the app +
   login must live on the same page/load). → **Fold `landing_I_grid` into
   `index.html` as a pre-auth overlay.**
3. **Client ID config**: no build script like their `generate_config.sh`. Use
   `window.GOOGLE_CLIENT_ID` set in a tiny inline script (or hardcode). Scope:
   `openid email profile` (add `https://www.googleapis.com/auth/drive.file` ONLY
   if we also adopt their Drive Picker — see §5).

### Backend
1. **`app/api/deps.py`**: add `require_user` reading the headers (their `_user`):
   ```python
   def require_user(
       x_user_email: str | None = Header(default=None, alias="X-User-Email"),
       x_user_id: str | None = Header(default=None, alias="X-User-Id"),
   ) -> str:
       if settings.auth_enabled and not x_user_email:
           raise HTTPException(401, {"code":"UNAUTHENTICATED","message":"Sign-in required"})
       return x_user_email or "default"
   ```
   Swap human endpoints from `require_api_key` → `require_user`. `Project.user_id`
   becomes the email (column already exists).
2. **Audit log**: add `app/services/activity_log.py` — append JSONL
   `{ts, email, user_id, action, method, path, project_id}` to
   `data/storage/activity-YYYY-MM-DD.jsonl` (mirrors their ledger), OR reuse the
   `AuditLog` table idea in `docs/LOGGING_IMPROVEMENTS.md`. Write it from one HTTP
   middleware (cheapest) or per-endpoint.
3. **CORS**: current `allow_origins=["*"]` + `allow_headers=["*"]` already permits
   the custom headers, and there are **no cookies** in this model → no CORS change
   needed. (Big win vs the Authlib plan.)
4. **Settings**: add `google_client_id`, `auth_enabled` (default False so dev/tests
   keep working). No client secret needed (client-side flow).

### Airflow — UNCHANGED
Headers are frontend→backend only. The DAG→backend compute calls keep
`require_service_token`. Trigger conf + DAG request bodies untouched. Confirmed.

## 4. The one real decision — security

Their backend **trusts** `X-User-Email`/`X-User-Id` without verifying. Anyone can
`curl -H "X-User-Email: x@y.com"` and impersonate. Their own doc flags this: only
safe **behind a gateway/private network**, or for **audit-only** (non-security).

Our requirement was "only allow access **after** Google sign-in." Client-side-only
headers **cannot enforce** that — a crafted request bypasses the UI gate.

Two options:
- **(a) Audit-only (their model as-is).** Cheapest. Login gates the *UI*; headers
  label the audit log. Acceptable if the API sits on a trusted/internal network or
  behind a proxy that authenticates. Least code.
- **(b) Verify on the backend (recommended if the API is exposed).** Frontend
  sends `Authorization: Bearer <access_token>` (or an `id_token`); backend verifies
  once via `https://oauth2.googleapis.com/tokeninfo` or the `google-auth` library,
  extracts the email server-side, and both gates AND audits. ~15 extra backend
  lines + `google-auth` dep. This is the meaningful upgrade over their doc.

Pick (a) for a quick internal rollout; (b) if the backend is publicly reachable.

> **DECISION (2026-07-02):** Option **(a) audit-only**. Backend is
> **internal / behind a gateway**, so header-trust is acceptable. No backend
> token verification, no `google-auth` dep. Revisit and move to (b) if the API
> is ever exposed publicly.

## 5. Optional extra they have, we don't

Their `PickerService` / `drive.file` scope powers a **Google Drive Picker** to
select project data. We already ingest a Drive file via
`POST /project/orthomosaic/from-url` (paste a link). Adopting the Picker is a UX
upgrade (browse instead of paste) but adds `PickerService`, a browser API key, and
the `drive.file` scope. **Out of scope for the auth task** — revisit later.

## 6. Change size

- **Frontend:** `gsi/client` script + ~70 lines inline AuthService + fold
  `landing_I_grid` into `index.html` as pre-auth overlay + `headers()` tweak.
- **Backend:** `require_user` (~10 lines) + `activity_log.py` (~25) + 1 middleware
  or per-endpoint wiring + 2 settings. Option (b) adds a verify call + `google-auth`.
- **No** SessionMiddleware, redirect routes, cookie/CORS rework, or Airflow change.

Net: **notably smaller than `docs/GOOGLE_OAUTH_PLAN.md`.** If we don't need
server-verified access control, this replaces that plan; if we do, take §4(b).

## 7. Suggested order
1. Backend `require_user` + settings (`auth_enabled=False` default).
2. `activity_log.py` + middleware.
3. Frontend: gsi script, AuthService, `headers()` inject, fold landing_I_grid in.
4. (If exposed) §4(b) token verification + `google-auth`.
5. Flip `auth_enabled=True`, set client ID, test popup → userinfo → gated app.
