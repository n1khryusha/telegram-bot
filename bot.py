import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import threading
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
import io

import aiohttp
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramForbiddenError
from aiogram.filters import Command, CommandStart, CommandObject
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

# --- FAZERCARDS API ---
FAZER_API_KEY = os.getenv("FAZER_API_KEY", "")
FAZER_API_BASE = "https://api.fzr.cards/api/v2"
FAZER_MARKUP_PERCENT = 15  # Наценка в % (15% сверху)
STARS_TO_RUB = 1.5         # 1 звезда = 1.5 рубля (для конвертации)

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
admin_router = Router()
admin_router.message.filter(F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

PAGE_LIMIT = 10
FAVORITES_LIMIT_FREE = 5
FAVORITES_LIMIT_PREMIUM = 30
GENRES_LIMIT_FREE = 3
GENRES_LIMIT_PREMIUM = 10

SEARCH_COOLDOWN = {}
SEARCH_COOLDOWN_SEC = 5

AVAILABLE_GENRES = [
    "Action", "Adventure", "RPG", "Strategy", "Simulation", 
    "Indie", "Massively Multiplayer", "Casual", "Racing", "Sports",
    "Horror", "Survival", "Open World", "Puzzle", "Platformer",
    "Shooter", "Fighting", "Stealth", "Arcade", "Visual Novel"
]

# ---------- КЭШ ЛОГОТИПА ----------
_LOGO_CACHE: Image.Image | None = None

def get_logo() -> Image.Image | None:
    global _LOGO_CACHE
    if _LOGO_CACHE is None and os.path.exists(LOGO_PATH):
        try:
            _LOGO_CACHE = Image.open(LOGO_PATH).convert("RGBA")
        except Exception as e:
            logger.warning(f"[Logo] Ошибка загрузки: {e}")
    return _LOGO_CACHE

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
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_updated TEXT
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
        last_daily_bonus TEXT,
        daily_bonus_streak INTEGER DEFAULT 0,
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
        expires_at TEXT,
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
    if "referred_by" not in cols: cur.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
    if "ref_bonus_claimed" not in cols: cur.execute("ALTER TABLE users ADD COLUMN ref_bonus_claimed INTEGER DEFAULT 0")
    if "last_review_date" not in cols: cur.execute("ALTER TABLE users ADD COLUMN last_review_date TEXT")
    if "last_daily_bonus" not in cols: cur.execute("ALTER TABLE users ADD COLUMN last_daily_bonus TEXT")
    if "daily_bonus_streak" not in cols: cur.execute("ALTER TABLE users ADD COLUMN daily_bonus_streak INTEGER DEFAULT 0")

    cur.execute("PRAGMA table_info(products)")
    cols = {row[1] for row in cur.fetchall()}
    if "tg_file_id" not in cols: cur.execute("ALTER TABLE products ADD COLUMN tg_file_id TEXT")
    if "genres" not in cols: cur.execute("ALTER TABLE products ADD COLUMN genres TEXT")
    if "last_updated" not in cols: cur.execute("ALTER TABLE products ADD COLUMN last_updated TEXT")
    
    try:
        cur.execute("PRAGMA index_list(products)")
        indexes = cur.fetchall()
        has_unique_title = False
        for idx in indexes:
            if idx[2] == 1:
                cur.execute(f"PRAGMA index_info({idx[1]})")
                idx_cols = [c[2] for c in cur.fetchall()]
                if "title" in idx_cols:
                    has_unique_title = True
                    break
        if not has_unique_title:
            cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_title ON products(title)")
    except Exception as e:
        logger.warning(f"[DB] Ошибка индекса: {e}")
    
    _db_conn.commit()
    logger.info("[DB] База данных инициализирована.")

def db_query(sql, params=(), fetch=False, fetchone=False):
    with _db_lock:
        try:
            cur = _db_conn.cursor()
            cur.execute(sql, params)
            if fetch: result = cur.fetchall()
            elif fetchone: result = cur.fetchone()
            else: result = None
            _db_conn.commit()
            return result
        except Exception as e:
            logger.error(f"[DB] Ошибка SQL: {e}\nSQL: {sql}\nParams: {params}")
            raise

# ---------- ХЕЛПЕРЫ ----------
def is_ascii_digits(s: str) -> bool:
    return bool(s) and s.isascii() and s.isdigit()

def safe_html_truncate(text: str, limit: int = 200) -> str:
    if not text: return ""
    text = html.escape(text, quote=False)
    if len(text) <= limit: return text
    return text[:limit] + "..."

def escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

def check_premium(user_id: int) -> bool:
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]: return False
    dt = parse_dt(res[0])
    return bool(dt and dt > utcnow())

def get_premium_until(user_id: int):
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]: return None
    return parse_dt(res[0])

