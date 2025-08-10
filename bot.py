"""
GiftsFelix Telegram bot — YooKassa flow + manual manager confirmation
- aiogram polling
- sqlite storage (users, gifts, orders)
- creates YooKassa payment (redirect confirmation)
- user uploads screenshot -> forwarded to manager (if MANAGER_CHAT_ID set)
- manager confirms via inline button or /confirm <order_id>
- minimal env setup in Railway
"""

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

# ======= ENV / config =======
API_TOKEN = os.getenv("API_TOKEN")  # Telegram bot token (обязательно)
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")  # shopId из кабинета ЮKassa
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")  # secret key из кабинета ЮKassa
RETURN_URL = os.getenv("RETURN_URL", "")  # куда вернётся пользователь после оплаты (рекомендуется: https://t.me/YourBotUsername)
ADMINS = os.getenv("ADMINS", "")  # список админов через запятую: "1234567,7654321"
MANAGER_CHAT_ID = os.getenv("MANAGER_CHAT_ID")  # numeric chat_id менеджера (если есть) — бот будет пересылать туда скрины
MANAGER_USERNAME = os.getenv("MANAGER_USERNAME", "")  # @username менеджера (на случай, если MANAGER_CHAT_ID не задан)
TEST_MODE = os.getenv("TEST_MODE", "true").lower() in ("1", "true", "yes")
DB_PATH = os.getenv("DB_PATH", "giftsfelix.db")

ADMINS = [int(x) for x in ADMINS.split(",") if x.strip().isdigit()]
if not API_TOKEN:
    raise RuntimeError("API_TOKEN не задан в переменных окружения")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# ======= DB helpers =======
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        created_at TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price INTEGER,
        description TEXT,
        created_at TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        gift_id INTEGER,
        status TEXT,
        amount INTEGER,
        local_invoice TEXT,
        payment_id TEXT,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    );
    """)
    conn.commit()
    conn.close()

def db_query(sql, params=(), fetch=False):
    conn = sqlite3.connect(DB_PATH)
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

def save_user(message: types.Message):
    db_query("""
    INSERT OR REPLACE INTO users (chat_id, first_name, last_name, username, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (message.from_user.id, message.from_user.first_name or "", message.from_user.last_name or "",
          message.from_user.username or "", datetime.utcnow()))

# ======= sample gifts (если пусто) =======
def ensure_sample_gifts():
    rows = db_query("SELECT id FROM gifts LIMIT 1", fetch=True)
    if not rows:
        db_query("INSERT INTO gifts (name, price, description, created_at) VALUES (?, ?, ?, ?)",
                 ("NFT Котик", 500, "Милый NFT котик — цифровой подарок", datetime.utcnow()))
        db_query("INSERT INTO gifts (name, price, description, created_at) VALUES (?, ?, ?, ?)",
                 ("NFT Машина", 1200, "Коллекционная машина", datetime.utcnow()))
        log.info("Добавлены примерные подарки")

# ======= YooKassa integration (simple requests) =======
YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"

def create_yookassa_payment(local_invoice_id: str, amount_rub: int, description: str):
    """
    Создаём платеж в YooKassa и возвращаем (payment_id, confirmation_url) или (None, None) в случае ошибки.
    Требует YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY в env.
    """
    if not (YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY):
        return None, None, "YOOKASSA not configured"

    url = f"{YOOKASSA_API_BASE}/payments"
    headers = {
        "Idempotence-Key": local_invoice_id,
        "Content-Type": "application/json"
    }
    body = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "payment_method_data": {"type": "bank_card"},
        "confirmation": {"type": "redirect", "return_url": RETURN_URL or "https://t.me/"},  # лучше указать RETURN_URL
        "description": description
    }
    try:
        resp = requests.post(url, json=body, headers=headers, auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY), timeout=10)
        if resp.status_code not in (200, 201):
            log.warning("YooKassa create payment failed: %s %s", resp.status_code, resp.text[:500])
            return None, None, f"YooKassa error {resp.status_code}"
        data = resp.json()
        payment_id = data.get("id")
        confirmation = data.get("confirmation", {})
        confirmation_url = confirmation.get("confirmation_url")
        return payment_id, confirmation_url, None
    except Exception as e:
        log.exception("create_yookassa_payment failed")
        return None, None, str(e)

