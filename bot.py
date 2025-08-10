"""
GiftsBot (MVP, autonomous, minimal work)
- polling aiogram bot
- sqlite storage (users, gifts, orders)
- simulated payments (TEST_MODE=True) OR CloudPayments check if CLOUDPAYMENTS_API_KEY set
- background watcher that confirms payments and delivers gifts
- admin commands to manage gifts and broadcast
"""

import os
import sqlite3
import asyncio
import logging
import time
import requests
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ========== Конфигурация через env ==========
API_TOKEN = os.getenv("API_TOKEN")  # обязателен
ADMINS = os.getenv("ADMINS", "")  # прописать через запятую свои chat_id (например: 12345678,87654321)
ADMINS = [int(x) for x in ADMINS.split(",") if x.strip().isdigit()]

TEST_MODE = os.getenv("TEST_MODE", "true").lower() in ("1", "true", "yes")
# CloudPayments (опционально) - если хочешь подключить реальную оплату, выставь эти переменные
CLOUDPAYMENTS_API_KEY = os.getenv("CLOUDPAYMENTS_API_KEY")  # api-key (APP Key) из CloudPayments
# пример: CLOUDPAYMENTS_API_KEY = "fcb6bd81970001eefda1cefd..."

DB_PATH = os.getenv("DB_PATH", "giftsbot.db")

if not API_TOKEN:
    raise RuntimeError("API_TOKEN не задан в переменных окружения")

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot)

# ========== SQLite helpers ==========
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # пользователи
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        first_name TEXT,
        last_name TEXT,
        username TEXT,
        created_at TIMESTAMP
    )""")
    # подарки
    cur.execute("""
    CREATE TABLE IF NOT EXISTS gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price INTEGER,
        description TEXT,
        created_at TIMESTAMP
    )""")
    # заказы
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER,
        gift_id INTEGER,
        status TEXT,
        amount INTEGER,
        invoice_id TEXT,
        created_at TIMESTAMP,
        updated_at TIMESTAMP
    )""")
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=False, one=False):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(query, params)
    if fetch:
        rows = cur.fetchall()
        conn.commit()
        conn.close()
        return rows
    if one:
        row = cur.fetchone()
        conn.commit()
        conn.close()
        return row
    conn.commit()
    conn.close()
    return None

# ========== Инициализация: добавим пару тестовых подарков, если пусто ==========
def ensure_sample_gifts():
    rows = db_execute("SELECT id FROM gifts LIMIT 1", fetch=True)
    if not rows:
        db_execute("INSERT INTO gifts (name, price, description, created_at) VALUES (?, ?, ?, ?)",
                   ("NFT Котик", 500, "Милый NFT котик", datetime.utcnow()))
        db_execute("INSERT INTO gifts (name, price, description, created_at) VALUES (?, ?, ?, ?)",
                   ("NFT Машина", 1200, "Коллекционная машина", datetime.utcnow()))
        log.info("Добавлены примерные подарки")

# ========== Утилиты ==========
def save_user(message: types.Message):
    db_execute("""
    INSERT OR REPLACE INTO users (chat_id, first_name, last_name, username, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (message.from_user.id, message.from_user.first_name, message.from_user.last_name or "",
          message.from_user.username or "", datetime.utcnow()))

def is_admin(user_id: int):
    return user_id in ADMINS

def create_order(chat_id: int, gift_id: int, amount: int):
    created = datetime.utcnow()
    invoice_id = f"order_{int(time.time())}_{chat_id}"
    db_execute("""
    INSERT INTO orders (chat_id, gift_id, status, amount, invoice_id, created_at, updated_at)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (chat_id, gift_id, "pending", amount, invoice_id, created, created))
    row = db_execute("SELECT id FROM orders WHERE invoice_id = ?", (invoice_id,), fetch=True)
    return row[0][0], invoice_id

def set_order_status(order_id: int, status: str):
    db_execute("UPDATE orders SET status = ?, updated_at = ? WHERE id = ?",
               (status, datetime.utcnow(), order_id))

def get_pending_orders(older_than_seconds=5):
    cutoff = datetime.utcnow() - timedelta(seconds=older_than_seconds)
    rows = db_execute("SELECT id, chat_id, gift_id, amount, invoice_id, created_at FROM orders WHERE status = 'pending'", fetch=True)
    # convert created_at string to datetime (sqlite stores as str) - but for simplicity we will just return all pending
    return rows

def get_gift(gift_id):
    rows = db_execute("SELECT id, name, price, description FROM gifts WHERE id = ?", (gift_id,), fetch=True)
    return rows[0] if rows else None

