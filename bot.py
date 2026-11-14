import os
import asyncio
import json
import time
import hmac
import hashlib
import secrets
import re
import urllib.request
import urllib.error
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from contextlib import asynccontextmanager
import asyncpg
from fastapi import FastAPI, HTTPException, Query, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =========================================================
# НАСТРОЙКИ И ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# =========================================================

TOKEN = os.getenv("BOT_TOKEN", "9165298433:AAHaiuVQHEcGT9ziWetjgbJ_tPUiF3Cy2pw").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://tasbehh_db_user:gr7YfjWZRyhxubOKvhR9HUXk7EKCuVD5@dpg-dbs0nbui0phs73bq91ng-a/tasbehh_db").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "8274122612") or "8274122612")

# Настройки для Mini App и Monetag
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rakhmatjonablakulov-dev.github.io/tasbeh_bot/")
MONETAG_POSTBACK_SECRET = os.getenv("MONETAG_POSTBACK_SECRET", "Z2f456G7nj9kS6ml8D").strip()
MONETAG_ZONE_ID = "12197315"
AD_REWARD = 200

MIN_WITHDRAWAL = 500_000
DAILY_REWARD = 100
DAILY_COOLDOWN = 24 * 60 * 60

REFERRAL_REWARD = 20_000
REFERRAL_REQUIRED_TAPS = 100
INIT_DATA_MAX_AGE = 24 * 60 * 60

METHODS = {
    "bank": "Банковский перевод",
    "crypto": "Криптовалюта",
    "stars": "Telegram Stars",
}

# Инициализация бота aiogram
bot = Bot(token=TOKEN)
dp = Dispatcher()
pool = None

# Файлы для локального бэкапа (если не используется БД)
TASKS_FILE = "tasks.json"
USERS_FILE = "users_data.json"


# =========================================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ И FASTAPI ЖИЗНЕННОГО ЦИКЛА
# =========================================================

async def init_database():
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                user_id BIGINT PRIMARY KEY,
                username TEXT DEFAULT '',
                balance BIGINT NOT NULL DEFAULT 0,
                referrals INTEGER NOT NULL DEFAULT 0,
                referred_by BIGINT,
                last_daily DATE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS taps BIGINT NOT NULL DEFAULT 0")
        await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS referral_awarded BOOLEAN NOT NULL DEFAULT FALSE")
        await conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS last_daily_at TIMESTAMPTZ")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT DEFAULT '',
                balance BIGINT NOT NULL DEFAULT 0,
                referrals INTEGER NOT NULL DEFAULT 0,
                referred_by BIGINT,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS taps BIGINT NOT NULL DEFAULT 0")
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_daily_at TIMESTAMPTZ")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS monetag_ad_sessions (
                ymid TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                zone_id TEXT NOT NULL DEFAULT '12197315',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                rewarded_at TIMESTAMPTZ,
                reward_event_type TEXT
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payout_requests (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                amount BIGINT NOT NULL,
                country TEXT NOT NULL,
                method TEXT NOT NULL,
                details TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                processed_at TIMESTAMP
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                reward BIGINT NOT NULL DEFAULT 0,
                url TEXT DEFAULT '',
                task_type TEXT DEFAULT 'general',
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP NOT NULL DEFAULT NOW()
            )
        """)
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS sponsor_name TEXT NOT NULL DEFAULT 'TASBEH'")
        await conn.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS max_participants INTEGER")

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS task_claims (
                id BIGSERIAL PRIMARY KEY,
                task_id BIGINT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                user_id BIGINT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                completed_at TIMESTAMP,
                created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                UNIQUE(task_id, user_id)
            )
        """)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")
    
    # Если DATABASE_URL не задана на Render, поднимаем локальный fallback или предупреждаем
    if DATABASE_URL:
        db_url = DATABASE_URL
        if db_url.startswith("postgres://"):
            db_url = db_url.replace("postgres://", "postgresql://", 1)
        pool = await asyncpg.create_pool(db_url, min_size=1, max_size=5, command_timeout=30)
        await init_database()
        print("🗄 PostgreSQL подключена для API и бота!")

    # Запускаем поллинг бота в фоновом режиме внутри FastAPI
    polling_task = asyncio.create_task(dp.start_polling(bot))
    print("✨ Тасбеҳ боти ва веб-сервер муваффақиятли ишга тушди...")
    
    yield
    
    polling_task.cancel()
    await bot.session.close()
    if pool:
        await pool.close()


