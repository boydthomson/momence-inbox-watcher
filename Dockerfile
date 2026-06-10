FROM python:3.12-slim

# Apprise provides 80+ notification services (Matrix, Telegram, Discord,
# Slack, ntfy, Pushover, email, ...) selected by URL in APPRISE_URLS.
RUN pip install --no-cache-dir apprise

WORKDIR /app
COPY watcher.py .

# Persisted dedup state lives here; mount a volume to survive container rebuilds.
RUN mkdir /data
ENV STATE_FILE=/data/state.json
VOLUME /data

# Run as non-root
RUN useradd -r -u 10001 watcher && chown watcher /data
USER watcher

CMD ["python3", "-u", "watcher.py"]