def get_yookassa_payment(payment_id: str):
    """GET /payments/{payment_id}"""
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

# ======= Orders helpers =======
def create_order(chat_id: int, gift_id: int, amount: int):
    local_invoice = f"inv_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    now = datetime.utcnow()
    db_query("INSERT INTO orders (chat_id, gift_id, status, amount, local_invoice, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
             (chat_id, gift_id, "pending", amount, local_invoice, now, now))
    row = db_query("SELECT id FROM orders WHERE local_invoice = ?", (local_invoice,), fetch=True)
    return row[0][0], local_invoice

def set_order_payment(order_id: int, payment_id: str):
    db_query("UPDATE orders SET payment_id = ?, status = ?, updated_at = ? WHERE id = ?",
             (payment_id, "payment_created", datetime.utcnow(), order_id))

def set_order_status(order_id: int, status: str):
    db_query("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
             (status, datetime.utcnow(), order_id))

def get_order(order_id: int):
    rows = db_query("SELECT id, chat_id, gift_id, status, amount, local_invoice, payment_id FROM orders WHERE id = ?", (order_id,), fetch=True)
    return rows[0] if rows else None

def get_pending_orders():
    return db_query("SELECT id, payment_id, local_invoice FROM orders WHERE status IN ('payment_created','pending')", fetch=True)

# ======= Delivery =======
async def deliver_order(order_id: int):
    o = get_order(order_id)
    if not o:
        return
    _, chat_id, gift_id, status, amount, local_invoice, payment_id = o
    gift = db_query("SELECT name, description FROM gifts WHERE id = ?", (gift_id,), fetch=True)
    name = gift[0][0] if gift else "Подарок"
    description = gift[0][1] if gift else ""
    await bot.send_message(chat_id, f"🎁 Ваш подарок *{name}* отправлен!\n\n{description}\n\nСпасибо за покупку!",
                           parse_mode="Markdown")
    set_order_status(order_id, "delivered")
    log.info("Order %s delivered", order_id)

# ======= FSM for screenshot upload =======
class UploadStates(StatesGroup):
    waiting_for_screenshot = State()

# ======= Handlers =======
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    save_user(message)
    args = message.get_args()
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add("🛒 Купить подарок", "💼 Мои заказы")
    kb.add("📜 Помощь", "⭐ Поделиться")
    if message.from_user.id in ADMINS:
        kb.add("🛠️ Админ")
    await message.answer("Привет! Я GiftsFelix — магазин NFT-подарков. Выберите действие:", reply_markup=kb)
    if args:
        # optional: deep link handling t.me/YourBot?start=gift_{id}
        if args.startswith("gift_"):
            try:
                gid = int(args.split("_",1)[1])
                rows = db_query("SELECT id, name, price, description FROM gifts WHERE id = ?", (gid,), fetch=True)
                if rows:
                    r = rows[0]
                    await message.answer(f"Подарок: {r[1]} — {r[2]}₽\n{r[3]}")
            except Exception:
                pass

@dp.message_handler(lambda m: m.text == "📜 Помощь" or m.text == "/help")
async def cmd_help(message: types.Message):
    text = (
        "📜 *Команды и как работать*\n\n"
        "Покупатели:\n"
        "🛒 — открыть каталог и купить подарок\n"
        "💼 — мои заказы\n"
        "⭐ — поделиться ботом с другом\n\n"
        "Если оплатили — нажмите *'Я оплатил'* и пришлите скрин менеджеру.\n\n"
        "Менеджер/Админ:\n"
        "/addgift Название|цена|описание — добавить подарок\n"
        "/listorders — посмотреть последние заказы\n"
        "/confirm <order_id> — подтвердить и выслать подарок\n"
        "/decline <order_id> — отменить заказ\n"
        "/broadcast Текст — рассылка всем пользователям\n    "
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message_handler(lambda m: m.text == "🛒 Купить подарок" or m.text == "/buy")
async def cmd_buy(message: types.Message):
    save_user(message)
    rows = db_query("SELECT id, name, price FROM gifts", fetch=True)
    if not rows:
        await message.answer("Пока нет доступных подарков.")
        return
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        gid, name, price = r
        kb.add(types.InlineKeyboardButton(text=f"{name} — {price}₽", callback_data=f"buy:{gid}"))
    await message.answer("Выберите подарок:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy:"))
