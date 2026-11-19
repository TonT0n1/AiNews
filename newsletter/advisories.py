"""
Recent security advisories (CVEs) for widely used AI software.

Reads the package watchlist in security.json and asks the GitHub Advisory Database
for reviewed advisories affecting any of those packages. Standard library only.
A GITHUB_TOKEN in the environment raises the rate limit but is not required.
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).parent
API = "https://api.github.com/advisories"


def window_days(config_path=HERE / "security.json"):
    return json.loads(Path(config_path).read_text(encoding="utf-8")).get("window_days", 14)


def _request(params, token):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-newsletter-bot/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{API}?{urlencode(params, doseq=True)}", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def _patched(vuln):
    fixed = vuln.get("first_patched_version")
    if isinstance(fixed, dict):
        fixed = fixed.get("identifier")
    return fixed or ""


def _parse_time(text):
    try:
        return datetime.fromisoformat((text or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _fetch_package(name, token):
    """The most recent reviewed advisories for one package, or an error message."""
    params = {"type": "reviewed", "sort": "published", "direction": "desc", "per_page": 10,
              "affects": name}
    try:
        return _request(params, token)
    except Exception as exc:
        return f"{name}: {type(exc).__name__}: {exc}"


def fetch(now, config_path=HERE / "security.json"):
    """Return a list of advisory dicts, newest first, or None if the lookup failed."""
    from concurrent.futures import ThreadPoolExecutor
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    packages = config["packages"]
    window = timedelta(days=config.get("window_days", 14))
    token = os.environ.get("GITHUB_TOKEN", "")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda name: _fetch_package(name, token), packages))
    errors = [r for r in results if isinstance(r, str)]
    results = [None if isinstance(r, str) else r for r in results]
    failed = len(errors)
    for line in errors[:3]:
        print(f"  advisories: {line}", file=sys.stderr)
    if failed == len(packages):
        return None

    watched = {p.lower() for p in packages}
    found, seen, latest = [], set(), []
    for name, raw in zip(packages, results):
        for adv in raw or []:
            published = _parse_time(adv.get("published_at"))
            if published:
                latest.append((published, name))
            if not published or published < now - window or adv.get("withdrawn_at"):
                continue
            if adv.get("ghsa_id") in seen:
                continue
            vulns = adv.get("vulnerabilities") or []
            hits = [v for v in vulns if (v.get("package") or {}).get("name", "").lower() in watched]
            if not hits:
                continue
            seen.add(adv.get("ghsa_id"))
            severity = (adv.get("severity") or "unknown").lower()
            found.append({
                "id": adv.get("cve_id") or adv.get("ghsa_id") or "",
                "summary": (adv.get("summary") or "").strip(),
                "url": adv.get("html_url") or f"https://github.com/advisories/{adv.get('ghsa_id', '')}",
                "severity": "medium" if severity == "moderate" else severity,
                "packages": sorted({v["package"]["name"] for v in hits}),
                "fixed": sorted({_patched(v) for v in hits if _patched(v)}),
                "when": published,
            })
    found.sort(key=lambda a: a["when"], reverse=True)
    newest = {}
    for d, n in latest:
        newest[n] = max(d, newest.get(n, d))
    recent = ", ".join(f"{n} {d:%d %b}" for n, d in sorted(newest.items(), key=lambda x: x[1], reverse=True)[:6])
    print(f"  advisories: checked {len(packages)} packages ({failed} failed); {len(found)} published "
          f"in the last {window.days} days. Most recent: {recent or 'none'}", file=sys.stderr)
    for a in found[:3]:
        print(f"       e.g. [{a['severity']}] {', '.join(a['packages'])}: {a['summary'][:70]}",
              file=sys.stderr)
    return found
