#!/usr/bin/env python3
"""
Build the AI news newsletter as a single self-contained HTML file.

Pulls headlines from the RSS/Atom feeds in feeds.json, groups them by topic,
and pins the upcoming events from events.json. Standard library only.

    python newsletter/build.py --out _site/index.html
"""

import argparse
import os
import html
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).parent

TITLE = "The AI Dispatch"
WINDOW_HOURS = 48        # how far back headlines go
NEW_HOURS = 12           # "new since the last edition"
MAX_PER_SOURCE = 8       # stop one busy feed drowning the rest
LEAD_COUNT = 6           # stories in the "Latest" block
MAX_PER_TOPIC = 14
MAX_TOOLS = 8            # stories in the "Tools & builds" block
TOOLS_WINDOW_HOURS = 96  # launches are rarer, so look back further
SUMMARY_CHARS = 600     # length of the summary shown when a story is opened
MAX_EVENTS = 10
MIN_ITEMS = 5            # fewer than this and the build fails, keeping the old page
USER_AGENT = "Mozilla/5.0 (compatible; ai-newsletter-bot/1.0)"

AI_RE = re.compile(
    r"\b(AI|A\.I\.|AGI|LLMs?|GPT[\w.-]*|artificial intelligence|machine learning|"
    r"deep learning|neural|chatbots?|generative|OpenAI|Anthropic|Claude|Gemini|"
    r"DeepMind|Mistral|Llama|Copilot|ChatGPT|Hugging Face|xAI|Grok|agentic)\b",
    re.I,
)

# First match wins, so order matters.
TOPICS = [
    ("Policy & safety", re.compile(
        r"\b(regulat\w*|law(s|suit|makers?)?|legislat\w*|senat\w*|congress|parliament|"
        r"court|judge|sue[sd]?|suing|copyright|ban(s|ned)?|govern\w*|policy|"
        r"executive order|AI Act|safety|privacy|antitrust|ftc|watchdog|treaty|"
        r"election|deepfakes?|misinformation)\b", re.I)),
    ("Business & funding", re.compile(
        r"\b(rais(es|ed|ing)|funding|valuation|valued|acqui\w*|ipo|revenue|"
        r"\$?\d+(\.\d+)?\s?(billion|million|bn|m)\b|invest\w*|startups?|deal|"
        r"layoffs?|earnings|shares|stock|merger|partnership|data cent(er|re)s?)\b", re.I)),
    ("Research", re.compile(
        r"\b(paper|study|studies|research\w*|benchmarks?|arxiv|scientists?|"
        r"breakthrough|dataset|experiment\w*|interpretability|theorem|protein)\b", re.I)),
    ("Models & products", re.compile(
        r"\b(launch\w*|releas\w*|models?|announc\w*|rolls? out|rolling out|features?|"
        r"agents?|updates?|open[- ]source|preview|available|introduc\w*|unveil\w*|"
        r"app|api|chips?|gpus?)\b", re.I)),
]
OTHER_TOPIC = "More headlines"


# --------------------------------------------------------------------------
# Feeds
# --------------------------------------------------------------------------

def _local(tag):
    return tag.rsplit("}", 1)[-1].lower()


