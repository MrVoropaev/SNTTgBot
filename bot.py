import os
import re
import logging
import aiohttp
import aiosqlite
import pandas as pd
import tempfile

from datetime import datetime, timedelta

from dotenv import load_dotenv

from apscheduler.triggers.cron import CronTrigger

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters
)

# =========================================================
# ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_LINK = os.getenv("PAYMENT_LINK")
CHAT_LINK = os.getenv("CHAT_LINK")
DEBTORS_FILE_URL = os.getenv("DEBTORS_FILE_URL")
GATE_PHONE = os.getenv("GATE_PHONE")

# =========================================================
# ПРОВЕРКА ENV
# =========================================================

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не найден в .env")

# =========================================================
# ПРИВЕТСТВИЕ
# =========================================================

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
# МАТ
# =========================================================

BAD_WORDS = [
    "блять",
    "блядь",
    "бля",
    "сука",
    "сучка",
    "хуй",
    "нахуй",
    "похуй",
    "хуево",
    "хуевый",
    "ебать",
    "ебаный",
    "ебучий",
    "уебок",
    "пизда",
    "пиздец",
    "залупа",
    "мудак",
    "долбоеб",
    "гандон",
    "мразь",
    "шлюха",
    "ебло",
    "пидор",
    "пидорас",
    "говно",
    "дерьмо",
    "еблан",
    "мудила",
    "сучара",
    "хер",
    "дебил"
]

# =========================================================
# НОРМАЛИЗАЦИЯ ТЕЛЕФОНА
# =========================================================

def normalize_phone(phone: str) -> str:

    phone = (
        str(phone)
        .replace(" ", "")
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
# БАЗА
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
            """
            SELECT
                id,
                phone,
                name,
                welcomed
            FROM users
            WHERE phone = ?
            """,
            (phone,)
        )

        return await cursor.fetchone()

# =========================================================
# ОБНОВЛЕНИЕ TELEGRAM ID
# =========================================================

async def update_telegram_id(
    phone: str,
    telegram_id: int
):

    async with aiosqlite.connect("database.db") as db:

        await db.execute(
            """
            UPDATE users
            SET telegram_id = ?
            WHERE phone = ?
            """,
            (telegram_id, phone)
        )

        await db.commit()

# =========================================================
# ПРИВЕТСТВИЕ ПОКАЗАНО
# =========================================================

async def set_welcomed(phone: str):

    async with aiosqlite.connect("database.db") as db:

        await db.execute(
            """
            UPDATE users
            SET welcomed = 1
            WHERE phone = ?
            """,
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

                    logger.error(
                        f"Ошибка загрузки Excel: "
                        f"{response.status}"
                    )

                    return []

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

        required_columns = [
            "номер участка",
            "телефон",
            "сумма взноса",
            "фактическая сумма"
        ]

        for column in required_columns:

            if column not in df.columns:

                logger.error(
                    f"Нет колонки: {column}"
                )

                return []

        debtors = []

        for _, row in df.iterrows():

            try:

                plot = row["номер участка"]

                phone = normalize_phone(
                    row["телефон"]
                )

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
                        "phone": phone,
                        "debt": debt
                    })

            except Exception as e:

                logger.error(e)

        return debtors

    except Exception as e:

        logger.error(
            f"Ошибка debtors: {e}"
        )

        return []

# =========================================================
# УВЕДОМЛЕНИЯ
# =========================================================

async def weekly_debt_notification(context):

    logger.info(
        "Weekly debt notification started"
    )

    debtors = await load_debtors()

    if not debtors:
        return

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute("""
            SELECT
                telegram_id,
                phone,
                name
            FROM users
            WHERE telegram_id IS NOT NULL
        """)

        users = await cursor.fetchall()

    for user in users:

        telegram_id = user[0]
        phone = user[1]
        name = user[2]

        for debtor in debtors:

            if debtor["phone"] == phone:

                try:

                    message = (
                        f"⚠️ Уважаемый(ая) {name}\n\n"
                        f"У вас имеется задолженность "
                        f"по участку №{debtor['plot']}.\n\n"
                        f"💰 Сумма долга: "
                        f"{int(debtor['debt'])} ₽"
                    )

                    await context.bot.send_message(
                        chat_id=telegram_id,
                        text=message
                    )

                except Exception as e:

                    logger.error(e)

# =========================================================
# START
# =========================================================

