import asyncio
import hashlib
import html
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton,
    InputMediaPhoto, BufferedInputFile,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# ============ НАСТРОЙКИ ============
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
LOG_PATH = os.getenv("LOG_PATH", "/data/bot.log")

PRICE_7_DAYS = int(os.getenv("PRICE_7_DAYS", "200"))
PRICE_30_DAYS = int(os.getenv("PRICE_30_DAYS", "500"))

# --- PLATI.MARKET ---
PLATI_AGENT_ID = os.getenv("PLATI_AGENT_ID", "1480092")
PLATI_SUPPORT = os.getenv("PLATI_SUPPORT", "@ObuzaMeduza")
STEAM_TOPUP_ITEM_ID = os.getenv("STEAM_TOPUP_ITEM_ID", "3961593")

# --- ROBOKASSA ---
ROBO_LOGIN = os.getenv("ROBO_LOGIN", "")
ROBO_PASS1 = os.getenv("ROBO_PASS1", "")
ROBO_PASS2 = os.getenv("ROBO_PASS2", "")
ROBO_IS_TEST = int(os.getenv("ROBO_IS_TEST", "0"))

# --- ЮРИДИЧЕСКАЯ ИНФОРМАЦИЯ ---
WEBSITE_URL = "https://mend-good-violet.tilda.ws/"
OFFER_URL = "https://disk.yandex.ru/d/o9me2ZalTIV3Ig"
PRIVACY_URL = "https://disk.yandex.ru/d/dLYJbTLifD15fA"
CONTACT_TG = "@ObuzaMeduza"
CONTACT_EMAIL = "nikhrushawork@gmail.com"
INN = "360903175784"
# ===================================

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в .env")

os.makedirs(os.path.dirname(LOG_PATH) or ".", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=2, encoding="utf-8"),
    ],
)
logger = logging.getLogger("bot")

if not ADMIN_IDS:
    logger.warning("⚠️ ADMIN_IDS пуст — админ-панель недоступна!")

router = Router()
PAGE_LIMIT = 50

WISHLIST_LIMIT_FREE = 1
WISHLIST_LIMIT_PREMIUM = 10

# ---------- КЭШ КАРТИНОК (УМЕНЬШЕН) ----------
IMAGE_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
IMAGE_CACHE_MAX_BYTES = 10 * 1024 * 1024  # 10 МБ
_image_cache_size = 0


# ---------- УТИЛИТЫ ВРЕМЕНИ ----------
def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    return utcnow().isoformat()


def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---------- БАЗА ----------
_db_lock = threading.Lock()
_db_conn: sqlite3.Connection | None = None