def add_premium_days(user_id: int, days: int):
    current = get_premium_until(user_id)
    now = utcnow()
    new_expiry = (current + timedelta(days=days)) if (current and current > now) else (now + timedelta(days=days))
    db_query("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_expiry.isoformat(), user_id))
    logger.info(f"[PREMIUM] +{days} дней пользователю {user_id}.")

def favorites_limit(user_id: int) -> int:
    return FAVORITES_LIMIT_PREMIUM if check_premium(user_id) else FAVORITES_LIMIT_FREE

def favorites_count(user_id: int) -> int:
    return db_query("SELECT COUNT(*) FROM favorites WHERE user_id = ?", (user_id,), fetchone=True)[0]

def list_premium_users() -> list:
    return db_query("SELECT user_id, username, first_name, premium_until FROM users WHERE premium_until > ?", (utcnow_iso(),), fetch=True)

def grant_premium_by_id(user_id: int, days: int) -> bool:
    row = db_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row: return False
    add_premium_days(user_id, days)
    return True

def revoke_premium(user_id: int) -> bool:
    row = db_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not row: return False
    db_query("UPDATE users SET premium_until = NULL WHERE user_id = ?", (user_id,))
    return True

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
        if len(current) >= limit: return "limit"
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
                while _image_cache_size + len(data) > IMAGE_CACHE_MAX_BYTES and IMAGE_CACHE:
                    _, old = IMAGE_CACHE.popitem(last=False)
                    _image_cache_size -= len(old)
                IMAGE_CACHE[url] = data
                _image_cache_size += len(data)
                return data
    except Exception as e:
        logger.warning(f"[IMG] {e}")
        return None

def apply_watermark(image_bytes: bytes) -> bytes:
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        width, height = img.size
        watermark = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        draw = ImageDraw.Draw(watermark)
        
        logo_x, logo_y = 15, 15
        logo = get_logo()
        if logo:
            try:
                logo_copy = logo.copy()
                logo_copy.thumbnail((60, 60), Image.Resampling.LANCZOS)
                shadow = Image.new("RGBA", logo_copy.size, (0, 0, 0, 0))
                shadow.paste((0, 0, 0, 150), (0, 0, logo_copy.width, logo_copy.height), logo_copy)
                watermark.paste(shadow, (logo_x + 2, logo_y + 2), shadow)
                watermark.paste(logo_copy, (logo_x, logo_y), logo_copy)
            except Exception: pass
            
        try: font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18) 
        except IOError: font = ImageFont.load_default()
        
        text = "BeaconKeys"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_height = bbox[3] - bbox[1]
        text_x, text_y = 20, height - text_height - 20 
        
        stroke_width = 2
        for dx in range(-stroke_width, stroke_width + 1):
            for dy in range(-stroke_width, stroke_width + 1):
                if dx != 0 or dy != 0:
                    draw.text((text_x + dx, text_y + dy), text, font=font, fill=(0, 0, 0, 255))
        draw.text((text_x, text_y), text, font=font, fill=(255, 255, 255, 255))
        
        out = Image.alpha_composite(img, watermark)
        output = io.BytesIO()
        out.convert("RGB").save(output, format="JPEG", quality=90)
        return output.getvalue()
    except Exception as e:
        logger.error(f"[Watermark] {e}")
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
        except TelegramForbiddenError:
            logger.info(f"[PHOTO] Пользователь {chat_id} заблокировал бота. Удаляем из базы.")
            db_query("DELETE FROM users WHERE user_id = ?", (chat_id,))
            db_query("DELETE FROM favorites WHERE user_id = ?", (chat_id,))
            return False
        except Exception: return False
            
    if not image_url: return False
    data = await download_image(image_url)
    if not data: return False
    data = apply_watermark(data)
    
    try:
        msg = await bot.send_photo(chat_id=chat_id, photo=BufferedInputFile(data, filename="game.jpg"), caption=caption, reply_markup=reply_markup, parse_mode=ParseMode.HTML)
        if product_id and msg.photo:
            db_query("UPDATE products SET tg_file_id = ? WHERE id = ?", (msg.photo[-1].file_id, product_id))
        return True
    except TelegramForbiddenError:
        db_query("DELETE FROM users WHERE user_id = ?", (chat_id,))
        db_query("DELETE FROM favorites WHERE user_id = ?", (chat_id,))
        return False
    except Exception: return False

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
                    if genres: result["genres"] = ",".join([g["description"] for g in genres])
    except Exception: pass
    return result

def _format_price(price_overview) -> tuple[str, str, str]:
    if not price_overview: return "", "", ""
    currency = price_overview.get("currency", "")
    final = price_overview.get("final", 0) / 100
    initial = price_overview.get("initial", 0) / 100
    discount = price_overview.get("discount_percent", 0)

    if currency == "RUB":
        final_str = f"{final:.0f} ₽"; original_str = f"{initial:.0f} ₽" if discount else ""; discount_str = f"-{discount}%" if discount else ""
    elif currency == "USD":
        final_str = f"${final:.2f}"; original_str = f"${initial:.2f}" if discount else ""; discount_str = f"-{discount}%" if discount else ""
    else:
        final_str = f"{final:.2f} {currency}"; original_str = f"{initial:.2f} {currency}" if discount else ""; discount_str = f"-{discount}%" if discount else ""
    return final_str, original_str, discount_str

async def fetch_steam_deals():
    logger.info("[PARSER] Начинаем парсинг Steam...")
    url = "https://store.steampowered.com/api/featuredcategories?cc=ru&l=russian"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200: return
                data = await resp.json()
    except Exception as e:
        logger.warning(f"[PARSER] {e}"); return

    added = 0
    async with aiohttp.ClientSession() as session:
        items_to_check = data.get("specials", {}).get("items", []) + data.get("top_sellers", {}).get("items", []) + data.get("new_releases", {}).get("items", [])
        for item in items_to_check:
            title, app_id = item.get("name"), item.get("id")
            if not title or not app_id: continue

            det = await _steam_get_details(session, app_id)
            if det["type"] != "game" or re.search(r'(soundtrack|ost|artbook|art book|dlc|expansion|season pass)', title, re.IGNORECASE) or "additional content for" in det["description"].lower(): continue
            if not det["price_overview"] or det["price_overview"].get("discount_percent", 0) == 0: continue

            shop_url = f"https://store.steampowered.com/app/{app_id}/"
            f_str, o_str, d_str = _format_price(det["price_overview"])
            price_usd = det["price_overview"].get("final_formatted", "") if det["price_overview"] else ""
            now_str = utcnow_iso()

            try:
                db_query("""
                    INSERT INTO products (title, price, old_price, discount, url, image, description, price_usd, genres, last_updated)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(title) DO UPDATE SET
                        price=excluded.price, old_price=excluded.old_price, discount=excluded.discount,
                        image=excluded.image, description=excluded.description, genres=excluded.genres, last_updated=excluded.last_updated
                """, (title, f_str or "Бесплатно", o_str, d_str, shop_url, det["image"], det["description"], price_usd, det["genres"], now_str))
                added += 1
            except Exception as e:
                logger.error(f"[PARSER] Ошибка вставки «{title}»: {e}")
            await asyncio.sleep(0.2)
    logger.info(f"[PARSER] Обновлено/добавлено игр: {added}")

# ---------- ЕЖЕДНЕВНАЯ СКИДКА ----------
async def send_daily_deal(bot: Bot):
    users = [row[0] for row in db_query("SELECT user_id FROM users", fetch=True)]
    if not users: return
    pool = db_query("SELECT id, title, price, old_price, discount, url, image, description FROM products WHERE image IS NOT NULL LIMIT 500", fetch=True)
    if not pool: return
    
    sent, blocked = 0, 0
    for uid in users:
        pid, title, price, old_price, discount, url, image, description = random.choice(pool)
        text = f"🎲 <b>Скидка дня: {html.escape(title)}</b>\n\n"
        if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
        if price: text += f"💵 Цена: <b>{price}</b>\n"
        if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
        if description: text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)], [InlineKeyboardButton(text="🎲 Ещё", callback_data="random_again")]])
        ok = await send_game_photo(bot, uid, pid, image, text, kb)
        if not ok:
            try:
                await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                sent += 1
            except Exception:
                db_query("DELETE FROM users WHERE user_id = ?", (uid,))
                blocked += 1
        else: sent += 1
        await asyncio.sleep(0.07)
    logger.info(f"[DAILY] Отправлено: {sent}, заблокировали: {blocked}")

