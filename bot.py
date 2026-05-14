import os
import re
import logging
import aiohttp
import aiosqlite
import pandas as pd
import tempfile

from datetime import datetime, timedelta, time

from dotenv import load_dotenv

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.constants import ChatPermissions

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# =========================================================
# ЗАГРУЗКА ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_LINK = os.getenv("PAYMENT_LINK")
CHAT_LINK = os.getenv("CHAT_LINK")
DEBTORS_FILE_URL = os.getenv("DEBTORS_FILE_URL")

GATE_PHONE = os.getenv("GATE_PHONE")

WELCOME_TEXT = """
Добро пожаловать в сообщество СНТ «Победа» — место, где ценятся добрососедство, взаимопомощь, уважение и любовь к природе.

Желаем всем солнечных дней, богатых урожаев, приятного отдыха и гармонии!

С уважением,
Правление СНТ «Победа»
"""

# =========================================================
# ЛОГИ
# =========================================================

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler("logs/bot.log"),
        logging.StreamHandler()
    ]
)

logger = logging.getLogger(__name__)

# =========================================================
# СОСТОЯНИЯ
# =========================================================

ASK_PHONE, MAIN_MENU = range(2)

# =========================================================
# RATE LIMIT
# =========================================================

LAST_GATE_OPEN = {}

# =========================================================
# СПИСОК МАТОВ
# =========================================================

BAD_WORDS = [
    "блять",
    "блядь",
    "бля",
    "сука",
    "сучка",
    "хуй",
    "хуйня",
    "хуево",
    "хуевый",
    "нахуй",
    "похуй",
    "ебать",
    "ебаный",
    "ебучий",
    "уебок",
    "уебище",
    "пизда",
    "пиздец",
    "пиздюк",
    "залупа",
    "мудак",
    "долбоеб",
    "долбаеб",
    "гандон",
    "мразь",
    "шлюха",
    "тварь",
    "сраный",
    "охуел",
    "охуеть",
    "ебло",
    "чмо",
    "пидор",
    "пидорас",
    "говно",
    "дерьмо"
]

# =========================================================
# НОРМАЛИЗАЦИЯ ТЕЛЕФОНА
# =========================================================

def normalize_phone(phone: str) -> str:

    phone = (
        phone.replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )

    if phone.startswith("8"):
        phone = "+7" + phone[1:]

    elif phone.startswith("7"):
        phone = "+" + phone

    return phone

# =========================================================
# RATE LIMIT ВОРОТ
# =========================================================

def can_open_gate(user_id: int) -> bool:

    now = datetime.now()

    if user_id in LAST_GATE_OPEN:

        delta = now - LAST_GATE_OPEN[user_id]

        if delta < timedelta(seconds=30):
            return False

    LAST_GATE_OPEN[user_id] = now

    return True

# =========================================================
# ИНИЦИАЛИЗАЦИЯ БД
# =========================================================

