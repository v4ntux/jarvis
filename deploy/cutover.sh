#!/bin/sh
# Run ON THE SERVER as administrator. Makes Jarvis 4 the live bot:
#   backup → stop the old bot → copy what it knew into jarvis4.db → start Jarvis 4, enabled at boot.
# Nothing of the old bot is deleted: rollback.sh puts it back in a few seconds.
set -e
cd /opt/jarvis
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p backup
tar --exclude=data/venv --exclude=data/browser --exclude=data/inbox -czf "backup/before-jarvis4-$STAMP.tar.gz" .env jarvis jarvis4 data
tar -tzf "backup/before-jarvis4-$STAMP.tar.gz" > /dev/null && echo "backup ok: backup/before-jarvis4-$STAMP.tar.gz"

sudo cp /opt/jarvis/deploy/jarvis4.service /etc/systemd/system/jarvis4.service
sudo systemctl daemon-reload

sudo systemctl stop jarvis
set -a; . /opt/jarvis/.env; set +a
/opt/jarvis/.venv/bin/python -m jarvis4.migrate /opt/jarvis/data/jarvis.db

sudo systemctl disable jarvis
sudo systemctl enable --now jarvis4
sleep 6
systemctl is-active jarvis4
journalctl -u jarvis4 --since "-1min" --no-pager | tail -25