app = FastAPI(title="Tasbeh Bot & API", version="2.2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =========================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И АВТОРИЗАЦИЯ MINI APP
# =========================================================

def verify_telegram(init_data: str):
    if not init_data:
        raise HTTPException(401, "Откройте тамбех через Telegram")
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = data.pop("hash", None)
        if not received_hash:
            raise HTTPException(401, "Нет подписи Telegram")

        auth_date = int(data.get("auth_date", "0"))
        now = int(datetime.now(timezone.utc).timestamp())
        if auth_date <= 0 or now - auth_date > INIT_DATA_MAX_AGE:
            raise HTTPException(401, "Авторизация истекла. Перезапустите приложение")

        check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            raise HTTPException(403, "Неверная подпись Telegram")

        user_data = json.loads(data.get("user", "{}"))
        return {
            "user_id": int(user_data["id"]),
            "username": user_data.get("username") or user_data.get("first_name") or "Игрок",
            "start_param": data.get("start_param", ""),
        }
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Не удалось проверить Telegram")


def get_user(x_telegram_init_data: str = Header(default="")):
    return verify_telegram(x_telegram_init_data)


async def notify_admin(message: str):
    if ADMIN_ID <= 0:
        return False
    try:
        await bot.send_message(ADMIN_ID, message, disable_web_page_preview=True)
        return True
    except Exception:
        return False


def require_admin(admin_id: int):
    if ADMIN_ID <= 0 or int(admin_id) != ADMIN_ID:
        raise HTTPException(403, "Доступ запрещён")


# =========================================================
# FASTAPI МАРШРУТЫ (API ДЛЯ MINI APP И МОНЕТАГ)
# =========================================================

@app.get("/")
async def root():
    return {"game": "Tasbeh Bot API", "status": "online", "version": "2.2"}


@app.get("/api/start")
async def start_game(x_telegram_init_data: str = Header(default="")):
    if not pool:
        return {"success": True, "balance": 0, "taps": 0}
    user = get_user(x_telegram_init_data)
    user_id = user["user_id"]

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO players(user_id, username) VALUES($1, $2)
                ON CONFLICT(user_id) DO UPDATE SET username = EXCLUDED.username
            """, user_id, user["username"])
            
            player = await conn.fetchrow("SELECT * FROM players WHERE user_id = $1", user_id)

    return {
        "success": True,
        "user_id": player["user_id"],
        "username": player["username"],
        "balance": player["balance"],
        "taps": player["taps"],
        "referrals": player["referrals"],
        "min_withdrawal": MIN_WITHDRAWAL,
    }


class ClickRequest(BaseModel):
    count: int = Field(default=1, ge=1, le=100)


@app.post("/api/click")
async def click_game(body: ClickRequest, x_telegram_init_data: str = Header(default="")):
    if not pool:
        return {"success": True, "balance": 0}
    user = get_user(x_telegram_init_data)
    user_id = user["user_id"]
    count = body.count

    async with pool.acquire() as conn:
        player = await conn.fetchrow("""
            UPDATE players
            SET balance = balance + $2, taps = taps + $2
            WHERE user_id = $1
            RETURNING balance, taps
        """, user_id, count)

    return {"success": True, "balance": player["balance"], "taps": player["taps"]}


@app.post("/api/ad/start")
async def start_monetag_ad(x_telegram_init_data: str = Header(default="")):
    if not pool:
        return {"success": False}
    user = get_user(x_telegram_init_data)
    ymid = f"u{user['user_id']}_{secrets.token_hex(12)}"
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO monetag_ad_sessions(ymid, user_id, zone_id) VALUES($1, $2, $3)",
            ymid, user["user_id"], MONETAG_ZONE_ID
        )
    return {"success": True, "ymid": ymid, "reward": AD_REWARD}


@app.get("/monetag/postback")
async def monetag_postback(
    secret: str = Query(default=""),
    ymid: str = Query(default=""),
    reward_event_type: str = Query(default=""),
    telegram_id: str = Query(default="")
):
    if not pool or not MONETAG_POSTBACK_SECRET or not hmac.compare_digest(secret, MONETAG_POSTBACK_SECRET):
        raise HTTPException(403, "Invalid postback secret")
    if reward_event_type != "valued" or not ymid:
        return {"success": True, "rewarded": False}

    async with pool.acquire() as conn:
        async with conn.transaction():
            session = await conn.fetchrow("SELECT user_id, rewarded_at FROM monetag_ad_sessions WHERE ymid = $1 FOR UPDATE", ymid)
            if not session or session["rewarded_at"] is not None:
                return {"success": True, "rewarded": False}

            user_id = int(session["user_id"])
            updated = await conn.fetchrow(
                "UPDATE players SET balance = balance + $2 WHERE user_id = $1 RETURNING balance",
                user_id, AD_REWARD
            )
            await conn.execute("UPDATE monetag_ad_sessions SET rewarded_at = NOW() WHERE ymid = $1", ymid)
            
    await bot.send_message(user_id, f"🎉 Реклама кўрилгани учун ҳисобингизга +{AD_REWARD} балл қўшилди!")
    return {"success": True, "rewarded": True}


# =========================================================
# ЛОКАЛЬНЫЕ ДАННЫЕ И СТАРЫЕ ФУНКЦИИ БОТА (FALLBACK ЕСЛИ НЕТ DB)
# =========================================================

TASKS_FILE = "tasks.json"
USERS_FILE = "users_data.json"

def load_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return [                 
        {"id": 1, "title": "📢 Расмий каналга обуна бўлиш", "reward": 50, "url": "https://t.me/telegram", "expires_at": None},
        {"id": 2, "title": "⭐ Лойиҳани қўллаб-қувватлаш", "reward": 30, "url": "https://t.me/telegram", "expires_at": None}
    ]

def save_tasks(tasks):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=4)

def load_users_data():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "users": set(data.get("users", [])),
                    "balances": {int(k): v for k, v in data.get("balances", {}).items()},
                    "names": {int(k): v for k, v in data.get("names", {}).items()},
                    "referred": set(data.get("referred", [])),
                    "completed_tasks": {int(k): v for k, v in data.get("completed_tasks", {}).items()}
                }
        except Exception:
            pass
    return {
        "users": {ADMIN_ID},
        "balances": {ADMIN_ID: 1000},
        "names": {ADMIN_ID: "Admin"},
        "referred": set(),
        "completed_tasks": {}
    }

def save_users_data():
    data = {
        "users": list(users),
        "balances": user_balances,
        "names": user_names,
        "referred": list(referred_users),
        "completed_tasks": user_completed_tasks
    }
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

tasks_list = load_tasks()
db_data = load_users_data()
users = db_data["users"]
user_balances = db_data["balances"]
user_names = db_data["names"]
referred_users = db_data["referred"]
user_completed_tasks = db_data["completed_tasks"]


# =========================================================
# FSM И КЛАВИАТУРЫ ТЕЛЕГРАМ-БОТА
# =========================================================

class WithdrawStates(StatesGroup):
    waiting_for_details = State()

class AdminBroadcast(StatesGroup):
    waiting_for_message = State()

class AdminBalance(StatesGroup):
    waiting_user_id = State()
    waiting_amount = State()

class AdminTask(StatesGroup):
    waiting_title = State()
    waiting_reward = State()
    waiting_url = State()
    waiting_hours = State()


def get_main_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📿 Т А С Б Е Х Н И  О Ч И Ш")
    builder.button(text="💰 Балансим")
    builder.button(text="💳 Пулни ечиб олиш")
    builder.button(text="🏆 Рейтинг")
    builder.button(text="👥 Дўстларни таклиф")
    builder.button(text="📋 Вазифалар")
    builder.button(text="⭐ Рекламани кўриш")
    builder.adjust(1, 2, 2, 2)
    return builder.as_markup(resize_keyboard=True)


def get_admin_menu():
    builder = ReplyKeyboardBuilder()
    builder.button(text="📊 Статистика")
    builder.button(text="👥 Фойдаланувчилар")
    builder.button(text="📢 Рассылка юбориш")
    builder.button(text="💰 Балансни бошқариш")
    builder.button(text="📋 Вазифа қўшиш")
    builder.button(text="📋 Вазифаларни бошқариш")
    builder.button(text="⬅️ Асосий меню")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


# =========================================================
# ОБРАБОТЧИКИ ТЕЛЕГРАМ-БОТА (AIOGRAM)
# =========================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    users.add(user_id)
    user_names[user_id] = message.from_user.first_name or "Foydalanuvchi"

    if user_id not in user_balances:
        user_balances[user_id] = 0

    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if referrer_id != user_id and user_id not in referred_users:
            referred_users.add(user_id)
            user_balances[referrer_id] = user_balances.get(referrer_id, 0) + 2000
            try:
                await bot.send_message(
                    referrer_id,
                    f"🎉 Рефералингиз орқали янги фойдаланувчи қўшилди! Ҳисобингизга +2000 балл қўшилди.",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    save_users_data()
    user_name = message.from_user.first_name or "Қадрли инсон"
    welcome_text = (
        f"✨ <b>Ассалому алайкум ва роҳматуллоҳи ва барокатуҳ, {user_name}!</b>\n\n"
        f"🌙 <b>«Муборак Тасбеҳ ва зикрлар»</b> ботига хуш келибсиз!\n\n"
        f"👇 <i>Қуйидаги менюдан керакли бўлимни танланг:</i>"
    )
    await message.answer(welcome_text, reply_markup=get_main_menu(), parse_mode="HTML")


@dp.message(F.text == "⬅️ Асосий меню")
async def back_to_main_menu(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("✨ Асосий менюга қайтдингиз.", reply_markup=get_main_menu())


@dp.message(F.text == "📿 Т А С Б Е Х Н И  О Ч И Ш")
async def open_tasbih(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Мини-иловани ишга тушириш", web_app=types.WebAppInfo(url=WEBAPP_URL))
    await message.answer(
        "📿 <b>Т А С Б Е Х!</b>\n\nҚуйидаги тугма орқали интерактив мини-иловага ўтинг ва зикр қилишни бошланг:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@dp.message(F.text == "💰 Балансим")
async def check_balance(message: types.Message):
    user_id = message.from_user.id
    balance = user_balances.get(user_id, 0)
    await message.answer(
        f"💰 <b>Сизнинг ҳисобингиз:</b>\n\n💎 Жорий баланс: <b>{balance} балл</b>\n💳 Энг кам пул ечиш: <b>500 000 балл</b>",
        parse_mode="HTML",
        reply_markup=get_main_menu()
    )


@dp.message(F.text == "💳 Пулни ечиб олиш")
async def process_withdrawal(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    balance = user_balances.get(user_id, 0)
    if balance < MIN_WITHDRAWAL:
        await message.answer(f"❌ Пул ечиш учун балансингиз етарли эмас! Керакли миқдор: {MIN_WITHDRAWAL:,} балл.", reply_markup=get_main_menu())
        return

    await message.answer("💳 Реквизитларингизни юборинг (Масалан: 8600 ... / Исм Шариф):", parse_mode="HTML")
    await state.set_state(WithdrawStates.waiting_for_details)


@dp.message(WithdrawStates.waiting_for_details)
async def receive_withdrawal_details(message: types.Message, state: FSMContext):
    user = message.from_user
    details = message.text
    balance = user_balances.get(user.id, 0)
    user_balances[user.id] = 0
    save_users_data()

    admin_text = f"🔔 <b>Янги пул чиқариш сўрови!</b>\nID: {user.id}\nСумма: {balance}\nРеквизит: {details}"
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Тўлаб берилди", callback_data=f"pay_yes_{user.id}_{balance}")
    builder.button(text="❌ Рад этиш", callback_data=f"pay_no_{user.id}_{balance}")
    
    await bot.send_message(ADMIN_ID, admin_text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await message.answer("✅ Сўровингиз қабул қилинди!", reply_markup=get_main_menu())
    await state.clear()


@dp.message(F.text == "🏆 Рейтинг")
async def show_leaderboard(message: types.Message):
    sorted_users = sorted(user_balances.items(), key=lambda x: x[1], reverse=True)[:10]
    text = "🏆 <b>Энг фаол фойдаланувчилар (Топ-10)</b>\n\n"
    for i, (uid, bal) in enumerate(sorted_users, 1):
        name = user_names.get(uid, f"User {uid}")
        text += f"{i}. {name} — 💎 {bal} балл\n"
    await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())


@dp.message(F.text == "👥 Дўстларни таклиф")
async def process_referral(message: types.Message):
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    await message.answer(f"🔗 Сизнинг реферал линкингиз:\n<code>{ref_link}</code>", parse_mode="HTML")


@dp.message(F.text == "📋 Вазифалар")
async def process_tasks(message: types.Message):
    global tasks_list
    tasks_list = load_tasks()
    if not tasks_list:
        await message.answer("📋 Ҳозирча вазифалар йўқ.", reply_markup=get_main_menu())
        return
    for task in tasks_list:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔗 Бажариш", url=task['url'])
        builder.button(text="🎁 Текшириш", callback_data=f"check_task_{task['id']}")
        await message.answer(f"📌 {task['title']}\n🎁 Мукофот: +{task['reward']} балл", reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("check_task_"))
async def task_callback_handler(callback: types.CallbackQuery):
    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    target_task = next((t for t in tasks_list if t['id'] == task_id), None)
    if not target_task:
        await callback.answer("Вазифа топилмади!", show_alert=True)
        return
    
    user_completed_tasks.setdefault(user_id, [])
    if task_id in user_completed_tasks[user_id]:
        await callback.answer("Сиз бу вазифани аллақачон бажаргансиз!", show_alert=True)
        return

    user_completed_tasks[user_id].append(task_id)
    user_balances[user_id] = user_balances.get(user_id, 0) + target_task['reward']
    save_users_data()
    await callback.answer(f"Вазифа бажарилди! +{target_task['reward']} балл", show_alert=False)


@dp.message(F.text == "⭐ Рекламани кўриш")
async def process_ads_view(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐ Рекламани кўриш", web_app=types.WebAppInfo(url=WEBAPP_URL))
    await message.answer("⭐ Рекламани кўриш орқали лойиҳани қўллаб-қувватланг ва баллар оलिंग:", reply_markup=builder.as_markup())


@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer("🛠 Админ панел", reply_markup=get_admin_menu())


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(f"📊 Жами фойдаланувчилар: {len(users)}", reply_markup=get_admin_menu())


# =========================================================
# ЗАПУСК БОТА (ЧЕРЕЗ LIFESPAN С FASTAPI)
# =========================================================

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run("bot:app", host="0.0.0.0", port=port, reload=False)
