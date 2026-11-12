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
import io

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
from PIL import Image, ImageDraw, ImageFont

# ============ НАСТРОЙКИ ============
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
LOG_PATH = os.getenv("LOG_PATH", "/data/bot.log")
REVIEW_CHAT_ID = int(os.getenv("REVIEW_CHAT_ID", "-1004296458172"))

PLATI_AGENT_ID = os.getenv("PLATI_AGENT_ID", "1480092")
PLATI_SUPPORT = os.getenv("PLATI_SUPPORT", "@ObuzaMeduza")
STEAM_TOPUP_ITEM_ID = os.getenv("STEAM_TOPUP_ITEM_ID", "3961593")

REF_BONUS_DAYS = 3
CONTACT_TG = "@ObuzaMeduza"
CONTACT_EMAIL = "nikhrushawork@gmail.com"
DONATE_URL = "https://dalink.to/beaconkeys"

STARS_7_DAYS   = 150
STARS_30_DAYS  = 350
STARS_180_DAYS = 1500
STARS_365_DAYS = 2500

LOGO_PATH = "logo.png" 
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
FAVORITES_LIMIT_FREE = 3
FAVORITES_LIMIT_PREMIUM = 20
GENRES_LIMIT_FREE = 3
GENRES_LIMIT_PREMIUM = 10

