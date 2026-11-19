#!/usr/bin/env python3
"""rcctl - управление списком устройств бота (devices.yaml). Запускать на VPS: sudo rcctl ...

  rcctl list [--check]                 список устройств (--check: проверить доступность агентов)
  rcctl add NAME --address IP|ИМЯ ...  добавить компьютер с агентом (будет доступен в Telegram-боте)
  rcctl add-client NAME                добавить клиент только для RustDesk (телефон): в боте его НЕТ
  rcctl show NAME                      параметры и команды установки (с секретом)
  rcctl rotate NAME                    выпустить новый секрет агента
  rcctl remove NAME                    убрать устройство
  rcctl migrate                        перенести устройства из config.yaml + .env в devices.yaml

Бот подхватывает изменения файла сам, перезапуск не нужен.
"""
import argparse
import ipaddress
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

DEFAULT_FILE = Path(os.environ.get("RC_DEVICES_FILE", "/opt/rc-bot/devices.yaml"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,19}$")
MAC_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
HEADER = ("# Список устройств. Ведётся утилитой rcctl; бот подхватывает изменения сам.\n"
          "# Файл содержит секреты: права 600, в git не добавлять.\n")
FIRST_WAKE_PORT = 40009


class CliError(Exception):
    pass


# ------------------------------------------------------------------ файл
def load(path: Path) -> dict:
    data = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["devices"] = data.get("devices") or {}
    data["clients"] = data.get("clients") or {}
    return data


def save(path: Path, data: dict):
    out = {"devices": data["devices"]}
    if data["clients"]:
        out["clients"] = data["clients"]
    text = HEADER + yaml.safe_dump(out, allow_unicode=True, sort_keys=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".devices-")
    except PermissionError:
        raise CliError(f"нет прав на запись в {path.parent}: запустите через sudo")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    owner = None
    if path.exists():
        st = path.stat()
        owner = (st.st_uid, st.st_gid)
    elif os.geteuid() == 0:
        try:
            import pwd
            pw = pwd.getpwnam("rcbot")
            owner = (pw.pw_uid, pw.pw_gid)
        except KeyError:
            pass
    if owner and os.geteuid() == 0:
        os.chown(tmp, *owner)
    os.replace(tmp, path)       # атомарно: бот никогда не увидит полузаписанный файл


# ------------------------------------------------------------------ проверки
def check_name(data: dict, name: str):
    if not NAME_RE.match(name):
        raise CliError("имя: латиница в нижнем регистре, цифры и дефис, до 20 символов (например, pc2)")
    if name in data["devices"] or name in data["clients"]:
        raise CliError(f"имя «{name}» уже занято")


def norm_mac(mac: str) -> str:
    m = mac.strip().upper().replace("-", ":")
    if not MAC_RE.match(m):
        raise CliError(f"неверный MAC: {mac} (ожидается AA:BB:CC:DD:EE:FF)")
    return m


def resolve_address(addr: str) -> str:
    try:
        ipaddress.ip_address(addr)
        return addr
    except ValueError:
        pass
    try:
        r = subprocess.run(["tailscale", "ip", "-4", addr], capture_output=True, text=True, timeout=10)
        ip = r.stdout.split()[0] if r.returncode == 0 and r.stdout.split() else ""
        ipaddress.ip_address(ip)
        return ip
    except Exception:
        raise CliError(f"не удалось определить Tailscale-адрес «{addr}». Укажите IP (100.x.y.z) "
                       "или имя устройства из `tailscale status`")


def next_wake_port(data: dict) -> int:
    ports = [int(d["wake"]["port"]) for d in data["devices"].values()
             if (d.get("wake") or {}).get("mode") == "router"]
    return max(ports + [FIRST_WAKE_PORT - 1]) + 1


def known_wake_host(data: dict):
    for d in data["devices"].values():
        w = d.get("wake") or {}
        if w.get("mode") == "router" and w.get("host"):
            return w["host"]
    return None


def need(data: dict, name: str, where: str = "devices") -> dict:
    if name not in data[where]:
        raise CliError(f"устройство «{name}» не найдено (см. rcctl list)")
    return data[where][name]


# ------------------------------------------------------------------ вывод
def install_hint(name: str, d: dict):
    s = d["secret"]
    print("\nУстановка агента на устройстве (папка agent/ из репозитория):")
    print("  Windows (PowerShell от администратора):")
    print(f"    powershell -ExecutionPolicy Bypass -File .\\install-windows.ps1 -Secret {s} -ConfigureWol")
    print("  Debian/Linux (обычный пользователь):")
    print(f"    bash install-linux.sh --secret {s} --wol ИМЯ_ИНТЕРФЕЙСА")
    w = d.get("wake") or {}
    if w.get("mode") == "router":
        print("\nПробуждение через роутер: резерв DHCP и ARP-привязка для этого MAC, а также виртуальный сервер "
              f"UDP: внешний порт {w['port']} -> внутренний IP устройства, порт 9.")
    elif w.get("mode") == "via":
        print(f"\nПробуждение: через устройство «{w['device']}» по локальной сети.")
    elif not d.get("mac"):
        print("\nMAC не указан, пробуждение для этого устройства отключено.")
    print("\nСекрет показан для установки; повторно его покажет `rcctl show`.")


# ------------------------------------------------------------------ команды
def cmd_list(a, path):
    data = load(path)
    if not data["devices"] and not data["clients"]:
        print("Список пуст. Добавьте устройство: rcctl add NAME --address 100.x.y.z --mac AA:BB:..")
        return
    if data["devices"]:
        print("Устройства (управление через бота):")
        for n, d in data["devices"].items():
            w = d.get("wake") or {}
            wake = {"router": f"WoL роутер {w.get('host')}:{w.get('port')}", "via": f"WoL через {w.get('device')}"}.get(w.get("mode"), "без WoL")
            state = ""
            if a.check:
                state = "  [" + check_agent(d) + "]"
            print(f"  {n:<14} {d['address']:<16} {d.get('mac', '-'):<18} {wake}  - {d.get('title', n)}{state}")
    if data["clients"]:
        print("\nКлиенты только RustDesk (в боте не показываются):")
        for n, c in data["clients"].items():
            print(f"  {n:<14} {c.get('title', n)}" + (f"  ({c['note']})" if c.get("note") else ""))


def check_agent(d: dict) -> str:
    from signing import make_headers
    url = f"http://{d['address']}:{d.get('port', 8765)}/status"
    req = urllib.request.Request(url, headers=make_headers(d["secret"], "GET", "/status"))
    try:
        json.load(urllib.request.urlopen(req, timeout=4))
        return "онлайн"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}"
    except Exception:
        return "не в сети"


