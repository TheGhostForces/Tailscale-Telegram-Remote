#!/usr/bin/env python3
"""Лёгкий агент удалённого управления (FastAPI).

Слушает ТОЛЬКО адрес интерфейса Tailscale. Каждый запрос подписан HMAC,
защищён от повтора (nonce + окно времени) и ограничен по частоте.
"""
import hmac
import io
import ipaddress
import logging
import logging.handlers
import os
import platform
import socket
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

import psutil
import uvicorn
import yaml
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from signing import sign

VERSION = "1.0"
BASE = Path(__file__).resolve().parent
# pythonw.exe запускается без консоли: stdout/stderr = None, и uvicorn на этом падает
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(BASE / "agent-error.log", "a", buffering=1, encoding="utf-8")
load_dotenv(BASE / ".env")
CFG = yaml.safe_load(open(os.environ.get("RC_AGENT_CONFIG", BASE / "config.yaml"), encoding="utf-8")) or {}
SECRET = os.environ.get("AGENT_SECRET", "")
if len(SECRET) < 32:
    sys.exit("AGENT_SECRET не задан или короче 32 символов (см. .env)")

IS_WIN = platform.system() == "Windows"
TS_NET = ipaddress.ip_network("100.64.0.0/10")
TS_WINDOW = 30          # допустимое расхождение часов, сек
RATE = int(CFG.get("rate_limit_per_minute", 30))

log = logging.getLogger("rc-agent")
log.setLevel(logging.INFO)
_h = logging.handlers.RotatingFileHandler(BASE / CFG.get("log_file", "agent.log"),
                                          maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
_nonces: dict[str, float] = {}
_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()


# ---------------------------------------------------------------- безопасность
async def auth(request: Request):
    ip = request.client.host if request.client else ""
    try:
        addr = ipaddress.ip_address(ip)
        if addr not in TS_NET and not addr.is_loopback:
            raise ValueError
    except ValueError:
        log.warning("DENY ip=%s %s", ip, request.url.path)
        raise HTTPException(403)

    now = time.time()
    with _lock:
        q = _hits[ip]
        while q and q[0] < now - 60:
            q.popleft()
        if len(q) >= RATE:
            log.warning("RATE ip=%s", ip)
            raise HTTPException(429)
        q.append(now)

    ts = request.headers.get("X-Timestamp", "")
    nonce = request.headers.get("X-Nonce", "")
    sig = request.headers.get("X-Signature", "")
    body = await request.body()
    ok = ts.isdigit() and abs(now - int(ts)) <= TS_WINDOW and 16 <= len(nonce) <= 64
    if ok:
        ok = hmac.compare_digest(sig, sign(SECRET, request.method, request.url.path, ts, nonce, body))
    if ok:
        with _lock:
            for k in [k for k, v in _nonces.items() if v < now]:
                del _nonces[k]
            if nonce in _nonces:
                ok = False
            else:
                _nonces[nonce] = now + TS_WINDOW * 2
    if not ok:
        log.warning("BADSIG ip=%s %s", ip, request.url.path)
        raise HTTPException(401)
    log.info("OK ip=%s %s %s", ip, request.method, request.url.path)


# ---------------------------------------------------------------- действия
def _later(fn, delay=1.5):
    """Выполнить после отправки HTTP-ответа."""
    threading.Timer(delay, fn).start()


def _run(argv):
    subprocess.run(argv, check=False, timeout=30)


def do_sleep():
    if IS_WIN:
        # Именно сон, а не гибернация: нужно `powercfg /hibernate off` (делает установщик)
        _run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    else:
        _run(["systemctl", "suspend"])


def do_lock():
    if IS_WIN:
        import ctypes
        ctypes.windll.user32.LockWorkStation()
    else:
        _run(["loginctl", "lock-session"])


def do_reboot():
    _run(["shutdown", "/r", "/t", "0"] if IS_WIN else ["systemctl", "reboot"])


def do_shutdown():
    _run(["shutdown", "/s", "/t", "0"] if IS_WIN else ["systemctl", "poweroff"])


ACTIONS = {"sleep": do_sleep, "lock": do_lock, "reboot": do_reboot, "shutdown": do_shutdown}


def screenshot_jpeg() -> bytes:
    from PIL import Image
    try:
        import mss
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])          # все мониторы
            img = Image.frombytes("RGB", shot.size, shot.rgb)
    except Exception as e:
        if IS_WIN:
            raise
        # запасной путь для Wayland/GNOME
        tmp = BASE / "_shot.png"
        r = subprocess.run(["gnome-screenshot", "-f", str(tmp)], capture_output=True, timeout=15)
        if r.returncode != 0 or not tmp.exists():
            raise RuntimeError(f"screenshot failed: {e}")
        img = Image.open(tmp).convert("RGB")
        tmp.unlink(missing_ok=True)
    img.thumbnail((1920, 1080))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=75)
    return buf.getvalue()


