#!/usr/bin/env bash
# Брандмауэр VPS: закрыто всё, кроме SSH и портов RustDesk; трафик из Tailscale разрешён.
# ВАЖНО: перед запуском убедитесь, что вход по SSH-ключу работает. Если у SSH нестандартный порт — поправьте.
set -euo pipefail
SSH_PORT="${SSH_PORT:-22}"
apt-get install -y ufw
ufw --force reset
ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp"
ufw allow 21115:21119/tcp     # RustDesk: hbbs/hbbr
ufw allow 21116/udp
ufw allow in on tailscale0
ufw --force enable
ufw status verbose
