# Plan — inline cluster review images, past-runs links, intuitive labelling

Three UX changes. Good news: the **backend already exposes every image and
endpoint needed** — most of this is frontend-only. No Airflow change anywhere.

---

## Change 1 — Show analyze plots + per-k images inline on the frontend

Today: after analyze, the UI only prints "k available: … recommended: …" + a
"Browse output files" FileBrowser link. To actually see the plots the user must
leave the app and dig through FileBrowser. We want them inline.

### Backend — already done (no change, one optional tweak)
`app/api/v1/clustering.py` already serves everything:
- `GET /project/clustering` → `build_clustering_payload`: `available_k`,
  `recommended_k`, `k_recommendation_table`, `k_selection_plot_url`, and
  `per_k[]` each with `tsne_plot_url` + `clusters_url`.
- `GET /project/clustering/k-selection.png`
- `GET /project/clustering/{k}/tsne.png`
- `GET /project/clustering/{k}/clusters` → per-cluster `sample_crowns[]` (crown
  PNG URLs).
- `GET /project/crowns/{image_name}` → renders a crown GeoTIFF to PNG.

Optional tweak: `tsne_plot_url` / `clusters_url` / `k_selection_plot_url` are
emitted as **absolute** `{request.base_url}...` (clustering.py ~L41,60,73). For
split-origin (UI :8200 / API :8123) `<img src>` must hit the API origin. Either
(a) leave absolute and it works same-origin, or (b) make them **relative** like
we did for `detection_overlay_url` and prefix with `apiBase()` in the frontend.
Recommend (b) for consistency. Small edit, clustering.py only.

### Frontend — `index.html` (the real work)
After analyze succeeds (the `analyze()` success block ~L705), fetch
`GET /api/v1/project/clustering` and render a **review panel**:
1. **k-selection plot** inline: `<img src=apiBase()+k_selection_plot_url>` + the
   `k_recommendation_table` as a small table (already available).
2. **Per-k gallery**: for each `per_k[]` entry, a card showing the **t-SNE plot**
   inline and a "View sample crowns" toggle that lazy-fetches `clusters_url`
   (`/clustering/{k}/clusters`) and lays out each cluster's `sample_crowns` as a
   thumbnail strip (`<img>` per crown).
3. A **"Use this k"** button on each card that sets `#ck` (chosen k) and jumps to
   Step 4 — directly feeding labelling (ties into Change 3).

New helpers: `renderClusterReview(payload)`, `loadKClusters(k)` (lazy). Lazy-load
thumbnails (only when a k card is expanded) so we don't fetch hundreds of crown
PNGs up front.

**Affected:** `frontend/index.html` (markup slot in Step 3 + ~60–90 lines JS);
optional `app/api/v1/clustering.py` (relative URLs). Backend endpoints reused
as-is.

### Alternative: serve images straight from the FileBrowser public share
Instead of our image APIs, embed files directly from the existing public share
(`{filebrowser_public_url}/api/public/dl/{share_hash}/<relative path>`). Cross-
origin `<img>` needs no CORS. Verified on disk:
- **Plots** — `clustering/k_selection.png`, `clustering/tsne_k{k}.png` are already
  **PNG** on disk → **YES, embed directly from the share, drop the `tsne.png` /
  `k-selection.png` APIs.** Path:
  `.../api/public/dl/{hash}/work/run_<n>/step1_output/clustering/tsne_k{k}.png`.
- **Crowns** — the crown files are **`.tif`** (`step1_output/clustering/k{k}/
  cluster_{c}/*.tif`). Browsers **cannot render TIFF** in `<img>`; there are no
  PNG crowns on disk. So crowns **cannot** be served from FileBrowser as-is. Our
  `/crowns/{name}` API exists to render tif→png on the fly. Options:
  - **(a)** keep `/crowns/{name}` for crowns, use the share only for plots.
  - **(b)** have Step 1 also write a `.png` thumbnail next to each crown `.tif`
    (one added write in the crop step) → FileBrowser can then serve crowns too and
    all image APIs can be dropped. Cost: existing runs need re-analyze; extra disk.

Constraints for the share route: FileBrowser must be enabled and the share must
exist (created at project creation); and it couples the frontend to the internal
storage layout + crown filenames. The `/clustering/{k}/clusters` API otherwise
supplies the crown-name list + cluster grouping, so keep it (or replicate the
grouping from FileBrowser's directory listing `/api/public/share/{hash}/{path}`,
which is more frontend code).

