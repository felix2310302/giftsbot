import os
from aiogram import Bot, Dispatcher, types
from aiogram.utils import executor
from aiogram.contrib.fsm_storage.memory import MemoryStorage

API_TOKEN = os.getenv("API_TOKEN")
if not API_TOKEN:
    raise ValueError("Не найден API_TOKEN")

bot = Bot(token=API_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot, storage=storage)

# Список подарков — пример
GIFTS = {
    "gift1": {"name": "NFT Подарок #1", "price": 500},
    "gift2": {"name": "NFT Подарок #2", "price": 1000},
    "gift3": {"name": "NFT Подарок #3", "price": 1500},
}

# Рейтинг пользователей (просто пример)
user_ratings = {}

@dp.message_handler(commands=["start", "help"])
async def send_welcome(message: types.Message):
    text = (
        "Привет! Это бот для покупки и продажи NFT подарков.\n\n"
        "Команды:\n"
        "/start - Запуск бота\n"
        "/buy - Купить подарок\n"
        "/sell - Продать подарок\n"
        "/rating - Посмотреть рейтинг\n"
    )
    await message.reply(text)

@dp.message_handler(commands=["buy"])
async def buy_handler(message: types.Message):
    keyboard = types.InlineKeyboardMarkup()
    for gift_id, gift in GIFTS.items():
        button = types.InlineKeyboardButton(
            text=f"{gift['name']} — {gift['price']}₽", callback_data=f"buy_{gift_id}"
        )
        keyboard.add(button)
    await message.answer("Выберите подарок для покупки:", reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data and c.data.startswith("buy_"))
async def process_buy(callback_query: types.CallbackQuery):
    gift_id = callback_query.data[4:]
    gift = GIFTS.get(gift_id)
    if not gift:
        await bot.answer_callback_query(callback_query.id, text="Подарок не найден.")
        return
    # Здесь должен быть вызов эквайринга — пока заглушка
    await bot.answer_callback_query(callback_query.id, text=f"Вы выбрали {gift['name']}. Для оплаты свяжитесь с продавцом.")
    await bot.send_message(callback_query.from_user.id, f"Спасибо за выбор! Для оплаты подарка {gift['name']} на сумму {gift['price']}₽ свяжитесь с продавцом.")

@dp.message_handler(commands=["sell"])
async def sell_handler(message: types.Message):
    await message.answer(
        "Чтобы продать подарок, отправьте его на наш сторонний аккаунт @nft_seller_bot.\n"
        "После продажи вы получите уведомление и сможете отправить реквизиты для оплаты."
    )

@dp.message_handler(commands=["rating"])
async def rating_handler(message: types.Message):
    if not user_ratings:
        await message.answer("Рейтинг пока пуст.")
        return
    rating_text = "Рейтинг пользователей:\n"
    sorted_rating = sorted(user_ratings.items(), key=lambda x: x[1], reverse=True)
    for user_id, score in sorted_rating:
        rating_text += f"Пользователь {user_id}: {score} очков\n"
    await message.answer(rating_text)

if __name__ == "__main__":
    executor.start_polling(dp, skip_updates=True)