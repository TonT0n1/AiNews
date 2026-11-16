# The AI Dispatch

A single-page AI news newsletter that rebuilds itself twice a day and keeps the
key upcoming events pinned on screen (a sticky sidebar on desktop, a sticky
strip on phones).

## How it works

- `build.py` fetches the RSS/Atom feeds in `feeds.json`, keeps the last 48 hours,
  de-duplicates, groups stories by topic and writes one self-contained HTML file.
  Standard library only.
- `events.json` is the pinned events list. Past events drop off automatically;
  countdowns update in the browser. Set `"tentative": true` to show a "TBC" tag.
- `.github/workflows/newsletter.yml` runs the build at 06:17 and 18:17 UTC and
  publishes it to GitHub Pages.

## Hosting on GitHub Pages

1. Merge this into the default branch (scheduled workflows only run from there).
2. Repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. Run the **AI newsletter** workflow once from the Actions tab, or wait for the
   next scheduled run.

To host elsewhere, run the build on any scheduler and serve the output file:

```
python3 newsletter/build.py --out _site/index.html
```

## Customising

- Add or remove sources in `feeds.json`. `"ai_only": true` keeps only AI-related
  stories from a general feed.
- Edit `events.json` to change what is pinned.
- Change the schedule in the workflow's `cron` line (times are UTC).
- Title, time window and limits are constants at the top of `build.py`.
