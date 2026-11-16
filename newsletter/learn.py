#!/usr/bin/env python3
"""
Build "The AI Classroom": a staged programme of free AI tutorials.

The programme itself is the hand-curated curriculum.json. Each build checks every
link and leaves out any that have died, and adds a "new lessons" strip from the
teaching channels and blogs in learn_feeds.json.

    python newsletter/learn.py --out _site/learn/index.html
"""

import argparse
import json
import re
import socket
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import build
from build import esc

HERE = Path(__file__).parent
TITLE = "The AI Classroom"
FRESH_DAYS = 30          # how far back the "new lessons" strip looks
FRESH_PER_SOURCE = 2
FRESH_TOTAL = 12
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")


# --------------------------------------------------------------------------
# Link checking
# --------------------------------------------------------------------------

def check_link(url):
    """Return (state, detail). state is "ok", "dead" (drop it) or "unknown" (keep it:
    many sites refuse automated requests, which says nothing about the page)."""
    host = urlsplit(url).netloc.lower()
    is_video = "youtube.com" in host and ("watch?" in url or "playlist?" in url)
    target = url
    if is_video:  # YouTube's oEmbed endpoint says whether a video or playlist exists
        target = "https://www.youtube.com/oembed?format=json&url=" + quote(url, safe="")
    req = urllib.request.Request(target, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if is_video:
                return "ok", json.loads(resp.read()).get("title", "")
            return "ok", ""
    except urllib.error.HTTPError as exc:
        return ("dead" if exc.code in (400, 404, 410) else "unknown"), f"HTTP {exc.code}"
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, socket.gaierror):
            return "dead", "site not found"
        return "unknown", str(exc.reason)
    except Exception as exc:  # timeouts, bad JSON
        return "unknown", type(exc).__name__


def check_curriculum(curriculum):
    items = [item for stage in curriculum["stages"] for item in stage["items"]]
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda item: check_link(item["url"]), items))
    dead = 0
    for item, (state, detail) in zip(items, results):
        item["dead"] = state == "dead"
        dead += item["dead"]
        if state == "ok" and detail:
            print(f"  video {item['id']}: {detail}", file=sys.stderr)
        elif state != "ok":
            print(f"  {state.upper():7} {item['id']}: {detail}  {item['url']}", file=sys.stderr)
    print(f"Checked {len(items)} links: {dead} dead and left out", file=sys.stderr)


# --------------------------------------------------------------------------
# New lessons
# --------------------------------------------------------------------------

