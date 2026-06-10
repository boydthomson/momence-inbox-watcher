# Momence Inbox Watcher

Get notified **within a minute** when a client messages you in
[Momence](https://momence.com) — on Matrix, Telegram, Discord, Slack, ntfy,
email, Pushover, or [any of ~80 services](https://github.com/caronc/apprise/wiki),
instead of waiting for Momence's email alert (which can lag by hours).

A tiny Docker container polls your Momence inbox every minute and pushes an
alert the moment a new unread message appears. New thread → alert. Client
replies again → alert. Nothing new → silence. Already read it in the
dashboard → no alert.

## Why this exists

The Momence public API (v2) does **not** expose the inbox — no message
endpoints, no webhooks. The only machine-readable source is the internal
endpoint the dashboard itself uses, authenticated by your dashboard session
cookie. This watcher replays that request. It is read-only and uses your own
account's session — but note it relies on an **unofficial** endpoint that
Momence could change at any time.

## Quick start (prebuilt image)

No clone needed — grab the [.env.example](https://raw.githubusercontent.com/boydthomson/momence-inbox-watcher/main/.env.example),
fill it in (3 things: host id, session cookie, notification URLs), then:

```bash
docker run -d --name momence-inbox-watcher \
  --env-file .env \
  -v momence-watcher-state:/data \
  --restart unless-stopped \
  ghcr.io/boydthomson/momence-inbox-watcher:latest
docker logs -f momence-inbox-watcher
```

Or with compose:

```yaml
services:
  momence-inbox-watcher:
    image: ghcr.io/boydthomson/momence-inbox-watcher:latest
    env_file: .env
    volumes:
      - watcher-state:/data
    restart: unless-stopped
volumes:
  watcher-state:
```

Images are built for `linux/amd64` and `linux/arm64` (Raspberry Pi friendly).

## Build from source

```bash
git clone https://github.com/boydthomson/momence-inbox-watcher.git
cd momence-inbox-watcher
cp .env.example .env
# edit .env  (3 things: host id, session cookie, notification URLs)
docker compose up -d --build
docker compose logs -f   # watch it run
```

### Filling in `.env`

1. **`MOMENCE_HOST_ID`** — log into your Momence dashboard; the URL is
   `https://momence.com/dashboard/<HOST_ID>/dashboard`. That number.
2. **`MOMENCE_SESSION_COOKIE`** — in the same logged-in browser:
   DevTools (F12) → Application/Storage → Cookies → `momence.com` → copy the
   **value** of `ribbon.connect.sid` (starts with `s%3A`).
3. **`APPRISE_URLS`** — comma-separated notification targets, e.g.
   ```
   APPRISE_URLS=matrixs://syt_token@matrix.example.org/!room:example.org,ntfy://ntfy.sh/my-momence-alerts
   ```
   See [Apprise URL formats](https://github.com/caronc/apprise/wiki) for your
   platform of choice. Leave empty to just log to the container console.

### Test a single check

```bash
docker compose run --rm momence-inbox-watcher python3 watcher.py --once
```

## When the cookie expires

`ribbon.connect.sid` is a session cookie; sooner or later Momence will reject
it (HTTP 401). The watcher then sends a **"needs re-auth"** notification (at
most once per hour) and keeps retrying. To fix: log into the dashboard again,
copy the fresh cookie value into `.env`, and `docker compose up -d`.

## Notes & caveats

- **Unofficial endpoint** — `/_api/readonly/host/<id>/inbox/unread-messages`
  is what the dashboard calls internally. It may change without notice; if it
  does, alerts stop and the log will show errors.
- **Field mapping** — the message-array item fields are extracted heuristically
  (name/preview/timestamp). If a notification ever shows raw JSON instead of a
  clean preview, open an issue with the (redacted) payload so the mapping can
  be pinned down.
- **Privacy** — your session cookie grants access to your Momence dashboard.
  It lives only in your `.env` and in the requests to momence.com. Keep `.env`
  out of version control (see `.gitignore`).
- **Read-only** — the watcher only GETs the unread list. It never marks
  messages read, never sends anything to clients.

## Without Docker

Works fine as a plain script too (Python 3.9+; `pip install apprise` for
notifications):

```bash
export $(grep -v '^#' .env | xargs)
STATE_FILE=./state.json python3 watcher.py
```
