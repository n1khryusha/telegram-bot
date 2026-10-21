import asyncio
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timedelta

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
# ===================================

if not BOT_TOKEN:
    raise SystemExit("❌ BOT_TOKEN не задан в .env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger("bot")

router = Router()
PAGE_LIMIT = 50

WISHLIST_LIMIT_FREE = 1
WISHLIST_LIMIT_PREMIUM = 10

# ---------- КЭШ КАРТИНОК (ограниченный) ----------
IMAGE_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
IMAGE_CACHE_MAX_BYTES = 50 * 1024 * 1024  # 50 МБ
_image_cache_size = 0


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
    CREATE TABLE IF NOT EXISTS notified_deals (product_id INTEGER PRIMARY KEY);
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
    """)
    _db_conn.commit()


def db_query(sql, params=(), fetch=False, fetchone=False):
    with _db_lock:
        cur = _db_conn.cursor()
        cur.execute(sql, params)
        if fetch:
            rows = cur.fetchall()
        elif fetchone:
            rows = cur.fetchone()
        else:
            rows = None
        _db_conn.commit()
        return rows


# ---------- ХЕЛПЕРЫ ----------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_ascii_digits(s: str) -> bool:
    return bool(s) and s.isascii() and s.isdigit()


def safe_html_truncate(text: str, limit: int = 200) -> str:
    """Обрезает текст и убирает незакрытые HTML-теги в конце."""
    if not text:
        return ""
    text = text[:limit]
    text = re.sub(r"<[^>]*$", "", text)
    return text


def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def register_user(message: Message):
    db_query(
        "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
        (message.from_user.id, message.from_user.username, message.from_user.first_name),
    )


def check_premium(user_id: int) -> bool:
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]:
        return False
    try:
        return datetime.fromisoformat(res[0]) > datetime.now()
    except (ValueError, TypeError):
        return False


def get_premium_until(user_id: int):
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]:
        return None
    try:
        return datetime.fromisoformat(res[0])
    except (ValueError, TypeError):
        return None


def add_premium_days(user_id: int, days: int):
    current = get_premium_until(user_id)
    now = datetime.now()
    new_expiry = (current + timedelta(days=days)) if (current and current > now) else (now + timedelta(days=days))
    db_query("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_expiry.isoformat(), user_id))


def wishlist_limit(user_id: int) -> int:
    return WISHLIST_LIMIT_PREMIUM if check_premium(user_id) else WISHLIST_LIMIT_FREE


def wishlist_count(user_id: int) -> int:
    return db_query("SELECT COUNT(*) FROM wishlist_titles WHERE user_id = ?", (user_id,), fetchone=True)[0]


def list_premium_users() -> list:
    return db_query(
        "SELECT user_id, username, first_name, premium_until "
        "FROM users WHERE premium_until > ? ORDER BY premium_until ASC",
        (datetime.now().isoformat(),), fetch=True,
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


# ---------- ПАРСЕР STEAM ----------
async def _steam_get_details(session, app_id):
    details_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=ru&l=russian"
    image_url, description, price_usd = "", "", ""
    try:
        async with session.get(details_url, timeout=5) as resp:
            if resp.status == 200:
                det_data = await resp.json()
                if str(app_id) in det_data and det_data[str(app_id)]["success"]:
                    game_data = det_data[str(app_id)]["data"]
                    image_url = game_data.get("header_image", "")
                    description = game_data.get("short_description", "")
                    price_usd = game_data.get("price_overview", {}).get("final_formatted", "")
    except Exception:
        pass
    return image_url, description, price_usd


async def fetch_steam_deals():
    url = "https://store.steampowered.com/api/featuredcategories?cc=ru&l=russian"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
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
            image_url, description, price_usd = await _steam_get_details(session, app_id)
            shop_url = f"https://store.steampowered.com/app/{app_id}/"
            db_query(
                "INSERT OR IGNORE INTO products (title, price, old_price, discount, url, image, description, price_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, f"{final_price_rub:.0f} ₽", f"{original_price_rub:.0f} ₽",
                 f"-{discount_percent}%", shop_url, image_url, description, price_usd),
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
                image_url, description, price_usd = await _steam_get_details(session, app_id)
                shop_url = f"https://store.steampowered.com/app/{app_id}/"
                final_price_str, original_price_str, discount_str = "", "", ""
                try:
                    details_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=ru&l=russian"
                    async with session.get(details_url, timeout=5) as r:
                        if r.status == 200:
                            dd = await r.json()
                            if str(app_id) in dd and dd[str(app_id)]["success"]:
                                pd = dd[str(app_id)]["data"].get("price_overview")
                                if pd:
                                    final_price_str = f"{pd.get('final', 0) / 100:.0f} ₽"
                                    original_price_str = f"{pd.get('initial', 0) / 100:.0f} ₽" if pd.get("discount_percent") else ""
                                    discount_str = f"-{pd.get('discount_percent')}%" if pd.get("discount_percent") else ""
                except Exception:
                    pass
                db_query(
                    "INSERT OR IGNORE INTO products (title, price, old_price, discount, url, image, description, price_usd) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, final_price_str or "Бесплатно", original_price_str,
                     discount_str, shop_url, image_url, description, price_usd),
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
        "SELECT title, price, old_price, discount, url, image, description, price_usd "
        "FROM products WHERE image IS NOT NULL AND image != '' LIMIT 500",
        fetch=True,
    )
    if not pool:
        return

    sent, blocked = 0, 0

    for uid in users:
        game = random.choice(pool)
        title, price, old_price, discount, url, image, description, price_usd = game

        text = f"🎲 <b>Скидка дня: {title}</b>\n\n"
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
        photo = await download_image(image) if image else None

        try:
            if photo:
                await bot.send_photo(
                    chat_id=uid,
                    photo=BufferedInputFile(photo, filename="deal.jpg"),
                    caption=text, reply_markup=kb, parse_mode=ParseMode.HTML,
                )
            else:
                await bot.send_message(
                    chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML,
                )
            sent += 1
        except TelegramBadRequest as e:
            if "blocked" in str(e).lower() or "chat not found" in str(e).lower():
                db_query("DELETE FROM users WHERE user_id = ?", (uid,))
            blocked += 1
        except Exception as e:
            logger.warning(f"[DAILY] {uid}: {e}")

        await asyncio.sleep(0.07)

    logger.info(f"[DAILY] Отправлено: {sent}, ошибок: {blocked}")


# ---------- УВЕДОМЛЕНИЯ WISHLIST ----------
async def notify_wishlist_matches(bot: Bot):
    matches = db_query("""
        SELECT wt.user_id, p.id, p.title, p.price, p.discount, p.url, p.image, p.description
        FROM wishlist_titles wt
        JOIN users u ON u.user_id = wt.user_id
        JOIN products p ON LOWER(p.title) LIKE '%' || LOWER(wt.title) || '%' ESCAPE '\\'
        WHERE u.premium_until > ?
          AND p.discount IS NOT NULL AND p.discount != ''
          AND LENGTH(wt.title) >= 3
          AND NOT EXISTS (
              SELECT 1 FROM notified_wishlist nw
              WHERE nw.user_id = wt.user_id AND nw.product_id = p.id
          )
    """, (datetime.now().isoformat(),), fetch=True)
    if not matches:
        return

    for uid, pid, title, price, discount, url, image, desc in matches:
        text = (
            f"⭐ <b>Твоя игра из Wishlist в скидке!</b>\n\n"
            f"🎮 {title}\n💵 {price}\n🔥 {discount}\n\n"
            f"📝 <i>{safe_html_truncate(desc or '', 100)}...</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить", url=url)]
        ])
        photo = await download_image(image) if image else None
        try:
            if photo:
                await bot.send_photo(chat_id=uid, photo=BufferedInputFile(photo, filename="w.jpg"),
                                     caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            db_query("INSERT OR IGNORE INTO notified_wishlist (user_id, product_id) VALUES (?, ?)", (uid, pid))
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.debug(f"[WISHLIST] {uid}: {e}")


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
        (datetime.now().isoformat(),), fetch=True,
    )
    for pid, title, price, discount, url, image, description in super_deals:
        text = (
            f"🔥 <b>СУПЕР-СКИДКА {discount}!</b>\n\n"
            f"🎮 {title}\n💵 {price}\n\n"
            f"📝 <i>{safe_html_truncate(description or '', 100)}...</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
        photo = await download_image(image) if image else None
        for (uid,) in premium_users:
            try:
                if photo:
                    await bot.send_photo(chat_id=uid, photo=BufferedInputFile(photo, filename="deal.jpg"),
                                         caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                else:
                    await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                await asyncio.sleep(0.05)
            except Exception:
                pass
        db_query("INSERT OR IGNORE INTO notified_deals (product_id) VALUES (?)", (pid,))


# ---------- ROBOKASSA ----------
def generate_robo_link(user_id: int, amount: int, days: int) -> str:
    inv_id = int(datetime.now().timestamp())
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
        "WHERE status='pending' AND created_at > datetime('now','-1 day')",
        fetch=True,
    )
    for inv_id, user_id, amount, days in pending:
        # ✅ правильный формат: MerchantLogin:InvoiceID:Password#2
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
            if not out_sum or int(float(out_sum)) < amount:
                logger.warning(f"[ROBO] Сумма не совпадает: {out_sum} < {amount}")
                continue
            db_query("UPDATE payments SET status='confirmed' WHERE payment_id=?", (str(inv_id),))
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
                [KeyboardButton(text="📢 Реклама")],
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
                [KeyboardButton(text="⬅️ В меню")],
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


# ---------- СТАРТ ----------
@router.message(CommandStart())
async def start_cmd(message: Message):
    register_user(message)
    is_prem = check_premium(message.from_user.id)
    premium_text = "\n👑 У тебя есть Премиум-доступ!" if is_prem else ""
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "🐷 Я бот для выгодных покупок.\n"
        "🔥 Самые жирные скидки на Steam-игры.\n"
        "💳 Пополнение Steam и мобильных игр напрямую в кошелёк.\n"
        f"{premium_text}\n"
        "Выбирай раздел ниже 👇",
        reply_markup=main_menu_kb(),
    )


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
        days_left = (until - datetime.now()).days + 1
        text += f"👑 Статус: <b>Премиум</b> (до {until.strftime('%d.%m.%Y')})\n⏳ Осталось: <b>{days_left} дн.</b>\n"
    else:
        text += f"💎 Статус: <b>Обычный</b>\n"
    text += f"\n⭐ <b>Wishlist:</b> {w_count} / {w_limit} игр\n"

    buttons = [[InlineKeyboardButton(text="⭐ Мой Wishlist", callback_data="wl_show")]]
    if w_count < w_limit:
        buttons.append([InlineKeyboardButton(text="➕ Добавить в Wishlist", callback_data="wl_add_start")])
    if not is_prem:
        buttons.append([InlineKeyboardButton(text="💎 Купить Премиум (до 10 игр)", callback_data="premium_show")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ---------- СКИДКИ ----------
def _fetch_products_page():
    return db_query(
        "SELECT id, title, price, old_price, discount, url, image, description, price_usd "
        "FROM products WHERE image IS NOT NULL AND image != '' ORDER BY id DESC LIMIT ?",
        (PAGE_LIMIT,), fetch=True,
    )


@router.message(F.text == "🔥 Скидки")
async def discounts_menu(message: Message):
    products = _fetch_products_page()
    if not products:
        await message.answer("Скидок пока нет. Админ скоро их добавит! 🙂")
        return
    await send_product_card(message, products, 0)


async def send_product_card(message_or_callback, products, index):
    pid, title, price, old_price, discount, url, image, description, price_usd = products[index]
    display_usd = f" (в долларах: {price_usd})" if (price_usd and "руб" not in price_usd.lower()) else ""
    text = f"🎮 <b>{title}</b>\n\n"
    if old_price:
        text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price:
        text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount:
        text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description:
        text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
    text += f"\n📄 Игра {index + 1} из {len(products)}"

    nav_row = []
    if index > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{index - 1}"))
    if index < len(products) - 1:
        nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{index + 1}"))
    buttons = []
    if nav_row:
        buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🛒 Купить в Steam", url=url)])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    photo_bytes = await download_image(image) if image else None

    if not isinstance(message_or_callback, CallbackQuery):
        if photo_bytes:
            await message_or_callback.answer_photo(
                photo=BufferedInputFile(photo_bytes, filename="game.jpg"),
                caption=text, reply_markup=kb, parse_mode=ParseMode.HTML,
            )
        else:
            await message_or_callback.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return

    cb = message_or_callback
    try:
        await cb.answer()
    except Exception:
        pass
    msg = cb.message
    try:
        if photo_bytes and msg.photo:
            await msg.edit_media(
                media=InputMediaPhoto(
                    media=BufferedInputFile(photo_bytes, filename="game.jpg"),
                    caption=text, parse_mode=ParseMode.HTML,
                ),
                reply_markup=kb,
            )
            return
        if not photo_bytes and not msg.photo:
            await msg.edit_text(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        logger.warning(f"[PAGINATION] {e}")
    try:
        if photo_bytes:
            await cb.bot.send_photo(
                chat_id=msg.chat.id,
                photo=BufferedInputFile(photo_bytes, filename="game.jpg"),
                caption=text, reply_markup=kb, parse_mode=ParseMode.HTML,
            )
        else:
            await cb.bot.send_message(
                chat_id=msg.chat.id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML,
            )
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        logger.error(f"[PAGINATION] {e}")


@router.callback_query(F.data.startswith("page_"))
async def paginate_products(cb: CallbackQuery):
    try:
        index = int(cb.data.split("_", 1)[1])
    except (IndexError, ValueError):
        await cb.answer("Ошибка", show_alert=False)
        return
    products = _fetch_products_page()
    if 0 <= index < len(products):
        await send_product_card(cb, products, index)
    else:
        await cb.answer("Дальше игр нет", show_alert=False)


# ---------- СЛУЧАЙНАЯ ИГРА ----------
@router.message(F.text == "🎲 Случайная игра")
async def random_game(message: Message):
    user_id = message.from_user.id
    is_prem = check_premium(user_id)

    if not is_prem:
        res = db_query("SELECT last_roulette FROM users WHERE user_id = ?", (user_id,), fetchone=True)
        if res and res[0]:
            try:
                last_time = datetime.fromisoformat(res[0])
                if datetime.now() - last_time < timedelta(days=1):
                    time_left = timedelta(days=1) - (datetime.now() - last_time)
                    h, m = time_left.seconds // 3600, (time_left.seconds % 3600) // 60
                    await message.answer(
                        f"⏳ <b>Лимит рулетки!</b>\n\nОбычные пользователи — 1 раз в день.\n"
                        f"Следующая попытка через: <b>{h} ч. {m} мин.</b>\n\n"
                        f"💎 <b>Купи Премиум</b> — крути без ограничений!",
                    )
                    return
            except (ValueError, TypeError):
                pass

    if is_prem:
        candidates = db_query(
            "SELECT title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE discount IS NOT NULL AND discount != '' "
            "AND image IS NOT NULL AND image != '' "
            "ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 20",
            fetch=True,
        )
        game = random.choice(candidates) if candidates else db_query(
            "SELECT title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1",
            fetchone=True,
        )
    else:
        game = db_query(
            "SELECT title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1",
            fetchone=True,
        )

    if not game:
        await message.answer("В базе пока нет игр.")
        return
    db_query("UPDATE users SET last_roulette = ? WHERE user_id = ?", (datetime.now().isoformat(), user_id))

    title, price, old_price, discount, url, image, description, price_usd = game
    display_usd = f" (в долларах: {price_usd})" if (price_usd and "руб" not in price_usd.lower()) else ""
    text = f"🎲 <b>Тебе выпала: {title}</b>\n\n"
    if old_price:
        text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price:
        text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount:
        text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description:
        text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
    if is_prem:
        text += f"\n👑 <i>Бонус Премиум: максимальная скидка!</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]])
    photo_bytes = await download_image(image) if image else None
    if photo_bytes:
        await message.answer_photo(photo=BufferedInputFile(photo_bytes, filename="game.jpg"),
                                   caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await message.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)


@router.callback_query(F.data == "random_again")
async def random_again(cb: CallbackQuery):
    uid = cb.from_user.id
    is_prem = check_premium(uid)

    if not is_prem:
        res = db_query("SELECT last_random_again FROM users WHERE user_id = ?", (uid,), fetchone=True)
        if res and res[0]:
            try:
                last = datetime.fromisoformat(res[0])
                if datetime.now() - last < timedelta(minutes=10):
                    await cb.answer("⏳ Раз в 10 минут. Купи Премиум — без лимита!", show_alert=True)
                    return
            except (ValueError, TypeError):
                pass
        db_query("UPDATE users SET last_random_again = ? WHERE user_id = ?",
                 (datetime.now().isoformat(), uid))

    if is_prem:
        game = db_query(
            "SELECT title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE discount IS NOT NULL AND discount != '' "
            "AND image IS NOT NULL AND image != '' "
            "ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 1",
            fetchone=True,
        )
    else:
        game = db_query(
            "SELECT title, price, old_price, discount, url, image, description, price_usd "
            "FROM products WHERE image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1",
            fetchone=True,
        )
    await cb.answer()
    if not game:
        await cb.answer("В базе пока пусто", show_alert=True)
        return

    title, price, old_price, discount, url, image, description, price_usd = game
    text = f"🎲 <b>Тебе выпала: {title}</b>\n\n"
    if old_price:
        text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price:
        text += f"💵 Цена: <b>{price}</b>\n"
    if discount:
        text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description:
        text += f"\n📝 <i>{safe_html_truncate(description, 150)}...</i>\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]
    ])
    photo = await download_image(image) if image else None
    if photo:
        await cb.message.answer_photo(
            photo=BufferedInputFile(photo, filename="game.jpg"),
            caption=text, reply_markup=kb, parse_mode=ParseMode.HTML,
        )
    else:
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
    raw = (message.text or "").strip()
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
        await message.answer(f"⚠️ <b>{raw}</b> уже есть в Wishlist.", reply_markup=main_menu_kb())
        return
    db_query("INSERT OR IGNORE INTO wishlist_titles (user_id, title) VALUES (?, ?)", (uid, raw))
    await state.clear()
    await message.answer(
        f"✅ <b>{raw}</b> добавлено в Wishlist!\nСейчас у тебя: {count + 1} / {limit} игр.",
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
        text += f"• {title}\n"
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить: {title[:25]}", callback_data=f"wl_del_{wid}")])
    if not check_premium(uid):
        text += f"\n💎 Хочешь до 10 игр + уведомления о скидках? Купи Премиум!"
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
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")]
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
        f"👤 Логин: <b>{raw}</b>\n\n"
        "Шаг 2 из 2. Введи <b>сумму пополнения в рублях</b> (только цифры).\n"
        "Например: <i>500</i>\n\n"
        "<i>Напиши «отмена», чтобы выйти.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")]
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
        f"👤 Логин: <b>{login}</b>\n"
        f"💵 Сумма: <b>{amount} ₽</b>\n\n"
        f"1️⃣ Нажми кнопку ниже — откроется страница товара.\n"
        f"2️⃣ Вставь свой логин <b>{login}</b> в поле «Ваш логин Steam».\n"
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
@router.message(F.text == "💎 Премиум")
async def premium_menu(message: Message):
    is_prem = check_premium(message.from_user.id)
    status = ""
    if is_prem:
        until = get_premium_until(message.from_user.id)
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


@router.callback_query(F.data == "premium_show")
async def premium_show(cb: CallbackQuery):
    await cb.answer()
    if cb.message:
        await premium_menu(cb.message)


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
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=payment_url)]
        ])
        try:
            await cb.message.edit_text(
                f"💳 <b>Оплата через Robokassa</b>\n\n"
                f"Срок: <b>{days} дн.</b> за <b>{amount} ₽</b>\n\n"
                f"Нажми кнопку ниже. Премиум активируется автоматически (проверка занимает до 1-2 минут).",
                reply_markup=kb,
            )
        except TelegramBadRequest:
            await cb.message.answer(
                f"💳 <b>Оплата через Robokassa</b>\n\n"
                f"Срок: <b>{days} дн.</b> за <b>{amount} ₽</b>\n\n"
                f"Нажми кнопку ниже.",
                reply_markup=kb,
            )
    except Exception as e:
        logger.error(f"Robokassa error: {e}")
        await cb.answer("❌ Ошибка создания платежа", show_alert=True)


# ---------- РЕКЛАМА ----------
@router.message(F.text == "📢 Реклама")
async def ads_info(message: Message):
    await message.answer(
        "📢 <b>Реклама в нашем боте</b>\n\n"
        "🎯 Пост в рассылке, закреп, упоминание в «Скидке дня».\n\n"
        "Пиши: 👉 <b>@ObuzaMeduza</b>",
        reply_markup=main_menu_kb(),
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
    rows = db_query("SELECT id, user_id, amount, days, status FROM payments ORDER BY id DESC LIMIT 10", fetch=True)
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
            try:
                days_left = (datetime.fromisoformat(until) - datetime.now()).days
                text_prem += f"  • <code>{uid}</code> — {days_left} дн.\n"
            except Exception:
                pass
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
        try:
            dt = datetime.fromisoformat(until)
            days_left = (dt - datetime.now()).days
            uname_str = f"@{uname}" if uname else (fname or "—")
            text += f"• <code>{uid}</code> | {uname_str} | до {dt.strftime('%d.%m.%Y')} ({days_left} дн.)\n"
        except (ValueError, TypeError):
            continue
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
        "🎁 <b>Выдача Премиума</b>\n\n"
        "Введи <b>user_id</b> пользователя (число).\n"
        "Можно узнать в его профиле или в статистике.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")]
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
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")]
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
        f"👤 Найден: <b>{fname or '—'}</b> (@{uname or '—'}, <code>{target}</code>)\n\n"
        f"На сколько <b>дней</b> выдать премиум? (например, 30)\n"
        f"Если у него уже есть премиум — дни добавятся сверху.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")]
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
                f"🎉 <b>Тебе активирован Премиум на {days} дней!</b>\n\n"
                f"Спасибо, что с нами 💎",
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


# ---------- ЗАПУСК ----------
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(fetch_steam_deals, "interval", hours=6)
    scheduler.add_job(check_robokassa_payments, "interval", minutes=1, args=[bot])
    scheduler.add_job(send_daily_deal, "cron", hour=12, minute=0, args=[bot])
    scheduler.add_job(send_daily_deal, "cron", hour=19, minute=0, args=[bot])
    scheduler.add_job(notify_premium_super_deals, "interval", hours=3, args=[bot])
    scheduler.add_job(notify_wishlist_matches, "interval", minutes=30, args=[bot])
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен! Robokassa + ежедневная рулетка.")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if _db_conn:
            _db_conn.close()


if __name__ == "__main__":
    asyncio.run(main())