AVAILABLE_GENRES = [
    "Action", "Adventure", "RPG", "Strategy", "Simulation", 
    "Indie", "Massively Multiplayer", "Casual", "Racing", "Sports",
    "Horror", "Survival", "Open World", "Puzzle", "Platformer",
    "Shooter", "Fighting", "Stealth", "Arcade", "Visual Novel"
]

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
        genres TEXT,
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
        last_review_date TEXT,
        referred_by INTEGER,
        ref_bonus_claimed INTEGER DEFAULT 0
    );
    CREATE TABLE IF NOT EXISTS user_genres (
        user_id INTEGER,
        genre TEXT,
        UNIQUE(user_id, genre)
    );
    CREATE TABLE IF NOT EXISTS notified_deals (
        product_id INTEGER PRIMARY KEY,
        notified_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS favorites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        product_id INTEGER,
        added_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(user_id, product_id)
    );
    CREATE TABLE IF NOT EXISTS link_clicks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        link_type TEXT,
        clicked_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS notified_favorites (
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
    if "last_review_date" not in cols:
        cur.execute("ALTER TABLE users ADD COLUMN last_review_date TEXT")

    cur.execute("PRAGMA table_info(products)")
    cols = {row[1] for row in cur.fetchall()}
    if "tg_file_id" not in cols:
        cur.execute("ALTER TABLE products ADD COLUMN tg_file_id TEXT")
    if "genres" not in cols:
        cur.execute("ALTER TABLE products ADD COLUMN genres TEXT")
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

def favorites_limit(user_id: int) -> int:
    return FAVORITES_LIMIT_PREMIUM if check_premium(user_id) else FAVORITES_LIMIT_FREE

def favorites_count(user_id: int) -> int:
    return db_query("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,), fetchone=True)[0]

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

# ---------- ЖАНРЫ ----------
def get_user_genres(user_id: int) -> list:
    rows = db_query("SELECT genre FROM user_genres WHERE user_id = ?", (user_id,), fetch=True)
    return [row[0] for row in rows]

def toggle_user_genre(user_id: int, genre: str) -> str:
    current = get_user_genres(user_id)
    limit = GENRES_LIMIT_PREMIUM if check_premium(user_id) else GENRES_LIMIT_FREE
    
    if genre in current:
        db_query("DELETE FROM user_genres WHERE user_id = ? AND genre = ?", (user_id, genre))
        return "removed"
    else:
        if len(current) >= limit:
            return "limit"
        db_query("INSERT OR IGNORE INTO user_genres (user_id, genre) VALUES (?, ?)", (user_id, genre))
        return "added"

# ---------- СКАЧИВАНИЕ И ОБРАБОТКА КАРТИНОК ----------
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
                return data
    except Exception as e:
        logger.warning(f"[IMG] {e}")
        return None

def apply_watermark(image_bytes: bytes) -> bytes:
    """Накладывает логотип и текст на картинку в левом верхнем углу."""
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        width, height = img.size
        
        watermark = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(watermark)
        
        # --- 1. ЛОГОТИП ---
        logo_x, logo_y = 15, 15
        if os.path.exists(LOGO_PATH):
            try:
                logo = Image.open(LOGO_PATH).convert("RGBA")
                logo.thumbnail((50, 50), Image.Resampling.LANCZOS) 
                watermark.paste(logo, (logo_x, logo_y), logo)
            except Exception as e:
                logger.warning(f"[Watermark] Ошибка загрузки логотипа: {e}")

        # --- 2. ТЕКСТ С ОБВОДКОЙ (УМЕНЬШЕННЫЙ) ---
        try:
            font = ImageFont.truetype("DejaVuSans-Bold.ttf", 16) 
        except IOError:
            font = ImageFont.load_default()
            
        text = "BeaconKeys"
        
        text_x = logo_x + 5
        text_y = logo_y + 55 
        
        stroke_width = 2
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx != 0 or dy != 0:
                    draw.text((text_x + dx, text_y + dy), text, font=font, fill=(0, 0, 0, 255))
        
        draw.text((text_x, text_y), text, font=font, fill=(255, 255, 255, 255))
        
        # --- 3. ОБЪЕДИНЯЕМ ---
        out = Image.alpha_composite(img, watermark)
        
        output = io.BytesIO()
        out.convert("RGB").save(output, format="JPEG", quality=90)
        return output.getvalue()
    except Exception as e:
        logger.error(f"[Watermark] Ошибка наложения: {e}")
        return image_bytes

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
    
    data = apply_watermark(data)
    
    try:
        msg = await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(data, filename="game.jpg"), caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        if product_id and msg.photo:
            db_query("UPDATE products SET tg_file_id = ? WHERE id = ?", (msg.photo[-1].file_id, product_id))
        return True
    except Exception as e:
        logger.warning(f"[PHOTO] {e}")
        return False

# ---------- ПАРСЕР STEAM ----------
async def _steam_get_details(session, app_id):
    details_url = f"https://store.steampowered.com/api/appdetails?appids={app_id}&cc=ru&l=russian"
    result = {"image": "", "description": "", "price_overview": None, "type": "game", "genres": ""}
    try:
        async with session.get(details_url, timeout=8) as resp:
            if resp.status == 200:
                det_data = await resp.json()
                if str(app_id) in det_data and det_data[str(app_id)]["success"]:
                    game_data = det_data[str(app_id)]["data"]
                    result["image"] = game_data.get("header_image", "")
                    result["description"] = game_data.get("short_description", "")
                    result["price_overview"] = game_data.get("price_overview")
                    result["type"] = game_data.get("type", "game")
                    genres = game_data.get("genres", [])
                    if genres:
                        result["genres"] = ",".join([g["description"] for g in genres])
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
        items_to_check = []
        for item in data.get("specials", {}).get("items", []):
            items_to_check.append(item)
        for section_key in ("top_sellers", "new_releases"):
            for item in data.get(section_key, {}).get("items", []):
                items_to_check.append(item)

        for item in items_to_check:
            title, app_id = item.get("name"), item.get("id")
            if not title or not app_id: continue

            det = await _steam_get_details(session, app_id)
            
            if det["type"] != "game":
                continue
            if re.search(r'(soundtrack|ost|artbook|art book|dlc|expansion|season pass)', title, re.IGNORECASE):
                continue
            if "additional content for" in det["description"].lower():
                continue
            if not det["price_overview"] or det["price_overview"].get("discount_percent", 0) == 0:
                continue

            if db_query("SELECT 1 FROM products WHERE title = ?", (title,), fetchone=True): continue

            shop_url = f"https://store.steampowered.com/app/{app_id}/"
            f_str, o_str, d_str = _format_price(det["price_overview"])
            price_usd = det["price_overview"].get("final_formatted", "") if det["price_overview"] else ""

            db_query("INSERT OR IGNORE INTO products (title, price, old_price, discount, url, image, description, price_usd, genres) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (title, f_str or "Бесплатно", o_str, d_str, shop_url, det["image"], det["description"], price_usd, det["genres"]))
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

# ---------- УВЕДОМЛЕНИЯ ИЗБРАННОГО ----------
async def notify_favorites_matches(bot: Bot):
    rows = db_query("SELECT f.user_id, p.id, p.title, p.price, p.discount, p.url, p.image, p.description FROM favorites f JOIN products p ON p.id = f.product_id JOIN users u ON u.user_id = f.user_id WHERE u.premium_until > ? AND p.discount IS NOT NULL AND p.discount != '' AND NOT EXISTS (SELECT 1 FROM notified_favorites nf WHERE nf.user_id = f.user_id AND nf.product_id = p.id) LIMIT 50", (utcnow_iso(),), fetch=True)
    if not rows: return
    for uid, pid, title, price, discount, url, image, desc in rows:
        text = f"⭐ <b>Игра из Избранного в скидке!</b>\n\n🎮 {html.escape(title)}\n💵 {price}\n🔥 {discount}\n\n📝 <i>{safe_html_truncate(desc or '', 100)}...</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
        ok = await send_game_photo(bot, uid, pid, image, text, kb)
        if not ok:
            try: await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception: continue
        db_query("INSERT OR IGNORE INTO notified_favorites (user_id, product_id) VALUES (?, ?)", (uid, pid))
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
            [KeyboardButton(text="🔥 Скидки"), KeyboardButton(text="🎲 Случайная игра"), KeyboardButton(text="💳 Пополнить Steam")],
            [KeyboardButton(text="📱 Мобильные игры"), KeyboardButton(text="💎 Премиум"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🎟 Промокод"), KeyboardButton(text="⭐ Оставить отзыв"), KeyboardButton(text="📢 Реклама")],
            [KeyboardButton(text="❤️ Поддержать"), KeyboardButton(text="🆘 Поддержка")],
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
class FavoritesFSM(StatesGroup):
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

class ReviewFSM(StatesGroup):
    waiting_text = State()

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
                    await message.bot.send_message(referrer_id, f"🎉 <b>По твоей ссылке пришел друг!</b>\n\nТебе начислено <b>+{REF_BONUS_DAYS} дня Премиума</b>! 👑")
                except Exception as e:
                    logger.warning(f"[REF] Не удалось отправить сообщение рефереру: {e}")
                await message.answer(f"✅ Ты пришел по реферальной ссылке! Твой друг получил <b>+{REF_BONUS_DAYS} дня Премиума</b>.")
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
    f_count, f_limit = favorites_count(uid), favorites_limit(uid)
    
    genres = get_user_genres(uid)
    g_limit = GENRES_LIMIT_PREMIUM if is_prem else GENRES_LIMIT_FREE
    g_text = ", ".join(genres) if genres else "не выбраны"

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
    text += f"\n⭐ <b>Избранное:</b> {f_count} / {f_limit} игр\n"
    text += f"🎮 <b>Твои жанры:</b> {g_text} ({len(genres)}/{g_limit})\n"
    text += f"\n👥 <b>Приглашено друзей:</b> {ref_count}\n"

    if refs:
        text += "<b>Твои рефералы:</b>\n"
        for uname, fname in refs:
            name = f"@{uname}" if uname else (fname or "—")
            text += f"• {html.escape(name)}\n"
        if ref_count == 20:
            text += "... (показаны последние 20)\n"

    buttons = [
        [InlineKeyboardButton(text="🎁 Пригласить друга (+3 дня)", callback_data="show_ref_link")],
        [InlineKeyboardButton(text="⭐ Мое Избранное", callback_data="fav_show")],
        [InlineKeyboardButton(text="🎮 Выбрать жанры для рулетки", callback_data="genres_show")],
        [InlineKeyboardButton(text="❤️ Поддержать проект", url=DONATE_URL)],
    ]
    
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ---------- ВЫБОР ЖАНРОВ ----------
@router.callback_query(F.data == "genres_show")
async def genres_show(cb: CallbackQuery):
    uid = cb.from_user.id
    is_prem = check_premium(uid)
    limit = GENRES_LIMIT_PREMIUM if is_prem else GENRES_LIMIT_FREE
    current = get_user_genres(uid)
    
    text = (
        f"🎮 <b>Настройка жанров для рулетки</b>\n\n"
        f"Выбери до <b>{limit}</b> жанров. Когда ты нажмешь «🎲 Случайная игра», "
        f"бот будет искать игры только среди них.\n\n"
        f"<i>Нажми на жанр, чтобы добавить или убрать его.</i>\n\n"
        f"Выбрано: <b>{len(current)} / {limit}</b>"
    )
    
    buttons = []
    row = []
    for genre in AVAILABLE_GENRES:
        check = "✅ " if genre in current else ""
        row.append(InlineKeyboardButton(text=f"{check}{genre}", callback_data=f"genre_toggle_{genre}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
        
    buttons.append([InlineKeyboardButton(text="⬅️ Назад в профиль", callback_data="profile_back")])
    
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except TelegramBadRequest:
        pass
    await cb.answer()

@router.callback_query(F.data.startswith("genre_toggle_"))
async def genre_toggle(cb: CallbackQuery):
    uid = cb.from_user.id
    genre = cb.data.split("_", 2)[2]
    res = toggle_user_genre(uid, genre)
    
    if res == "limit":
        limit = GENRES_LIMIT_PREMIUM if check_premium(uid) else GENRES_LIMIT_FREE
        await cb.answer(f"❌ Лимит жанров: {limit}.\nУбери один из выбранных или купи Премиум (до 10).", show_alert=True)
        return
    
    await genres_show(cb)

@router.callback_query(F.data == "profile_back")
async def profile_back(cb: CallbackQuery):
    await cb.answer()
    await profile_menu(cb.message)

# ---------- ОТЗЫВЫ ----------
@router.message(F.text == "⭐ Оставить отзыв")
async def review_start(message: Message):
    uid = message.from_user.id
    
    res = db_query("SELECT last_review_date FROM users WHERE user_id = ?", (uid,), fetchone=True)
    if res and res[0]:
        last_date = parse_dt(res[0])
        if last_date:
            time_passed = utcnow() - last_date
            if time_passed < timedelta(days=7):
                days_left = 7 - time_passed.days
                hours_left = (timedelta(days=7) - time_passed).seconds // 3600
                await message.answer(
                    f"⏳ <b>Вы уже оставляли отзыв недавно!</b>\n\n"
                    f"Следующий отзыв можно оставить через <b>{days_left} дн. {hours_left} ч.</b>\n\n"
                    f"<i>Мы ограничили частоту отзывов, чтобы они оставались честными и не спамили.</i>"
                )
                return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="1 ⭐", callback_data="rate_1"),
         InlineKeyboardButton(text="2 ⭐", callback_data="rate_2"),
         InlineKeyboardButton(text="3 ⭐", callback_data="rate_3"),
         InlineKeyboardButton(text="4 ⭐", callback_data="rate_4"),
         InlineKeyboardButton(text="5 ⭐", callback_data="rate_5")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="review_cancel")]
    ])
    await message.answer(
        "⭐ <b>Оцените нашего бота!</b>\n\n"
        "Поставьте оценку от 1 до 5 звезд, нажав на кнопку ниже. "
        "После этого вы сможете написать небольшой комментарий.",
        reply_markup=kb
    )

@router.callback_query(F.data.startswith("rate_"))
async def review_rate(cb: CallbackQuery, state: FSMContext):
    rating = int(cb.data.split("_")[1])
    await state.update_data(rating=rating)
    await state.set_state(ReviewFSM.waiting_text)
    
    await cb.message.edit_text(
        f"Вы поставили оценку: <b>{rating} ⭐</b>\n\n"
        "Теперь напишите небольшой комментарий о боте (что понравилось, что можно улучшить).\n\n"
        "<i>Напишите «отмена», чтобы выйти.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="review_cancel")]])
    )
    await cb.answer()

@router.callback_query(F.data == "review_cancel")
async def review_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("❌ Оставление отзыва отменено.")
    except Exception: pass
    await cb.answer()

@router.message(ReviewFSM.waiting_text)
async def review_text(message: Message, state: FSMContext, bot: Bot):
    text = (message.text or "").strip()
    if text.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=main_menu_kb())
        return
    
    if len(text) < 5:
        await message.answer("⚠️ Комментарий слишком короткий. Напишите хотя бы пару слов.")
        return
        
    if len(text) > 1000:
        await message.answer("⚠️ Комментарий слишком длинный (макс. 1000 символов).")
        return

    data = await state.get_data()
    rating = data.get("rating", 5)
    await state.clear()

    db_query("UPDATE users SET last_review_date = ? WHERE user_id = ?", (utcnow_iso(), message.from_user.id))

    user_info = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    review_msg = (
        f"📩 <b>Новый отзыв о боте!</b>\n\n"
        f"👤 <b>От:</b> {html.escape(user_info)} (<code>{message.from_user.id}</code>)\n"
        f"⭐ <b>Оценка:</b> {rating}/5\n"
        f"💬 <b>Комментарий:</b>\n{html.escape(text)}"
    )

    try:
        await bot.send_message(chat_id=REVIEW_CHAT_ID, text=review_msg, parse_mode=ParseMode.HTML)
        await message.answer(
            "✅ <b>Спасибо за отзыв!</b>\n\n"
            "Ваше мнение очень важно для нас, мы обязательно его учтем.\n"
            "<i>Следующий отзыв можно будет оставить через 7 дней.</i>",
            reply_markup=main_menu_kb()
        )
    except Exception as e:
        logger.error(f"Ошибка отправки отзыва: {e}")
        await message.answer(
            "✅ Спасибо! Ваш отзыв сохранен, но произошла ошибка при отправке администратору.\n"
            f"(Техническая причина: {type(e).__name__})",
            reply_markup=main_menu_kb()
        )

# ---------- ПРЕМИУМ ----------
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
        "⭐ <b>Избранное до 20 игр</b>\n"
        "   <i>Добавляй больше игр в список желаемого.</i>\n\n"
        "🎮 <b>До 10 жанров в рулетке</b>\n"
        "   <i>Настраивай рулетку под себя.</i>\n\n"
        "🔔 <b>Мгновенные алерты</b>\n"
        "   <i>Персональные уведомления, как только на игру из Избранного появляется скидка или цена падает ниже 80%.</i>\n\n"
        "🎁 <b>Как получить Премиум БЕСПЛАТНО?</b>\n"
        f"Пригласи друга по своей ссылке и получи <b>+{REF_BONUS_DAYS} дня Премиума</b> за каждого!\n\n"
        f"👥 Ты уже пригласил: <b>{ref_count} чел.</b>\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔥 7 дней — {STARS_7_DAYS} ⭐", callback_data="buy_premium_7")],
        [InlineKeyboardButton(text=f"👑 30 дней — {STARS_30_DAYS} ⭐", callback_data="buy_premium_30")],
        [InlineKeyboardButton(text=f"💎 6 месяцев — {STARS_180_DAYS} ⭐", callback_data="buy_premium_180")],
        [InlineKeyboardButton(text=f"🏆 1 год — {STARS_365_DAYS} ⭐", callback_data="buy_premium_365")],
    ])

    await message.answer(text, reply_markup=kb, disable_web_page_preview=True)