# ---------- СТАТИСТИКА ----------
async def send_daily_stats(bot: Bot):
    total_users = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    total_products = db_query("SELECT COUNT(*) FROM products", fetchone=True)[0]
    total_favorites = db_query("SELECT COUNT(*) FROM favorites", fetchone=True)[0]
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).replace(tzinfo=None).isoformat()
    new_users = db_query("SELECT COUNT(*) FROM users WHERE joined_at > ?", (yesterday,), fetchone=True)[0]
    premium_users = db_query("SELECT COUNT(*) FROM users WHERE premium_until > ?", (utcnow_iso(),), fetchone=True)[0]
    revenue_row = db_query("SELECT SUM(amount) FROM payments WHERE status = 'confirmed' AND created_at > ?", (yesterday,), fetchone=True)
    revenue_24h = revenue_row[0] if revenue_row and revenue_row[0] else 0
    clicks_steam = db_query("SELECT COUNT(*) FROM link_clicks WHERE link_type LIKE 'steam%' AND clicked_at > ?", (yesterday,), fetchone=True)[0]
    clicks_mobile = db_query("SELECT COUNT(*) FROM link_clicks WHERE link_type LIKE 'mobile%' AND clicked_at > ?", (yesterday,), fetchone=True)[0]
    new_refs = db_query("SELECT COUNT(*) FROM users WHERE referred_by IS NOT NULL AND joined_at > ?", (yesterday,), fetchone=True)[0]
    
    text = (
        f"📊 <b>ЕЖЕДНЕВНАЯ СТАТИСТИКА БОТА</b>\n📅 {datetime.now(timezone.utc).strftime('%d.%m.%Y')}\n\n"
        f"👥 <b>Пользователи:</b>\n  • Всего: <b>{total_users}</b>\n  • Новых за 24ч: <b>+{new_users}</b>\n  • Премиум: <b>{premium_users}</b>\n  • Пришло по рефке: <b>{new_refs}</b>\n\n"
        f"💰 <b>Финансы:</b>\n  • Заработано за 24ч: <b>{revenue_24h} ⭐</b>\n\n"
        f"📈 <b>Активность (за 24ч):</b>\n  • Кликов Steam: <b>{clicks_steam}</b>\n  • Кликов Mobile: <b>{clicks_mobile}</b>\n  • В избранном: <b>{total_favorites}</b> игр\n  • В базе: <b>{total_products}</b> игр\n\n"
        f"<i>Отчет сформирован автоматически.</i>"
    )
    try: await bot.send_message(chat_id=REVIEW_CHAT_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception as e: logger.error(f"[STATS] {e}")

# ---------- УВЕДОМЛЕНИЯ ----------
async def notify_favorites_matches(bot: Bot):
    rows = db_query("""
        SELECT f.user_id, p.id, p.title, p.price, p.discount, p.url, p.image, p.description, u.premium_until, u.last_daily_bonus
        FROM favorites f JOIN products p ON p.id = f.product_id JOIN users u ON u.user_id = f.user_id 
        WHERE p.discount IS NOT NULL AND p.discount != '' 
        AND NOT EXISTS (SELECT 1 FROM notified_favorites nf WHERE nf.user_id = f.user_id AND nf.product_id = p.id)
        LIMIT 50
    """, fetch=True)
    
    for uid, pid, title, price, discount, url, image, desc, prem_until, last_bonus in rows:
        is_prem = bool(prem_until and parse_dt(prem_until) > utcnow())
        if not is_prem:
            if last_bonus and (utcnow() - parse_dt(last_bonus)).days < 7: continue
            db_query("UPDATE users SET last_daily_bonus = ? WHERE user_id = ?", (utcnow_iso(), uid))
        
        text = f"⭐ <b>Игра из Избранного в скидке!</b>\n\n🎮 {html.escape(title)}\n💵 {price}\n🔥 {discount}\n\n📝 <i>{safe_html_truncate(desc or '', 100)}...</i>"
        if not is_prem: text += "\n\n💎 <i>Купи Премиум для мгновенных алертов!</i>"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
        ok = await send_game_photo(bot, uid, pid, image, text, kb)
        if not ok:
            try: await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception: continue
        db_query("INSERT OR IGNORE INTO notified_favorites (user_id, product_id) VALUES (?, ?)", (uid, pid))
        await asyncio.sleep(0.05)

async def notify_premium_super_deals(bot: Bot):
    super_deals = db_query("SELECT p.id, p.title, p.price, p.discount, p.url FROM products p LEFT JOIN notified_deals n ON n.product_id = p.id WHERE CAST(REPLACE(p.discount, '-', '') AS INTEGER) >= 80 AND p.discount IS NOT NULL AND n.product_id IS NULL LIMIT 5", fetch=True)
    if not super_deals: return
    premium_users = db_query("SELECT user_id FROM users WHERE premium_until > ?", (utcnow_iso(),), fetch=True)
    
    digest_text = "🔥 <b>СУПЕР-СКИДКИ ДНЯ (80%+)</b>\n\n"
    for i, (pid, title, price, discount, url) in enumerate(super_deals, 1):
        digest_text += f"{i}. {html.escape(title)} — <b>{price}</b> ({discount})\n"
    
    kb_rows = [[InlineKeyboardButton(text=f"🛒 {t[:30]}...", url=u)] for _, t, _, _, u in super_deals]
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    
    for (uid,) in premium_users:
        try: await bot.send_message(chat_id=uid, text=digest_text, reply_markup=kb, parse_mode=ParseMode.HTML)
        except Exception: pass
        await asyncio.sleep(0.05)
    
    for pid, *_ in super_deals:
        db_query("INSERT OR IGNORE INTO notified_deals (product_id, notified_at) VALUES (?, ?)", (pid, utcnow_iso()))

# ---------- FAZERCARDS API ----------
async def fazer_request(method: str, endpoint: str, data: dict = None) -> dict | None:
    if not FAZER_API_KEY: return None
    url = f"{FAZER_API_BASE}{endpoint}"
    headers = {"X-API-Key": FAZER_API_KEY, "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession() as session:
            if method == "GET":
                async with session.get(url, headers=headers, timeout=15) as resp:
                    if resp.status != 200: return None
                    return await resp.json()
            elif method == "POST":
                async with session.post(url, headers=headers, json=data, timeout=15) as resp:
                    if resp.status != 200: return None
                    return await resp.json()
    except Exception as e:
        logger.error(f"[FAZER] Ошибка запроса {endpoint}: {e}")
        return None

async def fazer_get_rates() -> dict | None:
    return await fazer_request("GET", "/steam-topup/rates")

async def fazer_check_login(login: str) -> bool:
    res = await fazer_request("POST", "/steam-topup/check-login", {"steamLogin": login})
    return res.get("can_refill", False) if res and res.get("ok") else False

async def fazer_get_balance() -> float | None:
    res = await fazer_request("GET", "/balance")
    return float(res.get("balance", 0)) if res and res.get("ok") else None

async def fazer_create_order(login: str, currency: str, amount: float) -> dict | None:
    res = await fazer_request("POST", "/steam-topup/order", {"steamLogin": login, "currency": currency, "amount": amount})
    return res.get("order") if res and res.get("ok") else None

# ---------- КЛАВИАТУРЫ ----------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔥 Скидки"), KeyboardButton(text="🎲 Случайная игра"), KeyboardButton(text="💳 Пополнить Steam (Авто)")],
            [KeyboardButton(text="📱 Мобильные игры"), KeyboardButton(text="💎 Премиум"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="🔍 Поиск"), KeyboardButton(text="⭐ Оставить отзыв"), KeyboardButton(text="🆘 Поддержка")],
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
            [KeyboardButton(text="💰 Баланс FazerCards"), KeyboardButton(text="🧪 Тест скидки дня")],
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
class FavoritesFSM(StatesGroup): waiting_title = State()
class TopUpFSM(StatesGroup):
    waiting_login = State()
    waiting_amount = State()
class SteamTopUpFSM(StatesGroup):
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
    waiting_expires = State()
class UserPromoFSM(StatesGroup): waiting_code = State()
class ReviewFSM(StatesGroup): waiting_text = State()
class SearchFSM(StatesGroup): waiting_query = State()

# ---------- ERROR HANDLER ----------
async def errors_handler(event, exception: Exception = None):
    if exception is None and hasattr(event, 'exception'): exception = event.exception
    logger.exception(f"[ERROR] {type(exception).__name__}: {exception}")
    try:
        if hasattr(event, 'message') and event.message: await event.message.answer("⚠️ Ошибка. Попробуйте позже.")
        elif hasattr(event, 'callback_query') and event.callback_query: await event.callback_query.answer("⚠️ Ошибка.", show_alert=True)
    except Exception: pass
    return True

# ---------- ГЛОБАЛЬНЫЙ /CANCEL ----------
@router.message(Command("cancel"))
async def global_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Действие отменено.", reply_markup=main_menu_kb())

# ---------- /HELP ----------
@router.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "🆘 <b>Помощь</b>\n\n"
        "🔥 <b>Скидки</b> — актуальные скидки в Steam\n"
        "🎲 <b>Случайная игра</b> — рулетка\n"
        "🔍 <b>Поиск</b> — найти игру\n"
        "💳 <b>Пополнить Steam (Авто)</b> — автоматическое пополнение через FazerCards\n"
        "📱 <b>Мобильные игры</b> — донат\n"
        "💎 <b>Премиум</b> — подписка\n"
        "👤 <b>Профиль</b> — статистика, избранное\n"
        "⭐ <b>Оставить отзыв</b> — оценка бота\n\n"
        "Команды:\n"
        "/start, /help, /cancel, /admin",
        reply_markup=main_menu_kb()
    )

# ---------- СТАРТ ----------
@router.message(CommandStart())
async def start_cmd(message: Message, command: CommandObject):
    if _db_conn is None: return
    referrer_id = None
    if command.args and command.args.startswith("ref_"):
        try: referrer_id = int(command.args[4:])
        except ValueError: pass

    with _db_lock:
        try:
            cur = _db_conn.cursor()
            cur.execute("INSERT OR IGNORE INTO users (user_id, username, first_name, referred_by) VALUES (?, ?, ?, ?)", (message.from_user.id, message.from_user.username, message.from_user.first_name, referrer_id))
            is_new = cur.rowcount > 0
            _db_conn.commit()
        except Exception: is_new = False

    if is_new:
        if referrer_id and referrer_id != message.from_user.id:
            if db_query("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,), fetchone=True):
                add_premium_days(referrer_id, REF_BONUS_DAYS)
                db_query("UPDATE users SET ref_bonus_claimed = 1 WHERE user_id = ?", (message.from_user.id,))
                try: await message.bot.send_message(referrer_id, f"🎉 <b>По твоей ссылке пришел друг!</b>\n\n+{REF_BONUS_DAYS} дня Премиума! 👑")
                except Exception: pass
    else:
        db_query("UPDATE users SET username=?, first_name=? WHERE user_id=?", (message.from_user.username, message.from_user.first_name, message.from_user.id))

    await message.answer(f"👋 Привет, {html.escape(message.from_user.first_name or 'друг')}!\n\n🐷 Я бот для выгодных покупок.\nВыбирай раздел ниже 👇", reply_markup=main_menu_kb())

# ---------- ПОИСК ----------
@router.message(F.text == "🔍 Поиск")
async def search_start(message: Message, state: FSMContext):
    uid = message.from_user.id; now = time.time()
    if now - SEARCH_COOLDOWN.get(uid, 0) < SEARCH_COOLDOWN_SEC:
        await message.answer(f"⏳ Подожди {int(SEARCH_COOLDOWN_SEC - (now - SEARCH_COOLDOWN.get(uid, 0)))} сек."); return
    SEARCH_COOLDOWN[uid] = now
    await state.set_state(SearchFSM.waiting_query)
    await message.answer("🔍 Введи название игры:\n\n<i>Напиши «отмена», чтобы выйти.</i>", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True))

@router.message(SearchFSM.waiting_query, F.text.in_(["⬅️ Отмена", "отмена", "Отмена", "/cancel"]))
async def search_cancel(message: Message, state: FSMContext):
    await state.clear(); await message.answer("❌ Поиск отменен.", reply_markup=main_menu_kb())

@router.message(SearchFSM.waiting_query)
async def search_results(message: Message, state: FSMContext):
    query = (message.text or "").strip()
    if not query: return
    rows = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE title LIKE ? ESCAPE '\\' LIMIT 10", (f"%{escape_like(query)}%",), fetch=True)
    if not rows: await message.answer("Ничего не найдено.", reply_markup=main_menu_kb()); await state.clear(); return
    await state.clear(); await message.answer(f"🔍 Найдено: {len(rows)}", reply_markup=main_menu_kb()); await send_product_card(message, rows, 0, 0, len(rows))

# ---------- ПРОФИЛЬ / ЖАНРЫ / БОНУС ----------
@router.message(F.text == "👤 Профиль")
async def profile_menu(message: Message):
    uid = message.from_user.id; is_prem = check_premium(uid); until = get_premium_until(uid)
    refs = db_query("SELECT COUNT(*) FROM users WHERE referred_by = ?", (uid,), fetchone=True)[0]
    text = f"👤 <b>Твой профиль</b>\n\n🆔 ID: <code>{uid}</code>\n"
    if is_prem and until: text += f"👑 Премиум до {until.strftime('%d.%m.%Y')}\n"
    else: text += "💎 Обычный\n"
    text += f"\n⭐ Избранное: {favorites_count(uid)} / {favorites_limit(uid)}\n🎮 Жанры: {', '.join(get_user_genres(uid)) or 'нет'}\n👥 Рефералы: {refs} (+{refs*REF_BONUS_DAYS} дн.)"
    
    buttons = [
        [InlineKeyboardButton(text="🎁 Ежедневный бонус", callback_data="daily_bonus")],
        [InlineKeyboardButton(text="🎁 Пригласить друга", callback_data="show_ref_link")],
        [InlineKeyboardButton(text="⭐ Избранное", callback_data="fav_show")],
        [InlineKeyboardButton(text="🎮 Жанры", callback_data="genres_show")],
        [InlineKeyboardButton(text="❤️ Поддержать", url=DONATE_URL)],
    ]
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

@router.callback_query(F.data == "daily_bonus")
async def daily_bonus(cb: CallbackQuery):
    uid = cb.from_user.id; now = utcnow(); today = now.date().isoformat()
    res = db_query("SELECT last_daily_bonus, daily_bonus_streak FROM users WHERE user_id = ?", (uid,), fetchone=True)
    last, streak = res if res else (None, 0)
    if last and parse_dt(last).date().isoformat() == today: await cb.answer("Уже получен!", show_alert=True); return
    streak = streak + 1 if (last and (now - parse_dt(last)).days == 1) else 1
    db_query("UPDATE users SET last_daily_bonus = ?, daily_bonus_streak = ? WHERE user_id = ?", (now.isoformat(), streak, uid))
    if streak >= 7:
        add_premium_days(uid, 1); db_query("UPDATE users SET daily_bonus_streak = 0 WHERE user_id = ?", (uid,))
        await cb.message.answer("🎉 +1 день Премиума за 7 дней подряд!"); await cb.answer("Получено!")
    else:
        await cb.message.answer(f"🎁 Бонус получен! Стрик: {streak}/7"); await cb.answer("Ок!")

@router.callback_query(F.data == "genres_show")
async def genres_show(cb: CallbackQuery):
    uid = cb.from_user.id; limit = GENRES_LIMIT_PREMIUM if check_premium(uid) else GENRES_LIMIT_FREE; cur = get_user_genres(uid)
    text = f"🎮 <b>Жанры для рулетки</b> (Выбрано: {len(cur)}/{limit})"
    buttons, row = [], []
    for g in AVAILABLE_GENRES:
        row.append(InlineKeyboardButton(text=f"{'✅ ' if g in cur else ''}{g}", callback_data=f"genre_toggle_{g}"))
        if len(row) == 2: buttons.append(row); row = []
    if row: buttons.append(row)
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="profile_back")])
    try: await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    except Exception: pass
    await cb.answer()

