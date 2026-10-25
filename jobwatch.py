#!/usr/bin/env python3
"""
JobWatch - zero-AI job watcher for Sun Heng.

Runs on a schedule, fetches postings from free public APIs, filters and scores
them with deterministic rules, and prints a digest of NEW high-scoring matches.

Prints NOTHING when there is nothing new worth seeing -- the Hermes cron
`no_agent` mode then sends no message at all. No LLM is ever invoked here.

Usage:
    python jobwatch.py              # normal run (dedupes against seen.json)
    python jobwatch.py --test       # ignore seen.json, show top matches, don't save
    python jobwatch.py --stats      # show store stats
"""

import json
import os
import re
import ssl
import sys
import time
import html
import urllib.request
import urllib.error
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "jobwatch_config.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def log(msg):
    if os.environ.get("JOBWATCH_DEBUG"):
        print(f"[debug] {msg}", file=sys.stderr)


def fetch(url, data=None, timeout=None, headers=None, retries=None):
    """GET/POST with retry.

    Cambodian and NGO sites are slow or flaky from overseas datacentre IPs
    (GitHub runners in particular), so a single short attempt silently drops
    the highest-priority sources. Retry with a growing timeout before giving up.
    """
    timeout = timeout or int(os.environ.get("JOBWATCH_TIMEOUT", "45"))
    retries = retries if retries is not None else 3
    h = {"User-Agent": UA, "Accept": "*/*",
         "Accept-Language": "en-US,en;q=0.9"}
    if data is not None:
        h["Content-Type"] = "application/json"
    if headers:
        h.update(headers)
    body = json.dumps(data).encode() if data is not None else None
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=body, headers=h)
            with urllib.request.urlopen(req, timeout=timeout + attempt * 20, context=_ctx) as r:
                return r.read()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(2 + attempt * 3)
    raise last


def fetch_json(url, data=None, timeout=None, headers=None):
    return json.loads(fetch(url, data=data, timeout=timeout, headers=headers))


def clean(text, limit=None):
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(text))
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if limit and len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "..."
    return text


def norm_id(source, raw):
    return f"{source}:{re.sub(r'[^a-zA-Z0-9]+', '-', str(raw))[:120]}"


# ----------------------------------------------------------------------------
# source adapters -- each returns a list of normalized job dicts
# ----------------------------------------------------------------------------

def src_workday(cfg):
    out = []
    for t in cfg.get("workday_tenants", []):
        base = f"https://{t['host']}/wday/cxs/{t['tenant']}/{t['site']}/jobs"
        for term in cfg.get("workday_search_terms", ["intern"]):
            try:
                j = fetch_json(base, {"appliedFacets": {}, "limit": 20,
                                      "offset": 0, "searchText": term})
            except Exception as e:
                log(f"workday {t['name']}/{term}: {e}")
                continue
            for p in j.get("jobPostings", []):
                path = p.get("externalPath") or ""
                url = f"https://{t['host']}/en-US/{t['site']}{path}"
                out.append({
                    "id": norm_id("wd", path or p.get("title")),
                    "title": clean(p.get("title")),
                    "company": t["name"],
                    "location": clean(p.get("locationsText")),
                    "url": url,
                    "posted": clean(p.get("postedOn")),
                    "desc": clean(p.get("bulletFields") and " ".join(p["bulletFields"])),
                    "source": "Workday",
                })
    return out


