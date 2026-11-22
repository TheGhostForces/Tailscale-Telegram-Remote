#!/usr/bin/env python3
"""Telegram-бот удалённого управления. Отвечает только владельцу (OWNER_ID).

Интерфейс: постоянная клавиатура внизу и одна «панель» (сообщение с inline-кнопками), которая при
навигации редактируется на месте. Отчёты (статус, скриншот, вывод команды), журнал и уведомления
о состоянии устройств приходят отдельными сообщениями и потом не меняются и не удаляются.
"""
import asyncio
import json
import logging
import logging.handlers
import os
import re
import secrets
import socket
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
import pyotp
import yaml
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import (BotCommand, BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, KeyboardButton, Message, ReplyKeyboardMarkup)
from dotenv import load_dotenv

from signing import make_headers

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
CFG = yaml.safe_load(open(os.environ.get("RC_BOT_CONFIG", BASE / "config.yaml"), encoding="utf-8")) or {}
TOKEN = os.environ.get("BOT_TOKEN") or sys.exit("BOT_TOKEN не задан")
OWNER = int(os.environ.get("OWNER_ID") or sys.exit("OWNER_ID не задан"))
TOTP = pyotp.TOTP(os.environ.get("TOTP_SECRET") or sys.exit("TOTP_SECRET не задан"))

DEVICES: dict = {}   # заполняется из devices.yaml (его ведёт rcctl) и перечитывается на лету
DEVICES_FILE = Path(os.environ.get("RC_DEVICES_FILE", BASE / "devices.yaml"))
TIMERS_FILE = Path(os.environ.get("RC_TIMERS_FILE", BASE / "timers.json"))
CONFIRM = set(CFG.get("confirm_actions", ["sleep", "reboot", "shutdown"]))
CLEAN_USER = bool(CFG.get("cleanup_user_messages", True))
ALERTS = CFG["alerts"] if "alerts" in CFG else {"battery_percent": 15, "disk_percent": 90}
ALERTS = ALERTS or {}

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo(CFG["timezone"]) if CFG.get("timezone") else None
except Exception:
    TZ = None

WAKE_POLL = 5          # сек между проверками при пробуждении
WAKE_SEND_GAP = 1      # пауза между повторами magic packet
TICK = 5               # период фонового цикла (таймеры, подтверждения, ожидаемые события)
PENDING_TTL = 120      # время на подтверждение действия
EXPECT_TTL = {"sleep": 300, "shutdown": 300, "reboot": 900}

log = logging.getLogger("rc-bot")
log.setLevel(logging.INFO)
_h = logging.handlers.RotatingFileHandler(BASE / CFG.get("log_file", "bot.log"),
                                          maxBytes=1_000_000, backupCount=5, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_h)
logging.basicConfig(level=logging.WARNING)


def load_devices() -> dict:
    """devices.yaml (ведёт rcctl) - источник истины; без него читается старый блок devices из config.yaml."""
    if DEVICES_FILE.exists():
        devs = (yaml.safe_load(DEVICES_FILE.read_text(encoding="utf-8")) or {}).get("devices") or {}
        if CFG.get("devices"):
            log.warning("devices в config.yaml игнорируются: используется %s", DEVICES_FILE.name)
    else:
        devs = {}
        for n, d in (CFG.get("devices") or {}).items():
            d = dict(d)
            d["secret"] = os.environ.get(d.get("secret_env", ""), "")
            devs[n] = d
    out = {}
    for n, d in devs.items():
        d = dict(d)
        if len(str(d.get("secret", ""))) < 32:
            raise ValueError(f"у устройства {n} не задан секрет (минимум 32 символа)")
        if not d.get("address"):
            raise ValueError(f"у устройства {n} не указан address")
        d.setdefault("title", n)
        d.setdefault("port", 8765)
        out[str(n)] = d
    return out


try:
    DEVICES.update(load_devices())
except Exception as _e:
    sys.exit(f"Ошибка списка устройств: {_e}")

bot = Bot(TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# ----------------------------------------------------------- состояние
PANEL: dict[int, int] = {}          # chat_id -> id текущей панели (редактируемого сообщения)
PENDING: dict[str, dict] = {}       # token -> действие, ожидающее подтверждения
WAITING_CODE: dict[int, str] = {}   # chat_id -> token, для которого ждём TOTP
USED_CODES: dict[str, float] = {}   # защита TOTP от повторного использования
STRANGER_SEEN: dict[int, float] = {}
ONLINE: dict[str, bool] = {}
MISSES: dict[str, int] = {}
WAKING: set[str] = set()            # устройства, чьё пробуждение ведёт wake_flow
EXPECT: dict[str, dict] = {}        # ожидаемые события после команд sleep/shutdown/reboot
ALERTED: set[tuple] = set()
TIMERS: dict[str, dict] = {}
CMDS: dict[str, list] = {}          # кэш списков команд /run по устройствам
TASKS: set = set()

ACTIONS = {                         # действие: (значок, название)
    "status": ("📊", "Статус"), "wake": ("⚡", "Включить"), "screenshot": ("🖼", "Скриншот"),
    "lock": ("🔒", "Блокировка"), "sleep": ("💤", "Сон"), "reboot": ("🔄", "Перезагрузка"),
    "shutdown": ("⏻", "Выключить"),
}
ACK = {"lock": "экран заблокирован", "sleep": "уходит в сон", "reboot": "перезагружается", "shutdown": "выключается"}
ALL_OK = {"status", "wake", "lock", "sleep", "reboot", "shutdown"}   # действия, доступные для «Все устройства»
TIMER_ACTIONS = ["sleep", "shutdown", "reboot", "lock"]
TIMER_DELAYS = [15, 30, 60, 120, 240, 480]

BTN_DEVICES, BTN_MORE = "🖥 Устройства", "⋯ Ещё"
BTN = {f"{i} {l}": a for a, (i, l) in ACTIONS.items()}          # текст кнопки -> действие
BUTTON_TEXTS = set(BTN) | {BTN_DEVICES, BTN_MORE}


def main_kb() -> ReplyKeyboardMarkup:
    def lab(a):
        return f"{ACTIONS[a][0]} {ACTIONS[a][1]}"
    rows = [[BTN_DEVICES, lab("status"), lab("wake")],
            [lab("screenshot"), lab("lock"), lab("sleep")],
            [lab("reboot"), lab("shutdown"), BTN_MORE]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows],
                               resize_keyboard=True, is_persistent=True, input_field_placeholder="Выберите действие")


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def title(n: str) -> str:
    return DEVICES.get(n, {}).get("title", n)


