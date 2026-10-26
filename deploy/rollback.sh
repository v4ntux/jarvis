#!/bin/sh
# Run ON THE SERVER as administrator. Puts the old bot (Jarvis 3) back: stops Jarvis 4, starts jarvis.service.
# Jarvis 4's own data (/opt/jarvis/data/jarvis4.db) stays where it is.
set -e
sudo systemctl disable --now jarvis4 || true
sudo systemctl enable --now jarvis
sleep 5
systemctl is-active jarvis
journalctl -u jarvis --since "-1min" --no-pager | tail -10