@router.message(F.text == "💎 Премиум")
async def premium_menu_msg(message: Message):
    await premium_menu(message)

@router.callback_query(F.data == "premium_show")
async def premium_show(cb: CallbackQuery):
    await cb.answer()
    if cb.message: await premium_menu(cb.message, user_id=cb.from_user.id)

@router.callback_query(F.data.startswith("buy_premium_"))
async def buy_premium_stars(cb: CallbackQuery):
    try:
        days = int(cb.data.rsplit("_", 1)[1])
    except (ValueError, IndexError):
        await cb.answer("Ошибка тарифа", show_alert=True)
        return

    price_map = {
        7:   STARS_7_DAYS,
        30:  STARS_30_DAYS,
        180: STARS_180_DAYS,
        365: STARS_365_DAYS,
    }
    price = price_map.get(days)
    if not price:
        await cb.answer("Неизвестный тариф", show_alert=True)
        return

    prices = [LabeledPrice(label=f"Премиум на {days} дней", amount=price)]

    try:
        await cb.bot.send_invoice(
            chat_id=cb.from_user.id,
            title=f"Премиум-доступ на {days} дней",
            description=f"Активация Премиум-доступа в боте BeaconKeys на {days} дней.",
            payload=f"premium_{days}",
            provider_token="",
            currency="XTR",
            prices=prices,
            start_parameter="premium",
        )
        await cb.answer("Счет выставлен в чате!")
    except Exception as e:
        logger.error(f"Ошибка создания счета: {e}")
        await cb.answer("❌ Ошибка создания счета. Попробуйте позже.", show_alert=True)