@router.callback_query(F.data.startswith("genre_toggle_"))
async def genre_toggle(cb: CallbackQuery):
    uid = cb.from_user.id; g = cb.data.split("_", 2)[2]
    if toggle_user_genre(uid, g) == "limit": await cb.answer("Лимит жанров!", show_alert=True); return
    await genres_show(cb)

@router.callback_query(F.data == "profile_back")
async def profile_back(cb: CallbackQuery): await cb.answer(); await profile_menu(cb.message)

@router.callback_query(F.data == "show_ref_link")
async def show_ref_link(cb: CallbackQuery):
    bot_info = await cb.bot.get_me()
    await cb.message.answer(f"🔗 <b>Твоя ссылка:</b>\n<code>https://t.me/{bot_info.username}?start=ref_{cb.from_user.id}</code>", parse_mode=ParseMode.HTML); await cb.answer()

# ---------- ИЗБРАННОЕ ----------
@router.callback_query(F.data.startswith("fav_add_"))
async def fav_add(cb: CallbackQuery):
    uid = cb.from_user.id; pid = int(cb.data.split("_")[2])
    if favorites_count(uid) >= favorites_limit(uid): await cb.answer("Лимит!", show_alert=True); return
    db_query("INSERT OR IGNORE INTO favorites (user_id, product_id) VALUES (?, ?)", (uid, pid))
    await cb.answer("✅ Добавлено в избранное!")
    prods = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE id = ?", (pid,), fetch=True)
    if prods: await send_product_card(cb, prods, 0, 0, 1)

