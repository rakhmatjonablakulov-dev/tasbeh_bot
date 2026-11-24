import os
import json
import hmac
import asyncio
from pathlib import Path
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from aiogram import Bot

# Импортируем диспетчер и бота из вашего файла bot.py
from bot import dp, bot as tg_bot

# =====================================
# SETTINGS
# =====================================

TOKEN = os.getenv("BOT_TOKEN")
MONETAG_SECRET = os.getenv("MONETAG_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8274122612"))

USERS_FILE = Path("users_data.json")

bot = tg_bot if tg_bot else (Bot(token=TOKEN) if TOKEN else None)
db_pool = None
bot_task = None


class ZikrSyncPayload(BaseModel):
    user_id: int
    username: str = ""
    total_zikr: int


# =====================================
# DATABASE TABLES
# =====================================

async def create_tables():
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tasbeh_users (
                user_id BIGINT PRIMARY KEY,
                balance BIGINT NOT NULL DEFAULT 0,
                username TEXT NOT NULL DEFAULT '',
                referred BOOLEAN NOT NULL DEFAULT FALSE,
                completed_tasks JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS monetag_events (
                event_id TEXT PRIMARY KEY,
                user_id BIGINT NOT NULL,
                reward BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_monetag_events_user
            ON monetag_events(user_id)
        """)


# =====================================
# MIGRATE OLD JSON DATA
# =====================================

async def migrate_json_data():
    if not USERS_FILE.exists():
        return

    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        balances = data.get("balances", {})
        names = data.get("names", {})
        referred = {int(x) for x in data.get("referred", [])}
        tasks = data.get("completed_tasks", {})
        users = {int(x) for x in data.get("users", [])}

        all_ids = (
            users
            | {int(x) for x in balances}
            | {int(x) for x in names}
            | {int(x) for x in tasks}
        )

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                for uid in all_ids:
                    await conn.execute("""
                        INSERT INTO tasbeh_users
                        (user_id, balance, username, referred, completed_tasks)
                        VALUES ($1, $2, $3, $4, $5::jsonb)
                        ON CONFLICT (user_id) DO NOTHING
                    """,
                        uid,
                        int(balances.get(str(uid), 0)),
                        str(names.get(str(uid), "")),
                        uid in referred,
                        json.dumps(tasks.get(str(uid), {}))
                    )

        print("JSON migration completed")

    except Exception as e:
        print("JSON migration error:", str(e))


# =====================================
# LIFESPAN (STARTUP & SHUTDOWN)
# =====================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    global db_pool, bot_task

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        ssl="require",
        min_size=1,
        max_size=5
    )

    await create_tables()
    await migrate_json_data()

    print("PostgreSQL connected successfully!")
    print("Monetag reward system ready (REWARDS DISABLED)!")

    if bot and dp:
        await bot.delete_webhook(drop_pending_updates=True)
        bot_task = asyncio.create_task(dp.start_polling(bot))
        print("Telegram Bot polling started!")

    yield

    if bot_task:
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass

    if db_pool:
        await db_pool.close()

    if bot:
        await bot.session.close()


app = FastAPI(title="Tasbeh Bot API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =====================================
# LEADERBOARD & ZIKR SYNC ENDPOINTS
# =====================================

@app.get("/leaderboard")
async def get_leaderboard(limit: int = Query(default=20, ge=1, le=100)):
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, COALESCE(username, '') AS username, COALESCE(balance, 0) AS total_zikr
            FROM tasbeh_users
            ORDER BY COALESCE(balance, 0) DESC, user_id ASC
            LIMIT $1
        """, limit)

    leaderboard = [
        {
            "rank": idx,
            "user_id": r["user_id"],
            "username": r["username"] or f"Фойдаланувчи {r['user_id']}",
            "total_zikr": int(r["total_zikr"] or 0)
        }
        for idx, r in enumerate(rows, start=1)
    ]

    return {
        "status": "ok",
        "leaderboard": leaderboard
    }


@app.post("/sync-zikr")
async def sync_zikr(payload: ZikrSyncPayload):
    if payload.user_id <= 0 or payload.total_zikr < 0:
        raise HTTPException(status_code=400, detail="Invalid payload")

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("""
            INSERT INTO tasbeh_users (user_id, balance, username)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id)
            DO UPDATE SET
                balance = GREATEST(COALESCE(tasbeh_users.balance, 0), $2),
                username = CASE WHEN $3 <> '' THEN $3 ELSE tasbeh_users.username END
            RETURNING balance
        """,
            payload.user_id,
            payload.total_zikr,
            payload.username.strip()
        )

    return {
        "status": "ok",
        "user_id": payload.user_id,
        "total_zikr": int(row["balance"] or 0)
    }


# =====================================
# MONETAG PROCESSING (REWARDS DISABLED)
# =====================================

async def process_reward(
    telegram_id: str = "",
    userid: str = "",
    secret: str = "",
    reward_event_type: str = "",
    event_type: str = "",
    ymid: str = "",
    event_id: str = "",
    transaction_id: str = "",
    zone_id: str = "",
    sub_zone_id: str = "",
    estimated_price: str = "",
    request_var: str = ""
):
    user_value = telegram_id or userid

    if not user_value or not user_value.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Invalid telegram_id or userid"
        )

    reward_type = reward_event_type or event_type

    if reward_type and reward_type != "valued":
        return {
            "status": "ignored",
            "reason": "Event is not valued"
        }

    user_id = int(user_value)
    unique_id = ymid or event_id or transaction_id or f"test_event_{asyncio.get_event_loop().time()}"

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                if unique_id:
                    inserted = await conn.fetchval("""
                        INSERT INTO monetag_events
                        (event_id, user_id, reward)
                        VALUES ($1, $2, 0)
                        ON CONFLICT (event_id) DO NOTHING
                        RETURNING event_id
                    """,
                        unique_id,
                        user_id
                    )

                    if not inserted:
                        return {
                            "status": "duplicate",
                            "message": "This event was already processed"
                        }

                await conn.execute("""
                    INSERT INTO tasbeh_users
                    (user_id, balance)
                    VALUES ($1, 0)
                    ON CONFLICT (user_id) DO NOTHING
                """,
                    user_id
                )

                current_balance = await conn.fetchval("""
                    SELECT balance
                    FROM tasbeh_users
                    WHERE user_id = $1
                """,
                    user_id
                )

        return {
            "status": "ok",
            "message": "Ad event processed without reward",
            "user_id": user_id,
            "reward": 0,
            "balance": current_balance
        }

    except HTTPException:
        raise

    except Exception as e:
        print("MONETAG ERROR:", str(e))
        raise HTTPException(
            status_code=500,
            detail="Reward processing failed"
        )


