# Feature Plan — SSO landing, STAC id/latlong fixes, overlay path, consent capture

Minimal-change plan. Each task maps to exact files. No Airflow DAG / conf change
anywhere (backend + frontend only). SQLite note: `init_db()` only *creates* new
tables, it does not ALTER existing ones — any new column on `projects` needs a
one-line `ALTER TABLE` (or drop the dev `treecrown.db`). New *tables* are created
automatically.

---

## Task 1 — Landing page: SSO + list past public FileBrowser URLs from DB

**Reuse** the OAuth landing from `docs/GOOGLE_OAUTH_PLAN.md` (the `/auth/me` gate
in `frontend/index.html`). Add to it a "your past runs" list built from the DB.

Backend — new endpoint (`app/api/v1/projects.py` or a small `mine.py`):
```
GET /api/v1/projects/mine   (depends on require_user)
-> [{project_id, name, state, files_url}]
```
Build `files_url` exactly like `results.py:49-52`:
```python
from app.services.filebrowser_client import filebrowser_enabled, share_url
hash_ = project.share_hash
files_url = share_url(hash_) if (filebrowser_enabled() and hash_) else None
```
Query `Project` filtered by `user_id == <email>` (the `user_id` column already
exists; OAuth plan makes it the signed-in email). `share_hash` already persisted
per project (`db/models.py:39`) — no schema change.

Frontend: on the landing page after sign-in, call `/projects/mine`, render each
prior project as a link to its `files_url`. Pure additive.

**Files:** `frontend/index.html`, new `projects/mine` route. **DB:** none.

---

## Task 2 — STAC id: append `_analyze` / `_finalize`

Today `build_stac_item` (`app/services/stac.py:~138`) builds one id:
```python
item_id = f"{_slug(project.name) or 'tree_crown'}_{project.id[:8]}_run{run}"
```
Add a `stage` param, suffix it:
```python
def build_stac_item(project, chosen_k=None, run=None, stage: str | None = None):
    ...
    item_id = f"{_slug(project.name) or 'tree_crown'}_{project.id[:8]}_run{run}"
    if stage:
        item_id += f"_{stage}"      # -> ..._run1_analyze / ..._run1_finalize
```
Thread `stage` through the two call paths in `app/services/assets.py`:
- `analyze_asset_fields` / `stac_response` → pass `stage="analyze"`.
- finalize path → pass `stage="finalize"`.

The two callers are the compute callbacks in `app/api/v1/compute.py`
(`compute_analyze` → analyze, `compute_finalize` → finalize) via `_ok()` →
`asset_response_fields`. Add a `stage` arg down that chain (default `None` keeps
every other caller unchanged).

**Files:** `stac.py`, `assets.py`, `compute.py`. **DB:** none.

---

## Task 3 — overlay link: emit the storage-relative FILE path only

`app/api/v1/clustering.py:61` emits an **absolute API URL**:
```python
base = str(request.base_url).rstrip("/")           # line 41
"detection_overlay_url": f"{base}/api/v1/project/detection/overlay.png",
```
Wanted: the storage-relative **file** path, not an API route:
```
projects/<project_id>/work/run_<n>/detectree/S3C/overlay.png
```
(i.e. `<storage_root>` + that = `data/drone_data/storage/projects/.../overlay.png`).
`relative_artifact_path()` (`storage.py:24`) produces exactly this — same helper
the STAC geojson asset uses.

Fix — resolve the real overlay file (same logic as `overlay_png`, lines 154-160),
then emit its relative path; **no `{base}`, no API route**:
```python
from app.core.storage import relative_artifact_path
det = project_paths(project.id, _run(project))["detectree"]
subs = sorted(d for d in os.listdir(det) if os.path.isdir(os.path.join(det, d))) if os.path.isdir(det) else []
f = os.path.join(det, subs[0], "overlay.png") if subs else ""
overlay_rel = relative_artifact_path(f) if (f and os.path.exists(f)) else None
...
"detection_overlay_url": overlay_rel,   # projects/<id>/work/run_<n>/detectree/S3C/overlay.png
```
`overlay_rel` is `None` when the file is absent (avoids emitting a dead link).
Apply the same `relative_artifact_path` treatment to `tsne_plot_url`,
`clusters_url`, `k_selection_plot_url` (lines 46-47, 59) if those should also be
storage-relative file paths — confirm scope with Saurav.

The HTTP endpoint `overlay_png` (lines 150-166) can be **removed** if nothing
fetches the image over HTTP anymore; otherwise leave it, it's independent of the
emitted link.

