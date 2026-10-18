#!/bin/bash
# cloud-init user-data for the Jackpot VM (Ubuntu 24.04). Log: /var/log/jackpot-setup.log
exec > /var/log/jackpot-setup.log 2>&1
set -x
# Reserved IP is attached right after boot; wait for internet.
for i in $(seq 1 90); do curl -s --max-time 5 https://api.ipify.org && break; sleep 10; done
IP=$(curl -s https://api.ipify.org)
HOST="$(echo "$IP" | tr . -).sslip.io"

timedatectl set-timezone Asia/Kolkata
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io docker-compose-v2 git caddy openssl
usermod -aG docker ubuntu

# Oracle images block everything but SSH in iptables
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
netfilter-persistent save || true

[ -d /opt/jackpot ] || git clone -b live-trading https://github.com/prag1987/Jackpot.git /opt/jackpot
cd /opt/jackpot
if [ ! -f .env ]; then
  PASS=$(openssl rand -base64 24 | tr -d '/+=' | cut -c1-24)
  KEY=$(openssl rand -base64 32 | tr '+/' '-_')
  cat > .env <<EOF
CG_ADMIN_PASSWORD=$PASS
CG_ENCRYPTION_KEY=$KEY
DHAN_CLIENT_ID=
DHAN_ACCESS_TOKEN=
DHAN_SANDBOX_CLIENT_ID=
DHAN_SANDBOX_ACCESS_TOKEN=
CG_STATIC_IP=$IP
CG_ALLOW_LIVE=NO
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
EOF
  chmod 600 .env
  echo "$PASS" > /root/dashboard_password.txt && chmod 600 /root/dashboard_password.txt
fi
chown -R ubuntu:ubuntu /opt/jackpot
# Dashboard only; worker starts after Dhan credentials are saved (see DEPLOY_VPS_HINDI.md)
docker compose up -d --build dashboard

cat > /etc/caddy/Caddyfile <<EOF
$HOST {
  encode gzip
  reverse_proxy 127.0.0.1:8501
}
EOF
systemctl enable caddy && systemctl restart caddy
echo "https://$HOST" > /root/jackpot_url.txt
echo "JACKPOT SETUP DONE https://$HOST"
