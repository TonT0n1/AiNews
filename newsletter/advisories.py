"""
Newly published critical vulnerabilities (CVSS 9.0 and above), for any product.

The main source is the US National Vulnerability Database (NVD), which carries every
published CVE. If NVD can't be reached, critical advisories from the GitHub Advisory
Database are used instead (open-source packages only). Standard library only.

Optional environment variables: NVD_API_KEY raises NVD's rate limit, GITHUB_TOKEN raises
GitHub's. Neither is required for two lookups a day.
"""

import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).parent
NVD_API = "https://services.nvd.nist.gov/rest/json/cves/2.0"
GITHUB_API = "https://api.github.com/advisories"


def config(path=HERE / "security.json"):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return {"min_score": float(data.get("min_score", 9.0)),
            "window_hours": int(data.get("window_hours", 48))}


def _get_json(url, headers, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "ai-newsletter-bot/1.0", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _parse_time(text):
    text = (text or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _short(text, limit=230):
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = cut.rfind(". ")
    return cut[:stop + 1] if stop > limit * 0.5 else cut.rsplit(" ", 1)[0].rstrip(",.;:") + "…"


# --------------------------------------------------------------------------
# NVD
# --------------------------------------------------------------------------

def _nvd_score(metrics):
    """NVD's own score where it has analysed the CVE, otherwise the reporter's (CNA) score,
    preferring the newest CVSS version. Returns (score, version) or (None, None)."""
    for primary in (True, False):
        for key, version in (("cvssMetricV40", "4.0"), ("cvssMetricV31", "3.1"), ("cvssMetricV30", "3.0")):
            for m in metrics.get(key) or []:
                if (m.get("type") == "Primary") == primary:
                    score = (m.get("cvssData") or {}).get("baseScore")
                    if isinstance(score, (int, float)):
                        return float(score), version
    return None, None


def _nvd_products(cve):
    names = []
    for conf in cve.get("configurations") or []:
        for node in conf.get("nodes") or []:
            for match in node.get("cpeMatch") or []:
                if not match.get("vulnerable"):
                    continue
                parts = (match.get("criteria") or "").split(":")
                if len(parts) > 4:
                    name = f"{parts[3]} {parts[4]}".replace("_", " ").title()
                    if name not in names:
                        names.append(name)
    return names[:2]


def _fetch_nvd(now, cfg):
    start = now - timedelta(hours=cfg["window_hours"])
    fmt = "%Y-%m-%dT%H:%M:%S.000"
    headers = {"apiKey": os.environ["NVD_API_KEY"]} if os.environ.get("NVD_API_KEY") else {}
    found = {}
    # A CVE can carry a v3 score, a v4 score or both, and NVD filters on one version at a time.
    for i, param in enumerate(("cvssV3Severity", "cvssV4Severity")):
        if i:
            time.sleep(7 if not headers else 1)  # NVD allows 5 requests per 30s without a key
        query = {"pubStartDate": start.strftime(fmt), "pubEndDate": now.strftime(fmt),
                 param: "CRITICAL", "resultsPerPage": 2000}
        data = _get_json(f"{NVD_API}?{urlencode(query)}", headers)
        for item in data.get("vulnerabilities") or []:
            cve = item.get("cve") or {}
            cid = cve.get("id")
            if not cid or cid in found or cve.get("vulnStatus") == "Rejected":
                continue
            score, version = _nvd_score(cve.get("metrics") or {})
            if score is None or score < cfg["min_score"]:
                continue
            text = next((d.get("value") for d in cve.get("descriptions") or []
                         if d.get("lang") == "en"), "")
            found[cid] = {
                "id": cid, "score": score, "version": version, "summary": _short(text),
                "products": _nvd_products(cve),
                "exploited": bool(cve.get("cisaExploitAdd")),
                "when": _parse_time(cve.get("published")) or now,
                "url": f"https://nvd.nist.gov/vuln/detail/{cid}",
            }
    return list(found.values())


# --------------------------------------------------------------------------
# GitHub Advisory Database (fallback)
# --------------------------------------------------------------------------

def _github_score(adv):
    sev = adv.get("cvss_severities") or {}
    for key, version in (("cvss_v4", "4.0"), ("cvss_v3", "3.1")):
        score = (sev.get(key) or {}).get("score")
        if isinstance(score, (int, float)) and score > 0:
            return float(score), version
    score = (adv.get("cvss") or {}).get("score")
    return (float(score), "3.1") if isinstance(score, (int, float)) and score > 0 else (None, None)


def _fetch_github(now, cfg):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    query = {"type": "reviewed", "severity": "critical", "sort": "published",
             "direction": "desc", "per_page": 100}
    raw = _get_json(f"{GITHUB_API}?{urlencode(query)}", headers)
    start = now - timedelta(hours=cfg["window_hours"])
    found = []
    for adv in raw:
        when = _parse_time(adv.get("published_at"))
        score, version = _github_score(adv)
        if not when or when < start or adv.get("withdrawn_at"):
            continue
        if score is None or score < cfg["min_score"]:
            continue
        pkgs = sorted({(v.get("package") or {}).get("name") for v in adv.get("vulnerabilities") or []} - {None})
        found.append({
            "id": adv.get("cve_id") or adv.get("ghsa_id"), "score": score, "version": version,
            "summary": _short(adv.get("summary")), "products": pkgs[:2], "exploited": False,
            "when": when, "url": adv.get("html_url") or "",
        })
    return found


# --------------------------------------------------------------------------

def fetch(now):
    """Return (advisories, source name), or (None, None) if every source failed."""
    cfg = config()
    for name, fetcher in (("NVD", _fetch_nvd), ("GitHub Advisory Database", _fetch_github)):
        try:
            found = fetcher(now, cfg)
        except Exception as exc:
            print(f"  advisories: {name} lookup failed ({type(exc).__name__}: {exc})", file=sys.stderr)
            continue
        found.sort(key=lambda a: (-a["score"], -a["when"].timestamp()))
        exploited = sum(a["exploited"] for a in found)
        print(f"  advisories: {len(found)} CVEs scored {cfg['min_score']}+ in the last "
              f"{cfg['window_hours']}h from {name} ({exploited} known exploited)", file=sys.stderr)
        for a in found[:3]:
            print(f"       e.g. {a['id']} {a['score']} {', '.join(a['products'])}: {a['summary'][:70]}",
                  file=sys.stderr)
        return found, name
    return None, None