@router.callback_query(F.data == "fav_already")
async def fav_already(cb: CallbackQuery): await cb.answer("Уже в избранном!")

@router.callback_query(F.data == "fav_show")
async def fav_show(cb: CallbackQuery):
    uid = cb.from_user.id; rows = db_query("SELECT p.id, p.title FROM favorites f JOIN products p ON p.id = f.product_id WHERE f.user_id = ?", (uid,), fetch=True)
    if not rows: await cb.answer("Пусто", show_alert=True); return
    text = f"⭐ <b>Избранное</b> ({len(rows)} / {favorites_limit(uid)}):\n\n"
    buttons = [[InlineKeyboardButton(text=f"❌ {t[:20]}", callback_data=f"fav_del_{p}")] for p, t in rows]
    await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await cb.answer()

@router.callback_query(F.data.startswith("fav_del_"))
async def fav_del(cb: CallbackQuery):
    db_query("DELETE FROM favorites WHERE product_id = ? AND user_id = ?", (int(cb.data.split("_")[2]), cb.from_user.id))
    await cb.answer("Удалено"); await fav_show(cb)

# ---------- РУЛЕТКА ----------
@router.message(F.text == "🎲 Случайная игра")
async def random_game(message: Message):
    uid = message.from_user.id; is_prem = check_premium(uid)
    if not is_prem and db_query("SELECT last_roulette FROM users WHERE user_id = ?", (uid,), fetchone=True)[0].startswith(utcnow().date().isoformat()):
        await message.answer("⏳ Лимит рулетки!"); return
    
    games = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL ORDER BY RANDOM() LIMIT 1", fetchone=True)
    if not games: return
    if not is_prem: db_query("UPDATE users SET last_roulette = ? WHERE user_id = ?", (utcnow_iso(), uid))
    pid, title, price, old_price, discount, url, image, description, price_usd = games
    text = f"🎲 <b>Тебе выпала: {html.escape(title)}</b>\n\n"
    if price: text += f"💵 {price}\n"
    if discount: text += f"🔥 {discount}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
    await send_game_photo(message.bot, message.chat.id, pid, image, text, kb)

