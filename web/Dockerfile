FROM nginx:alpine

RUN apk add --no-cache \
      python3 \
      openssh-client \
      sshpass \
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

RUN chmod +x /opt/update/update-slurm.sh /entrypoint.sh \
 && mkdir -p /var/log/update

ENV HPC_HOST=172.16.12.2 \
    HPC_USER=lz \
    DAYS=30 \
    OUT_DIR=/usr/share/nginx/html/data

ENTRYPOINT ["/sbin/tini","--","/entrypoint.sh"]