async def on_buy(callback_q: types.CallbackQuery):
    chat_id = callback_q.from_user.id
    gid = int(callback_q.data.split(":")[1])
    gift = db_query("SELECT id, name, price, description FROM gifts WHERE id = ?", (gid,), fetch=True)
    if not gift:
        await bot.answer_callback_query(callback_q.id, "Подарок не найден.")
        return
    gift = gift[0]
    price = gift[2]
    order_id, local_invoice = create_order(chat_id, gid, price)
    # create YooKassa payment
    payment_id, confirmation_url, err = create_yookassa_payment(local_invoice, price, f"Order #{order_id} - {gift[1]}")
    if err:
        # fallback to demo link if YooKassa not configured
        demo_link = f"https://example.com/pay?invoice={local_invoice}"
        set_order_payment(order_id, payment_id or "")
        await bot.send_message(chat_id,
            f"Создан заказ #{order_id} на сумму {price}₽.\n\nСсылка для оплаты (демо):\n{demo_link}\n\nПосле оплаты нажмите кнопку *Я оплатил* и пришлите скрин менеджеру.",
            reply_markup=types.InlineKeyboardMarkup().add(
                types.InlineKeyboardButton("Я оплатил — отправить скрин", callback_data=f"paid:{order_id}")
            ), parse_mode="Markdown"
        )
        await bot.answer_callback_query(callback_q.id, "Заказ создан (демо). Ссылка в чате.")
        return

    # save payment_id and status
    set_order_payment(order_id, payment_id)
    # send payment link and button to upload screeenshot after paying
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("Оплатить (ЮKassa)", url=confirmation_url))
    kb.add(types.InlineKeyboardButton("Я оплатил — отправить скрин менеджеру", callback_data=f"paid:{order_id}"))
    await bot.send_message(chat_id,
        f"Создан заказ #{order_id} на сумму {price}₽.\n\nПерейдите по кнопке для оплаты через ЮKassa. После успешной оплаты вернитесь и нажмите «Я оплатил» и пришлите скрин.",
        reply_markup=kb
    )
    await bot.answer_callback_query(callback_q.id, "Ссылка для оплаты отправлена в чат.")

# user presses "Я оплатил" — we ask to upload screenshot
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("paid:"))
async def on_paid_button(callback_q: types.CallbackQuery):
    order_id = int(callback_q.data.split(":")[1])
    await bot.send_message(callback_q.from_user.id, "Пожалуйста, пришлите скриншот оплаты (фото или файл). После получения я перешлю его менеджеру для проверки.")
    await UploadStates.waiting_for_screenshot.set()
    # store order_id in user state
    state = dp.current_state(user=callback_q.from_user.id)
    await state.update_data(order_id=order_id)
    await bot.answer_callback_query(callback_q.id)

# receive screenshot and forward to manager (if manager chat set) or instruct user to send manually
@dp.message_handler(content_types=types.ContentTypes.ANY, state=UploadStates.waiting_for_screenshot)
async def receive_screenshot(message: types.Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    await state.finish()
    # forward to manager if MANAGER_CHAT_ID set
    if MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).strip().isdigit():
        try:
            manager_id = int(MANAGER_CHAT_ID)
            # forward whole message
            await bot.forward_message(manager_id, message.chat.id, message.message_id)
            # send context to manager with inline confirm/decline buttons
            kb = types.InlineKeyboardMarkup()
            kb.add(types.InlineKeyboardButton("✅ Подтвердить и выслать подарок", callback_data=f"admin_confirm:{order_id}"))
            kb.add(types.InlineKeyboardButton("❌ Отклонить", callback_data=f"admin_decline:{order_id}"))
            order = get_order(order_id)
            if order:
                await bot.send_message(manager_id, f"Заявка #{order_id} — пользователь {order[1]} оплатил. Проверьте скрин и примите решение.", reply_markup=kb)
            await message.answer("Скриншот отправлен менеджеру. Ожидайте подтверждения.")
        except Exception as e:
            log.exception("forward to manager failed")
            await message.answer("Не удалось переслать менеджеру. Пожалуйста, напишите менеджеру вручную.")
    else:
        # manager not configured: instruct user to send to manager username
        if MANAGER_USERNAME:
            await message.answer(f"Пожалуйста, отправьте этот скрин менеджеру: @{MANAGER_USERNAME}. После подтверждения менеджер пришлёт подарок.")
        else:
            await message.answer("Менеджер не настроен в боте. Пожалуйста, напишите нам вручную и приложите скрин (контакт менеджера).")

