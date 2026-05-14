import os
import logging
import aiohttp
import aiosqlite
import pandas as pd
import tempfile

from datetime import datetime, timedelta
from dotenv import load_dotenv

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
# ЗАГРУЗКА .ENV
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
PAYMENT_LINK = os.getenv("PAYMENT_LINK")
CHAT_LINK = os.getenv("CHAT_LINK")
DEBTORS_FILE_URL = os.getenv("DEBTORS_FILE_URL")

GATE_API_URL = os.getenv("GATE_API_URL")
GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_PHONE = os.getenv("GATE_PHONE")

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

                logger.error(
                    f"Gate API error: {response.status}"
                )

                return False

    except Exception as e:

        logger.error(f"Gate open failed: {e}")

        return False

# =========================================================
# ЗАГРУЗКА ДОЛЖНИКОВ ИЗ EXCEL
# =========================================================

async def load_debtors_from_excel():

    try:

        async with aiohttp.ClientSession() as session:

            async with session.get(DEBTORS_FILE_URL) as response:

                if response.status != 200:

                    return (
                        "❌ Не удалось загрузить файл должников."
                    )

                content = await response.read()

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".xlsx"
        ) as temp_file:

            temp_file.write(content)

            temp_path = temp_file.name

        # Чтение Excel
        df = pd.read_excel(temp_path)

        # Удаление временного файла
        os.remove(temp_path)

        # Нормализация колонок
        df.columns = [
            str(col).strip().lower()
            for col in df.columns
        ]

        required_columns = [
            "номер участка",
            "сумма взноса",
            "фактическая сумма"
        ]

        for column in required_columns:

            if column not in df.columns:

                return (
                    "❌ Ошибка структуры файла.\n"
                    f"Не найден столбец: {column}"
                )

        debtors = []

        for _, row in df.iterrows():

            try:

                plot_number = row["номер участка"]

                required_amount = float(
                    row["сумма взноса"]
                )

                paid_amount = float(
                    row["фактическая сумма"]
                )

                debt = required_amount - paid_amount

                if debt > 0:

                    debtors.append(
                        {
                            "plot": plot_number,
                            "debt": debt
                        }
                    )

            except Exception:
                continue

        if not debtors:
            return "✅ Задолженностей нет."

        msg = "⚠️ ДОЛЖНИКИ СНТ\n\n"

        for debtor in debtors:

            msg += (
                f"🏠 Участок: {debtor['plot']}\n"
                f"💰 Долг: {int(debtor['debt'])} ₽\n\n"
            )

        return msg

    except Exception as e:

        logger.error(f"Debtors load error: {e}")

        return "❌ Ошибка обработки файла должников."

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

async def ask_phone(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return ASK_PHONE

    contact = update.message.contact

    if not contact:

        await update.message.reply_text(
            "❌ Нажмите кнопку «Поделиться номером»."
        )

        return ASK_PHONE

    if contact.user_id != update.effective_user.id:

        await update.message.reply_text(
            "❌ Отправьте свой собственный номер."
        )

        return ASK_PHONE

    phone = normalize_phone(contact.phone_number)

    logger.info(f"PHONE: {phone}")

    user = await get_user_by_phone(phone)

    logger.info(f"USER: {user}")

    if not user:

        await update.message.reply_text(
            "❌ Ваш номер отсутствует в базе СНТ.",
            reply_markup=ReplyKeyboardRemove()
        )

        logger.warning(
            f"Unauthorized access: {phone}"
        )

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

async def show_main_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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

async def handle_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return MAIN_MENU

    text = update.message.text

    # =====================================================
    # ВЗНОСЫ
    # =====================================================

    if text == "💰 Взносы":

        keyboard = [
            [
                InlineKeyboardButton(
                    "💳 Оплатить через СберБанк",
                    url=PAYMENT_LINK
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(
            keyboard
        )

        msg = (
            "💰 ВЗНОСЫ СНТ 2026\n\n"
            "Членский взнос: 15000 ₽\n\n"
            "Способы оплаты:\n"
            "• СберБанк Онлайн\n"
            "• СБП\n"
            "• Банковская карта\n\n"
            "Нажмите кнопку ниже для оплаты."
        )

        if os.path.exists("data/payment_qr.png"):

            with open(
                "data/payment_qr.png",
                "rb"
            ) as qr:

                await update.message.reply_photo(
                    photo=qr,
                    caption=msg,
                    reply_markup=reply_markup
                )

        else:

            await update.message.reply_text(
                msg,
                reply_markup=reply_markup
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
                "📰 Новости пока отсутствуют."
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

        await update.message.reply_text(
            "📄 Загружаем список должников..."
        )

        debtors_message = (
            await load_debtors_from_excel()
        )

        await update.message.reply_text(
            debtors_message
        )

    # =====================================================
    # ВОРОТА
    # =====================================================

    elif text == "🚪 Открыть ворота":

        user_id = update.effective_user.id

        if not can_open_gate(user_id):

            await update.message.reply_text(
                "⏳ Повторное открытие возможно "
                "через 30 секунд."
            )

            return MAIN_MENU

        msg = (
            "🚪 ОТКРЫТИЕ ВОРОТ\n\n"
            "📱 Для открытия ворот:\n"
            "1. Нажмите на номер ниже\n"
            "2. Совершите звонок\n\n"
            f"📞 {GATE_PHONE}\n\n"
            "⚠️ Работает только "
            "в мобильном Telegram."
        )

        await update.message.reply_text(msg)

    # =====================================================
    # НЕИЗВЕСТНАЯ КОМАНДА
    # =====================================================

    else:

        await update.message.reply_text(
            "Выберите пункт меню."
        )

    return MAIN_MENU

# =========================================================
# CANCEL
# =========================================================

async def cancel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.message:

        await update.message.reply_text(
            "До свидания.",
            reply_markup=ReplyKeyboardRemove()
        )

    return ConversationHandler.END

# =========================================================
# POST INIT
# =========================================================

async def post_init(app: Application):

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