# =====================================
# MONETAG POSTBACK
# =====================================

@app.get("/postback")
async def monetag_postback(
    telegram_id: str = Query(default=""),
    userid: str = Query(default=""),
    secret: str = Query(default=""),
    reward_event_type: str = Query(default=""),
    event_type: str = Query(default=""),
    ymid: str = Query(default=""),
    event_id: str = Query(default=""),
    transaction_id: str = Query(default=""),
    zone_id: str = Query(default=""),
    sub_zone_id: str = Query(default=""),
    estimated_price: str = Query(default=""),
    request_var: str = Query(default="")
):
    return await process_reward(
        telegram_id=telegram_id,
        userid=userid,
        secret=secret,
        reward_event_type=reward_event_type,
        event_type=event_type,
        ymid=ymid,
        event_id=event_id,
        transaction_id=transaction_id,
        zone_id=zone_id,
        sub_zone_id=sub_zone_id,
        estimated_price=estimated_price,
        request_var=request_var
    )


# =====================================
# CHECK BALANCE
# =====================================

@app.get("/reward")
async def manual_reward(userid: str = ""):
    if not userid.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Invalid userid"
        )

    async with db_pool.acquire() as conn:
        balance = await conn.fetchval("""
            SELECT balance
            FROM tasbeh_users
            WHERE user_id = $1
        """, int(userid))

    return {
        "status": "success",
        "user_id": int(userid),
        "balance": balance if balance is not None else 0
    }


# =====================================
# REGISTER USER
# =====================================

@app.get("/register")
async def register_user(
    userid: str = "",
    username: str = ""
):
    if not userid.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Invalid userid"
        )

    async with db_pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tasbeh_users
            (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
        """,
            int(userid),
            username
        )

    return {"status": "success"}


# =====================================
# DATABASE CHECK
# =====================================

@app.get("/db-check")
async def db_check():
    async with db_pool.acquire() as conn:
        result = await conn.fetchval("SELECT 1")

    return {
        "database": "connected",
        "test": result
    }


# =====================================
# ROOT & HEALTH
# =====================================

@app.get("/")
async def root():
    return {
        "status": "online",
        "message": "Tasbeh Bot API is running (Rewards Disabled)",
        "database": "PostgreSQL",
        "monetag": "bypassed",
        "reward": 0
    }


@app.get("/health")
async def health():
    return {"status": "ok"}
