# Optional container build - the bot runs just as well with plain `python3 main.py`
# (see README "Quick start"). No build step needed: no third-party dependencies - the
# Solami WebSocket and RPC clients are standard library only.
FROM python:3.12-slim

WORKDIR /app

# Copied separately so a future dependency addition doesn't bust the layer cache for the
# application code below; today this is a no-op install (see requirements.txt).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY alerts_bot/ alerts_bot/

RUN useradd --system --create-home --home-dir /app --shell /usr/sbin/nologin alerts \
    && mkdir -p /app/state \
    && chown -R alerts:alerts /app
USER alerts

ENV PYTHONUNBUFFERED=1

# Liveness for the Solami tape: the run loop rewrites state/heartbeat.json every 30 s,
# so a stale file means a hung process. (The legacy DexScreener mode writes no
# heartbeat; a missing file is treated as healthy.)
HEALTHCHECK --interval=60s --timeout=5s --start-period=90s --retries=3 \
  CMD python3 -c "import os,sys,time; p='/app/state/heartbeat.json'; sys.exit(1 if os.path.exists(p) and time.time()-os.path.getmtime(p) > 180 else 0)"

# .env is not copied into the image - mount it (and a state/ volume for the dedup
# database) at run time, e.g.:
#   docker run -d --name onchain-alerts \
#     -v $(pwd)/.env:/app/.env:ro \
#     -v onchain-alerts-state:/app/state \
#     telegram-onchain-alerts
# Stop with `docker stop` (SIGTERM): the bot closes its Solami sockets and flushes
# queued posts before exiting.
CMD ["python3", "main.py"]
