import asyncio
import logging
import sqlite3
import uuid
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
from yookassa import Configuration, Payment

# ============ НАСТРОЙКИ ============
BOT_TOKEN = "8961086874:AAGkgetL7pD8znZWAUxsLDdcEAVm6kT646E"
ADMIN_IDS = [1043675714]
DB_PATH = "/data/bot.db"

YOOKASSA_SHOP_ID = "ТВОЙ_SHOP_ID"
YOOKASSA_SECRET_KEY = "ТВОЙ_SECRET_KEY"
YOOKASSA_RETURN_URL = "https://t.me/YourBot"

PRICE_7_DAYS = 200
PRICE_30_DAYS = 500

# --- PLATI.MARKET ---
PLATI_AGENT_ID = "1480092"
PLATI_SUPPORT = "@ObuzaMeduza"

# ID товара-калькулятора (Steam пополнение)
STEAM_TOPUP_ITEM_ID = "3961593"

# Мобильные игры (пока ведем на общий поиск)
PLATI_MOBILE_URL = f"https://plati.market/search/Донат?agent={PLATI_AGENT_ID}"
# ===================================

if YOOKASSA_SHOP_ID and YOOKASSA_SHOP_ID != "ТВОЙ_SHOP_ID":
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

logging.basicConfig(level=logging.INFO)
router = Router()
PAGE_LIMIT = 50
IMAGE_CACHE = {}

WISHLIST_LIMIT_FREE = 1
WISHLIST_LIMIT_PREMIUM = 10