async def init_db():

    async with aiosqlite.connect("database.db") as db:

        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE,
                name TEXT,
                telegram_id INTEGER,
                welcomed INTEGER DEFAULT 0
            )
        """)

        await db.commit()

# =========================================================
# ПОЛУЧЕНИЕ ПОЛЬЗОВАТЕЛЯ
# =========================================================

async def get_user_by_phone(phone: str):

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute(
            "SELECT id, phone, name, welcomed "
            "FROM users WHERE phone = ?",
            (phone,)
        )

        return await cursor.fetchone()

# =========================================================
# ОБНОВЛЕНИЕ TELEGRAM ID
# =========================================================

async def update_telegram_id(phone, telegram_id):

    async with aiosqlite.connect("database.db") as db:

        await db.execute(
            "UPDATE users SET telegram_id = ? "
            "WHERE phone = ?",
            (telegram_id, phone)
        )

        await db.commit()

# =========================================================
# ПРИВЕТСТВИЕ
# =========================================================

async def set_welcomed(phone):

    async with aiosqlite.connect("database.db") as db:

        await db.execute(
            "UPDATE users SET welcomed = 1 "
            "WHERE phone = ?",
            (phone,)
        )

        await db.commit()

# =========================================================
# ЗАГРУЗКА ДОЛЖНИКОВ
# =========================================================

async def load_debtors():

    try:

        async with aiohttp.ClientSession() as session:

            async with session.get(
                DEBTORS_FILE_URL
            ) as response:

                if response.status != 200:
                    return None

                content = await response.read()

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".xlsx"
        ) as temp_file:

            temp_file.write(content)

            temp_path = temp_file.name

        df = pd.read_excel(temp_path)

        os.remove(temp_path)

        df.columns = [
            str(col).strip().lower()
            for col in df.columns
        ]

        debtors = []

        for _, row in df.iterrows():

            try:

                plot = row["номер участка"]

                required_amount = float(
                    row["сумма взноса"]
                )

                paid_amount = float(
                    row["фактическая сумма"]
                )

                debt = required_amount - paid_amount

                if debt > 0:

                    debtors.append({
                        "plot": plot,
                        "debt": debt
                    })

            except:
                continue

        return debtors

    except Exception as e:

        logger.error(e)

        return None

# =========================================================
# УВЕДОМЛЕНИЯ О ДОЛГАХ
# =========================================================

async def weekly_debt_notification(context):

    debtors = await load_debtors()

    if not debtors:
        return

    message = "⚠️ Напоминание о задолженности\n\n"

    for debtor in debtors:

        message += (
            f"🏠 Участок: {debtor['plot']}\n"
            f"💰 Долг: {int(debtor['debt'])} ₽\n\n"
        )

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute(
            "SELECT telegram_id "
            "FROM users "
            "WHERE telegram_id IS NOT NULL"
        )

        users = await cursor.fetchall()

    for user in users:

        try:

            await context.bot.send_message(
                chat_id=user[0],
                text=message
            )

        except Exception as e:

            logger.error(e)

# =========================================================
# СТАРТ
# =========================================================

async def start(update, context):

    contact_button = KeyboardButton(
        text="📱 Поделиться номером",
        request_contact=True
    )

    keyboard = [[contact_button]]

    await update.message.reply_text(
        "Здравствуйте!\n\n"
        "Подтвердите номер телефона.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )

    return ASK_PHONE

# =========================================================
# ПРОВЕРКА ТЕЛЕФОНА
# =========================================================

async def ask_phone(update, context):

    contact = update.message.contact

    if not contact:

        await update.message.reply_text(
            "Нажмите кнопку отправки номера."
        )

        return ASK_PHONE

    phone = normalize_phone(
        contact.phone_number
    )

    user = await get_user_by_phone(phone)

    if not user:

        await update.message.reply_text(
            "❌ Номер отсутствует в базе.",
            reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END

    await update_telegram_id(
        phone,
        update.effective_user.id
    )

    if user[3] == 0:

        await update.message.reply_text(
            WELCOME_TEXT
        )

        await set_welcomed(phone)

    await update.message.reply_text(
        f"✅ Добро пожаловать, {user[2]}!",
        reply_markup=ReplyKeyboardRemove()
    )

    return await show_menu(update, context)

# =========================================================
# МЕНЮ
# =========================================================

async def show_menu(update, context):

    keyboard = [
        ["💰 Взносы", "📰 Новости"],
        ["💬 Чат СНТ", "🚪 Открыть ворота"],
        ["⚠️ Должники"]
    ]

    await update.message.reply_text(
        "Выберите раздел:",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )

    return MAIN_MENU

# =========================================================
# ОБРАБОТКА МЕНЮ
# =========================================================

async def handle_menu(update, context):

    text = update.message.text

    # ВЗНОСЫ
    if text == "💰 Взносы":

        keyboard = [[
            InlineKeyboardButton(
                "💳 Оплатить",
                url=PAYMENT_LINK
            )
        ]]

        await update.message.reply_text(
            "💰 Оплата взносов СНТ",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

    # НОВОСТИ
    elif text == "📰 Новости":

        try:

            with open(
                "data/news.txt",
                "r",
                encoding="utf-8"
            ) as f:

                news = f.read()

            await update.message.reply_text(
                news
            )

        except:

            await update.message.reply_text(
                "Новостей пока нет."
            )

    # ЧАТ
    elif text == "💬 Чат СНТ":

        await update.message.reply_text(
            CHAT_LINK
        )

    # ДОЛЖНИКИ
    elif text == "⚠️ Должники":

        debtors = await load_debtors()

        if not debtors:

            await update.message.reply_text(
                "❌ Ошибка загрузки файла."
            )

            return MAIN_MENU

        message = "⚠️ ДОЛЖНИКИ СНТ\n\n"

        for debtor in debtors:

            message += (
                f"🏠 Участок: {debtor['plot']}\n"
                f"💰 Долг: {int(debtor['debt'])} ₽\n\n"
            )

        await update.message.reply_text(
            message
        )

    # ВОРОТА
    elif text == "🚪 Открыть ворота":

        user_id = update.effective_user.id

        if not can_open_gate(user_id):

            await update.message.reply_text(
                "⏳ Повторите через 30 секунд."
            )

            return MAIN_MENU

        await update.message.reply_text(
            "🚪 Для открытия ворот "
            f"совершите звонок:\n\n{GATE_PHONE}"
        )

    else:

        await update.message.reply_text(
            "Выберите пункт меню."
        )

    return MAIN_MENU

# =========================================================
# МОДЕРАЦИЯ ЧАТА
# =========================================================

async def moderate_chat(update, context):

    if not update.message:
        return

    if not update.message.text:
        return

    text = update.message.text.lower()

    for word in BAD_WORDS:

        if word in text:

            try:

                await update.message.delete()

            except:
                pass

            try:

                until_date = (
                    datetime.now() +
                    timedelta(hours=24)
                )

                await context.bot.ban_chat_member(
                    chat_id=update.effective_chat.id,
                    user_id=update.effective_user.id,
                    until_date=until_date
                )

                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=(
                        f"⛔ "
                        f"{update.effective_user.full_name} "
                        f"заблокирован на 24 часа "
                        f"за нарушение правил."
                    )
                )

            except Exception as e:

                logger.error(e)

            return

# =========================================================
# CANCEL
# =========================================================

async def cancel(update, context):

    await update.message.reply_text(
        "До свидания.",
        reply_markup=ReplyKeyboardRemove()
    )

    return ConversationHandler.END

# =========================================================
# POST INIT
# =========================================================

async def post_init(app):

    await init_db()

    logger.info("DB initialized")

# =========================================================
# MAIN
# =========================================================

def main():

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    conv_handler = ConversationHandler(

        entry_points=[
            CommandHandler("start", start)
        ],

        states={

            ASK_PHONE: [
                MessageHandler(
                    filters.CONTACT,
                    ask_phone
                )
            ],

            MAIN_MENU: [
                MessageHandler(
                    filters.TEXT &
                    ~filters.COMMAND,
                    handle_menu
                )
            ]
        },

        fallbacks=[
            CommandHandler("cancel", cancel)
        ]
    )

    app.add_handler(conv_handler)

    # МОДЕРАЦИЯ ЧАТА
    app.add_handler(
        MessageHandler(
            filters.TEXT &
            filters.ChatType.GROUPS,
            moderate_chat
        )
    )

    # ЕЖЕНЕДЕЛЬНОЕ НАПОМИНАНИЕ
    app.job_queue.run_weekly(
        weekly_debt_notification,
        time=time(hour=10, minute=0),
        days=(0,)
    )

    logger.info("Bot started")

    app.run_polling()

# =========================================================

if __name__ == "__main__":
    main()