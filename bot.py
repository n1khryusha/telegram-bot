import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import threading
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
    LabeledPrice, PreCheckoutQuery, SuccessfulPayment
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

# ============ НАСТРОЙКИ ============
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
LOG_PATH = os.getenv("LOG_PATH", "/data/bot.log")

# --- PLATI.MARKET ---
PLATI_AGENT_ID = os.getenv("PLATI_AGENT_ID", "1480092")
PLATI_SUPPORT = os.getenv("PLATI_SUPPORT", "@ObuzaMeduza")
STEAM_TOPUP_ITEM_ID = os.getenv("STEAM_TOPUP_ITEM_ID", "3961593")

# --- РЕФЕРАЛЬНАЯ СИСТЕМА ---
REF_BONUS_DAYS = 3  # <--- ИЗМЕНЕНО НА 3 ДНЯ

# --- КОНТАКТЫ ---
CONTACT_TG = "@ObuzaMeduza"
CONTACT_EMAIL = "nikhrushawork@gmail.com"

# --- ДОНАТ (DA LINK) ---
DONATE_URL = "https://dalink.to/beaconkeys"

# --- ЦЕНЫ В ЗВЕЗДАХ ---
STARS_7_DAYS = 150  # 150 звезд за 7 дней
STARS_30_DAYS = 350 # 350 звезд за 30 дней
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

router = Router()
PAGE_LIMIT = 50
WISHLIST_LIMIT_FREE = 1
WISHLIST_LIMIT_PREMIUM = 10

