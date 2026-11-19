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


def fetch(now, config_path=HERE / "security.json"):
    """Return a list of advisory dicts, newest first, or None if the lookup failed."""
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    packages = config["packages"]
    window = timedelta(days=config.get("window_days", 14))
    token = os.environ.get("GITHUB_TOKEN", "")
    since = (now - window).date().isoformat()
    base = {"type": "reviewed", "sort": "published", "direction": "desc", "per_page": 100,
            "published": f">={since}"}

    raw = None
    for affects in (",".join(packages), packages):  # comma list first, then affects[]=…
        params = dict(base)
        params["affects" if isinstance(affects, str) else "affects[]"] = affects
        try:
            raw = _request(params, token)
            break
        except urllib.error.HTTPError as exc:
            print(f"  advisories: HTTP {exc.code} from GitHub; "
                  f"{'retrying' if isinstance(affects, str) else 'giving up'}", file=sys.stderr)
        except Exception as exc:
            print(f"  advisories: lookup failed ({type(exc).__name__}: {exc})", file=sys.stderr)
            return None
    if not isinstance(raw, list):
        return None

    watched = {p.lower() for p in packages}
    found, off_list, too_old = [], 0, 0
    for adv in raw:
        published = _parse_time(adv.get("published_at"))
        if not published or published < now - window or adv.get("withdrawn_at"):
            too_old += 1
            continue
        vulns = adv.get("vulnerabilities") or []
        hits = [v for v in vulns if (v.get("package") or {}).get("name", "").lower() in watched]
        if not hits:  # only show advisories that really affect a watched package
            off_list += 1
            continue
        names = sorted({v["package"]["name"] for v in hits})
        fixed = sorted({_patched(v) for v in hits if _patched(v)})
        severity = (adv.get("severity") or "unknown").lower()
        found.append({
            "id": adv.get("cve_id") or adv.get("ghsa_id") or "",
            "summary": (adv.get("summary") or "").strip(),
            "url": adv.get("html_url") or f"https://github.com/advisories/{adv.get('ghsa_id', '')}",
            "severity": "medium" if severity == "moderate" else severity,
            "packages": names,
            "fixed": fixed,
            "when": published,
        })
    newest = max((a.get("published_at") or "" for a in raw), default="none")
    print(f"  advisories: {len(raw)} returned since {since} (newest {newest}); {len(found)} kept, "
          f"{too_old} older or withdrawn, {off_list} not for a watched package", file=sys.stderr)
    if raw and not found:
        a = raw[0]
        pkgs = [(v.get("package") or {}).get("name") for v in a.get("vulnerabilities") or []]
        print(f"       first returned: {a.get('ghsa_id')} published {a.get('published_at')} "
              f"packages {pkgs[:4]}", file=sys.stderr)
    for a in found[:3]:
        print(f"       e.g. [{a['severity']}] {', '.join(a['packages'])}: {a['summary'][:70]}",
              file=sys.stderr)
    return found
