#!/usr/bin/env python3
"""Telegram-бот удалённого управления. Отвечает только владельцу (OWNER_ID)."""
import asyncio
import logging
import logging.handlers
import os
import secrets
import socket
import sys
import time
from pathlib import Path

import httpx
import pyotp
import yaml
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

from signing import make_headers

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
CFG = yaml.safe_load(open(os.environ.get("RC_BOT_CONFIG", BASE / "config.yaml"), encoding="utf-8"))
TOKEN = os.environ.get("BOT_TOKEN") or sys.exit("BOT_TOKEN не задан")
OWNER = int(os.environ.get("OWNER_ID") or sys.exit("OWNER_ID не задан"))
TOTP = pyotp.TOTP(os.environ.get("TOTP_SECRET") or sys.exit("TOTP_SECRET не задан"))

DEVICES: dict = CFG["devices"]
for n, d in DEVICES.items():
    d["secret"] = os.environ.get(d["secret_env"], "")
    if len(d["secret"]) < 32:
        sys.exit(f"{d['secret_env']} для устройства {n} не задан")
CONFIRM = set(CFG.get("confirm_actions", ["sleep", "reboot", "shutdown"]))

log = logging.getLogger("rc-bot")
log.setLevel(logging.INFO)
_h = logging.handlers.RotatingFileHandler(BASE / CFG.get("log_file", "bot.log"),
                                          maxBytes=1_000_000, backupCount=5, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)
logging.basicConfig(level=logging.WARNING)

bot = Bot(TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

PENDING: dict[str, dict] = {}      # token -> {action, device, arg, exp}
WAITING_CODE: dict[int, str] = {}  # user_id -> token
USED_CODES: dict[str, float] = {}  # защита TOTP от повторного использования
STRANGER_SEEN: dict[int, float] = {}
ONLINE: dict[str, bool] = {}
ICON = {"sleep": "💤", "reboot": "🔄", "shutdown": "⏻", "lock": "🔒", "screenshot": "🖼", "run": "▶️"}


# ----------------------------------------------------------- доступ
class OwnerOnly(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id == OWNER:
            return await handler(event, data)
        if user:
            log.warning("STRANGER id=%s username=%s", user.id, user.username)
            if time.time() - STRANGER_SEEN.get(user.id, 0) > 300:
                STRANGER_SEEN[user.id] = time.time()
                await notify(f"⚠️ Попытка доступа к боту: id={user.id} @{user.username}")
        return None  # посторонним не отвечаем


dp.message.outer_middleware(OwnerOnly())
dp.callback_query.outer_middleware(OwnerOnly())


async def notify(text: str):
    try:
        await bot.send_message(OWNER, text)
    except Exception as e:
        log.error("notify failed: %s", e)


# ----------------------------------------------------------- агент
async def agent(dev: str, method: str, path: str, json_body: dict | None = None, timeout=10):
    d = DEVICES[dev]
    import json
    body = json.dumps(json_body).encode() if json_body is not None else b""
    headers = make_headers(d["secret"], method, path, body)
    if body:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as c:
        return await c.request(method, f"http://{d['address']}:{d['port']}{path}", content=body, headers=headers)


async def get_status(dev: str, timeout=3):
    try:
        r = await agent(dev, "GET", "/status", timeout=timeout)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def fmt_status(name: str, s: dict | None) -> str:
    title = DEVICES[name]["title"]
    if not s:
        return f"🔴 {title} — не в сети"
    up = s["uptime_s"]
    bat = f" · 🔋 {s['battery']['percent']}%{'⚡' if s['battery']['plugged'] else ''}" if s.get("battery") else ""
    return (f"🟢 {title} — онлайн\n"
            f"CPU {s['cpu']:.0f}% · RAM {s['ram']['percent']:.0f}% ({s['ram']['used_gb']}/{s['ram']['total_gb']} ГБ) · "
            f"Диск {s['disk']['percent']:.0f}%{bat}\n"
            f"Аптайм {up // 86400}д {up % 86400 // 3600}ч {up % 3600 // 60}м · {s['os']}")


# ----------------------------------------------------------- Wake-on-LAN
def magic(mac: str) -> bytes:
    raw = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    return b"\xff" * 6 + raw * 16


def _send_udp(host: str, port: int, pkt: bytes):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.sendto(pkt, (host, port))


async def send_wake(dev: str):
    d = DEVICES[dev]
    w = d["wake"]
    for i in range(3):
        if w["mode"] == "router":
            await asyncio.get_running_loop().run_in_executor(None, _send_udp, w["host"], int(w["port"]), magic(d["mac"]))
        elif w["mode"] == "via":
            r = await agent(w["device"], "POST", "/wake", {"mac": d["mac"]})
            if r.status_code != 200:
                raise RuntimeError(f"агент {w['device']} вернул {r.status_code}")
        else:
            raise RuntimeError("неизвестный wake.mode")
        await asyncio.sleep(1)


async def wake_flow(chat_id: int, dev: str):
    if await get_status(dev):
        await bot.send_message(chat_id, f"🟢 {DEVICES[dev]['title']} уже онлайн")
        return
    try:
        await send_wake(dev)
    except Exception as e:
        log.error("WAKE %s failed: %s", dev, e)
        await bot.send_message(chat_id, f"❌ Не удалось отправить magic packet: {e}")
        return
    log.info("WAKE sent %s", dev)
    await bot.send_message(chat_id, "📡 Magic packet отправлен, жду выхода в сеть…")
    deadline = time.time() + int(CFG.get("wake_wait_seconds", 120))
    while time.time() < deadline:
        await asyncio.sleep(5)
        if await get_status(dev):
            await bot.send_message(chat_id, f"🟢 {DEVICES[dev]['title']} проснулся")
            return
    note = "\nНоутбук будится только дома." if dev == "laptop" else ""
    await bot.send_message(chat_id, f"⏱ {DEVICES[dev]['title']} не появился за {CFG.get('wake_wait_seconds', 120)} с.{note}")


# ----------------------------------------------------------- выполнение действий
async def execute(chat_id: int, action: str, dev: str, arg: str | None = None):
    title = DEVICES[dev]["title"]
    log.info("EXEC action=%s dev=%s arg=%s", action, dev, arg)
    try:
        if action == "screenshot":
            r = await agent(dev, "GET", "/screenshot", timeout=30)
            if r.status_code == 200:
                await bot.send_photo(chat_id, BufferedInputFile(r.content, "screen.jpg"), caption=title)
            else:
                await bot.send_message(chat_id, f"❌ {title}: {r.json().get('error', r.status_code)}")
        elif action == "run":
            r = await agent(dev, "POST", f"/run/{arg}", timeout=40)
            if r.status_code != 200:
                await bot.send_message(chat_id, f"❌ {title}: команда «{arg}» недоступна ({r.status_code})")
            else:
                j = r.json()
                await bot.send_message(chat_id, f"{'✅' if j['ok'] else '❌'} {title}: {arg}\n{j['out'] or '(нет вывода)'}"[:4000])
        else:
            r = await agent(dev, "POST", f"/{action}")
            ok = r.status_code == 200
            await bot.send_message(chat_id, f"{'✅' if ok else '❌'} {title}: {action}" + ("" if ok else f" ({r.status_code})"))
    except Exception as e:
        log.error("EXEC failed: %s", e)
        await bot.send_message(chat_id, f"❌ {title} недоступен ({type(e).__name__})")


def device_kb(prefix: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=d["title"], callback_data=f"{prefix}:{n}")]
                                                 for n, d in DEVICES.items()])


