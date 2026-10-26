#!/bin/sh
# Run ON THE SERVER as administrator. Makes Jarvis 4 the live bot:
#   backup → stop the old bot → copy what it knew into jarvis4.db → start Jarvis 4, enabled at boot → check it came up.
# If anything fails (or Jarvis 4 does not come up cleanly) the old bot is started again by itself.
# Nothing of the old bot is deleted: rollback.sh does the same by hand.
set -e
cd /opt/jarvis
ok=0
trap 'if [ "$ok" -ne 1 ]; then echo "CUTOVER FAILED: starting the old bot again"; sudo systemctl disable --now jarvis4 2>/dev/null || true; sudo systemctl enable --now jarvis; fi' EXIT

sudo cp /opt/jarvis/deploy/jarvis4.service /etc/systemd/system/jarvis4.service
sudo systemctl daemon-reload

# The old bot stops first: its database and session stand still, so the backup is a consistent copy
sudo systemctl stop jarvis
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p backup
tar --exclude=data/venv --exclude=data/browser --exclude=data/inbox -czf "backup/before-jarvis4-$STAMP.tar.gz" .env jarvis jarvis4 data
tar -tzf "backup/before-jarvis4-$STAMP.tar.gz" > /dev/null && echo "backup ok: backup/before-jarvis4-$STAMP.tar.gz"
set -a; . /opt/jarvis/.env; set +a
/opt/jarvis/.venv/bin/python -m jarvis4.migrate /opt/jarvis/data/jarvis.db

sudo systemctl disable jarvis
sudo systemctl enable --now jarvis4
sleep 10
systemctl is-active --quiet jarvis4
journalctl -u jarvis4 --since "-1min" --no-pager | tail -25
journalctl -u jarvis4 --since "-1min" --no-pager | grep -q "Jarvis 4 is up"
if journalctl -u jarvis4 --since "-1min" --no-pager | grep -q "Traceback"; then echo "traceback in the log"; exit 1; fi
ok=1
echo "CUTOVER OK: Jarvis 4 is live (rollback: sh /opt/jarvis/deploy/rollback.sh)"