@router.callback_query(F.data == "random_again")
async def random_again(cb: CallbackQuery):
    uid = cb.from_user.id; is_prem = check_premium(uid)
    if not is_prem and utcnow() - parse_dt(db_query("SELECT last_random_again FROM users WHERE user_id = ?", (uid,), fetchone=True)[0]) < timedelta(minutes=10): await cb.answer("Жди 10 мин", show_alert=True); return
    
    game = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL ORDER BY RANDOM() LIMIT 1", fetchone=True)
    await cb.answer()
    if not game: return
    if not is_prem: db_query("UPDATE users SET last_random_again = ? WHERE user_id = ?", (utcnow_iso(), uid))
    pid, title, price, old_price, discount, url, image, description, price_usd = game
    text = f"🎲 <b>Тебе выпала: {html.escape(title)}</b>\n\n"
    if price: text += f"💵 {price}\n"
    if discount: text += f"🔥 {discount}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
    await send_game_photo(cb.bot, cb.message.chat.id, pid, image, text, kb)

# ---------- АВТО-ПОПОЛНЕНИЕ STEAM (FAZERCARDS) ----------
@router.message(F.text == "💳 Пополнить Steam (Авто)")
async def steam_topup_start(message: Message, state: FSMContext):
    if not FAZER_API_KEY: await message.answer("❌ Авто-пополнение недоступно (нет ключа)."); return
    bal = await fazer_get_balance()
    if bal is None or bal < 1: await message.answer("❌ Ошибка баланса FazerCards."); return
    await state.set_state(SteamTopUpFSM.waiting_login)
    await message.answer("💳 <b>Пополнение Steam (Авто)</b>\n\nВведи <b>логин Steam</b>:\n\n<i>Напиши «отмена», чтобы выйти.</i>", reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="⬅️ Отмена")]], resize_keyboard=True))

@router.message(SteamTopUpFSM.waiting_login)
async def steam_topup_login(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"): await state.clear(); await message.answer("❌ Отменено.", reply_markup=main_menu_kb()); return
    if len(raw) < 3 or " " in raw: await message.answer("⚠️ Некорректный логин."); return
    msg = await message.answer("⏳ Проверяю логин...")
    if not await fazer_check_login(raw): await msg.edit_text(f"❌ Логин <b>{html.escape(raw)}</b> не найден."); return
    await msg.edit_text(f"✅ Логин <b>{html.escape(raw)}</b> найден!\n\nВведи <b>сумму в рублях</b> (только цифры):")
    await state.update_data(login=raw); await state.set_state(SteamTopUpFSM.waiting_amount)

@router.message(SteamTopUpFSM.waiting_amount)
async def steam_topup_amount(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if raw.lower() in ("отмена", "cancel", "/cancel"): await state.clear(); await message.answer("❌ Отменено.", reply_markup=main_menu_kb()); return
    if not is_ascii_digits(raw): await message.answer("⚠️ Только цифры."); return
    amount_rub = int(raw)
    if amount_rub < 15: await message.answer("⚠️ Минимум 15 ₽."); return
    if amount_rub > 80000: await message.answer("⚠️ Максимум 80 000 ₽."); return
    
    rates = await fazer_get_rates()
    if not rates: await message.answer("❌ Ошибка курса."); return
    rate_rub = rates.get("rates", {}).get("RUB", 95)
    amount_usd = round(amount_rub / rate_rub, 2)
    
    total_rub = int(amount_rub * (1 + FAZER_MARKUP_PERCENT / 100))
    stars_price = int(total_rub / STARS_TO_RUB)
    if stars_price < 1: stars_price = 1
    
    await state.update_data(amount_usd=amount_usd, amount_rub=amount_rub, stars_price=stars_price)
    text = f"💳 <b>Заказ</b>\n👤 {html.escape((await state.get_data()).get('login'))}\n💵 {amount_rub} ₽ (≈ ${amount_usd})\n💰 Итого: {total_rub} ₽ (≈ {stars_price} ⭐)"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=f"💳 Оплатить {stars_price} ⭐", callback_data="steam_topup_pay")], [InlineKeyboardButton(text="❌ Отмена", callback_data="steam_topup_cancel")]])
    await message.answer(text, reply_markup=kb)