**Recommendation:** plots → FileBrowser share (free, no API); crowns → (b) if we
want zero image APIs long-term, else (a) now.

---

## Change 2 — Past runs: prior shared FileBrowser URLs by email

Today: `GET /projects/mine` (`app/api/v1/projects.py`) already returns the
signed-in user's projects with `files_url` built from `Project.share_hash` via
`share_url()`, and the frontend `#pastRuns` panel (`loadMyProjects()`) renders
them as clickable links. With Google sign-in on, `Project.user_id` = the email,
so this **already fetches prior shared URLs by email**. Mostly complete.

### Remaining polish
- **Filter to shared/openable runs:** in `my_projects`, optionally only include
  rows where `files_url` is non-null (a public share exists), or return all but
  let the UI show a "no share yet" state. Decide product-side.
- **Confirm share persistence:** `share_hash` is created at project creation
  (`projects.py` → `filebrowser_client`) and stored on the row. Verify it isn't
  overwritten on re-runs so old runs keep working links.
- **UI:** `loadMyProjects()` already renders name + state + "Browse files →".
  Polish: sort by `updated_at` (done), show created date, and only render the
  panel when ≥1 run has a `files_url`.
- **Auth dependency:** correct attribution needs `auth_enabled=true` (else
  `user_id="default"` and everyone shares one bucket). Tie to the GIS auth
  rollout (`docs/OAUTH_GIS_INTEGRATION_PLAN.md`).

**Affected:** `frontend/index.html` (`loadMyProjects` render polish);
`app/api/v1/projects.py` (`my_projects` optional filter). No new endpoint, no DB
change (`share_hash`, `user_id` already exist).

---

## Change 3 — Make the labelling step intuitive

Today (Step 4, `index.html` ~L307–314): user must (a) type chosen k, (b) type
species names, (c) click **"Build cluster table"** to generate dropdown rows,
(d) click **"Submit labels"**. The two-button "build then submit" is unintuitive
and the table shows only cluster numbers — no crown images to label against.

### Target flow (single, guided)
1. **Auto-build the table** — drop the "Build cluster table" button. Rebuild the
   rows **reactively** whenever `#ck` (chosen k) or `#species` changes
   (`oninput`), and auto-populate `#ck` when the user clicks "Use this k" in the
   Change-1 review. So the table just *appears*; only **"Submit labels"** remains.
2. **Show crowns in each row** — for the chosen k, fetch `/clustering/{k}/clusters`
   and render each cluster row with its `sample_crowns` thumbnails beside the
   species `<select>`. User labels while looking at the actual crowns (merges the
   review + label steps the info-box currently tells them to do manually in
   FileBrowser).
3. **Species entry** — keep the comma-separated names box to populate the
   dropdowns, and allow a free-text option per row for one-offs.
4. Keep submit semantics identical: build the same `cluster,species,notes` CSV
   and `POST /api/v1/project/labels` (unchanged) — only the UX around it changes.

### Code
- Rework `buildClusterRows()` → `renderLabelTable()` that (a) reads k, (b) fetches
  cluster thumbnails once, (c) renders rows with images + selects. Call it from
  `#ck`/`#species` `oninput` and from "Use this k".
- Remove the `onclick="buildClusterRows()"` button; keep `submitLabels()`.
- `submitLabels()` stays as-is (same CSV, same endpoint).

**Affected:** `frontend/index.html` (Step 4 markup + `buildClusterRows`/
`submitLabels` JS). Backend: none — reuses `/clustering/{k}/clusters` and
`/project/labels`.

---

## Summary of impact

| Change | Backend | Frontend | DB | Airflow |
|---|---|---|---|---|
| 1 — inline plots/images | none (opt: relative URLs in `clustering.py`) | index.html: review panel + JS | none | none |
| 2 — past runs by email | opt filter in `projects.py::my_projects` | index.html: `loadMyProjects` polish | none | none |
| 3 — intuitive labelling | none | index.html: Step 4 rework | none | none |

All three are frontend-dominant and reuse existing endpoints. Sequence: **3**
(labelling clarity, self-contained) → **1** (review gallery, shares the
`/clusters` fetch with 3) → **2** (past-runs polish, needs auth for real email
attribution). Best done after the GIS auth rollout so `user_id` = email.