**Files:** `clustering.py`. **DB:** none.

---

## Task 4 — Send lat/long to Airflow after finalize as a proper STAC item

Coords already exist: `build_stac_item` computes WGS84 `bbox` + `geometry` via
`_footprint_wgs84(paths["input_ortho"])` (`stac.py:150`, reprojects the TIFF's
`src.crs`/`src.bounds` to `EPSG:4326`). The finalize compute 200 body already
carries the full item under `stac` (`compute.py :: _ok` → `asset_response_fields`
→ `stac_response`). So the item flows **backend → DAG** in the finalize response.

Two things to make it "correct + proper":

1. **Guarantee the footprint is populated at finalize.** `_footprint_wgs84`
   returns `(None, None)` if `input_ortho` dir has no readable TIFF or `src.crs`
   is None. At finalize the ortho must still be on disk. Add a fallback: if bbox
   is None, derive from `project.source_epsg` + the crown GeoJSON bounds
   (already reprojected to 4326 in `tree_crown_pipeline.py:764`). Prevents a
   STAC item with `geometry: null`.
2. **Apply Task 2 stage suffix** so the finalize item id ends `_finalize`.

Constraint honored: the DAG's `call_analyze`/finalize task only reads
`data.get("asset_id")` and xcom-pushes it — it *ignores* `stac`. So including a
correct STAC item in the response does **not** change the DAG request contract;
whatever downstream "stackbox"/catalog reads the response gets a valid item. No
DAG or conf edit.

> If the real requirement is that the STAC item must reach an external STAC
> catalog ("stackbox") rather than just sit in the response, do it backend-side:
> POST the item from `job_b_finalize` (or right after `write_stac_item`) to the
> catalog URL. Still no Airflow change. Confirm the target endpoint with Saurav.

**Files:** `stac.py` (footprint fallback), `assets.py`/`compute.py` (stage).
**DB:** none.

---

## Task 5 — Consent capture after finalize, stored in DB

**Values:** `0` = no / not given (default), `1` = yes, all data, `2` = yes,
unlabelled only (Step-1 output only).

Schema — add one column to `Project` (`app/db/models.py`):
```python
consent: Mapped[int] = mapped_column(Integer, default=0)   # 0 no | 1 all | 2 unlabelled-only
consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
```
(SQLite: run `ALTER TABLE projects ADD COLUMN consent INTEGER DEFAULT 0;` +
`consent_at`, or drop dev DB.)

Endpoint — new, in `app/api/v1/results.py` or `finalize.py`:
```
POST /api/v1/project/consent   body: {"consent": 0|1|2}   (require_user)
-> validates project.state == COMPLETED, sets project.consent + consent_at
```
Return `consent` inside `build_results_payload` (`results.py:53`) so the UI can
show current state.

Frontend — after finalize completes (results screen in `index.html`), show a
consent box with 3 radio options and a Submit that POSTs `/project/consent`.

**Consent box copy (exact intent to display):**
> Your uploaded data and results are retained for a fixed period for policy /
> compliance reasons only. By default they are **not** used for training or any
> other purpose. If you grant consent below, the selected data will be made
> **publicly available for training and viewing**.
>
> - **No** — do not make my data public (retained for the fixed policy period only).
> - **Yes, all data** — make all data (including labelled crowns) public for training and viewing.
> - **Yes, unlabelled only** — make only the Step-1 unlabelled crown data public.

**Files:** `db/models.py`, `results.py` (endpoint + payload field),
`frontend/index.html`. **DB:** +2 columns on `projects`.

---

## Cross-cutting notes

- **Airflow untouched:** Tasks 2/4 change only the *response body* the backend
  returns; the DAG reads `asset_id` only. Tasks 1/3/5 are backend+frontend. Zero
  DAG/conf edits — constraint satisfied.
- **Auth dependency:** Tasks 1 and 5 need real user identity → they build on the
  OAuth work (`require_user`, `Project.user_id = email`). If OAuth not yet in,
  they still work single-tenant with `user_id="default"`.
- **DB migrations:** only Task 5 touches an existing table. Everything else is
  additive or column-free.

## Suggested order
1. Task 3 (one-line, no deps).
2. Task 2 (stac id stage) → unblocks Task 4's `_finalize` suffix.
3. Task 4 (footprint fallback + verify finalize response item).
4. Task 5 (consent column + endpoint + UI).
5. Task 1 (landing "past runs" list; needs OAuth `user_id`).
6. Update `CODEBASE_MAP.md` + run `python update_codebase_map.py`.