@router.callback_query(F.data == "steam_topup_pay")
async def steam_topup_pay(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    login = data.get("login"); amount_usd = data.get("amount_usd"); stars_price = data.get("stars_price")
    if not login or not amount_usd or not stars_price: await cb.answer("Ошибка. Начни заново.", show_alert=True); await state.clear(); return
    prices = [LabeledPrice(label=f"Steam {login}", amount=stars_price)]
    try:
        await cb.bot.send_invoice(chat_id=cb.from_user.id, title=f"Пополнение Steam", description=f"Пополнение {login} на {amount_usd} USD", payload=f"steam_topup|{login}|{amount_usd}", provider_token="", currency="XTR", prices=prices, start_parameter="steam")
        await cb.answer("Счет выставлен!")
    except Exception as e: await cb.answer("Ошибка.", show_alert=True)
    await state.clear()

@router.callback_query(F.data == "steam_topup_cancel")
async def steam_topup_cancel(cb: CallbackQuery, state: FSMContext): await state.clear(); await cb.message.edit_text("❌ Отменено."); await cb.answer()

# ---------- УСПЕШНАЯ ОПЛАТА ----------
@router.message(F.successful_payment)
async def successful_payment_handler(message: Message, bot: Bot):
    payload = message.successful_payment.invoice_payload
    if payload.startswith("steam_topup|"):
        parts = payload.split("|")
        login = parts[1]; amount_usd = float(parts[2])
        await message.answer("⏳ Оплата получена! Создаю заказ на FazerCards...")
        order = await fazer_create_order(login, "USD", amount_usd)
        if order: await message.answer(f"✅ <b>Успешно!</b>\n🆔 {order.get('id')}\n👤 {login}\n💵 ${amount_usd}\n\nСредства поступят в течение 15 минут.")
        else: await message.answer(f"⚠️ Ошибка FazerCards. Напишите в поддержку {PLATI_SUPPORT} чек: {message.successful_payment.telegram_payment_charge_id}")
        try: db_query("INSERT INTO payments (payment_id, user_id, amount, days, status) VALUES (?, ?, ?, 0, 'confirmed')", (message.successful_payment.telegram_payment_charge_id, message.from_user.id, message.successful_payment.total_amount))
        except Exception: pass
        return
    try: days = int(payload.split("_")[1])
    except: days = 7
    add_premium_days(message.from_user.id, days)
    try: db_query("INSERT INTO payments (payment_id, user_id, amount, days, status) VALUES (?, ?, ?, ?, 'confirmed')", (message.successful_payment.telegram_payment_charge_id, message.from_user.id, message.successful_payment.total_amount, days))
    except Exception: pass
    await message.answer(f"✅ <b>Оплата успешна!</b>\nНачислено {days} дней Премиума!", reply_markup=main_menu_kb())

@router.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery): await pre_checkout_q.answer(ok=True)

# ---------- ПРОЧЕЕ (ПОДДЕРЖКА, СКИДКИ, ПРЕМИУМ) ----------
@router.message(F.text == "🆘 Поддержка")
async def support_info(message: Message): await message.answer(f"🆘 Поддержка: {CONTACT_TG}", reply_markup=main_menu_kb())

@router.message(F.text == "🔥 Скидки")
async def discounts_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 90%+", callback_data="filter_90"), InlineKeyboardButton(text="💸 До 100₽", callback_data="filter_cheap")],
        [InlineKeyboardButton(text="🆕 Новинки", callback_data="filter_new"), InlineKeyboardButton(text="📜 Все", callback_data="filter_all")],
    ])
    await message.answer("🔥 <b>Скидки в Steam</b>\n\nВыбери фильтр:", reply_markup=kb)

@router.callback_query(F.data.startswith("filter_"))
async def filter_products(cb: CallbackQuery):
    f = cb.data.split("_", 1)[1]
    if f == "90": products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE CAST(REPLACE(discount, '-', '') AS INTEGER) >= 90 AND image IS NOT NULL ORDER BY id DESC LIMIT ?", (PAGE_LIMIT,), fetch=True)
    elif f == "cheap": products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE price LIKE '%₽' AND CAST(REPLACE(price, ' ₽', '') AS INTEGER) <= 100 AND image IS NOT NULL ORDER BY id DESC LIMIT ?", (PAGE_LIMIT,), fetch=True)
    elif f == "new": products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL ORDER BY created_at DESC LIMIT ?", (PAGE_LIMIT,), fetch=True)
    else: products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL ORDER BY id DESC LIMIT ?", (PAGE_LIMIT,), fetch=True)
    if not products: await cb.answer("Ничего не найдено.", show_alert=True); return
    await cb.answer(f"Найдено: {len(products)}")
    await send_product_card(cb, products, 0, 0, len(products))

