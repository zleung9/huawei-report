#!/bin/sh
# Start crond in background and nginx in foreground.
# Runs an initial update so the page has data immediately (best-effort; nginx
# still starts even if it fails).
set -eu

LOG=/var/log/update/update.log
touch "$LOG"
chmod 644 "$LOG"

# Materialize the cron schedule from $UPDATE_CRON if provided.
if [ -n "${UPDATE_CRON:-}" ]; then
  echo "${UPDATE_CRON} /opt/update/update-slurm.sh >> ${LOG} 2>&1" > /etc/crontabs/root
fi

echo "==> crontab:"
cat /etc/crontabs/root
echo "==> starting crond"
crond -b -L /var/log/update/crond.log

echo "==> initial update (best-effort)"
/opt/update/update-slurm.sh >> "$LOG" 2>&1 || echo "initial update failed (check $LOG); nginx will start anyway" >&2

echo "==> starting apikey submission server (127.0.0.1:18789)"
python3 /opt/update/apikey_server.py >> /var/log/update/apikey.log 2>&1 &

# Stream logs to container stdout so `docker logs` shows them.
tail -F "$LOG" /var/log/update/crond.log /var/log/update/apikey.log >&2 &

echo "==> starting nginx"
exec nginx -g 'daemon off;'