@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery):
    await pre_checkout_q.answer(ok=True)

@router.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    payment_info: SuccessfulPayment = message.successful_payment
    payload = payment_info.invoice_payload

    try:
        days = int(payload.split("_")[1])
    except (IndexError, ValueError):
        days = 7

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
        f"За каждого друга, который зайдет по ней, ты получишь <b>+{REF_BONUS_DAYS} дня Премиума</b>!",
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
    
    uid = message_or_callback.from_user.id
    is_fav = db_query("SELECT 1 FROM favorites WHERE user_id = ? AND product_id = ?", (uid, pid), fetchone=True)
    
    nav_row = []
    if global_index > 1: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{global_index - 2}"))
    if global_index < total: nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{global_index}"))
    
    buttons = []
    if nav_row: buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🛒 Купить в Steam", url=url)])
    
    if is_fav:
        buttons.append([InlineKeyboardButton(text="✅ В Избранном", callback_data="fav_already")])
    else:
        buttons.append([InlineKeyboardButton(text="⭐ Добавить в избранное", callback_data=f"fav_add_{pid}")])
        
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
                if data: 
                    data = apply_watermark(data)
                    media = BufferedInputFile(data, filename="game.jpg")
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

# ---------- ДОБАВЛЕНИЕ В ИЗБРАННОЕ ----------
@router.callback_query(F.data.startswith("fav_add_"))
async def fav_add(cb: CallbackQuery):
    uid = cb.from_user.id
    pid = int(cb.data.split("_")[2])
    
    limit = favorites_limit(uid)
    count = favorites_count(uid)
    if count >= limit:
        await cb.answer(f"Лимит Избранного: {limit} игр.\nКупи Премиум, чтобы добавить до 20!", show_alert=True)
        return
    
    db_query("INSERT OR IGNORE INTO favorites (user_id, product_id) VALUES (?, ?)", (uid, pid))
    await cb.answer("✅ Игра добавлена в Избранное! Теперь ты получишь уведомление, если на нее будет скидка.")
    
    products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE id = ?", (pid,), fetch=True)
    if products:
        total = db_query("SELECT COUNT(*) FROM products WHERE image IS NOT NULL AND image != ''", fetchone=True)[0]
        await send_product_card(cb, products, 0, 0, total)

