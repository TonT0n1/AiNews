# The AI Dispatch

A single-page AI news newsletter that rebuilds itself twice a day and keeps the
key upcoming events pinned on screen (a sticky sidebar on desktop, a sticky
strip on phones).

## How it works

- `build.py` fetches the RSS/Atom feeds in `feeds.json`, keeps the last 48 hours,
  de-duplicates, puts launches and projects in a "Tools & builds" block at the top, groups the rest by topic and writes one self-contained HTML file.
  Standard library only, apart from the optional AI step.
- `events.json` is the pinned events list. Past events drop off automatically;
  countdowns update in the browser. Set `"tentative": true` to show a "TBC" tag.
- `.github/workflows/newsletter.yml` runs the build at 06:17 and 18:17 UTC and
  publishes it to GitHub Pages.

## The AI Classroom (learning page)

A second page, published at `/learn/`, is a staged programme of free AI tutorials: from what
a neural network is, through how language models are built and fine-tuned, to building agents and the hardware
and Kubernetes infrastructure they run on.

- `newsletter/curriculum.json` is the hand-picked programme: stages, the resources in each, and
  the three suggested routes through them. Edit this file to add, remove or reorder resources.
- `newsletter/learn.py` builds the page. On every run it checks each link and leaves out any
  that have died, and adds a "New lessons" strip from the channels and blogs in
  `newsletter/learn_feeds.json`.
- Readers tick items off as they go; progress is stored in their own browser.

## AI editing (optional)

With an API key, each build also asks an AI model to write a short summary of every story,
rank them (the top block becomes "Top stories"), choose their section, and merge several
outlets' coverage of the same event into one entry. Summaries are written only from the text
publishers put in their feeds, not the full articles.

To switch it on, add a repository secret named `OPENAI_API_KEY`
(Settings → Secrets and variables → Actions → New repository secret). It uses the light
`gpt-5.4-mini` model by default; to use a different one, add a repository *variable* named
`OPENAI_MODEL` on the Variables tab of the same page. An `ANTHROPIC_API_KEY` secret works
too and uses Claude. Without a key, or if the API call fails, the page is built from the
publishers' own text as before.

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
  stories from a general feed; `"aggregator": true` marks a feed that
  relays other publishers (Google News, Techmeme) so stories are credited to the real source; `"section": "tools"` sends a feed to the Tools & builds block.
- Edit `events.json` to change what is pinned.
- Change the schedule in the workflow's `cron` line (times are UTC).
- Title, time window and limits are constants at the top of `build.py`.
