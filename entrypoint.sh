#!/bin/bash
set -e

CRON_EXPR=$(python3 -c "
import yaml
try:
    with open('config.yaml', 'r') as f:
        cfg = yaml.safe_load(f)
    print(cfg.get('cron', '0 1 * * *'))
except:
    print('0 1 * * *')
")

WEB_PORT=${WEB_PORT:-8080}

echo "启动 Web 管理界面 (port: ${WEB_PORT})..."
echo "定时任务: ${CRON_EXPR}"

echo "${CRON_EXPR} cd /app && python3 main.py >> /proc/1/fd/1 2>&1" > /etc/crontabs/root

echo "启动定时任务调度..."
uvicorn web:app --host 0.0.0.0 --port "${WEB_PORT}" --log-level warning &
exec crond -f -l 2