async def send_product_card(message_or_callback, products, index: int, offset: int, total: int):
    pid, title, price, old_price, discount, url, image, description, price_usd = products[index]
    text = f"🎮 <b>{html.escape(title)}</b>\n\n"
    if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price: text += f"💵 Цена: <b>{price}</b>\n"
    if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description: text += f"\n📝 <i>{safe_html_truncate(description, 200)}...</i>\n"
    text += f"\n📄 Игра {offset + index + 1} из {total}"
    
    uid = message_or_callback.from_user.id
    is_fav = db_query("SELECT 1 FROM favorites WHERE user_id = ? AND product_id = ?", (uid, pid), fetchone=True)
    nav_row = []
    if offset + index + 1 > 1: nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"page_{offset + index - 1}"))
    if offset + index + 1 < total: nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"page_{offset + index + 1}"))
    buttons = []
    if nav_row: buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🛒 Купить", url=url), InlineKeyboardButton(text="📤 Поделиться", url=f"https://t.me/share/url?url={url}&text={html.escape(title)}")])
    if is_fav: buttons.append([InlineKeyboardButton(text="✅ В Избранном", callback_data="fav_already")])
    else: buttons.append([InlineKeyboardButton(text="⭐ В избранное", callback_data=f"fav_add_{pid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    if not isinstance(message_or_callback, CallbackQuery):
        if not await send_game_photo(message_or_callback.bot, message_or_callback.chat.id, pid, image, text, kb): await message_or_callback.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        return
    cb = message_or_callback
    try: await cb.answer()
    except Exception: pass
    msg = cb.message
    if msg:
        try:
            if msg.photo:
                row = db_query("SELECT tg_file_id FROM products WHERE id = ?", (pid,), fetchone=True)
                media = row[0] if row and row[0] else None
                if media is None:
                    data = await download_image(image)
                    if data: media = BufferedInputFile(apply_watermark(data), filename="game.jpg")
                if media:
                    await msg.edit_media(media=InputMediaPhoto(media=media, caption=text, parse_mode=ParseMode.HTML), reply_markup=kb)
                    return
            else:
                await msg.edit_text(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                return
        except TelegramBadRequest: pass
        if not await send_game_photo(cb.bot, msg.chat.id, pid, image, text, kb):
            try: await cb.bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            except Exception: return
        try: await msg.delete()
        except Exception: pass

@router.callback_query(F.data.startswith("page_"))
async def paginate_products(cb: CallbackQuery):
    try: g_idx = int(cb.data.split("_", 1)[1])
    except (IndexError, ValueError): return
    offset = (g_idx // PAGE_LIMIT) * PAGE_LIMIT
    idx = g_idx - offset
    products = db_query("SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image IS NOT NULL ORDER BY id DESC LIMIT ? OFFSET ?", (PAGE_LIMIT, offset), fetch=True)
    total = db_query("SELECT COUNT(*) FROM products WHERE image IS NOT NULL", fetchone=True)[0]
    if 0 <= idx < len(products): await send_product_card(cb, products, idx, offset, total)

@router.message(F.text == "💎 Премиум")
async def premium_menu(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔥 7 дней — {STARS_7_DAYS} ⭐", callback_data="buy_premium_7")],
        [InlineKeyboardButton(text=f"👑 30 дней — {STARS_30_DAYS} ⭐", callback_data="buy_premium_30")],
        [InlineKeyboardButton(text=f"💎 6 мес — {STARS_180_DAYS} ⭐", callback_data="buy_premium_180")],
        [InlineKeyboardButton(text=f"🏆 1 год — {STARS_365_DAYS} ⭐", callback_data="buy_premium_365")],
    ])
    await message.answer("💎 <b>Премиум</b>\n\nБезлимитная рулетка, 30 избранных, мгновенные алерты!\nВыбирай тариф:", reply_markup=kb)

@router.callback_query(F.data.startswith("buy_premium_"))
async def buy_premium(cb: CallbackQuery):
    days = int(cb.data.split("_")[2]); price = {7: STARS_7_DAYS, 30: STARS_30_DAYS, 180: STARS_180_DAYS, 365: STARS_365_DAYS}.get(days)
    if not price: await cb.answer("Ошибка", show_alert=True); return
    try:
        await cb.bot.send_invoice(chat_id=cb.from_user.id, title=f"Премиум на {days} дней", description=f"Активация Премиум", payload=f"premium_{days}", provider_token="", currency="XTR", prices=[LabeledPrice(label="Премиум", amount=price)], start_parameter="premium")
        await cb.answer("Счет выставлен!")
    except Exception: await cb.answer("Ошибка.", show_alert=True)

# ---------- АДМИНКА ----------
@admin_router.message(Command("admin"))
async def admin_cmd(message: Message): await message.answer("🛠 Админка", reply_markup=admin_menu_kb())

@admin_router.message(F.text == "⬅️ В меню")
async def to_main(message: Message): await message.answer("Меню", reply_markup=main_menu_kb())

@admin_router.message(F.text == "🔄 Спарсить скидки")
async def manual_parse(message: Message): await message.answer("Парсим..."); await fetch_steam_deals(); await message.answer("Готово!", reply_markup=admin_menu_kb())

@admin_router.message(F.text == "🗑 Очистить базу")
async def clear_db(message: Message): db_query("DELETE FROM products"); await message.answer("Очищено.", reply_markup=admin_menu_kb())

@admin_router.message(F.text == "💰 Баланс FazerCards")
async def fazer_balance(message: Message):
    bal = await fazer_get_balance()
    if bal is None: await message.answer("Ошибка ключа/сети.")
    else: await message.answer(f"💰 Баланс FazerCards: <b>${bal:.2f} USD</b>", reply_markup=admin_menu_kb())

@admin_router.message(F.text == "📊 Статистика")
async def stats(message: Message):
    u = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]; p = db_query("SELECT COUNT(*) FROM products", fetchone=True)[0]; w = db_query("SELECT COUNT(*) FROM favorites", fetchone=True)[0]
    await message.answer(f"👥 Юзеров: {u}\n🎮 Игр: {p}\n⭐ Избранное: {w}", reply_markup=admin_menu_kb())

@admin_router.message(F.text == "📢 Рассылка")
async def admin_broadcast(message: Message, state: FSMContext):
    await state.set_state(AdminBroadcastFSM.waiting_message)
    await message.answer("Отправь текст/фото для рассылки (или 'отмена'):")

@admin_router.message(AdminBroadcastFSM.waiting_message)
async def bc_preview(message: Message, state: FSMContext, bot: Bot):
    if message.text and message.text.lower() in ("отмена", "cancel"): await state.clear(); await message.answer("Отмена.", reply_markup=admin_menu_kb()); return
    p_id = message.photo[-1].file_id if message.photo else None; txt = message.text if message.text else message.caption
    await state.update_data(pid=p_id, txt=txt); await state.set_state(AdminBroadcastFSM.waiting_confirm)
    await message.answer("Отправить?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data="bc_yes")], [InlineKeyboardButton(text="❌ Нет", callback_data="bc_no")]]))

@admin_router.callback_query(F.data == "bc_no")
async def bc_no(cb: CallbackQuery, state: FSMContext): await state.clear(); await cb.message.edit_text("Отменено."); await cb.answer()

@admin_router.callback_query(F.data == "bc_yes")
async def bc_yes(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data(); p = data.get("pid"); t = data.get("txt"); await state.clear()
    users = db_query("SELECT user_id FROM users", fetch=True); sent = 0
    await cb.message.edit_text(f"Рассылка на {len(users)}...")
    for (uid,) in users:
        try:
            if p: await bot.send_photo(uid, p, caption=t)
            else: await bot.send_message(uid, t)
            sent += 1
        except Exception: pass
        await asyncio.sleep(0.05)
    await cb.message.edit_text(f"Отправлено: {sent}"); await cb.answer()

# ---------- ЗАПУСК ----------
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    dp.include_router(admin_router)
    dp.errors.register(errors_handler)

    scheduler = AsyncIOScheduler(timezone=timezone(timedelta(hours=3)))
    scheduler.add_job(fetch_steam_deals, "cron", hour=0, minute=0)
    scheduler.add_job(send_daily_stats, "cron", hour=12, minute=0, args=[bot])
    scheduler.add_job(send_daily_deal, "cron", hour=12, minute=5, args=[bot])
    scheduler.add_job(send_daily_deal, "cron", hour=19, minute=0, args=[bot])
    scheduler.add_job(notify_premium_super_deals, "interval", hours=3, args=[bot])
    scheduler.add_job(notify_favorites_matches, "interval", minutes=30, args=[bot])
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("Бот запущен с FazerCards API!")
    try: await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        if _db_conn: _db_conn.close()

if __name__ == "__main__":
    asyncio.run(main())