# ---------- БАЗА ----------
def db_init():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, price TEXT NOT NULL, old_price TEXT, discount TEXT, url TEXT NOT NULL, image TEXT, description TEXT, price_usd TEXT, clicks INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, joined_at TEXT DEFAULT CURRENT_TIMESTAMP, premium_until TEXT, last_roulette TEXT);
    CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, payment_id TEXT UNIQUE, user_id INTEGER, amount INTEGER, days INTEGER, status TEXT DEFAULT 'pending', created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS notified_deals (product_id INTEGER PRIMARY KEY, notified_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS wishlist (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product_id INTEGER, UNIQUE(user_id, product_id));
    CREATE TABLE IF NOT EXISTS wishlist_titles (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, added_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS link_clicks (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, link_type TEXT, clicked_at TEXT DEFAULT CURRENT_TIMESTAMP);
    """)
    con.commit()
    con.close()


def db_query(sql, params=(), fetch=False, fetchone=False):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(sql, params)
    if fetch:
        rows = cur.fetchall()
    elif fetchone:
        rows = cur.fetchone()
    else:
        rows = None
    con.commit()
    con.close()
    return rows


# ---------- СКАЧИВАНИЕ КАРТИНОК ----------
async def download_image(url: str) -> bytes | None:
    if not url: return None
    if url in IMAGE_CACHE: return IMAGE_CACHE[url]
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://store.steampowered.com/"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=15) as resp:
                if resp.status != 200: return None
                data = await resp.read()
                if not data or len(data) < 100: return None
                IMAGE_CACHE[url] = data
                return data
    except Exception as e:
        logging.warning(f"[IMG] {e}")
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
                if resp.status != 200: return
                data = await resp.json()
    except Exception as e:
        print(f"[PARSER] {e}")
        return

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    added = 0
    async with aiohttp.ClientSession() as session:
        for item in data.get("specials", {}).get("items", []):
            title, app_id = item.get("name"), item.get("id")
            if not title or not app_id: continue
            final_price_rub = item.get("final_price", 0) / 100
            discount_percent = item.get("discount_percent", 0)
            original_price_rub = item.get("original_price", 0) / 100
            if final_price_rub <= 0 or discount_percent == 0: continue
            cur.execute("SELECT id FROM products WHERE title = ?", (title,))
            if cur.fetchone(): continue
            image_url, description, price_usd = await _steam_get_details(session, app_id)
            shop_url = f"https://store.steampowered.com/app/{app_id}/"
            cur.execute(
                "INSERT INTO products (title, price, old_price, discount, url, image, description, price_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (title, f"{final_price_rub:.0f} ₽", f"{original_price_rub:.0f} ₽", f"-{discount_percent}%", shop_url,
                 image_url, description, price_usd))
            added += 1
            await asyncio.sleep(0.2)

        for section_key in ("top_sellers", "new_releases"):
            for item in data.get(section_key, {}).get("items", []):
                title, app_id = item.get("name"), item.get("id")
                if not title or not app_id: continue
                cur.execute("SELECT id FROM products WHERE title = ?", (title,))
                if cur.fetchone(): continue
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
                                    original_price_str = f"{pd.get('initial', 0) / 100:.0f} ₽" if pd.get(
                                        "discount_percent") else ""
                                    discount_str = f"-{pd.get('discount_percent')}%" if pd.get(
                                        "discount_percent") else ""
                except Exception:
                    pass
                cur.execute(
                    "INSERT INTO products (title, price, old_price, discount, url, image, description, price_usd) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (title, final_price_str or "Бесплатно", original_price_str, discount_str, shop_url, image_url,
                     description, price_usd))
                added += 1
                await asyncio.sleep(0.2)
    con.commit()
    con.close()
    print(f"[PARSER] Добавлено новых игр: {added}")


async def send_daily_deal(bot: Bot):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT title, price, old_price, discount, url, image, description, price_usd FROM products ORDER BY RANDOM() LIMIT 1")
    game = cur.fetchone()
    if not game:
        con.close();
        return
    title, price, old_price, discount, url, image, description, price_usd = game
    cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    con.close()

    text = f"🎲 <b>Скидка дня: {title}</b>\n\n"
    if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
    if price: text += f"💵 Цена: <b>{price}</b>\n"
    if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if description: text += f"\n📝 <i>{description[:200]}...</i>\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]])
    photo = await download_image(image) if image else None

    for (uid,) in users:
        try:
            if photo:
                await bot.send_photo(chat_id=uid, photo=BufferedInputFile(photo, filename="deal.jpg"), caption=text,
                                     reply_markup=kb, parse_mode=ParseMode.HTML)
            else:
                await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.05)
        except Exception:
            pass


# ---------- КЛАВИАТУРЫ ----------
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔥 Скидки"), KeyboardButton(text="🎲 Случайная игра")],
            [KeyboardButton(text="💳 Пополнить Steam"), KeyboardButton(text="📱 Мобильные игры")],
            [KeyboardButton(text="💎 Премиум"), KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text="📢 Реклама")],
        ],
        resize_keyboard=True,
    )


def admin_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔄 Спарсить скидки"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="💳 Проверить оплаты"), KeyboardButton(text="⬅️ В меню")],
        ],
        resize_keyboard=True,
    )


# ---------- ХЕЛПЕРЫ ----------
def is_admin(user_id: int) -> bool: return user_id in ADMIN_IDS


def register_user(message: Message):
    db_query("INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
             (message.from_user.id, message.from_user.username, message.from_user.first_name))


def check_premium(user_id: int) -> bool:
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]: return False
    try:
        return datetime.fromisoformat(res[0]) > datetime.now()
    except (ValueError, TypeError):
        return False


def get_premium_until(user_id: int):
    res = db_query("SELECT premium_until FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not res or not res[0]: return None
    try:
        return datetime.fromisoformat(res[0])
    except (ValueError, TypeError):
        return None


def add_premium_days(user_id: int, days: int):
    current = get_premium_until(user_id)
    now = datetime.now()
    new_expiry = (current + timedelta(days=days)) if (current and current > now) else (now + timedelta(days=days))
    db_query("UPDATE users SET premium_until = ? WHERE user_id = ?", (new_expiry.isoformat(), user_id))


def wishlist_limit(user_id: int) -> int: return WISHLIST_LIMIT_PREMIUM if check_premium(
    user_id) else WISHLIST_LIMIT_FREE


def wishlist_count(user_id: int) -> int: return \
db_query("SELECT COUNT(*) FROM wishlist_titles WHERE user_id = ?", (user_id,), fetchone=True)[0]


# ---------- FSM ----------
class WishlistFSM(StatesGroup): waiting_title = State()


class TopUpFSM(StatesGroup):
    waiting_login = State()
    waiting_amount = State()


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
    if joined and joined[0]: text += f"📅 С нами с: {joined[0][:10]}\n"
    text += "\n"
    if is_prem and until:
        days_left = (until - datetime.now()).days + 1
        text += f"👑 Статус: <b>Премиум</b> (до {until.strftime('%d.%m.%Y')})\n⏳ Осталось: <b>{days_left} дн.</b>\n"
    else:
        text += f"💎 Статус: <b>Обычный</b>\n"
    text += f"\n⭐ <b>Wishlist:</b> {w_count} / {w_limit} игр\n"

    buttons = [[InlineKeyboardButton(text="⭐ Мой Wishlist", callback_data="wl_show")]]
    if w_count < w_limit: buttons.append(
        [InlineKeyboardButton(text="➕ Добавить в Wishlist", callback_data="wl_add_start")])
    if not is_prem: buttons.append(
        [InlineKeyboardButton(text="💎 Купить Премиум (до 10 игр)", callback_data="premium_show")])
    await message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


# ---------- СКИДКИ ----------
def _fetch_products_page():
    return db_query(
        "SELECT id, title, price, old_price, discount, url, image, description, price_usd FROM products WHERE image != '' ORDER BY id DESC LIMIT ?",
        (PAGE_LIMIT,), fetch=True)


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
    if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price: text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description: text += f"\n📝 <i>{description[:200]}...</i>\n"
    text += f"\n📄 Игра {index + 1} из {len(products)}"

    nav_row = []
    if index > 0: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"page_{index - 1}"))
    if index < len(products) - 1: nav_row.append(
        InlineKeyboardButton(text="Вперед ➡️", callback_data=f"page_{index + 1}"))
    buttons = []
    if nav_row: buttons.append(nav_row)
    buttons.append([InlineKeyboardButton(text="🛒 Купить в Steam", url=url)])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    photo_bytes = await download_image(image) if image else None

    if not isinstance(message_or_callback, CallbackQuery):
        if photo_bytes:
            await message_or_callback.answer_photo(photo=BufferedInputFile(photo_bytes, filename="game.jpg"),
                                                   caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
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
                media=InputMediaPhoto(media=BufferedInputFile(photo_bytes, filename="game.jpg"), caption=text,
                                      parse_mode=ParseMode.HTML), reply_markup=kb)
            return
        if not photo_bytes and not msg.photo:
            await msg.edit_text(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
            return
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower(): return
        logging.warning(f"[PAGINATION] {e}")
    try:
        if photo_bytes:
            await cb.bot.send_photo(chat_id=msg.chat.id, photo=BufferedInputFile(photo_bytes, filename="game.jpg"),
                                    caption=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        else:
            await cb.bot.send_message(chat_id=msg.chat.id, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
        try:
            await msg.delete()
        except Exception:
            pass
    except Exception as e:
        logging.error(f"[PAGINATION] {e}")


@router.callback_query(F.data.startswith("page_"))
async def paginate_products(cb: CallbackQuery):
    try:
        index = int(cb.data.split("_", 1)[1])
    except (IndexError, ValueError):
        await cb.answer("Ошибка", show_alert=False);
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
                        f"⏳ <b>Лимит рулетки!</b>\n\nОбычные пользователи — 1 раз в день.\nСледующая попытка через: <b>{h} ч. {m} мин.</b>\n\n💎 <b>Купи Премиум</b> — крути без ограничений!")
                    return
            except (ValueError, TypeError):
                pass

    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if is_prem:
        cur.execute(
            "SELECT title, price, old_price, discount, url, image, description, price_usd FROM products WHERE discount IS NOT NULL AND discount != '' ORDER BY CAST(REPLACE(discount, '-', '') AS INTEGER) DESC, RANDOM() LIMIT 20")
        candidates = cur.fetchall()
        if candidates:
            import random
            game = random.choice(candidates)
        else:
            cur.execute(
                "SELECT title, price, old_price, discount, url, image, description, price_usd FROM products ORDER BY RANDOM() LIMIT 1")
            game = cur.fetchone()
    else:
        cur.execute(
            "SELECT title, price, old_price, discount, url, image, description, price_usd FROM products ORDER BY RANDOM() LIMIT 1")
        game = cur.fetchone()
    con.close()

    if not game:
        await message.answer("В базе пока нет игр.");
        return
    db_query("UPDATE users SET last_roulette = ? WHERE user_id = ?", (datetime.now().isoformat(), user_id))

    title, price, old_price, discount, url, image, description, price_usd = game
    display_usd = f" (в долларах: {price_usd})" if (price_usd and "руб" not in price_usd.lower()) else ""
    text = f"🎲 <b>Тебе выпала: {title}</b>\n\n"
    if old_price: text += f"❌ Старая цена: <s>{old_price}</s>\n"
    if price: text += f"💵 Цена: <b>{price}</b>{display_usd}\n"
    if discount: text += f"🔥 Скидка: <b>{discount}</b>\n"
    if description: text += f"\n📝 <i>{description[:200]}...</i>\n"
    if is_prem: text += f"\n👑 <i>Бонус Премиум: максимальная скидка!</i>"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить в Steam", url=url)]])
    photo_bytes = await download_image(image) if image else None
    if photo_bytes:
        await message.answer_photo(photo=BufferedInputFile(photo_bytes, filename="game.jpg"), caption=text,
                                   reply_markup=kb, parse_mode=ParseMode.HTML)
    else:
        await message.answer(text=text, reply_markup=kb, parse_mode=ParseMode.HTML)


# ---------- WISHLIST ----------
@router.callback_query(F.data == "wl_add_start")
async def wl_add_start(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    limit, count = wishlist_limit(uid), wishlist_count(uid)
    if count >= limit:
        await cb.answer(f"Лимит Wishlist: {limit} игр.\nКупи Премиум, чтобы добавить до 10!", show_alert=True);
        return
    await state.set_state(WishlistFSM.waiting_title)
    await cb.message.answer(f"⭐ <b>Добавление в Wishlist</b>\n\nНапиши название игры.\nУ тебя: {count} / {limit} игр.",
                            reply_markup=InlineKeyboardMarkup(
                                inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="wl_cancel")]]))
    await cb.answer()


@router.callback_query(F.data == "wl_cancel")
async def wl_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Добавление отменено.")
    await cb.answer()


@router.message(WishlistFSM.waiting_title)
async def wl_add_title(message: Message, state: FSMContext):
    uid = message.from_user.id
    title = (message.text or "").strip()
    if len(title) < 2: await message.answer("Слишком короткое название."); return
    if len(title) > 200: await message.answer("Слишком длинное название."); return
    limit, count = wishlist_limit(uid), wishlist_count(uid)
    if count >= limit:
        await state.clear()
        await message.answer(f"❌ Достигнут лимит Wishlist ({limit} игр).", reply_markup=main_menu_kb());
        return
    existing = db_query("SELECT id FROM wishlist_titles WHERE user_id = ? AND LOWER(title) = LOWER(?)", (uid, title),
                        fetchone=True)
    if existing:
        await state.clear()
        await message.answer(f"⚠️ <b>{title}</b> уже есть в Wishlist.", reply_markup=main_menu_kb());
        return
    db_query("INSERT INTO wishlist_titles (user_id, title) VALUES (?, ?)", (uid, title))
    await state.clear()
    await message.answer(f"✅ <b>{title}</b> добавлено в Wishlist!\nСейчас у тебя: {count + 1} / {limit} игр.",
                         reply_markup=main_menu_kb())


@router.callback_query(F.data == "wl_show")
async def wl_show(cb: CallbackQuery):
    uid = cb.from_user.id
    limit = wishlist_limit(uid)
    rows = db_query("SELECT id, title FROM wishlist_titles WHERE user_id = ? ORDER BY id", (uid,), fetch=True)
    if not rows:
        await cb.answer("Wishlist пуст", show_alert=True);
        return
    text = f"⭐ <b>Твой Wishlist</b> ({len(rows)} / {limit}):\n\n"
    buttons = []
    for wid, title in rows:
        text += f"• {title}\n"
        buttons.append([InlineKeyboardButton(text=f"❌ Удалить: {title[:25]}", callback_data=f"wl_del_{wid}")])
    if not check_premium(uid):
        text += f"\n💎 Хочешь до 10 игр? Купи Премиум!"
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
        await cb.answer("Ошибка"); return
    db_query("DELETE FROM wishlist_titles WHERE id = ? AND user_id = ?", (wid, cb.from_user.id))
    await cb.answer("Удалено")
    await wl_show(cb)


# ---------- ПОПОЛНЕНИЕ STEAM (ВВОД ЛОГИНА И СУММЫ) ----------
@router.message(F.text == "💳 Пополнить Steam")
async def steam_topup_start(message: Message, state: FSMContext):
    await state.set_state(TopUpFSM.waiting_login)
    await message.answer(
        "💳 <b>Пополнение Steam</b>\n\n"
        "Шаг 1 из 2. Введи свой <b>логин Steam</b> (не пароль!).\n"
        "Например: <i>my_nickname</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")]
        ])
    )


@router.callback_query(F.data == "topup_cancel")
async def topup_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Пополнение отменено.")
    await cb.answer()


@router.message(TopUpFSM.waiting_login)
async def topup_login(message: Message, state: FSMContext):
    login = (message.text or "").strip()

    if not login or len(login) < 3 or " " in login or len(login) > 64:
        await message.answer("⚠️ Некорректный логин. Попробуй ещё раз (от 3 до 64 символов, без пробелов).")
        return

    await state.update_data(login=login)
    await state.set_state(TopUpFSM.waiting_amount)

    await message.answer(
        f"👤 Логин: <b>{login}</b>\n\n"
        "Шаг 2 из 2. Введи <b>сумму пополнения в рублях</b> (только цифры).\n"
        "Например: <i>500</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="topup_cancel")]
        ])
    )


@router.message(TopUpFSM.waiting_amount)
async def topup_amount(message: Message, state: FSMContext):
    amount_str = (message.text or "").strip()

    if not amount_str.isdigit():
        await message.answer("⚠️ Пожалуйста, введи сумму только цифрами (например, 500).")
        return

    amount = int(amount_str)
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
        [InlineKeyboardButton(text="❌ Закрыть", callback_data="close_msg")]
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
        reply_markup=kb
    )


@router.callback_query(F.data == "close_msg")
async def close_msg(cb: CallbackQuery):
    await cb.message.delete()
    await cb.answer()


# ---------- МОБИЛЬНЫЕ ИГРЫ ----------
@router.message(F.text == "📱 Мобильные игры")
async def mobile_topup_menu(message: Message):
    db_query("INSERT INTO link_clicks (user_id, link_type) VALUES (?, ?)", (message.from_user.id, "mobile_menu"))
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
        if until: status = f"👑 <b>Премиум активен до {until.strftime('%d.%m.%Y %H:%M')}</b>\n\n"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔥 7 дней — {PRICE_7_DAYS} ₽ (выгодно!)", callback_data=f"promo_{PRICE_7_DAYS}_7")],
        [InlineKeyboardButton(text=f"👑 30 дней — {PRICE_30_DAYS} ₽ (макс. выгода)",
                              callback_data=f"promo_{PRICE_30_DAYS}_30")],
    ])
    await message.answer(
        f"{status}"
        "💎 <b>Премиум-доступ</b>\n\n"
        "Что ты получаешь:\n"
        "🎲 <b>Безлимитная рулетка</b>\n"
        "🏆 <b>Лучшие скидки</b>\n"
        "⭐ <b>Wishlist до 10 игр</b>\n"
        "🔔 <b>Супер-скидки 80%+</b>\n\n"
        "Выбери срок:",
        reply_markup=kb
    )


@router.callback_query(F.data == "premium_show")
async def premium_show(cb: CallbackQuery):
    await cb.answer()
    await premium_menu(cb.message)


@router.callback_query(F.data.startswith("promo_"))
async def promo_select(cb: CallbackQuery):
    parts = cb.data.split("_")
    if len(parts) < 3: await cb.answer("Ошибка"); return
    try:
        amount, days = int(parts[1]), int(parts[2])
    except (ValueError, IndexError):
        await cb.answer("Ошибка"); return
    if not YOOKASSA_SHOP_ID or YOOKASSA_SHOP_ID == "ТВОЙ_SHOP_ID":
        await cb.answer("Оплата временно недоступна", show_alert=True);
        return
    idempotence_key = str(uuid.uuid4())
    try:
        payment = Payment.create({
            "amount": {"value": f"{amount}.00", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": YOOKASSA_RETURN_URL},
            "capture": True,
            "description": f"Премиум на {days} дн.",
            "metadata": {"user_id": cb.from_user.id, "days": days}
        }, idempotence_key)
        db_query("INSERT INTO payments (payment_id, user_id, amount, days, status) VALUES (?, ?, ?, ?, 'pending')",
                 (payment.id, cb.from_user.id, amount, days))
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} ₽", url=payment.confirmation.confirmation_url)]])
        await cb.message.edit_text(
            f"💳 <b>Оплата через ЮKassa</b>\n\nСрок: <b>{days} дн.</b> за <b>{amount} ₽</b>\n\nНажми кнопку ниже. Премиум активируется автоматически.",
            reply_markup=kb)
    except Exception as e:
        print(f"ЮKassa: {e}")
        await cb.message.edit_text("❌ Ошибка создания платежа.")


# ---------- РЕКЛАМА ----------
@router.message(F.text == "📢 Реклама")
async def ads_info(message: Message):
    await message.answer(
        "📢 <b>Реклама в нашем боте</b>\n\n🎯 Пост в рассылке, закреп, упоминание в «Скидке дня».\n\nПиши: 👉 <b>@ObuzaMeduza</b>",
        reply_markup=main_menu_kb())


# ---------- ПРОВЕРКА ОПЛАТ ----------
async def check_yookassa_payments(bot: Bot):
    if not YOOKASSA_SHOP_ID or YOOKASSA_SHOP_ID == "ТВОЙ_SHOP_ID": return
    pending = db_query("SELECT payment_id, user_id, amount, days FROM payments WHERE status='pending'", fetch=True)
    for pay_id, user_id, amount, days in pending:
        try:
            payment = Payment.find_one(pay_id)
            if payment.status == "succeeded":
                db_query("UPDATE payments SET status='confirmed' WHERE payment_id=?", (pay_id,))
                add_premium_days(user_id, days)
                try:
                    await bot.send_message(user_id,
                                           f"✅ Оплата прошла! Премиум активирован на {days} дн.\n\n👑 Теперь тебе доступны: безлимитная рулетка, лучшие скидки, Wishlist до 10 игр, уведомления 80%+!")
                except Exception:
                    pass
            elif payment.status == "canceled":
                db_query("UPDATE payments SET status='rejected' WHERE payment_id=?", (pay_id,))
        except Exception as e:
            print(f"Ошибка проверки {pay_id}: {e}")


# ---------- АДМИНКА ----------
@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())


@router.message(F.text == "⬅️ В меню")
async def to_main(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню:", reply_markup=main_menu_kb())


@router.message(F.text == "🔄 Спарсить скидки")
async def manual_parse(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("⏳ Парсим Steam...")
    await fetch_steam_deals()
    await message.answer("✅ Парсинг завершён!", reply_markup=admin_menu_kb())


@router.message(F.text == "💳 Проверить оплаты")
async def check_payments(message: Message):
    if not is_admin(message.from_user.id): return
    rows = db_query("SELECT id, user_id, amount, days, status FROM payments ORDER BY id DESC LIMIT 10", fetch=True)
    if not rows:
        await message.answer("Нет платежей.", reply_markup=admin_menu_kb());
        return
    text = "📋 <b>Последние платежи:</b>\n\n"
    for pay_id, user_id, amount, days, status in rows:
        emoji = "✅" if status == "confirmed" else "⏳" if status == "pending" else "❌"
        text += f"{emoji} #{pay_id} | U:{user_id} | {amount}₽ / {days}д.\n"
    await message.answer(text, reply_markup=admin_menu_kb())


@router.message(F.text == "📊 Статистика")
async def stats(message: Message):
    if not is_admin(message.from_user.id): return
    users = db_query("SELECT COUNT(*) FROM users", fetchone=True)[0]
    prem = db_query("SELECT COUNT(*) FROM users WHERE premium_until > ?", (datetime.now().isoformat(),), fetchone=True)[
        0]
    products = db_query("SELECT COUNT(*) FROM products", fetchone=True)[0]
    confirmed_pay = db_query("SELECT SUM(amount) FROM payments WHERE status='confirmed'", fetchone=True)[0] or 0
    wish = db_query("SELECT COUNT(*) FROM wishlist_titles", fetchone=True)[0]
    steam_clicks = db_query("SELECT COUNT(*) FROM link_clicks WHERE link_type LIKE 'steam%'", fetchone=True)[0]
    mobile_clicks = db_query("SELECT COUNT(*) FROM link_clicks WHERE link_type LIKE 'mobile%'", fetchone=True)[0]
    await message.answer(
        f"📊 <b>Статистика</b>\n\n👥 Всего юзеров: {users}\n👑 Премиум: {prem}\n🎮 Игр: {products}\n⭐ Wishlist записей: {wish}\n💵 Заработано: {confirmed_pay} ₽\n\n💳 Кликов Steam: {steam_clicks}\n📱 Кликов Mobile: {mobile_clicks}",
        reply_markup=admin_menu_kb(),
    )


# ---------- УВЕДОМЛЕНИЯ О СУПЕР-СКИДКАХ ----------
async def notify_premium_super_deals(bot: Bot):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "SELECT p.id, p.title, p.price, p.discount, p.url, p.image, p.description FROM products p LEFT JOIN notified_deals n ON n.product_id = p.id WHERE CAST(REPLACE(p.discount, '-', '') AS INTEGER) >= 80 AND p.discount IS NOT NULL AND p.discount != '' AND n.product_id IS NULL")
    super_deals = cur.fetchall()
    if not super_deals:
        con.close();
        return
    cur.execute("SELECT user_id FROM users WHERE premium_until > ?", (datetime.now().isoformat(),))
    premium_users = cur.fetchall()
    for game in super_deals:
        pid, title, price, discount, url, image, description = game
        text = f"🔥 <b>СУПЕР-СКИДКА {discount}!</b>\n\n🎮 {title}\n💵 {price}\n\n📝 <i>{(description or '')[:100]}...</i>"
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить", url=url)]])
        photo = await download_image(image) if image else None
        for (uid,) in premium_users:
            try:
                if photo:
                    await bot.send_photo(chat_id=uid, photo=BufferedInputFile(photo, filename="deal.jpg"), caption=text,
                                         reply_markup=kb, parse_mode=ParseMode.HTML)
                else:
                    await bot.send_message(chat_id=uid, text=text, reply_markup=kb, parse_mode=ParseMode.HTML)
                await asyncio.sleep(0.05)
            except Exception:
                pass
        cur.execute("INSERT OR IGNORE INTO notified_deals (product_id) VALUES (?)", (pid,))
    con.commit()
    con.close()


# ---------- ЗАПУСК ----------
async def main():
    db_init()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)

    scheduler = AsyncIOScheduler()
    scheduler.add_job(fetch_steam_deals, 'interval', hours=6)
    scheduler.add_job(fetch_steam_deals, 'date', run_date=datetime.now() + timedelta(minutes=1))
    scheduler.add_job(send_daily_deal, 'cron', hour=12, minute=0, args=[bot])
    scheduler.add_job(notify_premium_super_deals, 'interval', hours=3, args=[bot])
    scheduler.add_job(check_yookassa_payments, 'interval', minutes=1, args=[bot])
    scheduler.start()

    await bot.delete_webhook(drop_pending_updates=True)
    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
