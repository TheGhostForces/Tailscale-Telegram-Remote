#!/usr/bin/env bash
# Удаляет агента с этого устройства. Запускать обычным пользователем: bash uninstall-linux.sh
set -uo pipefail
systemctl --user disable --now rc-agent.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/rc-agent.service"
systemctl --user daemon-reload
rm -rf "$HOME/.local/share/rc-agent"
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw delete allow in on tailscale0 to any port 8765 proto tcp 2>/dev/null
fi
echo "Агент удалён с этого устройства."
echo "Дальше: на сервере rcctl remove ИМЯ и в Tailscale admin console: Machines -> Remove."