def dot(n: str) -> str:
    return "🟢" if ONLINE.get(n) else ("🔴" if n in ONLINE else "⚪")


def fmt_dur(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} мин"
    h, m = divmod(minutes, 60)
    return f"{h} ч" + (f" {m} мин" if m else "")


def fmt_left(sec: float) -> str:
    return "менее минуты" if sec < 60 else fmt_dur(int(sec // 60))


def fmt_clock(ts: float) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%H:%M")


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


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


async def notify(text: str, **kw):
    """Отдельное сообщение владельцу. Такие уведомления потом не редактируются и не удаляются."""
    try:
        await bot.send_message(OWNER, text, **kw)
    except Exception as e:
        log.error("notify failed: %s", e)


# ----------------------------------------------------------- сообщения и панель
async def safe_delete(chat_id: int, message_id: int):
    try:
        await bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def cleanup(m: Message):
    """Убрать из чата сообщение пользователя (нажатие кнопки или команда)."""
    if CLEAN_USER:
        await safe_delete(m.chat.id, m.message_id)


async def edit_or_send(chat_id: int, message_id, text: str, markup=None) -> int:
    """Отредактировать сообщение; если его нет или оно недоступно, отправить новое."""
    text = text[:4000]
    if message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
            return message_id
        except Exception as e:
            if "not modified" in str(e).lower():
                return message_id
    m = await bot.send_message(chat_id, text, reply_markup=markup)
    return m.message_id


def _forget_pending(chat_id: int, mid: int):
    for tok in [t for t, p in PENDING.items() if p["chat"] == chat_id and p["msg_id"] == mid]:
        PENDING.pop(tok, None)
    if WAITING_CODE.get(chat_id) not in PENDING:
        WAITING_CODE.pop(chat_id, None)


async def close_panel(chat_id: int, mid):
    """Удалить панель вместе с привязанными к ней подтверждениями."""
    if mid is None:
        return
    if PANEL.get(chat_id) == mid:
        PANEL.pop(chat_id, None)
    _forget_pending(chat_id, mid)
    await safe_delete(chat_id, mid)


async def drop_panel(chat_id: int):
    await close_panel(chat_id, PANEL.get(chat_id))


async def put_panel(chat_id: int, mid, text: str, markup) -> int:
    """Показать панель. mid=None: старая панель удаляется и создаётся новая внизу чата;
    иначе сообщение mid редактируется на месте."""
    if mid is None:
        await drop_panel(chat_id)
        m = await bot.send_message(chat_id, text[:4000], reply_markup=markup)
        PANEL[chat_id] = m.message_id
        return m.message_id
    new = await edit_or_send(chat_id, mid, text, markup)
    PANEL[chat_id] = new
    return new


async def finish_panel(chat_id: int, mid, text: str):
    """Превратить панель в итоговое сообщение (без кнопок). Дальше оно не меняется."""
    if mid is not None and PANEL.get(chat_id) == mid:
        PANEL.pop(chat_id, None)
    if mid is not None:
        _forget_pending(chat_id, mid)
    await edit_or_send(chat_id, mid, text, None)


async def result_message(chat_id: int, mid, text: str):
    """Отчёт: панель удаляется, отчёт приходит новым сообщением и больше не меняется."""
    await close_panel(chat_id, mid)
    await bot.send_message(chat_id, text[:4000])


# ----------------------------------------------------------- агент
async def agent(dev: str, method: str, path: str, json_body: dict | None = None, timeout=10):
    d = DEVICES[dev]
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
    t = title(name)
    if not s:
        return f"🔴 {t} — не в сети"
    up = s["uptime_s"]
    bat = f" · 🔋 {s['battery']['percent']}%{'⚡' if s['battery']['plugged'] else ''}" if s.get("battery") else ""
    return (f"🟢 {t} — онлайн\n"
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
    for _ in range(3):
        if w["mode"] == "router":
            await asyncio.get_running_loop().run_in_executor(None, _send_udp, w["host"], int(w["port"]), magic(d["mac"]))
        elif w["mode"] == "via":
            r = await agent(w["device"], "POST", "/wake", {"mac": d["mac"]})
            if r.status_code != 200:
                raise RuntimeError(f"агент {w['device']} вернул {r.status_code}")
        else:
            raise RuntimeError("неизвестный wake.mode")
        await asyncio.sleep(WAKE_SEND_GAP)


async def wake_flow(chat_id: int, dev: str, mid=None):
    """Разбудить устройство. mid - панель, которую можно использовать как сообщение-ожидание.
    Сообщение-ожидание удаляется, когда устройство ответило, и приходит новое."""
    d = DEVICES.get(dev)
    if not d or not d.get("wake") or not d.get("mac"):
        await finish_panel(chat_id, mid, f"ℹ️ Для {title(dev)} не настроено пробуждение (нет mac/wake).")
        return
    name = d["title"]
    if await get_status(dev):
        await finish_panel(chat_id, mid, f"🟢 {name} уже онлайн")
        return
    WAKING.add(dev)
    try:
        try:
            await send_wake(dev)
        except Exception as e:
            log.error("WAKE %s failed: %s", dev, e)
            await finish_panel(chat_id, mid, f"❌ {name}: не удалось отправить magic packet: {e}")
            return
        log.info("WAKE sent %s", dev)
        wid = await edit_or_send(chat_id, mid, f"📡 Magic packet отправлен ({name}), жду выхода в сеть…")
        deadline = time.time() + int(CFG.get("wake_wait_seconds", 120))
        while time.time() < deadline:
            await asyncio.sleep(WAKE_POLL)
            if await get_status(dev):
                ONLINE[dev] = True
                MISSES[dev] = 0
                await safe_delete(chat_id, wid)
                await bot.send_message(chat_id, f"🟢 {name} проснулся")
                return
        await safe_delete(chat_id, wid)
        await bot.send_message(chat_id, f"⏱ {name} не появился за {CFG.get('wake_wait_seconds', 120)} с. "
                                        "Проверьте, что устройство подключено к домашней сети.")
    finally:
        WAKING.discard(dev)


# ----------------------------------------------------------- действия над устройствами
def expect(dev: str, action: str):
    """После sleep/shutdown/reboot отключение устройства ожидаемо: отдельное уведомление не нужно."""
    if action in EXPECT_TTL:
        EXPECT[dev] = {"kind": action, "until": time.time() + EXPECT_TTL[action], "down": False, "misses": 0}


async def do_action(action: str, dev: str, arg: str | None = None):
    """Выполнить действие на устройстве. Возвращает (успех, текст, фото|None)."""
    if dev not in DEVICES:
        return False, f"{dev}: устройство удалено", None
    t = title(dev)
    log.info("EXEC action=%s dev=%s arg=%s", action, dev, arg)
    try:
        if action == "screenshot":
            r = await agent(dev, "GET", "/screenshot", timeout=30)
            if r.status_code == 200:
                return True, t, r.content
            try:
                err = r.json().get("error", r.status_code)
            except Exception:
                err = r.status_code
            return False, f"{t}: {err}", None
        if action == "run":
            r = await agent(dev, "POST", f"/run/{arg}", timeout=40)
            if r.status_code != 200:
                return False, f"{t}: команда «{arg}» недоступна ({r.status_code})", None
            j = r.json()
            return bool(j["ok"]), f"{t}: {arg}\n{j['out'] or '(нет вывода)'}", None
        r = await agent(dev, "POST", f"/{action}")
        if r.status_code == 200:
            expect(dev, action)
            return True, f"{t}: {ACK.get(action, action)}", None
        return False, f"{t}: ошибка {r.status_code}", None
    except Exception as e:
        log.error("EXEC failed: %s", e)
        return False, f"{t} недоступен", None


def what(p: dict) -> str:
    tgt = "все устройства" if p["dev"] == "*" else title(p["dev"])
    if p["action"] == "run":
        s = f"▶️ Запуск «{p['arg']}» — {tgt}"
    else:
        icon, label = ACTIONS[p["action"]]
        s = f"{icon} {label} — {tgt}"
    return f"⏰ Через {fmt_dur(p['delay'])}: {s}" if p.get("delay") else s


async def start_action(chat_id: int, action: str, dev: str, mid=None, arg=None, delay=None):
    if dev != "*" and dev not in DEVICES:
        await finish_panel(chat_id, mid, "⚠️ Устройство удалено из списка.")
        return
    if action in CONFIRM or action == "run" or delay:
        tok = secrets.token_hex(6)
        p = {"action": action, "dev": dev, "arg": arg, "delay": delay, "chat": chat_id, "msg_id": None,
             "exp": time.time() + PENDING_TTL, "tries": 0}
        markup = kb([[btn("✅ Подтвердить", f"ok:{tok}"), btn("✖ Отмена", f"no:{tok}")]])
        p["msg_id"] = await put_panel(chat_id, mid, f"⚠️ {what(p)}\nПодтвердить действие?", markup)
        PENDING[tok] = p
        return
    await perform(chat_id, action, dev, mid, arg)


async def perform(chat_id: int, action: str, dev: str, mid=None, arg=None):
    if action == "wake":
        targets = [n for n, d in DEVICES.items() if d.get("wake") and d.get("mac")] if dev == "*" else [dev]
        if len(targets) == 1 and mid is not None:
            if PANEL.get(chat_id) == mid:
                PANEL.pop(chat_id, None)
            spawn(wake_flow(chat_id, targets[0], mid))          # панель становится сообщением-ожиданием
        else:
            await close_panel(chat_id, mid)
            for n in targets:
                spawn(wake_flow(chat_id, n))
        return
    targets = list(DEVICES) if dev == "*" else [dev]
    if action == "status":
        res = await asyncio.gather(*(get_status(n, timeout=5) for n in targets))
        await result_message(chat_id, mid, "\n\n".join(fmt_status(n, s) for n, s in zip(targets, res)))
        return
    if action == "screenshot":
        ok, text, photo = await do_action("screenshot", targets[0])
        if ok:
            await close_panel(chat_id, mid)
            await bot.send_photo(chat_id, BufferedInputFile(photo, "screen.jpg"), caption=text)
        else:
            await finish_panel(chat_id, mid, f"❌ {text}")
        return
    if action == "run":
        ok, text, _ = await do_action("run", targets[0], arg)
        await result_message(chat_id, mid, f"{'✅' if ok else '❌'} {text}")
        return
    res = await asyncio.gather(*(do_action(action, n) for n in targets))
    await finish_panel(chat_id, mid, "\n".join(f"{'✅' if ok else '❌'} {text}" for ok, text, _ in res))


async def perform_confirmed(p: dict):
    chat, mid = p["chat"], p["msg_id"]
    if p.get("delay"):
        t = add_timer(chat, p["dev"], p["action"], p["delay"])
        await finish_panel(chat, mid, f"⏰ Запланировано: {title(p['dev'])} — {ACTIONS[p['action']][1].lower()} "
                                      f"через {fmt_dur(p['delay'])} (≈ {fmt_clock(t['due'])})")
        return
    await perform(chat, p["action"], p["dev"], mid, p.get("arg"))


# ----------------------------------------------------------- таймеры
def save_timers():
    try:
        tmp = TIMERS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(TIMERS.values())), encoding="utf-8")
        os.replace(tmp, TIMERS_FILE)
    except OSError as e:
        log.error("save timers failed: %s", e)


def load_timers():
    try:
        for t in json.loads(TIMERS_FILE.read_text(encoding="utf-8")):
            TIMERS[t["id"]] = t
    except (OSError, ValueError, KeyError, TypeError):
        pass


def add_timer(chat_id: int, dev: str, action: str, minutes: int) -> dict:
    tid = secrets.token_hex(3)
    TIMERS[tid] = {"id": tid, "dev": dev, "action": action, "due": time.time() + minutes * 60, "chat": chat_id}
    save_timers()
    log.info("TIMER set dev=%s action=%s min=%s", dev, action, minutes)
    return TIMERS[tid]


async def fire_timer(t: dict, late: float):
    name, label = title(t["dev"]), ACTIONS[t["action"]][1].lower()
    if t["dev"] not in DEVICES:
        await notify(f"⚠️ Таймер отменён: устройство {t['dev']} удалено из списка.")
        return
    if late > 300:
        await notify(f"⚠️ Таймер пропущен ({label} — {name}): бот был недоступен.")
        return
    log.info("TIMER fired dev=%s action=%s", t["dev"], t["action"])
    ok, text, _ = await do_action(t["action"], t["dev"])
    await bot.send_message(t["chat"], f"⏰ По таймеру: {'✅' if ok else '❌'} {text}")


# ----------------------------------------------------------- экраны (панели)
async def flow_pick(chat_id: int, action: str, mid=None):
    """Действие выбрано (кнопкой внизу): выбрать устройство. Одно устройство - сразу к делу."""
    icon, label = ACTIONS[action]
    if not DEVICES:
        await finish_panel(chat_id, mid, "Список устройств пуст. Добавьте устройство командой rcctl add на сервере.")
        return
    devs = [n for n, d in DEVICES.items() if d.get("wake") and d.get("mac")] if action == "wake" else list(DEVICES)
    if not devs:
        await finish_panel(chat_id, mid, f"{icon} {label}: нет подходящих устройств (нужны mac и wake).")
        return
    if len(devs) == 1:
        await start_action(chat_id, action, devs[0], mid)
        return
    rows = [[btn(f"{dot(n)} {title(n)}", f"a:{action}:{n}")] for n in devs]
    if action in ALL_OK:
        rows.append([btn("🌐 Все устройства", f"a:{action}:*")])
    rows.append([btn("✖ Закрыть", "x")])
    await put_panel(chat_id, mid, f"{icon} {label}: выберите устройство", kb(rows))


async def flow_dashboard(chat_id: int, mid=None):
    if not DEVICES:
        await finish_panel(chat_id, mid, "Список устройств пуст. Добавьте устройство командой rcctl add на сервере.")
        return
    names = list(DEVICES)
    res = await asyncio.gather(*(get_status(n) for n in names))
    rows = [[btn(f"{'🟢' if s else '🔴'} {title(n)}", f"d:{n}")] for n, s in zip(names, res)]
    rows.append([btn("🔄 Обновить", "dl"), btn("✖ Закрыть", "x")])
    text = "🖥 Устройства\n" + "\n".join(f"{'🟢' if s else '🔴'} {title(n)}" for n, s in zip(names, res))
    await put_panel(chat_id, mid, text, kb(rows))


async def flow_card(chat_id: int, mid, dev: str):
    if dev not in DEVICES:
        await finish_panel(chat_id, mid, "⚠️ Устройство удалено из списка.")
        return
    s = await get_status(dev, timeout=5)
    rows = []
    if s:
        rows += [[btn("🖼 Скриншот", f"a:screenshot:{dev}"), btn("🔒 Блокировка", f"a:lock:{dev}")],
                 [btn("💤 Сон", f"a:sleep:{dev}"), btn("🔄 Перезагрузка", f"a:reboot:{dev}"),
                  btn("⏻ Выключить", f"a:shutdown:{dev}")],
                 [btn("⏰ Таймер", f"tn:{dev}"), btn("▶️ Команды", f"rn:{dev}")]]
    elif DEVICES[dev].get("wake") and DEVICES[dev].get("mac"):
        rows.append([btn("⚡ Включить", f"a:wake:{dev}")])
    rows.append([btn("⬅️ Назад", "dl"), btn("🔄 Обновить", f"d:{dev}")])
    await put_panel(chat_id, mid, fmt_status(dev, s), kb(rows))


async def flow_more(chat_id: int, mid=None):
    rows = [[btn("⏰ Таймеры", "tm"), btn("▶️ Команды", "rn"), btn("📜 Журнал", "hist")],
            [btn("🔁 Обновить список устройств", "rl")],
            [btn("✖ Закрыть", "x")]]
    await put_panel(chat_id, mid, "⋯ Ещё", kb(rows))


async def flow_run_pick(chat_id: int, mid=None):
    if not DEVICES:
        await finish_panel(chat_id, mid, "Список устройств пуст.")
        return
    if len(DEVICES) == 1:
        await flow_run_list(chat_id, mid, next(iter(DEVICES)))
        return
    rows = [[btn(f"{dot(n)} {title(n)}", f"rn:{n}")] for n in DEVICES]
    rows.append([btn("⬅️ Назад", "more"), btn("✖ Закрыть", "x")])
    await put_panel(chat_id, mid, "▶️ Команды: выберите устройство", kb(rows))


async def flow_run_list(chat_id: int, mid, dev: str):
    if dev not in DEVICES:
        await finish_panel(chat_id, mid, "⚠️ Устройство удалено из списка.")
        return
    try:
        r = await agent(dev, "GET", "/commands", timeout=5)
        names = r.json()["commands"]
    except Exception:
        await finish_panel(chat_id, mid, f"❌ {title(dev)} недоступен")
        return
    CMDS[dev] = names
    if not names:
        await finish_panel(chat_id, mid, f"ℹ️ Для {title(dev)} не настроено команд (раздел commands в config.yaml агента).")
        return
    rows = [[btn(f"▶️ {n}", f"rc:{dev}:{i}")] for i, n in enumerate(names)]
    rows.append([btn("⬅️ Назад", f"d:{dev}"), btn("✖ Закрыть", "x")])
    await put_panel(chat_id, mid, f"▶️ Команды: {title(dev)}", kb(rows))


async def flow_timers(chat_id: int, mid=None):
    now = time.time()
    items = sorted(TIMERS.values(), key=lambda t: t["due"])
    if items:
        text = "⏰ Таймеры\n" + "\n".join(
            f"• {title(t['dev'])} — {ACTIONS[t['action']][1].lower()} в {fmt_clock(t['due'])} "
            f"(через {fmt_left(t['due'] - now)})" for t in items)
    else:
        text = "⏰ Таймеров нет."
    rows = [[btn(f"❌ {title(t['dev'])}: {ACTIONS[t['action']][1].lower()} {fmt_clock(t['due'])}", f"tx:{t['id']}")]
            for t in items]
    rows.append([btn("➕ Новый таймер", "tn")])
    rows.append([btn("⬅️ Назад", "more"), btn("✖ Закрыть", "x")])
    await put_panel(chat_id, mid, text, kb(rows))


async def flow_timer_device(chat_id: int, mid=None):
    if not DEVICES:
        await finish_panel(chat_id, mid, "Список устройств пуст.")
        return
    if len(DEVICES) == 1:
        await flow_timer_action(chat_id, mid, next(iter(DEVICES)))
        return
    rows = [[btn(f"{dot(n)} {title(n)}", f"tn:{n}")] for n in DEVICES]
    rows.append([btn("⬅️ Назад", "tm"), btn("✖ Закрыть", "x")])
    await put_panel(chat_id, mid, "⏰ Новый таймер: выберите устройство", kb(rows))


async def flow_timer_action(chat_id: int, mid, dev: str):
    if dev not in DEVICES:
        await finish_panel(chat_id, mid, "⚠️ Устройство удалено из списка.")
        return
    rows = [[btn(f"{ACTIONS[a][0]} {ACTIONS[a][1]}", f"ta:{dev}:{a}")] for a in TIMER_ACTIONS]
    rows.append([btn("⬅️ Назад", "tm"), btn("✖ Закрыть", "x")])
    await put_panel(chat_id, mid, f"⏰ Таймер для {title(dev)}: что сделать?", kb(rows))


async def flow_timer_delay(chat_id: int, mid, dev: str, action: str):
    row, rows = [], []
    for m in TIMER_DELAYS:
        row.append(btn(fmt_dur(m), f"td:{dev}:{action}:{m}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("⬅️ Назад", f"tn:{dev}"), btn("✖ Закрыть", "x")])
    icon, label = ACTIONS[action]
    await put_panel(chat_id, mid, f"⏰ {icon} {label} — {title(dev)}: через сколько?", kb(rows))


# ----------------------------------------------------------- журнал
LOG_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ (EXEC|WAKE sent|TIMER|TOTP FAIL|STRANGER)\b(.*)$")


def read_history(n: int = 12) -> list:
    path = BASE / CFG.get("log_file", "bot.log")
    try:
        data = path.read_bytes()[-200_000:].decode("utf-8", "replace")
    except OSError:
        return []
    out = [m.groups() for m in (LOG_RE.match(line) for line in data.splitlines()) if m]
    return out[-n:]


def fmt_hist(ts: str, kind: str, rest: str) -> str:
    try:
        t = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").astimezone(TZ).strftime("%d.%m %H:%M")
    except ValueError:
        t = ts
    kv = dict(re.findall(r"(\w+)=(\S+)", rest))
    if kind == "EXEC":
        icon, label = ACTIONS.get(kv.get("action", ""), ("▶️", kv.get("action", "?")))
        extra = f" «{kv['arg']}»" if kv.get("arg") not in (None, "None") else ""
        s = f"{icon} {label}{extra} — {title(kv.get('dev', '?'))}"
    elif kind == "WAKE sent":
        s = f"⚡ Magic packet — {title(rest.strip())}"
    elif kind == "TIMER":
        verb = {"set": "запланирован", "fired": "выполнен", "cancel": "отменён"}.get(rest.split()[0] if rest.split() else "", "")
        s = f"⏰ Таймер {verb}: {ACTIONS.get(kv.get('action', ''), ('', '?'))[1].lower()} — {title(kv.get('dev', '?'))}"
    elif kind == "TOTP FAIL":
        s = "⚠️ Неверный TOTP-код"
    else:
        s = f"🚫 Чужой доступ к боту (id {kv.get('id', '?')})"
    return f"{t} · {s}"


async def flow_history(chat_id: int, mid=None):
    rows = read_history(12)
    text = "📜 Журнал (последние события)\n" + ("\n".join(fmt_hist(*r) for r in rows) if rows else "пока пусто")
    await result_message(chat_id, mid, text)


# ----------------------------------------------------------- обработчики
@router.message(Command("start", "menu", "help"))
async def cmd_start(m: Message):
    await cleanup(m)
    await drop_panel(m.chat.id)
    await m.answer("🤖 Пульт управления. Кнопки внизу.\n"
                   "Команды тоже работают: /devices /status /wake /lock /screenshot /sleep /reboot /shutdown "
                   "/run /timers /log /reload\nПосле имени устройства можно написать all, например /lock all.",
                   reply_markup=main_kb())


@router.message(F.text.in_(BUTTON_TEXTS))
async def on_button(m: Message):
    await cleanup(m)
    if m.text == BTN_DEVICES:
        await flow_dashboard(m.chat.id)
    elif m.text == BTN_MORE:
        await flow_more(m.chat.id)
    else:
        await flow_pick(m.chat.id, BTN[m.text])


@router.message(Command("devices"))
async def cmd_devices(m: Message):
    await cleanup(m)
    await flow_dashboard(m.chat.id)


@router.message(Command("status", "wake", "sleep", "reboot", "shutdown", "lock", "screenshot"))
async def cmd_device_action(m: Message, command: CommandObject):
    await cleanup(m)
    action = command.command
    name = (command.args or "").strip().lower()
    if name in ("all", "все"):
        name = "*" if action in ALL_OK else ""
    if name == "*" or name in DEVICES:
        await start_action(m.chat.id, action, name)
    else:
        await flow_pick(m.chat.id, action)


@router.message(Command("run"))
async def cmd_run(m: Message, command: CommandObject):
    await cleanup(m)
    parts = (command.args or "").split()
    if parts and parts[0].lower() in DEVICES:
        dev = parts[0].lower()
        if len(parts) >= 2:
            await start_action(m.chat.id, "run", dev, None, arg=parts[1])
        else:
            await flow_run_list(m.chat.id, None, dev)
    else:
        await flow_run_pick(m.chat.id)


@router.message(Command("timers"))
async def cmd_timers(m: Message):
    await cleanup(m)
    await flow_timers(m.chat.id)


@router.message(Command("log"))
async def cmd_log(m: Message):
    await cleanup(m)
    await flow_history(m.chat.id)


@router.message(Command("reload"))
async def cmd_reload(m: Message):
    await cleanup(m)
    err = await reload_devices(announce=False)
    await m.answer(f"⚠️ {err}" if err else "Устройства: " + (", ".join(DEVICES) or "список пуст"))


@router.message(F.text.regexp(r"^\d{6}$"))
async def on_code(m: Message):
    chat = m.chat.id
    tok = WAITING_CODE.get(chat)
    p = PENDING.get(tok) if tok else None
    if not p or p["exp"] < time.time():
        return                                    # посторонние цифры игнорируем
    await safe_delete(chat, m.message_id)         # введённый код не остаётся в чате
    code, now = m.text, time.time()
    for k in [k for k, v in USED_CODES.items() if v < now]:
        del USED_CODES[k]
    if code in USED_CODES or not TOTP.verify(code, valid_window=1):
        p["tries"] += 1
        log.warning("TOTP FAIL tries=%s action=%s dev=%s", p["tries"], p["action"], p["dev"])
        if p["tries"] >= 3:
            PENDING.pop(tok, None)
            WAITING_CODE.pop(chat, None)
            await finish_panel(chat, p["msg_id"], "⛔ Слишком много неверных кодов. Действие отменено.")
            await notify("⚠️ 3 неверных TOTP-кода подряд, действие отменено.")
        else:
            await put_panel(chat, p["msg_id"], f"🔐 {what(p)}\n❌ Неверный код, осталось попыток: {3 - p['tries']}. "
                                               "Введите код ещё раз.", kb([[btn("✖ Отмена", f"no:{tok}")]]))
        return
    USED_CODES[code] = now + 90
    PENDING.pop(tok, None)
    WAITING_CODE.pop(chat, None)
    await perform_confirmed(p)


@router.callback_query()
async def on_callback(c: CallbackQuery):
    data = c.data or ""
    chat, mid = c.message.chat.id, c.message.message_id
    kind, _, rest = data.partition(":")

    if kind == "ok":
        p = PENDING.get(rest)
        if not p or p["exp"] < time.time():
            PENDING.pop(rest, None)
            await c.answer("Запрос устарел", show_alert=True)
            await close_panel(chat, mid)
            return
        await c.answer()
        p.update(tries=0, exp=time.time() + PENDING_TTL, msg_id=mid)
        WAITING_CODE[chat] = rest
        await put_panel(chat, mid, f"🔐 {what(p)}\nВведите 6-значный TOTP-код из приложения (2 мин).",
                        kb([[btn("✖ Отмена", f"no:{rest}")]]))
        return
    if kind == "rl":
        err = await reload_devices(announce=False)
        await c.answer(f"Ошибка: {err[:150]}" if err else "Список устройств обновлён", show_alert=bool(err))
        return

    await c.answer()
    if kind == "x":
        await close_panel(chat, mid)
    elif kind == "no":
        PENDING.pop(rest, None)
        if WAITING_CODE.get(chat) == rest:
            WAITING_CODE.pop(chat, None)
        await close_panel(chat, mid)
    elif kind == "dl":
        await flow_dashboard(chat, mid)
    elif kind == "d":
        await flow_card(chat, mid, rest)
    elif kind == "a":
        action, _, dev = rest.partition(":")
        if action in ACTIONS and (dev == "*" or dev in DEVICES):
            await start_action(chat, action, dev, mid)
        else:
            await finish_panel(chat, mid, "⚠️ Устройство удалено из списка.")
    elif kind == "more":
        await flow_more(chat, mid)
    elif kind == "hist":
        await flow_history(chat, mid)
    elif kind == "rn":
        if rest:
            await flow_run_list(chat, mid, rest)
        else:
            await flow_run_pick(chat, mid)
    elif kind == "rc":
        dev, _, idx = rest.partition(":")
        names = CMDS.get(dev) or []
        if dev in DEVICES and idx.isdigit() and int(idx) < len(names):
            await start_action(chat, "run", dev, mid, arg=names[int(idx)])
        else:
            await flow_run_list(chat, mid, dev)       # список устарел: показать заново
    elif kind == "tm":
        await flow_timers(chat, mid)
    elif kind == "tn":
        if rest:
            await flow_timer_action(chat, mid, rest)
        else:
            await flow_timer_device(chat, mid)
    elif kind == "ta":
        dev, _, action = rest.partition(":")
        if dev in DEVICES and action in TIMER_ACTIONS:
            await flow_timer_delay(chat, mid, dev, action)
        else:
            await finish_panel(chat, mid, "⚠️ Устройство удалено из списка.")
    elif kind == "td":
        dev, action, minutes = (rest.split(":") + ["", "", ""])[:3]
        if dev in DEVICES and action in TIMER_ACTIONS and minutes.isdigit() and int(minutes) in TIMER_DELAYS:
            await start_action(chat, action, dev, mid, delay=int(minutes))
        else:
            await finish_panel(chat, mid, "⚠️ Не удалось создать таймер.")
    elif kind == "tx":
        t = TIMERS.pop(rest, None)
        if t:
            save_timers()
            log.info("TIMER cancel dev=%s action=%s", t["dev"], t["action"])
        await flow_timers(chat, mid)


# ----------------------------------------------------------- устройства: перечитывание списка
async def reload_devices(announce: bool = True) -> str | None:
    """Перечитать devices.yaml. Возвращает текст ошибки (прежний список при этом сохраняется)."""
    try:
        new = load_devices()
    except Exception as e:
        log.error("reload devices failed: %s", e)
        if announce:
            await notify(f"⚠️ Не удалось применить список устройств: {e}\nОстаётся прежний список.")
        return str(e)
    old = dict(DEVICES)
    DEVICES.clear()
    DEVICES.update(new)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    changed = sorted(n for n in new if n in old and new[n] != old[n])
    for n in removed:
        ONLINE.pop(n, None)
        MISSES.pop(n, None)
        EXPECT.pop(n, None)
    log.info("devices reloaded +%s -%s ~%s", added, removed, changed)
    if announce and (added or removed or changed):
        parts = [f"добавлено: {', '.join(added)}" if added else "",
                 f"удалено: {', '.join(removed)}" if removed else "",
                 f"изменено: {', '.join(changed)}" if changed else ""]
        await notify("🔧 Список устройств обновлён (" + "; ".join(p for p in parts if p) + ")")
    return None


def _devices_mtime() -> int:
    try:
        return DEVICES_FILE.stat().st_mtime_ns
    except FileNotFoundError:
        return 0


async def watch_devices():
    last = _devices_mtime()
    while True:
        await asyncio.sleep(5)
        cur = _devices_mtime()
        if cur != last:
            last = cur
            await asyncio.sleep(1)      # дать дозаписать файл
            await reload_devices()


# ----------------------------------------------------------- мониторинг, оповещения, фоновые задачи
async def on_state_change(n: str, online: bool):
    """Уведомление о выходе в сеть / пропаже: отдельное сообщение, не редактируется.
    Не дублируем события, которые бот сам вызвал (пробуждение, sleep/shutdown/reboot)."""
    if n in EXPECT or n in WAKING:
        return
    await notify(f"{'🟢 вышел в сеть' if online else '🔴 пропал из сети'}: {title(n)}")


async def check_alerts(n: str, s: dict):
    b, th = s.get("battery"), ALERTS.get("battery_percent")
    key = (n, "battery")
    if b and th:
        if not b["plugged"] and b["percent"] <= th and key not in ALERTED:
            ALERTED.add(key)
            await notify(f"🪫 {title(n)}: заряд {b['percent']}%, не заряжается")
        elif key in ALERTED and (b["plugged"] or b["percent"] > th + 5):
            ALERTED.discard(key)
    th = ALERTS.get("disk_percent")
    key = (n, "disk")
    if th:
        used = s["disk"]["percent"]
        if used >= th and key not in ALERTED:
            ALERTED.add(key)
            await notify(f"💾 {title(n)}: диск заполнен на {used:.0f}% (свободно {s['disk']['free_gb']} ГБ)")
        elif key in ALERTED and used < th - 5:
            ALERTED.discard(key)


async def poll_once():
    """Один проход мониторинга: состояние устройств, уведомления, оповещения."""
    names = list(DEVICES)
    res = await asyncio.gather(*(get_status(n) for n in names))
    for n, s in zip(names, res):
        if n not in DEVICES:
            continue
        if n not in ONLINE:                      # первое наблюдение: просто запомнить состояние
            ONLINE[n] = bool(s)
            MISSES[n] = 0 if s else 2
        else:
            MISSES[n] = 0 if s else MISSES.get(n, 0) + 1
            now_online = MISSES[n] < 2
            if ONLINE[n] != now_online:
                ONLINE[n] = now_online
                await on_state_change(n, now_online)
        if s:
            await check_alerts(n, s)
    for n in [n for n in ONLINE if n not in DEVICES]:
        ONLINE.pop(n, None)
        MISSES.pop(n, None)


async def monitor():
    interval = int(CFG.get("monitor_interval_seconds", 30))
    while True:
        try:
            await poll_once()
        except Exception:
            log.exception("monitor failed")
        await asyncio.sleep(interval)


async def track_expected():
    """Следить за устройствами после sleep/shutdown/reboot: одно итоговое сообщение вместо пары уведомлений."""
    for n, exp in list(EXPECT.items()):
        if n not in DEVICES:
            EXPECT.pop(n, None)
            continue
        if time.time() >= exp["until"]:
            EXPECT.pop(n, None)
            label = ACTIONS[exp["kind"]][1].lower()
            if exp["kind"] == "reboot":
                msg = (f"⚠️ {title(n)} не вернулся в сеть после перезагрузки" if exp["down"]
                       else f"⚠️ {title(n)} не перезагрузился (остался онлайн)")
            else:
                msg = f"⚠️ {title(n)} всё ещё в сети после команды «{label}»"
            await notify(msg)
            continue
        s = await get_status(n, timeout=2)
        if not s:
            exp["misses"] += 1
            if exp["misses"] >= 2 and not exp["down"]:
                exp["down"] = True
                ONLINE[n], MISSES[n] = False, 2      # monitor не пришлёт своё уведомление
                if exp["kind"] != "reboot":
                    EXPECT.pop(n, None)
        else:
            exp["misses"] = 0
            if exp["down"] and exp["kind"] == "reboot":
                EXPECT.pop(n, None)
                ONLINE[n], MISSES[n] = True, 0
                await notify(f"🟢 {title(n)} перезагрузился")


async def tick():
    now = time.time()
    for tok, p in list(PENDING.items()):             # просроченные подтверждения: панель удаляется
        if p["exp"] < now:
            PENDING.pop(tok, None)
            if WAITING_CODE.get(p["chat"]) == tok:
                WAITING_CODE.pop(p["chat"], None)
            await close_panel(p["chat"], p["msg_id"])
    for t in list(TIMERS.values()):                  # сработавшие таймеры
        if t["due"] <= now:
            TIMERS.pop(t["id"], None)
            save_timers()
            spawn(fire_timer(t, now - t["due"]))
    await track_expected()


async def ticker():
    while True:
        await asyncio.sleep(TICK)
        try:
            await tick()
        except Exception:
            log.exception("tick failed")


async def main():
    load_timers()
    spawn(monitor())
    spawn(watch_devices())
    spawn(ticker())
    try:
        await bot.set_my_commands([BotCommand(command="menu", description="Показать кнопки"),
                                   BotCommand(command="devices", description="Устройства"),
                                   BotCommand(command="timers", description="Таймеры"),
                                   BotCommand(command="log", description="Журнал событий"),
                                   BotCommand(command="reload", description="Обновить список устройств")])
    except Exception as e:
        log.error("set_my_commands failed: %s", e)
    if CFG.get("startup_message", False):
        await notify("🤖 Бот запущен", reply_markup=main_kb(), disable_notification=True)
    log.info("bot started")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
