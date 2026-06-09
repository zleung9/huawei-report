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

echo "==> ensuring DB exists (first run will create + backfill 180 days)"
if [ ! -f "${DB_PATH:-/opt/db-data/usage.sqlite}" ]; then
    echo "    DB not found, running backfill_hpc.sh"
    BACKFILL_DAYS="${BACKFILL_DAYS:-180}" /opt/dbtools/backfill_hpc.sh \
        >> /var/log/update/backfill.log 2>&1 \
        || echo "WARN: backfill failed (check backfill.log); continuing" >&2
else
    echo "    DB already present at ${DB_PATH:-/opt/db-data/usage.sqlite}"
fi

echo "==> initial update (best-effort)"
/opt/update/update-slurm.sh >> "$LOG" 2>&1 || echo "initial update failed (check $LOG); nginx will start anyway" >&2

echo "==> bootstrapping admin account"
python3 /opt/dbtools/admin_bootstrap.py >> /var/log/update/auth.log 2>&1 \
    || echo "WARN: admin bootstrap failed (check auth.log); continuing" >&2

echo "==> starting apikey submission server (127.0.0.1:28789)"
python3 /opt/update/apikey_server.py >> /var/log/update/apikey.log 2>&1 &

echo "==> starting auth server (127.0.0.1:28790)"
python3 /opt/update/auth_server.py >> /var/log/update/auth.log 2>&1 &

touch /var/log/update/backfill.log
# Stream logs to container stdout so `docker logs` shows them.
touch /var/log/update/auth.log
tail -F "$LOG" /var/log/update/crond.log /var/log/update/apikey.log /var/log/update/backfill.log /var/log/update/auth.log >&2 &

echo "==> starting nginx"
exec nginx -g 'daemon off;'
