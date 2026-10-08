"""
O'TOV BALIQ MARKAZI - Telegram bot (to'liq versiya)

Ishga tushirish:
    pip install "aiogram>=3.7,<4"
    python bot.py

Python 3.9+ talab qilinadi.
"""
import subprocess
import sys

# aiogram o'rnatilmagan bo'lsa (masalan, requirements.txt topilmasa), o'zi o'rnatadi
try:
    import aiogram  # noqa: F401
except ImportError:
    for extra in ([], ["--break-system-packages"]):
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install",
                                   "aiogram>=3.7,<4", *extra])
            break
        except Exception:
            continue

import asyncio
import glob
import html
import json
import logging
import os
import re
import sqlite3
from datetime import date as date_cls, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    CallbackQuery, ErrorEvent, FSInputFile, InlineKeyboardButton,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ======================= SOZLAMALAR (o'zgarmas) =======================
BOT_TOKEN = "8909146315:AAGPf3Z7BaSF9bFmy7nzU547a6pN6h5GVGM"
SUPER_ADMIN_ID = 7740552653
MAIN_GROUP_ID = -1003989681202
BUSINESS_NAME = "O'TOV BALIQ MARKAZI"
# Railway'da Volume /data ga ulansa, baza shu yerda saqlanadi (qayta deploydan keyin ham o'chmaydi)
DATA_DIR = "/data" if os.path.isdir("/data") else "."
DB_FILE = os.path.join(DATA_DIR, "otov.db")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
TZ_HOURS = 5            # Toshkent vaqti (UTC+5)
UNITS = ("kg", "dona", "porsiya")
SLOT_STEP = 30          # vaqt qadami (daqiqa)
MIN_HOURS = 1
MAX_HOURS = 4
MAX_PENDING = 3         # bitta mijozning javob kutayotgan buyurtmalari
ADMIN_REMIND_MIN = 15   # admin javob bermasa (daqiqa)
BOOKING_REMIND_MIN = 60  # band vaqtidan oldin eslatma (daqiqa)
# =====================================================================

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("otov")

WEEKDAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba",
            "Juma", "Shanba", "Yakshanba"]
WD_SHORT = ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"]
STATUS = {
    "pending": "⏳ Kutilmoqda",
    "confirmed": "✅ Tasdiqlangan",
    "rejected": "❌ Rad etilgan",
    "cancelled": "🚫 Mijoz bekor qildi",
    "done": "🏁 Yakunlangan",
    "expired": "⌛ Muddati o'tgan",
}
ACTIVE = ("pending", "confirmed")

# ------------------------------ BAZA ---------------------------------
db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row


def run(sql, args=()):
    cur = db.execute(sql, args)
    db.commit()
    return cur


def one(sql, args=()):
    r = db.execute(sql, args).fetchone()
    return dict(r) if r else None


def many(sql, args=()):
    return [dict(r) for r in db.execute(sql, args).fetchall()]