async def request_action(chat_id: int, action: str, dev: str, arg: str | None = None):
    if action in CONFIRM or action == "run":
        tok = secrets.token_hex(6)
        PENDING[tok] = {"action": action, "dev": dev, "arg": arg, "exp": time.time() + 120}
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"ok:{tok}"),
            InlineKeyboardButton(text="✖ Отмена", callback_data=f"no:{tok}")]])
        what = f"{action} {arg}" if arg else action
        await bot.send_message(chat_id, f"{ICON.get(action, '')} Выполнить «{what}» на {DEVICES[dev]['title']}?", reply_markup=kb)
    else:
        await execute(chat_id, action, dev)


# ----------------------------------------------------------- команды
@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    await m.answer("/devices — список и статус\n/status <имя>\n/wake <имя>\n/lock <имя>\n/screenshot <имя>\n"
                   "/sleep, /reboot, /shutdown <имя> — с подтверждением и TOTP\n/run <имя> <команда> — из белого списка\n\n"
                   "Без имени устройства покажу кнопки. Имена: " + ", ".join(DEVICES))


@router.message(Command("devices"))
async def cmd_devices(m: Message):
    names = list(DEVICES)
    res = await asyncio.gather(*(get_status(n) for n in names))
    await m.answer("\n\n".join(fmt_status(n, s) for n, s in zip(names, res)))


@router.message(Command("status", "wake", "sleep", "reboot", "shutdown", "lock", "screenshot"))
async def cmd_device_action(m: Message, command: CommandObject):
    action = command.command
    name = (command.args or "").strip().lower()
    if name not in DEVICES:
        await m.answer("Выберите устройство:", reply_markup=device_kb(f"pick:{action}"))
        return
    await dispatch(m.chat.id, action, name)


