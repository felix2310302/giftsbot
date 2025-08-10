# bot.py
import os
import sqlite3
import time
import uuid
import logging
import asyncio
import requests
from datetime import datetime
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ========== CONFIG (env) ==========
API_TOKEN = os.getenv("API_TOKEN")  # обязательно
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
RETURN_URL = os.getenv("RETURN_URL", "")  # желательно: https://t.me/YourBotUsername
ADMINS = os.getenv("ADMINS", "")  # например "123456789,987654321"
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID", "")  # numeric chat id менеджера (опционально)
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "giftsmanage")  # default @giftsmanage
TEST_MODE = os.getenv("TEST_MODE", "true").lower() in ("1", "true", "yes")
DB_PATH = os.getenv("DB_PATH", "giftsfelix.db")

ADMINS = [int(x) for x in ADMINS.split(",") if x.strip().isdigit()]

if not API_TOKEN:
    raise RuntimeError("API_TOKEN required in env")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# ========== DB helpers ==========
def db_connect():
    return sqlite3.connect(DB_PATH)

def init_db():
    conn = db_connect()
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price INTEGER,
        description TEXT,
        image_file_id TEXT,
        created_at TEXT
    );
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        gift_id INTEGER,
        status TEXT,
        amount INTEGER,
        local_invoice TEXT,
        payment_id TEXT,
        created_at TEXT,
        updated_at TEXT
    );
    """)
    conn.commit()
    conn.close()

def db_exec(sql, params=(), fetch=False):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(sql, params)
    if fetch:
        rows = cur.fetchall()
        conn.commit()
        conn.close()
        return rows
    conn.commit()
    conn.close()
    return None

# ========== Bootstrap sample gifts ==========
def ensure_sample_gifts():
    rows = db_exec("SELECT id FROM gifts LIMIT 1", fetch=True)
    if not rows:
        now = datetime.utcnow().isoformat()
        db_exec("INSERT INTO gifts (name, price, description, image_file_id, created_at) VALUES (?, ?, ?, ?, ?)",
                ("NFT Котик", 500, "Милый NFT котик — цифровой подарок", None, now))
        db_exec("INSERT INTO gifts (name, price, description, image_file_id, created_at) VALUES (?, ?, ?, ?, ?)",
                ("NFT Машина", 1200, "Коллекционная машина", None, now))
        log.info("Sample gifts inserted")

# ========== YooKassa helpers ==========
YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"

def create_yookassa_payment(local_invoice_id: str, amount_rub: int, description: str):
    """Returns (payment_id, confirmation_url, error_message)"""
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return None, None, "YOOKASSA not configured"
    url = f"{YOOKASSA_API_BASE}/payments"
    headers = {"Idempotence-Key": local_invoice_id, "Content-Type": "application/json"}
    body = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "payment_method_data": {"type": "bank_card"},
        "confirmation": {"type": "redirect", "return_url": RETURN_URL or "https://t.me/"},
        "description": description
    }
    try:
        resp = requests.post(url, json=body, headers=headers, auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY), timeout=10)
        if resp.status_code not in (200, 201):
            log.warning("YooKassa create payment failed: %s %s", resp.status_code, resp.text[:300])
            return None, None, f"YooKassa error {resp.status_code}"
        data = resp.json()
        payment_id = data.get("id")
        confirmation = data.get("confirmation") or {}
        confirmation_url = confirmation.get("confirmation_url")
        return payment_id, confirmation_url, None
    except Exception as e:
        log.exception("create_yookassa_payment failed")
        return None, None, str(e)

def get_yookassa_payment(payment_id: str):
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return None
    url = f"{YOOKASSA_API_BASE}/payments/{payment_id}"
    try:
        resp = requests.get(url, auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY), timeout=10)
        if resp.status_code != 200:
            log.warning("YooKassa get payment %s -> %s", payment_id, resp.status_code)
            return None
        return resp.json()
    except Exception as e:
        log.exception("get_yookassa_payment failed")
        return None

# ========== Orders / gifts helpers ==========
def create_order(chat_id: int, gift_id: int, amount: int):
    local_invoice = f"inv_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow().isoformat()
    db_exec("INSERT INTO orders (chat_id, gift_id, status, amount, local_invoice, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, gift_id, "pending", amount, local_invoice, now, now))
    row = db_exec("SELECT id FROM orders WHERE local_invoice = ?", (local_invoice,), fetch=True)
    return row[0][0], local_invoice

def set_order_payment(order_id: int, payment_id: str):
    db_exec("UPDATE orders SET payment_id = ?, status = ?, updated_at = ? WHERE id = ?",
            (payment_id, "payment_created", datetime.utcnow().isoformat(), order_id))

def set_order_status(order_id: int, status: str):
    db_exec("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.utcnow().isoformat(), order_id))

def get_order(order_id: int):
    rows = db_exec("SELECT id, chat_id, gift_id, status, amount, local_invoice, payment_id FROM orders WHERE id = ?", (order_id,), fetch=True)
    return rows[0] if rows else None

def get_pending_orders():
    return db_exec("SELECT id, payment_id, local_invoice FROM orders WHERE status IN ('payment_created','pending')", fetch=True)

def get_gifts_count():
    rows = db_exec("SELECT COUNT(*) FROM gifts", fetch=True)
    return rows[0][0] if rows else 0

def get_gift_by_index(index: int):
    rows = db_exec("SELECT id, name, price, description, image_file_id FROM gifts ORDER BY id LIMIT 1 OFFSET ?", (index,), fetch=True)
    return rows[0] if rows else None

def get_gift_by_id(gid: int):
    rows = db_exec("SELECT id, name, price, description, image_file_id FROM gifts WHERE id = ?", (gid,), fetch=True)
    return rows[0] if rows else None

def save_user(message: types.Message):
    db_exec("INSERT OR REPLACE INTO users (chat_id, first_name, last_name, username, created_at) VALUES (?, ?, ?, ?, ?)",
            (message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "", message.from_user.username or "", datetime.utcnow().isoformat()))

def notify_admins_text(text: str, buttons: list = None):
    """Send text to ADMINS with optional inline buttons (list of (text,url))"""
    kb = None
    if buttons:
        kb = types.InlineKeyboardMarkup()
        for t,u in buttons:
            kb.add(types.InlineKeyboardButton(t, url=u))
    for adm in ADMINS:
        try:
            bot.send_message(adm, text, reply_markup=kb, disable_web_page_preview=True)
        except Exception as e:
            log.warning("Notify admin failed %s: %s", adm, e)

def notify_admins_order_created(order_id: int):
    o = get_order(order_id)
    if not o:
        return
    oid, chat_id, gift_id, status, amount, local_invoice, payment_id = o
    gift = get_gift_by_id(gift_id)
    gname = gift[1] if gift else "—"
    # buyer info
    user_rows = db_exec("SELECT username, first_name FROM users WHERE chat_id = ?", (chat_id,), fetch=True)
    if user_rows:
        username = user_rows[0][0]
        first = user_rows[0][1]
    else:
        username = None
        first = ""
    contact_url = None
    if username:
        contact_url = f"https://t.me/{username}"
    else:
        contact_url = f"tg://user?id={chat_id}"
    text = f"Новый заказ #{oid}\nПодарок: {gname}\nСумма: {amount}₽\nПокупатель: {first} (id: {chat_id})"
    buttons = [("Написать покупателю", contact_url)]
    notify_admins_text(text, buttons)

# ========== FSMs ==========
class UploadStates(StatesGroup):
    waiting_for_screenshot = State()

class AddGiftStates(StatesGroup):
    waiting_for_photo = State()
    waiting_for_name = State()
    waiting_for_price = State()
    waiting_for_description = State()

# ========== Handlers: start/help/catalog/buy/sell ==========
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    save_user(message)
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🛒 Купить подарок", "💼 Мои заказы")
    kb.row("💰 Продать свой подарок", "📜 Помощь")
    kb.add("⭐ Поделиться")
    if message.from_user.id in ADMINS:
        kb.add("🛠️ Админ")
    await message.answer("Привет! Я GiftsFelix — магазин цифровых подарков. Выберите действие:", reply_markup=kb)

@dp.message_handler(lambda m: m.text == "📜 Помощь" or m.text == "/help")
async def cmd_help(message: types.Message):
    text = (
        "📜 Команды и помощь\n\n"
        "🛒 Купить подарок — открыть каталог и купить\n"
        "💰 Продать свой подарок — инструкции, как отправить подарок менеджеру\n"
        "💼 Мои заказы — список ваших заказов\n\n"
        "Админ: /addgift — добавить подарок (пошагово)\n"
        "/listorders — просмотреть заказы\n"
        "/confirm <order_id> — подтвердить и выслать подарок\n"
        "/decline <order_id> — отменить заказ\n"
    )
    await message.answer(text)

# Catalog browsing with pagination
@dp.message_handler(lambda m: m.text == "🛒 Купить подарок" or m.text == "/buy")
async def cmd_buy(message: types.Message):
    save_user(message)
    count = get_gifts_count()
    if count == 0:
        await message.answer("Пока нет доступных подарков.")
        return
    await show_gift_page(message.chat.id, 0)

async def show_gift_page(chat_id: int, index: int):
    count = get_gifts_count()
    if count == 0:
        await bot.send_message(chat_id, "Пока нет подарков.")
        return
    if index < 0:
        index = 0
    if index >= count:
        index = count - 1
    g = get_gift_by_index(index)
    if not g:
        await bot.send_message(chat_id, "Ошибка при получении подарка.")
        return
    gid, name, price, descr, image_file_id = g
    caption = f"*{name}*\n{descr}\n\nЦена: {price}₽\n\n({index+1}/{count})"
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Купить", callback_data=f"buy:{gid}"))
    nav = []
    if index > 0:
        nav.append(types.InlineKeyboardButton("⬅️ Назад", callback_data=f"page:{index-1}"))
    if index < count-1:
        nav.append(types.InlineKeyboardButton("Вперед ➡️", callback_data=f"page:{index+1}"))
    if nav:
        kb.row(*nav)
    # send photo if exists else text
    if image_file_id:
        try:
            await bot.send_photo(chat_id, image_file_id, caption=caption, parse_mode="Markdown", reply_markup=kb)
        except Exception as e:
            log.warning("send_photo failed: %s", e)
            await bot.send_message(chat_id, caption, parse_mode="Markdown", reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("page:"))
async def cb_page(callback_q: types.CallbackQuery):
    index = int(callback_q.data.split(":",1)[1])
    await show_gift_page(callback_q.from_user.id, index)
    await bot.answer_callback_query(callback_q.id)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy:"))
async def cb_buy(callback_q: types.CallbackQuery):
    chat_id = callback_q.from_user.id
    gid = int(callback_q.data.split(":",1)[1])
    gift = get_gift_by_id(gid)
    if not gift:
        await bot.answer_callback_query(callback_q.id, "Подарок не найден.")
        return
    _, name, price, descr, img = gift
    order_id, local_invoice = create_order(chat_id, gid, price)
    # create YooKassa payment
    payment_id, confirmation_url, err = create_yookassa_payment(local_invoice, price, f"Order #{order_id} - {name}")
    if err:
        # fallback demo link
        demo_link = f"https://example.com/pay?invoice={local_invoice}"
        set_order_payment(order_id, payment_id or "")
        # notify admins
        notify_admins_order_created(order_id)
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("Я оплатил — отправить скрин менеджеру", callback_data=f"paid:{order_id}"))
        await bot.send_message(chat_id,
            f"Создан заказ #{order_id} на сумму {price}₽.\n\nСсылка для оплаты (демо):\n{demo_link}\n\n"
            f"*Чтобы получить подарок*: отправьте подтверждение оплаты (чек/скрин) менеджеру @{MANAGER_USERNAME} и нажмите кнопку «Я оплатил» — мы переслём скрин менеджеру для проверки.",
            reply_markup=kb, parse_mode="Markdown"
        )
        await bot.answer_callback_query(callback_q.id, "Заказ создан (демо). Ссылка в чате.")
        return
    # save payment_id
    set_order_payment(order_id, payment_id)
    notify_admins_order_created(order_id)
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Оплатить (ЮKassa)", url=confirmation_url))
    kb.add(types.InlineKeyboardButton("Я оплатил — отправить скрин менеджеру", callback_data=f"paid:{order_id}"))
    await bot.send_message(chat_id,
        f"Создан заказ #{order_id} на сумму {price}₽.\n\nПерейдите по кнопке для оплаты через ЮKassa.\n\n"
        f"*Чтобы получить подарок*: отправьте подтверждение оплаты (чек/скрин) менеджеру @{MANAGER_USERNAME} и нажмите «Я оплатил».",
        reply_markup=kb, parse_mode="Markdown"
    )
    await bot.answer_callback_query(callback_q.id, "Ссылка для оплаты отправлена в чат.")

# Paid -> ask user to upload screenshot
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("paid:"))
async def cb_paid(callback_q: types.CallbackQuery):
    order_id = int(callback_q.data.split(":",1)[1])
    await bot.send_message(callback_q.from_user.id, "Пожалуйста, пришлите скриншот/чек оплаты (фото или файл). Я автоматически перешлю его менеджеру для проверки.")
    state = dp.current_state(user=callback_q.from_user.id)
    await state.set_state(UploadStates.waiting_for_screenshot.state)
    await state.update_data(order_id=order_id)
    await bot.answer_callback_query(callback_q.id)

# Receive screenshot, forward to manager and notify admins
@dp.message_handler(content_types=types.ContentTypes.ANY, state=UploadStates.waiting_for_screenshot)
async def receive_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    await state.finish()
    # forward to manager if MANAGER_CHAT_ID set (numeric)
    if MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit():
        try:
            manager_id = int(MANAGER_CHAT_ID)
            await bot.forward_message(manager_id, message.chat.id, message.message_id)
            # send inline confirm/decline for manager
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Подтвердить и выслать подарок", callback_data=f"admin_confirm:{order_id}"))
            kb.add(types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_decline:{order_id}"))
            order = get_order(order_id)
            if order:
                chat_id = order[1]
                await bot.send_message(manager_id, f"Заявка #{order_id} — проверка платежа. Покупатель id: {chat_id}", reply_markup=kb)
            await message.answer("Скрин отправлен менеджеру. Ожидайте подтверждения.")
            # notify admins too
            notify_admins_text(f"Пользователь {message.from_user.id} отправил скрин для заказа #{order_id}.")
            return
        except Exception as e:
            log.exception("Forward to manager failed: %s", e)
    # else instruct sending to username
    if MANAGER_USERNAME:
        await message.answer(f"Пожалуйста, отправьте этот скрин в Telegram менеджеру: @{MANAGER_USERNAME}. После подтверждения менеджер пришлёт подарок.")
    else:
        await message.answer("Менеджер не настроен. Пожалуйста, свяжитесь с поддержкой вручную.")
    notify_admins_text(f"Пользователь {message.from_user.id} отправил скрин для заказа #{order_id} (менеджер не настроен авто-forward).")

# Manager inline confirm/decline
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("admin_confirm:"))
async def cb_admin_confirm(callback_q: types.CallbackQuery):
    order_id = int(callback_q.data.split(":",1)[1])
    user = callback_q.from_user
    allowed = False
    if MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and user.id == int(MANAGER_CHAT_ID):
        allowed = True
    if user.id in ADMINS:
        allowed = True
    if not allowed:
        await bot.answer_callback_query(callback_q.id, "Нет прав для этого действия.")
        return
    set_order_status(order_id, "confirmed")
    await deliver_order(order_id)
    await bot.answer_callback_query(callback_q.id, f"Заказ #{order_id} подтверждён и отправлен.")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("admin_decline:"))
async def cb_admin_decline(callback_q: types.CallbackQuery):
    order_id = int(callback_q.data.split(":",1)[1])
    user = callback_q.from_user
    allowed = False
    if MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and user.id == int(MANAGER_CHAT_ID):
        allowed = True
    if user.id in ADMINS:
        allowed = True
    if not allowed:
        await bot.answer_callback_query(callback_q.id, "Нет прав для этого действия.")
        return
    set_order_status(order_id, "declined")
    await bot.answer_callback_query(callback_q.id, f"Заказ #{order_id} отклонён.")

# Delivery: send gift to user (simple text or photo)
async def deliver_order(order_id: int):
    o = get_order(order_id)
    if not o:
        return
    oid, chat_id, gift_id, status, amount, local_invoice, payment_id = o
    gift = get_gift_by_id(gift_id)
    if not gift:
        await bot.send_message(chat_id, "Ошибка: подарок не найден.")
        set_order_status(order_id, "error")
        return
    _, name, price, descr, image_file_id = gift
    text = f"🎁 Ваш подарок *{name}* отправлен!\n\n{descr}\n\nСпасибо за покупку!"
    if image_file_id:
        try:
            await bot.send_photo(chat_id, image_file_id, caption=text, parse_mode="Markdown")
        except:
            await bot.send_message(chat_id, text, parse_mode="Markdown")
    else:
        await bot.send_message(chat_id, text, parse_mode="Markdown")
    set_order_status(order_id, "delivered")
    # notify admins that delivery done
    notify_admins_text(f"Заказ #{order_id} доставлен пользователю {chat_id}.")

# Sell flow: user sees manager link and instructions
@dp.message_handler(lambda m: m.text == "💰 Продать свой подарок" or m.text == "/sell")
async def cmd_sell(message: types.Message):
    save_user(message)
    text = (
        f"Чтобы продать свой подарок — отправьте его на аккаунт менеджера @{MANAGER_USERNAME}.\n\n"
        "Процесс:\n"
        "1) Отправьте подарок (файл/ссылка/описание) менеджеру в Telegram.\n"
        "2) Менеджер проверит подарок и выставит его на продажу в боте.\n"
        "3) После продажи менеджер свяжется с вами и организует выплату.\n\n"
        f"Связаться с менеджером: https://t.me/{MANAGER_USERNAME}"
    )
    kb = types.InlineKeyboardMarkup().add(types.InlineKeyboardButton("Написать менеджеру", url=f"https://t.me/{MANAGER_USERNAME}"))
    await message.answer(text, reply_markup=kb)

# My orders
@dp.message_handler(lambda m: m.text == "💼 Мои заказы" or m.text == "/orders")
async def cmd_my_orders(message: types.Message):
    save_user(message)
    rows = db_exec("SELECT id, gift_id, amount, status, created_at FROM orders WHERE chat_id = ? ORDER BY id DESC", (message.from_user.id,), fetch=True)
    if not rows:
        await message.answer("У вас пока нет заказов.")
        return
    out = []
    for r in rows:
        oid, gid, amt, status, created_at = r
        g = get_gift_by_id(gid)
        gname = g[1] if g else "—"
        out.append(f"#{oid} {gname} — {amt}₽ — {status}")
    await message.answer("\n".join(out))

# ========== Admin flows: addgift (FSM) and order management ==========
@dp.message_handler(commands=["addgift"])
async def cmd_addgift(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    await message.answer("Добавление подарка. Пришлите фотографию подарка или напишите /skip, чтобы добавить без фото.")
    state = dp.current_state(user=message.from_user.id)
    await state.set_state(AddGiftStates.waiting_for_photo.state)

@dp.message_handler(lambda m: m.text == "/skip", state=AddGiftStates.waiting_for_photo)
async def addgift_skip_photo(message: types.Message, state: FSMContext):
    await state.update_data(image_file_id=None)
    await message.answer("Отправьте название подарка:")
    await state.set_state(AddGiftStates.waiting_for_name.state)

@dp.message_handler(content_types=types.ContentType.PHOTO, state=AddGiftStates.waiting_for_photo)
async def addgift_photo(message: types.Message, state: FSMContext):
    file_id = message.photo[-1].file_id
    await state.update_data(image_file_id=file_id)
    await message.answer("Фото сохранено. Теперь пришлите название подарка:")
    await state.set_state(AddGiftStates.waiting_for_name.state)

@dp.message_handler(state=AddGiftStates.waiting_for_name, content_types=types.ContentTypes.TEXT)
async def addgift_name(message: types.Message, state: FSMContext):
    await state.update_data(name=message.text.strip())
    await message.answer("Укажите цену в рублях (числом):")
    await state.set_state(AddGiftStates.waiting_for_price.state)

@dp.message_handler(state=AddGiftStates.waiting_for_price, content_types=types.ContentTypes.TEXT)
async def addgift_price(message: types.Message, state: FSMContext):
    txt = message.text.strip()
    if not txt.isdigit():
        await message.answer("Цена должна быть числом. Попробуйте снова:")
        return
    await state.update_data(price=int(txt))
    await message.answer("Напишите описание подарка:")
    await state.set_state(AddGiftStates.waiting_for_description.state)

@dp.message_handler(state=AddGiftStates.waiting_for_description, content_types=types.ContentTypes.TEXT)
async def addgift_description(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data.get("name")
    price = data.get("price")
    image_file_id = data.get("image_file_id")
    descr = message.text.strip()
    now = datetime.utcnow().isoformat()
    db_exec("INSERT INTO gifts (name, price, description, image_file_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, price, descr, image_file_id, now))
    await message.answer(f"Подарок '{name}' добавлен в каталог.")
    await state.finish()

# listorders / confirm / decline / broadcast
@dp.message_handler(commands=["listorders"])
async def cmd_listorders(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    rows = db_exec("SELECT id, chat_id, gift_id, amount, status FROM orders ORDER BY id DESC LIMIT 50", fetch=True)
    if not rows:
        await message.reply("Нет заказов.")
        return
    lines = []
    for r in rows:
        oid, chat_id, gift_id, amount, status = r
        g = get_gift_by_id(gift_id)
        gname = g[1] if g else "—"
        lines.append(f"#{oid} {gname} {amount}₽ — {status} — user:{chat_id}")
    await message.reply("\n".join(lines))

@dp.message_handler(commands=["confirm"])
async def cmd_confirm(message: types.Message):
    if message.from_user.id not in ADMINS and not (MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and message.from_user.id == int(MANAGER_CHAT_ID)):
        return
    args = message.get_args()
    if not args or not args.isdigit():
        await message.reply("Использование: /confirm <order_id>")
        return
    oid = int(args)
    set_order_status(oid, "confirmed")
    await deliver_order(oid)
    await message.reply(f"Заказ {oid} подтверждён и доставлен.")

@dp.message_handler(commands=["decline"])
async def cmd_decline(message: types.Message):
    if message.from_user.id not in ADMINS and not (MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and message.from_user.id == int(MANAGER_CHAT_ID)):
        return
    args = message.get_args()
    if not args or not args.isdigit():
        await message.reply("Использование: /decline <order_id>")
        return
    oid = int(args)
    set_order_status(oid, "declined")
    await message.reply(f"Заказ {oid} отклонён.")

@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    text = message.get_args()
    if not text:
        await message.reply("Использование: /broadcast Текст")
        return
    users = db_exec("SELECT chat_id FROM users", fetch=True)
    sent = 0
    for u in users:
        try:
            await bot.send_message(u[0], text)
            sent += 1
            await asyncio.sleep(0.03)
        except Exception as e:
            log.warning("Broadcast failed to %s: %s", u[0], e)
    await message.reply(f"Отправлено {sent} сообщений.")

# share / promo simple
@dp.message_handler(lambda m: m.text == "⭐ Поделиться" or m.text == "/share")
async def cmd_share(message: types.Message):
    rows = db_exec("SELECT id, name, price FROM gifts LIMIT 3", fetch=True)
    text = "Я купил подарок в GiftsFelix! Посмотри: "
    for r in rows:
        text += f"\n{r[1]} — {r[2]}₽"
    bot_username = (await bot.get_me()).username
    share_text = text + f"\n\nКупить: https://t.me/{bot_username}"
    await message.answer("Поделиться можно этим текстом (перешли друзьям):")
    await message.answer(share_text)

# ========== payment watcher (detect paid via YooKassa or simulate in TEST_MODE) ==========
async def payment_watcher():
    log.info("Payment watcher started. TEST_MODE=%s", TEST_MODE)
    while True:
        pending = get_pending_orders()
        for row in pending:
            order_id, payment_id, local_invoice = row
            # TEST mode simulate paying after 15s
            if TEST_MODE:
                try:
                    ts = int(local_invoice.split("_")[1])
                except:
                    ts = int(time.time())
                if time.time() - ts > 15:
                    # set to paid_pending_confirmation
                    set_order_status(order_id, "paid_pending_confirmation")
                    o = get_order(order_id)
                    if o:
                        chat_id = o[1]
                        await bot.send_message(chat_id, f"Оплата получена (тест). Чтобы получить подарок — отправьте чек менеджеру @{MANAGER_USERNAME} и нажмите «Я оплатил».")
                        notify_admins_text(f"Оплата (тест) для заказа #{order_id}. Покупатель: {chat_id}")
            else:
                # real flow: check YooKassa
                if not payment_id:
                    continue
                info = get_yookassa_payment(payment_id)
                if not info:
                    continue
                paid_flag = info.get("paid", False)
                status = str(info.get("status", "")).lower()
                if paid_flag or status in ("succeeded", "paid", "waiting_for_capture"):
                    set_order_status(order_id, "paid_pending_confirmation")
                    o = get_order(order_id)
                    if o:
                        chat_id = o[1]
                        await bot.send_message(chat_id, f"Оплата подтверждена. Чтобы получить подарок — отправьте чек менеджеру @{MANAGER_USERNAME} и нажмите «Я оплатил».")
                        notify_admins_text(f"Оплата подтверждена для заказа #{order_id}. Покупатель: {chat_id}")
        await asyncio.sleep(6)

# ========== startup ==========
async def on_startup(_):
    init_db()
    ensure_sample_gifts()
    asyncio.create_task(payment_watcher())
    log.info("Bot started")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)