async def start(update, context):

    if not update.message:
        return ConversationHandler.END

    contact_button = KeyboardButton(
        text="📱 Поделиться номером",
        request_contact=True
    )

    keyboard = [[contact_button]]

    await update.message.reply_text(
        "Здравствуйте!\n\n"
        "Для входа подтвердите номер телефона.",
        reply_markup=ReplyKeyboardMarkup(
            keyboard,
            resize_keyboard=True
        )
    )

    return ASK_PHONE

# =========================================================
# ПРОВЕРКА НОМЕРА
# =========================================================

async def ask_phone(update, context):

    if not update.message:
        return ASK_PHONE

    contact = update.message.contact

    if not contact:

        await update.message.reply_text(
            "❌ Нажмите кнопку отправки номера."
        )

        return ASK_PHONE

    if contact.user_id != update.effective_user.id:

        await update.message.reply_text(
            "❌ Отправьте свой номер."
        )

        return ASK_PHONE

    phone = normalize_phone(
        contact.phone_number
    )

    user = await get_user_by_phone(phone)

    if not user:

        await update.message.reply_text(
            "❌ Ваш номер отсутствует в базе СНТ.",
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
# МЕНЮ
# =========================================================

async def handle_menu(update, context):

    if not update.message:
        return MAIN_MENU

    text = update.message.text

    # =====================================================
    # ВЗНОСЫ
    # =====================================================

    if text == "💰 Взносы":

        keyboard = [[
            InlineKeyboardButton(
                "💳 Оплатить взнос",
                url=PAYMENT_LINK
            )
        ]]

        await update.message.reply_text(
            "💰 Оплата взносов СНТ",
            reply_markup=InlineKeyboardMarkup(
                keyboard
            )
        )

    # =====================================================
    # НОВОСТИ
    # =====================================================

    elif text == "📰 Новости":

        try:

            with open(
                "data/news.txt",
                "r",
                encoding="utf-8"
            ) as f:

                news = f.read()

            await update.message.reply_text(
                f"📰 Новости СНТ:\n\n{news}"
            )

        except FileNotFoundError:

            await update.message.reply_text(
                "📰 Новости отсутствуют."
            )

    # =====================================================
    # ЧАТ
    # =====================================================

    elif text == "💬 Чат СНТ":

        await update.message.reply_text(
            f"💬 Чат СНТ:\n{CHAT_LINK}"
        )

    # =====================================================
    # ДОЛЖНИКИ
    # =====================================================

    elif text == "⚠️ Должники":

        debtors = await load_debtors()

        if not debtors:

            await update.message.reply_text(
                "✅ Должников нет."
            )

            return MAIN_MENU

        message = "⚠️ ДОЛЖНИКИ СНТ\n\n"

        for debtor in debtors:

            message += (
                f"🏠 Участок: {debtor['plot']}\n"
                f"💰 Долг: "
                f"{int(debtor['debt'])} ₽\n\n"
            )

        await update.message.reply_text(
            message
        )

    # =====================================================
    # ВОРОТА
    # =====================================================

    elif text == "🚪 Открыть ворота":

        user_id = update.effective_user.id

        if not can_open_gate(user_id):

            await update.message.reply_text(
                "⏳ Подождите 30 секунд."
            )

            return MAIN_MENU

        await update.message.reply_text(
            "🚪 Для открытия ворот "
            "совершите звонок:\n\n"
            f"📞 {GATE_PHONE}"
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
                        f"заблокирован на 24 часа."
                    )
                )

            except Exception as e:

                logger.error(
                    f"Ban error: {e}"
                )

            return

# =========================================================
# CANCEL
# =========================================================

async def cancel(update, context):

    if update.message:

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

    logger.info("Database initialized")

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

    # =====================================================
    # CONVERSATION
    # =====================================================

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
            CommandHandler(
                "cancel",
                cancel
            )
        ]
    )

    app.add_handler(conv_handler)

    # =====================================================
    # МОДЕРАЦИЯ
    # =====================================================

    app.add_handler(
        MessageHandler(
            filters.TEXT &
            (
                filters.ChatType.GROUP |
                filters.ChatType.SUPERGROUP
            ),
            moderate_chat
        )
    )

    # =====================================================
    # JOB QUEUE
    # =====================================================

    app.job_queue.run_custom(
        weekly_debt_notification,
        job_kwargs={
            "trigger": CronTrigger(
                day_of_week="mon",
                hour=10,
                minute=0
            )
        },
        name="weekly_debt_notification"
    )

    logger.info("Bot started")

    app.run_polling()

# =========================================================
# START APP
# =========================================================

if __name__ == "__main__":
    main()