def magic_packet(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("bad mac")
    return b"\xff" * 6 + raw * 16


# ---------------------------------------------------------------- эндпоинты
@app.get("/status", dependencies=[Depends(auth)])
def status():
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("C:\\" if IS_WIN else "/")
    bat = psutil.sensors_battery()
    return {
        "host": socket.gethostname(),
        "os": f"{platform.system()} {platform.release()}",
        "uptime_s": int(time.time() - psutil.boot_time()),
        "cpu": psutil.cpu_percent(interval=0.3),
        "ram": {"percent": vm.percent, "used_gb": round(vm.used / 2**30, 1), "total_gb": round(vm.total / 2**30, 1)},
        "disk": {"percent": du.percent, "free_gb": round(du.free / 2**30, 1)},
        "battery": {"percent": round(bat.percent), "plugged": bat.power_plugged} if bat else None,
        "agent": VERSION,
    }


@app.get("/screenshot", dependencies=[Depends(auth)])
def screenshot():
    try:
        return Response(screenshot_jpeg(), media_type="image/jpeg")
    except Exception as e:
        log.error("screenshot: %s", e)
        return JSONResponse({"ok": False, "error": "не удалось сделать скриншот (экран заблокирован / нет сессии / Wayland)"}, 500)


@app.get("/commands", dependencies=[Depends(auth)])
def commands():
    return {"commands": sorted((CFG.get("commands") or {}).keys())}


@app.post("/wake", dependencies=[Depends(auth)])
async def wake(request: Request):
    data = await request.json()
    mac = str(data.get("mac", "")).upper().replace("-", ":")
    known = {d["mac"].upper() for d in (CFG.get("devices") or [])}
    if known and mac not in known:          # белый список MAC необязателен
        raise HTTPException(400, "unknown mac")
    try:
        pkt = magic_packet(mac)
    except ValueError:
        raise HTTPException(400, "bad mac")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for _ in range(3):
            s.sendto(pkt, (CFG.get("wol_broadcast", "255.255.255.255"), int(CFG.get("wol_port", 9))))
            time.sleep(0.3)
    log.info("WAKE %s", mac)
    return {"ok": True}


@app.post("/run/{name}", dependencies=[Depends(auth)])
def run_cmd(name: str):
    argv = (CFG.get("commands") or {}).get(name)
    if not argv:
        raise HTTPException(404)
    log.info("RUN %s", name)
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=30)
        return {"ok": r.returncode == 0, "code": r.returncode, "out": (r.stdout + r.stderr)[-2000:]}
    except Exception as e:
        return {"ok": False, "out": str(e)}


@app.post("/{action}", dependencies=[Depends(auth)])
def power_action(action: str):
    fn = ACTIONS.get(action)
    if not fn:
        raise HTTPException(404)
    log.info("ACTION %s", action)
    _later(fn)
    return {"ok": True, "action": action}


# ---------------------------------------------------------------- запуск
def tailscale_ip():
    for addrs in psutil.net_if_addrs().values():
        for a in addrs:
            if a.family == socket.AF_INET and ipaddress.ip_address(a.address) in TS_NET:
                return a.address
    return None


def main():
    bind = str(CFG.get("bind", "auto"))
    while True:                       # Tailscale может подняться позже агента
        ip = tailscale_ip() if bind == "auto" else bind
        if ip:
            break
        time.sleep(5)
    log.info("start v%s on %s:%s", VERSION, ip, CFG.get("port", 8765))
    try:
        uvicorn.run(app, host=ip, port=int(CFG.get("port", 8765)), log_config=None, log_level="warning")
    except BaseException:
        log.exception("agent crashed")
        raise


if __name__ == "__main__":
    main()