def fetch_lessons(feed):
    req = urllib.request.Request(feed["url"], headers={"User-Agent": build.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            data = resp.read()
        lessons = build.parse_feed(data, feed["name"])
        root = ET.fromstring(data)
        feed_title = next((("".join(n.itertext())).strip() for n in root.iter()
                           if build._local(n.tag) == "title"), "")
    except Exception as exc:
        print(f"  FAIL {feed['name']}: {exc}", file=sys.stderr)
        return []
    if feed.get("ai_only"):
        lessons = [s for s in lessons if build.AI_RE.search(s["title"] + " " + s["summary"])]
    print(f"  ok   {feed['name']} (feed title: {feed_title[:40]!r}): {len(lessons)}", file=sys.stderr)
    return lessons


def collect_lessons(feeds, now):
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(fetch_lessons, feeds))
    cutoff = now - timedelta(days=FRESH_DAYS)
    lessons = sorted((s for b in batches for s in b if cutoff <= s["when"] <= now + timedelta(hours=1)),
                     key=lambda s: s["when"], reverse=True)
    kept, per_source, seen = [], {}, set()
    for s in lessons:
        key = build._title_key(s["title"])
        if key in seen or per_source.get(s["source"], 0) >= FRESH_PER_SOURCE:
            continue
        if "#shorts" in s["title"].lower() or "/shorts/" in s["url"]:
            continue
        seen.add(key)
        per_source[s["source"]] = per_source.get(s["source"], 0) + 1
        kept.append(s)
        if len(kept) == FRESH_TOTAL:
            break
    return kept


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def render_item(item):
    level = item["level"]
    return (
        f'<li class="item" data-id="{esc(item["id"])}" data-level="{esc(level)}">'
        f'<input type="checkbox" aria-label="Mark as done: {esc(item["title"])}">'
        f'<div class="item-main">'
        f'<a class="item-title" href="{esc(item["url"])}" target="_blank" rel="noopener noreferrer">'
        f'{esc(item["title"])}</a>'
        f'<p class="by">{esc(item["by"])}</p>'
        f'<p class="why">{esc(item["why"])}</p>'
        f'<p class="tags"><span class="tag fmt">{esc(item["format"])}</span>'
        f'<span class="tag lvl lvl-{esc(level.lower())}">{esc(level)}</span>'
        f'<span class="tag">{esc(item["time"])}</span></p>'
        f'</div></li>'
    )


def render(curriculum, lessons, now, template):
    stages = [dict(s, items=[i for i in s["items"] if not i.get("dead")]) for s in curriculum["stages"]]
    stages = [s for s in stages if s["items"]]
    number = {s["id"]: n for n, s in enumerate(stages, 1)}

    stage_html, nav_html = [], []
    for s in stages:
        n = number[s["id"]]
        nav_html.append(
            f'<li><a href="#{esc(s["id"])}" data-stage="{esc(s["id"])}">'
            f'<span class="num">{n}</span><span class="nav-title">{esc(s["title"].split(":")[0])}</span>'
            f'<span class="count" data-count="{esc(s["id"])}">0/{len(s["items"])}</span></a></li>'
        )
        stage_html.append(
            f'<section class="stage" id="{esc(s["id"])}">'
            f'<p class="stage-num">Stage {n}</p><h2>{esc(s["title"])}</h2>'
            f'<p class="blurb">{esc(s["blurb"])}</p>'
            f'<p class="outcome"><b>By the end:</b> {esc(s["outcome"])}</p>'
            f'<ol class="items">' + "".join(render_item(i) for i in s["items"]) + '</ol></section>'
        )

    routes = []
    for r in curriculum["routes"]:
        steps = " → ".join(
            f'<a href="#{esc(sid)}">{number[sid]}</a>' for sid in r["stages"] if sid in number)
        ids = " ".join(sid for sid in r["stages"] if sid in number)
        routes.append(
            f'<li class="route"><h3>{esc(r["name"])}</h3><p>{esc(r["for"])}</p>'
            f'<p class="steps">Stages {steps}</p>'
            f'<button type="button" data-route="{esc(ids)}">Show only this route</button></li>'
        )

    lesson_html = "".join(
        f'<li><a href="{esc(s["url"])}" target="_blank" rel="noopener noreferrer">{esc(s["title"])}</a>'
        f'<span class="meta"><span class="source">{esc(s["source"])}</span>'
        f'<time datetime="{s["when"].isoformat()}" data-rel>{s["when"]:%-d %b}</time></span></li>'
        for s in lessons
    )
    total = sum(len(s["items"]) for s in stages)
    values = {
        "title": esc(TITLE),
        "built_iso": now.isoformat(),
        "built_text": f"{now.day} {now:%B %Y}",
        "total": str(total),
        "stage_count": str(len(stages)),
        "nav": "".join(nav_html),
        "routes": "".join(routes),
        "stages": "".join(stage_html),
        "lessons": (f'<section id="new"><h2>New lessons</h2><p class="blurb">Recent videos and posts '
                    f'from good teachers. Refreshed twice a day.</p><ul class="lessons">{lesson_html}'
                    f'</ul></section>') if lessons else "",
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_site/learn/index.html")
    parser.add_argument("--curriculum", default=HERE / "curriculum.json")
    parser.add_argument("--feeds", default=HERE / "learn_feeds.json")
    parser.add_argument("--no-check", action="store_true", help="skip the link check")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    curriculum = json.loads(Path(args.curriculum).read_text(encoding="utf-8"))
    if not args.no_check:
        check_curriculum(curriculum)
    feeds = json.loads(Path(args.feeds).read_text(encoding="utf-8"))
    print(f"Fetching {len(feeds)} lesson feeds…", file=sys.stderr)
    lessons = collect_lessons(feeds, now)

    page = render(curriculum, lessons, now,
                  (HERE / "learn_template.html").read_text(encoding="utf-8"))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"Wrote {out}: {len(lessons)} new lessons", file=sys.stderr)


if __name__ == "__main__":
    main()
