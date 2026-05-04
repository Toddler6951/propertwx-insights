# PropertyWX Insights

Open analytics from the NOAA NCEI Storm Events Database, served at
[insights.propertywx.com](https://insights.propertywx.com).

This repo is the **content / data side** of [propertywx.com](https://propertywx.com)
— the live tool lives at [Toddler6951/storm-claim-tool](https://github.com/Toddler6951/storm-claim-tool).

## What's inside

| File | Purpose |
| --- | --- |
| `index.html` | Landing page with national-level analytics (events by year, month, state rankings, top events). |
| `state.html` | Per-state page (deep link via `?s=TX`). Shows year/month/county breakdowns and notable events for that state. |
| `build_insights.py` | Python script that processes per-state SED CSVs into `insights.json` + per-state JSON files. |
| `.github/workflows/refresh-insights.yml` | Action that runs the script monthly and commits the data. |
| `data/insights.json` | National-level analytics (auto-generated). |
| `data/by-state/<STATE>.json` | Per-state analytics (auto-generated). |

## How it works

1. The Action runs monthly (1st at 07:30 UTC, after `storm-claim-tool` refreshes its SED data at 06:00 UTC).
2. It checks out both this repo and the `Toddler6951/storm-claim-tool` repo (sparse, just `sed-data/`).
3. `build_insights.py` reads the per-state SED CSVs and emits `data/insights.json` and `data/by-state/<STATE>.json`.
4. The committed JSON is served from GitHub Pages at `insights.propertywx.com`.
5. The HTML pages fetch the JSON and render charts client-side via Plotly.

## Deployment

1. Create this repo on GitHub.
2. Settings → Pages → Source: *Deploy from a branch*, Branch: `main` / `(root)`.
3. Add a `CNAME` record at your DNS (Squarespace): `insights` → `Toddler6951.github.io`.
4. Settings → Pages → Custom domain: `insights.propertywx.com`. Wait for DNS check, enable HTTPS.
5. The first push triggers the Action; ~2–5 minutes later `data/insights.json` is committed and the site goes live.

## Local development

```bash
python build_insights.py --src /path/to/storm-claim-tool/sed-data --out data
python -m http.server 8000
# Open http://localhost:8000/
```

## Roadmap

- **Phase 1** (this repo): national overview + per-state pages from JSON ✓
- **Phase 2**: long-form articles (climatology overviews, major-event recaps) using Astro/MDX
- **Phase 3**: programmatic per-county landing pages for long-tail SEO
