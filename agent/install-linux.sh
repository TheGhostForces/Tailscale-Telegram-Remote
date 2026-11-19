#!/usr/bin/env bash
# Установка агента на Debian/GNOME. Запускать ОБЫЧНЫМ пользователем (не root) из папки agent/:
#   bash install-linux.sh [--secret СЕКРЕТ] [--wol ИНТЕРФЕЙС]
#   --secret  секрет, выданный командой `rcctl add` на сервере (без него будет сгенерирован новый)
#   --wol     включить Wake-on-LAN на этом проводном интерфейсе (ip link)
# Можно запускать повторно: файлы обновятся, агент перезапустится.
set -euo pipefail
SECRET=""; WOL_IF=""
while [ $# -gt 0 ]; do
  case "$1" in
    --secret) SECRET="${2:-}"; shift 2 ;;
    --wol)    WOL_IF="${2:-}"; shift 2 ;;
    *) echo "Неизвестный параметр: $1"; exit 1 ;;
  esac
done
[ -z "$SECRET" ] || [ "${#SECRET}" -ge 32 ] || { echo "Секрет должен быть не короче 32 символов"; exit 1; }

SRC="$(cd "$(dirname "$0")" && pwd)"
DIR="$HOME/.local/share/rc-agent"
[ "$(id -u)" -ne 0 ] || { echo "Не запускайте от root"; exit 1; }

sudo apt-get install -y python3-venv ethtool gnome-screenshot
mkdir -p "$DIR" "$HOME/.config/systemd/user"
install -m 644 "$SRC/agent.py" "$SRC/signing.py" "$SRC/requirements.txt" "$DIR/"
if [ ! -f "$DIR/config.yaml" ]; then
  if [ -f "$SRC/config.yaml" ]; then cp "$SRC/config.yaml" "$DIR/config.yaml"
  else cp "$SRC/config.example.yaml" "$DIR/config.yaml"; fi
fi
python3 -m venv "$DIR/venv"
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

if [ -n "$SECRET" ]; then
  (umask 077; echo "AGENT_SECRET=$SECRET" > "$DIR/.env")
  echo "AGENT_SECRET задан из параметра --secret."
elif [ ! -f "$DIR/.env" ]; then
  GEN="$(python3 -c 'import secrets;print(secrets.token_hex(32))')"
  (umask 077; echo "AGENT_SECRET=$GEN" > "$DIR/.env")
  echo; echo "Сгенерирован AGENT_SECRET: $GEN"
  echo "Зарегистрируйте устройство на сервере: rcctl add ИМЯ --address <tailscale-ip> --mac <MAC> --secret $GEN"; echo
fi
chmod 700 "$DIR"; chmod 600 "$DIR/.env"

install -m 644 "$SRC/rc-agent.service" "$HOME/.config/systemd/user/rc-agent.service"
systemctl --user daemon-reload
systemctl --user enable rc-agent.service
systemctl --user restart rc-agent.service

# Брандмауэр (если используется ufw): порт агента только из Tailscale
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow in on tailscale0 to any port 8765 proto tcp
fi

# Wake-on-LAN (опционально)
if [ -n "$WOL_IF" ]; then
  CON="$(nmcli -g GENERAL.CONNECTION device show "$WOL_IF")"
  sudo nmcli connection modify "$CON" 802-3-ethernet.wake-on-lan magic
  sudo ethtool -s "$WOL_IF" wol g || true
  echo "WoL включён для $WOL_IF (подключение '$CON'). MAC: $(cat /sys/class/net/$WOL_IF/address)"
  echo "Не забудьте включить WoL в BIOS/UEFI."
fi
echo "Готово. Статус: systemctl --user status rc-agent"
echo "Автовход GNOME (чтобы агент стартовал после перезагрузки без вас): Параметры -> Пользователи -> Автоматический вход."
