import os
import asyncio
import json
import time
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import asyncpg

# Настройки бота ва ўзгарувчилар
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8274122612"))
DATABASE_URL = os.getenv("DATABASE_URL")

# WEBAPP_URL ни GitHub Pages манзилига мослаб қўямиз
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://rakhmatjonablakulov-dev.github.io/tasbeh_bot/")

if not TOKEN:
    print("⚠️ Огоҳлантириш: BOT_TOKEN аниқланмади! Муҳит ўзгарувчиларини текширинг.")

bot = Bot(token=TOKEN) if TOKEN else None
dp = Dispatcher()

TASKS_FILE = "tasks.json"
db_pool = None


# --- ПОСТГРЕСС БАЗА БИЛАН ИШЛАШ ФУНКЦИЯЛАРИ ---
async def get_db_pool():
    global db_pool
    if not DATABASE_URL:
        return None
    if db_pool is None:
        db_pool = await asyncpg.create_pool(DATABASE_URL, ssl="require", min_size=1, max_size=5)
    return db_pool


async def db_get_user(user_id: int):
    pool = await get_db_pool()
    if not pool:
        return {"balance": 0, "username": "Foydalanuvchi", "completed_tasks": {}}
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance, username, completed_tasks FROM tasbeh_users WHERE user_id = $1", user_id)
        if row:
            tasks = row["completed_tasks"]
            if isinstance(tasks, str):
                tasks = json.loads(tasks)
            return {
                "balance": row["balance"],
                "username": row["username"] or "Foydalanuvchi",
                "completed_tasks": tasks or {}
            }
        else:
            await conn.execute("""
                INSERT INTO tasbeh_users (user_id, balance, username, completed_tasks)
                VALUES ($1, $2, $3, $4::jsonb)
                ON CONFLICT (user_id) DO NOTHING
            """, user_id, 1000 if user_id == ADMIN_ID else 0, "Admin" if user_id == ADMIN_ID else "Foydalanuvchi", json.dumps({}))
            return {
                "balance": 1000 if user_id == ADMIN_ID else 0,
                "username": "Admin" if user_id == ADMIN_ID else "Foydalanuvchi",
                "completed_tasks": {}
            }


