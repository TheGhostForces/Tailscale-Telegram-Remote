#!/usr/bin/env bash
# Установка бота на VPS (Debian/Ubuntu). От root, из папки bot/: sudo bash install-vps.sh
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
apt-get install -y python3-venv
id rcbot >/dev/null 2>&1 || useradd --system --home /opt/rc-bot --shell /usr/sbin/nologin rcbot
mkdir -p /opt/rc-bot
install -m 644 "$SRC/bot.py" "$SRC/signing.py" "$SRC/gen_totp.py" "$SRC/requirements.txt" /opt/rc-bot/
[ -f /opt/rc-bot/config.yaml ] || cp "${SRC}/config.yaml" /opt/rc-bot/config.yaml 2>/dev/null || { cp "$SRC/config.example.yaml" /opt/rc-bot/config.yaml; echo "!! заполните /opt/rc-bot/config.yaml"; }
[ -f /opt/rc-bot/.env ] || { cp "${SRC}/.env" /opt/rc-bot/.env 2>/dev/null || cp "$SRC/.env.example" /opt/rc-bot/.env; echo "!! заполните /opt/rc-bot/.env"; }
python3 -m venv /opt/rc-bot/venv
/opt/rc-bot/venv/bin/pip install -q --upgrade pip
/opt/rc-bot/venv/bin/pip install -q -r /opt/rc-bot/requirements.txt
chown -R rcbot:rcbot /opt/rc-bot
chmod 600 /opt/rc-bot/.env
install -m 644 "$SRC/rc-bot.service" /etc/systemd/system/rc-bot.service
systemctl daemon-reload
systemctl enable rc-bot.service
echo "Заполните config.yaml и .env, затем: systemctl restart rc-bot && journalctl -u rc-bot -f"