@router.callback_query(F.data == "fav_already")
async def fav_already(cb: CallbackQuery):
    await cb.answer("✅ Эта игра уже в твоем Избранном!")

@router.callback_query(F.data == "fav_show")
async def fav_show(cb: CallbackQuery):
    uid = cb.from_user.id
    limit = favorites_limit(uid)
    rows = db_query("SELECT p.id, p.title FROM favorites f JOIN products p ON p.id = f.product_id WHERE f.user_id = ? ORDER BY f.id", (uid,), fetch=True)
    if not rows:
        await cb.answer("Твое Избранное пусто", show_alert=True)
        return
    text = f"⭐ <b>Твое Избранное</b> ({len(rows)} / {limit}):\n\n"
    buttons = []
    for pid, title in rows:
        text += f"• {html.escape(title)}\n"
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить: {title[:25]}", callback_data=f"fav_del_{pid}")])
    if not check_premium(uid):
        text += "\n💎 Хочешь до 20 игр + уведомления о скидках? Купи Премиум!"
        buttons.append([InlineKeyboardButton(text="💎 Премиум", callback_data="premium_show")])
    try: await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except TelegramBadRequest: await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await cb.answer()

@router.callback_query(F.data.startswith("fav_del_"))
async def fav_del(cb: CallbackQuery):
    try: pid = int(cb.data.split("_", 2)[2])
    except (IndexError, ValueError): await cb.answer("Ошибка"); return
    db_query("DELETE FROM favorites WHERE product_id = ? AND user_id = ?", (pid, cb.from_user.id))
    await cb.answer("Удалено из Избранного")
    await fav_show(cb)

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
            await message.answer(f"⏳ <b>Лимит рулетки!</b>\n\nОбычные пользователи — 1 раз в день.\nСледующая попытка через: <b>{h} ч. {m} мин.</b>\n\n💎 <b>Купи Премиум</b> — крути без ограничений!")
            return

    user_genres = get_user_genres(user_id)
    
    if user_genres:
        genre_conditions = " OR ".join(["genres LIKE ?"] * len(user_genres))
        params = [f"%{g}%" for g in user_genres]
        
        if is_prem:
            query = f"SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE ({genre_conditions}) AND discount IS NOT NULL AND discount != '' AND image IS NOT NULL AND image != '' ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 20"
        else:
            query = f"SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE ({genre_conditions}) AND image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1"
        
        candidates = db_query(query, params, fetch=True)
        game = random.choice(candidates) if candidates else None
        
        if not game:
            await message.answer(f"😔 В жанрах {', '.join(user_genres)} пока нет игр со скидками.\nПопробуй выбрать другие жанры в профиле или зайди позже.")
            return
    else:
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
    
    user_genres = get_user_genres(uid)
    
    if user_genres:
        genre_conditions = " OR ".join(["genres LIKE ?"] * len(user_genres))
        params = [f"%{g}%" for g in user_genres]
        
        if is_prem:
            query = f"SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE ({genre_conditions}) AND discount IS NOT NULL AND discount != '' AND image IS NOT NULL AND image != '' ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 1"
        else:
            query = f"SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE ({genre_conditions}) AND image IS NOT NULL AND image != '' ORDER BY RANDOM() LIMIT 1"
        
        game = db_query(query, params, fetchone=True)
    else:
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
    wish = db_query("SELECT COUNT(*) FROM favorites", fetchone=True)[0]
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
    await message.answer(f"📊 <b>Статистика</b>\n\n👥 Всего юзеров: {users}\n🎮 Игр: {products}\n⭐ Избранное (записей): {wish}\n💳 Кликов Steam: {steam_clicks}\n📱 Кликов Mobile: {mobile_clicks}\n{text_prem}", reply_markup=admin_menu_kb())

