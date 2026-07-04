# Plan — Change 3 (labelling UX) + parameter info + FAQ refresh

All three are frontend-only (`frontend/index.html`), additive, no backend / API /
FileBrowser / Airflow dependency. The crown-thumbnail part of the original
Change 3 (which needs `/clusters` or FileBrowser) is **deferred** —
`docs/FRONTEND_REVIEW_LABEL_UX_PLAN.md` still tracks it.

## A. Change 3 — intuitive labelling (no image dependency)
Today (Step 4): type chosen k → type species → click **"Build cluster table"** →
click **"Submit labels"**. The two-button "build then submit" is the confusing
part.

Change:
- **Drop the "Build cluster table" button.** Render the cluster rows
  **reactively** whenever chosen k (`#ck`) or species (`#species`) changes
  (`oninput`). Table just appears; only **"Submit labels"** remains.
- `#ck` is already prefilled with the recommended k on analyze success — so the
  table auto-appears right after analysis with sensible defaults.
- Add one line of helper text explaining: set k → assign a species to each
  cluster (leave "unlabelled" to exclude) → Submit.
- Keep `submitLabels()` and the `POST /project/labels` CSV exactly as-is.

Code: `buildClusterRows()` stays but is triggered from `#ck`/`#species`
`oninput` (debounced-ish; it's cheap) instead of a button. Guard: render nothing
until k ≥ 1. Remove the `onclick="buildClusterRows()"` button element.

## B. Parameter info (advanced params in Step 3)
The `Parameters` block (tile_size, buffer, img_size, iou_threshold,
conf_threshold, pca_components, batch_size, k_list) has no explanation of what
each does — only advanced users touch them, but there's zero guidance.

Change: add a compact always-visible muted **hint under each label** (`.hint`
class), plus a one-liner that defaults are fine for most users. Copy:
- **tile_size** — detection tile edge (m); smaller = finer, more tiles, slower.
- **buffer** — tile overlap (m) so crowns on edges aren't cut in half.
- **img_size** — crop resized to this many px before DINOv2 (224 is standard).
- **iou_threshold** — merge overlapping crown boxes above this (0–1); higher = fewer merges.
- **conf_threshold** — keep detections above this confidence (0–1); higher = fewer, surer crowns.
- **pca_components** — feature dims kept before clustering; lower = faster, coarser.
- **batch_size** — crops processed per batch; lower it if you hit out-of-memory.
- **k_list** — candidate cluster counts to try; the app recommends one to label.

Small CSS: `.hint{font-size:11px;color:var(--muted);margin-top:2px;display:block}`.

## C. FAQ refresh
Improve/extend the sidebar FAQ:
- Fix stale references (sign-in via Google, no more API/Step-0/activity-log).
- New item **"What do the analysis parameters mean?"** — mirrors the §B glossary
  so users get it without opening the params block.
- New item **"Project vs run — can I re-run with different parameters?"**
  (re-runs change params/labels on the same ortho; new project for new data).
- New item **"What outputs do I get?"** (KMZ, CSVs, plots, cluster images).
- Tighten wording on existing items; keep the "How do I pick k?" item but note the
  app recommends a starting k and the label table appears automatically.

## Affected
Only `frontend/index.html`: Step 4 markup + labelling JS (A), Step 3 Parameters
markup + `.hint` CSS (B), FAQ aside (C). No backend, DB, or Airflow change.
