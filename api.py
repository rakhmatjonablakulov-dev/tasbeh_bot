
import os
import json
import hmac
import asyncio
from pathlib import Path

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from aiogram import Bot

# =====================================
# SETTINGS
# =====================================

TOKEN = os.getenv("BOT_TOKEN")
MONETAG_SECRET = os.getenv("MONETAG_SECRET")
DATABASE_URL = os.getenv("DATABASE_URL")

ADMIN_ID = int(os.getenv("ADMIN_ID", "8274122612"))
REWARD_AMOUNT = 200

USERS_FILE = Path("users_data.json")

app = FastAPI(title="Tasbeh Bot API")

bot = Bot(token=TOKEN) if TOKEN else None
db_pool = None


# =====================================
# DATABASE STARTUP
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
                reward BIGINT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)

        await conn.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_monetag_events_user
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

        users = set(data.get("users", []))
        balances = data.get("balances", {})
        names = data.get("names", {})
        referred = set(data.get("referred", []))
        tasks = data.get("completed_tasks", {})

        all_ids = (
            users
            | {int(x) for x in balances}
            | {int(x) for x in names}
            | {int(x) for x in tasks}
        )

        async with db_pool.acquire() as conn:
            async with conn.transaction():

                for uid in all_ids:
                    uid = int(uid)

                    await conn.execute("""
                        INSERT INTO tasbeh_users
                        (user_id, balance, username, referred,
                         completed_tasks)
                        VALUES ($1, $2, $3, $4, $5::jsonb)
                        ON CONFLICT (user_id) DO NOTHING
                    """,
                        uid,
                        int(balances.get(str(uid), 0)),
                        str(names.get(str(uid), "")),
                        uid in referred,
                        json.dumps(tasks.get(str(uid), {}))
                    )

        print("Old JSON data migration completed")

    except Exception as e:
        print("JSON migration error:", str(e))


# =====================================
# STARTUP AND SHUTDOWN
# =====================================

@app.on_event("startup")
async def startup():

    global db_pool

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing in Render")

    db_pool = await asyncpg.create_pool(
        DATABASE_URL,
        ssl="require",
        min_size=1,
        max_size=5
    )

    await create_tables()
    await migrate_json_data()

    print("PostgreSQL connected successfully!")


@app.on_event("shutdown")
async def shutdown():

    global db_pool

    if db_pool:
        await db_pool.close()

    if bot:
        await bot.session.close()


# =====================================
# MONETAG REWARD PROCESSING
# =====================================

async def process_reward(
    userid: str,
    secret: str,
    reward_event_type: str,
    event_id: str = ""
):

    if not MONETAG_SECRET:
        raise HTTPException(
            status_code=500,
            detail="MONETAG_SECRET is not configured"
        )

    if not secret or not hmac.compare_digest(
        secret, MONETAG_SECRET
    ):
        raise HTTPException(
            status_code=403,
            detail="Invalid secret"
        )

    if reward_event_type != "valued":
        return {
            "status": "ignored",
            "reason": "Event is not valued"
        }

    if not userid or not userid.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Invalid userid"
        )

    user_id = int(userid)

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():

                # If Monetag sends a unique event ID,
                # prevent processing it twice.
                if event_id:
                    inserted = await conn.fetchval("""
                        INSERT INTO monetag_events
                        (event_id, user_id, reward)
                        VALUES ($1, $2, $3)
                        ON CONFLICT (event_id) DO NOTHING
                        RETURNING event_id
                    """, event_id, user_id, REWARD_AMOUNT)

                    if not inserted:
                        return {
                            "status": "duplicate",
                            "message": "Event already processed"
                        }

                row = await conn.fetchrow("""
                    INSERT INTO tasbeh_users
                    (user_id, balance)
                    VALUES ($1, $2)
                    ON CONFLICT (user_id)
                    DO UPDATE SET
                        balance = tasbeh_users.balance + $2
                    RETURNING balance
                """, user_id, REWARD_AMOUNT)

                new_balance = row["balance"]

        print(
            f"MONETAG: user={user_id}, "
            f"reward={REWARD_AMOUNT}, "
            f"balance={new_balance}"
        )

        # Telegram notification
        if bot:
            try:
                await bot.send_message(
                    user_id,
                    "⭐ <b>Рекламани кўрганингиз учун ташаккур!</b>\n"
                    f"💎 Ҳисобингизга <b>+{REWARD_AMOUNT} балл</b> "
                    "қўшилди.",
                    parse_mode="HTML"
                )
            except Exception as e:
                print("Telegram notification error:", str(e))

        return {
            "status": "ok",
            "message": "Reward granted",
            "user_id": user_id,
            "reward": REWARD_AMOUNT,
            "balance": new_balance
        }

    except HTTPException:
        raise

    except Exception as e:
        print("Reward processing error:", str(e))
        raise HTTPException(
            status_code=500,
            detail="Reward processing failed"
        )


# =====================================
# MONETAG POSTBACK
# =====================================

@app.get("/postback")
async def monetag_postback(
    userid: str = Query(default=""),
    secret: str = Query(default=""),
    reward_event_type: str = Query(default=""),
    event_id: str = Query(default=""),
    transaction_id: str = Query(default="")
):

    unique_id = event_id or transaction_id

    return await process_reward(
        userid=userid,
        secret=secret,
        reward_event_type=reward_event_type,
        event_id=unique_id
    )


@app.get("/ymid")
async def monetag_ymid(
    userid: str = Query(default=""),
    secret: str = Query(default=""),
    reward_event_type: str = Query(default=""),
    event_id: str = Query(default=""),
    transaction_id: str = Query(default="")
):

    unique_id = event_id or transaction_id

    return await process_reward(
        userid=userid,
        secret=secret,
        reward_event_type=reward_event_type,
        event_id=unique_id
    )


# =====================================
# CHECK USER BALANCE
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
# ADD USER
# =====================================

@app.get("/register")
async def register_user(userid: str = "", username: str = ""):

    if not userid.isdigit():
        raise HTTPException(
            status_code=400,
            detail="Invalid userid"
        )

    async with db_pool.acquire() as conn:

        await conn.execute("""
            INSERT INTO tasbeh_users (user_id, username)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO NOTHING
        """, int(userid), username)

    return {"status": "success"}


# =====================================
# DATABASE TEST
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
# ROOT
# =====================================

@app.get("/")
async def root():

    return {
        "status": "online",
        "message": "Tasbeh Bot API is running",
        "database": "PostgreSQL",
        "reward": REWARD_AMOUNT
    }


@app.get("/health")
async def health():

    return {"status": "ok"}
