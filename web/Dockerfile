FROM nginx:alpine

RUN apk add --no-cache \
      python3 \
      openssh-client \
      sshpass \
      sqlite \
      tini \
      tzdata \
 && cp /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && echo "Asia/Shanghai" > /etc/timezone

WORKDIR /opt/update
COPY scripts/collect_slurm.py /opt/update/collect_slurm.py
COPY scripts/update-slurm.sh  /opt/update/update-slurm.sh
COPY scripts/apikey_server.py /opt/update/apikey_server.py
COPY scripts/entrypoint.sh    /entrypoint.sh
COPY crontab                  /etc/crontabs/root

# DB tooling (scripts live inside the image; the actual SQLite file lives in
# a bind-mounted volume — keep these two paths separate so the volume doesn't
# mask the scripts).
COPY db/schema.sql            /opt/dbtools/schema.sql
COPY db/init_db.py            /opt/dbtools/init_db.py
COPY db/aggregate_hpc.py      /opt/dbtools/aggregate_hpc.py
COPY db/backfill_hpc.sh       /opt/dbtools/backfill_hpc.sh

RUN chmod +x /opt/update/update-slurm.sh /entrypoint.sh \
            /opt/dbtools/init_db.py /opt/dbtools/aggregate_hpc.py /opt/dbtools/backfill_hpc.sh \
 && mkdir -p /var/log/update /opt/db-data

ENV HPC_HOST=172.16.12.2 \
    HPC_USER=lz \
    DAYS=30 \
    OUT_DIR=/usr/share/nginx/html/data \
    DB_PATH=/opt/db-data/usage.sqlite

ENTRYPOINT ["/sbin/tini","--","/entrypoint.sh"]