def db_init():
    global _db_conn
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    _db_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute("PRAGMA synchronous=NORMAL")
    cur = _db_conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL UNIQUE,
        price TEXT NOT NULL,
        old_price TEXT,
        discount TEXT,
        url TEXT NOT NULL,
        image TEXT,
        description TEXT,
        price_usd TEXT,
        clicks INTEGER DEFAULT 0,
        tg_file_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT,
        first_name TEXT,
        joined_at TEXT DEFAULT CURRENT_TIMESTAMP,
        premium_until TEXT,
        last_roulette TEXT,
        last_random_again TEXT
    );
    CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_id TEXT UNIQUE,
        user_id INTEGER,
        amount INTEGER,
        days INTEGER,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS notified_deals (
        product_id INTEGER PRIMARY KEY,
        notified_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS wishlist_titles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT COLLATE NOCASE,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, title)
    );
    CREATE TABLE IF NOT EXISTS link_clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        link_type TEXT,
        clicked_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS notified_wishlist (
        user_id INTEGER,
        product_id INTEGER,
        notified_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, product_id)
    );
    CREATE INDEX IF NOT EXISTS idx_products_discount ON products(discount);
    CREATE INDEX IF NOT EXISTS idx_users_premium ON users(premium_until);
    CREATE INDEX IF NOT EXISTS idx_payments_status ON payments(status);
    """)
    # Миграции для старых БД
    cur.execute("PRAGMA table_info(products)")
    cols = {row[1] for row in cur.fetchall()}
    if "tg_file_id" not in cols:
        try:
            cur.execute("ALTER TABLE products ADD COLUMN tg_file_id TEXT")
        except sqlite3.OperationalError:
            pass
    _db_conn.commit()


def db_query(sql, params=(), fetch=False, fetchone=False):
    with _db_lock:
        cur = _db_conn.cursor()
        cur.execute(sql, params)
        if fetch:
            result = cur.fetchall()
        elif fetchone:
            result = cur.fetchone()
        else:
            result = None
        rowcount = cur.rowcount
        lastrowid = cur.lastrowid
        _db_conn.commit()
        if not fetch and not fetchone:
            return rowcount, lastrowid
        return result


def db_exec(sql, params=()) -> int:
    with _db_lock:
        cur = _db_conn.cursor()
        cur.execute(sql, params)
        _db_conn.commit()
        return cur.rowcount


# ---------- ХЕЛПЕРЫ ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_ascii_digits(s: str) -> bool:
    return bool(s) and s.isascii() and s.isdigit()


def safe_html_truncate(text: str, limit: int = 200) -> str:
    if not text:
        return ""
    text = html.escape(text, quote=False)
    if len(text) <= limit:
        return text
    text = text[:limit]
    text = re.sub(r"<[^>]*$", "", text)
    return text


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def normalize_title(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def upsert_user(user_id: int, username: str | None, first_name: str | None):
    if user_id is None:
        return
    db_query(
        "INSERT INTO users (user_id, username, first_name) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name",
        (user_id, username, first_name),
    )


def register_user(message: Message):
    if message.from_user is None:
        return
    upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)


def check_premium(user_id: int) -> bool:
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]:
        return False
    dt = parse_dt(res[0])
    return bool(dt and dt > utcnow())


def get_premium_until(user_id: int) -> datetime | None:
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]:
        return None
    return parse_dt(res[0])


def add_premium_days(user_id: int, days: int):
    current = get_premium_until(user_id)
    now = utcnow()
    new_expiry = (current + timedelta(days=days)) if (current and current > now) else (now + timedelta(days=days))
    db_query("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_expiry.isoformat(), user_id))


def wishlist_limit(user_id: int) -> int:
    return WISHLIST_LIMIT_PREMIUM if check_premium(user_id) else WISHLIST_LIMIT_FREE


def wishlist_count(user_id: int) -> int:
    return db_query("SELECT COUNT(*) FROM wishlist_titles WHERE user_id = ?", (user_id,), fetchone=True)[0]


def list_premium_users() -> list:
    return db_query(
        "SELECT user_id, username, first_name, premium_until FROM users "
        "WHERE premium_until > ? ORDER BY premium_until ASC",
        (utcnow_iso(),), fetch=True,
    )


def grant_premium_by_id(user_id: int, days: int) -> bool:
    row = db_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row:
        return False
    add_premium_days(user_id, days)
    return True


def revoke_premium(user_id: int) -> bool:
    row = db_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row:
        return False
    db_query("UPDATE users SET premium_until = NULL WHERE user_id = ?", (user_id,))
    return True


# ---------- СКАЧИВАНИЕ КАРТИНОК ----------
async def download_image(url: str) -> bytes | None:
    global _image_cache_size
    if not url:
        return None
    if url in IMAGE_CACHE:
        IMAGE_CACHE.move_to_end(url)
        return IMAGE_CACHE[url]
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://store.steampowered.com/"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if not data or len(data) < 100:
                    return None
                while _image_cache_size + len(data) > IMAGE_CACHE_MAX_BYTES and IMAGE_CACHE:
                    _, old = IMAGE_CACHE.popitem(last=False)
                    _image_cache_size -= len(old)
                IMAGE_CACHE[url] = data
                _image_cache_size += len(data)
                return data
    except Exception as e:
        logger.warning(f"[IMG] {e}")
        return None


async def send_game_photo(bot: Bot, chat_id: int, product_id: int | None, image_url: str | None,
                          caption: str, reply_markup) -> bool:
    tg_file_id = None
    if product_id:
        row = db_query("SELECT tg_file_id FROM products WHERE id = ?", (product_id,), fetchone=True)
        if row and row[0]:
            tg_file_id = row[0]

    if tg_file_id:
        try:
            await bot.send_photo(chat_id=chat_id, photo=tg_file_id,
                                 caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            return True
        except TelegramBadRequest:
            if product_id:
                db_query("UPDATE products SET tg_file_id = NULL WHERE id = ?", (product_id,))

    if not image_url:
        return False

    data = await download_image(image_url)
    if not data:
        return False

    try:
        msg = await bot.send_photo(
            chat_id=chat_id,
            photo=BufferedInputFile(data, filename="game.jpg"),
            caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML,
        )
        if product_id and msg.photo:
            db_query("UPDATE products SET tg_file_id = ? WHERE id = ?",
                     (msg.photo[-1].file_id, product_id))
        return True
    except Exception as e:
        logger.warning(f"[PHOTO] {e}")
        return False


# ---------- ПАРСЕР STEAM ----------
async def _steam_get_details(session, app_id):
    details_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=ru&l=russian"
    result = {"image": "", "description": "", "price_overview": None}
    try:
        async with session.get(details_url, timeout=8) as resp:
            if resp.status == 200:
                det_data = await resp.json()
                if str(app_id) in det_data and det_data[str(app_id)]["success"]:
                    game_data = det_data[str(app_id)]["data"]
                    result["image"] = game_data.get("header_image", "")
                    result["description"] = game_data.get("short_description", "")
                    result["price_overview"] = game_data.get("price_overview")
    except Exception:
        pass
    return result


def _format_price(price_overview) -> tuple[str, str, str]:
    if not price_overview:
        return "", "", ""
    final = price_overview.get("final", 0) / 100
    initial = price_overview.get("initial", 0) / 100
    discount = price_overview.get("discount_percent", 0)
    final_str = f"{final:.0f} ₽"
    if discount:
        original_str = f"{initial:.0f} ₽"
        discount_str = f"-{discount}%"
    else:
        original_str, discount_str = "", ""
    return final_str, original_str, discount_str


async def fetch_steam_deals():
    url = "https://store.steampowered.com/api/featuredcategories?cc=ru&l=russian"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200:
                    return
                data = await resp.json()
    except Exception as e:
        logger.warning(f"[PARSER] {e}")
        return

    added = 0
    async with aiohttp.ClientSession() as session:
        for item in data.get("specials", {}).get("items", []):
            title, app_id = item.get("name"), item.get("id")
            if not title or not app_id:
                continue
            final_price_rub = item.get("final_price", 0) / 100
            discount_percent = item.get("discount_percent", 0)
            original_price_rub = item.get("original_price", 0) / 100
            if final_price_rub <= 0 or discount_percent == 0:
                continue
            if db_query("SELECT 1 FROM products WHERE title = ?", (title,), fetchone=True):
                continue
            det = await _steam_get_details(session, app_id)
            shop_url = f"https://store.steampowered.com/app/{app_id}/"
            price_usd = ""
            if det["price_overview"]:
                price_usd = det["price_overview"].get("final_formatted", "")
            db_query(
                "INSERT OR IGNORE INTO products (title, price, old_price, discount, url, image, description, price_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, f"{final_price_rub:.0f} ₽", f"{original_price_rub:.0f} ₽",
                 f"-{discount_percent}%", shop_url, det["image"], det["description"], price_usd),
            )
            added += 1
            await asyncio.sleep(0.2)

        for section_key in ("top_sellers", "new_releases"):
            for item in data.get(section_key, {}).get("items", []):
                title, app_id = item.get("name"), item.get("id")
                if not title or not app_id:
                    continue
                if db_query("SELECT 1 FROM products WHERE title = ?", (title,), fetchone=True):
                    continue
                det = await _steam_get_details(session, app_id)
                shop_url = f"https://store.steampowered.com/app/{app_id}/"
                final_str, original_str, discount_str = _format_price(det["price_overview"])
                price_usd = ""
                if det["price_overview"]:
                    price_usd = det["price_overview"].get("final_formatted", "")
                db_query(
                    "INSERT OR IGNORE INTO products (title, price, old_price, discount, url, image, description, price_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, final_str or "Бесплатно", original_str,
                     discount_str, shop_url, det["image"], det["description"], price_usd),
                )
                added += 1
                await asyncio.sleep(0.2)
    logger.info(f"[PARSER] Добавлено новых игр: {added}")


# ---------- ЕЖЕДНЕВНАЯ СКИДКА ----------
async def send_daily_deal(bot: Bot):
    users = [row[0] for row in db_query("SELECT user_id FROM users", fetch=True)]
    if not users:
        return

    pool = db_query(
        "SELECT id, title, price, old_price, discount, url, image, description, price_usd "
        "FROM products WHERE image IS NOT NULL AND image != '' LIMIT 500",
        fetch=True,
    )
    if not pool:
        return

    sent, blocked = 0, 0

    for uid in users:
        pid, title, price, old_price, discount, url, image, description, price_usd = random.choice(pool)

        text = f"🎲 <b>Скидка дня: {html.escape(title)}</b>\n\n"
        if discount:
            text += f"🔥 Скидка: <b>{discount}</b>\n"
        if price:
            text += f"💵 Цена: <b>{price}</b>\n"
        if old_price:
            text += f"❌ Старая цена: <s>{old_price}</s>\n"
        if description:
            text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить в Steam", url=url)],
            [InlineKeyboardButton(text="🎲 Ещё случайная", callback_data="random_again")],
        ])

        ok = await send_game_photo(bot, uid, pid, image, text, kb)
        if not ok:
            try:
                await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                sent += 1
            except TelegramBadRequest as e:
                if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                    db_query("DELETE FROM users WHERE user_id = ?", (uid,))
                blocked += 1
            except Exception as e:
                logger.warning(f"[DAILY] {uid}: {e}")
                blocked += 1
        else:
            sent += 1

        await asyncio.sleep(0.07)

    logger.info(f"[DAILY] Отправлено: {sent}, ошибок: {blocked}")


# ---------- УВЕДОМЛЕНИЯ WISHLIST ----------
async def notify_wishlist_matches(bot: Bot):
    rows = db_query("""
        SELECT wt.user_id, wt.title
        FROM wishlist_titles wt
        JOIN users u ON u.user_id = wt.user_id
        WHERE u.premium_until > ? AND LENGTH(wt.title) >= 3
    """, (utcnow_iso(),), fetch=True)
    if not rows:
        return

    for uid, wtitle in rows:
        pattern = "%" + escape_like(wtitle) + "%"
        matches = db_query("""
            SELECT p.id, p.title, p.price, p.discount, p.url, p.image, p.description
            FROM products p
            WHERE LOWER(p.title) LIKE LOWER(?) ESCAPE '\\'
              AND p.discount IS NOT NULL AND p.discount != ''
              AND NOT EXISTS (
                  SELECT 1 FROM notified_wishlist nw
                  WHERE nw.user_id = ? AND nw.product_id = p.id
              )
            LIMIT 5
        """, (pattern, uid), fetch=True)
        for pid, title, price, discount, url, image, desc in matches:
            text = (
                f"⭐ <b>Твоя игра из Wishlist в скидке!</b>\n\n"
                f"🎮 {html.escape(title)}\n💵 {price}\n🔥 {discount}\n\n"
                f"📝 <i>{safe_html_truncate(desc or '', 100)}...</i>"
            )
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
            ok = await send_game_photo(bot, uid, pid, image, text, kb)
            if not ok:
                try:
                    await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                except Exception:
                    continue
            db_query("INSERT OR IGNORE INTO notified_wishlist (user_id, product_id) VALUES (?, ?)", (uid, pid))
            await asyncio.sleep(0.05)


# ---------- УВЕДОМЛЕНИЯ О СУПЕР-СКИДКАХ ----------
async def notify_premium_super_deals(bot: Bot):
    super_deals = db_query(
        "SELECT p.id, p.title, p.price, p.discount, p.url, p.image, p.description "
        "FROM products p LEFT JOIN notified_deals n ON n.product_id = p.id "
        "WHERE CAST(REPLACE(p.discount, '-', '') AS INTEGER) >= 80 "
        "AND p.discount IS NOT NULL AND p.discount != '' AND n.product_id IS NULL",
        fetch=True,
    )
    if not super_deals:
        return
    premium_users = db_query(
        "SELECT user_id FROM users WHERE premium_until > ?",
        (utcnow_iso(),), fetch=True,
    )
    for pid, title, price, discount, url, image, description in super_deals:
        text = (
            f"🔥 <b>СУПЕР-СКИДКА {discount}!</b>\n\n"
            f"🎮 {html.escape(title)}\n💵 {price}\n\n"
            f"📝 <i>{safe_html_truncate(description or '', 100)}...</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
        for (uid,) in premium_users:
            ok = await send_game_photo(bot, uid, pid, image, text, kb)
            if not ok:
                try:
                    await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
            await asyncio.sleep(0.05)
        db_query("INSERT OR IGNORE INTO notified_deals (product_id, notified_at) VALUES (?, ?)",
                 (pid, utcnow_iso()))


# ---------- ROBOKASSA ----------
def _gen_inv_id() -> int:
    return int(time.time() * 1000) * 1000 + random.randint(0, 999)


def generate_robo_link(user_id: int, amount: int, days: int) -> str:
    inv_id = _gen_inv_id()
    receipt = {
        "sno": "usn_income",
        "items": [{
            "name": f"Премиум-доступ на {days} дней",
            "quantity": 1, "sum": amount,
            "payment_method": "full_payment",
            "payment_object": "service", "tax": "none",
        }],
    }
    receipt_json = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    receipt_encoded = urllib.parse.quote(receipt_json, safe="")

    sig_str = f"{ROBO_LOGIN}:{amount}.00:{inv_id}:{receipt_encoded}:{ROBO_PASS1}"
    signature = hashlib.md5(sig_str.encode()).hexdigest()

    params = {
        "MerchantLogin": ROBO_LOGIN,
        "OutSum": f"{amount}.00",
        "InvId": inv_id,
        "Description": f"Премиум на {days} дней",
        "Receipt": receipt_encoded,
        "SignatureValue": signature,
        "IsTest": ROBO_IS_TEST,
    }
    db_query(
        "INSERT INTO payments (payment_id, user_id, amount, days, status) VALUES (?, ?, ?, ?, 'pending')",
        (str(inv_id), user_id, amount, days),
    )
    return f"https://auth.robokassa.ru/Merchant/Index.aspx?{urllib.parse.urlencode(params)}"


async def check_robokassa_payments(bot: Bot):
    if not ROBO_PASS2:
        return
    pending = db_query(
        "SELECT payment_id, user_id, amount, days FROM payments "
        "WHERE status='pending' AND created_at > datetime('now','-1 day') LIMIT 20",
        fetch=True,
    )
    for inv_id, user_id, amount, days in pending:
        sig_str = f"{ROBO_LOGIN}:{inv_id}:{ROBO_PASS2}"
        sig = hashlib.md5(sig_str.encode()).hexdigest()
        url = (
            "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpStateExt"
            f"?MerchantLogin={ROBO_LOGIN}&InvoiceID={inv_id}&Signature={sig}"
        )
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=10) as resp:
                    if resp.status != 200:
                        continue
                    text = await resp.text()
            try:
                root = ET.fromstring(text)
            except ET.ParseError:
                continue
            if root.findtext("ResultCode") != "0":
                continue
            out_sum = root.findtext("OutSum")
            if not out_sum or int(float(out_sum)) != amount:
                logger.warning(f"[ROBO] Сумма не совпадает: {out_sum} != {amount}")
                continue
            rows = db_exec(
                "UPDATE payments SET status='confirmed' WHERE payment_id=? AND status='pending'",
                (str(inv_id),),
            )
            if rows == 0:
                continue
            add_premium_days(user_id, days)
            try:
                await bot.send_message(
                    user_id,
                    f"✅ Оплата прошла! Премиум активирован на {days} дн.\n\n"
                    f"👑 Теперь тебе доступны: безлимитная рулетка, уведомления о скидках на Wishlist, "
                    f"алерты 80%+!",
                )
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[ROBO] {inv_id}: {e}")


# ---------- КЛАВИАТУРЫ ----------
_main_menu_kb = None
_admin_menu_kb = None


def main_menu_kb() -> ReplyKeyboardMarkup:
    global _main_menu_kb
    if _main_menu_kb is None:
        _main_menu_kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🔥 Скидки"), KeyboardButton(text="🎲 Случайная игра")],
                [KeyboardButton(text="💳 Пополнить Steam"), KeyboardButton(text="📱 Мобильные игры")],
                [KeyboardButton(text="💎 Премиум"), KeyboardButton(text="👤 Профиль")],
                [KeyboardButton(text="📢 Реклама"), KeyboardButton(text="⚖️ Документы")],
            ],
            resize_keyboard=True,
        )
    return _main_menu_kb


def admin_menu_kb() -> ReplyKeyboardMarkup:
    global _admin_menu_kb
    if _admin_menu_kb is None:
        _admin_menu_kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="🔄 Спарсить скидки"), KeyboardButton(text="📊 Статистика")],
                [KeyboardButton(text="💳 Проверить оплаты"), KeyboardButton(text="👑 Премиум-юзеры")],
                [KeyboardButton(text="🎁 Выдать премиум"), KeyboardButton(text="🚫 Снять премиум")],
                [KeyboardButton(text="📢 Рассылка"), KeyboardButton(text="⬅️ В меню")],
            ],
            resize_keyboard=True,
        )
    return _admin_menu_kb


# ---------- FSM ----------
class WishlistFSM(StatesGroup):
    waiting_title = State()


class TopUpFSM(StatesGroup):
    waiting_login = State()
    waiting_amount = State()


class AdminPremiumFSM(StatesGroup):
    waiting_user_id = State()
    waiting_days = State()


class AdminBroadcastFSM(StatesGroup):
    waiting_message = State()
    waiting_confirm = State()


# ---------- MIDDLEWARE: РЕГИСТРАЦИЯ + ОБНОВЛЕНИЕ ДАННЫХ ----------
class UserSyncMiddleware:
    async def __call__(self, handler, event, data):
        user = None
        if isinstance(event, Message):
            user = event.from_user
        elif isinstance(event, CallbackQuery):
            user = event.from_user
        if user is not None and not user.is_bot:
            try:
                upsert_user(user.id, user.username, user.first_name)
            except Exception as e:
                logger.debug(f"[MW] {e}")
        return await handler(event, data)


# ---------- ГЛОБАЛЬНЫЙ ERROR-HANDLER ----------
async def errors_handler(event, exception: Exception, bot: Bot):
    logger.exception(f"[ERROR] {type(exception).__name__}: {exception}")
    try:
        msg = f"⚠️ <b>Ошибка</b>\n<code>{html.escape(type(exception).__name__)}: {html.escape(str(exception)[:300])}</code>"
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(admin_id, msg)
            except Exception:
                pass
    except Exception:
        pass
    return True


# ---------- СТАРТ ----------
@router.message(CommandStart())
async def start_cmd(message: Message):
    is_prem = check_premium(message.from_user.id)
    premium_text = "\n👑 У тебя есть Премиум-доступ!" if is_prem else ""

    await message.answer(
        f"👋 Привет, {html.escape(message.from_user.first_name or 'друг')}!\n\n"
        "🐷 Я бот для выгодных покупок.\n"
        "🔥 Самые жирные скидки на Steam-игры.\n"
        "💳 Пополнение Steam и мобильных игр напрямую в кошелёк.\n"
        f"{premium_text}\n"
        "Выбирай раздел ниже 👇",
        reply_markup=main_menu_kb(),
    )

    await message.answer(
        "⚖️ <b>Юридическая информация</b>\n\n"
        "Ознакомиться с условиями оказания услуг, правилами возврата и офертой вы можете по ссылке ниже.\n\n"
        f"📞 <b>Контакты поддержки:</b> {CONTACT_TG} (email: {CONTACT_EMAIL})\n"
        f"🆔 <b>Реквизиты продавца:</b> Самозанятый, ИНН {INN}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Наш сайт", url=WEBSITE_URL)],
            [InlineKeyboardButton(text="📄 Публичная оферта", url=OFFER_URL)],
            [InlineKeyboardButton(text="📄 Политика конфиденциальности", url=PRIVACY_URL)],
        ]),
    )


@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "❓ <b>Помощь</b>\n\n"
        "🔥 <b>Скидки</b> — каталог скидок Steam\n"
        "🎲 <b>Случайная игра</b> — рулетка (Премиум — безлимит)\n"
        "💳 <b>Пополнить Steam</b> — пополнение кошелька\n"
        "📱 <b>Мобильные игры</b> — донат через Plati.Market\n"
        "💎 <b>Премиум</b> — подписка\n"
        "👤 <b>Профиль</b> — статус и Wishlist\n\n"
        f"📞 Поддержка: {CONTACT_TG}",
        reply_markup=main_menu_kb(),
    )


@router.message(Command("menu"))
async def menu_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(Command("cancel"))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_kb())


# ---------- ПРОФИЛЬ ----------
@router.message(F.text == "👤 Профиль")
async def profile_menu(message: Message):
    uid = message.from_user.id
    is_prem = check_premium(uid)
    until = get_premium_until(uid)
    joined = db_query("SELECT joined_at FROM users WHERE user_id = ?", (uid,), fetchone=True)
    w_count, w_limit = wishlist_count(uid), wishlist_limit(uid)
    text = f"👤 <b>Твой профиль</b>\n\n🆔 ID: <code>{uid}</code>\n"
    if joined and joined[0]:
        text += f"📅 С нами с: {joined[0][:10]}\n"
    text += "\n"
    if is_prem and until:
        days_left = (until - utcnow()).days + 1
        text += f"👑 Статус: <b>Премиум</b> (до {until.strftime('%d.%m.%Y')})\n⏳ Осталось: <b>{days_left} дн.</b>\n"
    else:
        text += "💎 Статус: <b>Обычный</b>\n"
    text += f"\n⭐ <b>Wishlist:</b> {w_count} / {w_limit} игр\n"

    buttons = [[InlineKeyboardButton(text="⭐ Мой Wishlist", callback_data="wl_show")]]
    if w_count < w_limit:
        buttons.append([InlineKeyboardButton(text="➕ Добавить в Wishlist", callback_data="wl_add_start")])
    if not is_prem:
        buttons.append([InlineKeyboardButton(text="💎 Купить Премиум (до 10 игр)", callback_data="premium_show")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ---------- СКИДКИ (пагинация с OFFSET) ----------
def _fetch_products_page(offset: int = 0):
    return db_query(
        "SELECT id, title, price, old_price, discount, url, image, description, price_usd "
        "FROM products WHERE image IS NOT NULL AND image != '' ORDER BY id DESC LIMIT ? OFFSET ?",
        (PAGE_LIMIT, offset), fetch=True,
    )


def _products_total() -> int:
    return db_query(
        "SELECT COUNT(*) FROM products WHERE image IS NOT NULL AND image != ''",
        fetchone=True,
    )[0]


@router.message(F.text == "🔥 Скидки")
async def discounts_menu(message: Message):
    products = _fetch_products_page(0)
    if not products:
        await message.answer("Скидок пока нет. Админ скоро их добавит! 🙂")
        return
    total = _products_total()
    await send_product_card(message, products, 0, 0, total)


async def send_product_card(message_or_callback, products, index: int, offset: int, total: int):
    pid, title, price, old_price, discount, url, image, description, price_usd = products[index]
    display_usd = f" (в долларах: {price_usd})" if (price_usd and "руб" not in price_usd.lower() and "₽" not in price_usd) else ""
    text = f"🎮 <b>{html.escape(title)}</b>\n\n"
    if old_price:
        text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price:
        text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount:
        text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description:
        text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
    global_index = offset + index + 1
    text += f"\n📄 Игра {global_index} из {total}"

    nav_row = []
    if global_index > 1:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{global_index - 2}"))
    if global_index < total:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{global_index}"))
    buttons = []
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🛒 Купить в Steam", url=url)])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    if not isinstance(message_or_callback, CallbackQuery):
        ok = await send_game_photo(message_or_callback.bot, message_or_callback.chat.id, pid, image, text, kb)
        if not ok:
            await message_or_callback.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    cb = message_or_callback
    try:
        await cb.answer()
    except Exception:
        pass
    msg = cb.message
    if msg is None:
        return
    try:
        if msg.photo:
            tg_file_id = None
            row = db_query("SELECT tg_file_id FROM products WHERE id = ?", (pid,), fetchone=True)
            if row and row[0]:
                tg_file_id = row[0]
            media = tg_file_id if tg_file_id else None
            if media is None:
                data = await download_image(image)
                if data:
                    media = BufferedInputFile(data, filename="game.jpg")
            if media:
                await msg.edit_media(
                    media=InputMediaPhoto(media=media, caption=text, parse_mode=ParseMode.HTML),
                    reply_markup=kb,
                )
                return
        else:
            await msg.edit_text(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        logger.debug(f"[PAGINATION] {e}")

    ok = await send_game_photo(cb.bot, msg.chat.id, pid, image, text, kb)
    if not ok:
        try:
            await cb.bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception as e:
            logger.error(f"[PAGINATION] {e}")
            return
    try:
        await msg.delete()
    except Exception:
        pass


@router.callback_query(F.data.startswith("page_"))
async def paginate_products(cb: CallbackQuery):
    try:
        global_index = int(cb.data.split("_", 1)[1])
    except (IndexError, ValueError):
        await cb.answer("Ошибка", show_alert=False)
        return
    if global_index < 0:
        await cb.answer("Ошибка", show_alert=False)
        return
    offset = (global_index // PAGE_LIMIT) * PAGE_LIMIT
    index = global_index - offset
    products = _fetch_products_page(offset)
    total = _products_total()
    if 0 <= index < len(products):
        await send_product_card(cb, products, index, offset, total)
    else:
        await cb.answer("Дальше игр нет", show_alert=False)


# ---------- СЛУЧАЙНАЯ ИГРА ----------
@router.message(F.text == "🎲 Случайная игра")
async def random_game(message: Message):
    user_id = message.from_user.id
    is_prem = check_premium(user_id)

    if not is_prem:
        res = db_query("SELECT last_roulette FROM users WHERE user_id = ?", (user_id,), fetchone=True)
        last = parse_dt(res[0]) if res else None
        if last and utcnow() - last < timedelta(days=1):
            time_left = timedelta(days=1) - (utcnow() - last)
            h, m = time_left.seconds // 3600, (time_left.seconds % 3600) // 60
            await message.answer(
                f"⏳ <b>Лимит рулетки!</b>\n\nОбычные пользователи — 1 раз в день.\n"
                f"Следующая попытка через: <b>{h} ч. {m} мин.</b>\n\n"
                f"💎 <b>Купи Премиум</b> — крути без ограничений!",
            )
            return

    if is_prem:
        candidates = db_query(
            "SELECT id, title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE discount IS NOT NULL AND discount != '' "
            "AND image IS NOT NULL AND image != '' "
            "ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 20",
            fetch=True,
        )
        game = random.choice(candidates) if candidates else None
    else:
        game = None
    if not game:
        game = db_query(
            "SELECT id, title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1",
            fetchone=True,
        )

    if not game:
        await message.answer("В базе пока нет игр.")
        return
    db_query("UPDATE users SET last_roulette = ? WHERE user_id = ?", (utcnow_iso(), user_id))

    pid, title, price, old_price, discount, url, image, description, price_usd = game
    display_usd = f" (в долларах: {price_usd})" if (price_usd and "руб" not in price_usd.lower() and "₽" not in price_usd) else ""
    text = f"🎲 <b>Тебе выпала: {html.escape(title)}</b>\n\n"
    if old_price:
        text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price:
        text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount:
        text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description:
        text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
    if is_prem:
        text += "\n👑 <i>Бонус Премиум: максимальная скидка!</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]])
    ok = await send_game_photo(message.bot, message.chat.id, pid, image, text, kb)
    if not ok:
        await message.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "random_again")
async def random_again(cb: CallbackQuery):
    uid = cb.from_user.id
    is_prem = check_premium(uid)

    if not is_prem:
        res = db_query("SELECT last_random_again FROM users WHERE user_id = ?", (uid,), fetchone=True)
        last = parse_dt(res[0]) if res else None
        if last and utcnow() - last < timedelta(minutes=10):
            await cb.answer("⏳ Раз в 10 минут. Купи Премиум — без лимита!", show_alert=True)
            return

    if is_prem:
        game = db_query(
            "SELECT id, title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE discount IS NOT NULL AND discount != '' "
            "AND image IS NOT NULL AND image != '' "
            "ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 1",
            fetchone=True,
        )
    else:
        game = db_query(
            "SELECT id, title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1",
            fetchone=True,
        )
    await cb.answer()
    if not game:
        await cb.answer("В базе пока пусто", show_alert=True)
        return

    if not is_prem:
        db_query("UPDATE users SET last_random_again = ? WHERE user_id = ?", (utcnow_iso(), uid))

    pid, title, price, old_price, discount, url, image, description, price_usd = game
    text = f"🎲 <b>Тебе выпала: {html.escape(title)}</b>\n\n"
    if old_price:
        text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price:
        text += f"💵 Цена: <b>{price}</b>\n"
    if discount:
        text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description:
        text += f"\n📝 <i>{safe_html_truncate(description, 150)}...</i>\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]])
    ok = await send_game_photo(cb.bot, cb.message.chat.id, pid, image, text, kb)
    if not ok:
        await cb.message.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ---------- WISHLIST ----------
@router.callback_query(F.data == "wl_add_start")
async def wl_add_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    limit, count = wishlist_limit(uid), wishlist_count(uid)
    if count >= limit:
        await cb.answer(f"Лимит Wishlist: {limit} игр.\nКупи Премиум, чтобы добавить до 10!", show_alert=True)
        return
    await state.set_state(WishlistFSM.waiting_title)
    await cb.message.answer(
        f"⭐ <b>Добавление в Wishlist</b>\n\nНапиши название игры.\nУ тебя: {count} / {limit} игр.\n\n"
        f"<i>Напиши «отмена», чтобы выйти.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="wl_cancel")]]),
    )
    await cb.answer()


@router.callback_query(F.data == "wl_cancel")
async def wl_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❌ Добавление отменено.")
    except Exception:
        pass
    await cb.answer()


@router.message(WishlistFSM.waiting_title)
async def wl_add_title(message: Message, state: FSMContext):
    raw = normalize_title(message.text or "")
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_menu_kb())
        return
    if len(raw) < 2:
        await message.answer("Слишком короткое название. Попробуй ещё раз или напиши «отмена».")
        return
    if len(raw) > 200:
        await message.answer("Слишком длинное название. Попробуй ещё раз или напиши «отмена».")
        return
    uid = message.from_user.id
    limit, count = wishlist_limit(uid), wishlist_count(uid)
    if count >= limit:
        await state.clear()
        await message.answer(f"❌ Достигнут лимит Wishlist ({limit} игр).", reply_markup=main_menu_kb())
        return
    existing = db_query(
        "SELECT id FROM wishlist_titles WHERE user_id = ? AND LOWER(title) = LOWER(?)",
        (uid, raw), fetchone=True,
    )
    if existing:
        await state.clear()
        await message.answer(f"⚠️ <b>{html.escape(raw)}</b> уже есть в Wishlist.", reply_markup=main_menu_kb())
        return
    db_query("INSERT OR IGNORE INTO wishlist_titles (user_id, title) VALUES (?, ?)", (uid, raw))
    await state.clear()
    await message.answer(
        f"✅ <b>{html.escape(raw)}</b> добавлено в Wishlist!\nСейчас у тебя: {count + 1} / {limit} игр.",
        reply_markup=main_menu_kb(),
    )


@router.callback_query(F.data == "wl_show")
async def wl_show(cb: CallbackQuery):
    uid = cb.from_user.id
    limit = wishlist_limit(uid)
    rows = db_query("SELECT id, title FROM wishlist_titles WHERE user_id = ? ORDER BY id", (uid,), fetch=True)
    if not rows:
        await cb.answer("Wishlist пуст", show_alert=True)
        return
    text = f"⭐ <b>Твой Wishlist</b> ({len(rows)} / {limit}):\n\n"
    buttons = []
    for wid, title in rows:
        text += f"• {html.escape(title)}\n"
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить: {title[:25]}", callback_data=f"wl_del_{wid}")])
    if not check_premium(uid):
        text += "\n💎 Хочешь до 10 игр + уведомления о скидках? Купи Премиум!"
        buttons.append([InlineKeyboardButton(text="💎 Премиум", callback_data="premium_show")])
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await cb.answer()


@router.callback_query(F.data.startswith("wl_del_"))
async def wl_del(cb: CallbackQuery):
    try:
        wid = int(cb.data.split("_", 2)[2])
    except (IndexError, ValueError):
        await cb.answer("Ошибка")
        return
    db_query("DELETE FROM wishlist_titles WHERE id = ? AND user_id = ?", (wid, cb.from_user.id))
    await cb.answer("Удалено")
    await wl_show(cb)


# ---------- ПОПОЛНЕНИЕ STEAM ----------
@router.message(F.text == "💳 Пополнить Steam")
async def steam_topup_start(message: Message, state: FSMContext):
    await state.set_state(TopUpFSM.waiting_login)
    await message.answer(
        "💳 <b>Пополнение Steam</b>\n\n"
        "Шаг 1 из 2. Введи свой <b>логин Steam</b> (не пароль!).\n"
        "Например: <i>my_nickname</i>\n\n"
        "<i>Напиши «отмена», чтобы выйти.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")],
        ]),
    )


@router.callback_query(F.data == "topup_cancel")
async def topup_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❌ Пополнение отменено.")
    except Exception:
        pass
    await cb.answer()


@router.message(TopUpFSM.waiting_login)
async def topup_login(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_menu_kb())
        return
    if not raw or len(raw) < 3 or " " in raw or len(raw) > 64:
        await message.answer("⚠️ Некорректный логин. Попробуй ещё раз (от 3 до 64 символов, без пробелов).")
        return
    await state.update_data(login=raw)
    await state.set_state(TopUpFSM.waiting_amount)
    await message.answer(
        f"👤 Логин: <b>{html.escape(raw)}</b>\n\n"
        "Шаг 2 из 2. Введи <b>сумму пополнения в рублях</b> (только цифры).\n"
        "Например: <i>500</i>\n\n"
        "<i>Напиши «отмена», чтобы выйти.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")],
        ]),
    )


@router.message(TopUpFSM.waiting_amount)
async def topup_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_menu_kb())
        return
    if not is_ascii_digits(raw) or len(raw) > 6:
        await message.answer("⚠️ Пожалуйста, введи сумму только цифрами (например, 500).")
        return
    amount = int(raw)
    if amount < 100:
        await message.answer("⚠️ Минимальная сумма пополнения — 100 рублей. Попробуй ещё раз.")
        return
    if amount > 50000:
        await message.answer("⚠️ Максимальная сумма — 50 000 рублей. Если нужно больше, сделай несколько пополнений.")
        return

    data = await state.get_data()
    login = data.get("login", "")
    await state.clear()

    db_query("INSERT INTO link_clicks (user_id, link_type) VALUES (?, ?)",
             (message.from_user.id, f"steam_sum_{amount}"))

    url = f"https://plati.market/itm/{STEAM_TOPUP_ITEM_ID}?agent={PLATI_AGENT_ID}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=url)],
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close_msg")],
    ])

    await message.answer(
        f"✅ <b>Готово к пополнению</b>\n\n"
        f"👤 Логин: <b>{html.escape(login)}</b>\n"
        f"💵 Сумма: <b>{amount} ₽</b>\n\n"
        f"1️⃣ Нажми кнопку ниже — откроется страница товара.\n"
        f"2️⃣ Вставь свой логин <b>{html.escape(login)}</b> в поле «Ваш логин Steam».\n"
        f"3️⃣ Введи сумму <b>{amount}</b> в поле «Получу, РУБЛЕЙ RUB».\n"
        f"4️⃣ Оплати картой и жди зачисления (5–15 минут).\n\n"
        f"⚠️ <b>Важно:</b> пароль вводить не нужно — только логин!",
        reply_markup=kb,
    )


@router.callback_query(F.data == "close_msg")
async def close_msg(cb: CallbackQuery):
    try:
        await cb.message.delete()
    except Exception:
        pass
    await cb.answer()


# ---------- МОБИЛЬНЫЕ ИГРЫ ----------
@router.message(F.text == "📱 Мобильные игры")
async def mobile_topup_menu(message: Message):
    db_query("INSERT INTO link_clicks (user_id, link_type) VALUES (?, ?)",
             (message.from_user.id, "mobile_menu"))
    PLATI_MOBILE_URL = f"https://plati.market/search/Донат?agent={PLATI_AGENT_ID}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎮 Каталог мобильных игр", url=PLATI_MOBILE_URL)],
        [InlineKeyboardButton(text="❓ Как это работает", callback_data="mobile_help")],
    ])
    await message.answer(
        "📱 <b>Пополнение мобильных игр</b>\n\n"
        "Пополняй любимые мобильные игры через <b>Plati.Market</b>:\n\n"
        "🎮 Genshin Impact, Honkai, PUBG Mobile, Brawl Stars и другие\n"
        "⚡ Быстрое зачисление (5–15 минут)\n"
        "💳 Оплата картами РФ\n\n"
        "Нажми кнопку ниже 👇",
        reply_markup=kb,
    )


@router.callback_query(F.data == "mobile_help")
async def mobile_help(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "❓ <b>Как пополнить мобильную игру</b>\n\n"
        "1️⃣ Нажми «Пополнить мобильные игры»\n"
        "2️⃣ Выбери свою игру из списка\n"
        "3️⃣ Введи <b>игровой ID</b> (не логин/пароль!)\n"
        "4️⃣ Выбери номинал (например, 60 гемов, 100 UC)\n"
        "5️⃣ Оплати картой\n\n"
        "⚠️ <b>Важно:</b> вводи только игровой ID!\n\n"
        f"Если что-то пошло не так — пиши {PLATI_SUPPORT}",
    )


# ---------- ПРЕМИУМ ----------
async def premium_menu(message: Message, user_id: int | None = None):
    uid = user_id if user_id is not None else message.from_user.id
    is_prem = check_premium(uid)
    status = ""
    if is_prem:
        until = get_premium_until(uid)
        if until:
            status = f"👑 <b>Твой Премиум активен до {until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔥 7 дней — {PRICE_7_DAYS} ₽", callback_data=f"promo_{PRICE_7_DAYS}_7")],
        [InlineKeyboardButton(text=f"👑 30 дней — {PRICE_30_DAYS} ₽ (выгодно!)", callback_data=f"promo_{PRICE_30_DAYS}_30")],
    ])
    await message.answer(
        f"{status}"
        "💎 <b>Премиум-доступ</b>\n\n"
        "Открой все возможности бота:\n\n"
        "🎲 <b>Безлимитная рулетка</b>\n"
        "   <i>Крути сколько хочешь и находи лучшие скидки первым.</i>\n\n"
        "🏆 <b>Только топовые скидки</b>\n"
        "   <i>Рулетка выдает игры с максимальным процентом скидки.</i>\n\n"
        "⭐ <b>Wishlist до 10 игр</b>\n"
        "   <i>Добавляй больше игр в список желаемого.</i>\n\n"
        "🔔 <b>Мгновенные алерты</b>\n"
        "   <i>Персональные уведомления, как только на игру из Wishlist появляется скидка или цена падает ниже 80%.</i>\n\n"
        "Выбери срок:",
        reply_markup=kb,
    )


@router.message(F.text == "💎 Премиум")
async def premium_menu_msg(message: Message):
    await premium_menu(message)


@router.callback_query(F.data == "premium_show")
async def premium_show(cb: CallbackQuery):
    await cb.answer()
    if cb.message:
        await premium_menu(cb.message, user_id=cb.from_user.id)


@router.callback_query(F.data.startswith("promo_"))
async def promo_select(cb: CallbackQuery):
    parts = cb.data.split("_")
    if len(parts) < 3:
        await cb.answer("Ошибка")
        return
    try:
        amount, days = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        await cb.answer("Ошибка")
        return

    if not ROBO_PASS1 or not ROBO_LOGIN:
        await cb.answer("Оплата временно недоступна", show_alert=True)
        return

    uid = cb.from_user.id
    pending = db_query(
        "SELECT COUNT(*) FROM payments WHERE user_id=? AND status='pending' "
        "AND created_at > datetime('now','-10 minutes')",
        (uid,), fetchone=True,
    )[0]
    if pending >= 3:
        await cb.answer("⏳ У тебя уже есть неоплаченные счета. Подожди 10 минут.", show_alert=True)
        return

    try:
        payment_url = generate_robo_link(uid, amount, days)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=payment_url)],
        ])
        text = (
            f"💳 <b>Оплата через Robokassa</b>\n\n"
            f"Срок: <b>{days} дн.</b> за <b>{amount} ₽</b>\n\n"
            f"Нажми кнопку ниже. Премиум активируется автоматически (проверка занимает до 1-2 минут)."
        )
        try:
            await cb.message.edit_text(text, reply_markup=kb)
        except TelegramBadRequest:
            await cb.message.answer(text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Robokassa error: {e}")
        await cb.answer("❌ Ошибка создания платежа", show_alert=True)


# ---------- РЕКЛАМА ----------
@router.message(F.text == "📢 Реклама")
async def ads_info(message: Message):
    await message.answer(
        "📢 <b>Реклама в нашем боте</b>\n\n"
        "🎯 Пост в рассылке, закреп, упоминание в «Скидке дня».\n\n"
        f"Пиши: 👉 <b>{CONTACT_TG}</b>",
        reply_markup=main_menu_kb(),
    )


# ---------- ДОКУМЕНТЫ ----------
@router.message(F.text == "⚖️ Документы")
async def docs_info(message: Message):
    await message.answer(
        "⚖️ <b>Юридическая информация</b>\n\n"
        "Ознакомьтесь с документами перед использованием бота:\n\n"
        f"📞 <b>Контакты поддержки:</b> {CONTACT_TG} (email: {CONTACT_EMAIL})\n"
        f"🆔 <b>Реквизиты продавца:</b> Самозанятый, ИНН {INN}\n\n"
        "Правила возврата средств указаны в оферте.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌐 Наш сайт", url=WEBSITE_URL)],
            [InlineKeyboardButton(text="📄 Публичная оферта", url=OFFER_URL)],
            [InlineKeyboardButton(text="📄 Политика конфиденциальности", url=PRIVACY_URL)],
        ]),
    )


# ---------- АДМИНКА ----------
@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())


@router.message(F.text == "⬅️ В меню")
async def to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(F.text == "🔄 Спарсить скидки")
async def manual_parse(message: Message):
    if not is_admin(message.from_user.id):
        return
    await message.answer("⏳ Парсим Steam...")
    await fetch_steam_deals()
    await message.answer("✅ Парсинг завершён!", reply_markup=admin_menu_kb())


@router.message(F.text == "💳 Проверить оплаты")
async def check_payments(message: Message):
    if not is_admin(message.from_user.id):
        return
    rows = db_query(
        "SELECT id, user_id, amount, days, status FROM payments ORDER BY id DESC LIMIT 20",
        fetch=True,
    )
    if not rows:
        await message.answer("Нет платежей.", reply_markup=admin_menu_kb())
        return
    text = "📋 <b>Последние платежи:</b>\n\n"
    for pay_id, user_id, amount, days, status in rows:
        emoji = "✅" if status == "confirmed" else "⏳" if status == "pending" else "❌"
        text += f"{emoji} #{pay_id} | U:{user_id} | {amount}₽ / {days}д.\n"
    await message.answer(text, reply_markup=admin_menu_kb())


@router.message(F.text == "📊 Статистика")
async def stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    users = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    products = db_query("SELECT COUNT(*) FROM products", fetchone=True)[0]
    confirmed_pay = db_query("SELECT SUM(amount) FROM payments WHERE status='confirmed'", fetchone=True)[0] or 0
    wish = db_query("SELECT COUNT(*) FROM wishlist_titles", fetchone=True)[0]
    steam_clicks = db_query("SELECT COUNT(*) FROM link_clicks WHERE link_type LIKE 'steam%'", fetchone=True)[0]
    mobile_clicks = db_query("SELECT COUNT(*) FROM link_clicks WHERE link_type LIKE 'mobile%'", fetchone=True)[0]

    prem_list = list_premium_users()
    prem_count = len(prem_list)
    text_prem = f"\n👑 <b>Премиум: {prem_count}</b>\n"
    if prem_count:
        for uid, uname, fname, until in prem_list[:10]:
            dt = parse_dt(until)
            if dt:
                days_left = (dt - utcnow()).days
                text_prem += f"  • <code>{uid}</code> — {days_left} дн.\n"
        if prem_count > 10:
            text_prem += f"  ... и ещё {prem_count - 10}\n"

    await message.answer(
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего юзеров: {users}\n"
        f"🎮 Игр: {products}\n"
        f"⭐ Wishlist записей: {wish}\n"
        f"💵 Заработано: {confirmed_pay} ₽\n"
        f"💳 Кликов Steam: {steam_clicks}\n"
        f"📱 Кликов Mobile: {mobile_clicks}\n"
        f"{text_prem}",
        reply_markup=admin_menu_kb(),
    )


@router.message(F.text == "👑 Премиум-юзеры")
async def admin_list_premium(message: Message):
    if not is_admin(message.from_user.id):
        return
    users = list_premium_users()
    if not users:
        await message.answer("👑 Активных премиум-юзеров пока нет.", reply_markup=admin_menu_kb())
        return
    text = f"👑 <b>Активные премиум-юзеры ({len(users)}):</b>\n\n"
    for uid, uname, fname, until in users:
        dt = parse_dt(until)
        if not dt:
            continue
        days_left = (dt - utcnow()).days
        uname_str = f"@{uname}" if uname else (fname or "—")
        text += f"• <code>{uid}</code> | {uname_str} | до {dt.strftime('%d.%m.%Y')} ({days_left} дн.)\n"
        if len(text) > 3500:
            await message.answer(text, reply_markup=admin_menu_kb())
            text = ""
    if text:
        await message.answer(text, reply_markup=admin_menu_kb())


@router.message(F.text == "🎁 Выдать премиум")
async def admin_grant_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminPremiumFSM.waiting_user_id)
    await state.update_data(action="grant")
    await message.answer(
        "🎁 <b>Выдача Премиума</b>\n\nВведи <b>user_id</b> пользователя (число).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")],
        ]),
    )


@router.message(F.text == "🚫 Снять премиум")
async def admin_revoke_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminPremiumFSM.waiting_user_id)
    await state.update_data(action="revoke")
    await message.answer(
        "🚫 <b>Снятие Премиума</b>\n\nВведи <b>user_id</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")],
        ]),
    )


@router.message(AdminPremiumFSM.waiting_user_id)
async def admin_uid_router(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    data = await state.get_data()
    action = data.get("action", "grant")

    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=admin_menu_kb())
        return
    if not is_ascii_digits(raw):
        await message.answer("⚠️ Нужно целое число — user_id.")
        return
    target = int(raw)

    if action == "revoke":
        await state.clear()
        if revoke_premium(target):
            await message.answer(f"🚫 Премиум снят с <code>{target}</code>.", reply_markup=admin_menu_kb())
            try:
                await message.bot.send_message(target, "ℹ️ Твой Премиум был отключён администратором.")
            except Exception:
                pass
        else:
            await message.answer("❌ Юзер не найден.", reply_markup=admin_menu_kb())
        return

    row = db_query("SELECT first_name, username FROM users WHERE user_id = ?", (target,), fetchone=True)
    if not row:
        await message.answer("❌ Пользователь с таким ID не найден в базе.")
        return
    await state.update_data(target_id=target)
    await state.set_state(AdminPremiumFSM.waiting_days)
    fname, uname = row
    await message.answer(
        f"👤 Найден: <b>{html.escape(fname or '—')}</b> (@{uname or '—'}, <code>{target}</code>)\n\n"
        f"На сколько <b>дней</b> выдать премиум? (например, 30)\n"
        f"Если уже есть премиум — дни добавятся сверху.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")],
        ]),
    )


@router.message(AdminPremiumFSM.waiting_days)
async def admin_grant_days(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=admin_menu_kb())
        return
    if not is_ascii_digits(raw):
        await message.answer("⚠️ Введи число дней от 1 до 3650.")
        return
    days = int(raw)
    if days <= 0 or days > 3650:
        await message.answer("⚠️ Введи число дней от 1 до 3650.")
        return
    data = await state.get_data()
    target = data.get("target_id")
    await state.clear()

    if not target:
        await message.answer("❌ Ошибка: пользователь не выбран.", reply_markup=admin_menu_kb())
        return

    ok = grant_premium_by_id(target, days)
    if ok:
        await message.answer(
            f"✅ Премиум выдан пользователю <code>{target}</code> на <b>{days} дн.</b>",
            reply_markup=admin_menu_kb(),
        )
        try:
            await message.bot.send_message(
                target,
                f"🎉 <b>Тебе активирован Премиум на {days} дней!</b>\n\nСпасибо, что с нами 💎",
            )
        except Exception:
            pass
    else:
        await message.answer("❌ Не удалось — юзер не найден.", reply_markup=admin_menu_kb())


@router.callback_query(F.data == "admin_prem_cancel")
async def admin_prem_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❌ Отменено.")
    except Exception:
        pass
    await cb.answer()


# ---------- РАССЫЛКА С ПРЕДПРОСМОТРОМ ----------
@router.message(F.text == "📢 Рассылка")
async def admin_broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminBroadcastFSM.waiting_message)
    await message.answer(
        "📢 <b>Режим рассылки</b>\n\n"
        "Отправь мне сообщение (текст и/или фото), которое нужно разослать.\n"
        "Перед отправкой покажу предпросмотр.\n\n"
        "<i>Напиши «отмена», чтобы выйти.</i>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="⬅️ Отмена")]],
            resize_keyboard=True,
        ),
    )


@router.message(AdminBroadcastFSM.waiting_message)
async def admin_broadcast_preview(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        return
    if message.text == "⬅️ Отмена" or (message.text and message.text.lower() in ("отмена", "cancel", "/cancel")):
        await state.clear()
        await message.answer("❌ Рассылка отменена.", reply_markup=admin_menu_kb())
        return

    photo_id = message.photo[-1].file_id if message.photo else None
    text = message.text if message.text else message.caption
    if not photo_id and not text:
        await message.answer("⚠️ Сообщение пустое. Отправь текст или фото.")
        return

    await state.update_data(photo_id=photo_id, text=text)
    await state.set_state(AdminBroadcastFSM.waiting_confirm)

    total = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    eta_sec = total * 0.05
    eta_str = f"{int(eta_sec // 60)} мин {int(eta_sec % 60)} сек"

    await message.answer(
        f"👁 <b>Предпросмотр</b>\n\n"
        f"Получателей: <b>{total}</b>\n"
        f"Ориентировочное время: <b>{eta_str}</b>\n\n"
        f"Отправляем?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="bc_send")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")],
        ]),
    )
    try:
        if photo_id:
            await bot.send_photo(
                chat_id=message.chat.id, photo=photo_id, caption=text, parse_mode=ParseMode.HTML,
            )
        else:
            await bot.send_message(chat_id=message.chat.id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e:
        await message.answer(f"⚠️ Не удалось показать предпросмотр: {e}")


@router.callback_query(F.data == "bc_cancel")
async def admin_broadcast_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❌ Рассылка отменена.")
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data == "bc_send")
async def admin_broadcast_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    photo_id = data.get("photo_id")
    text = data.get("text")
    await state.clear()

    if not photo_id and not text:
        await cb.answer("Пустое сообщение", show_alert=True)
        return

    users = db_query("SELECT user_id FROM users", fetch=True)
    total = len(users)
    try:
        await cb.message.edit_text(f"⏳ Рассылка для {total} пользователей...")
    except Exception:
        pass
    await cb.answer()

    sent, blocked = 0, 0
    for (uid,) in users:
        try:
            if photo_id:
                await bot.send_photo(chat_id=uid, photo=photo_id, caption=text, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramBadRequest as e:
            if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                db_query("DELETE FROM users WHERE user_id = ?", (uid,))
            blocked += 1
        except Exception as e:
            logger.warning(f"[BROADCAST] {uid}: {e}")
            blocked += 1
        await asyncio.sleep(0.05)

    try:
        await cb.message.edit_text(
            f"✅ <b>Рассылка завершена!</b>\n\n"
            f"📤 Отправлено: {sent}\n"
            f"🚫 Заблокировали/Ошибка: {blocked}",
        )
    except Exception:
        await bot.send_message(cb.from_user.id,
                               f"✅ Рассылка: {sent} отправлено, {blocked} ошибок.",
                               reply_markup=admin_menu_kb())
    await bot.send_message(cb.from_user.id, "Готово.", reply_markup=admin_menu_kb())


# ---------- ЗАПУСК ----------
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(UserSyncMiddleware())
    dp.callback_query.middleware(UserSyncMiddleware())
    dp.include_router(router)
    dp.errors.register(errors_handler)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(fetch_steam_deals, "interval", hours=12, max_instances=1)
    scheduler.add_job(check_robokassa_payments, "interval", minutes=1, args=[bot], max_instances=1)
    scheduler.add_job(send_daily_deal, "cron", hour=12, minute=0, args=[bot], max_instances=1)
    scheduler.add_job(send_daily_deal, "cron", hour=19, minute=0, args=[bot], max_instances=1)
    scheduler.add_job(notify_premium_super_deals, "interval", hours=3, args=[bot], max_instances=1)
    scheduler.add_job(notify_wishlist_matches, "interval", minutes=30, args=[bot], max_instances=1)
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен! (Оптимизированная версия)")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if _db_conn:
            _db_conn.close()


if __name__ == "__main__":
    asyncio.run(main())