# manager inline confirm/decline
@dp.callback_query_handler(lambda c: c.data and c.data.startswith("admin_confirm:"))
async def admin_confirm_cb(callback_q: types.CallbackQuery):
    order_id = int(callback_q.data.split(":")[1])
    user = callback_q.from_user
    # allow only manager or admins
    allowed = False
    if MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and user.id == int(MANAGER_CHAT_ID):
        allowed = True
    if user.id in ADMINS:
        allowed = True
    if not allowed:
        await bot.answer_callback_query(callback_q.id, "У вас нет прав для этого действия.")
        return
    set_order_status(order_id, "confirmed")
    await deliver_order(order_id)
    await bot.answer_callback_query(callback_q.id, f"Заказ #{order_id} подтверждён и отправлен.")

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("admin_decline:"))
async def admin_decline_cb(callback_q: types.CallbackQuery):
    order_id = int(callback_q.data.split(":")[1])
    user = callback_q.from_user
    if MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and user.id == int(MANAGER_CHAT_ID):
        allowed = True
    else:
        allowed = user.id in ADMINS
    if not allowed:
        await bot.answer_callback_query(callback_q.id, "У вас нет прав для этого действия.")
        return
    set_order_status(order_id, "declined")
    await bot.answer_callback_query(callback_q.id, f"Заказ #{order_id} отклонён.")

# admin commands: confirm/decline manually and addgift/listorders/broadcast
@dp.message_handler(commands=["confirm"])
async def cmd_confirm(message: types.Message):
    if message.from_user.id not in ADMINS and not (MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and message.from_user.id == int(MANAGER_CHAT_ID)):
        return
    args = message.get_args()
    if not args or not args.isdigit():
        await message.reply("Использование: /confirm <order_id>")
        return
    order_id = int(args)
    set_order_status(order_id, "confirmed")
    await deliver_order(order_id)
    await message.reply(f"Заказ {order_id} подтверждён и доставлен.")

@dp.message_handler(commands=["decline"])
async def cmd_decline(message: types.Message):
    if message.from_user.id not in ADMINS and not (MANAGER_CHAT_ID and str(MANAGER_CHAT_ID).isdigit() and message.from_user.id == int(MANAGER_CHAT_ID)):
        return
    args = message.get_args()
    if not args or not args.isdigit():
        await message.reply("Использование: /decline <order_id>")
        return
    order_id = int(args)
    set_order_status(order_id, "declined")
    await message.reply(f"Заказ {order_id} отклонён.")