def init_db():
    run("""CREATE TABLE IF NOT EXISTS admins(
        user_id INTEGER PRIMARY KEY, name TEXT, added_by INTEGER)""")
    run("""CREATE TABLE IF NOT EXISTS groups(
        chat_id INTEGER PRIMARY KEY, title TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS rooms(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        number INTEGER UNIQUE NOT NULL, name TEXT NOT NULL)""")
    run("""CREATE TABLE IF NOT EXISTS menu(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, unit TEXT NOT NULL, price INTEGER NOT NULL)""")
    run("""CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY, value TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY, name TEXT, username TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS fsm(
        key TEXT PRIMARY KEY, state TEXT, data TEXT)""")
    run("""CREATE TABLE IF NOT EXISTS bookings(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, user_name TEXT, phone TEXT,
        room_id INTEGER, room_number INTEGER, room_name TEXT,
        date TEXT, start_min INTEGER, end_min INTEGER,
        note TEXT DEFAULT '', status TEXT DEFAULT 'pending',
        reject_reason TEXT DEFAULT '',
        handled_by INTEGER, handled_name TEXT DEFAULT '',
        created_at TEXT, finished_at TEXT,
        reminded INTEGER DEFAULT 0, admin_reminded INTEGER DEFAULT 0,
        group_msgs TEXT DEFAULT '{}')""")
    run("""CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, user_name TEXT, phone TEXT,
        items TEXT, total INTEGER,
        note TEXT DEFAULT '', status TEXT DEFAULT 'pending',
        reject_reason TEXT DEFAULT '', ready_text TEXT DEFAULT '',
        handled_by INTEGER, handled_name TEXT DEFAULT '',
        created_at TEXT, finished_at TEXT,
        admin_reminded INTEGER DEFAULT 0,
        group_msgs TEXT DEFAULT '{}')""")
    run("INSERT OR IGNORE INTO admins(user_id,name,added_by) VALUES(?,?,?)",
        (SUPER_ADMIN_ID, "Super admin", SUPER_ADMIN_ID))
    run("INSERT OR IGNORE INTO groups(chat_id,title) VALUES(?,?)",
        (MAIN_GROUP_ID, "Asosiy guruh"))
    for k, v in {"work_start": "10:00", "work_end": "23:00",
                 "duration": "2", "phone": "", "last_report": ""}.items():
        run("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))


def get_setting(key):
    r = one("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else ""


def set_setting(key, value):
    run("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))


def is_admin(uid):
    return one("SELECT 1 AS x FROM admins WHERE user_id=?", (uid,)) is not None


def is_super(uid):
    return uid == SUPER_ADMIN_ID


def get_b(bid):
    return one("SELECT * FROM bookings WHERE id=?", (bid,))


def get_o(oid):
    return one("SELECT * FROM orders WHERE id=?", (oid,))


# --------------------- FSM: SQLite'da saqlanadigan xotira -------------
class SQLiteStorage(BaseStorage):
    """Bot qayta ishga tushsa ham holat va ma'lumotlar yo'qolmaydi."""

    @staticmethod
    def _k(key: StorageKey) -> str:
        parts = (key.bot_id, key.chat_id, key.user_id,
                 getattr(key, "thread_id", None) or 0,
                 getattr(key, "destiny", "default"))
        return ":".join(str(x) for x in parts)

    async def set_state(self, key, state=None):
        s = state.state if isinstance(state, State) else state
        k = self._k(key)
        run("INSERT OR IGNORE INTO fsm(key,state,data) VALUES(?,NULL,'{}')", (k,))
        run("UPDATE fsm SET state=? WHERE key=?", (s, k))

    async def get_state(self, key):
        r = one("SELECT state FROM fsm WHERE key=?", (self._k(key),))
        return r["state"] if r else None

    async def set_data(self, key, data):
        k = self._k(key)
        run("INSERT OR IGNORE INTO fsm(key,state,data) VALUES(?,NULL,'{}')", (k,))
        run("UPDATE fsm SET data=? WHERE key=?",
            (json.dumps(dict(data), ensure_ascii=False), k))

    async def get_data(self, key):
        r = one("SELECT data FROM fsm WHERE key=?", (self._k(key),))
        if r and r["data"]:
            try:
                return json.loads(r["data"])
            except ValueError:
                return {}
        return {}

    async def close(self):
        return None


STORAGE = SQLiteStorage()


def user_state(bot: Bot, uid: int) -> FSMContext:
    """Boshqa foydalanuvchining (shaxsiy chat) holatini boshqarish uchun."""
    return FSMContext(storage=STORAGE,
                      key=StorageKey(bot_id=bot.id, chat_id=uid, user_id=uid))


# ---------------------------- YORDAMCHILAR ----------------------------
def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def money(n):
    return f"{int(n):,}".replace(",", " ") + " so'm"


def now():
    tz = timezone(timedelta(hours=TZ_HOURS))
    return datetime.now(tz).replace(tzinfo=None)


def ts(dt=None):
    return (dt or now()).strftime("%Y-%m-%d %H:%M:%S")


def parse_ts(s):
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def hhmm(mins):
    return f"{mins // 60:02d}:{mins % 60:02d}"


def to_min(t):
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def fmt_date(d):
    y, m, dd = d.split("-")
    return f"{dd}.{m}.{y}"


def weekday(d):
    return WEEKDAYS[datetime.strptime(d, "%Y-%m-%d").weekday()]


def start_dt(b):
    return datetime.strptime(b["date"], "%Y-%m-%d") + timedelta(minutes=b["start_min"])


def norm_time(t):
    t = (t or "").strip()
    if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
        return None
    h, m = t.split(":")
    return f"{int(h):02d}:{m}"


def norm_phone(t):
    d = re.sub(r"\D", "", t or "")
    if len(d) == 9:
        d = "998" + d
    if len(d) == 12 and d.startswith("998"):
        return "+" + d
    return None


def dec(s):
    try:
        return Decimal(str(s).replace(",", ".").strip())
    except (InvalidOperation, ValueError):
        return None


def parse_qty(text, unit):
    v = dec(text)
    if v is None or not v.is_finite():
        return None
    if v <= 0 or v > 100:
        return None
    if unit == "kg":
        if v != v.quantize(Decimal("0.01")):
            return None
    else:
        if v != v.to_integral_value():
            return None
        v = v.to_integral_value()
    return v


def fmt_qty(v):
    return format(Decimal(v).normalize(), "f")


def calc_sum(price, qty):
    return int((Decimal(price) * Decimal(qty)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def kb(*rows):
    b = InlineKeyboardBuilder()
    for row in rows:
        b.row(*[InlineKeyboardButton(text=t, callback_data=d) for t, d in row])
    return b.as_markup()


CANCEL = kb([("❌ Bekor qilish", "cancel")])   # admin uchun
CX = kb([("❌ Bekor qilish", "cx")])           # mijoz uchun


async def show(c: CallbackQuery, text, markup=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await c.bot.send_message(c.from_user.id, text, reply_markup=markup)
    except Exception:
        await c.bot.send_message(c.from_user.id, text, reply_markup=markup)


async def send(ev, text, markup=None):
    if isinstance(ev, CallbackQuery):
        await show(ev, text, markup)
    else:
        await ev.answer(text, reply_markup=markup)


async def notify(bot: Bot, uid, text, markup=None):
    try:
        await bot.send_message(uid, text, reply_markup=markup)
        return True
    except Exception as e:
        log.warning("Xabar yuborilmadi (%s): %s", uid, e)
        return False


def user_link(row):
    return f'<a href="tg://user?id={row["user_id"]}">{esc(row["user_name"])}</a>'


def admin_phone_line():
    ph = get_setting("phone")
    return f"\n☎️ Administrator: {esc(ph)}" if ph else ""


class AdminFilter(BaseFilter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user) and is_admin(event.from_user.id)


class SuperFilter(BaseFilter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user) and is_super(event.from_user.id)


# ------------------------------ HOLATLAR ------------------------------
class RoomAdd(StatesGroup):
    number = State()
    name = State()


class RoomEdit(StatesGroup):
    value = State()


class MenuAdd(StatesGroup):
    name = State()
    unit = State()
    price = State()


class MenuEdit(StatesGroup):
    value = State()


class SettingEdit(StatesGroup):
    value = State()


class AdminAdd(StatesGroup):
    uid = State()


class GroupAdd(StatesGroup):
    chat_id = State()


class Rej(StatesGroup):
    reason = State()


class Ready(StatesGroup):
    text = State()


class BK(StatesGroup):
    date = State()
    time = State()
    dur = State()
    room = State()
    note = State()
    phone = State()


class OR(StatesGroup):
    menu = State()
    qty = State()
    confirm = State()
    cart = State()
    note = State()
    phone = State()


# ------------------------------ ROUTERLAR -----------------------------
PRIVATE = F.chat.type == "private"

start_router = Router()
start_router.message.filter(PRIVATE)
client = Router()
client.message.filter(PRIVATE)
admin = Router()
admin.message.filter(AdminFilter(), PRIVATE)
admin.callback_query.filter(AdminFilter())
sup = Router()
sup.message.filter(SuperFilter(), PRIVATE)
sup.callback_query.filter(SuperFilter())
fb = Router()

BTN_BOOK = "🪑 Joy buyurtma qilish"
BTN_ORDER = "🥡 Olib ketish"
BTN_MY = "📋 Buyurtmalarim"
BTN_CONTACT = "☎️ Administrator bilan bog'lanish"

CLIENT_KB = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_BOOK), KeyboardButton(text=BTN_ORDER)],
        [KeyboardButton(text=BTN_MY), KeyboardButton(text=BTN_CONTACT)],
    ],
    resize_keyboard=True,
)


# ====================== BAND/BO'SH TEKSHIRUVI ===========================
def room_busy(room_id, date, s, e, exclude=None):
    rows = many("SELECT id,start_min,end_min FROM bookings "
                "WHERE room_id=? AND date=? AND status='confirmed'", (room_id, date))
    for r in rows:
        if exclude and r["id"] == exclude:
            continue
        if r["start_min"] < e and s < r["end_min"]:
            return True
    return False


def work_range():
    return to_min(get_setting("work_start")), to_min(get_setting("work_end"))


def valid_slots(date):
    try:
        d = datetime.strptime(date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return []
    n = now()
    if d < n.date():
        return []
    ws, we = work_range()
    slots, s = [], ws
    while s + MIN_HOURS * 60 <= we:
        slots.append(s)
        s += SLOT_STEP
    if d == n.date():
        cur = n.hour * 60 + n.minute
        slots = [x for x in slots if x > cur]
    return slots


def pending_count(uid):
    a = one("SELECT COUNT(*) AS n FROM bookings WHERE user_id=? AND status='pending'", (uid,))["n"]
    b = one("SELECT COUNT(*) AS n FROM orders WHERE user_id=? AND status='pending'", (uid,))["n"]
    return a + b


# ============================ MATNLAR ===================================
def status_line(r):
    s = STATUS.get(r["status"], r["status"])
    if r["status"] == "rejected" and r.get("reject_reason"):
        s += f"\n📝 Sabab: {esc(r['reject_reason'])}"
    return "Holat: " + s


def booking_text(b, client_view=False):
    t = [f"🪑 <b>Joy buyurtma №{b['id']}</b>"]
    if not client_view:
        t.append(f"👤 {user_link(b)}")
    t.append(f"📞 {esc(b['phone'])}")
    t.append(f"📅 {fmt_date(b['date'])} ({weekday(b['date'])})")
    hours = (b["end_min"] - b["start_min"]) // 60
    t.append(f"🕒 {hhmm(b['start_min'])}–{hhmm(b['end_min'])} ({hours} soat)")
    t.append(f"🚪 {b['room_number']}-xona · {esc(b['room_name'])}")
    if b["note"]:
        t.append(f"💬 {esc(b['note'])}")
    t.append("")
    t.append(status_line(b))
    if not client_view and b["handled_name"]:
        t.append(f"👨‍💼 Admin: {esc(b['handled_name'])}")
    return "\n".join(t)


def item_line(i, l):
    return (f"{i}. {esc(l['name'])} — {l['qty']} {l['unit']} × "
            f"{money(l['price'])} = {money(l['sum'])}")


def order_text(o, client_view=False):
    items = json.loads(o["items"])
    t = [f"🥡 <b>Olib ketish №{o['id']}</b>"]
    if not client_view:
        t.append(f"👤 {user_link(o)}")
    t.append(f"📞 {esc(o['phone'])}")
    t.append("")
    for i, l in enumerate(items, 1):
        t.append(item_line(i, l))
    t.append("")
    t.append(f"💰 <b>Jami: {money(o['total'])}</b>")
    if o["note"]:
        t.append(f"💬 {esc(o['note'])}")
    if o["ready_text"] and o["status"] == "confirmed":
        t.append(f"⏰ Tayyor bo'lish: {esc(o['ready_text'])}")
    t.append("")
    t.append(status_line(o))
    if not client_view and o["handled_name"]:
        t.append(f"👨‍💼 Admin: {esc(o['handled_name'])}")
    return "\n".join(t)


def receipt_header():
    return f"<b>━━━━━━━━━━━━━━━━━━\n🐟  {esc(BUSINESS_NAME)}\n━━━━━━━━━━━━━━━━━━</b>"


def receipt_footer():
    s = "\n━━━━━━━━━━━━━━━━━━\n🙏 <b>Tashakkur!</b> Yana kutib qolamiz."
    ph = get_setting("phone")
    if ph:
        s += f"\n☎️ {esc(ph)}"
    return s


def fmt_finished(r):
    try:
        return parse_ts(r["finished_at"]).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ts()


def order_receipt(o):
    items = json.loads(o["items"])
    t = [receipt_header(), f"🧾 Chek № {o['id']}", f"📅 {fmt_finished(o)}",
         f"👤 {esc(o['user_name'])}", f"📞 {esc(o['phone'])}", ""]
    for i, l in enumerate(items, 1):
        t.append(f"{i}. <b>{esc(l['name'])}</b>")
        t.append(f"    {l['qty']} {l['unit']} × {money(l['price'])} = {money(l['sum'])}")
    t.append("")
    t.append(f"📦 Buyurtmalar soni: {len(items)} ta")
    t.append(f"💰 <b>JAMI: {money(o['total'])}</b>")
    t.append("✅ <b>TO'LANGAN</b>")
    return "\n".join(t) + receipt_footer()


def booking_receipt(b):
    hours = (b["end_min"] - b["start_min"]) // 60
    t = [receipt_header(), f"🧾 Chek (joy) № {b['id']}", f"📅 {fmt_finished(b)}",
         f"👤 {esc(b['user_name'])}", f"📞 {esc(b['phone'])}", "",
         f"🚪 {b['room_number']}-xona · {esc(b['room_name'])}",
         f"📅 {fmt_date(b['date'])} ({weekday(b['date'])})",
         f"🕒 {hhmm(b['start_min'])}–{hhmm(b['end_min'])} ({hours} soat)", "",
         "✅ <b>Xizmat ko'rsatildi</b>"]
    return "\n".join(t) + receipt_footer()


# ======================= GURUHGA YUBORISH VA YANGILASH ==================
def group_ids():
    return [r["chat_id"] for r in many("SELECT chat_id FROM groups ORDER BY rowid")]


async def send_groups(bot: Bot, text, markup=None):
    """Barcha guruhlarga yuboradi. Hech biriga bormasa, adminlarga shaxsiy xabar."""
    res = {}
    for cid in group_ids():
        try:
            m = await bot.send_message(cid, text, reply_markup=markup)
            res[str(cid)] = m.message_id
        except Exception as e:
            log.warning("Guruhga yuborilmadi (%s): %s", cid, e)
    if not res:
        for a in many("SELECT user_id FROM admins"):
            try:
                m = await bot.send_message(a["user_id"], text, reply_markup=markup)
                res[str(a["user_id"])] = m.message_id
            except Exception as e:
                log.warning("Adminga yuborilmadi (%s): %s", a["user_id"], e)
    return res


async def edit_msgs(bot: Bot, msgs_json, text, markup):
    try:
        msgs = json.loads(msgs_json or "{}")
    except ValueError:
        msgs = {}
    for cid, mid in msgs.items():
        try:
            await bot.edit_message_text(text, chat_id=int(cid), message_id=mid,
                                        reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                log.warning("Tahrirlanmadi (%s): %s", cid, e)
        except Exception as e:
            log.warning("Tahrirlanmadi (%s): %s", cid, e)


def booking_kb(b):
    i = b["id"]
    if b["status"] == "pending":
        return kb([("✅ Tasdiqlash", f"bkg:ok:{i}"), ("❌ Rad etish", f"bkg:no:{i}")],
                  [("🔁 Boshqa xonaga biriktirish", f"bkg:sw:{i}")])
    if b["status"] == "confirmed":
        return kb([("🏁 Yakunlash", f"bkg:fin:{i}")],
                  [("🔁 Boshqa xona", f"bkg:sw:{i}"), ("❌ Rad etish", f"bkg:no:{i}")])
    return None


def order_kb(o):
    i = o["id"]
    if o["status"] == "pending":
        return kb([("✅ Tasdiqlash", f"ord:ok:{i}"), ("❌ Rad etish", f"ord:no:{i}")])
    if o["status"] == "confirmed":
        return kb([("🏁 Yakunlash", f"ord:fin:{i}")],
                  [("⏱ Tayyor vaqti", f"ord:rt:{i}"), ("❌ Rad etish", f"ord:no:{i}")])
    return None


async def refresh_booking(bot, bid):
    b = get_b(bid)
    if not b:
        return
    text = booking_receipt(b) if b["status"] == "done" else booking_text(b)
    await edit_msgs(bot, b["group_msgs"], text, booking_kb(b))


async def refresh_order(bot, oid):
    o = get_o(oid)
    if not o:
        return
    text = order_receipt(o) if o["status"] == "done" else order_text(o)
    await edit_msgs(bot, o["group_msgs"], text, order_kb(o))


async def dm_ask(c: CallbackQuery, bot: Bot, text, st, **data):
    """Admin guruhdagi tugmani bosganda, matn kiritish uchun shaxsiy chatga so'raydi."""
    uid = c.from_user.id
    try:
        await bot.send_message(uid, text, reply_markup=CANCEL)
    except Exception:
        await c.answer("Avval botga shaxsiy chatda /start bosing, so'ng qayta urining.",
                       show_alert=True)
        return False
    s = user_state(bot, uid)
    await s.set_state(st)
    await s.set_data(data)
    await c.answer("Shaxsiy chatingizga qarang 👆 — bot u yerda so'radi.", show_alert=True)
    return True


# ============================== /START ==================================
def home_kb(uid):
    rows = [
        [("🚪 Xonalar", "rm:list"), ("🍽 Taomnoma", "mn:list")],
        [("⚙️ Sozlamalar", "st:list"), ("👥 Adminlar", "ad:list")],
        [("📊 Bugungi hisobot", "rp:today")],
    ]
    if is_super(uid):
        rows.append([("📣 Guruhlar", "gr:list")])
    return kb(*rows)


@start_router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    u = m.from_user
    run("INSERT OR REPLACE INTO users(user_id,name,username) VALUES(?,?,?)",
        (u.id, u.full_name, u.username or ""))
    if is_admin(u.id):
        if await state.get_state():
            await m.answer("↩️ Oldingi amalingiz davom etmoqda. Yuqoridagi so'rovga "
                           "javob yozing yoki bekor qiling:", reply_markup=CANCEL)
            return
        await m.answer(f"👋 Xush kelibsiz, admin!\n<b>{esc(BUSINESS_NAME)}</b>",
                       reply_markup=CLIENT_KB)
        await m.answer("🛠 <b>Admin panel</b>", reply_markup=home_kb(u.id))
        return
    await state.clear()
    await m.answer(f"👋 Assalomu alaykum!\n<b>{esc(BUSINESS_NAME)}</b> botiga xush kelibsiz.\n"
                   "Kerakli bo'limni tanlang 👇", reply_markup=CLIENT_KB)


@start_router.message(F.text == BTN_CONTACT)
async def contact_admin(m: Message, state: FSMContext):
    await state.clear()
    ph = get_setting("phone")
    if ph:
        await m.answer(f"☎️ Administrator: <b>{esc(ph)}</b>")
    else:
        await m.answer("☎️ Administrator raqami hali kiritilmagan.")


@start_router.message(F.text == BTN_BOOK)
async def book_start(m: Message, state: FSMContext):
    await state.clear()
    await step_date(m, state)


@start_router.message(F.text == BTN_ORDER)
async def order_start(m: Message, state: FSMContext):
    await state.clear()
    await step_menu(m, state)


@start_router.message(F.text == BTN_MY)
async def my_orders(m: Message, state: FSMContext):
    await state.clear()
    await send_my(m, m.from_user.id)


# ============================== MIJOZ: UMUMIY ===========================
@client.callback_query(F.data == "cx")
async def cx(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, "❌ Bekor qilindi. Kerakli bo'limni menyudan tanlang.")


# ============================== MIJOZ: JOY BUYURTMA =====================
async def step_date(ev, state):
    if not many("SELECT id FROM rooms LIMIT 1"):
        await send(ev, "😔 Hozircha xonalar kiritilmagan. Administrator bilan bog'laning."
                   + admin_phone_line())
        return
    today = now().date()
    rows, row = [], []
    for i in range(7):
        d = today + timedelta(days=i)
        name = "Bugun" if i == 0 else ("Ertaga" if i == 1 else WD_SHORT[d.weekday()])
        row.append((f"{name} {d.strftime('%d.%m')}", f"bk:d:{d.isoformat()}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("❌ Bekor qilish", "cx")])
    await state.set_state(BK.date)
    await send(ev, "📅 <b>Sanani tanlang:</b>", kb(*rows))


async def step_time(ev, state):
    d = (await state.get_data()).get("date")
    slots = valid_slots(d)
    await state.set_state(BK.time)
    if not slots:
        await send(ev, f"😔 {fmt_date(d)} uchun bo'sh vaqt qolmadi. Boshqa sanani tanlang.",
                   kb([("⬅️ Orqaga", "bk:back:date")], [("❌ Bekor qilish", "cx")]))
        return
    rows, row = [], []
    for s in slots:
        row.append((hhmm(s), f"bk:t:{s}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([("⬅️ Orqaga", "bk:back:date"), ("❌ Bekor qilish", "cx")])
    ws, we = work_range()
    await send(ev, f"🕒 <b>Boshlanish vaqtini tanlang</b>\n📅 {fmt_date(d)} ({weekday(d)})\n"
                   f"Ish vaqti: {hhmm(ws)}–{hhmm(we)}", kb(*rows))


async def step_dur(ev, state):
    data = await state.get_data()
    s = data["start"]
    _, we = work_range()
    default = int(get_setting("duration") or 2)
    row = []
    for h in range(MIN_HOURS, MAX_HOURS + 1):
        if s + h * 60 <= we:
            row.append((f"{h} soat" + (" ⭐" if h == default else ""), f"bk:h:{h}"))
    await state.set_state(BK.dur)
    await send(ev, f"⏱ <b>Necha soatga band qilasiz?</b>\n🕒 Boshlanish: {hhmm(s)}\n"
                   f"⭐ — standart davomiylik",
               kb(row, [("⬅️ Orqaga", "bk:back:time"), ("❌ Bekor qilish", "cx")]))


async def step_room(ev, state):
    d = await state.get_data()
    date, s, h = d["date"], d["start"], d["hours"]
    e = s + h * 60
    rows, free = [], 0
    for r in many("SELECT * FROM rooms ORDER BY number"):
        busy = room_busy(r["id"], date, s, e)
        if not busy:
            free += 1
        label = f"{'🔴' if busy else '🟢'} {r['number']}-xona · {r['name']}"
        if busy:
            label += " (band)"
        rows.append([(label, f"bk:r:{r['id']}")])
    rows.append([("⬅️ Orqaga", "bk:back:dur"), ("❌ Bekor qilish", "cx")])
    await state.set_state(BK.room)
    extra = "" if free else "\n\n😔 Bu vaqtda barcha xonalar band. Boshqa vaqtni tanlang."
    await send(ev, f"🚪 <b>Xonani tanlang</b>\n📅 {fmt_date(date)} · 🕒 {hhmm(s)}–{hhmm(e)}\n"
                   f"🟢 bo'sh · 🔴 band{extra}", kb(*rows))


@client.callback_query(F.data.startswith("bk:back:"))
async def bk_back(c: CallbackQuery, state: FSMContext):
    await c.answer()
    where = c.data.split(":")[2]
    d = await state.get_data()
    if where == "time" and d.get("date"):
        await step_time(c, state)
    elif where == "dur" and d.get("date") and d.get("start") is not None:
        await step_dur(c, state)
    elif where == "room" and d.get("date") and d.get("start") is not None and d.get("hours"):
        await step_room(c, state)
    else:
        await step_date(c, state)


@client.callback_query(BK.date, F.data.startswith("bk:d:"))
async def bk_date(c: CallbackQuery, state: FSMContext):
    d = c.data[5:]
    try:
        dd = datetime.strptime(d, "%Y-%m-%d").date()
    except ValueError:
        return await c.answer("Sana noto'g'ri", show_alert=True)
    if dd < now().date():
        return await c.answer("Bu sana o'tib ketgan", show_alert=True)
    await c.answer()
    await state.update_data(date=d)
    await step_time(c, state)


@client.callback_query(BK.time, F.data.startswith("bk:t:"))
async def bk_time(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        s = int(c.data[5:])
    except ValueError:
        return await c.answer()
    if s not in valid_slots(data.get("date")):
        await c.answer("Bu vaqt endi mavjud emas", show_alert=True)
        return await step_time(c, state)
    await c.answer()
    await state.update_data(start=s)
    await step_dur(c, state)


@client.callback_query(BK.dur, F.data.startswith("bk:h:"))
async def bk_dur(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    try:
        h = int(c.data[5:])
    except ValueError:
        return await c.answer()
    _, we = work_range()
    if not (MIN_HOURS <= h <= MAX_HOURS) or data.get("start") is None \
            or data["start"] + h * 60 > we:
        return await c.answer("Bu davomiylik ish vaqtiga sig'maydi", show_alert=True)
    await c.answer()
    await state.update_data(hours=h)
    await step_room(c, state)


@client.callback_query(BK.room, F.data.startswith("bk:r:"))
async def bk_room(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if d.get("date") is None or d.get("start") is None or not d.get("hours"):
        await c.answer("Seans eskirgan, qaytadan boshlang", show_alert=True)
        return await step_date(c, state)
    try:
        rid = int(c.data[5:])
    except ValueError:
        return await c.answer()
    r = one("SELECT * FROM rooms WHERE id=?", (rid,))
    if not r:
        await c.answer("Xona topilmadi", show_alert=True)
        return await step_room(c, state)
    e = d["start"] + d["hours"] * 60
    if room_busy(rid, d["date"], d["start"], e):
        await c.answer("Kechirasiz, ushbu xona band", show_alert=True)
        return await step_room(c, state)
    await c.answer()
    await state.update_data(room_id=rid)
    await state.set_state(BK.note)
    await show(c, f"✅ Tanlandi: <b>{r['number']}-xona · {esc(r['name'])}</b>\n"
                  f"📅 {fmt_date(d['date'])} · 🕒 {hhmm(d['start'])}–{hhmm(e)}\n\n"
                  "💬 Xabar qoldirmoqchimisiz? (masalan: 8 kishi, tug'ilgan kun)\n"
                  "Yozing yoki «O'tkazib yuborish»ni bosing.",
               kb([("⏭ O'tkazib yuborish", "bk:skip")],
                  [("⬅️ Orqaga", "bk:back:room"), ("❌ Bekor qilish", "cx")]))


async def ask_phone(ev, state, st):
    await state.set_state(st)
    await send(ev, "📞 Aloqa uchun <b>telefon raqamingizni</b> kiriting.\n"
                   "Masalan: <code>+998901234567</code>", CX)


@client.callback_query(BK.note, F.data == "bk:skip")
async def bk_skip(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.update_data(note="")
    await ask_phone(c, state, BK.phone)


@client.message(BK.note, F.text)
async def bk_note(m: Message, state: FSMContext):
    await state.update_data(note=m.text.strip()[:300])
    await ask_phone(m, state, BK.phone)


@client.message(BK.phone, F.text)
async def bk_phone(m: Message, state: FSMContext, bot: Bot):
    phone = norm_phone(m.text)
    if not phone:
        return await m.answer("❗ Raqam noto'g'ri. Masalan: <code>+998901234567</code>",
                              reply_markup=CX)
    d = await state.get_data()
    u = m.from_user
    if d.get("date") is None or d.get("start") is None or not d.get("hours") \
            or not d.get("room_id"):
        await state.clear()
        return await m.answer("Seans eskirgan. Qaytadan boshlang.", reply_markup=CLIENT_KB)
    s, h = d["start"], d["hours"]
    e = s + h * 60
    if datetime.strptime(d["date"], "%Y-%m-%d") + timedelta(minutes=s) <= now():
        await m.answer("😔 Bu vaqt o'tib ketdi. Boshqa vaqtni tanlang.")
        return await step_date(m, state)
    r = one("SELECT * FROM rooms WHERE id=?", (d["room_id"],))
    if not r or room_busy(r["id"], d["date"], s, e):
        await m.answer("😔 Kechirasiz, bu xona band bo'lib qoldi. Boshqa xonani tanlang.")
        return await step_room(m, state)
    if pending_count(u.id) >= MAX_PENDING:
        await state.clear()
        return await m.answer(f"❗ Sizda javob kutilayotgan {MAX_PENDING} ta buyurtma bor. "
                              "Iltimos, admin javobini kuting.", reply_markup=CLIENT_KB)
    cur = run("""INSERT INTO bookings(user_id,user_name,phone,room_id,room_number,room_name,
                 date,start_min,end_min,note,status,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,'pending',?)""",
              (u.id, u.full_name, phone, r["id"], r["number"], r["name"],
               d["date"], s, e, d.get("note", ""), ts()))
    bid = cur.lastrowid
    await state.clear()
    b = get_b(bid)
    await m.answer("✅ <b>Buyurtmangiz qabul qilindi!</b>\n\n" + booking_text(b, True)
                   + "\n\n⏳ Iltimos, administrator javobini kuting — odatda 2 daqiqa ichida.\n"
                     f"Agar {ADMIN_REMIND_MIN} daqiqa ichida javob bo'lmasa, administrator bilan "
                     "bog'laning." + admin_phone_line()
                   + "\n\n<i>Xona administrator tasdiqlagandan keyin sizga biriktiriladi.</i>",
                   reply_markup=CLIENT_KB)
    msgs = await send_groups(bot, booking_text(b), booking_kb(b))
    run("UPDATE bookings SET group_msgs=? WHERE id=?", (json.dumps(msgs), bid))


# ============================== MIJOZ: OLIB KETISH ======================
def clean_cart(cart):
    out = []
    for it in cart or []:
        try:
            if one("SELECT id FROM menu WHERE id=?", (int(it["id"]),)):
                Decimal(it["qty"])
                out.append({"id": int(it["id"]), "qty": str(it["qty"])})
        except (KeyError, ValueError, TypeError, InvalidOperation):
            continue
    return out


def cart_lines(cart):
    lines = []
    for it in cart:
        m = one("SELECT * FROM menu WHERE id=?", (it["id"],))
        if not m:
            continue
        qty = Decimal(it["qty"])
        lines.append({"item_id": m["id"], "name": m["name"], "unit": m["unit"],
                      "price": m["price"], "qty": fmt_qty(qty),
                      "sum": calc_sum(m["price"], qty)})
    return lines


async def step_menu(ev, state):
    items = many("SELECT * FROM menu ORDER BY name")
    if not items:
        await send(ev, "😔 Hozircha taomnoma kiritilmagan. Administrator bilan bog'laning."
                   + admin_phone_line())
        return
    data = await state.get_data()
    cart = clean_cart(data.get("cart", []))
    await state.update_data(cart=cart, edit_idx=None)
    await state.set_state(OR.menu)
    rows = [[(f"🍽 {i['name']} · {money(i['price'])} / {i['unit']}", f"or:i:{i['id']}")]
            for i in items]
    if cart:
        total = sum(l["sum"] for l in cart_lines(cart))
        rows.append([(f"🛒 Savat ({len(cart)}) · {money(total)}", "or:cart")])
    rows.append([("❌ Bekor qilish", "cx")])
    await send(ev, "🥡 <b>Olib ketish</b>\nTaomni tanlang:", kb(*rows))


async def ask_qty(ev, state, m):
    await state.set_state(OR.qty)
    unit = m["unit"]
    quick = ["0.5", "1", "1.5", "2", "3", "5"] if unit == "kg" else ["1", "2", "3", "4", "5", "10"]
    rows = [[(f"{x} {unit}", f"or:q:{x}") for x in quick[:3]],
            [(f"{x} {unit}", f"or:q:{x}") for x in quick[3:]],
            [("⬅️ Menyu", "or:menu")]]
    hint = "kg da, masalan: 1.5" if unit == "kg" else f"{unit}, butun son"
    await send(ev, f"🍽 <b>{esc(m['name'])}</b>\n💰 Narxi: {money(m['price'])} / {unit}\n\n"
                   f"✍️ Miqdorni tanlang yoki yozing ({hint}):", kb(*rows))


async def step_cart(ev, state):
    data = await state.get_data()
    cart = clean_cart(data.get("cart", []))
    await state.update_data(cart=cart, edit_idx=None)
    if not cart:
        return await step_menu(ev, state)
    await state.set_state(OR.cart)
    lines = cart_lines(cart)
    total = sum(l["sum"] for l in lines)
    text = ("🛒 <b>Savat</b>\n\n" + "\n".join(item_line(i + 1, l) for i, l in enumerate(lines))
            + f"\n\n💰 <b>Jami: {money(total)}</b>")
    rows = [[(f"✏️ {i + 1}. {l['name']}", f"or:ce:{i}"), ("🗑", f"or:cd:{i}")]
            for i, l in enumerate(lines)]
    rows.append([("➕ Taom qo'shish", "or:menu"), ("🧹 Tozalash", "or:clr")])
    rows.append([("✅ Buyurtma berish", "or:go")])
    rows.append([("❌ Bekor qilish", "cx")])
    await send(ev, text, kb(*rows))


@client.callback_query(F.data == "or:menu")
async def or_menu(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await step_menu(c, state)


@client.callback_query(F.data == "or:cart")
async def or_cart(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await step_cart(c, state)


@client.callback_query(F.data.startswith("or:i:"))
async def or_item(c: CallbackQuery, state: FSMContext):
    try:
        mid = int(c.data[5:])
    except ValueError:
        return await c.answer()
    m = one("SELECT * FROM menu WHERE id=?", (mid,))
    if not m:
        await c.answer("Bu taom endi mavjud emas", show_alert=True)
        return await step_menu(c, state)
    await c.answer()
    await state.update_data(item_id=mid, edit_idx=None)
    await ask_qty(c, state, m)


async def apply_qty(ev, state, text):
    data = await state.get_data()
    m = one("SELECT * FROM menu WHERE id=?", (data.get("item_id"),))
    if not m:
        await send(ev, "Taom topilmadi. Qaytadan tanlang.")
        return await step_menu(ev, state)
    qty = parse_qty(text, m["unit"])
    if qty is None:
        msg = ("❗ Noto'g'ri miqdor. 0.01 dan 100 gacha son kiriting (masalan: 1.5)."
               if m["unit"] == "kg" else
               "❗ Noto'g'ri miqdor. 1 dan 100 gacha butun son kiriting.")
        await send(ev, msg, kb([("⬅️ Menyu", "or:menu")]))
        return
    await state.update_data(qty=fmt_qty(qty))
    await state.set_state(OR.confirm)
    total = calc_sum(m["price"], qty)
    await send(ev, f"🍽 <b>{esc(m['name'])}</b>\n{fmt_qty(qty)} {m['unit']} × "
                   f"{money(m['price'])} = <b>{money(total)}</b>",
               kb([("✅ Tasdiqlash", "or:add")],
                  [("✏️ Miqdorni o'zgartirish", "or:reqty"), ("⬅️ Menyu", "or:menu")]))


@client.callback_query(OR.qty, F.data.startswith("or:q:"))
async def or_quick(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await apply_qty(c, state, c.data[5:])


@client.message(OR.qty, F.text)
async def or_qty_text(m: Message, state: FSMContext):
    await apply_qty(m, state, m.text)


@client.callback_query(OR.confirm, F.data == "or:reqty")
async def or_reqty(c: CallbackQuery, state: FSMContext):
    await c.answer()
    data = await state.get_data()
    m = one("SELECT * FROM menu WHERE id=?", (data.get("item_id"),))
    if not m:
        return await step_menu(c, state)
    await ask_qty(c, state, m)


@client.callback_query(OR.confirm, F.data == "or:add")
async def or_add(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    mid, qty = data.get("item_id"), data.get("qty")
    if not mid or not qty or not one("SELECT id FROM menu WHERE id=?", (mid,)):
        await c.answer("Seans eskirgan", show_alert=True)
        return await step_menu(c, state)
    cart = clean_cart(data.get("cart", []))
    idx = data.get("edit_idx")
    if idx is not None and 0 <= idx < len(cart):
        cart[idx]["qty"] = qty
    else:
        found = next((x for x in cart if x["id"] == mid), None)
        if found:
            new = Decimal(found["qty"]) + Decimal(qty)
            if new > 100:
                return await c.answer("Bir taomning jami miqdori 100 dan oshmasin",
                                      show_alert=True)
            found["qty"] = fmt_qty(new)
        else:
            if len(cart) >= 15:
                return await c.answer("Savatda 15 tadan ortiq taom bo'lmasin", show_alert=True)
            cart.append({"id": mid, "qty": qty})
    await c.answer("Savatga qo'shildi ✅")
    await state.update_data(cart=cart, edit_idx=None)
    await step_cart(c, state)


@client.callback_query(F.data.startswith("or:ce:"))
async def or_cart_edit(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = clean_cart(data.get("cart", []))
    try:
        i = int(c.data[6:])
    except ValueError:
        return await c.answer()
    if not (0 <= i < len(cart)):
        await c.answer("Topilmadi", show_alert=True)
        return await step_cart(c, state)
    m = one("SELECT * FROM menu WHERE id=?", (cart[i]["id"],))
    await c.answer()
    await state.update_data(cart=cart, item_id=cart[i]["id"], edit_idx=i)
    await ask_qty(c, state, m)


@client.callback_query(F.data.startswith("or:cd:"))
async def or_cart_del(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = clean_cart(data.get("cart", []))
    try:
        i = int(c.data[6:])
    except ValueError:
        return await c.answer()
    if 0 <= i < len(cart):
        cart.pop(i)
    await c.answer("O'chirildi")
    await state.update_data(cart=cart)
    await step_cart(c, state)


@client.callback_query(F.data == "or:clr")
async def or_clear(c: CallbackQuery, state: FSMContext):
    await c.answer("Savat tozalandi")
    await state.update_data(cart=[])
    await step_menu(c, state)


@client.callback_query(F.data == "or:go")
async def or_go(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cart = clean_cart(data.get("cart", []))
    if not cart:
        await c.answer("Savat bo'sh", show_alert=True)
        return await step_menu(c, state)
    await c.answer()
    await state.update_data(cart=cart)
    await state.set_state(OR.note)
    await show(c, "💬 Buyurtmaga xabar qoldirmoqchimisiz? (ixtiyoriy)\n"
                  "Yozing yoki «O'tkazib yuborish»ni bosing.",
               kb([("⏭ O'tkazib yuborish", "or:skip")],
                  [("⬅️ Savat", "or:cart"), ("❌ Bekor qilish", "cx")]))


@client.callback_query(OR.note, F.data == "or:skip")
async def or_skip(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.update_data(note="")
    await ask_phone(c, state, OR.phone)


@client.message(OR.note, F.text)
async def or_note(m: Message, state: FSMContext):
    await state.update_data(note=m.text.strip()[:300])
    await ask_phone(m, state, OR.phone)


@client.message(OR.phone, F.text)
async def or_phone(m: Message, state: FSMContext, bot: Bot):
    phone = norm_phone(m.text)
    if not phone:
        return await m.answer("❗ Raqam noto'g'ri. Masalan: <code>+998901234567</code>",
                              reply_markup=CX)
    data = await state.get_data()
    u = m.from_user
    cart = clean_cart(data.get("cart", []))
    lines = cart_lines(cart)
    if not lines:
        await state.clear()
        return await m.answer("Savat bo'sh. Qaytadan boshlang.", reply_markup=CLIENT_KB)
    if pending_count(u.id) >= MAX_PENDING:
        await state.clear()
        return await m.answer(f"❗ Sizda javob kutilayotgan {MAX_PENDING} ta buyurtma bor. "
                              "Iltimos, admin javobini kuting.", reply_markup=CLIENT_KB)
    total = sum(l["sum"] for l in lines)
    cur = run("""INSERT INTO orders(user_id,user_name,phone,items,total,note,status,created_at)
                 VALUES(?,?,?,?,?,?,'pending',?)""",
              (u.id, u.full_name, phone, json.dumps(lines, ensure_ascii=False), total,
               data.get("note", ""), ts()))
    oid = cur.lastrowid
    await state.clear()
    o = get_o(oid)
    await m.answer("✅ <b>Buyurtmangiz qabul qilindi!</b>\n\n" + order_text(o, True)
                   + "\n\n⏳ Iltimos, administrator javobini kuting — odatda 2 daqiqa ichida.\n"
                     f"Agar {ADMIN_REMIND_MIN} daqiqa ichida javob bo'lmasa, administrator bilan "
                     "bog'laning." + admin_phone_line(), reply_markup=CLIENT_KB)
    msgs = await send_groups(bot, order_text(o), order_kb(o))
    run("UPDATE orders SET group_msgs=? WHERE id=?", (json.dumps(msgs), oid))


# Matn kutilayotgan holatda boshqa turdagi xabar kelsa
@client.message(StateFilter(BK.note, BK.phone, OR.qty, OR.note, OR.phone))
async def need_text(m: Message):
    await m.answer("Iltimos, matn ko'rinishida yuboring.")


# ============================== MIJOZ: BUYURTMALARIM ====================
async def send_my(ev, uid):
    bs = many("SELECT * FROM bookings WHERE user_id=? AND status IN ('pending','confirmed') "
              "ORDER BY id DESC LIMIT 10", (uid,))
    os_ = many("SELECT * FROM orders WHERE user_id=? AND status IN ('pending','confirmed') "
               "ORDER BY id DESC LIMIT 10", (uid,))
    if not bs and not os_:
        return await send(ev, "📋 Faol buyurtmalaringiz yo'q.")
    rows = []
    for b in bs:
        mark = "⏳" if b["status"] == "pending" else "✅"
        rows.append([(f"{mark} 🪑 №{b['id']} · {fmt_date(b['date'])} {hhmm(b['start_min'])}",
                      f"my:bv:{b['id']}")])
    for o in os_:
        mark = "⏳" if o["status"] == "pending" else "✅"
        rows.append([(f"{mark} 🥡 №{o['id']} · {money(o['total'])}", f"my:ov:{o['id']}")])
    await send(ev, "📋 <b>Faol buyurtmalaringiz</b>\nBatafsil ko'rish va bekor qilish uchun bosing:",
               kb(*rows))


@client.callback_query(F.data == "my:list")
async def my_list(c: CallbackQuery):
    await c.answer()
    await send_my(c, c.from_user.id)


@client.callback_query(F.data.startswith("my:bv:"))
async def my_bview(c: CallbackQuery):
    await c.answer()
    b = get_b(int(c.data[6:]))
    if not b or b["user_id"] != c.from_user.id:
        return await show(c, "Buyurtma topilmadi.", kb([("⬅️ Orqaga", "my:list")]))
    rows = []
    if b["status"] in ACTIVE:
        rows.append([("🚫 Bekor qilish", f"my:bc:{b['id']}")])
    rows.append([("⬅️ Orqaga", "my:list")])
    await show(c, booking_text(b, True), kb(*rows))


@client.callback_query(F.data.startswith("my:ov:"))
async def my_oview(c: CallbackQuery):
    await c.answer()
    o = get_o(int(c.data[6:]))
    if not o or o["user_id"] != c.from_user.id:
        return await show(c, "Buyurtma topilmadi.", kb([("⬅️ Orqaga", "my:list")]))
    rows = []
    if o["status"] in ACTIVE:
        rows.append([("🚫 Bekor qilish", f"my:oc:{o['id']}")])
    rows.append([("⬅️ Orqaga", "my:list")])
    await show(c, order_text(o, True), kb(*rows))


@client.callback_query(F.data.startswith("my:bc:"))
async def my_bcancel_ask(c: CallbackQuery):
    await c.answer()
    i = c.data[6:]
    await show(c, "❓ Buyurtmani bekor qilishni tasdiqlaysizmi?",
               kb([("✅ Ha, bekor qilish", f"my:bx:{i}"), ("❌ Yo'q", f"my:bv:{i}")]))


@client.callback_query(F.data.startswith("my:oc:"))
async def my_ocancel_ask(c: CallbackQuery):
    await c.answer()
    i = c.data[6:]
    await show(c, "❓ Buyurtmani bekor qilishni tasdiqlaysizmi?",
               kb([("✅ Ha, bekor qilish", f"my:ox:{i}"), ("❌ Yo'q", f"my:ov:{i}")]))


@client.callback_query(F.data.startswith("my:bx:"))
async def my_bcancel(c: CallbackQuery, bot: Bot):
    bid = int(c.data[6:])
    b = get_b(bid)
    if not b or b["user_id"] != c.from_user.id or b["status"] not in ACTIVE:
        await c.answer("Bu buyurtmani bekor qilib bo'lmaydi", show_alert=True)
        return await send_my(c, c.from_user.id)
    run("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    await c.answer("Bekor qilindi")
    await show(c, f"🚫 Buyurtma №{bid} bekor qilindi.")
    await refresh_booking(bot, bid)
    b = get_b(bid)
    await send_groups(bot, f"🚫 <b>Mijoz joy buyurtma №{bid} ni bekor qildi</b>\n"
                           f"👤 {user_link(b)} · 📞 {esc(b['phone'])}\n"
                           f"📅 {fmt_date(b['date'])} · 🕒 {hhmm(b['start_min'])}–{hhmm(b['end_min'])}\n"
                           f"🚪 {b['room_number']}-xona · {esc(b['room_name'])}")


@client.callback_query(F.data.startswith("my:ox:"))
async def my_ocancel(c: CallbackQuery, bot: Bot):
    oid = int(c.data[6:])
    o = get_o(oid)
    if not o or o["user_id"] != c.from_user.id or o["status"] not in ACTIVE:
        await c.answer("Bu buyurtmani bekor qilib bo'lmaydi", show_alert=True)
        return await send_my(c, c.from_user.id)
    run("UPDATE orders SET status='cancelled' WHERE id=?", (oid,))
    await c.answer("Bekor qilindi")
    await show(c, f"🚫 Buyurtma №{oid} bekor qilindi.")
    await refresh_order(bot, oid)
    o = get_o(oid)
    await send_groups(bot, f"🚫 <b>Mijoz olib ketish buyurtma №{oid} ni bekor qildi</b>\n"
                           f"👤 {user_link(o)} · 📞 {esc(o['phone'])}\n"
                           f"💰 {money(o['total'])}")


# ============================== ADMIN: HOME / CANCEL ====================
@admin.callback_query(F.data == "cancel")
@admin.callback_query(F.data == "adm:home")
async def adm_home(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, "🛠 <b>Admin panel</b>", home_kb(c.from_user.id))


# ============================== ADMIN: GURUHDA BOOKING ==================
@admin.callback_query(F.data.startswith("bkg:ok:"))
async def bkg_ok(c: CallbackQuery, bot: Bot):
    bid = int(c.data.split(":")[2])
    b = get_b(bid)
    if not b:
        return await c.answer("Buyurtma topilmadi", show_alert=True)
    if b["status"] != "pending":
        await c.answer(f"Bu buyurtma allaqachon: {STATUS.get(b['status'])}", show_alert=True)
        return await refresh_booking(bot, bid)
    if room_busy(b["room_id"], b["date"], b["start_min"], b["end_min"], exclude=bid):
        return await c.answer("Bu xona shu vaqtda boshqa buyurtmaga band. "
                              "«Boshqa xonaga biriktirish»ni bosing.", show_alert=True)
    do_confirm_booking(b, c.from_user)
    await c.answer("Tasdiqlandi ✅")
    await refresh_booking(bot, bid)
    await notify_booking_confirmed(bot, get_b(bid), changed=False)


def do_confirm_booking(b, admin_user, room=None):
    if room:
        run("UPDATE bookings SET room_id=?, room_number=?, room_name=? WHERE id=?",
            (room["id"], room["number"], room["name"], b["id"]))
    reminded = 1 if (start_dt(b) - now()) <= timedelta(minutes=BOOKING_REMIND_MIN) else 0
    run("UPDATE bookings SET status='confirmed', handled_by=?, handled_name=?, reminded=? "
        "WHERE id=?", (admin_user.id, admin_user.full_name, reminded, b["id"]))


async def notify_booking_confirmed(bot, b, changed):
    head = ("🔁 <b>Xonangiz o'zgartirildi!</b>" if changed
            else "✅ <b>Buyurtmangiz tasdiqlandi!</b>")
    await notify(bot, b["user_id"],
                 f"{head}\n\n"
                 f"🪑 Buyurtma №{b['id']}\n"
                 f"🚪 <b>{b['room_number']}-xona · {esc(b['room_name'])}</b>\n"
                 f"📅 {fmt_date(b['date'])} ({weekday(b['date'])})\n"
                 f"🕒 {hhmm(b['start_min'])}–{hhmm(b['end_min'])}\n\n"
                 f"Sizni kutamiz! 🐟{admin_phone_line()}")


@admin.callback_query(F.data.startswith("bkg:sw:"))
async def bkg_sw(c: CallbackQuery):
    bid = int(c.data.split(":")[2])
    b = get_b(bid)
    if not b or b["status"] not in ACTIVE:
        return await c.answer("Bu buyurtma faol emas", show_alert=True)
    rows = []
    for r in many("SELECT * FROM rooms ORDER BY number"):
        if r["id"] == b["room_id"]:
            continue
        if not room_busy(r["id"], b["date"], b["start_min"], b["end_min"], exclude=bid):
            rows.append([(f"🟢 {r['number']}-xona · {r['name']}", f"bkg:swr:{bid}:{r['id']}")])
    if not rows:
        return await c.answer("Bu vaqtda boshqa bo'sh xona yo'q", show_alert=True)
    rows.append([("⬅️ Orqaga", f"bkg:bk:{bid}")])
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=kb(*rows))
    except Exception as e:
        log.warning("edit_reply_markup: %s", e)


@admin.callback_query(F.data.startswith("bkg:bk:"))
async def bkg_back(c: CallbackQuery, bot: Bot):
    await c.answer()
    await refresh_booking(bot, int(c.data.split(":")[2]))


@admin.callback_query(F.data.startswith("bkg:swr:"))
async def bkg_swr(c: CallbackQuery, bot: Bot):
    _, _, bid, rid = c.data.split(":")
    bid, rid = int(bid), int(rid)
    b = get_b(bid)
    r = one("SELECT * FROM rooms WHERE id=?", (rid,))
    if not b or b["status"] not in ACTIVE:
        return await c.answer("Bu buyurtma faol emas", show_alert=True)
    if not r:
        return await c.answer("Xona topilmadi", show_alert=True)
    if room_busy(rid, b["date"], b["start_min"], b["end_min"], exclude=bid):
        return await c.answer("Bu xona shu vaqtda band", show_alert=True)
    was_pending = b["status"] == "pending"
    if was_pending:
        do_confirm_booking(b, c.from_user, room=r)
    else:
        run("UPDATE bookings SET room_id=?, room_number=?, room_name=? WHERE id=?",
            (r["id"], r["number"], r["name"], bid))
    await c.answer("Xona biriktirildi ✅")
    await refresh_booking(bot, bid)
    await notify_booking_confirmed(bot, get_b(bid), changed=True)


@admin.callback_query(F.data.startswith("bkg:no:"))
async def bkg_no(c: CallbackQuery, bot: Bot):
    bid = int(c.data.split(":")[2])
    b = get_b(bid)
    if not b or b["status"] not in ACTIVE:
        return await c.answer("Bu buyurtma faol emas", show_alert=True)
    await dm_ask(c, bot, f"❌ Joy buyurtma №{bid} ni rad etish sababini yozing "
                         "(mijozga yuboriladi):", Rej.reason, kind="b", rid=bid)


@admin.callback_query(F.data.startswith("bkg:fin:"))
async def bkg_fin(c: CallbackQuery):
    bid = int(c.data.split(":")[2])
    b = get_b(bid)
    if not b or b["status"] != "confirmed":
        return await c.answer("Faqat tasdiqlangan buyurtmani yakunlash mumkin", show_alert=True)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=kb(
            [("✅ Ha, yakunlash va chek yuborish", f"bkg:finok:{bid}")],
            [("⬅️ Orqaga", f"bkg:bk:{bid}")]))
    except Exception as e:
        log.warning("edit_reply_markup: %s", e)


@admin.callback_query(F.data.startswith("bkg:finok:"))
async def bkg_finok(c: CallbackQuery, bot: Bot):
    bid = int(c.data.split(":")[2])
    b = get_b(bid)
    if not b or b["status"] != "confirmed":
        await c.answer("Bu buyurtma allaqachon yakunlangan yoki faol emas", show_alert=True)
        return await refresh_booking(bot, bid)
    run("UPDATE bookings SET status='done', finished_at=?, handled_by=?, handled_name=? "
        "WHERE id=?", (ts(), c.from_user.id, c.from_user.full_name, bid))
    await c.answer("Yakunlandi 🏁")
    await refresh_booking(bot, bid)
    await notify(bot, b["user_id"], booking_receipt(get_b(bid)))


# ============================== ADMIN: GURUHDA ORDER ====================
READY_OPTS = {15: "15 daqiqadan keyin", 30: "30 daqiqadan keyin", 45: "45 daqiqadan keyin",
              60: "1 soatdan keyin", 90: "1.5 soatdan keyin"}


def ready_kb(oid):
    return kb([(f"{m} daq" if m < 60 else ("1 soat" if m == 60 else "1.5 soat"),
                f"ord:rd:{oid}:{m}") for m in (15, 30, 45)],
              [("1 soat", f"ord:rd:{oid}:60"), ("1.5 soat", f"ord:rd:{oid}:90")],
              [("✍️ Boshqa vaqt yozish", f"ord:rdx:{oid}")],
              [("⬅️ Orqaga", f"ord:bk:{oid}")])


async def apply_ready(bot, oid, ready_text, admin_id, admin_name):
    o = get_o(oid)
    if not o or o["status"] not in ACTIVE:
        return False
    was_pending = o["status"] == "pending"
    run("UPDATE orders SET status='confirmed', ready_text=?, handled_by=?, handled_name=? "
        "WHERE id=?", (ready_text[:100], admin_id, admin_name, oid))
    await refresh_order(bot, oid)
    o = get_o(oid)
    head = ("✅ <b>Buyurtmangiz tasdiqlandi!</b>" if was_pending
            else "⏱ <b>Tayyor bo'lish vaqti yangilandi!</b>")
    await notify(bot, o["user_id"],
                 f"{head}\n\n{order_text(o, True)}\n\n"
                 f"⏰ <b>Tayyor bo'lish: {esc(o['ready_text'])}</b>{admin_phone_line()}")
    return True


@admin.callback_query(F.data.startswith("ord:ok:"))
@admin.callback_query(F.data.startswith("ord:rt:"))
async def ord_ready_menu(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = get_o(oid)
    if not o or o["status"] not in ACTIVE:
        return await c.answer("Bu buyurtma faol emas", show_alert=True)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=ready_kb(oid))
    except Exception as e:
        log.warning("edit_reply_markup: %s", e)


@admin.callback_query(F.data.startswith("ord:bk:"))
async def ord_back(c: CallbackQuery, bot: Bot):
    await c.answer()
    await refresh_order(bot, int(c.data.split(":")[2]))


@admin.callback_query(F.data.startswith("ord:rd:"))
async def ord_ready(c: CallbackQuery, bot: Bot):
    _, _, oid, mins = c.data.split(":")
    oid, mins = int(oid), int(mins)
    if mins not in READY_OPTS:
        return await c.answer()
    clock = (now() + timedelta(minutes=mins)).strftime("%H:%M")
    ok = await apply_ready(bot, oid, f"{READY_OPTS[mins]} (≈{clock})",
                           c.from_user.id, c.from_user.full_name)
    if not ok:
        await c.answer("Bu buyurtma faol emas", show_alert=True)
        return await refresh_order(bot, oid)
    await c.answer("Saqlandi ✅")


@admin.callback_query(F.data.startswith("ord:rdx:"))
async def ord_ready_custom(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[2])
    o = get_o(oid)
    if not o or o["status"] not in ACTIVE:
        return await c.answer("Bu buyurtma faol emas", show_alert=True)
    await dm_ask(c, bot, f"⏰ Buyurtma №{oid} qachon tayyor bo'lishini yozing "
                         "(masalan: «19:30 da» yoki «40 daqiqadan keyin»):", Ready.text, rid=oid)


@admin.callback_query(F.data.startswith("ord:no:"))
async def ord_no(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[2])
    o = get_o(oid)
    if not o or o["status"] not in ACTIVE:
        return await c.answer("Bu buyurtma faol emas", show_alert=True)
    await dm_ask(c, bot, f"❌ Olib ketish №{oid} ni rad etish sababini yozing "
                         "(mijozga yuboriladi):", Rej.reason, kind="o", rid=oid)


@admin.callback_query(F.data.startswith("ord:fin:"))
async def ord_fin(c: CallbackQuery):
    oid = int(c.data.split(":")[2])
    o = get_o(oid)
    if not o or o["status"] != "confirmed":
        return await c.answer("Faqat tasdiqlangan buyurtmani yakunlash mumkin", show_alert=True)
    await c.answer()
    try:
        await c.message.edit_reply_markup(reply_markup=kb(
            [("✅ Ha, yakunlash va chek yuborish", f"ord:finok:{oid}")],
            [("⬅️ Orqaga", f"ord:bk:{oid}")]))
    except Exception as e:
        log.warning("edit_reply_markup: %s", e)


@admin.callback_query(F.data.startswith("ord:finok:"))
async def ord_finok(c: CallbackQuery, bot: Bot):
    oid = int(c.data.split(":")[2])
    o = get_o(oid)
    if not o or o["status"] != "confirmed":
        await c.answer("Bu buyurtma allaqachon yakunlangan yoki faol emas", show_alert=True)
        return await refresh_order(bot, oid)
    run("UPDATE orders SET status='done', finished_at=?, handled_by=?, handled_name=? "
        "WHERE id=?", (ts(), c.from_user.id, c.from_user.full_name, oid))
    await c.answer("Yakunlandi 🏁")
    await refresh_order(bot, oid)
    await notify(bot, o["user_id"], order_receipt(get_o(oid)))


# ---- sabab / vaqt matnini qabul qilish (shaxsiy chatda) ----
@admin.message(Rej.reason, F.text)
async def rej_reason(m: Message, state: FSMContext, bot: Bot):
    reason = m.text.strip()[:300]
    if not reason:
        return await m.answer("❗ Sabab yozing.", reply_markup=CANCEL)
    d = await state.get_data()
    await state.clear()
    kind, rid = d.get("kind"), d.get("rid")
    if kind == "b":
        r = get_b(rid)
        table = "bookings"
    else:
        r = get_o(rid)
        table = "orders"
    if not r or r["status"] not in ACTIVE:
        return await m.answer("ℹ️ Bu buyurtma allaqachon yopilgan.")
    run(f"UPDATE {table} SET status='rejected', reject_reason=?, handled_by=?, "
        "handled_name=? WHERE id=?", (reason, m.from_user.id, m.from_user.full_name, rid))
    if kind == "b":
        await refresh_booking(bot, rid)
        what = "Joy buyurtma"
    else:
        await refresh_order(bot, rid)
        what = "Olib ketish buyurtmasi"
    await notify(bot, r["user_id"], f"❌ <b>{what} №{rid} rad etildi.</b>\n"
                                    f"📝 Sabab: {esc(reason)}{admin_phone_line()}")
    await m.answer(f"✅ №{rid} rad etildi va mijozga yuborildi.")


@admin.message(Ready.text, F.text)
async def ready_text(m: Message, state: FSMContext, bot: Bot):
    txt = m.text.strip()
    if not txt:
        return await m.answer("❗ Vaqtni yozing.", reply_markup=CANCEL)
    d = await state.get_data()
    await state.clear()
    ok = await apply_ready(bot, d.get("rid"), txt, m.from_user.id, m.from_user.full_name)
    await m.answer("✅ Saqlandi va mijozga yuborildi." if ok else "ℹ️ Bu buyurtma faol emas.")


# ============================== ADMIN: HISOBOT ==========================
def report_text(d, partial=False):
    bs = {r["status"]: r["n"] for r in many(
        "SELECT status, COUNT(*) AS n FROM bookings WHERE date=? GROUP BY status", (d,))}
    os_ = {r["status"]: r["n"] for r in many(
        "SELECT status, COUNT(*) AS n FROM orders WHERE substr(created_at,1,10)=? "
        "GROUP BY status", (d,))}
    rev = one("SELECT COALESCE(SUM(total),0) AS s FROM orders WHERE status='done' "
              "AND substr(created_at,1,10)=?", (d,))["s"]

    def block(title, dct):
        out = [f"{title} jami: <b>{sum(dct.values())}</b>"]
        for k, label in (("done", "✅ Yakunlangan"), ("confirmed", "✔️ Tasdiqlangan (yakunlanmagan)"),
                         ("pending", "🕓 Kutilmoqda"), ("rejected", "❌ Rad etilgan"),
                         ("cancelled", "🚫 Bekor qilingan"), ("expired", "⌛ Muddati o'tgan")):
            if dct.get(k):
                out.append(f"   {label}: {dct[k]}")
        return out

    t = [f"📊 <b>Kunlik hisobot — {fmt_date(d)}</b>"]
    if partial:
        t.append("<i>(hozirgi holat)</i>")
    t.append("")
    t += block("🪑 Joy buyurtmalar", bs)
    t.append("")
    t += block("🥡 Olib ketish", os_)
    t.append("")
    t.append(f"💰 <b>Daromad: {money(rev)}</b>")
    return "\n".join(t)


@admin.callback_query(F.data == "rp:today")
async def rp_today(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    await show(c, report_text(now().date().isoformat(), partial=True),
               kb([("🔄 Yangilash", "rp:today")], [("⬅️ Orqaga", "adm:home")]))


# ============================== ADMIN: XONALAR ==========================
@admin.callback_query(F.data == "rm:list")
async def rm_list(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    rooms = many("SELECT * FROM rooms ORDER BY number")
    rows = [[(f"🚪 {r['number']}-xona · {r['name']}", f"rm:view:{r['id']}")] for r in rooms]
    rows.append([("➕ Xona qo'shish", "rm:add")])
    rows.append([("⬅️ Orqaga", "adm:home")])
    await show(c, "🚪 <b>Xonalar</b>" + ("" if rooms else "\n\nHozircha xona yo'q."), kb(*rows))


@admin.callback_query(F.data == "rm:add")
async def rm_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(RoomAdd.number)
    await show(c, "Xona <b>raqamini</b> kiriting (masalan: 1):", CANCEL)


@admin.message(RoomAdd.number, F.text)
async def rm_add_num(m: Message, state: FSMContext):
    t = m.text.strip()
    if not t.isdigit() or not (0 < int(t) < 100000):
        return await m.answer("❗ Faqat musbat son kiriting.", reply_markup=CANCEL)
    if one("SELECT 1 AS x FROM rooms WHERE number=?", (int(t),)):
        return await m.answer("❗ Bu raqamli xona allaqachon bor. Boshqa raqam kiriting.",
                              reply_markup=CANCEL)
    await state.update_data(number=int(t))
    await state.set_state(RoomAdd.name)
    await m.answer("Xona <b>nomini</b> kiriting (masalan: Oilaviy xona):", reply_markup=CANCEL)


@admin.message(RoomAdd.name, F.text)
async def rm_add_name(m: Message, state: FSMContext):
    name = m.text.strip()[:50]
    if not name:
        return await m.answer("❗ Nom kiriting.", reply_markup=CANCEL)
    d = await state.get_data()
    if one("SELECT 1 AS x FROM rooms WHERE number=?", (d["number"],)):
        await state.clear()
        return await m.answer("❗ Bu raqam band bo'lib qoldi. Qaytadan urining.",
                              reply_markup=kb([("🚪 Xonalar", "rm:list")]))
    run("INSERT INTO rooms(number,name) VALUES(?,?)", (d["number"], name))
    await state.clear()
    await m.answer(f"✅ {d['number']}-xona «{esc(name)}» qo'shildi.",
                   reply_markup=kb([("🚪 Xonalar", "rm:list")]))


@admin.callback_query(F.data.startswith("rm:view:"))
async def rm_view(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    rid = int(c.data.split(":")[2])
    r = one("SELECT * FROM rooms WHERE id=?", (rid,))
    if not r:
        return await show(c, "Xona topilmadi.", kb([("⬅️ Orqaga", "rm:list")]))
    await show(c, f"🚪 <b>{r['number']}-xona</b>\nNomi: {esc(r['name'])}",
               kb([("✏️ Raqam", f"rm:edit:{rid}:number"), ("✏️ Nom", f"rm:edit:{rid}:name")],
                  [("🗑 O'chirish", f"rm:del:{rid}")],
                  [("⬅️ Orqaga", "rm:list")]))


@admin.callback_query(F.data.startswith("rm:edit:"))
async def rm_edit(c: CallbackQuery, state: FSMContext):
    await c.answer()
    _, _, rid, field = c.data.split(":")
    await state.set_state(RoomEdit.value)
    await state.update_data(rid=int(rid), field=field)
    await show(c, "Yangi raqamni kiriting:" if field == "number" else "Yangi nomni kiriting:",
               CANCEL)


@admin.message(RoomEdit.value, F.text)
async def rm_edit_val(m: Message, state: FSMContext, bot: Bot):
    d = await state.get_data()
    t = m.text.strip()
    if d["field"] == "number":
        if not t.isdigit() or not (0 < int(t) < 100000):
            return await m.answer("❗ Faqat musbat son kiriting.", reply_markup=CANCEL)
        if one("SELECT 1 AS x FROM rooms WHERE number=? AND id!=?", (int(t), d["rid"])):
            return await m.answer("❗ Bu raqam band. Boshqasini kiriting.", reply_markup=CANCEL)
        run("UPDATE rooms SET number=? WHERE id=?", (int(t), d["rid"]))
    else:
        if not t:
            return await m.answer("❗ Nom kiriting.", reply_markup=CANCEL)
        run("UPDATE rooms SET name=? WHERE id=?", (t[:50], d["rid"]))
    await state.clear()
    r = one("SELECT * FROM rooms WHERE id=?", (d["rid"],))
    if r:
        affected = many("SELECT id FROM bookings WHERE room_id=? AND status IN "
                        "('pending','confirmed')", (r["id"],))
        run("UPDATE bookings SET room_number=?, room_name=? WHERE room_id=? AND status IN "
            "('pending','confirmed')", (r["number"], r["name"], r["id"]))
        for a in affected:
            await refresh_booking(bot, a["id"])
    await m.answer("✅ Saqlandi.", reply_markup=kb([("🚪 Xonalar", "rm:list")]))


@admin.callback_query(F.data.startswith("rm:del:"))
async def rm_del(c: CallbackQuery):
    rid = int(c.data.split(":")[2])
    n = one("SELECT COUNT(*) AS n FROM bookings WHERE room_id=? AND status IN "
            "('pending','confirmed')", (rid,))["n"]
    if n:
        return await c.answer(f"Bu xonaga {n} ta faol buyurtma biriktirilgan. "
                              "Avval ularni yakunlang yoki boshqa xonaga ko'chiring.",
                              show_alert=True)
    await c.answer()
    await show(c, "❓ Xonani o'chirishni tasdiqlaysizmi?",
               kb([("✅ Ha, o'chirish", f"rm:delok:{rid}"), ("❌ Yo'q", f"rm:view:{rid}")]))


@admin.callback_query(F.data.startswith("rm:delok:"))
async def rm_delok(c: CallbackQuery):
    rid = int(c.data.split(":")[2])
    n = one("SELECT COUNT(*) AS n FROM bookings WHERE room_id=? AND status IN "
            "('pending','confirmed')", (rid,))["n"]
    if n:
        return await c.answer("Bu xonaga faol buyurtmalar biriktirilgan", show_alert=True)
    await c.answer("O'chirildi")
    run("DELETE FROM rooms WHERE id=?", (rid,))
    await show(c, "🗑 Xona o'chirildi.", kb([("🚪 Xonalar", "rm:list")]))


# ============================== ADMIN: TAOMNOMA =========================
@admin.callback_query(F.data == "mn:list")
async def mn_list(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    items = many("SELECT * FROM menu ORDER BY name")
    rows = [[(f"🍽 {r['name']} · 1 {r['unit']} = {money(r['price'])}", f"mn:view:{r['id']}")]
            for r in items]
    rows.append([("➕ Taom qo'shish", "mn:add")])
    rows.append([("⬅️ Orqaga", "adm:home")])
    await show(c, "🍽 <b>Taomnoma</b>" + ("" if items else "\n\nHozircha taom yo'q."), kb(*rows))


@admin.callback_query(F.data == "mn:add")
async def mn_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(MenuAdd.name)
    await show(c, "Taom <b>nomini</b> kiriting (masalan: Baliq):", CANCEL)


@admin.message(MenuAdd.name, F.text)
async def mn_add_name(m: Message, state: FSMContext):
    t = m.text.strip()[:60]
    if not t:
        return await m.answer("❗ Nom kiriting.", reply_markup=CANCEL)
    await state.update_data(name=t)
    await state.set_state(MenuAdd.unit)
    await m.answer("O'lchov birligini tanlang:",
                   reply_markup=kb([(u, f"mnu:{u}") for u in UNITS], [("❌ Bekor qilish", "cancel")]))


@admin.callback_query(MenuAdd.unit, F.data.startswith("mnu:"))
async def mn_add_unit(c: CallbackQuery, state: FSMContext):
    unit = c.data.split(":")[1]
    if unit not in UNITS:
        return await c.answer()
    await c.answer()
    await state.update_data(unit=unit)
    await state.set_state(MenuAdd.price)
    await show(c, f"1 {unit} narxini so'mda kiriting (masalan: 50000):", CANCEL)


@admin.message(MenuAdd.price, F.text)
async def mn_add_price(m: Message, state: FSMContext):
    t = re.sub(r"[\s,]", "", m.text)
    if not t.isdigit() or not (0 < int(t) < 10**10):
        return await m.answer("❗ Narxni faqat raqamda kiriting.", reply_markup=CANCEL)
    d = await state.get_data()
    run("INSERT INTO menu(name,unit,price) VALUES(?,?,?)", (d["name"], d["unit"], int(t)))
    await state.clear()
    await m.answer(f"✅ Qo'shildi: {esc(d['name'])} — 1 {d['unit']} = {money(int(t))}",
                   reply_markup=kb([("🍽 Taomnoma", "mn:list")]))


@admin.callback_query(F.data.startswith("mn:view:"))
async def mn_view(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    mid = int(c.data.split(":")[2])
    r = one("SELECT * FROM menu WHERE id=?", (mid,))
    if not r:
        return await show(c, "Taom topilmadi.", kb([("⬅️ Orqaga", "mn:list")]))
    await show(c, f"🍽 <b>{esc(r['name'])}</b>\nBirlik: {r['unit']}\nNarx: {money(r['price'])}",
               kb([("✏️ Nom", f"mn:edit:{mid}:name"), ("✏️ Narx", f"mn:edit:{mid}:price")],
                  [("✏️ Birlik", f"mn:edit:{mid}:unit")],
                  [("🗑 O'chirish", f"mn:del:{mid}")],
                  [("⬅️ Orqaga", "mn:list")]))


@admin.callback_query(F.data.startswith("mn:edit:"))
async def mn_edit(c: CallbackQuery, state: FSMContext):
    await c.answer()
    _, _, mid, field = c.data.split(":")
    if field == "unit":
        return await show(c, "Yangi o'lchov birligini tanlang:",
                          kb([(u, f"mn:setunit:{mid}:{u}") for u in UNITS],
                             [("⬅️ Orqaga", f"mn:view:{mid}")]))
    await state.set_state(MenuEdit.value)
    await state.update_data(mid=int(mid), field=field)
    await show(c, "Yangi nomni kiriting:" if field == "name" else "Yangi narxni kiriting (so'm):",
               CANCEL)


@admin.callback_query(F.data.startswith("mn:setunit:"))
async def mn_setunit(c: CallbackQuery):
    _, _, mid, unit = c.data.split(":")
    if unit not in UNITS:
        return await c.answer()
    await c.answer("Saqlandi")
    run("UPDATE menu SET unit=? WHERE id=?", (unit, int(mid)))
    await show(c, "✅ Saqlandi.", kb([("🍽 Taomnoma", "mn:list")]))


@admin.message(MenuEdit.value, F.text)
async def mn_edit_val(m: Message, state: FSMContext):
    d = await state.get_data()
    t = m.text.strip()
    if d["field"] == "name":
        if not t:
            return await m.answer("❗ Nom kiriting.", reply_markup=CANCEL)
        run("UPDATE menu SET name=? WHERE id=?", (t[:60], d["mid"]))
    else:
        t = re.sub(r"[\s,]", "", t)
        if not t.isdigit() or not (0 < int(t) < 10**10):
            return await m.answer("❗ Narxni faqat raqamda kiriting.", reply_markup=CANCEL)
        run("UPDATE menu SET price=? WHERE id=?", (int(t), d["mid"]))
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=kb([("🍽 Taomnoma", "mn:list")]))


@admin.callback_query(F.data.startswith("mn:del:"))
async def mn_del(c: CallbackQuery):
    await c.answer()
    mid = c.data.split(":")[2]
    await show(c, "❓ Taomni o'chirishni tasdiqlaysizmi?",
               kb([("✅ Ha, o'chirish", f"mn:delok:{mid}"), ("❌ Yo'q", f"mn:view:{mid}")]))


@admin.callback_query(F.data.startswith("mn:delok:"))
async def mn_delok(c: CallbackQuery):
    await c.answer("O'chirildi")
    run("DELETE FROM menu WHERE id=?", (int(c.data.split(":")[2]),))
    await show(c, "🗑 Taom o'chirildi.", kb([("🍽 Taomnoma", "mn:list")]))


# ============================== ADMIN: SOZLAMALAR =======================
SETTING_LABELS = {
    "work_start": "🕘 Ish boshlanishi",
    "work_end": "🕙 Ish tugashi",
    "duration": "⏱ Standart band davomiyligi",
    "phone": "☎️ Administrator telefoni",
}


@admin.callback_query(F.data == "st:list")
async def st_list(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    rows = []
    for k, label in SETTING_LABELS.items():
        v = get_setting(k) or "—"
        if k == "duration" and v != "—":
            v += " soat"
        rows.append([(f"{label}: {v}", f"st:edit:{k}")])
    rows.append([("⬅️ Orqaga", "adm:home")])
    await show(c, "⚙️ <b>Sozlamalar</b>\nO'zgartirish uchun bosing:", kb(*rows))


@admin.callback_query(F.data.startswith("st:edit:"))
async def st_edit(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 2)[2]
    if key not in SETTING_LABELS:
        return await c.answer()
    await c.answer()
    await state.set_state(SettingEdit.value)
    await state.update_data(key=key)
    hint = {"work_start": "Masalan: 10:00", "work_end": "Masalan: 23:00",
            "duration": "1 dan 4 gacha son", "phone": "Masalan: +998901234567"}[key]
    await show(c, f"{SETTING_LABELS[key]}\nYangi qiymatni kiriting.\n<i>{hint}</i>", CANCEL)


@admin.message(SettingEdit.value, F.text)
async def st_edit_val(m: Message, state: FSMContext):
    key = (await state.get_data()).get("key")
    t = m.text.strip()
    if key in ("work_start", "work_end"):
        v = norm_time(t)
        if not v:
            return await m.answer("❗ Vaqtni HH:MM ko'rinishida kiriting.", reply_markup=CANCEL)
        ws = to_min(v) if key == "work_start" else to_min(get_setting("work_start"))
        we = to_min(v) if key == "work_end" else to_min(get_setting("work_end"))
        if ws + MIN_HOURS * 60 > we:
            return await m.answer("❗ Ish boshlanishi tugashidan kamida 1 soat oldin bo'lishi "
                                  "kerak (bir kun ichida).", reply_markup=CANCEL)
    elif key == "duration":
        if not t.isdigit() or not (MIN_HOURS <= int(t) <= MAX_HOURS):
            return await m.answer("❗ 1 dan 4 gacha son kiriting.", reply_markup=CANCEL)
        v = t
    elif key == "phone":
        v = norm_phone(t)
        if not v:
            return await m.answer("❗ Raqam noto'g'ri. Masalan: +998901234567",
                                  reply_markup=CANCEL)
    else:
        await state.clear()
        return
    set_setting(key, v)
    await state.clear()
    await m.answer("✅ Saqlandi.", reply_markup=kb([("⚙️ Sozlamalar", "st:list")]))


# ============================== ADMIN: ADMINLAR =========================
@admin.callback_query(F.data == "ad:list")
async def ad_list(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    rows = []
    for a in many("SELECT * FROM admins ORDER BY rowid"):
        mark = "⭐ " if a["user_id"] == SUPER_ADMIN_ID else "👤 "
        rows.append([(f"{mark}{a['name'] or '—'} · {a['user_id']}", f"ad:view:{a['user_id']}")])
    rows.append([("➕ Admin qo'shish", "ad:add")])
    rows.append([("⬅️ Orqaga", "adm:home")])
    await show(c, "👥 <b>Adminlar</b>", kb(*rows))


@admin.callback_query(F.data == "ad:add")
async def ad_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(AdminAdd.uid)
    await show(c, "Yangi adminning <b>user ID</b> sini kiriting.\n"
                  "<i>ID ni @userinfobot orqali bilish mumkin. Yangi admin botga /start "
                  "bosgan bo'lishi kerak.</i>", CANCEL)


@admin.message(AdminAdd.uid, F.text)
async def ad_add_id(m: Message, state: FSMContext, bot: Bot):
    t = m.text.strip()
    if not t.isdigit() or int(t) <= 0:
        return await m.answer("❗ Faqat raqamli ID kiriting.", reply_markup=CANCEL)
    uid = int(t)
    if is_admin(uid):
        return await m.answer("❗ Bu foydalanuvchi allaqachon admin.", reply_markup=CANCEL)
    name, warn = "—", ""
    try:
        ch = await bot.get_chat(uid)
        name = ch.full_name or "—"
    except Exception:
        warn = "\n⚠️ Foydalanuvchi hali botga /start bosmagan, ismi aniqlanmadi."
    run("INSERT INTO admins(user_id,name,added_by) VALUES(?,?,?)", (uid, name, m.from_user.id))
    await state.clear()
    await notify(bot, uid, "✅ Siz admin qilib tayinlandingiz. /start bosing.")
    await m.answer(f"✅ Admin qo'shildi: {esc(name)} ({uid}){warn}",
                   reply_markup=kb([("👥 Adminlar", "ad:list")]))


@admin.callback_query(F.data.startswith("ad:view:"))
async def ad_view(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    uid = int(c.data.split(":")[2])
    a = one("SELECT * FROM admins WHERE user_id=?", (uid,))
    if not a:
        return await show(c, "Admin topilmadi.", kb([("⬅️ Orqaga", "ad:list")]))
    rows = []
    if uid != SUPER_ADMIN_ID and uid != c.from_user.id:
        rows.append([("🗑 O'chirish", f"ad:del:{uid}")])
    rows.append([("⬅️ Orqaga", "ad:list")])
    await show(c, f"👤 <b>{esc(a['name'] or '—')}</b>\nID: <code>{uid}</code>", kb(*rows))


@admin.callback_query(F.data.startswith("ad:del:"))
async def ad_del(c: CallbackQuery):
    uid = int(c.data.split(":")[2])
    if uid == SUPER_ADMIN_ID or uid == c.from_user.id:
        return await c.answer("Bu adminni o'chirib bo'lmaydi", show_alert=True)
    await c.answer()
    await show(c, "❓ Adminni o'chirishni tasdiqlaysizmi?",
               kb([("✅ Ha, o'chirish", f"ad:delok:{uid}"), ("❌ Yo'q", f"ad:view:{uid}")]))


@admin.callback_query(F.data.startswith("ad:delok:"))
async def ad_delok(c: CallbackQuery, bot: Bot):
    uid = int(c.data.split(":")[2])
    if uid == SUPER_ADMIN_ID or uid == c.from_user.id:
        return await c.answer("Bu adminni o'chirib bo'lmaydi", show_alert=True)
    await c.answer("O'chirildi")
    run("DELETE FROM admins WHERE user_id=?", (uid,))
    await user_state(bot, uid).clear()
    await notify(bot, uid, "ℹ️ Sizning adminlik huquqingiz bekor qilindi.")
    await show(c, "🗑 Admin o'chirildi.", kb([("👥 Adminlar", "ad:list")]))


# Admin holatida matn kutilganda boshqa turdagi xabar kelsa
@admin.message(~StateFilter(None))
async def admin_need_text(m: Message):
    await m.answer("Iltimos, matn ko'rinishida yuboring yoki bekor qiling.", reply_markup=CANCEL)


# ============================== SUPER ADMIN: GURUHLAR ===================
@sup.callback_query(F.data == "gr:list")
async def gr_list(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.clear()
    rows = []
    for g in many("SELECT * FROM groups ORDER BY rowid"):
        if g["chat_id"] == MAIN_GROUP_ID:
            rows.append([(f"🔒 {g['title']} · {g['chat_id']}", "gr:main")])
        else:
            rows.append([(f"📣 {g['title']} · {g['chat_id']}", f"gr:del:{g['chat_id']}")])
    rows.append([("➕ Guruh qo'shish", "gr:add")])
    rows.append([("⬅️ Orqaga", "adm:home")])
    await show(c, "📣 <b>Guruhlar</b>\nBuyurtmalar barcha guruhlarga yuboriladi.\n"
                  "Guruhni o'chirish uchun uni bosing.", kb(*rows))


@sup.callback_query(F.data == "gr:main")
async def gr_main(c: CallbackQuery):
    await c.answer("Asosiy guruhni o'chirib bo'lmaydi", show_alert=True)


@sup.callback_query(F.data == "gr:add")
async def gr_add(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await state.set_state(GroupAdd.chat_id)
    await show(c, "Guruh <b>ID</b> sini kiriting (masalan: <code>-1001234567890</code>).\n"
                  "<i>Avval botni guruhga qo'shib, admin qiling.</i>", CANCEL)


@sup.message(GroupAdd.chat_id, F.text)
async def gr_add_id(m: Message, state: FSMContext, bot: Bot):
    t = m.text.strip()
    if not re.fullmatch(r"-\d{5,}", t):
        return await m.answer("❗ Guruh ID si minus belgisi bilan boshlanadi. "
                              "Masalan: -1001234567890", reply_markup=CANCEL)
    cid = int(t)
    if one("SELECT 1 AS x FROM groups WHERE chat_id=?", (cid,)):
        return await m.answer("❗ Bu guruh allaqachon ulangan.", reply_markup=CANCEL)
    try:
        chat = await bot.get_chat(cid)
        await bot.send_message(cid, f"✅ <b>{esc(BUSINESS_NAME)}</b> boti ushbu guruhga ulandi.")
    except Exception:
        return await m.answer("❗ Guruhga ulanib bo'lmadi. Bot guruhda ekanini (admin bo'lishi "
                              "tavsiya etiladi) va ID to'g'riligini tekshiring, so'ng qayta "
                              "kiriting.", reply_markup=CANCEL)
    run("INSERT INTO groups(chat_id,title) VALUES(?,?)", (cid, chat.title or str(cid)))
    await state.clear()
    await m.answer(f"✅ Guruh qo'shildi: {esc(chat.title or cid)}",
                   reply_markup=kb([("📣 Guruhlar", "gr:list")]))


@sup.callback_query(F.data.startswith("gr:del:"))
async def gr_del(c: CallbackQuery):
    await c.answer()
    cid = c.data.split(":", 2)[2]
    await show(c, "❓ Guruhni ro'yxatdan o'chirishni tasdiqlaysizmi?",
               kb([("✅ Ha", f"gr:delok:{cid}"), ("❌ Yo'q", "gr:list")]))


@sup.callback_query(F.data.startswith("gr:delok:"))
async def gr_delok(c: CallbackQuery):
    cid = int(c.data.split(":", 2)[2])
    if cid == MAIN_GROUP_ID:
        return await c.answer("Asosiy guruhni o'chirib bo'lmaydi", show_alert=True)
    await c.answer("O'chirildi")
    run("DELETE FROM groups WHERE chat_id=?", (cid,))
    await show(c, "🗑 Guruh o'chirildi.", kb([("📣 Guruhlar", "gr:list")]))


# ============================== QOLGAN HAMMASI (fallback) ===============
@fb.callback_query()
async def fb_cb(c: CallbackQuery):
    if is_admin(c.from_user.id):
        await c.answer("Bu tugma eskirgan. Qaytadan boshlang.", show_alert=True)
    else:
        await c.answer("Bu tugma eskirgan yoki sizda ruxsat yo'q.", show_alert=True)


@fb.message(PRIVATE)
async def fb_msg(m: Message):
    await m.answer("Kerakli bo'limni menyudan tanlang 👇", reply_markup=CLIENT_KB)


# ============================== FON VAZIFALAR ===========================
def backup_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"otov_{now().strftime('%Y-%m-%d_%H%M')}.db")
    dst = sqlite3.connect(path)
    try:
        db.backup(dst)
    finally:
        dst.close()
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "otov_*.db")))
    for f in files[:-14]:
        try:
            os.remove(f)
        except OSError:
            pass
    return path


async def job_expire(bot):
    n = now()
    for b in many("SELECT * FROM bookings WHERE status='pending'"):
        if start_dt(b) <= n:
            run("UPDATE bookings SET status='expired' WHERE id=? AND status='pending'", (b["id"],))
            await refresh_booking(bot, b["id"])
            await notify(bot, b["user_id"],
                         f"⌛ Joy buyurtma №{b['id']} ko'rib chiqilmadi va muddati o'tdi. "
                         f"Iltimos, qaytadan buyurtma bering.{admin_phone_line()}")


async def job_admin_remind(bot):
    limit = now() - timedelta(minutes=ADMIN_REMIND_MIN)
    ph = get_setting("phone")
    contact = f" Administrator: {esc(ph)}" if ph else ""
    for kind, table, label in (("b", "bookings", "Joy buyurtma"), ("o", "orders", "Olib ketish")):
        for r in many(f"SELECT * FROM {table} WHERE status='pending' AND admin_reminded=0"):
            try:
                if parse_ts(r["created_at"]) > limit:
                    continue
            except Exception:
                continue
            run(f"UPDATE {table} SET admin_reminded=1 WHERE id=?", (r["id"],))
            await send_groups(bot, f"⏰ <b>Diqqat!</b> {label} №{r['id']} "
                                   f"{ADMIN_REMIND_MIN} daqiqadan beri javob kutmoqda.\n"
                                   f"👤 {user_link(r)} · 📞 {esc(r['phone'])}")
            await notify(bot, r["user_id"],
                         f"⏳ {label} №{r['id']} hali ko'rib chiqilmadi. "
                         f"Iltimos, administrator bilan bog'laning.{contact}")


async def job_booking_remind(bot):
    n = now()
    for b in many("SELECT * FROM bookings WHERE status='confirmed' AND reminded=0"):
        diff = start_dt(b) - n
        if diff <= timedelta(minutes=BOOKING_REMIND_MIN):
            run("UPDATE bookings SET reminded=1 WHERE id=?", (b["id"],))
            if diff > timedelta(0):
                await notify(bot, b["user_id"],
                             f"⏰ <b>Eslatma!</b>\nBugun soat <b>{hhmm(b['start_min'])}</b> da "
                             f"<b>{b['room_number']}-xona · {esc(b['room_name'])}</b> sizni kutadi.\n"
                             f"Sizni kutamiz! 🐟{admin_phone_line()}")


async def job_daily(bot):
    today = now().date()
    target = today - timedelta(days=1)
    last = get_setting("last_report")
    if not last:
        set_setting("last_report", target.isoformat())
        return
    last_d = date_cls.fromisoformat(last)
    if last_d >= target:
        return
    d = max(last_d + timedelta(days=1), target - timedelta(days=6))
    while d <= target:
        await send_groups(bot, report_text(d.isoformat()))
        set_setting("last_report", d.isoformat())
        d += timedelta(days=1)
    try:
        path = backup_db()
        await bot.send_document(SUPER_ADMIN_ID, FSInputFile(path),
                                caption=f"🗄 Kunlik zaxira — {target.strftime('%d.%m.%Y')}")
    except Exception as e:
        log.warning("Zaxira yuborilmadi: %s", e)


async def scheduler(bot: Bot):
    while True:
        for job in (job_expire, job_admin_remind, job_booking_remind, job_daily):
            try:
                await job(bot)
            except Exception:
                log.exception("Fon vazifa xatosi: %s", job.__name__)
        await asyncio.sleep(20)


# ============================== ISHGA TUSHIRISH =========================
async def on_error(event: ErrorEvent):
    log.error("Xatolik: %s", event.exception, exc_info=event.exception)
    cq = getattr(event.update, "callback_query", None)
    if cq:
        try:
            await cq.answer("Xatolik yuz berdi. Qaytadan urinib ko'ring.", show_alert=True)
        except Exception:
            pass
    return True


async def main():
    init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=STORAGE)
    dp.errors.register(on_error)
    dp.include_router(start_router)
    dp.include_router(client)
    dp.include_router(sup)
    dp.include_router(admin)
    dp.include_router(fb)
    await bot.delete_webhook(drop_pending_updates=False)
    me = await bot.get_me()
    log.info("Bot ishga tushdi: @%s", me.username)
    task = asyncio.create_task(scheduler(bot))
    try:
        await dp.start_polling(bot)
    finally:
        task.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
