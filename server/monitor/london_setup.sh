#!/bin/sh
# Lets Jarvis watch this server, read-only. Run once as root (the owner types the password himself):
#   PowerShell: Get-Content D:\code\vibe\jarvis\server\monitor\london_setup.sh -Raw | ssh root@46.226.162.170 "tr -d '\r' | sh"
# Jarvis's key gets one right only: running /usr/local/bin/jarvis-status ("restrict,command=" below).
# It cannot open a shell, forward ports or change anything. To take the access back, delete the line with
# "jarvis-monitor" from /root/.ssh/authorized_keys.
set -e

cat > /usr/local/bin/jarvis-status <<'EOF'
#!/bin/sh
# What Jarvis sees of this server: load, memory, disks, broken services and containers, SSH logins.
echo "host $(hostname)"
echo "load $(cut -d' ' -f1 /proc/loadavg) $(nproc 2>/dev/null || echo 1)"
awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{print "mem", t, a}' /proc/meminfo
df -P -x tmpfs -x devtmpfs -x overlay -x squashfs -x efivarfs 2>/dev/null | awk 'NR>1{gsub("%","",$5); print "disk", $6, $5}'
if command -v systemctl >/dev/null 2>&1; then
  systemctl --failed --no-legend --plain 2>/dev/null | awk 'NF{print "failed", $1}'
fi
if command -v docker >/dev/null 2>&1; then
  docker ps -a --format 'docker {{.Names}} {{.State}}' 2>/dev/null
fi
if command -v pm2 >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
  pm2 jlist 2>/dev/null | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>{try{for(const p of JSON.parse(s))console.log("pm2",p.name,p.pm2_env.status)}catch(e){}})'
fi
if command -v journalctl >/dev/null 2>&1; then
  journalctl -u ssh -u sshd --since "-15min" -o short-unix --no-pager 2>/dev/null | grep -E 'Accepted|Failed|Invalid user' | sed 's/^/log /'
  echo "fails24 $(journalctl -u ssh -u sshd --since '-24h' -o cat --no-pager 2>/dev/null | grep -cE 'Failed|Invalid user')"
fi
EOF
chmod 755 /usr/local/bin/jarvis-status

KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIKKKXVtHjKdJCZVWYh7AShfiGEUMuatIcUa6Hw+LdXl1 jarvis-monitor'
mkdir -p /root/.ssh
chmod 700 /root/.ssh
touch /root/.ssh/authorized_keys
if ! grep -q 'jarvis-monitor' /root/.ssh/authorized_keys; then
  echo "restrict,command=\"/usr/local/bin/jarvis-status\" $KEY" >> /root/.ssh/authorized_keys
fi
chmod 600 /root/.ssh/authorized_keys
echo "Done: Jarvis can now see this server (read-only)."
/usr/local/bin/jarvis-status | head -5
