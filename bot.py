import os
import re
import logging
import aiohttp
import aiosqlite
import tempfile
import openpyxl

from datetime import datetime, timedelta
from dotenv import load_dotenv

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions
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
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")

GATE_API_URL = os.getenv("GATE_API_URL")
GATE_API_KEY = os.getenv("GATE_API_KEY")
GATE_PHONE = os.getenv("GATE_PHONE")

# ID группы СНТ
SNT_CHAT_ID = int(os.getenv("SNT_CHAT_ID", "0"))

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
# АНТИМАТ
# =========================================================

BAD_WORDS = [
    "хуй",
    "пизд",
    "еб",
    "бля",
    "сука",
    "нахуй",
    "мудак",
    "долбоеб",
    "гандон",
    "уеб"
]

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
# ПРОВЕРКА НА МАТ
# =========================================================

def contains_bad_words(text: str) -> bool:

    if not text:
        return False

    text = text.lower()

    # защита от обхода мата
    text = text.replace("1", "и")
    text = text.replace("@", "а")
    text = text.replace("*", "")
    text = text.replace("#", "")
    text = text.replace("$", "с")

    for word in BAD_WORDS:

        pattern = rf"\b{re.escape(word)}\w*\b"

        if re.search(pattern, text):
            return True

    return False

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

        # пользователи
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT UNIQUE,
                name TEXT,
                telegram_id INTEGER
            )
        """)

        # предупреждения
        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                user_id INTEGER PRIMARY KEY,
                warnings INTEGER DEFAULT 0
            )
        """)

        await db.commit()

# =========================================================
# ПРЕДУПРЕЖДЕНИЯ
# =========================================================

async def get_user_warnings(user_id: int) -> int:

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute(
            "SELECT warnings FROM warnings WHERE user_id = ?",
            (user_id,)
        )

        row = await cursor.fetchone()

        if row:
            return row[0]

        return 0

async def add_warning(user_id: int):

    warnings = await get_user_warnings(user_id)
    warnings += 1

    async with aiosqlite.connect("database.db") as db:

        await db.execute("""
            INSERT INTO warnings(user_id, warnings)
            VALUES(?, ?)
            ON CONFLICT(user_id)
            DO UPDATE SET warnings=excluded.warnings
        """, (user_id, warnings))

        await db.commit()

    return warnings

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
# ЗАГРУЗКА ДОЛЖНИКОВ
# =========================================================

async def load_debtors_from_excel():

    try:

        if not GOOGLE_SHEETS_ID:
            return "❌ GOOGLE_SHEETS_ID не задан"

        url = (
            f"https://docs.google.com/spreadsheets/d/"
            f"{GOOGLE_SHEETS_ID}/export?format=xlsx"
        )

        async with aiohttp.ClientSession() as session:

            async with session.get(url) as response:

                if response.status != 200:
                    return "❌ Ошибка загрузки Excel"

                content = await response.read()

        with tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".xlsx"
        ) as tmp:

            tmp.write(content)
            path = tmp.name

        wb = openpyxl.load_workbook(path)
        ws = wb.active

        rows = list(ws.iter_rows(values_only=True))

        if not rows:
            return "❌ Пустой файл"

        headers = [
            str(h).strip().lower()
            for h in rows[0]
        ]

        try:

            idx_plot = headers.index("номер участка")
            idx_required = headers.index("сумма взноса")
            idx_paid = headers.index("фактическая сумма")

        except ValueError:
            return "❌ Неверные названия столбцов"

        debtors = []

        for row in rows[1:]:

            try:

                plot = str(row[idx_plot]).strip()

                required = float(row[idx_required] or 0)
                paid = float(row[idx_paid] or 0)

                debt = required - paid

                if debt > 0:
                    debtors.append((plot, debt))

            except Exception:
                continue

        wb.close()
        os.remove(path)

        if not debtors:
            return "✅ Должников нет"

        debtors.sort(
            key=lambda x: x[1],
            reverse=True
        )

        msg = "⚠️ ДОЛЖНИКИ СНТ\n\n"

        for plot, debt in debtors:

            msg += (
                f"🏠 Участок: {plot}\n"
                f"💰 Долг: {int(debt)} ₽\n\n"
            )

        return msg

    except Exception as e:

        logger.error(f"Excel error: {e}")

        return "❌ Ошибка обработки файла"

# =========================================================
# МОДЕРАЦИЯ ЧАТА
# =========================================================

async def moderate_chat(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    # только сообщения из группы
    if update.message.chat_id != SNT_CHAT_ID:
        return

    # только текст
    if not update.message.text:
        return

    text = update.message.text

    # проверка мата
    if not contains_bad_words(text):
        return

    user = update.effective_user

    if not user:
        return

    user_id = user.id

    # удаляем сообщение
    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Delete error: {e}")

    # добавляем предупреждение
    warnings = await add_warning(user_id)

    username = (
        f"@{user.username}"
        if user.username
        else user.first_name
    )

    # =====================================================
    # ПЕРВОЕ НАРУШЕНИЕ
    # =====================================================

    if warnings == 1:

        await context.bot.send_message(
            chat_id=SNT_CHAT_ID,
            text=(
                f"⚠️ {username}, "
                "обнаружен мат.\n\n"
                "Следующее нарушение → мут 3 часа.\n"
                "Далее → мут 24 часа."
            )
        )

    # =====================================================
    # ВТОРОЕ НАРУШЕНИЕ
    # =====================================================

    elif warnings == 2:

        until_date = datetime.now() + timedelta(hours=3)

        try:

            await context.bot.restrict_chat_member(
                chat_id=SNT_CHAT_ID,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False
                ),
                until_date=until_date
            )

            await context.bot.send_message(
                chat_id=SNT_CHAT_ID,
                text=(
                    f"⛔ {username} "
                    "получил мут на 3 часа "
                    "за повторный мат."
                )
            )

        except Exception as e:
            logger.error(f"Mute error: {e}")

    # =====================================================
    # ТРЕТЬЕ И ДАЛЕЕ
    # =====================================================

    else:

        until_date = datetime.now() + timedelta(hours=24)

        try:

            await context.bot.restrict_chat_member(
                chat_id=SNT_CHAT_ID,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=False
                ),
                until_date=until_date
            )

            await context.bot.send_message(
                chat_id=SNT_CHAT_ID,
                text=(
                    f"⛔ {username} "
                    "получил мут на 24 часа "
                    "за систематические нарушения."
                )
            )

        except Exception as e:
            logger.error(f"Mute error: {e}")

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

    context.user_data["phone"] = phone

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
                "Новости отсутствуют."
            )

    # =====================================================
    # ЧАТ
    # =====================================================

    elif text == "💬 Чат СНТ":

        keyboard = [
            [
                InlineKeyboardButton(
                    "💬 Открыть чат СНТ",
                    url=CHAT_LINK
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Нажмите кнопку ниже для перехода в чат СНТ:",
            reply_markup=reply_markup
        )

    # =====================================================
    # ДОЛЖНИКИ
    # =====================================================

    elif text == "⚠️ Должники":

        await update.message.reply_text(
            "📄 Загружаем список..."
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

    # меню бота
    app.add_handler(conv_handler)

    # модерация чата
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            moderate_chat
        )
    )

    logger.info("Bot started")

    app.run_polling()

# =========================================================

if __name__ == "__main__":
    main()