def _clean(text, limit=None):
    """Strip markup and collapse whitespace."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        cut = text[:limit]
        stop = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        if stop > limit * 0.5:  # prefer ending on a whole sentence
            text = cut[:stop + 1]
        else:
            text = cut.rsplit(" ", 1)[0].rstrip(",.;:") + "…"
    return text


def _parse_date(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_url(url):
    url = (url or "").strip()
    return url if urlsplit(url).scheme in ("http", "https") else ""


def parse_feed(xml_bytes, source):
    """Return a list of story dicts from RSS or Atom bytes."""
    root = ET.fromstring(xml_bytes)
    stories = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields, link = {}, ""
        for child in node:
            name = _local(child.tag)
            if name == "link":
                href = child.get("href")
                if href:  # Atom
                    if child.get("rel", "alternate") == "alternate" and not link:
                        link = href
                elif not link:
                    link = child.text or ""
            elif name not in fields:
                fields[name] = "".join(child.itertext())
        title = _clean(fields.get("title"))
        link = _safe_url(link)
        when = _parse_date(
            fields.get("pubdate") or fields.get("published")
            or fields.get("updated") or fields.get("date")
        )
        if not (title and link and when):
            continue
        summary = _clean(fields.get("description") or fields.get("summary"))
        if len(summary) < 120:  # a bare teaser: fall back to the article body if the feed has it
            body = _clean(fields.get("encoded") or fields.get("content"))
            summary = body if len(body) > len(summary) else summary
        text = max(summary, _clean(fields.get("encoded") or fields.get("content")), key=len)
        text = re.split(r"\b(?:Article|Comments) URL:", text)[0].strip()
        summary = re.sub(r"\s*The post .{0,200}? (appeared first|first appeared) on .*$", "", summary)
        summary = re.sub(r"\s*(\[…\]|\[\.\.\.\]|Read more\W*|Continue reading\W*)$", "", summary)
        # Hacker News feeds append link/points boilerplate to the post text.
        summary = re.split(r"\b(?:Article|Comments) URL:", summary)[0]
        summary = _clean(summary, limit=SUMMARY_CHARS)
        if summary.lower().startswith(title.lower()[:40]):
            summary = ""
        stories.append({"title": title, "url": link, "when": when,
                        "summary": summary, "text": text, "source": source})
    return stories


def fetch_feed(feed):
    req = urllib.request.Request(feed["url"], headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            stories = parse_feed(resp.read(), feed["name"])
    except Exception as exc:  # one dead feed must not sink the edition
        print(f"  FAIL {feed['name']}: {exc}", file=sys.stderr)
        return []
    if feed.get("ai_only"):
        stories = [s for s in stories if AI_RE.search(s["title"] + " " + s["summary"])]
    for s in stories:
        s["section"] = feed.get("section", "news")
        s["title"] = re.sub(r"^(Show|Launch) HN:\s*", "", s["title"])
    print(f"  ok   {feed['name']}: {len(stories)} stories", file=sys.stderr)
    return stories


def _title_key(title):
    return re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()


def collect(feeds, now):
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(fetch_feed, feeds))
    stories = [s for batch in batches for s in batch if s["when"] <= now + timedelta(hours=1)]
    stories.sort(key=lambda s: s["when"], reverse=True)

    seen_urls, seen_titles, per_source, kept = {}, {}, {}, []
    for s in stories:
        hours = TOOLS_WINDOW_HOURS if s["section"] == "tools" else WINDOW_HOURS
        if s["when"] < now - timedelta(hours=hours):
            continue
        url_key = s["url"].split("?")[0].rstrip("/")
        title_key = _title_key(s["title"])
        dupe = seen_urls.get(url_key) or seen_titles.get(title_key)
        if dupe:
            if s["section"] == "tools":  # a launch also seen in a news feed stays a launch
                dupe["section"] = "tools"
            continue
        if per_source.get(s["source"], 0) >= MAX_PER_SOURCE:
            continue
        seen_urls[url_key] = seen_titles[title_key] = s
        per_source[s["source"]] = per_source.get(s["source"], 0) + 1
        s["new"] = s["when"] >= now - timedelta(hours=NEW_HOURS)
        kept.append(s)
    return kept


def apply_ai(stories):
    """Let Claude write summaries, rank, sort into sections and merge duplicate coverage."""
    from ai import edit  # imported here so the build works without the anthropic package
    topics = [name for name, _ in TOPICS] + [OTHER_TOPIC]
    rows = edit(stories, topics)
    if not rows:
        return stories, False
    for i, s in enumerate(stories):
        row = rows.get(i)
        if not row:
            continue
        if row["summary"].strip():
            s["summary"] = row["summary"].strip()
        s["importance"] = min(5, max(1, row["importance"]))
        if row["topic"] in topics:
            s["topic"] = row["topic"]
    kept, removed = [], set()
    for i, s in enumerate(stories):
        main = rows.get(i, {}).get("duplicate_of", -1)
        seen = {i}
        while main in rows and main not in seen and rows[main].get("duplicate_of", -1) != -1:
            seen.add(main)  # follow chains to the story everything points at
            main = rows[main]["duplicate_of"]
        if isinstance(main, int) and 0 <= main < len(stories) and main != i:
            target = stories[main]
            if s["source"] != target["source"]:
                target.setdefault("also", []).append((s["source"], s["url"]))
            target["new"] = target["new"] or s["new"]
            target["importance"] = max(target.get("importance", 1), s.get("importance", 1))
            if s["section"] == "tools":
                target["section"] = "tools"
            removed.add(i)
    kept = [s for i, s in enumerate(stories) if i not in removed]
    return kept, True


def classify(story):
    if story.get("topic"):
        return story["topic"]
    text = story["title"] + " " + story["summary"]
    for name, pattern in TOPICS:
        if pattern.search(text):
            return name
    return OTHER_TOPIC


def split_lead(stories, ranked):
    """Stories for the top block, at most two per source: the most important ones when the
    AI pass has ranked them, otherwise simply the newest."""
    lead, per_source = [], {}
    pool = sorted(stories, key=lambda s: -s.get("importance", 0)) if ranked else stories
    for s in pool:  # sorted() is stable, so equal importance stays newest-first
        if len(lead) < LEAD_COUNT and per_source.get(s["source"], 0) < 2:
            lead.append(s)
            per_source[s["source"]] = per_source.get(s["source"], 0) + 1
    return lead, [s for s in stories if not any(s is x for x in lead)]


# --------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------

def load_events(path, today):
    events = []
    for raw in json.loads(Path(path).read_text(encoding="utf-8")):
        start = date.fromisoformat(raw["start"])
        end = date.fromisoformat(raw.get("end") or raw["start"])
        if end < today:
            continue  # already over
        events.append({**raw, "start_date": start, "end_date": end})
    events.sort(key=lambda e: e["start_date"])
    return events[:MAX_EVENTS]


def _date_range(start, end):
    if start == end:
        return f"{start.day} {start:%b %Y}"
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.day}–{end.day} {end:%b %Y}"
    if start.year == end.year:
        return f"{start.day} {start:%b} – {end.day} {end:%b %Y}"
    return f"{start.day} {start:%b %Y} – {end.day} {end:%b %Y}"


def _countdown(start, end, today):
    days = (start - today).days
    if days <= 0 <= (end - today).days:
        return "Happening now"
    if days == 1:
        return "Tomorrow"
    if days < 14:
        return f"In {days} days"
    if days < 70:
        return f"In {round(days / 7)} weeks"
    return f"In {round(days / 30.4)} months"


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def esc(text):
    return html.escape(str(text), quote=True)


def _ago(when, now):
    minutes = max(0, int((now - when).total_seconds() // 60))
    if minutes < 60:
        return f"{max(minutes, 1)}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def render_story(s, now):
    """A story is collapsed to its headline; opening it shows the summary and the source link."""
    badge = '<span class="new">New</span>' if s["new"] else ""
    summary = esc(s["summary"]) if s["summary"] else "The source did not provide a summary for this story."
    also = ""
    if s.get("also"):
        links = ", ".join(
            f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(source)}</a>'
            for source, url in s["also"])
        also = f'<p class="also">Also covered by {links}</p>'
    return (
        f'<li><details class="story">'
        f'<summary><span class="story-title">{esc(s["title"])}</span>'
        f'<span class="meta">{badge}<span class="source">{esc(s["source"])}</span>'
        f'<time datetime="{s["when"].isoformat()}" data-rel>{_ago(s["when"], now)}</time></span></summary>'
        f'<div class="story-body"><p>{summary}</p>{also}'
        f'<a class="read" href="{esc(s["url"])}" target="_blank" rel="noopener noreferrer">'
        f'Read the full story at {esc(s["source"])} →</a></div>'
        f'</details></li>'
    )


def render_event(e, today):
    name = esc(e["name"])
    url = _safe_url(e.get("url"))
    if url:
        name = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{name}</a>'
    tentative = '<span class="tentative" title="Not yet officially confirmed">TBC</span>' \
        if e.get("tentative") else ""
    now_cls = " live" if e["start_date"] <= today <= e["end_date"] else ""
    return (
        f'<li class="event{now_cls}" data-start="{e["start_date"].isoformat()}" '
        f'data-end="{e["end_date"].isoformat()}">'
        f'<p class="countdown">{_countdown(e["start_date"], e["end_date"], today)}</p>'
        f'<p class="event-name">{name}{tentative}</p>'
        f'<p class="event-meta">{_date_range(e["start_date"], e["end_date"])}'
        f' · {esc(e.get("location", ""))}</p>'
        f'<p class="event-kind">{esc(e.get("kind", "Event"))}</p>'
        f'</li>'
    )


def render(stories, events, now, template, ai_edited=False):
    tools, per_source = [], {}
    for s in stories:  # newest first, at most three per source so one feed can't fill the block
        if s["section"] == "tools" and len(tools) < MAX_TOOLS and per_source.get(s["source"], 0) < 3:
            tools.append(s)
            per_source[s["source"]] = per_source.get(s["source"], 0) + 1
    lead, rest = split_lead([s for s in stories if s["section"] != "tools"], ai_edited)
    groups = {}
    for s in rest:
        groups.setdefault(classify(s), []).append(s)
    order = [name for name, _ in TOPICS] + [OTHER_TOPIC]

    lead_title = "Top stories" if ai_edited else "Latest"
    sections, nav = [], [f'<a href="#latest">{lead_title}</a>']
    tools_html = ""
    if tools:
        nav.insert(0, '<a href="#tools">Tools &amp; builds</a>')
        tools_html = (
            '<section id="tools" class="feature"><h2>Tools &amp; builds</h2>'
            '<p class="blurb">New tools, launches and things people are making with AI.</p>'
            '<ul class="stories">' + "".join(render_story(s, now) for s in tools)
            + "</ul></section>"
        )
    for name in order:
        items = groups.get(name, [])[:MAX_PER_TOPIC]
        if not items:
            continue
        slug = re.sub(r"[^a-z]+", "-", name.lower()).strip("-")
        nav.append(f'<a href="#{slug}">{esc(name)}</a>')
        sections.append(
            f'<section id="{slug}"><h2>{esc(name)}</h2><ul class="stories">'
            + "".join(render_story(s, now) for s in items) + "</ul></section>"
        )

    edition = "Morning edition" if now.hour < 12 else "Evening edition"
    sources = sorted({s["source"] for s in stories}, key=str.lower)
    values = {
        "title": esc(TITLE),
        "edition": edition,
        "date_long": f"{now:%A} {now.day} {now:%B %Y}",
        "built_iso": now.isoformat(),
        "built_text": f"{now:%H:%M} UTC",
        "story_count": str(len(stories)),
        "new_count": str(sum(s["new"] for s in stories)),
        "source_count": str(len(sources)),
        "sources": esc(", ".join(sources)),
        "nav": "".join(nav),
        "tools": tools_html,
        "lead_title": lead_title,
        "ai_note": (" Summaries, ranking and grouping are written by AI from the publishers' text "
                    "and can contain mistakes; follow the link for the original.") if ai_edited else "",
        "lead": "".join(render_story(s, now) for s in lead),
        "sections": "".join(sections),
        "events": "".join(render_event(e, now.date()) for e in events)
                  or '<li class="event"><p class="event-meta">No upcoming events listed.</p></li>',
    }
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values[m.group(1)], template)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="_site/index.html")
    parser.add_argument("--feeds", default=HERE / "feeds.json")
    parser.add_argument("--events", default=HERE / "events.json")
    args = parser.parse_args()

    now = datetime.now(timezone.utc).replace(microsecond=0)
    feeds = json.loads(Path(args.feeds).read_text(encoding="utf-8"))
    print(f"Fetching {len(feeds)} feeds…", file=sys.stderr)
    stories = collect(feeds, now)
    if len(stories) < MIN_ITEMS:
        sys.exit(f"Only {len(stories)} stories found; refusing to publish an empty edition.")

    ai_edited = False
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            stories, ai_edited = apply_ai(stories)
        except Exception as exc:  # the edition still goes out, with the publishers' own text
            print(f"  AI: skipped ({type(exc).__name__}: {exc})", file=sys.stderr)
    else:
        print("  AI: no ANTHROPIC_API_KEY set; using publishers' own text", file=sys.stderr)

    events = load_events(args.events, now.date())
    page = render(stories, events, now, (HERE / "template.html").read_text(encoding="utf-8"),
                  ai_edited)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"Wrote {out}: {len(stories)} stories, {len(events)} pinned events", file=sys.stderr)


if __name__ == "__main__":
    main()