@router.message(F.text == "👥 Рефералы")
async def admin_refs(message: Message):
    if not is_admin(message.from_user.id): return

    rows = db_query("""
        SELECT u.user_id, u.username, u.first_name, u.joined_at, u.referred_by, u.ref_bonus_claimed
        FROM users u
        WHERE u.referred_by IS NOT NULL
        ORDER BY u.joined_at DESC LIMIT 20
    """, fetch=True)

    if not rows:
        await message.answer("Пока нет пользователей, которые пришли по реферальным ссылкам.", reply_markup=admin_menu_kb())
        return

    text = "👥 <b>Последние 20 рефералов:</b>\n\n"
    for uid, uname, fname, joined, ref_by, claimed in rows:
        status = "✅" if claimed else "⏳"
        text += f"{status} <code>{uid}</code> | @{uname or '—'} | пришел от: <code>{ref_by}</code>\n"

    await message.answer(text, reply_markup=admin_menu_kb())

# ---------- УПРАВЛЕНИЕ ПРОМОКОДАМИ ----------
@router.message(F.text == "🎟 Промокоды")
async def admin_promo_menu(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer(
        "🎟 <b>Управление промокодами</b>\n\n"
        "Здесь вы можете создавать новые промокоды и смотреть список существующих.",
        reply_markup=promo_menu_kb()
    )

@router.message(F.text == "⬅️ Назад в админку")
async def admin_promo_back(message: Message, state: FSMContext):
    await state.clear()
    if not is_admin(message.from_user.id): return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())

@router.message(F.text == "➕ Создать промокод")
async def admin_promo_create_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(PromoFSM.waiting_code)
    await message.answer(
        "➕ <b>Создание промокода</b>\n\n"
        "Введите название промокода (например, <code>WELCOME2026</code>).\n"
        "Он будет автоматически приведен к верхнему регистру.\n\n"
        "<i>Напишите «отмена», чтобы выйти.</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True)
    )

@router.message(PromoFSM.waiting_code)
async def admin_promo_create_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if message.text == "⬅️ Отмена":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=promo_menu_kb())
        return

    code = (message.text or "").strip().upper()
    if not code or " " in code:
        await message.answer("⚠️ Код не должен содержать пробелов. Попробуйте снова.")
        return

    existing = db_query("SELECT 1 FROM promocodes WHERE code = ?", (code,), fetchone=True)
    if existing:
        await message.answer("⚠️ Такой промокод уже существует. Введите другой.")
        return

    await state.update_data(code=code)
    await state.set_state(PromoFSM.waiting_days)
    await message.answer(
        f"Код: <b>{code}</b>\n\n"
        "На сколько <b>дней</b> Премиума рассчитан этот код? (например, 7)\n\n"
        "<i>Напишите «отмена», чтобы выйти.</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True)
    )

@router.message(PromoFSM.waiting_days)
async def admin_promo_create_days(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if message.text == "⬅️ Отмена":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=promo_menu_kb())
        return

    raw = (message.text or "").strip()
    if not is_ascii_digits(raw) or int(raw) <= 0 or int(raw) > 3650:
        await message.answer("⚠️ Введите число дней от 1 до 3650.")
        return

    await state.update_data(days=int(raw))
    await state.set_state(PromoFSM.waiting_max_uses)
    await message.answer(
        "Сколько раз можно использовать этот промокод? (например, 1 для одноразового или 100 для массового)\n\n"
        "<i>Напишите «отмена», чтобы выйти.</i>",
        reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True)
    )

