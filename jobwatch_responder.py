#!/usr/bin/env python3
"""Cloud-side Telegram responder for JobWatch.

Runs on GitHub Actions. Answers commands when the laptop is OFF, and stays
completely out of the way when it is ON.

How the laptop is detected -- no heartbeat file needed:
    Telegram allows exactly one process to poll getUpdates at a time. When the
    Hermes gateway on the laptop is running, our call returns HTTP 409
    Conflict. That 409 *is* the "laptop is on" signal, so this script exits
    immediately and lets the gateway (a full agent with tools) handle the
    message. Only when there is no 409 do we consume updates and reply.

No LLM is used here.
"""

import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
STATE_DIR = os.environ.get("JOBWATCH_DATA_DIR", "state")
OFFSET_FILE = os.path.join(STATE_DIR, "tg_offset.json")
API = f"https://api.telegram.org/bot{TOKEN}"

_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def api(method, params=None, timeout=25):
    data = urllib.parse.urlencode(params or {}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                 headers={"User-Agent": "jobwatch-responder"})
    with urllib.request.urlopen(req, timeout=timeout, context=_ctx) as r:
        return json.load(r)


def say(text):
    try:
        api("sendMessage", {"chat_id": CHAT, "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": "true"})
    except Exception as e:
        print(f"send failed: {e}", file=sys.stderr)


def load_offset():
    try:
        with open(OFFSET_FILE, encoding="utf-8") as f:
            return json.load(f).get("offset", 0)
    except Exception:
        return 0


def save_offset(v):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(OFFSET_FILE, "w", encoding="utf-8") as f:
        json.dump({"offset": v}, f)


OFFLINE_HELP = (
    "💤 <b>Your laptop is off.</b>\n"
    "The local watcher (Wing Bank, Smart Axiata, ReliefWeb) and anything that "
    "builds documents need it running.\n\n"
    "<b>Works right now from the cloud:</b>\n"
    "<code>/scan</code> — check global sources (Workday, RemoteOK, Remotive)\n"
    "<code>/status</code> — what is running where\n\n"
    "<b>Needs the laptop on:</b>\n"
    "<code>/tailor N</code> — build CV + cover letter for match N\n"
    "<code>/local</code> — scan Cambodian employers + ReliefWeb\n\n"
    "<i>Turn the laptop on and send the command again.</i>"
)


def handle(cmd, arg):
    if cmd in ("/scan", "/jobs"):
        say("🔎 Running the global scan from the cloud…")
        os.environ["JOBWATCH_LABEL"] = "Global (cloud)"
        os.environ["JOBWATCH_SKIP"] = "src_html_careers,src_rss"
        rc = os.system(f'"{sys.executable}" jobwatch.py --telegram')
        if rc != 0:
            say("⚠️ Scan failed. Check the Actions log.")
        return

    if cmd == "/status":
        try:
            hist = os.path.join(STATE_DIR, "jobs_history.jsonl")
            n = sum(1 for _ in open(hist, encoding="utf-8")) if os.path.exists(hist) else 0
        except Exception:
            n = 0
        say("📊 <b>JobWatch status</b>\n"
            "☁️ Cloud watcher: <b>running</b> (every 4h, global sources)\n"
            "💤 Laptop: <b>off</b> — Cambodia sources paused\n"
            f"🗂 Matches recorded: <b>{n}</b>\n\n"
            "<i>Cloud alerts keep arriving regardless of your laptop.</i>")
        return

    if cmd in ("/tailor", "/local", "/apply", "/cv"):
        say(OFFLINE_HELP)
        return

    if cmd in ("/start", "/help", "/commands"):
        say(OFFLINE_HELP)
        return

    say(OFFLINE_HELP)


def laptop_is_on():
    """Decide liveness from the heartbeat variable, never from Telegram.

    getUpdates is mutually exclusive: probing it does not observe the laptop's
    gateway, it *evicts* it -- the gateway logs a polling conflict and drops
    offline for ~20s before recovering. So liveness is read from a timestamp
    the laptop pushes to a GitHub repo variable instead. If anything about the
    heartbeat is unreadable we assume the laptop is ON and stay silent, because
    a wrong "laptop is off" reply is worse than no reply.
    """
    raw = os.environ.get("LAPTOP_HEARTBEAT", "").strip()
    if not raw:
        print("no heartbeat value -> assuming laptop ON, standing down")
        return True
    try:
        age = time.time() - int(raw)
    except ValueError:
        print("unparseable heartbeat -> assuming laptop ON, standing down")
        return True
    stale_after = int(os.environ.get("HEARTBEAT_STALE_SECONDS", "900"))
    print(f"heartbeat age: {int(age)}s (stale after {stale_after}s)")
    return age < stale_after


def main():
    if not TOKEN or not CHAT:
        print("missing telegram credentials", file=sys.stderr)
        return 0

    if laptop_is_on():
        print("laptop is ON -> gateway handles messages; not touching Telegram")
        return 0

    offset = load_offset()
    try:
        res = api("getUpdates", {"offset": offset, "timeout": 0, "limit": 20})
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print("409: laptop came online mid-run; standing down")
            return 0
        print(f"getUpdates failed: {e}", file=sys.stderr)
        return 0
    except Exception as e:
        print(f"getUpdates failed: {e}", file=sys.stderr)
        return 0

    updates = res.get("result", [])
    if not updates:
        print("no pending updates")
        return 0

    last = offset
    cmds = []
    for u in updates:
        last = max(last, u.get("update_id", 0) + 1)
        msg = u.get("message") or u.get("edited_message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT:
            continue
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        parts = text.split(maxsplit=1)
        cmds.append((parts[0].split("@")[0].lower(),
                     parts[1] if len(parts) > 1 else ""))

    save_offset(last)

    # Only answer the most recent command, so a backlog after a long offline
    # stretch does not produce a wall of duplicate replies.
    if cmds:
        cmd, arg = cmds[-1]
        print(f"handling {cmd}")
        handle(cmd, arg)
    else:
        print("no commands in updates")
    return 0


if __name__ == "__main__":
    sys.exit(main())