async def dispatch(chat_id: int, action: str, name: str):
    if action == "status":
        await bot.send_message(chat_id, fmt_status(name, await get_status(name, timeout=5)))
    elif action == "wake":
        asyncio.create_task(wake_flow(chat_id, name))
    else:
        await request_action(chat_id, action, name)


@router.message(Command("run"))
async def cmd_run(m: Message, command: CommandObject):
    parts = (command.args or "").split()
    if not parts or parts[0].lower() not in DEVICES:
        await m.answer("Формат: /run <устройство> <команда>. Без команды покажу список.", reply_markup=device_kb("pick:run"))
        return
    dev = parts[0].lower()
    if len(parts) < 2:
        await show_commands(m.chat.id, dev)
        return
    await request_action(m.chat.id, "run", dev, parts[1])


async def show_commands(chat_id: int, dev: str):
    try:
        r = await agent(dev, "GET", "/commands")
        names = r.json()["commands"]
    except Exception:
        await bot.send_message(chat_id, "❌ устройство недоступно")
        return
    await bot.send_message(chat_id, ("Доступные команды: " + ", ".join(names) + f"\n/run {dev} <команда>") if names else "Список команд пуст.")


# ----------------------------------------------------------- кнопки и TOTP
@router.callback_query(F.data.startswith("pick:"))
async def cb_pick(c: CallbackQuery):
    _, action, name = c.data.split(":")
    await c.answer()
    if name not in DEVICES:
        return
    if action == "run":
        await show_commands(c.message.chat.id, name)
    else:
        await dispatch(c.message.chat.id, action, name)


@router.callback_query(F.data.startswith("no:"))
async def cb_no(c: CallbackQuery):
    PENDING.pop(c.data[3:], None)
    WAITING_CODE.pop(c.from_user.id, None)
    await c.answer("Отменено")
    await c.message.edit_text("✖ Отменено")


@router.callback_query(F.data.startswith("ok:"))
async def cb_ok(c: CallbackQuery):
    tok = c.data[3:]
    p = PENDING.get(tok)
    if not p or p["exp"] < time.time():
        PENDING.pop(tok, None)
        await c.answer("Запрос устарел", show_alert=True)
        return
    p.update(tries=0, exp=time.time() + 120)
    WAITING_CODE[c.from_user.id] = tok
    await c.answer()
    await c.message.edit_text("🔐 Введите 6-значный TOTP-код из приложения-аутентификатора (2 мин).")


@router.message(F.text.regexp(r"^\d{6}$"))
async def on_code(m: Message):
    tok = WAITING_CODE.get(m.from_user.id)
    p = PENDING.get(tok) if tok else None
    if not p or p["exp"] < time.time():
        WAITING_CODE.pop(m.from_user.id, None)
        return
    code = m.text
    try:
        await m.delete()
    except Exception:
        pass
    now = time.time()
    for k in [k for k, v in USED_CODES.items() if v < now]:
        del USED_CODES[k]
    if code in USED_CODES or not TOTP.verify(code, valid_window=1):
        p["tries"] += 1
        log.warning("TOTP FAIL tries=%s action=%s dev=%s", p["tries"], p["action"], p["dev"])
        if p["tries"] >= 3:
            PENDING.pop(tok, None)
            WAITING_CODE.pop(m.from_user.id, None)
            await notify("⚠️ 3 неверных TOTP-кода подряд, действие отменено.")
            await m.answer("⛔ Слишком много попыток. Действие отменено.")
        else:
            await m.answer(f"❌ Неверный код. Осталось попыток: {3 - p['tries']}")
        return
    USED_CODES[code] = now + 90
    PENDING.pop(tok, None)
    WAITING_CODE.pop(m.from_user.id, None)
    await execute(m.chat.id, p["action"], p["dev"], p["arg"])


# ----------------------------------------------------------- мониторинг
async def monitor():
    interval = int(CFG.get("monitor_interval_seconds", 30))
    misses = {n: 0 for n in DEVICES}
    while True:
        res = await asyncio.gather(*(get_status(n) for n in DEVICES))
        for n, s in zip(DEVICES, res):
            misses[n] = 0 if s else misses[n] + 1
            now_online = misses[n] < 2
            if n in ONLINE and ONLINE[n] != now_online:
                await notify(f"{'🟢 вышел в сеть' if now_online else '🔴 пропал из сети'}: {DEVICES[n]['title']}")
            ONLINE[n] = now_online
        await asyncio.sleep(interval)


async def main():
    asyncio.create_task(monitor())
    log.info("bot started")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