def cmd_add(a, path):
    data = load(path)
    check_name(data, a.name)
    entry = {"title": a.title or a.name, "address": resolve_address(a.address), "port": a.port}
    secret = a.secret or secrets.token_hex(32)
    if len(secret) < 32:
        raise CliError("секрет должен быть не короче 32 символов")
    entry["secret"] = secret
    if a.mac:
        entry["mac"] = norm_mac(a.mac)
        if a.wake_via:
            need(data, a.wake_via)
            entry["wake"] = {"mode": "via", "device": a.wake_via}
        else:
            host = a.wake_host or known_wake_host(data)
            if not host:
                raise CliError("укажите --wake-host (белый IP или DDNS роутера) либо --wake-via ДРУГОЕ_УСТРОЙСТВО")
            port = next_wake_port(data) if a.wake_port in (None, "auto") else int(a.wake_port)
            entry["wake"] = {"mode": "router", "host": host, "port": port}
    data["devices"][a.name] = entry
    save(path, data)
    print(f"Добавлено: {a.name} ({entry['address']}). Бот подхватит устройство в течение нескольких секунд.")
    install_hint(a.name, entry)


def cmd_add_client(a, path):
    data = load(path)
    check_name(data, a.name)
    data["clients"][a.name] = {k: v for k, v in {"title": a.title or a.name, "note": a.note}.items() if v}
    save(path, data)
    print(f"Клиент «{a.name}» записан (только RustDesk, без управления через бота).")
    print("На устройстве: установите Tailscale (тот же аккаунт) и RustDesk, укажите ваш сервер и ключ. Больше ничего не нужно.")


def cmd_show(a, path):
    data = load(path)
    if a.name in data["clients"]:
        print(yaml.safe_dump({a.name: data["clients"][a.name]}, allow_unicode=True, sort_keys=False))
        return
    d = need(data, a.name)
    print(yaml.safe_dump({a.name: {k: v for k, v in d.items() if k != "secret"}}, allow_unicode=True, sort_keys=False))
    install_hint(a.name, d)


