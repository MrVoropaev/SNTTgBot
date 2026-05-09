import os
import logging
import aiohttp
import aiosqlite
from datetime import datetime, timedelta

from dotenv import load_dotenv

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
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
# ЗАГРУЗКА .ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_LINK = os.getenv("PAYMENT_LINK")
CHAT_LINK = os.getenv("CHAT_LINK")

GATE_API_URL = os.getenv("GATE_API_URL")
GATE_API_KEY = os.getenv("GATE_API_KEY")

# =========================================================
# ЛОГИРОВАНИЕ
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
# НОРМАЛИЗАЦИЯ НОМЕРА
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
# ПРОВЕРКА RATE LIMIT
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
                telegram_id INTEGER
            )
        """)

        await db.commit()

# =========================================================
# ПОИСК ПОЛЬЗОВАТЕЛЯ
# =========================================================

async def get_user_by_phone(phone: str):

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute(
            "SELECT id, phone, name FROM users WHERE phone = ?",
            (phone,)
        )

        row = await cursor.fetchone()

        return row

# =========================================================
# ОБНОВЛЕНИЕ TELEGRAM ID
# =========================================================

async def update_telegram_id(phone: str, telegram_id: int):

    async with aiosqlite.connect("database.db") as db:

        await db.execute(
            "UPDATE users SET telegram_id = ? WHERE phone = ?",
            (telegram_id, phone)
        )

        await db.commit()

# =========================================================
# ОТКРЫТИЕ ВОРОТ
# =========================================================

async def open_gate():

    headers = {
        "Authorization": f"Bearer {GATE_API_KEY}"
    }

    try:

        async with aiohttp.ClientSession() as session:

            async with session.post(
                GATE_API_URL,
                headers=headers,
                timeout=10
            ) as response:

                if response.status == 200:
                    return True

                logger.error(f"Gate API error: {response.status}")

                return False

    except Exception as e:
        logger.error(f"Gate open failed: {e}")
        return False

# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return ConversationHandler.END

    contact_button = KeyboardButton(
        text="📱 Поделиться номером",
        request_contact=True
    )

    keyboard = [[contact_button]]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )

    await update.message.reply_text(
        "Здравствуйте!\n\n"
        "Для входа подтвердите номер телефона.",
        reply_markup=reply_markup
    )

    return ASK_PHONE

# =========================================================
# ПРОВЕРКА НОМЕРА
# =========================================================

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):

    print(update)

    if not update.message:

        return ASK_PHONE

    contact = update.message.contact

    # Если контакта нет
    if not contact:

        await update.message.reply_text(
            "❌ Нажмите кнопку «Поделиться номером»."
        )

        return ASK_PHONE

    # Защита от чужих контактов
    if contact.user_id != update.effective_user.id:

        await update.message.reply_text(
            "❌ Отправьте свой собственный номер."
        )

        return ASK_PHONE

    phone = normalize_phone(contact.phone_number)

    print("PHONE:", phone)

    user = await get_user_by_phone(phone)

    print("USER:", user)

    if not user:

        await update.message.reply_text(
            "❌ Ваш номер отсутствует в базе СНТ.",
            reply_markup=ReplyKeyboardRemove()
        )

        logger.warning(f"Unauthorized access: {phone}")

        return ConversationHandler.END

    await update_telegram_id(
        phone,
        update.effective_user.id
    )

    context.user_data["phone"] = phone

    logger.info(f"Authorized: {phone}")

    await update.message.reply_text(
        f"✅ Добро пожаловать, {user[2]}!",
        reply_markup=ReplyKeyboardRemove()
    )

    return await show_main_menu(update, context)

# =========================================================
# ГЛАВНОЕ МЕНЮ
# =========================================================

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        ["💰 Взносы", "📰 Новости"],
        ["💬 Чат СНТ", "🚪 Открыть ворота"],
        ["⚠️ Должники"]
    ]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )

    await update.message.reply_text(
        "Выберите раздел:",
        reply_markup=reply_markup
    )

    return MAIN_MENU

# =========================================================
# ОБРАБОТКА МЕНЮ
# =========================================================

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return MAIN_MENU

    text = update.message.text

    # ==========================================
    # ВЗНОСЫ
    # ==========================================

    if text == "💰 Взносы":

        msg = (
            "💰 ВЗНОСЫ 2026\n\n"
            "Членский: 5000 ₽\n"
            "Целевой: 3000 ₽\n\n"
            f"Оплата:\n{PAYMENT_LINK}"
        )

        await update.message.reply_text(msg)

    # ==========================================
    # НОВОСТИ
    # ==========================================

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
                "Новости пока отсутствуют."
            )

    # ==========================================
    # ЧАТ
    # ==========================================

    elif text == "💬 Чат СНТ":

        await update.message.reply_text(
            f"💬 Чат СНТ:\n{CHAT_LINK}"
        )

    # ==========================================
    # ДОЛЖНИКИ
    # ==========================================

    elif text == "⚠️ Должники":

        try:

            with open(
                "data/debtors.txt",
                "r",
                encoding="utf-8"
            ) as f:

                debtors = f.read()

            await update.message.reply_text(
                f"⚠️ Должники:\n\n{debtors}"
            )

        except FileNotFoundError:

            await update.message.reply_text(
                "Список должников отсутствует."
            )

    # ==========================================
    # ВОРОТА
    # ==========================================

    elif text == "🚪 Открыть ворота":

        user_id = update.effective_user.id

        if not can_open_gate(user_id):

            await update.message.reply_text(
                "⏳ Подождите 30 секунд."
            )

            return MAIN_MENU

        await update.message.reply_text(
            "📞 Отправляем команду..."
        )

        success = await open_gate()

        if success:

            logger.info(
                f"Gate opened by user {user_id}"
            )

            await update.message.reply_text(
                "✅ Ворота открываются."
            )

        else:

            await update.message.reply_text(
                "❌ Ошибка открытия ворот."
            )

    # ==========================================
    # НЕИЗВЕСТНО
    # ==========================================

    else:

        await update.message.reply_text(
            "Выберите пункт меню."
        )

    return MAIN_MENU

# =========================================================
# CANCEL
# =========================================================

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if update.message:

        await update.message.reply_text(
            "До свидания.",
            reply_markup=ReplyKeyboardRemove()
        )

    return ConversationHandler.END

# =========================================================
# MAIN
# =========================================================

async def post_init(app: Application):

    await init_db()

    logger.info("Database initialized")

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
                    filters.ALL,
                    ask_phone
                )
            ],

            MAIN_MENU: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_menu
                )
            ]
        },

        fallbacks=[
            CommandHandler("cancel", cancel)
        ]
    )

    app.add_handler(conv_handler)

    logger.info("Bot started")

    app.run_polling()

# =========================================================

if __name__ == "__main__":
    main()