# ---------- КЭШ КАРТИНОК ----------
IMAGE_CACHE: "OrderedDict[str, bytes]" = OrderedDict()
IMAGE_CACHE_MAX_BYTES = 10 * 1024 * 1024
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
        last_random_again TEXT,
        referred_by INTEGER,
        ref_bonus_claimed INTEGER DEFAULT 0
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
    CREATE TABLE IF NOT EXISTS promocodes (
        code TEXT PRIMARY KEY,
        days INTEGER NOT NULL,
        max_uses INTEGER DEFAULT 1,
        uses_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS promocode_uses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT,
        user_id INTEGER,
        used_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(code, user_id)
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
    CREATE INDEX IF NOT EXISTS idx_products_discount ON products(discount);
    CREATE INDEX IF NOT EXISTS idx_users_premium ON users(premium_until);
    """)
    # Миграции
    cur.execute("PRAGMA table_info(users)")
    cols = {row[1] for row in cur.fetchall()}
    if "referred_by" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    if "ref_bonus_claimed" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN ref_bonus_claimed INTEGER DEFAULT 0")
    
    cur.execute("PRAGMA table_info(products)")
    cols = {row[1] for row in cur.fetchall()}
    if "tg_file_id" not in cols:
        cur.execute("ALTER TABLE products ADD COLUMN tg_file_id TEXT")
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
        _db_conn.commit()
        return result

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

def check_premium(user_id: int) -> bool:
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]:
        return False
    dt = parse_dt(res[0])
    return bool(dt and dt > utcnow())

def get_premium_until(user_id: int):
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]:
        return None
    return parse_dt(res[0])

def add_premium_days(user_id: int, days: int):
    current = get_premium_until(user_id)
    now = utcnow()
    new_expiry = (current + timedelta(days=days)) if (current and current > now) else (now + timedelta(days=days))
    db_query("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_expiry.isoformat(), user_id))
    logger.info(f"[PREMIUM] +{days} дней пользователю {user_id}. Новая дата: {new_expiry}")

def wishlist_limit(user_id: int) -> int:
    return WISHLIST_LIMIT_PREMIUM if check_premium(user_id) else WISHLIST_LIMIT_FREE

def wishlist_count(user_id: int) -> int:
    return db_query("SELECT COUNT(*) FROM wishlist_titles WHERE user_id = ?", (user_id,), fetchone=True)[0]

def list_premium_users() -> list:
    return db_query(
        "SELECT user_id, username, first_name, premium_until FROM users WHERE premium_until > ?",
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
    if not url: return None
    if url in IMAGE_CACHE:
        IMAGE_CACHE.move_to_end(url)
        return IMAGE_CACHE[url]
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://store.steampowered.com/"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200: return None
                data = await resp.read()
                if not data or len(data) < 100: return None
                while _image_cache_size + len(data) > IMAGE_CACHE_MAX_BYTES and IMAGE_CACHE:
                    _, old = IMAGE_CACHE.popitem(last=False)
                    _image_cache_size -= len(old)
                IMAGE_CACHE[url] = data
                _image_cache_size += len(data)
                return data
    except Exception as e:
        logger.warning(f"[IMG] {e}")
        return None

async def send_game_photo(bot: Bot, chat_id: int, product_id: int | None, image_url: str | None, caption: str, reply_markup) -> bool:
    tg_file_id = None
    if product_id:
        row = db_query("SELECT tg_file_id FROM products WHERE id = ?", (product_id,), fetchone=True)
        if row and row[0]: tg_file_id = row[0]
    if tg_file_id:
        try:
            await bot.send_photo(chat_id=chat_id, photo=tg_file_id, caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
            return True
        except TelegramBadRequest:
            if product_id: db_query("UPDATE products SET tg_file_id = NULL WHERE id = ?", (product_id,))
    if not image_url: return False
    data = await download_image(image_url)
    if not data: return False
    try:
        msg = await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(data, filename="game.jpg"), caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        if product_id and msg.photo:
            db_query("UPDATE products SET tg_file_id = ? WHERE id = ?", (msg.photo[-1].file_id, product_id))
        return True
    except Exception as e:
        logger.warning(f"[PHOTO] {e}")
        return False

# ---------- ПАРСЕР STEAM (УМНЫЙ) ----------
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
    except Exception: pass
    return result

def _format_price(price_overview) -> tuple[str, str, str]:
    if not price_overview: return "", "", ""
    currency = price_overview.get("currency", "")
    final = price_overview.get("final", 0) / 100
    initial = price_overview.get("initial", 0) / 100
    discount = price_overview.get("discount_percent", 0)
    
    if currency == "RUB":
        final_str = f"{final:.0f} ₽"
        original_str = f"{initial:.0f} ₽" if discount else ""
        discount_str = f"-{discount}%" if discount else ""
    elif currency == "USD":
        final_str = f"${final:.2f}"
        original_str = f"${initial:.2f}" if discount else ""
        discount_str = f"-{discount}%" if discount else ""
    else:
        final_str = f"{final:.2f} {currency}"
        original_str = f"{initial:.2f} {currency}" if discount else ""
        discount_str = f"-{discount}%" if discount else ""
    return final_str, original_str, discount_str

async def fetch_steam_deals():
    url = "https://store.steampowered.com/api/featuredcategories?cc=ru&l=russian"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200: return
                data = await resp.json()
    except Exception as e:
        logger.warning(f"[PARSER] {e}")
        return
    
    added = 0
    async with aiohttp.ClientSession() as session:
        for item in data.get("specials", {}).get("items", []):
            title, app_id = item.get("name"), item.get("id")
            if not title or not app_id: continue
            
            det = await _steam_get_details(session, app_id)
            if not det["price_overview"] or det["price_overview"].get("discount_percent", 0) == 0:
                continue
            
            if db_query("SELECT 1 FROM products WHERE title = ?", (title,), fetchone=True): continue
            
            shop_url = f"https://store.steampowered.com/app/{app_id}/"
            f_str, o_str, d_str = _format_price(det["price_overview"])
            price_usd = det["price_overview"].get("final_formatted", "") if det["price_overview"] else ""
            
            db_query("INSERT OR IGNORE INTO products (title, price, old_price, discount, url, image, description, price_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, f_str or "Бесплатно", o_str, d_str, shop_url, det["image"], det["description"], price_usd))
            added += 1
            await asyncio.sleep(0.2)

        for section_key in ("top_sellers", "new_releases"):
            for item in data.get(section_key, {}).get("items", []):
                title, app_id = item.get("name"), item.get("id")
                if not title or not app_id: continue
                
                det = await _steam_get_details(session, app_id)
                if not det["price_overview"] or det["price_overview"].get("discount_percent", 0) == 0:
                    continue
                
                if db_query("SELECT 1 FROM products WHERE title = ?", (title,), fetchone=True): continue
                
                shop_url = f"https://store.steampowered.com/app/{app_id}/"
                f_str, o_str, d_str = _format_price(det["price_overview"])
                price_usd = det["price_overview"].get("final_formatted", "") if det["price_overview"] else ""
                
                db_query("INSERT OR IGNORE INTO products (title, price, old_price, discount, url, image, description, price_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, f_str or "Бесплатно", o_str, d_str, shop_url, det["image"], det["description"], price_usd))
                added += 1
                await asyncio.sleep(0.2)
    logger.info(f"[PARSER] Добавлено новых игр со скидками: {added}")

# ---------- ЕЖЕДНЕВНАЯ СКИДКА ----------
async def send_daily_deal(bot: Bot):
    users = [row[0] for row in db_query("SELECT user_id FROM users", fetch=True)]
    if not users: return
    pool = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL AND image != '' LIMIT 500", fetch=True)
    if not pool: return
    sent, blocked = 0, 0
    for uid in users:
        pid, title, price, old_price, discount, url, image, description, price_usd = random.choice(pool)
        text = f"🎲 <b>Скидка дня: {html.escape(title)}</b>\n\n"
        if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
        if price: text += f"💵 Цена: <b>{price}</b>\n"
        if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
        if description: text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)], [InlineKeyboardButton(text="🎲 Ещё случайная", callback_data="random_again")]])
        ok = await send_game_photo(bot, uid, pid, image, text, kb)
        if not ok:
            try:
                await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                sent += 1
            except TelegramBadRequest as e:
                if "blocked" in str(e).lower() or "chat not found" in str(e).lower(): db_query("DELETE FROM users WHERE user_id = ?", (uid,))
                blocked += 1
            except Exception: blocked += 1
        else: sent += 1
        await asyncio.sleep(0.07)
    logger.info(f"[DAILY] Отправлено: {sent}, ошибок: {blocked}")

# ---------- УВЕДОМЛЕНИЯ WISHLIST ----------
async def notify_wishlist_matches(bot: Bot):
    rows = db_query("SELECT wt.user_id, wt.title FROM wishlist_titles wt JOIN users u ON u.user_id = wt.user_id WHERE u.premium_until > ? AND LENGTH(wt.title) >= 3", (utcnow_iso(),), fetch=True)
    if not rows: return
    for uid, wtitle in rows:
        pattern = "%" + escape_like(wtitle) + "%"
        matches = db_query("SELECT p.id, p.title, p.price, p.discount, p.url, p.image, p.description FROM products p WHERE LOWER(p.title) LIKE LOWER(?) ESCAPE '\\' AND p.discount IS NOT NULL AND p.discount != '' AND NOT EXISTS (SELECT 1 FROM notified_wishlist nw WHERE nw.user_id = ? AND nw.product_id = p.id) LIMIT 5", (pattern, uid), fetch=True)
        for pid, title, price, discount, url, image, desc in matches:
            text = f"⭐ <b>Твоя игра из Wishlist в скидке!</b>\n\n🎮 {html.escape(title)}\n💵 {price}\n🔥 {discount}\n\n📝 <i>{safe_html_truncate(desc or '', 100)}...</i>"
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
            ok = await send_game_photo(bot, uid, pid, image, text, kb)
            if not ok:
                try: await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                except Exception: continue
            db_query("INSERT OR IGNORE INTO notified_wishlist (user_id, product_id) VALUES (?, ?)", (uid, pid))
            await asyncio.sleep(0.05)

# ---------- УВЕДОМЛЕНИЯ О СУПЕР-СКИДКАХ ----------
async def notify_premium_super_deals(bot: Bot):
    super_deals = db_query("SELECT p.id, p.title, p.price, p.discount, p.url, p.image, p.description FROM products p LEFT JOIN notified_deals n ON n.product_id = p.id WHERE CAST(REPLACE(p.discount, '-', '') AS INTEGER) >= 80 AND p.discount IS NOT NULL AND p.discount != '' AND n.product_id IS NULL", fetch=True)
    if not super_deals: return
    premium_users = db_query("SELECT user_id FROM users WHERE premium_until > ?", (utcnow_iso(),), fetch=True)
    for pid, title, price, discount, url, image, description in super_deals:
        text = f"🔥 <b>СУПЕР-СКИДКА {discount}!</b>\n\n🎮 {html.escape(title)}\n💵 {price}\n\n📝 <i>{safe_html_truncate(description or '', 100)}...</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
        for (uid,) in premium_users:
            ok = await send_game_photo(bot, uid, pid, image, text, kb)
            if not ok:
                try: await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                except Exception: pass
            await asyncio.sleep(0.05)
        db_query("INSERT OR IGNORE INTO notified_deals (product_id, notified_at) VALUES (?, ?)", (pid, utcnow_iso()))

# ---------- КЛАВИАТУРЫ ----------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔥 Скидки"), KeyboardButton(text="🎲 Случайная игра")],
            [KeyboardButton(text="💳 Пополнить Steam"), KeyboardButton(text="📱 Мобильные игры")],
            [KeyboardButton(text="💎 Премиум"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🎟 Промокод"), KeyboardButton(text="❤️ Поддержать")],
            [KeyboardButton(text="📢 Реклама"), KeyboardButton(text="🆘 Поддержка")],
        ],
        resize_keyboard=True,
    )

def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔄 Спарсить скидки"), KeyboardButton(text="🗑 Очистить базу")],
            [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="👥 Рефералы")],
            [KeyboardButton(text="🎁 Выдать премиум"), KeyboardButton(text="🚫 Снять премиум")],
            [KeyboardButton(text="🎟 Промокоды"), KeyboardButton(text="📢 Рассылка")],
            [KeyboardButton(text="⬅️ В меню")],
        ],
        resize_keyboard=True,
    )

def promo_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="➕ Создать промокод"), KeyboardButton(text="📋 Список промокодов")],
            [KeyboardButton(text="⬅️ Назад в админку")],
        ],
        resize_keyboard=True,
    )

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

class PromoFSM(StatesGroup):
    waiting_code = State()
    waiting_days = State()
    waiting_max_uses = State()

class UserPromoFSM(StatesGroup):
    waiting_code = State()

# ---------- ERROR HANDLER ----------
async def errors_handler(event, exception: Exception, bot: Bot):
    logger.exception(f"[ERROR] {type(exception).__name__}: {exception}")
    return True

# ---------- СТАРТ ----------
@router.message(CommandStart())
async def start_cmd(message: Message):
    text = message.text or ""
    referrer_id = None
    
    match = re.search(r"ref_(\d+)", text)
    if match:
        referrer_id = int(match.group(1))
        logger.info(f"[START] Referrer ID from text: {referrer_id}")

    logger.info(f"[START] User: {message.from_user.id}, Text: {text}")

    existing = db_query("SELECT user_id, referred_by FROM users WHERE user_id = ?", (message.from_user.id,), fetchone=True)
    is_new = existing is None

    if is_new:
        db_query(
            "INSERT INTO users (user_id, username, first_name, referred_by) VALUES (?, ?, ?, ?)",
            (message.from_user.id, message.from_user.username, message.from_user.first_name, referrer_id)
        )
        logger.info(f"[START] New user created: {message.from_user.id}, referrer: {referrer_id}")
        
        if referrer_id and referrer_id != message.from_user.id:
            ref_row = db_query("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,), fetchone=True)
            if ref_row:
                add_premium_days(referrer_id, REF_BONUS_DAYS)
                db_query("UPDATE users SET ref_bonus_claimed = 1 WHERE user_id = ?", (message.from_user.id,))
                try:
                    await message.bot.send_message(referrer_id, f"🎉 <b>По твоей ссылке пришел друг!</b>\n\nТебе начислено <b>+{REF_BONUS_DAYS} дней Премиума</b>! 👑")
                except Exception as e:
                    logger.warning(f"[REF] Не удалось отправить сообщение рефереру: {e}")
                await message.answer(f"✅ Ты пришел по реферальной ссылке! Твой друг получил <b>+{REF_BONUS_DAYS} дней Премиума</b>.")
            else:
                logger.warning(f"[REF] Referrer {referrer_id} not found in DB")
    else:
        db_query("UPDATE users SET username=?, first_name=? WHERE user_id=?", (message.from_user.username, message.from_user.first_name, message.from_user.id))

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

# ---------- ПРОФИЛЬ ----------
@router.message(F.text == "👤 Профиль")
async def profile_menu(message: Message):
    uid = message.from_user.id
    is_prem = check_premium(uid)
    until = get_premium_until(uid)
    joined = db_query("SELECT joined_at FROM users WHERE user_id = ?", (uid,), fetchone=True)
    w_count, w_limit = wishlist_count(uid), wishlist_limit(uid)
    
    refs = db_query(
        "SELECT username, first_name FROM users WHERE referred_by = ? ORDER BY joined_at DESC LIMIT 20",
        (uid,), fetch=True
    )
    ref_count = len(refs)
    
    text = f"👤 <b>Твой профиль</b>\n\n🆔 ID: <code>{uid}</code>\n"
    if joined and joined[0]: text += f"📅 С нами с: {joined[0][:10]}\n"
    text += "\n"
    if is_prem and until:
        days_left = (until - utcnow()).days + 1
        text += f"👑 Статус: <b>Премиум</b> (до {until.strftime('%d.%m.%Y')})\n⏳ Осталось: <b>{days_left} дн.</b>\n"
    else:
        text += "💎 Статус: <b>Обычный</b>\n"
    text += f"\n⭐ <b>Wishlist:</b> {w_count} / {w_limit} игр\n"
    text += f"\n👥 <b>Приглашено друзей:</b> {ref_count}\n"
    
    if refs:
        text += "<b>Твои рефералы:</b>\n"
        for uname, fname in refs:
            name = f"@{uname}" if uname else (fname or "—")
            text += f"• {html.escape(name)}\n"
        if ref_count == 20:
            text += "... (показаны последние 20)\n"

    buttons = [
        [InlineKeyboardButton(text=f"🎁 Пригласить друга (+{REF_BONUS_DAYS} дней)", callback_data="show_ref_link")],
        [InlineKeyboardButton(text="⭐ Мой Wishlist", callback_data="wl_show")],
        [InlineKeyboardButton(text="❤️ Поддержать проект", url=DONATE_URL)],
    ]
    if w_count < w_limit:
        buttons.append([InlineKeyboardButton(text="➕ Добавить в Wishlist", callback_data="wl_add_start")])
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ---------- ПРЕМИУМ (С ОПЛАТОЙ ЗВЕЗДАМИ) ----------
async def premium_menu(message: Message, user_id: int | None = None):
    uid = user_id if user_id is not None else message.from_user.id
    is_prem = check_premium(uid)
    until = get_premium_until(uid)
    ref_count = db_query("SELECT COUNT(*) FROM users WHERE referred_by = ? AND ref_bonus_claimed = 1", (uid,), fetchone=True)[0]
    
    status = ""
    if is_prem and until:
        status = f"👑 <b>Твой Премиум активен до {until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"

    text = (
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
        "🎁 <b>Как получить Премиум БЕСПЛАТНО?</b>\n"
        f"Пригласи друга по своей ссылке и получи <b>+{REF_BONUS_DAYS} дней Премиума</b> за каждого!\n\n"
        f"👥 Ты уже пригласил: <b>{ref_count} чел.</b>\n"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔥 7 дней — {STARS_7_DAYS} ⭐", callback_data="buy_premium_7")],
        [InlineKeyboardButton(text=f"👑 30 дней — {STARS_30_DAYS} ⭐", callback_data="buy_premium_30")],
    ])
    
    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)

@router.message(F.text == "💎 Премиум")
async def premium_menu_msg(message: Message):
    await premium_menu(message)

@router.callback_query(F.data == "premium_show")
async def premium_show(cb: CallbackQuery):
    await cb.answer()
    if cb.message: await premium_menu(cb.message, user_id=cb.from_user.id)

# ---------- ОПЛАТА ЗВЕЗДАМИ (TELEGRAM STARS) ----------
@router.callback_query(F.data.startswith("buy_premium_"))
async def buy_premium_stars(cb: CallbackQuery):
    days = 7 if cb.data == "buy_premium_7" else 30
    price = STARS_7_DAYS if days == 7 else STARS_30_DAYS
    
    # Формируем счет
    prices = [LabeledPrice(label=f"Премиум на {days} дней", amount=price)]
    
    try:
        await cb.bot.send_invoice(
            chat_id=cb.from_user.id,
            title=f"Премиум-доступ на {days} дней",
            description=f"Активация Премиум-доступа в боте BeaconKeys на {days} дней.",
            payload=f"premium_{days}",
            provider_token="",  # Для Stars provider_token пустой!
            currency="XTR",     # XTR - это код валюты Telegram Stars
            prices=prices,
            start_parameter="premium",
        )
        await cb.answer("Счет выставлен в чате!")
    except Exception as e:
        logger.error(f"Ошибка создания счета: {e}")
        await cb.answer("❌ Ошибка создания счета. Попробуйте позже.", show_alert=True)

# ---------- ПРОВЕРКА ПЕРЕД ОПЛАТОЙ ----------
@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery):
    # Подтверждаем готовность принять платеж
    await pre_checkout_q.answer(ok=True)

# ---------- УСПЕШНАЯ ОПЛАТА ----------
@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment_info: SuccessfulPayment = message.successful_payment
    payload = payment_info.invoice_payload  # Например, "premium_7"
    
    try:
        days = int(payload.split("_")[1])
    except (IndexError, ValueError):
        days = 7 # По умолчанию

    add_premium_days(message.from_user.id, days)
    
    await message.answer(
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"Вам начислено <b>+{days} дней Премиума</b>! 👑\n\n"
        f"Спасибо за поддержку! 🙏",
        reply_markup=main_menu_kb()
    )
    logger.info(f"[STARS] Пользователь {message.from_user.id} оплатил Премиум на {days} дней. Сумма: {payment_info.total_amount} XTR")

# ---------- ССЫЛКА НА ДРУГА ----------
@router.callback_query(F.data == "show_ref_link")
async def show_ref_link(cb: CallbackQuery):
    uid = cb.from_user.id
    bot_info = await cb.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{uid}"
    
    await cb.message.answer(
        f"🔗 <b>Твоя реферальная ссылка:</b>\n\n"
        f"<code>{ref_link}</code>\n\n"
        f"<i>Нажми на ссылку, чтобы скопировать её, и отправь другу.</i>\n\n"
        f"За каждого друга, который зайдет по ней, ты получишь <b>+{REF_BONUS_DAYS} дней Премиума</b>!",
        parse_mode=ParseMode.HTML
    )
    await cb.answer()

# ---------- ВВОД ПРОМОКОДА (ЮЗЕР) ----------
@router.message(F.text == "🎟 Промокод")
async def user_promo_start(message: Message, state: FSMContext):
    await state.set_state(UserPromoFSM.waiting_code)
    await message.answer(
        "🎟 <b>Введите промокод</b>\n\n"
        "Отправьте сообщение с вашим промокодом, чтобы получить Премиум.\n\n"
        "<i>Напишите «отмена» или нажмите кнопку ниже, чтобы выйти.</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True)
    )

@router.message(UserPromoFSM.waiting_code, F.text.in_(["⬅️ Отмена", "отмена", "Отмена"]))
async def user_promo_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Ввод промокода отменен.", reply_markup=main_menu_kb())

@router.message(UserPromoFSM.waiting_code)
async def user_promo_check(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    
    if code in ("ОТМЕНА", "CANCEL", "/CANCEL"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_menu_kb())
        return
    
    if not code:
        await message.answer("⚠️ Введите код.")
        return

    promo = db_query("SELECT days, max_uses, uses_count FROM promocodes WHERE code = ?", (code,), fetchone=True)
    if not promo:
        await message.answer("❌ Такого промокода не существует. Попробуйте еще раз или нажмите «Отмена».")
        return
    
    days, max_uses, uses_count = promo
    if uses_count >= max_uses:
        await message.answer("❌ Этот промокод больше не действителен (лимит использований исчерпан).")
        return
    
    already = db_query("SELECT 1 FROM promocode_uses WHERE code = ? AND user_id = ?", (code, message.from_user.id), fetchone=True)
    if already:
        await message.answer("❌ Вы уже использовали этот промокод.")
        return

    db_query("UPDATE promocodes SET uses_count = uses_count + 1 WHERE code = ?", (code,))
    db_query("INSERT INTO promocode_uses (code, user_id) VALUES (?, ?)", (code, message.from_user.id))
    add_premium_days(message.from_user.id, days)
    
    await state.clear()
    await message.answer(
        f"✅ <b>Промокод активирован!</b>\n\n"
        f"Вам начислено <b>+{days} дней Премиума</b>! 👑",
        reply_markup=main_menu_kb()
    )

# ---------- СКИДКИ ----------
@router.message(F.text == "🔥 Скидки")
async def discounts_menu(message: Message):
    products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL AND image != '' ORDER BY id DESC LIMIT ?", (PAGE_LIMIT,), fetch=True)
    if not products:
        await message.answer("Скидок пока нет. Админ скоро их добавит! 🙂")
        return
    total = db_query("SELECT COUNT(*) FROM products WHERE image IS NOT NULL AND image != ''", fetchone=True)[0]
    await send_product_card(message, products, 0, 0, total)

async def send_product_card(message_or_callback, products, index: int, offset: int, total: int):
    pid, title, price, old_price, discount, url, image, description, price_usd = products[index]
    display_usd = f" (в долларах: {price_usd})" if (price_usd and "руб" not in price_usd.lower() and "₽" not in price_usd) else ""
    text = f"🎮 <b>{html.escape(title)}</b>\n\n"
    if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price: text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description: text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
    global_index = offset + index + 1
    text += f"\n📄 Игра {global_index} из {total}"
    nav_row = []
    if global_index > 1: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{global_index - 2}"))
    if global_index < total: nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{global_index}"))
    buttons = []
    if nav_row: buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🛒 Купить в Steam", url=url)])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    if not isinstance(message_or_callback, CallbackQuery):
        ok = await send_game_photo(message_or_callback.bot, message_or_callback.chat.id, pid, image, text, kb)
        if not ok: await message_or_callback.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    cb = message_or_callback
    try: await cb.answer()
    except Exception: pass
    msg = cb.message
    if msg is None: return
    try:
        if msg.photo:
            row = db_query("SELECT tg_file_id FROM products WHERE id = ?", (pid,), fetchone=True)
            media = row[0] if row and row[0] else None
            if media is None:
                data = await download_image(image)
                if data: media = BufferedInputFile(data, filename="game.jpg")
            if media:
                await msg.edit_media(media=InputMediaPhoto(media=media, caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
                return
        else:
            await msg.edit_text(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower(): return
    ok = await send_game_photo(cb.bot, msg.chat.id, pid, image, text, kb)
    if not ok:
        try: await cb.bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception: return
    try: await msg.delete()
    except Exception: pass

@router.callback_query(F.data.startswith("page_"))
async def paginate_products(cb: CallbackQuery):
    try: global_index = int(cb.data.split("_", 1)[1])
    except (IndexError, ValueError): await cb.answer("Ошибка"); return
    offset = (global_index // PAGE_LIMIT) * PAGE_LIMIT
    index = global_index - offset
    products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL AND image != '' ORDER BY id DESC LIMIT ? OFFSET ?", (PAGE_LIMIT, offset), fetch=True)
    total = db_query("SELECT COUNT(*) FROM products WHERE image IS NOT NULL AND image != ''", fetchone=True)[0]
    if 0 <= index < len(products): await send_product_card(cb, products, index, offset, total)
    else: await cb.answer("Дальше игр нет", show_alert=False)

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
            await message.answer(f"⏳ <b>Лимит рулетки!</b>\n\nОбычные пользователи — 1 раз в день.\nСледующая попытка через: <b>{h} ч. {m} мин.</b>\n\n💎 <b>Купи Премиум</b> — крути без ограничений!")
            return
    if is_prem:
        candidates = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE discount IS NOT NULL AND discount != '' AND image IS NOT NULL AND image != '' ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 20", fetch=True)
        game = random.choice(candidates) if candidates else None
    else: game = None
    if not game:
        game = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1", fetchone=True)
    if not game: await message.answer("В базе пока нет игр."); return
    db_query("UPDATE users SET last_roulette = ? WHERE user_id = ?", (utcnow_iso(), user_id))
    pid, title, price, old_price, discount, url, image, description, price_usd = game
    display_usd = f" (в долларах: {price_usd})" if (price_usd and "руб" not in price_usd.lower() and "₽" not in price_usd) else ""
    text = f"🎲 <b>Тебе выпала: {html.escape(title)}</b>\n\n"
    if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price: text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description: text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
    if is_prem: text += "\n👑 <i>Бонус Премиум: максимальная скидка!</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]])
    ok = await send_game_photo(message.bot, message.chat.id, pid, image, text, kb)
    if not ok: await message.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)

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
        game = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE discount IS NOT NULL AND discount != '' AND image IS NOT NULL AND image != '' ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 1", fetchone=True)
    else:
        game = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1", fetchone=True)
    await cb.answer()
    if not game: await cb.answer("В базе пока пусто", show_alert=True); return
    if not is_prem: db_query("UPDATE users SET last_random_again = ? WHERE user_id = ?", (utcnow_iso(), uid))
    pid, title, price, old_price, discount, url, image, description, price_usd = game
    text = f"🎲 <b>Тебе выпала: {html.escape(title)}</b>\n\n"
    if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price: text += f"💵 Цена: <b>{price}</b>\n"
    if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description: text += f"\n📝 <i>{safe_html_truncate(description, 150)}...</i>\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]])
    ok = await send_game_photo(cb.bot, cb.message.chat.id, pid, image, text, kb)
    if not ok: await cb.message.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)

# ---------- WISHLIST ----------
@router.callback_query(F.data == "wl_add_start")
async def wl_add_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    limit, count = wishlist_limit(uid), wishlist_count(uid)
    if count >= limit:
        await cb.answer(f"Лимит Wishlist: {limit} игр.\nКупи Премиум, чтобы добавить до 10!", show_alert=True)
        return
    await state.set_state(WishlistFSM.waiting_title)
    await cb.message.answer(f"⭐ <b>Добавление в Wishlist</b>\n\nНапиши название игры.\nУ тебя: {count} / {limit} игр.\n\n<i>Напиши «отмена», чтобы выйти.</i>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="wl_cancel")]]))
    await cb.answer()

@router.callback_query(F.data == "wl_cancel")
async def wl_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("❌ Добавление отменено.")
    except Exception: pass
    await cb.answer()

@router.message(WishlistFSM.waiting_title)
async def wl_add_title(message: Message, state: FSMContext):
    raw = normalize_title(message.text or "")
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear(); await message.answer("❌ Отменено.", reply_markup=main_menu_kb()); return
    if len(raw) < 2: await message.answer("Слишком короткое название. Попробуй ещё раз или напиши «отмена»."); return
    if len(raw) > 200: await message.answer("Слишком длинное название. Попробуй ещё раз или напиши «отмена»."); return
    uid = message.from_user.id
    limit, count = wishlist_limit(uid), wishlist_count(uid)
    if count >= limit: await state.clear(); await message.answer(f"❌ Достигнут лимит Wishlist ({limit} игр).", reply_markup=main_menu_kb()); return
    existing = db_query("SELECT id FROM wishlist_titles WHERE user_id = ? AND LOWER(title) = LOWER(?)", (uid, raw), fetchone=True)
    if existing: await state.clear(); await message.answer(f"⚠️ <b>{html.escape(raw)}</b> уже есть в Wishlist.", reply_markup=main_menu_kb()); return
    db_query("INSERT OR IGNORE INTO wishlist_titles (user_id, title) VALUES (?, ?)", (uid, raw))
    await state.clear()
    await message.answer(f"✅ <b>{html.escape(raw)}</b> добавлено в Wishlist!\nСейчас у тебя: {count + 1} / {limit} игр.", reply_markup=main_menu_kb())

@router.callback_query(F.data == "wl_show")
async def wl_show(cb: CallbackQuery):
    uid = cb.from_user.id
    limit = wishlist_limit(uid)
    rows = db_query("SELECT id, title FROM wishlist_titles WHERE user_id = ? ORDER BY id", (uid,), fetch=True)
    if not rows: await cb.answer("Wishlist пуст", show_alert=True); return
    text = f"⭐ <b>Твой Wishlist</b> ({len(rows)} / {limit}):\n\n"
    buttons = []
    for wid, title in rows:
        text += f"• {html.escape(title)}\n"
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить: {title[:25]}", callback_data=f"wl_del_{wid}")])
    if not check_premium(uid):
        text += "\n💎 Хочешь до 10 игр + уведомления о скидках? Пригласи друга!"
        buttons.append([InlineKeyboardButton(text="💎 Премиум", callback_data="premium_show")])
    try: await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except TelegramBadRequest: await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await cb.answer()

@router.callback_query(F.data.startswith("wl_del_"))
async def wl_del(cb: CallbackQuery):
    try: wid = int(cb.data.split("_", 2)[2])
    except (IndexError, ValueError): await cb.answer("Ошибка"); return
    db_query("DELETE FROM wishlist_titles WHERE id = ? AND user_id = ?", (wid, cb.from_user.id))
    await cb.answer("Удалено"); await wl_show(cb)

# ---------- ПОПОЛНЕНИЕ STEAM ----------
@router.message(F.text == "💳 Пополнить Steam")
async def steam_topup_start(message: Message, state: FSMContext):
    await state.set_state(TopUpFSM.waiting_login)
    await message.answer("💳 <b>Пополнение Steam</b>\n\nШаг 1 из 2. Введи свой <b>логин Steam</b> (не пароль!).\nНапример: <i>my_nickname</i>\n\n<i>Напиши «отмена», чтобы выйти.</i>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")]]))

@router.callback_query(F.data == "topup_cancel")
async def topup_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("❌ Пополнение отменено.")
    except Exception: pass
    await cb.answer()

@router.message(TopUpFSM.waiting_login)
async def topup_login(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear(); await message.answer("❌ Отменено.", reply_markup=main_menu_kb()); return
    if not raw or len(raw) < 3 or " " in raw or len(raw) > 64:
        await message.answer("⚠️ Некорректный логин. Попробуй ещё раз (от 3 до 64 символов, без пробелов)."); return
    await state.update_data(login=raw); await state.set_state(TopUpFSM.waiting_amount)
    await message.answer(f"👤 Логин: <b>{html.escape(raw)}</b>\n\nШаг 2 из 2. Введи <b>сумму пополнения в рублях</b> (только цифры).\nНапример: <i>500</i>\n\n<i>Напиши «отмена», чтобы выйти.</i>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")]]))

@router.message(TopUpFSM.waiting_amount)
async def topup_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear(); await message.answer("❌ Отменено.", reply_markup=main_menu_kb()); return
    if not is_ascii_digits(raw) or len(raw) > 6: await message.answer("⚠️ Пожалуйста, введи сумму только цифрами (например, 500)."); return
    amount = int(raw)
    if amount < 100: await message.answer("⚠️ Минимальная сумма пополнения — 100 рублей. Попробуй ещё раз."); return
    if amount > 50000: await message.answer("⚠️ Максимальная сумма — 50 000 рублей. Если нужно больше, сделай несколько пополнений."); return
    data = await state.get_data(); login = data.get("login", ""); await state.clear()
    db_query("INSERT INTO link_clicks (user_id, link_type) VALUES (?, ?)", (message.from_user.id, f"steam_sum_{amount}"))
    url = f"https://plati.market/itm/{STEAM_TOPUP_ITEM_ID}?agent={PLATI_AGENT_ID}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="💳 Перейти к оплате", url=url)], [InlineKeyboardButton(text="❌ Закрыть", callback_data="close_msg")]])
    await message.answer(f"✅ <b>Готово к пополнению</b>\n\n👤 Логин: <b>{html.escape(login)}</b>\n💵 Сумма: <b>{amount} ₽</b>\n\n1️⃣ Нажми кнопку ниже — откроется страница товара.\n2️⃣ Вставь свой логин <b>{html.escape(login)}</b> в поле «Ваш логин Steam».\n3️⃣ Введи сумму <b>{amount}</b> в поле «Получу, РУБЛЕЙ RUB».\n4️⃣ Оплати картой и жди зачисления (5–15 минут).\n\n⚠️ <b>Важно:</b> пароль вводить не нужно — только логин!", reply_markup=kb)

@router.callback_query(F.data == "close_msg")
async def close_msg(cb: CallbackQuery):
    try: await cb.message.delete()
    except Exception: pass
    await cb.answer()

# ---------- МОБИЛЬНЫЕ ИГРЫ ----------
@router.message(F.text == "📱 Мобильные игры")
async def mobile_topup_menu(message: Message):
    db_query("INSERT INTO link_clicks (user_id, link_type) VALUES (?, ?)", (message.from_user.id, "mobile_menu"))
    PLATI_MOBILE_URL = f"https://plati.market/search/Донат?agent={PLATI_AGENT_ID}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎮 Каталог мобильных игр", url=PLATI_MOBILE_URL)], [InlineKeyboardButton(text="❓ Как это работает", callback_data="mobile_help")]])
    await message.answer("📱 <b>Пополнение мобильных игр</b>\n\nПополняй любимые мобильные игры через <b>Plati.Market</b>:\n\n🎮 Genshin Impact, Honkai, PUBG Mobile, Brawl Stars и другие\n⚡ Быстрое зачисление (5–15 минут)\n💳 Оплата картами РФ\n\nНажми кнопку ниже 👇", reply_markup=kb)

@router.callback_query(F.data == "mobile_help")
async def mobile_help(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("❓ <b>Как пополнить мобильную игру</b>\n\n1️⃣ Нажми «Пополнить мобильные игры»\n2️⃣ Выбери свою игру из списка\n3️⃣ Введи <b>игровой ID</b> (не логин/пароль!)\n4️⃣ Выбери номинал (например, 60 гемов, 100 UC)\n5️⃣ Оплати картой\n\n⚠️ <b>Важно:</b> вводи только игровой ID!\n\n" f"Если что-то пошло не так — пиши {PLATI_SUPPORT}")

# ---------- РЕКЛАМА ----------
@router.message(F.text == "📢 Реклама")
async def ads_info(message: Message):
    await message.answer(
        "📢 <b>Реклама в нашем боте</b>\n\n"
        "Покупая рекламу, вы получаете <b>рассылку вашего рекламного поста всем пользователям нашего бота</b>.\n\n"
        "🎯 Мы отправим ваше сообщение (текст + фото/видео) каждому пользователю бота.\n"
        "📊 Вы получите охват всей нашей аудитории.\n\n"
        f"Для заказа и уточнения цены пиши: 👉 <b>{CONTACT_TG}</b>",
        reply_markup=main_menu_kb(),
    )

# ---------- ДОНАТ (ПОДДЕРЖАТЬ) ----------
@router.message(F.text == "❤️ Поддержать")
async def donate_info(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💸 Сделать донат", url=DONATE_URL)],
        [InlineKeyboardButton(text="⬅️ В меню", callback_data="close_msg")]
    ])
    await message.answer(
        "❤️ <b>Поддержать проект</b>\n\n"
        "Если наш бот помогает тебе экономить на играх, ты можешь отблагодарить разработчика!\n\n"
        "Твой донат пойдет на оплату сервера и развитие бота.\n\n"
        "Спасибо за поддержку! 🙏",
        reply_markup=kb,
    )

# ---------- ПОДДЕРЖКА ----------
@router.message(F.text == "🆘 Поддержка")
async def support_info(message: Message):
    await message.answer(
        "🆘 <b>Служба поддержки</b>\n\n"
        f"📱 Telegram: {CONTACT_TG}\n"
        f"✉️ Email: {CONTACT_EMAIL}\n\n"
        "Напишите нам, если возникли вопросы по работе бота.",
        reply_markup=main_menu_kb(),
    )

# ---------- АДМИНКА ----------
@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())

@router.message(F.text == "⬅️ В меню")
async def to_main(message: Message, state: FSMContext):
    await state.clear(); await message.answer("Главное меню:", reply_markup=main_menu_kb())

@router.message(F.text == "🗑 Очистить базу")
async def clear_db(message: Message):
    if not is_admin(message.from_user.id): return
    db_query("DELETE FROM products")
    await message.answer("✅ База игр очищена. Теперь нажми «🔄 Спарсить скидки», чтобы загрузить только реальные скидки.", reply_markup=admin_menu_kb())

@router.message(F.text == "🔄 Спарсить скидки")
async def manual_parse(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("⏳ Парсим Steam..."); await fetch_steam_deals(); await message.answer("✅ Парсинг завершён!", reply_markup=admin_menu_kb())

@router.message(F.text == "📊 Статистика")
async def stats(message: Message):
    if not is_admin(message.from_user.id): return
    users = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    products = db_query("SELECT COUNT(*) FROM products", fetchone=True)[0]
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
        if prem_count > 10: text_prem += f"  ... и ещё {prem_count - 10}\n"
    await message.answer(f"📊 <b>Статистика</b>\n\n👥 Всего юзеров: {users}\n🎮 Игр: {products}\n⭐ Wishlist записей: {wish}\n💳 Кликов Steam: {steam_clicks}\n📱 Кликов Mobile: {mobile_clicks}\n{text_prem}", reply_markup=admin_menu_kb())

@router.message(F.text == "👥 Рефералы")
async def admin_refs(message: Message):
    if not is_admin(message.from_user.id): return
    