def cmd_rotate(a, path):
    data = load(path)
    d = need(data, a.name)
    d["secret"] = secrets.token_hex(32)
    save(path, data)
    print(f"Новый секрет для «{a.name}» выпущен. До переустановки агента связь с этим устройством прервётся.")
    install_hint(a.name, d)


def cmd_remove(a, path):
    data = load(path)
    where = "devices" if a.name in data["devices"] else "clients" if a.name in data["clients"] else None
    if not where:
        raise CliError(f"«{a.name}» не найдено (см. rcctl list)")
    if not a.yes and input(f"Удалить «{a.name}»? [y/N] ").strip().lower() != "y":
        print("Отменено.")
        return
    dependents = [n for n, d in data["devices"].items() if (d.get("wake") or {}).get("device") == a.name]
    if dependents:
        print(f"Внимание: через «{a.name}» будятся {', '.join(dependents)}. Для них настройте wake заново.")
    del data[where][a.name]
    save(path, data)
    print(f"Удалено из списка: {a.name}. Остаётся закрыть доступ:")
    print("  1. Tailscale admin console -> Machines -> устройство -> Remove (это и есть отзыв доступа).")
    if where == "devices":
        print("  2. На самом устройстве удалить агента: uninstall-windows.ps1 / uninstall-linux.sh.")
        print("  3. На роутере удалить проброс UDP-порта, резерв DHCP и ARP-привязку.")
    print("  RustDesk-сервер клиентов не хранит; удалите RustDesk на устройстве. Если устройство было потеряно "
          "или украдено, смените постоянные пароли на остальных и при необходимости ключ сервера.")


def parse_env(p: Path) -> dict:
    out = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip("'\"")
    return out


def cmd_migrate(a, path):
    data = load(path)
    if data["devices"] and not a.force:
        raise CliError(f"{path} уже содержит устройства (--force, чтобы заменить)")
    cfg = path.parent / "config.yaml"
    legacy = (yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}).get("devices") if cfg.exists() else None
    if not legacy:
        raise CliError(f"в {cfg} нет блока devices")
    env = parse_env(path.parent / ".env")
    for n, d in legacy.items():
        secret = env.get(d.get("secret_env", ""), "")
        if len(secret) < 32:
            raise CliError(f"для «{n}» не найден секрет {d.get('secret_env')} в .env")
        e = {k: v for k, v in d.items() if k != "secret_env"}
        e["secret"] = secret
        e.setdefault("port", 8765)
        data["devices"][n] = e
    save(path, data)
    print(f"Перенесено устройств: {len(legacy)} -> {path}. Бот переключится на новый файл сам.")
    print("Блок devices в config.yaml и переменные *_SECRET в .env теперь не нужны, их можно удалить.")


# ------------------------------------------------------------------ main
def main():
    ap = argparse.ArgumentParser(prog="rcctl", description="Управление устройствами удалённого управления")
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE, help=f"devices.yaml (по умолчанию {DEFAULT_FILE})")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list"); s.add_argument("--check", action="store_true"); s.set_defaults(fn=cmd_list)

    s = sub.add_parser("add", help="компьютер с агентом")
    s.add_argument("name")
    s.add_argument("--address", required=True, help="Tailscale IP (100.x.y.z) или имя из `tailscale status`")
    s.add_argument("--title", help="название в боте")
    s.add_argument("--mac", help="MAC для Wake-on-LAN")
    s.add_argument("--wake-host", help="белый IP/DDNS роутера (по умолчанию как у уже добавленных)")
    s.add_argument("--wake-port", help="внешний UDP-порт на роутере или auto (по умолчанию auto)")
    s.add_argument("--wake-via", metavar="УСТРОЙСТВО", help="будить по LAN через другое устройство с агентом")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--secret", help="свой секрет (по умолчанию генерируется)")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("add-client", help="телефон и т.п.: только RustDesk, без бота")
    s.add_argument("name"); s.add_argument("--title"); s.add_argument("--note")
    s.set_defaults(fn=cmd_add_client)

    for n, fn, h in (("show", cmd_show, "параметры и команды установки"), ("rotate", cmd_rotate, "новый секрет")):
        s = sub.add_parser(n, help=h); s.add_argument("name"); s.set_defaults(fn=fn)

    s = sub.add_parser("remove"); s.add_argument("name"); s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("migrate"); s.add_argument("--force", action="store_true"); s.set_defaults(fn=cmd_migrate)

    a = ap.parse_args()
    try:
        a.fn(a, a.file)
    except CliError as e:
        sys.exit(f"Ошибка: {e}")
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    main()