def src_greenhouse(cfg):
    out = []
    for board in cfg.get("greenhouse_boards", []):
        try:
            j = fetch_json(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
        except Exception as e:
            log(f"greenhouse {board}: {e}")
            continue
        for p in j.get("jobs", []):
            out.append({
                "id": norm_id("gh", p.get("id")),
                "title": clean(p.get("title")),
                "company": board.title(),
                "location": clean((p.get("location") or {}).get("name")),
                "url": p.get("absolute_url", ""),
                "posted": clean(p.get("updated_at"))[:10],
                "desc": "",
                "source": "Greenhouse",
            })
    return out


def src_lever(cfg):
    out = []
    for board in cfg.get("lever_boards", []):
        try:
            j = fetch_json(f"https://api.lever.co/v0/postings/{board}?mode=json")
        except Exception as e:
            log(f"lever {board}: {e}")
            continue
        for p in j if isinstance(j, list) else []:
            cat = p.get("categories") or {}
            out.append({
                "id": norm_id("lv", p.get("id")),
                "title": clean(p.get("text")),
                "company": board.title(),
                "location": clean(cat.get("location")),
                "url": p.get("hostedUrl", ""),
                "posted": "",
                "desc": clean(p.get("descriptionPlain"), 600),
                "source": "Lever",
            })
    return out


def src_ashby(cfg):
    out = []
    for board in cfg.get("ashby_boards", []):
        try:
            j = fetch_json(f"https://api.ashbyhq.com/posting-api/job-board/{board}")
        except Exception as e:
            log(f"ashby {board}: {e}")
            continue
        for p in j.get("jobs", []):
            out.append({
                "id": norm_id("ab", p.get("id")),
                "title": clean(p.get("title")),
                "company": board.title(),
                "location": clean(p.get("location")),
                "url": p.get("jobUrl", ""),
                "posted": clean(p.get("publishedAt"))[:10],
                "desc": "",
                "source": "Ashby",
            })
    return out


def src_remoteok(cfg):
    if not cfg.get("enable_remoteok"):
        return []
    try:
        j = fetch_json("https://remoteok.com/api")
    except Exception as e:
        log(f"remoteok: {e}")
        return []
    out = []
    for p in j:
        if not isinstance(p, dict) or not p.get("position"):
            continue
        out.append({
            "id": norm_id("rok", p.get("id")),
            "title": clean(p.get("position")),
            "company": clean(p.get("company")),
            "location": clean(p.get("location")) or "Remote",
            "url": p.get("url", ""),
            "posted": clean(p.get("date"))[:10],
            "desc": clean(p.get("description"), 600),
            "source": "RemoteOK",
        })
    return out


def src_remotive(cfg):
    if not cfg.get("enable_remotive"):
        return []
    out = []
    for cat in cfg.get("remotive_categories", ["data"]):
        try:
            j = fetch_json(f"https://remotive.com/api/remote-jobs?category={cat}&limit=50")
        except Exception as e:
            log(f"remotive {cat}: {e}")
            continue
        for p in j.get("jobs", []):
            out.append({
                "id": norm_id("rmv", p.get("id")),
                "title": clean(p.get("title")),
                "company": clean(p.get("company_name")),
                "location": clean(p.get("candidate_required_location")) or "Remote",
                "url": p.get("url", ""),
                "posted": clean(p.get("publication_date"))[:10],
                "desc": clean(p.get("description"), 600),
                "source": "Remotive",
            })
    return out


def src_rss(cfg):
    out = []
    for feed in cfg.get("rss_feeds", []):
        try:
            raw = fetch(feed["url"]).decode("utf-8", "ignore")
        except Exception as e:
            log(f"rss {feed['name']}: {e}")
            continue
        items = re.findall(r"<item>(.*?)</item>", raw, re.S)
        for it in items:
            def tag(t):
                m = re.search(rf"<{t}[^>]*>(.*?)</{t}>", it, re.S)
                return clean(m.group(1)) if m else ""
            title = tag("title")
            if not title:
                continue
            out.append({
                "id": norm_id("rss", tag("link") or title),
                "title": title,
                "company": feed["name"],
                "location": "",
                "url": tag("link"),
                "posted": tag("pubDate")[:16],
                "desc": clean(tag("description"), 600),
                "source": feed["name"],
            })
    return out


def src_html_careers(cfg):
    """Generic scraper for server-rendered careers pages.

    Many Cambodian / regional employers publish jobs as plain links on one page
    rather than through an ATS API. Each entry needs a `link_pattern` regex that
    a posting href must match -- for Wing that is '/careers/<slug>'.
    """
    out = []
    for site in cfg.get("html_careers", []):
        try:
            raw = fetch(site["url"]).decode("utf-8", "ignore")
        except Exception as e:
            log(f"html_careers {site.get('name')}: {e}")
            continue
        pat = re.compile(site.get("link_pattern", r"/careers?/[a-z0-9\-]{4,}"), re.I)
        base = site.get("base", "")
        seen_here = set()
        for href, inner in re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', raw, re.S):
            if not pat.search(href):
                continue
            title = clean(inner)
            if not title or len(title) < 4 or len(title) > 120:
                continue
            url = href if href.startswith("http") else base.rstrip("/") + "/" + href.lstrip("/")
            if url in seen_here:
                continue
            seen_here.add(url)
            out.append({
                "id": norm_id("hc", url),
                "title": title,
                "company": site.get("name", "Careers"),
                "location": site.get("location", ""),
                "url": url,
                "posted": "",
                "desc": "",
                "source": site.get("name", "Careers page"),
            })
    return out


SOURCES = [src_workday, src_greenhouse, src_lever, src_ashby,
           src_remoteok, src_remotive, src_rss, src_html_careers]


# ----------------------------------------------------------------------------
# filtering + scoring (pure rules, zero AI)
# ----------------------------------------------------------------------------

def score_job(job, cfg):
    """Return (score, reasons, flags). Higher is better."""
    title = job["title"].lower()
    blob = " ".join([job["title"], job.get("desc", ""), job.get("location", "")]).lower()
    loc = (job.get("location", "") + " " + job.get("title", "")).lower()

    reasons, flags = [], []

    # hard reject: obvious seniority / irrelevant trades
    for bad in cfg.get("exclude_title", []):
        if bad in title:
            return -999, [f"excluded: '{bad.strip()}' in title"], []

    # hard reject: scam signals
    for sig in cfg.get("scam_signals", []):
        if sig in blob:
            return -999, [f"scam signal: '{sig}'"], ["SCAM"]

    # hard reject: must contain at least one CORE technical keyword.
    # Without this, admin roles that merely mention "reporting" or "data entry"
    # accumulate enough weak keyword points to clear the threshold.
    core = cfg.get("core_role_keywords", [])
    if core and not any(k in blob for k in core):
        return -999, ["no core technical keyword"], []

    score = 0

    # role relevance (required - must hit at least one)
    role_hits = []
    for kw, pts in cfg.get("role_keywords", {}).items():
        if kw in blob:
            score += pts
            role_hits.append(kw.strip())
    if not role_hits:
        return -999, ["no role keyword match"], []
    reasons.append("role: " + ", ".join(sorted(set(role_hits))[:4]))

    # level fit
    lvl_hits = []
    for kw, pts in cfg.get("level_keywords", {}).items():
        if kw in blob:
            score += pts
            lvl_hits.append(kw.strip())
    if lvl_hits:
        reasons.append("level: " + ", ".join(sorted(set(lvl_hits))[:3]))

    # location preference
    loc_hits = []
    for kw, pts in cfg.get("location_bonus", {}).items():
        if kw in loc:
            score += pts
            loc_hits.append(f"{kw}{'+' if pts > 0 else ''}{pts}")
    if loc_hits:
        reasons.append("loc: " + ", ".join(loc_hits[:3]))

    # sponsorship signals
    for kw in cfg.get("sponsor_positive", []):
        if kw in blob:
            score += 25
            flags.append("SPONSOR-FRIENDLY")
            reasons.append(f"sponsor: '{kw}'")
            break
    for kw in cfg.get("sponsor_negative", []):
        if kw in blob:
            score -= 20
            flags.append("NEEDS-LOCAL-AUTH")
            reasons.append(f"auth required: '{kw}'")
            break

    return score, reasons, flags


# ----------------------------------------------------------------------------
# store
# ----------------------------------------------------------------------------

def load_seen(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(path, seen, cap=6000):
    seen = list(seen)[-cap:]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(seen, f)


def append_history(path, rows):
    if not rows:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def send_telegram(text):
    """Post directly to the Telegram Bot API.

    Used when running outside Hermes (e.g. GitHub Actions), where no gateway
    exists to deliver stdout. Credentials come from the environment so the
    token never lands in the repo.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set", file=sys.stderr)
        return False
    # Telegram caps messages at 4096 chars
    for chunk in [text[i:i + 3800] for i in range(0, len(text), 3800)]:
        payload = urllib.parse.urlencode({
            "chat_id": chat,
            "text": chunk,
            "parse_mode": "Markdown",
            "disable_web_page_preview": "true",
        }).encode()
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30, context=_ctx) as r:
                if not json.load(r).get("ok"):
                    return False
        except Exception as e:
            # Markdown can fail on odd characters; retry as plain text
            try:
                payload = urllib.parse.urlencode({"chat_id": chat, "text": chunk}).encode()
                req = urllib.request.Request(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    data=payload, headers={"User-Agent": UA})
                urllib.request.urlopen(req, timeout=30, context=_ctx).read()
            except Exception:
                print(f"[telegram] send failed: {type(e).__name__}", file=sys.stderr)
                return False
    return True


def main():
    test_mode = "--test" in sys.argv
    stats_mode = "--stats" in sys.argv
    tg_mode = "--telegram" in sys.argv

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # JOBWATCH_DATA_DIR lets CI (GitHub Actions) keep state inside the repo
    # checkout instead of the Windows path baked into the config.
    data_dir = os.environ.get("JOBWATCH_DATA_DIR") or cfg.get("data_dir", HERE)
    seen_path = os.path.join(data_dir, "seen.json")
    hist_path = os.path.join(data_dir, "jobs_history.jsonl")

    if stats_mode:
        seen = load_seen(seen_path)
        n = sum(1 for _ in open(hist_path, encoding="utf-8")) if os.path.exists(hist_path) else 0
        print(f"seen ids: {len(seen)}  |  history rows: {n}  |  data dir: {data_dir}")
        return 0

    # 1. fetch everything (free)
    jobs, errors = [], []
    for fn in SOURCES:
        try:
            got = fn(cfg)
            jobs.extend(got)
            log(f"{fn.__name__}: {len(got)}")
        except Exception as e:
            errors.append(f"{fn.__name__}: {type(e).__name__}")

    # 2. dedupe within this run
    uniq = {}
    for j in jobs:
        if j.get("url") and j["id"] not in uniq:
            uniq[j["id"]] = j
    jobs = list(uniq.values())

    # 3. score (free)
    scored = []
    for j in jobs:
        s, reasons, flags = score_job(j, cfg)
        if s <= 0:
            continue
        j = dict(j, score=s, reasons=reasons, flags=flags)
        scored.append(j)
    scored.sort(key=lambda x: -x["score"])

    threshold = cfg.get("score_threshold", 45)
    seen = set() if test_mode else load_seen(seen_path)

    fresh = [j for j in scored if j["score"] >= threshold and j["id"] not in seen]
    limit = cfg.get("max_alerts_per_run", 8)
    show = fresh[:limit]

    if test_mode:
        print(f"[test] fetched={len(jobs)} scored={len(scored)} "
              f"above_threshold={len([j for j in scored if j['score'] >= threshold])}")
        if errors:
            print(f"[test] source errors: {', '.join(errors)}")
        show = scored[:limit]

    if not show:
        return 0  # silence => cron sends nothing

    # 4. render digest
    stamp = datetime.now(timezone.utc).astimezone().strftime("%d %b %Y, %H:%M")
    lines = [f"*JobWatch* — {len(fresh)} new match{'es' if len(fresh) != 1 else ''}  ({stamp})", ""]

    for i, j in enumerate(show, 1):
        tags = ""
        if "SPONSOR-FRIENDLY" in j["flags"]:
            tags += " ✅sponsor"
        if "NEEDS-LOCAL-AUTH" in j["flags"]:
            tags += " ⚠️needs-local-auth"
        loc = j["location"] or "—"
        lines.append(f"*{i}. {j['title']}*{tags}")
        lines.append(f"   {j['company']} · {loc} · score {j['score']}")
        if j.get("desc"):
            lines.append(f"   _{clean(j['desc'], cfg.get('max_description_chars', 400))}_")
        lines.append(f"   {j['url']}")
        lines.append("")

    if len(fresh) > len(show):
        lines.append(f"…and {len(fresh) - len(show)} more in jobs_history.jsonl")
    lines.append("Reply with a number to have the CV + cover letter tailored.")

    digest = "\n".join(lines)
    print(digest)
    if tg_mode:
        send_telegram(digest)

    # 5. persist
    if not test_mode:
        for j in fresh:
            seen.add(j["id"])
        save_seen(seen_path, seen)
        append_history(hist_path, [
            {"ts": datetime.now(timezone.utc).isoformat(), **{
                k: j[k] for k in ("id", "title", "company", "location", "url",
                                  "score", "flags", "source")}}
            for j in fresh
        ])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # never spam the channel on failure; surface only in debug
        log(f"fatal: {type(e).__name__}: {e}")
        sys.exit(0)
