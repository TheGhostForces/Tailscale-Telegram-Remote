#!/usr/bin/env bash
# Установка агента на Debian/GNOME. Запускать ОБЫЧНЫМ пользователем (не root) из папки agent/:
#   bash install-linux.sh [--wol <интерфейс>]
# Агент работает как systemd --user сервис в графической сессии: так доступны скриншоты и lock.
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.local/share/rc-agent"
[ "$(id -u)" -ne 0 ] || { echo "Не запускайте от root"; exit 1; }

sudo apt-get install -y python3-venv ethtool gnome-screenshot
mkdir -p "$DIR" "$HOME/.config/systemd/user"
install -m 644 "$SRC/agent.py" "$SRC/signing.py" "$SRC/requirements.txt" "$DIR/"
if [ ! -f "$DIR/config.yaml" ]; then
  if [ -f "$SRC/config.yaml" ]; then cp "$SRC/config.yaml" "$DIR/config.yaml"
  else cp "$SRC/config.example.yaml" "$DIR/config.yaml"; echo "!! Отредактируйте $DIR/config.yaml (MAC, команды)"; fi
fi
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

if [ ! -f "$DIR/.env" ]; then
  SECRET="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  (umask 077; echo "AGENT_SECRET=$SECRET" > "$DIR/.env")
  echo; echo "AGENT_SECRET (впишите в bot/.env как LAPTOP_SECRET): $SECRET"; echo
fi
chmod 700 "$DIR"; chmod 600 "$DIR/.env"

install -m 644 "$SRC/rc-agent.service" "$HOME/.config/systemd/user/rc-agent.service"
systemctl --user daemon-reload
systemctl --user enable --now rc-agent.service

# Брандмауэр (если используется ufw): порт агента только из Tailscale
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow in on tailscale0 to any port 8765 proto tcp
fi

# Wake-on-LAN (опционально)
if [ "${1:-}" = "--wol" ] && [ -n "${2:-}" ]; then
  IF="$2"
  CON="$(nmcli -g GENERAL.CONNECTION device show "$IF")"
  sudo nmcli connection modify "$CON" 802-3-ethernet.wake-on-lan magic
  sudo ethtool -s "$IF" wol g || true
  echo "WoL включён для $IF (подключение '$CON'). MAC: $(cat /sys/class/net/$IF/address)"
  echo "Не забудьте включить WoL в BIOS/UEFI."
fi
echo "Готово. Статус: systemctl --user status rc-agent"
echo "Автовход GNOME (чтобы агент стартовал после перезагрузки без вас): Параметры -> Пользователи -> Автоматический вход."
