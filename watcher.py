#!/usr/bin/env python3
"""Momence Inbox Watcher.

Polls the Momence host dashboard's unread-messages endpoint and pushes a
notification (via Apprise: Matrix, Telegram, Discord, Slack, ntfy, email, ...)
the moment a client message arrives — instead of waiting hours for Momence's
own email digest.

The Momence public API does not expose the inbox, so this replays the internal
endpoint the dashboard itself calls:

    GET https://momence.com/_api/readonly/host/<HOST_ID>/inbox/unread-messages

authenticated by the `ribbon.connect.sid` session cookie from a logged-in
dashboard browser session. That cookie eventually expires; when it does, the
watcher sends a "needs re-auth" notification (rate-limited to one per hour).

All configuration is via environment variables — see .env.example.
State (already-alerted messages) persists in STATE_FILE so restarts don't
re-notify.
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

try:
    import apprise
except ImportError:
    apprise = None

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
HOST_ID = os.environ.get("MOMENCE_HOST_ID", "").strip()
COOKIE = os.environ.get("MOMENCE_SESSION_COOKIE", "").strip()
APPRISE_URLS = [u.strip() for u in os.environ.get("APPRISE_URLS", "").split(",") if u.strip()]
INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.environ.get("STATE_FILE", "/data/state.json")
USER_AGENT = os.environ.get(
    "USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0",
)
REAUTH_ALERT_INTERVAL = 3600  # seconds between repeated "cookie expired" alerts


class AuthExpired(Exception):
    pass


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def fetch_unread_raw():
    url = f"https://momence.com/_api/readonly/host/{HOST_ID}/inbox/unread-messages"
    req = urllib.request.Request(url)
    req.add_header("Cookie", f"ribbon.connect.sid={COOKIE}")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json, text/plain, */*")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return json.loads(raw) if raw else []
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AuthExpired(f"HTTP {e.code} — Momence session cookie expired")
        raise


def _dig(item, *names):
    """First truthy value among top-level or commonly nested keys."""
    for n in names:
        if item.get(n):
            return item[n]
    for container in ("contact", "member", "customer", "sender", "from", "conversation", "lastMessage"):
        sub = item.get(container)
        if isinstance(sub, dict):
            for n in names:
                if sub.get(n):
                    return sub[n]
    return None


def parse_unread(payload):
    """Normalize the unread-messages array to [{id, name, preview, updated}]."""
    items = payload if isinstance(payload, list) else (payload or {}).get("payload", [])
    out = []
    for raw in items:
        it = raw if isinstance(raw, dict) else {"value": raw}
        # Confirmed shape (2026-06): sender is it["contact"]{firstName,lastName,...},
        # text in "body", timestamp in "createdAt", channel in "channelType".
        contact = it.get("contact") or {}
        name = (
            f"{contact.get('firstName') or ''} {contact.get('lastName') or ''}".strip()
            or _dig(it, "memberName", "customerName", "name", "displayName", "fullName")
            or f"{_dig(it, 'firstName') or ''} {_dig(it, 'lastName') or ''}".strip()
            or it.get("recipient")
            or "Unknown sender"
        )
        text = _dig(it, "text", "body", "message", "lastMessage", "preview", "snippet", "content")
        if isinstance(text, dict):
            text = text.get("text") or text.get("body") or json.dumps(text)
        preview = (str(text) if text else json.dumps(it))[:200]
        mid = _dig(it, "messageId", "id", "conversationId", "threadId", "uuid")
        if not mid:
            mid = "h:" + hashlib.sha1(json.dumps(it, sort_keys=True).encode()).hexdigest()[:16]
        updated = _dig(it, "updatedAt", "createdAt", "lastMessageAt", "timestamp", "sentAt", "date") or ""
        out.append({"id": str(mid), "name": name, "preview": preview, "updated": updated,
                    "channel": it.get("channelType")})
    return out


# ---------------------------------------------------------------------------
# Notify
# ---------------------------------------------------------------------------

def make_notifier():
    if APPRISE_URLS and apprise:
        a = apprise.Apprise()
        ok = all(a.add(u) for u in APPRISE_URLS)
        if not ok:
            print("WARNING: one or more APPRISE_URLS were not recognized", flush=True)
        def send(title, body):
            if not a.notify(title=title, body=body):
                print(f"WARNING: notification failed: {title}", flush=True)
        return send
    if APPRISE_URLS and not apprise:
        print("WARNING: APPRISE_URLS set but apprise not installed; printing to console", flush=True)
    def send(title, body):
        print(f"\n*** {title} ***\n{body}\n", flush=True)
    return send


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"seen": {}, "last_auth_alert": 0}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def check_once(state, notify):
    """One poll: fetch, diff, notify. Mutates and returns state."""
    try:
        unread = parse_unread(fetch_unread_raw())
    except AuthExpired as e:
        now = time.time()
        if now - state.get("last_auth_alert", 0) >= REAUTH_ALERT_INTERVAL:
            state["last_auth_alert"] = now
            notify(
                "⚠️ Momence inbox watcher needs re-auth",
                f"{e}. Log into the Momence dashboard, copy the fresh "
                f"ribbon.connect.sid cookie value, update MOMENCE_SESSION_COOKIE, "
                f"and restart the container.",
            )
        print(f"[{time.strftime('%H:%M:%S')}] AUTH EXPIRED: {e}", flush=True)
        return state
    except Exception as e:  # transient network/server errors: log, retry next tick
        print(f"[{time.strftime('%H:%M:%S')}] fetch error (will retry): {e}", flush=True)
        return state

    seen = state.get("seen", {})
    new = 0
    current_ids = set()
    for conv in unread:
        cid, updated = conv["id"], conv["updated"]
        current_ids.add(cid)
        if seen.get(cid) != updated:  # new message, or newer message in thread
            channel = f" ({conv['channel']})" if conv.get("channel") else ""
            notify(f"📨 New Momence message — {conv['name']}{channel}", conv["preview"])
            new += 1
        seen[cid] = updated
    # Forget threads no longer unread so a future message re-alerts.
    state["seen"] = {cid: ts for cid, ts in seen.items() if cid in current_ids}
    print(f"[{time.strftime('%H:%M:%S')}] checked — {new} new, {len(unread)} unread total", flush=True)
    return state


def main():
    missing = [n for n, v in [("MOMENCE_HOST_ID", HOST_ID), ("MOMENCE_SESSION_COOKIE", COOKIE)] if not v]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)} (see .env.example)")

    notify = make_notifier()
    once = "--once" in sys.argv

    print(f"Momence inbox watcher — host {HOST_ID}, every {INTERVAL}s, "
          f"{len(APPRISE_URLS)} notification target(s)", flush=True)
    while True:
        state = load_state()
        state = check_once(state, notify)
        save_state(state)
        if once:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