@router.message(PromoFSM.waiting_max_uses)
async def admin_promo_create_max_uses(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    if message.text == "⬅️ Отмена":
        await state.clear()
        await message.answer("❌ Отменено.", reply_markup=promo_menu_kb())
        return

    raw = (message.text or "").strip()
    if not is_ascii_digits(raw) or int(raw) <= 0:
        await message.answer("⚠️ Введите число больше 0.")
        return

    max_uses = int(raw)
    data = await state.get_data()
    code = data.get("code")
    days = data.get("days")

    db_query("INSERT INTO promocodes (code, days, max_uses) VALUES (?, ?, ?)", (code, days, max_uses))
    await state.clear()

    await message.answer(
        f"✅ <b>Промокод создан!</b>\n\n"
        f"Код: <code>{code}</code>\n"
        f"Дней Премиума: <b>{days}</b>\n"
        f"Максимум использований: <b>{max_uses}</b>",
        reply_markup=promo_menu_kb()
    )

@router.message(F.text == "📋 Список промокодов")
async def admin_promo_list(message: Message):
    if not is_admin(message.from_user.id): return
    rows = db_query("SELECT code, days, max_uses, uses_count FROM promocodes ORDER BY created_at DESC LIMIT 20", fetch=True)
    if not rows:
        await message.answer("Промокодов пока нет.", reply_markup=promo_menu_kb())
        return

    text = "🎟 <b>Последние 20 промокодов:</b>\n\n"
    for code, days, max_uses, uses_count in rows:
        status = "✅" if uses_count < max_uses else "❌"
        text += f"{status} <code>{code}</code> | {days} дн. | {uses_count}/{max_uses}\n"

    await message.answer(text, reply_markup=promo_menu_kb())

@router.message(PromoFSM.waiting_code, F.text == "⬅️ Отмена")
async def admin_promo_cancel_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.clear()
    await message.answer("❌ Отменено.", reply_markup=promo_menu_kb())

# ---------- ВЫДАЧА/СНЯТИЕ ПРЕМИУМА ----------
@router.message(F.text == "🎁 Выдать премиум")
async def admin_grant_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminPremiumFSM.waiting_user_id); await state.update_data(action="grant")
    await message.answer("🎁 <b>Выдача Премиума</b>\n\nВведи <b>user_id</b> пользователя (число).", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")]]))

@router.message(F.text == "🚫 Снять премиум")
async def admin_revoke_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminPremiumFSM.waiting_user_id); await state.update_data(action="revoke")
    await message.answer("🚫 <b>Снятие Премиума</b>\n\nВведи <b>user_id</b>:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")]]))

@router.message(AdminPremiumFSM.waiting_user_id)
async def admin_uid_router(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    data = await state.get_data(); action = data.get("action", "grant")
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear(); await message.answer("❌ Отменено.", reply_markup=admin_menu_kb()); return
    if not is_ascii_digits(raw): await message.answer("⚠️ Нужно целое число — user_id."); return
    target = int(raw)
    if action == "revoke":
        await state.clear()
        if revoke_premium(target):
            await message.answer(f"🚫 Премиум снят с <code>{target}</code>.", reply_markup=admin_menu_kb())
            try: await message.bot.send_message(target, "ℹ️ Твой Премиум был отключён администратором.")
            except Exception: pass
        else: await message.answer("❌ Юзер не найден.", reply_markup=admin_menu_kb())
        return
    row = db_query("SELECT first_name, username FROM users WHERE user_id = ?", (target,), fetchone=True)
    if not row: await message.answer("❌ Пользователь с таким ID не найден в базе."); return
    await state.update_data(target_id=target); await state.set_state(AdminPremiumFSM.waiting_days)
    fname, uname = row
    await message.answer(f"👤 Найден: <b>{html.escape(fname or '—')}</b> (@{uname or '—'}, <code>{target}</code>)\n\nНа сколько <b>дней</b> выдать премиум? (например, 30)\nЕсли уже есть премиум — дни добавятся сверху.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin_prem_cancel")]]))