@dp.message_handler(commands=["addgift"])
async def cmd_addgift(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    # format: /addgift Название|цена|описание
    args = message.get_args()
    if not args or "|" not in args:
        await message.reply("Формат: /addgift Название|цена|описание")
        return
    name, price, descr = [p.strip() for p in args.split("|", 2)]
    try:
        price_i = int(price)
    except:
        await message.reply("Цена должна быть числом.")
        return
    db_query("INSERT INTO gifts (name, price, description, created_at) VALUES (?, ?, ?, ?)",
             (name, price_i, descr, datetime.utcnow()))
    await message.reply("Подарок добавлен.")

@dp.message_handler(commands=["listorders"])
async def cmd_listorders(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    rows = db_query("SELECT id, chat_id, gift_id, amount, status FROM orders ORDER BY id DESC LIMIT 50", fetch=True)
    if not rows:
        await message.reply("Нет заказов.")
        return
    lines = []
    for r in rows:
        oid, chat_id, gift_id, amount, status = r
        g = db_query("SELECT name FROM gifts WHERE id = ?", (gift_id,), fetch=True)
        gname = g[0][0] if g else "—"
        lines.append(f"#{oid} {gname} {amount}₽ — {status} — user:{chat_id}")
    await message.reply("\n".join(lines))

@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    if message.from_user.id not in ADMINS:
        return
    text = message.get_args()
    if not text:
        await message.reply("Использование: /broadcast Текст")
        return
    users = db_query("SELECT chat_id FROM users", fetch=True)
    sent = 0
    for u in users:
        try:
            await bot.send_message(u[0], text)
            sent += 1
            await asyncio.sleep(0.03)
        except Exception as e:
            log.warning("Broadcast failed to %s: %s", u[0], e)
    await message.reply(f"Отправлено {sent} сообщений.")

# share and promo to attract users
@dp.message_handler(lambda m: m.text == "⭐ Поделиться" or m.text == "/share")
async def cmd_share(message: types.Message):
    rows = db_query("SELECT id, name, price FROM gifts LIMIT 3", fetch=True)
    text = "Я купил подарок в GiftsFelix! Посмотри: "
    for r in rows:
        text += f"\n{r[1]} — {r[2]}₽"
    bot_username = (await bot.get_me()).username
    share_text = text + f"\n\nКупить: https://t.me/{bot_username}"
    await message.answer("Поделиться можно этим текстом (перешли друзьям):")
    await message.answer(share_text)

@dp.message_handler(commands=["promo"])
async def cmd_promo(message: types.Message):
    # простой пример промо — можно усложнить с базой скидок
    await message.reply("🔥 Промо: при покупке любого подарка — промокод FRIEND дает скидку 10% (ограничено). Напиши менеджеру для активации промо.")

# user orders listing
@dp.message_handler(lambda m: m.text == "💼 Мои заказы" or m.text == "/orders")
async def cmd_my_orders(message: types.Message):
    rows = db_query("SELECT id, gift_id, amount, status FROM orders WHERE chat_id = ? ORDER BY id DESC", (message.from_user.id,), fetch=True)
    if not rows:
        await message.answer("У вас пока нет заказов.")
        return
    out = []
    for r in rows:
        oid, gid, amt, status = r
        g = db_query("SELECT name FROM gifts WHERE id = ?", (gid,), fetch=True)
        gname = g[0][0] if g else "—"
        out.append(f"#{oid} {gname} — {amt}₽ — {status}")
    await message.answer("\n".join(out))

# ======= Background watcher: проверяет статусы платежей в YooKassa и - при paid -> просит прислать менеджеру скрин =======
async def payment_watcher():
    log.info("Payment watcher started. TEST_MODE=%s", TEST_MODE)
    while True:
        pending = get_pending_orders()
        for row in pending:
            order_id, payment_id, local_invoice = row
            if TEST_MODE:
                # для теста: помечаем как paid через 12 секунд
                try:
                    ts = int(local_invoice.split("_")[1])
                except:
                    ts = int(time.time())
                if time.time() - ts > 12:
                    # mark as paid -> ask user to send screenshot to manager
                    set_order_status(order_id, "paid_pending_confirmation")
                    order = get_order(order_id)
                    if order:
                        chat_id = order[1]
                        await bot.send_message(chat_id, "Оплата обнаружена (тест). Пожалуйста, нажмите «Я оплатил» и пришлите скрин менеджеру.")
            else:
                # check YooKassa payment status
                if not payment_id:
                    continue
                info = get_yookassa_payment(payment_id)
                if not info:
                    continue
                paid_flag = info.get("paid", False)
                status = info.get("status", "").lower()
                if paid_flag or status in ("succeeded", "paid", "waiting_for_capture", "succeeded"):
                    # we don't auto-deliver (по твоему требованию) — просим отправить скрин менеджеру
                    set_order_status(order_id, "paid_pending_confirmation")
                    order = get_order(order_id)
                    if order:
                        chat_id = order[1]
                        await bot.send_message(chat_id, "Оплата на ЮKassa подтверждена. Пожалуйста, нажмите «Я оплатил» и пришлите скрин менеджеру.")
        await asyncio.sleep(6)

# ======= startup =======
async def on_startup(dp):
    init_db()
    ensure_sample_gifts()
    asyncio.create_task(payment_watcher())
    log.info("Bot started")

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)