# ========== CloudPayments check (optional) ==========
def check_cloudpayments_invoice(invoice_id: str):
    """
    Пример запроса к CloudPayments v2 payments/find:
    POST https://api.cloudpayments.ru/v2/payments/find
    body: { "InvoiceId": "<invoice>" }
    header: api-key: <APP_KEY>
    См. документацию CloudPayments. (в этом коде мы считаем, что статус 'Completed' означает оплату).
    """
    if not CLOUDPAYMENTS_API_KEY:
        return None
    url = "https://api.cloudpayments.ru/v2/payments/find"
    try:
        resp = requests.post(url, json={"InvoiceId": invoice_id}, headers={"api-key": CLOUDPAYMENTS_API_KEY}, timeout=10)
        if resp.status_code != 200:
            log.warning("CloudPayments find returned %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        # В зависимости от ответа провайдера - нужно адаптировать:
        model = data.get("Model") or {}
        status = model.get("Status") or model.get("status") or None
        return {"raw": data, "status": status}
    except Exception as e:
        log.exception("CloudPayments check failed: %s", e)
        return None

# ========== Функция доставки подарка ==========
async def deliver_order(order_id: int):
    row = db_execute("SELECT chat_id, gift_id, amount, invoice_id FROM orders WHERE id = ?", (order_id,), fetch=True)
    if not row:
        return
    chat_id, gift_id, amount, invoice_id = row[0]
    gift = get_gift(gift_id)
    if not gift:
        await bot.send_message(chat_id, "Ошибка: подарок не найден.")
        set_order_status(order_id, "error")
        return
    name = gift[1]
    # Простая логика доставки: отправляем текстовое сообщение с "подарком"
    await bot.send_message(chat_id, f"🎉 Оплата подтверждена! Вот ваш подарок: *{name}*.\nСпасибо за покупку!",
                           parse_mode="Markdown")
    set_order_status(order_id, "delivered")
    log.info("Order %s delivered to %s", order_id, chat_id)

# ========== Background watcher: проверяет pending orders и подтверждает оплату ==========
async def order_watcher():
    log.info("Watcher started. TEST_MODE=%s", TEST_MODE)
    while True:
        pending = get_pending_orders()
        for ord_row in pending:
            order_id, chat_id, gift_id, amount, invoice_id, created_at = ord_row
            # Если TEST_MODE — симулируем оплату через 8 секунд
            if TEST_MODE:
                # берем созданное время, если прошло >8 сек — считаем оплаченным
                # (в sqlite created_at может быть пустым string; для простоты — доставляем если older 8s by invoice_id timestamp)
                # здесь простой алгоритм: доставлять все старее 8 сек
                # (в реальной интеграции — проверять статус у провайдера)
                await asyncio.sleep(0.1)  # чтобы не блокировать loop
                # compute naive: deliver after 8 seconds since invoice_id timestamp embedded
                try:
                    ts = int(invoice_id.split("_")[1])
                except Exception:
                    ts = int(time.time())
                if time.time() - ts > 8:
                    log.info("Simulate payment complete for order %s", order_id)
                    await deliver_order(order_id)
            else:
                # check CloudPayments status
                info = check_cloudpayments_invoice(invoice_id)
                if info and info.get("status"):
                    status = str(info["status"]).lower()
                    log.info("CloudPayments status for %s = %s", invoice_id, status)
                    # adjust this logic per CloudPayments real statuses (Completed / Authorized / Declined)
                    if status in ("completed", "success", "completedwithfraud"):  # example
                        await deliver_order(order_id)
                    elif status in ("declined", "failed"):
                        set_order_status(order_id, "failed")
                        await bot.send_message(chat_id, "Оплата не прошла. Попробуйте ещё раз или свяжитесь с поддержкой.")
        await asyncio.sleep(4)

# ========== Telegram handlers ==========
@dp.message_handler(commands=["start"])
async def cmd_start(message: types.Message):
    save_user(message)
    keyboard = types.ReplyKeyboardMarkup(resize_keyboard=True)
    keyboard.add("🛒 Купить подарок", "💼 Мои заказы")
    if is_admin(message.from_user.id):
        keyboard.add("🛠️ Админ")
    await message.answer("Привет! Я автоматический бот магазина подарков. Выбери действие:", reply_markup=keyboard)

@dp.message_handler(lambda m: m.text == "🛒 Купить подарок" or m.text == "/buy")
async def cmd_buy(message: types.Message):
    rows = db_execute("SELECT id, name, price FROM gifts", fetch=True)
    if not rows:
        await message.answer("Пока нет доступных подарков.")
        return
    kb = types.InlineKeyboardMarkup()
    for r in rows:
        gid, name, price = r
        kb.add(types.InlineKeyboardButton(text=f"{name} — {price}₽", callback_data=f"buy:{gid}"))
    await message.answer("Выберите подарок:", reply_markup=kb)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy:"))
async def on_buy(callback: types.CallbackQuery):
    chat_id = callback.from_user.id
    gid = int(callback.data.split(":")[1])
    gift = get_gift(gid)
    if not gift:
        await bot.answer_callback_query(callback.id, "Подарок не найден.")
        return
    price = gift[2]
    order_id, invoice_id = create_order(chat_id, gid, price)
    # Ссылка на оплату — в TEST_MODE это заглушка; в проде нужно создать ссылку через провайдера (CloudPayments/ЮKassa) и положить сюда
    if TEST_MODE:
        pay_link = f"https://example.com/pay?invoice={invoice_id}"  # просто заглушка
        await bot.send_message(chat_id,
            f"Создан заказ #{order_id} на сумму {price}₽.\n\n"
            f"Оплата (демо): перейдите по ссылке и подтвердите оплату (симуляция):\n{pay_link}\n\n"
            "Оплата будет подтверждена автоматически через несколько секунд.")
    else:
        # Здесь: вызов функции, которая создаёт платеж/сессию у CloudPayments и возвращает ссылку.
        # Пример: create_cloudpayments_payment_link(invoice_id, amount)
        await bot.send_message(chat_id, "Создан заказ. Перенаправляем на оплату (реализация CloudPayments — в настройках).")
    await bot.answer_callback_query(callback.id, f"Заказ {order_id} создан. Ссылка отправлена в чат.")

@dp.message_handler(lambda m: m.text == "💼 Мои заказы" or m.text == "/orders")
async def my_orders(message: types.Message):
    rows = db_execute("SELECT id, gift_id, amount, status, created_at FROM orders WHERE chat_id = ? ORDER BY id DESC", (message.from_user.id,), fetch=True)
    if not rows:
        await message.answer("У вас пока нет заказов.")
        return
    out = []
    for r in rows:
        oid, gift_id, amount, status, created_at = r
        gift = get_gift(gift_id)
        name = gift[1] if gift else "—"
        out.append(f"#{oid} {name} — {amount}₽ — {status}")
    await message.answer("\n".join(out))

# ========== Admin handlers ==========
@dp.message_handler(commands=["addgift"])
async def cmd_addgift(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    # формат: /addgift Название|цена|описание
    parts = message.get_args()
    if not parts:
        await message.reply("Использование: /addgift Название|цена|описание")
        return
    try:
        name, price, desc = [p.strip() for p in parts.split("|", 2)]
        price = int(price)
    except Exception:
        await message.reply("Неверный формат. Пример: /addgift NFT Котик|500|Милый котик")
        return
    db_execute("INSERT INTO gifts (name, price, description, created_at) VALUES (?, ?, ?, ?)", (name, price, desc, datetime.utcnow()))
    await message.reply("Подарок добавлен.")

@dp.message_handler(commands=["listorders"])
async def cmd_listorders(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    rows = db_execute("SELECT id, chat_id, gift_id, amount, status, invoice_id FROM orders ORDER BY id DESC LIMIT 50", fetch=True)
    if not rows:
        await message.reply("Нет заказов.")
        return
    out = []
    for r in rows:
        oid, chat_id, gift_id, amount, status, invoice_id = r
        gift = get_gift(gift_id)
        name = gift[1] if gift else "—"
        out.append(f"#{oid} {name} ({amount}₽) — {status} — user:{chat_id} — invoice:{invoice_id}")
    await message.reply("\n".join(out))

@dp.message_handler(commands=["broadcast"])
async def cmd_broadcast(message: types.Message):
    if not is_admin(message.from_user.id):
        return
    text = message.get_args()
    if not text:
        await message.reply("Напиши текст после команды: /broadcast Текст сообщения")
        return
    rows = db_execute("SELECT chat_id FROM users", fetch=True)
    sent = 0
    for r in rows:
        try:
            await bot.send_message(r[0], f"📢 {text}")
            sent += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning("Broadcast failed to %s: %s", r[0], e)
    await message.reply(f"Отправлено {sent} сообщений.")

# ========== Startup: db и watcher ==========
async def on_startup(_):
    init_db()
    ensure_sample_gifts()
    asyncio.create_task(order_watcher())
    log.info("Bot started")

# ========== Run ==========
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True, on_startup=on_startup)