@router.message(AdminPremiumFSM.waiting_days)
async def admin_grant_days(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"):
        await state.clear(); await message.answer("❌ Отменено.", reply_markup=admin_menu_kb()); return
    if not is_ascii_digits(raw): await message.answer("⚠️ Введи число дней от 1 до 3650."); return
    days = int(raw)
    if days <= 0 or days > 3650: await message.answer("⚠️ Введи число дней от 1 до 3650."); return
    data = await state.get_data(); target = data.get("target_id"); await state.clear()
    if not target: await message.answer("❌ Ошибка: пользователь не выбран.", reply_markup=admin_menu_kb()); return
    ok = grant_premium_by_id(target, days)
    if ok:
        await message.answer(f"✅ Премиум выдан пользователю <code>{target}</code> на <b>{days} дн.</b>", reply_markup=admin_menu_kb())
        try: await message.bot.send_message(target, f"🎉 <b>Тебе активирован Премиум на {days} дней!</b>\n\nСпасибо, что с нами 💎")
        except Exception: pass
    else: await message.answer("❌ Не удалось — юзер не найден.", reply_markup=admin_menu_kb())

@router.callback_query(F.data == "admin_prem_cancel")
async def admin_prem_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("❌ Отменено.")
    except Exception: pass
    await cb.answer()

# ---------- РАССЫЛКА ----------
@router.message(F.text == "📢 Рассылка")
async def admin_broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id): return
    await state.set_state(AdminBroadcastFSM.waiting_message)
    await message.answer("📢 <b>Режим рассылки</b>\n\nОтправь мне сообщение (текст и/или фото), которое нужно разослать.\nПеред отправкой покажу предпросмотр.\n\n<i>Напиши «отмена», чтобы выйти.</i>", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True))

@router.message(AdminBroadcastFSM.waiting_message)
async def admin_broadcast_preview(message: Message, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id): return
    if message.text == "⬅️ Отмена" or (message.text and message.text.lower() in ("отмена", "cancel", "/cancel")):
        await state.clear(); await message.answer("❌ Рассылка отменена.", reply_markup=admin_menu_kb()); return
    photo_id = message.photo[-1].file_id if message.photo else None
    text = message.text if message.text else message.caption
    if not photo_id and not text: await message.answer("⚠️ Сообщение пустое. Отправь текст или фото."); return
    await state.update_data(photo_id=photo_id, text=text); await state.set_state(AdminBroadcastFSM.waiting_confirm)
    total = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    eta_sec = total * 0.05; eta_str = f"{int(eta_sec // 60)} мин {int(eta_sec % 60)} сек"
    await message.answer(f"👁 <b>Предпросмотр</b>\n\nПолучателей: <b>{total}</b>\nОриентировочное время: <b>{eta_str}</b>\n\nОтправляем?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Отправить", callback_data="bc_send")], [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")]]))
    try:
        if photo_id: await bot.send_photo(chat_id=message.chat.id, photo=photo_id, caption=text, parse_mode=ParseMode.HTML)
        else: await bot.send_message(chat_id=message.chat.id, text=text, parse_mode=ParseMode.HTML)
    except Exception as e: await message.answer(f"⚠️ Не удалось показать предпросмотр: {e}")

@router.callback_query(F.data == "bc_cancel")
async def admin_broadcast_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await cb.message.edit_text("❌ Рассылка отменена.")
    except Exception: pass
    await cb.answer()

@router.callback_query(F.data == "bc_send")
async def admin_broadcast_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(cb.from_user.id): await cb.answer("Нет доступа", show_alert=True); return
    data = await state.get_data(); photo_id = data.get("photo_id"); text = data.get("text"); await state.clear()
    if not photo_id and not text: await cb.answer("Пустое сообщение", show_alert=True); return
    users = db_query("SELECT user_id FROM users", fetch=True); total = len(users)
    try: await cb.message.edit_text(f"⏳ Рассылка для {total} пользователей...")
    except Exception: pass
    await cb.answer()
    sent, blocked = 0, 0
    for (uid,) in users:
        try:
            if photo_id: await bot.send_photo(chat_id=uid, photo=photo_id, caption=text, parse_mode=ParseMode.HTML)
            else: await bot.send_message(chat_id=uid, text=text, parse_mode=ParseMode.HTML)
            sent += 1
        except TelegramBadRequest as e:
            if "blocked" in str(e).lower() or "chat not found" in str(e).lower(): db_query("DELETE FROM users WHERE user_id = ?", (uid,))
            blocked += 1
        except Exception: blocked += 1
        await asyncio.sleep(0.05)
    try: await cb.message.edit_text(f"✅ <b>Рассылка завершена!</b>\n\n📤 Отправлено: {sent}\n🚫 Заблокировали/Ошибка: {blocked}")
    except Exception: await bot.send_message(cb.from_user.id, f"✅ Рассылка: {sent} отправлено, {blocked} ошибок.", reply_markup=admin_menu_kb())
    await bot.send_message(cb.from_user.id, "Готово.", reply_markup=admin_menu_kb())

# ---------- ЗАПУСК ----------
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.errors.register(errors_handler)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(fetch_steam_deals, "interval", hours=12, max_instances=1)
    scheduler.add_job(send_daily_deal, "cron", hour=12, minute=0, args=[bot], max_instances=1)
    scheduler.add_job(send_daily_deal, "cron", hour=19, minute=0, args=[bot], max_instances=1)
    scheduler.add_job(notify_premium_super_deals, "interval", hours=3, args=[bot], max_instances=1)
    scheduler.add_job(notify_favorites_matches, "interval", minutes=30, args=[bot], max_instances=1)
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен (Водяной знак в левом верхнем углу, уменьшенный текст)!")
    try: await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if _db_conn: _db_conn.close()

if __name__ == "__main__":
    asyncio.run(main())
