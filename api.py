import os
import json
import time
from fastapi import FastAPI, HTTPException
from aiogram import Bot

app = FastAPI()

TOKEN = os.getenv("BOT_TOKEN")
MONETAG_SECRET = os.getenv("MONETAG_SECRET")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8274122612"))

bot = Bot(token=TOKEN) if TOKEN else None

USERS_FILE = "users_data.json"

def load_users_data():
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {
                    "users": set(data.get("users", [ADMIN_ID])),
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

def save_users_data(users, user_balances, user_names, referred_users, user_completed_tasks):
    data = {
        "users": list(users),
        "balances": user_balances,
        "names": user_names,
        "referred": list(referred_users),
        "completed_tasks": user_completed_tasks
    }
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)


# --- Функция для начисления награды и отправки сообщения ---
async def process_reward(userid: str, secret: str, reward_event_type: str = "valued"):
    # Проверка секретного ключа Monetag (если задан)
    if MONETAG_SECRET and secret and secret != MONETAG_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret key")

    if userid and userid.isdigit():
        user_id = int(userid)
        reward = 200  # 📌 Сумма награды

        db = load_users_data()
        balances = db["balances"]

        if user_id not in balances:
            balances[user_id] = 0

        balances[user_id] += reward
        save_users_data(db["users"], balances, db["names"], db["referred"], db["completed_tasks"])

        try:
            if bot:
                await bot.send_message(
                    user_id,
                    f"⭐ <b>Рекламани кўрганингиз учун ташаккур!</b>\n"
                    f"💎 Ҳисобингизга <b>+{reward} балл</b> қўшилди.",
                    parse_mode="HTML"
                )
        except Exception as e:
            print(f"Failed to send telegram message: {e}")

        return {"status": "ok", "message": "Reward granted", "user_id": user_id}

    return {"status": "ignored", "reason": "Invalid or empty userid"}


# --- MONETAG POSTBACK ENDPOINTS ---
@app.get("/postback")
async def monetag_postback(
    userid: str = "",
    secret: str = "",
    reward_event_type: str = ""
):
    return await process_reward(userid, secret, reward_event_type)

@app.get("/ymid")
async def monetag_ymid(
    userid: str = "",
    secret: str = "",
    reward_event_type: str = ""
):
    return await process_reward(userid, secret, reward_event_type)


# --- REWARD ENDPOINT (Для запросов /reward?userid=...) ---
@app.get("/reward")
async def manual_reward(userid: str = ""):
    if userid and userid.isdigit():
        user_id = int(userid)
        db = load_users_data()
        balances = db["balances"]
        
        current_balance = balances.get(user_id, 0)
        return {
            "status": "success",
            "user_id": user_id,
            "balance": current_balance
        }
    return {"status": "error", "message": "Invalid userid"}


@app.get("/")
async def root():
    return {"status": "online", "message": "API & Monetag Postback Server is running!"}
