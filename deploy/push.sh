#!/bin/sh
# Copies jarvis4/ (code, tests, exam; not reports) to the server's /opt/jarvis/jarvis4. The previous copy is kept in
# /opt/jarvis/backup. Does not restart anything: `ssh ... sudo systemctl restart jarvis4` when you want it live.
set -e
cd "$(dirname "$0")/.."
HOST="${JARVIS_HOST:-administrator@85.190.96.176}"
tar --exclude=__pycache__ --exclude=reports -czf - jarvis4 deploy | ssh -o BatchMode=yes "$HOST" '
  set -e
  cd /opt/jarvis
  if [ -d jarvis4 ]; then mv jarvis4 "backup/jarvis4-$(date +%Y%m%d-%H%M%S)"; fi
  rm -rf deploy
  tar -xzf -
  find jarvis4 -name "*.py" | wc -l | sed "s/$/ python files pushed/"
'
