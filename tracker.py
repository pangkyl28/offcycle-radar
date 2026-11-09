#!/usr/bin/env python3
"""Off-Cycle Radar: job board scanner, off-season classifier, and alerts.

The tracker only reads public job postings. No application is submitted.
Requires: requests. Run: python tracker.py scan
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
import hashlib
import html
import json
import os
from pathlib import Path
import re
import smtplib
import sys
import time
from typing import Any
from urllib.parse import urlparse, parse_qsl, urlencode

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
STATE_PATH = ROOT / "data" / "state.json"
OUTPUT_PATH = ROOT / "docs" / "jobs.json"

SWE_RE = re.compile(
    r"software|\bswe\b|developer|development engineer|back.?end|front.?end|full.?stack|"
    r"systems engineer|systems programming|systems software|platform engineer|"
    r"infrastructure engineer|compiler engineer|machine learning engineer|\bml engineer\b|"
    r"distributed systems|low.latency|quantitative developer|quant developer|"
    r"production engineer|site reliability engineer|\bsre\b",
    re.I,
)
QUANT_RE = re.compile(
    r"\bquant(?:itative)?\b|trader|\btrading\b|systematic|algo(?:rithmic)? trading|"
    r"market making|research scientist.*(trading|finance)",
    re.I,
)
INTERNSHIP_RE = re.compile(r"\bintern(?:ship)?s?\b|co[ -]?op|placement|work.?study", re.I)
SUMMER_RE = re.compile(r"\bsummer\b", re.I)
OFFCYCLE_RE = re.compile(r"off[ -]?cycle|off[ -]?season|non[ -]?summer|\bspring\b|\bfall\b|\bautumn\b|\bwinter\b|\bco[ -]?op\b|\bplacement\b", re.I)
PHD_ONLY_RE = re.compile(r"\bph\.?d\b|postdoc|doctoral", re.I)
EXCLUDE_RE = re.compile(r"trading operations|sales trader|trade support|business analyst|product manager|product management|IT support|cyber.?security|electrical engineer|mechanical engineer", re.I)
YEAR_RE = re.compile(r"\b20(?:2[5-9]|3[0-5])\b")
MARKDOWN_LINK_RE = re.compile(r"\[([^]]+)\]\((https?://[^)\s]+)\)")
HTML_LINK_RE = re.compile(r"<a\s+[^>]*href=[\"'](https?://[^\"']+)", re.I)
TAG_RE = re.compile(r"<[^>]*>")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def plain(value: Any) -> str:
    txt = str(value or "")
    txt = TAG_RE.sub(" ", txt)
    txt = MARKDOWN_LINK_RE.sub(lambda m: m.group(1), txt)
    txt = txt.replace("**", "").replace("__", "").replace("`", "")
    txt = txt.replace("↳", "").replace("🇺🇸", "").replace("🛂", "").replace("⏳", "")
    return re.sub(r"\s+", " ", html.unescape(txt)).strip()


def normalize(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", plain(value).casefold())


def valid_link(url: str) -> bool:
    p = urlparse(url)
    return p.scheme == "https" and bool(p.netloc)


def canonical_url(url: str) -> str:
    """Strip tracking queries but preserve job identifiers such as gh_jid."""
    p = urlparse(url)
    query = sorted((k.lower(), v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                   if not k.lower().startswith("utm_") and k.lower() not in {"gh_src", "source", "ref", "referral", "s"})
    return (p.netloc.lower() + p.path.rstrip("/").lower() + ("?" + urlencode(query) if query else ""))


def extract_link(text: str) -> str:
    match = HTML_LINK_RE.search(text) or MARKDOWN_LINK_RE.search(text)
    if not match:
        return ""
    return html.unescape(match.group(1 if match.re is HTML_LINK_RE else 2))


def make_job(company: str, title: str, location: str, url: str,
             source: str, description: str = "", hint: str = "") -> dict[str, Any] | None:
    company, title, location = plain(company), plain(title), plain(location)
    if not (company and title and valid_link(url)):
        return None
    # Title + location remain distinct: companies often open the same role in multiple cities.
    key = "|".join([normalize(company), normalize(title), normalize(location)])
    job_id = hashlib.sha256(key.encode()).hexdigest()[:20]
    return {
        "id": job_id, "company": company, "title": title,
        "location": location or "Location not stated", "url": url,
        "source": source, "description": plain(description)[:7000], "hint": hint,
    }


def markdown_rows(markdown: str) -> list[list[str]]:
    rows = []
    for line in markdown.splitlines():
        s = line.strip()
        if not (s.startswith("|") and s.endswith("|")):
            continue
        cells = [c.strip() for c in s[1:-1].split("|")]
        if len(cells) >= 5 and not all(re.fullmatch(r"[-:\s]+", c) for c in cells):
            rows.append(cells)
    return rows


def parse_general_github(source: dict, markdown: str) -> list[dict]:
    """Parses community five-column Company | Role | Location | Apply | Added tables."""
    results = []
    previous_company = ""
    # Avoid unrelated tables in intro/footer.
    lower = markdown.lower()
    start = lower.find("| company | role | location |")
    if start == -1:
        raise ValueError("No internship table found")
    markdown = markdown[start:]
    for cells in markdown_rows(markdown):
        if len(cells) != 5:
            continue
        company_cell, role_cell, loc_cell, apply_cell, _ = cells
        if plain(company_cell).casefold() == "company":
            continue
        if "🔒" in apply_cell or "closed" in apply_cell.lower():
            continue
        company = previous_company if plain(company_cell) in ("", "↳") or "↳" in company_cell else plain(company_cell)
        if company:
            previous_company = company
        url = extract_link(apply_cell)
        job = make_job(company, role_cell, loc_cell, url, source["name"])
        if job:
            results.append(job)
    return results


def parse_quant_github(source: dict, markdown: str) -> list[dict]:
    """Only internship sections, excluding graduate and program listings."""
    results = []
    section = ""
    previous_company = ""
    in_internships = False
    for line in markdown.splitlines():
        if line.startswith("## "):
            in_internships = line.strip().lower() == "## internships"
            section = ""
            continue
        if line.startswith("### "):
            section = plain(line[4:]).lower() if in_internships else ""
            continue
        if not in_internships or not line.startswith("|"):
            continue
        # QuantScraper has 6 columns: Company | Role | Location | Focus | Salary | Posted.
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6 or plain(cells[0]).casefold() == "company":
            continue
        if not ("trading" in section or "quantitative research" in section or "software" in section):
            continue
        company = previous_company if cells[0] == "↳" else plain(cells[0])
        if company:
            previous_company = company
        role = plain(cells[1])
        url = extract_link(cells[1])
        hint = "quant" if "trading" in section or "quantitative research" in section else "swe"
        job = make_job(company, role, cells[2], url, source["name"], hint=hint)
        if job:
            results.append(job)
    return results


def classify(job: dict, config: dict) -> dict | None:
    title = job["title"]
    description = job.get("description", "")
    # Explicit summer-only positions do not count even when the description mentions other seasons.
    if SUMMER_RE.search(title) or EXCLUDE_RE.search(title):
        return None
    if config.get("skip_phd_only", True) and PHD_ONLY_RE.search(title):
        if not re.search(r"bachelor|\bBS\b|undergraduate", title, re.I):
            return None
    if not INTERNSHIP_RE.search(title):
        return None

    # Important: source hints classify trading/research jobs, but cannot override intern filtering.
    hint = job.get("hint", "")
    if QUANT_RE.search(title) or (hint == "quant" and re.search(r"research|trading|trader|quant", title, re.I)):
        track = "Quant"
    elif SWE_RE.search(title):
        track = "SWE"
    elif hint == "swe" and re.search(r"engineering|technology|systems|developer", title, re.I):
        track = "SWE"
    else:
        return None

    years = {int(y) for y in YEAR_RE.findall(title)}
    target_years = set(config.get("target_years", [utc_now().year, utc_now().year + 1]))
    if years and not years.intersection(target_years):
        return None

    # Only exact season terms from the title or description (rather than generic "intern").
    title_hit = OFFCYCLE_RE.search(title)
    desc_hit = OFFCYCLE_RE.search(description) if not title_hit else None
    if not years and desc_hit:
        local_context = description[max(0, desc_hit.start()-20):desc_hit.end()+35]
        context_years = {int(y) for y in YEAR_RE.findall(local_context)}
        if context_years and not context_years.intersection(target_years):
            return None
        years = context_years
    if title_hit:
        term = term_label(title_hit.group())
        confidence = "Confirmed"
    elif desc_hit:
        term = term_label(desc_hit.group())
        confidence = "Confirmed"
    else:
        term = "Term unspecified"
        confidence = "Possible"
        if not config.get("include_possible_offcycle", True):
            return None

    if confidence == "Confirmed" and years:
        valid_year = min(years.intersection(target_years))
        if term not in ("Co-op", "Off-cycle"):
            term += f" {valid_year}"

    scored = dict(job)
    scored["track"] = track
    scored["term"] = term
    scored["confidence"] = confidence
    scored["score"] = score_job(scored)
    scored.pop("hint", None)
    # Don't publish a potentially very large description; it isn't needed by the dashboard.
    scored.pop("description", None)
    return scored


def term_label(word: str) -> str:
    w = word.lower()
    if "spring" in w:
        return "Spring"
    if "fall" in w or "autumn" in w:
        return "Fall"
    if "winter" in w:
        return "Winter"
    if "co" in w and "op" in w or "placement" in w:
        return "Co-op"
    return "Off-cycle"


def score_job(job: dict) -> int:
    title = job["title"].lower()
    location = job["location"].lower()
    score = 60 if job["confidence"] == "Confirmed" else 25
    if any(s in title for s in ("low latency", "low-latency", "systems", "c++", "compiler", "infrastructure", "quantitative developer")):
        score += 15
    if any(s in location for s in ("chicago", "new york", "remote", "illinois")):
        score += 10
    if job["track"] == "Quant":
        score += 5
    return min(score, 100)


def make_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(total=2, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.headers.update({"User-Agent": "OffCycleRadar/1.0 (+personal internship alerts)", "Accept": "application/json, text/plain, */*"})
    return session


def get(session: requests.Session, url: str, **kwargs) -> requests.Response:
    response = session.get(url, timeout=20, **kwargs)
    response.raise_for_status()
    return response


def fetch_source(session: requests.Session, source: dict) -> list[dict]:
    kind = source["type"]
    board = source.get("board", "")
    name = source["name"]
    if kind == "github_quant":
        return parse_quant_github(source, get(session, source["url"]).text)
    if kind == "github_general":
        return parse_general_github(source, get(session, source["url"]).text)
    if kind == "greenhouse":
        base = "https://boards.eu.greenhouse.io" if source.get("region") == "eu" else "https://boards-api.greenhouse.io"
        # Greenhouse EU publishes GET API on boards.eu.greenhouse.io.
        payload = get(session, f"{base}/v1/boards/{board}/jobs", params={"content": "true"}).json()
        return [job for post in payload.get("jobs", []) if (job := make_job(
            name, post.get("title", ""), (post.get("location") or {}).get("name", ""),
            post.get("absolute_url", ""), name, post.get("content", "")
        ))]
    if kind == "lever":
        base = "https://api.eu.lever.co" if source.get("region") == "eu" else "https://api.lever.co"
        listings = []
        for offset in range(0, 2000, 100):
            page = get(session, f"{base}/v0/postings/{board}", params={"mode": "json", "skip": offset, "limit": 100}).json()
            if not isinstance(page, list):
                raise ValueError("Unexpected Lever response")
            for post in page:
                locs = post.get("categories") or {}
                description = " ".join([post.get("descriptionPlain", ""), post.get("additionalPlain", "")])
                job = make_job(name, post.get("text", ""), locs.get("location", ""),
                               post.get("hostedUrl", "") or post.get("applyUrl", ""), name, description)
                if job:
                    listings.append(job)
            if len(page) < 100:
                break
        return listings
    if kind == "ashby":
        payload = get(session, f"https://api.ashbyhq.com/posting-api/job-board/{board}").json()
        return [job for post in payload.get("jobs", []) if post.get("isListed", True) and (job := make_job(
            name, post.get("title", ""), post.get("location", ""),
            post.get("applyUrl", "") or post.get("jobUrl", ""), name,
            post.get("descriptionPlain", "")
        ))]
    raise ValueError(f"Unknown source type: {kind}")


def load_json(path: Path, fallback: dict) -> dict:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as ex:
        raise RuntimeError(f"Cannot parse {path}: {ex}") from ex


def write_json_atomic(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(content, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def notify_telegram(jobs: list[dict], headline: str, session: requests.Session) -> bool:
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    for start in range(0, len(jobs), 7):
        block = jobs[start:start+7]
        lines = [f"<b>{html.escape(headline)}</b>", ""]
        for job in block:
            title = html.escape(job["title"])
            company = html.escape(job["company"])
            location = html.escape(job["location"])
            link = html.escape(job["url"], quote=True)
            lines.append(f"• <b>{title}</b> — {company}\n  {html.escape(job['term'])} · {location}\n  <a href=\"{link}\">Apply here</a>")
        response = session.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": "\n\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": True},
                                timeout=20)
        response.raise_for_status()
        if not response.json().get("ok"):
            raise RuntimeError("Telegram API did not accept notification")
        if start + 7 < len(jobs):
            time.sleep(0.5)
    return True


def notify_discord(jobs: list[dict], headline: str, session: requests.Session) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not webhook:
        return False
    if not valid_link(webhook):
        raise ValueError("DISCORD_WEBHOOK_URL must be an HTTPS URL")
    for start in range(0, len(jobs), 8):
        lines = [f"**{headline}**", ""]
        for job in jobs[start:start+8]:
            lines.append(f"• **{job['company']} — {job['title']}**\n{job['term']} · {job['location']}\n{job['url']}")
        response = session.post(webhook, json={"content": "\n\n".join(lines)[:1950], "allowed_mentions": {"parse": []}}, timeout=20)
        response.raise_for_status()
    return True


def notify_email(jobs: list[dict], headline: str) -> bool:
    host = os.getenv("SMTP_HOST", "")
    port = int(os.getenv("SMTP_PORT") or "587")
    username = os.getenv("SMTP_USER", "")
    password = os.getenv("SMTP_PASSWORD", "")
    recipient = os.getenv("EMAIL_TO", "")
    if not all((host, username, password, recipient)):
        return False
    msg = EmailMessage()
    msg["Subject"] = headline
    msg["From"] = username
    msg["To"] = recipient
    msg.set_content("\n\n".join(f"{j['company']} — {j['title']}\n{j['term']} · {j['location']}\n{j['url']}" for j in jobs))
    with smtplib.SMTP(host, port, timeout=20) as smtp:
        smtp.starttls()
        smtp.login(username, password)
        smtp.send_message(msg)
    return True


def deliver_notifications(jobs: list[dict], headline: str, session: requests.Session) -> list[str]:
    if not jobs:
        return []
    channels = []
    if notify_telegram(jobs, headline, session):
        channels.append("Telegram")
    if notify_discord(jobs, headline, session):
        channels.append("Discord")
    if notify_email(jobs, headline):
        channels.append("Email")
    return channels


def scan(*, notify_existing: bool = False, dry_run: bool = False,
         session: requests.Session | None = None) -> dict:
    config = load_json(CONFIG_PATH, {})
    existing = STATE_PATH.exists()
    state = load_json(STATE_PATH, {"seen": {}, "alerted": [], "snapshots": {}})
    state.setdefault("seen", {})
    state.setdefault("alerted", [])
    state.setdefault("snapshots", {})
    previously_alerted = set(state["alerted"])
    previous_seen = state["seen"]
    previous_snapshots = state["snapshots"]
    sources_meta = []
    current_snapshots = {}
    checked_at = iso_now()
    session = session or make_session()
    fresh_successes = 0
    cache_ttl = timedelta(hours=float(config.get("source_cache_hours", 72)))

    for index, source in enumerate(config.get("sources", [])):
        sid = f"{source['type']}:{source['name']}:{source.get('board', source.get('url', ''))}"
        try:
            raw = fetch_source(session, source)
            filtered = [match for j in raw if (match := classify(j, config)) is not None]
            current_snapshots[sid] = {"checked_at": checked_at, "jobs": filtered}
            sources_meta.append({"name": source["name"], "status": "OK", "matches": len(filtered)})
            fresh_successes += 1
        except (requests.RequestException, ValueError, TypeError, KeyError) as ex:
            print(f"WARNING: {source['name']}: {ex}", file=sys.stderr)
            previous = previous_snapshots.get(sid)
            can_use_cache = False
            if previous:
                try:
                    then = datetime.fromisoformat(previous["checked_at"])
                    can_use_cache = utc_now() - then <= cache_ttl
                except (TypeError, ValueError, KeyError):
                    pass
            if can_use_cache:
                current_snapshots[sid] = previous
            sources_meta.append({"name": source["name"], "status": "Cached" if can_use_cache else "Failed", "matches": len(previous.get("jobs", [])) if can_use_cache else 0})

    if not fresh_successes:
        raise RuntimeError("All sources failed. Previous dashboard and notification state were preserved.")

    merged: dict[str, dict] = {}
    url_to_id = {}
    for source in config.get("sources", []):
        sid = f"{source['type']}:{source['name']}:{source.get('board', source.get('url', ''))}"
        for job in current_snapshots.get(sid, {}).get("jobs", []):
            url_key = canonical_url(job["url"])
            job_id = url_to_id.get(url_key, job["id"])
            if job_id not in merged:
                merged[job_id] = dict(job)
                merged[job_id]["id"] = job_id
                merged[job_id]["sources"] = [job["source"]]
            else:
                if job["source"] not in merged[job_id]["sources"]:
                    merged[job_id]["sources"].append(job["source"])
                # Prefer a confirmed match over a possible match from another source.
                if merged[job_id]["confidence"] != "Confirmed" and job["confidence"] == "Confirmed":
                    old_sources = merged[job_id]["sources"]
                    merged[job_id].update(job)
                    merged[job_id]["id"] = job_id
                    merged[job_id]["sources"] = old_sources
            url_to_id[url_key] = job_id

    new_ids = []
    for job in merged.values():
        if job["id"] not in previous_seen:
            new_ids.append(job["id"])
        job["first_seen"] = previous_seen.get(job["id"], checked_at)
        job["is_new"] = job["id"] in new_ids
    jobs = sorted(merged.values(), key=lambda j: (j["confidence"] == "Confirmed", j["score"], j["first_seen"]), reverse=True)
    new_jobs = [j for j in jobs if j["id"] in new_ids]
    eligible_jobs = [j for j in jobs if j["confidence"] == "Confirmed" or config.get("notify_possible_offcycle", False)]
    # A possible-term job can become confirmed on a later scan. Keep eligible jobs
    # queued until a notification succeeds, even when a scan exceeds alert_limit.
    to_notify = [j for j in eligible_jobs if j["id"] not in previously_alerted]
    baseline = not existing and not notify_existing
    if baseline:
        to_notify = []
    limit = max(1, int(config.get("max_notifications_per_run", 25)))
    notice = to_notify[:limit]
    headline = f"Off-Cycle Radar: {len(to_notify)} internship alert{'s' if len(to_notify) != 1 else ''}"
    if len(to_notify) > limit:
        headline += f" (first {limit} below)"

    if not dry_run:
        # A notification failure interrupts the scan *before* advancing seen state,
        # so the next run can retry. Partial cross-channel delivery can duplicate alerts.
        channels = deliver_notifications(notice, headline, session) if notice else []
        if baseline:
            previously_alerted.update(j["id"] for j in eligible_jobs)
        elif channels:
            previously_alerted.update(j["id"] for j in notice)
        for job_id in new_ids:
            previous_seen[job_id] = checked_at
        state_out = {"seen": previous_seen, "alerted": sorted(previously_alerted),
                     "snapshots": current_snapshots, "last_checked_at": checked_at}
        week_ago = utc_now() - timedelta(days=7)
        week_new = sum(1 for j in jobs if datetime.fromisoformat(j["first_seen"]) >= week_ago)
        public = {
            "updated_at": checked_at, "jobs": jobs, "sources": sources_meta,
            "summary": {
                "total": len(jobs), "swe": sum(j["track"] == "SWE" for j in jobs),
                "quant": sum(j["track"] == "Quant" for j in jobs),
                "confirmed": sum(j["confidence"] == "Confirmed" for j in jobs),
                "possible": sum(j["confidence"] == "Possible" for j in jobs),
                "new_this_week": week_new,
            },
            "message": "Matches are automatically classified, not independently verified as open or eligible. Check each posting before applying."
        }
        write_json_atomic(STATE_PATH, state_out)
        write_json_atomic(OUTPUT_PATH, public)
    else:
        channels = []

    print(f"Scan {checked_at}: {fresh_successes}/{len(config.get('sources', []))} sources succeeded; "
          f"{len(jobs)} matches ({sum(j['confidence'] == 'Confirmed' for j in jobs)} confirmed, "
          f"{sum(j['confidence'] == 'Possible' for j in jobs)} possible), "
          f"{len(new_jobs)} newly discovered; {len(notice)} alert candidates. "
          f"Notifications: {', '.join(channels) if channels else 'none configured/sent'}."
          + (" DRY RUN: no files changed." if dry_run else ""))
    return {"jobs": jobs, "new_jobs": new_jobs, "alert_candidates": notice, "sources": sources_meta}


def send_test_notification() -> None:
    job = {
        "company": "Example Company", "title": "Software Engineer Intern (Spring 2027)",
        "location": "Chicago, IL", "term": "Spring 2027",
        "url": "https://example.com/example-internship",
    }
    channels = deliver_notifications([job], "Off-Cycle Radar: test notification", make_session())
    if not channels:
        raise RuntimeError("No notification provider configured. Set Telegram, Discord, or SMTP credentials.")
    print("Test alert sent to: " + ", ".join(channels))


def main() -> int:
    parser = argparse.ArgumentParser(description="Off-Cycle Radar internship monitor")
    parser.add_argument("command", choices=("scan", "test-notification"))
    parser.add_argument("--dry-run", action="store_true", help="Scan without saving or notifying")
    parser.add_argument("--notify-existing", action="store_true", help="Alert on existing matches at first scan")
    args = parser.parse_args()
    try:
        if args.command == "test-notification":
            send_test_notification()
        else:
            scan(notify_existing=args.notify_existing, dry_run=args.dry_run)
        return 0
    except (RuntimeError, OSError, requests.RequestException, smtplib.SMTPException, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