async def db_update_balance_and_tasks(user_id: int, balance_change: int = 0, completed_tasks: dict = None, username: str = None):
    pool = await get_db_pool()
    if not pool:
        return
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO tasbeh_users (user_id, balance, username, completed_tasks)
            VALUES ($1, 0, '', '{}'::jsonb)
            ON CONFLICT (user_id) DO NOTHING
        """, user_id)

        if balance_change != 0:
            await conn.execute("UPDATE tasbeh_users SET balance = balance + $2 WHERE user_id = $1", user_id, balance_change)
        if completed_tasks is not None:
            await conn.execute("UPDATE tasbeh_users SET completed_tasks = $2::jsonb WHERE user_id = $1", user_id, json.dumps(completed_tasks))
        if username:
            await conn.execute("UPDATE tasbeh_users SET username = $2 WHERE user_id = $1", user_id, username)


async def db_get_all_users():
    pool = await get_db_pool()
    if not pool:
        return []
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT user_id, balance, username FROM tasbeh_users ORDER BY user_id DESC")
        return [dict(r) for r in rows]


# --- ВАЗИФАЛАРНИ САҚЛАШ ВА ЎҚИШ (Файл орқали) ---
def load_tasks():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, "r", encoding="utf-8") as f:
                tasks = json.load(f)
                current_time = time.time()

                active_tasks = []
                for task in tasks:
                    expires_at = task.get('expires_at')
                    if expires_at is None or expires_at > current_time:
                        active_tasks.append(task)

                if len(active_tasks) != len(tasks):
                    save_tasks(active_tasks)

                return active_tasks
        except Exception:
            pass

    return [
        {"id": 1, "title": "📢 Расмий каналга обуна бўлиш", "reward": 50, "url": "https://t.me/telegram", "expires_at": None},
        {"id": 2, "title": "⭐ Лойиҳани қўллаб-қувватлаш", "reward": 30, "url": "https://t.me/telegram", "expires_at": None}
    ]


def save_tasks(tasks):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=4)


def delete_task_by_id(task_id):
    tasks = load_tasks()
    tasks = [t for t in tasks if t['id'] != task_id]
    save_tasks(tasks)
    return tasks


tasks_list = load_tasks()


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
    builder.button(text="🏆 Рейтинг")
    builder.button(text="👥 Дўстларни таклиф")
    builder.button(text="📋 Вазифалар")
    builder.adjust(1, 2, 1)
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
    builder.adjust(2, 2, 2, 1)
    return builder.as_markup(resize_keyboard=True)


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "Foydalanuvchi"

    await db_update_balance_and_tasks(user_id, balance_change=0, username=user_name)

    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])

        if referrer_id != user_id:
            await db_update_balance_and_tasks(referrer_id, balance_change=2000)

            try:
                if bot:
                    await bot.send_message(
                        referrer_id,
                        f"🎉 <b>Табриклаймиз!</b>\n\n"
                        f"👥 Сизнинг реферал ҳаволангиз орқали <b>{user_name}</b> ботга аъзо бўлди!\n"
                        f"💎 Ҳисобингизга <b>+2000 балл</b> қўшилди.",
                        parse_mode="HTML"
                    )
            except Exception:
                pass

    welcome_text = (
        f"✨ <b>Ассалому алайкум ва роҳматуллоҳи ва барокатуҳ, {user_name}!</b>\n\n"
        f"🌙 <b>«Муборак Тасбеҳ ва зикрлар»</b> ботига хуш келибсиз!\n\n"
        f"<i>«Огоҳ бўлингизким, Аллоҳни зикр қилиш билан қалблар ором олур!»</i> (Раъд сураси, 28-оят).\n\n"
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
        "📿 <b>Т А С Б Е Х!</b>\n\n"
        "Қуйидаги тугма орқали интерактив мини-иловага ўтинг ва зикр қилишни бошланг:",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("❌ Жараён бекор қилинди.", reply_markup=get_main_menu())


@dp.message(F.text == "🏆 Рейтинг")
async def show_leaderboard(message: types.Message):
    all_users = await db_get_all_users()
    sorted_users = sorted(all_users, key=lambda x: x['balance'], reverse=True)[:10]

    text = (
        "🏆 <b>Энг фаол фойдаланувчилар рейтинги (Топ-10)</b>\n\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "🏅 <b>Пешқадамлар рўйхати:</b>\n\n"
    )

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for index, u in enumerate(sorted_users):
        name = u['username'] or f"Фойдаланувчи {u['user_id']}"
        balance = u['balance']
        medal = medals[index] if index < len(medals) else f"{index+1}."
        text += f"{medal} <b>{name}</b> — 💎 <b>{balance} балл</b>\n\n"

    await message.answer(text, parse_mode="HTML", reply_markup=get_main_menu())


@dp.message(F.text == "👥 Дўстларни таклиф")
async def process_referral(message: types.Message):
    if bot:
        bot_info = await bot.get_me()
        ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    else:
        ref_link = "#"

    await message.answer(
        "👥 <b>Дўстларни таклиф қилиш тизими</b>\n\n"
        "Яқинларингизни таклиф қилинг ва ҳар бир таклиф қилинган дўстингиз учун <b>2000 балл</b> олиб боринг!\n\n"
        "🔗 <b>Сизнинг шахсий линкингиз:</b>\n"
        f"<code>{ref_link}</code>",
        parse_mode="HTML"
    )


@dp.message(F.text == "📋 Вазифалар")
async def process_tasks(message: types.Message):
    global tasks_list
    tasks_list = load_tasks()

    if not tasks_list:
        await message.answer("📋 Ҳозирча янги вазифалар мавжуд эмас.", reply_markup=get_main_menu())
        return

    user_id = message.from_user.id
    user_data = await db_get_user(user_id)
    completed_tasks_dict = user_data["completed_tasks"]

    active_tasks = [task for task in tasks_list if str(task['id']) not in completed_tasks_dict and task['id'] not in completed_tasks_dict]

    if not active_tasks:
        await message.answer(
            "🎉 <b>Табриклаймиз!</b>\n\nСиз барча мавжуд вазифаларни бажариб бўлдингиз.\nТегишли янги вазифалар қўшилишини кутинг!",
            parse_mode="HTML",
            reply_markup=get_main_menu()
        )
        return

    await message.answer("📋 <b>Кунлик вазифалар ва бонуслар</b>\n\nҚуйидаги вазифаларни бажариб баллар жамғаринг:", parse_mode="HTML")

    for task in active_tasks:
        builder = InlineKeyboardBuilder()
        builder.button(text="🔗 Бажариш", url=task['url'])
        builder.button(text="🎁 Текшириш", callback_data=f"check_task_{task['id']}")
        builder.adjust(2)

        await message.answer(
            f"📌 <b>{task['title']}</b>\n🎁 Мукофот: <b>+{task['reward']} балл</b>",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith(("check_task_", "noop_", "ok_task_")))
async def task_callback_handler(callback: types.CallbackQuery):
    if callback.data.startswith("noop_"):
        await callback.answer("Сиз бу вазифани аллақачон бажаргансиз!", show_alert=True)
        return

    if callback.data.startswith("ok_task_"):
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    task_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    global tasks_list
    tasks_list = load_tasks()
    target_task = next((t for t in tasks_list if t['id'] == task_id), None)

    if not target_task:
        await callback.answer("Вазифа топилмади ёки муддати тугаган!", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        return

    user_data = await db_get_user(user_id)
    completed_tasks = user_data["completed_tasks"]

    if str(task_id) in completed_tasks or task_id in completed_tasks:
        await callback.answer("Сиз бу вазифа учун аллақачон балл олгансиз!", show_alert=True)
        return

    url = target_task['url']
    if "t.me/" in url and bot:
        channel_username = "@" + url.split("t.me/")[-1].strip("/").split("?")[0]
        try:
            member = await bot.get_chat_member(chat_id=channel_username, user_id=user_id)
            if member.status in ["left", "kicked"]:
                await callback.answer(
                    "❌ Сиз ҳали каналга обуна бўлмагансиз!\nИлтимос, аввал обуна бўлинг ва кейин текширинг.",
                    show_alert=True
                )
                return
        except Exception:
            pass

    completed_tasks[str(task_id)] = True
    await db_update_balance_and_tasks(user_id, balance_change=target_task['reward'], completed_tasks=completed_tasks)

    await callback.answer("Вазифа муваффақиятли бажарилди!", show_alert=False)

    ok_builder = InlineKeyboardBuilder()
    ok_builder.button(text="OK", callback_data=f"ok_task_{task_id}")

    await callback.message.answer(
        f"✅ Ҳисобингизга <b>({target_task['reward']})</b> балл қўшилди!",
        reply_markup=ok_builder.as_markup(),
        parse_mode="HTML"
    )

    try:
        await callback.message.delete()
    except Exception:
        pass


@dp.message(F.web_app_data)
async def web_app_data_handler(message: Message):
    user_id = message.from_user.id
    data = message.web_app_data.data

    user_data = await db_get_user(user_id)

    if "add_bonus" in data or "watched_ad" in data or data:
        reward = 10
        await db_update_balance_and_tasks(user_id, balance_change=reward)
        new_balance = user_data["balance"] + reward

        await message.answer(
            f"🎉 <b>Табриклаймиз!</b>\n"
            f"⭐ Рекламани кўрганингиз учун ҳисобингизга <b>+{reward} балл</b> қўшилди!\n"
            f"💎 Жорий балансингиз: <b>{new_balance} балл</b>",
            parse_mode="HTML"
        )


@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Сизда бу буйруқни ишлатиш ҳуқуқи йўқ.")
        return

    await message.answer(
        "🛠 <b>Бошқарув панели (Админ)</b>\n\n"
        "Қуйидаги тугмалардан керакли бўлимни танланг:",
        reply_markup=get_admin_menu(),
        parse_mode="HTML"
    )


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    all_users = await db_get_all_users()
    await message.answer(
        f"📊 <b>Тизим статистикаси:</b>\n\n"
        f"• Жами фойдаланувчилар: <b>{len(all_users)} та</b>",
        parse_mode="HTML",
        reply_markup=get_admin_menu()
    )


@dp.message(F.text == "👥 Фойдаланувчилар")
async def admin_users_list(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    all_users = await db_get_all_users()
    if not all_users:
        await message.answer("📭 Ҳозирча ботга кирган фойдаланувчилар йўқ.", reply_markup=get_admin_menu())
        return

    header = f"👥 <b>Ботдаги фойдаланувчилар рўйхати (Жами: {len(all_users)} та):</b>\n\n"
    chunk = header

    for count, u in enumerate(all_users, start=1):
        name = u['username'] or "Номаълум"
        uid = u['user_id']
        line = f"{count}. <b>{name}</b> — ID: <code>{uid}</code>\n"

        if len(chunk) + len(line) > 3800:
            await message.answer(chunk, parse_mode="HTML")
            chunk = line
        else:
            chunk += line

    if chunk:
        await message.answer(chunk, parse_mode="HTML", reply_markup=get_admin_menu())


@dp.message(F.text == "📢 Рассылка юбориш")
async def admin_broadcast_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminBroadcast.waiting_for_message)
    await message.answer(
        "📢 Барча фойдаланувчиларга юбормоқчи бўлган хабарингизни юборинг:\n\n"
        "<i>Бекор қилиш учун 'отмена' денг.</i>",
        parse_mode="HTML"
    )


@dp.message(AdminBroadcast.waiting_for_message)
async def admin_broadcast_send(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text and message.text.lower() == "отмена":
        await state.clear()
        await message.answer("❌ Рассылка бекор қилинди.", reply_markup=get_admin_menu())
        return

    await state.clear()
    await message.answer("🚀 Рассылка юбориш жараёни бошланди...")

    all_users = await db_get_all_users()
    success = 0
    failed = 0
    for u in all_users:
        uid = u['user_id']
        try:
            if bot:
                await message.copy_to(chat_id=uid)
            success += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1

    await message.answer(
        f"✅ <b>Рассылка муваффақиятли якунланди!</b>\n\n"
        f"🟢 Етиб борди: <b>{success} та</b>\n"
        f"🔴 Блок қилганлар: <b>{failed} та</b>",
        parse_mode="HTML",
        reply_markup=get_admin_menu()
    )


@dp.message(F.text == "💰 Балансни бошқариш")
async def admin_balance_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminBalance.waiting_user_id)
    await message.answer("🆔 Балансини ўзгартирмоқчи бўлган фойдаланувчининг <b>Telegram ID</b> сини киритинг:", parse_mode="HTML")


@dp.message(AdminBalance.waiting_user_id)
async def admin_balance_get_id(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text)
        await state.update_data(target_id=target_id)
        await state.set_state(AdminBalance.waiting_amount)
        await message.answer(f"✍️ Фойдаланувчи (ID: {target_id}) учун қўшиладиган ёки айриладиган балл миқдорини киритинг (Масалан: <code>100</code> ёки <code>-50</code>):", parse_mode="HTML")
    except ValueError:
        await message.answer("❌ Фақат рақамлардан иборат ID киритинг:")


@dp.message(AdminBalance.waiting_amount)
async def admin_balance_get_amount(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        amount = int(message.text)
        data = await state.get_data()
        target_id = data.get("target_id")

        await db_update_balance_and_tasks(target_id, balance_change=amount)
        updated_user = await db_get_user(target_id)
        new_balance = updated_user["balance"]
        await state.clear()

        await message.answer(
            f"✅ Муваффақиятли! Фойдаланувчининг янги баланси: <b>{new_balance} балл</b>",
            parse_mode="HTML",
            reply_markup=get_admin_menu()
        )

        try:
            if bot:
                await bot.send_message(target_id, f"🎁 Админ томонидан балансингиз ўзгартирилди! Жорий баланс: {new_balance} балл")
        except Exception:
            pass

    except ValueError:
        await message.answer("❌ Фақат бутун рақам киритинг (масалан: 50 ёки -20):")


@dp.message(F.text == "📋 Вазифа қўшиш")
async def admin_task_start(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.set_state(AdminTask.waiting_title)
    await message.answer("✍️ Янги вазифа номини ва шартини киритинг (Масалан: <code>📢 Телеграм каналга обуна бўлиш</code>):", parse_mode="HTML")


@dp.message(AdminTask.waiting_title)
async def admin_task_get_title(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    await state.update_data(title=message.text)
    await state.set_state(AdminTask.waiting_reward)
    await message.answer("💰 Ушбу вазифа учун бериладиган мукофот (балл) миқдорини рақамда киритинг (Масалан: <code>50</code>):", parse_mode="HTML")


@dp.message(AdminTask.waiting_reward)
async def admin_task_get_reward(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        reward = int(message.text)
        await state.update_data(reward=reward)
        await state.set_state(AdminTask.waiting_url)
        await message.answer("🔗 Вазифа учун ҳаволани (URL ёки t.me манзилини) киритинг:")
    except ValueError:
        await message.answer("❌ Фақат рақам киритинг (масалан: 50):")


@dp.message(AdminTask.waiting_url)
async def admin_task_get_url(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    url = message.text.strip()
    await state.update_data(url=url)
    await state.set_state(AdminTask.waiting_hours)
    await message.answer(
        "⏳ Вазифа қанча соат давомида амал қилишини киритинг:\n"
        "<i>(Масалан: <code>24</code> соат, <code>48</code> соат ёки чексиз бўлса <code>0</code> рақамини киритинг)</i>",
        parse_mode="HTML"
    )


@dp.message(AdminTask.waiting_hours)
async def admin_task_get_hours(message: types.Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        hours = int(message.text)
        data = await state.get_data()
        title = data.get("title")
        reward = data.get("reward")
        url = data.get("url")

        expires_at = None
        if hours > 0:
            expires_at = time.time() + (hours * 3600)

        global tasks_list
        tasks_list = load_tasks()

        new_id = max((t['id'] for t in tasks_list), default=0) + 1
        new_task = {
            "id": new_id,
            "title": title,
            "reward": reward,
            "url": url,
            "expires_at": expires_at
        }

        tasks_list.append(new_task)
        save_tasks(tasks_list)

        await state.clear()

        time_info = f"{hours} соат" if hours > 0 else "Чексиз (муддатсиз)"
        await message.answer(
            f"✅ <b>Янги вазифа муваффақиятли қўшилди!</b>\n\n"
            f"📌 Номи: {title}\n"
            f"🎁 Мукофот: {reward} балл\n"
            f"⏳ Амал қилиш муддати: {time_info}\n"
            f"🔗 Ҳавола: {url}",
            parse_mode="HTML",
            reply_markup=get_admin_menu()
        )
    except ValueError:
        await message.answer("❌ Фақат бутун рақам киритинг (масалан: 24 ёки 0):")


@dp.message(F.text == "📋 Вазифаларни бошқариш")
async def admin_manage_tasks(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    global tasks_list
    tasks_list = load_tasks()

    if not tasks_list:
        await message.answer("📭 Ҳозирча фаол вазифалар йўқ.", reply_markup=get_admin_menu())
        return

    await message.answer("📋 <b>Амалдаги вазифалар рўйхати:</b>\nКеракли вазифани ўчириш учун қуйидаги тугмани босинг:", parse_mode="HTML")

    for task in tasks_list:
        builder = InlineKeyboardBuilder()
        builder.button(text="🗑 Ўчириш (Бекор қилиш)", callback_data=f"del_task_{task['id']}")

        expires_str = "Чексиз"
        if task.get('expires_at'):
            remaining = int((task['expires_at'] - time.time()) / 3600)
            expires_str = f"Қолди: ~{remaining} соат" if remaining > 0 else "Муддати тугамоқда"

        await message.answer(
            f"📌 <b>{task['title']}</b>\n🎁 Мукофот: {task['reward']} балл\n⏳ Муддати: {expires_str}\n🔗 Ҳавола: {task['url']}",
            reply_markup=builder.as_markup(),
            parse_mode="HTML"
        )


@dp.callback_query(F.data.startswith("del_task_"))
async def callback_delete_task(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Сизда бу ҳуқуқ йўқ!", show_alert=True)
        return

    task_id = int(callback.data.split("_")[2])

    remaining_tasks = delete_task_by_id(task_id)
    global tasks_list
    tasks_list = remaining_tasks

    await callback.answer("✅ Вазифа муваффақиятли ўчирилди!", show_alert=True)

    try:
        await callback.message.edit_text("❌ <b>Ушбу вазифа бекор қилинди ва ўчирилди.</b>", parse_mode="HTML")
    except Exception:
        pass


async def main():
    if not bot:
        print("❌ Хатолик: BOT_TOKEN созланмаганлиги сабабли бот ишга тушмади!")
        return

    print("Тасбеҳ боти polling орқали PostgreSQL базаси билан ишга тушмоқда...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
