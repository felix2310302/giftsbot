import logging
import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils import executor
from collections import defaultdict
from aiogram.contrib.fsm_storage.memory 
import MemoryStorage

# Получаем токен из переменной окружения (Railway Variables)
API_TOKEN = os.getenv("API_TOKEN")

if not API_TOKEN:
    raise ValueError("❌ Не найден API_TOKEN. Установи его в Railway Variables!")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=API_TOKEN)
dp = Dispatcher(bot, storage=MemoryStorage())

# ==========================
# ДАННЫЕ (временные заглушки)
# ==========================
GIFTS = {
    1: {"name": "NFT Котик", "price": 500},
    2: {"name": "NFT Машина", "price": 1200},
    3: {"name": "NFT Пейзаж", "price": 800},
}

user_ratings = defaultdict(lambda: 5.0)  # рейтинг продавцов
user_history = defaultdict(list)  # история покупок/продаж
sales_queue = {}  # ожидающие продажи

# ==========================
# КЛАВИАТУРЫ
# ==========================
main_kb = ReplyKeyboardMarkup(resize_keyboard=True)
main_kb.add(KeyboardButton("🛒 Купить подарок"), KeyboardButton("💰 Продать подарок"))
main_kb.add(KeyboardButton("📜 Команды"), KeyboardButton("⭐ Рейтинг"))

cancel_kb = ReplyKeyboardMarkup(resize_keyboard=True)
cancel_kb.add(KeyboardButton("❌ Отмена"))

# ==========================
# СТАРТ
# ==========================
@dp.message_handler(commands=["start"])
async def start_cmd(message: types.Message):
    await message.answer("Привет! 🎁 Я бот для покупки и продажи NFT-подарков.\nВыбери действие:", reply_markup=main_kb)

# ==========================
# СПИСОК КОМАНД
# ==========================
@dp.message_handler(lambda m: m.text == "📜 Команды")
async def cmd_list(message: types.Message):
    commands_text = (
        "📜 Доступные команды:\n"
        "/start — начать работу\n"
        "/buy — купить подарок\n"
        "/sell — продать подарок\n"
        "/rating — рейтинг продавцов\n"
        "/history — история сделок\n"
        "/help — помощь"
    )
    await message.answer(commands_text)

# ==========================
# КУПИТЬ ПОДАРОК
# ==========================
@dp.message_handler(lambda m: m.text in ["🛒 Купить подарок", "/buy"])
async def buy_gift(message: types.Message):
    text = "🎁 Доступные подарки:\n\n"
    for gid, gift in GIFTS.items():
        text += f"{gid}. {gift['name']} — {gift['price']}₽\n"
    text += "\nНапиши номер подарка, чтобы купить."
    await message.answer(text, reply_markup=cancel_kb)
    await dp.current_state(user=message.from_user.id).set_state("choosing_gift")

@dp.message_handler(state="choosing_gift")
async def choose_gift(message: types.Message):
    if message.text == "❌ Отмена":
        await message.answer("Отменено ✅", reply_markup=main_kb)
        await dp.current_state(user=message.from_user.id).reset_state()
        return

    try:
        gift_id = int(message.text)
        if gift_id not in GIFTS:
            raise ValueError
    except ValueError:
        await message.answer("❌ Неверный номер подарка. Попробуй ещё раз.")
        return

    gift = GIFTS[gift_id]
    await message.answer(f"Вы выбрали: {gift['name']} за {gift['price']}₽.\n"
                         f"💳 Переходим к оплате...\n\n"
                         f"(Здесь будет интеграция с ЮKassa/CloudPayments)", reply_markup=main_kb)

    # Заглушка вместо оплаты
    await asyncio.sleep(2)
    await message.answer(f"✅ Оплата успешна! Вот ваш подарок: {gift['name']} 🎉")

    user_history[message.from_user.id].append(f"Купил: {gift['name']} ({gift['price']}₽)")

    await dp.current_state(user=message.from_user.id).reset_state()

# ==========================
# ПРОДАТЬ ПОДАРОК
# ==========================
@dp.message_handler(lambda m: m.text in ["💰 Продать подарок", "/sell"])
async def sell_gift(message: types.Message):
    await message.answer("Отправьте подарок (файл/код) для продажи:", reply_markup=cancel_kb)
    await dp.current_state(user=message.from_user.id).set_state("sending_gift")

@dp.message_handler(state="sending_gift", content_types=types.ContentTypes.ANY)
async def receive_gift(message: types.Message):
    if message.text == "❌ Отмена":
        await message.answer("Отменено ✅", reply_markup=main_kb)
        await dp.current_state(user=message.from_user.id).reset_state()
        return

    sales_queue[message.from_user.id] = {"gift": message.text or "Подарок-файл", "status": "в продаже"}
    await message.answer("🎁 Подарок отправлен на проверку. Мы уведомим, когда он будет продан.", reply_markup=main_kb)

    # Имитация продажи
    asyncio.create_task(simulate_sale(message.from_user.id))

    await dp.current_state(user=message.from_user.id).reset_state()

async def simulate_sale(user_id):
    await asyncio.sleep(5)
    await bot.send_message(user_id, "✅ Ваш подарок продан! Отправьте реквизиты карты для перевода:")
    await dp.current_state(user=user_id).set_state("waiting_payment_details")

@dp.message_handler(state="waiting_payment_details")
async def receive_payment_details(message: types.Message):
    details = message.text
    await message.answer(f"💳 Реквизиты получены: {details}\nВыплачиваем деньги...", reply_markup=main_kb)
    user_history[message.from_user.id].append(f"Продал подарок, получил оплату на: {details}")
    await dp.current_state(user=message.from_user.id).reset_state()

# ==========================
# РЕЙТИНГ
# ==========================
@dp.message_handler(lambda m: m.text in ["⭐ Рейтинг", "/rating"])
async def rating_cmd(message: types.Message):
    text = "⭐ Рейтинг продавцов:\n"
    for uid, rating in list(user_ratings.items())[:10]:
        text += f"Пользователь {uid}: {rating}/5\n"
    await message.answer(text)

# ==========================
# ИСТОРИЯ СДЕЛОК
# ==========================
@dp.message_handler(commands=["history"])
async def history_cmd(message: types.Message):
    history = user_history[message.from_user.id]
    if not history:
        await message.answer("📭 У вас пока нет сделок.")
        return
    text = "📜 Ваша история сделок:\n" + "\n".join(history)
    await message.answer(text)

# ==========================
# HELP
# ==========================
@dp.message_handler(commands=["help"])
async def help_cmd(message: types.Message):
    await message.answer("ℹ️ Этот бот помогает покупать и продавать NFT-подарки.\n"
                         "Выберите действие в меню или используйте команды:\n"
                         "/buy, /sell, /rating, /history")

# ==========================
# ЗАПУСК
# ==========================
if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)
