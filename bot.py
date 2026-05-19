
import os
import re
import random
import asyncio
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
    CallbackQueryHandler,
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

SNT_CHAT_ID = int(os.getenv("SNT_CHAT_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DEBT_CHECK_HOUR = int(os.getenv("DEBT_CHECK_HOUR", "10"))

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

ASK_PHONE, ASK_PLOT, MAIN_MENU, REQUEST_TEXT = range(4)

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
# ПРОВЕРКА НА МАТ
# =========================================================


def contains_bad_words(text: str) -> bool:

    if not text:
        return False

    text = text.lower()

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
                plot TEXT
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS warnings (
                user_id INTEGER PRIMARY KEY,
                warnings INTEGER DEFAULT 0
            )
        """)

        await db.execute("""
            CREATE TABLE IF NOT EXISTS votes (
                user_id INTEGER PRIMARY KEY,
                vote TEXT
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
# ПОЛЬЗОВАТЕЛИ
# =========================================================


async def get_user_by_phone(phone: str):

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute(
            "SELECT id, phone, name FROM users WHERE phone = ?",
            (phone,)
        )

        return await cursor.fetchone()


async def update_telegram_id(phone: str, telegram_id: int):

    async with aiosqlite.connect("database.db") as db:

        await db.execute(
            "UPDATE users SET telegram_id = ? WHERE phone = ?",
            (telegram_id, phone)
        )

        await db.commit()


async def update_user_plot(phone: str, plot: str):

    async with aiosqlite.connect("database.db") as db:

        await db.execute(
            "UPDATE users SET plot = ? WHERE phone = ?",
            (plot, phone)
        )

        await db.commit()


async def get_user_by_telegram_id(telegram_id: int):

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute(
            "SELECT phone, name, plot FROM users WHERE telegram_id = ?",
            (telegram_id,)
        )

        return await cursor.fetchone()

# =========================================================
# ВОРОТА
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

                return response.status == 200

    except Exception as e:

        logger.error(f"Gate open failed: {e}")

        return False

# =========================================================
# ЗАГРУЗКА EXCEL
# =========================================================


async def download_excel():

    url = (
        f"https://docs.google.com/spreadsheets/d/"
        f"{GOOGLE_SHEETS_ID}/export?format=xlsx"
    )

    async with aiohttp.ClientSession() as session:

        async with session.get(url) as response:

            if response.status != 200:
                return None

            content = await response.read()

    with tempfile.NamedTemporaryFile(
        delete=False,
        suffix=".xlsx"
    ) as tmp:

        tmp.write(content)
        return tmp.name

# =========================================================
# ДОЛЖНИКИ
# =========================================================


async def load_debtors_from_excel():

    try:

        path = await download_excel()

        if not path:
            return "❌ Ошибка загрузки файла"

        wb = openpyxl.load_workbook(path)
        ws = wb.active

        rows = list(ws.iter_rows(values_only=True))

        headers = [
            str(h).strip().lower()
            for h in rows[0]
        ]

        idx_plot = headers.index("номер участка")
        idx_required = headers.index("сумма взноса")
        idx_paid = headers.index("фактическая сумма")

        debtors = []

        for row in rows[1:]:

            try:

                plot_value = row[idx_plot]

                if isinstance(plot_value, float):
                    plot = str(int(plot_value))
                else:
                    plot = str(plot_value).strip()

                required = float(row[idx_required] or 0)
                paid = float(row[idx_paid] or 0)

                debt = required - paid

                if debt > 0:
                    debtors.append((plot, int(debt)))

            except Exception:
                continue

        wb.close()
        os.remove(path)

        if not debtors:
            return "✅ Должников нет"

        debtors.sort(key=lambda x: x[1], reverse=True)

        msg = "⚠️ ДОЛЖНИКИ СНТ\n\n"

        for plot, debt in debtors:

            msg += (
                f"🏠 Участок: {plot}\n"
                f"💰 Долг: {debt} ₽\n\n"
            )

        return msg

    except Exception as e:

        logger.error(f"Debtors error: {e}")

        return "❌ Ошибка обработки файла"

# =========================================================
# ПЕРСОНАЛЬНЫЙ ДОЛГ
# =========================================================


async def get_personal_debt(plot: str):

    try:

        path = await download_excel()

        if not path:
            return None

        wb = openpyxl.load_workbook(path)
        ws = wb.active

        rows = list(ws.iter_rows(values_only=True))

        headers = [
            str(h).strip().lower()
            for h in rows[0]
        ]

        idx_plot = headers.index("номер участка")
        idx_required = headers.index("сумма взноса")
        idx_paid = headers.index("фактическая сумма")

        for row in rows[1:]:

            row_plot = row[idx_plot]

            if isinstance(row_plot, float):
                row_plot = str(int(row_plot))
            else:
                row_plot = str(row_plot).strip()

            if row_plot == str(plot):

                required = float(row[idx_required] or 0)
                paid = float(row[idx_paid] or 0)

                debt = int(required - paid)

                wb.close()
                os.remove(path)

                return max(0, debt)

        wb.close()
        os.remove(path)

        return 0

    except Exception as e:

        logger.error(f"Personal debt error: {e}")

        return None

# =========================================================
# АВТОНАПОМИНАНИЯ
# =========================================================


async def debt_notifications_loop(app: Application):

    await asyncio.sleep(15)

    while True:

        try:

            now = datetime.now()

            if now.hour == DEBT_CHECK_HOUR and now.minute == 0:

                async with aiosqlite.connect("database.db") as db:

                    cursor = await db.execute(
                        "SELECT telegram_id, plot FROM users WHERE telegram_id IS NOT NULL AND plot IS NOT NULL"
                    )

                    users = await cursor.fetchall()

                for telegram_id, plot in users:

                    debt = await get_personal_debt(plot)

                    if debt and debt > 0:

                        try:

                            await asyncio.sleep(
                                random.randint(10, 60)
                            )

                            await app.bot.send_message(
                                chat_id=telegram_id,
                                text=(
                                    f"⚠️ Напоминание СНТ\n\n"
                                    f"🏠 Участок: {plot}\n"
                                    f"💰 Задолженность: {debt} ₽\n\n"
                                    "Просим оплатить задолженность."
                                )
                            )

                        except Exception as e:

                            logger.error(f"Notify error: {e}")

                await asyncio.sleep(3600)

            await asyncio.sleep(60)

        except Exception as e:

            logger.error(f"Debt loop error: {e}")
            await asyncio.sleep(60)

# =========================================================
# МОДЕРАЦИЯ
# =========================================================


async def moderate_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return

    if update.message.chat_id != SNT_CHAT_ID:
        return

    if not update.message.text:
        return

    text = update.message.text

    if not contains_bad_words(text):
        return

    user = update.effective_user

    if not user:
        return

    user_id = user.id

    try:
        await update.message.delete()
    except Exception as e:
        logger.error(f"Delete error: {e}")

    warnings = await add_warning(user_id)

    username = (
        f"@{user.username}"
        if user.username
        else user.first_name
    )

    if warnings == 1:

        await context.bot.send_message(
            chat_id=SNT_CHAT_ID,
            text=(
                f"⚠️ {username}, обнаружен мат.\n\n"
                "Следующее нарушение → мут 3 часа."
            )
        )

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
                text=f"⛔ {username} получил мут на 3 часа"
            )

        except Exception as e:
            logger.error(f"Mute error: {e}")

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
                text=f"⛔ {username} получил мут на 24 часа"
            )

        except Exception as e:
            logger.error(f"Mute error: {e}")

# =========================================================
# START
# =========================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

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
        "Здравствуйте!\n\nПодтвердите номер телефона.",
        reply_markup=reply_markup
    )

    return ASK_PHONE

# =========================================================
# ПРОВЕРКА ТЕЛЕФОНА
# =========================================================


async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message:
        return ASK_PHONE

    contact = update.message.contact

    if not contact:

        await update.message.reply_text(
            "❌ Нажмите кнопку подтверждения номера"
        )

        return ASK_PHONE

    if contact.user_id != update.effective_user.id:

        await update.message.reply_text(
            "❌ Отправьте свой номер"
        )

        return ASK_PHONE

    phone = normalize_phone(contact.phone_number)

    user = await get_user_by_phone(phone)

    if not user:

        await update.message.reply_text(
            "❌ Ваш номер отсутствует в базе СНТ",
            reply_markup=ReplyKeyboardRemove()
        )

        return ConversationHandler.END

    await update_telegram_id(
        phone,
        update.effective_user.id
    )

    context.user_data["phone"] = phone

    await update.message.reply_text(
        f"✅ Добро пожаловать, {user[2]}!"
    )

    await update.message.reply_text(
        "🏠 Введите номер участка:"
    )

    return ASK_PLOT

# =========================================================
# НОМЕР УЧАСТКА
# =========================================================


async def ask_plot(update: Update, context: ContextTypes.DEFAULT_TYPE):

    plot = update.message.text.strip()

    phone = context.user_data.get("phone")

    await update_user_plot(phone, plot)

    await update.message.reply_text(
        f"✅ Участок {plot} сохранён"
    )

    return await show_main_menu(update, context)

# =========================================================
# ГЛАВНОЕ МЕНЮ
# =========================================================


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    keyboard = [
        ["💰 Взносы", "📰 Новости"],
        ["💬 Чат СНТ", "🚪 Открыть ворота"],
        ["⚠️ Должники", "💳 Мой долг"],
        ["🛠 Заявка", "🗳 Голосования"]
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
# ЗАЯВКИ
# =========================================================


async def request_text(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    user = await get_user_by_telegram_id(
        update.effective_user.id
    )

    if not user:
        return MAIN_MENU

    name = user[1]
    plot = user[2]

    msg = (
        f"🛠 НОВАЯ ЗАЯВКА\n\n"
        f"👤 {name}\n"
        f"🏠 Участок: {plot}\n\n"
        f"📄 {text}"
    )

    try:

        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=msg
        )

        await update.message.reply_text(
            "✅ Заявка отправлена председателю"
        )

    except Exception as e:

        logger.error(f"Request error: {e}")

        await update.message.reply_text(
            "❌ Ошибка отправки"
        )

    return MAIN_MENU

# =========================================================
# ГОЛОСОВАНИЯ
# =========================================================


async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    vote = (
        "Да"
        if query.data == "vote_yes"
        else "Нет"
    )

    async with aiosqlite.connect("database.db") as db:

        cursor = await db.execute(
            "SELECT vote FROM votes WHERE user_id = ?",
            (user_id,)
        )

        existing = await cursor.fetchone()

        if existing:

            await query.edit_message_text(
                "⚠️ Вы уже голосовали"
            )

            return

        await db.execute(
            "INSERT INTO votes(user_id, vote) VALUES(?, ?)",
            (user_id, vote)
        )

        await db.commit()

        cursor = await db.execute(
            "SELECT COUNT(*) FROM votes WHERE vote='Да'"
        )

        yes_count = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM votes WHERE vote='Нет'"
        )

        no_count = (await cursor.fetchone())[0]

    await query.edit_message_text(
        (
            f"✅ Ваш голос: {vote}\n\n"
            f"📊 Результаты:\n"
            f"Да: {yes_count}\n"
            f"Нет: {no_count}"
        )
    )

# =========================================================
# ОБРАБОТКА МЕНЮ
# =========================================================


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):

    text = update.message.text

    if text == "💰 Взносы":

        keyboard = [
            [
                InlineKeyboardButton(
                    "💳 Оплатить",
                    url=PAYMENT_LINK
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "💰 Оплата взносов СНТ",
            reply_markup=reply_markup
        )

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
                "Новости отсутствуют"
            )

    elif text == "💬 Чат СНТ":

        keyboard = [
            [
                InlineKeyboardButton(
                    "💬 Открыть чат",
                    url=CHAT_LINK
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "Нажмите кнопку ниже:",
            reply_markup=reply_markup
        )

    elif text == "⚠️ Должники":

        await update.message.reply_text(
            "📄 Загружаем список..."
        )

        debtors_message = await load_debtors_from_excel()

        await update.message.reply_text(debtors_message)

    elif text == "💳 Мой долг":

        user = await get_user_by_telegram_id(
            update.effective_user.id
        )

        if not user:

            await update.message.reply_text(
                "❌ Пользователь не найден"
            )

            return MAIN_MENU

        plot = user[2]

        debt = await get_personal_debt(plot)

        if debt is None:

            await update.message.reply_text(
                "❌ Ошибка загрузки"
            )

        elif debt <= 0:

            await update.message.reply_text(
                f"✅ У участка {plot} задолженности нет"
            )

        else:

            await update.message.reply_text(
                (
                    f"🏠 Участок: {plot}\n"
                    f"💰 Задолженность: {debt} ₽"
                )
            )

    elif text == "🛠 Заявка":

        await update.message.reply_text(
            "📝 Опишите проблему одним сообщением"
        )

        return REQUEST_TEXT

    elif text == "🗳 Голосования":

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Да",
                    callback_data="vote_yes"
                ),
                InlineKeyboardButton(
                    "❌ Нет",
                    callback_data="vote_no"
                )
            ]
        ]

        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            (
                "🗳 ГОЛОСОВАНИЕ\n\n"
                "Установить дополнительные камеры?"
            ),
            reply_markup=reply_markup
        )

    elif text == "🚪 Открыть ворота":

        user_id = update.effective_user.id

        if not can_open_gate(user_id):

            await update.message.reply_text(
                "⏳ Повторное открытие через 30 секунд"
            )

            return MAIN_MENU

        msg = (
            "🚪 ОТКРЫТИЕ ВОРОТ\n\n"
            "📞 Позвоните:\n\n"
            f"{GATE_PHONE}"
        )

        await update.message.reply_text(msg)

    return MAIN_MENU

# =========================================================
# CANCEL
# =========================================================


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "До свидания",
        reply_markup=ReplyKeyboardRemove()
    )

    return ConversationHandler.END

# =========================================================
# POST INIT
# =========================================================


async def post_init(app: Application):

    await init_db()

    asyncio.create_task(
        debt_notifications_loop(app)
    )

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

            ASK_PLOT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    ask_plot
                )
            ],

            MAIN_MENU: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    handle_menu
                )
            ],

            REQUEST_TEXT: [
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    request_text
                )
            ]
        },

        fallbacks=[
            CommandHandler("cancel", cancel)
        ]
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            moderate_chat
        ),
        group=0
    )

    app.add_handler(conv_handler, group=1)

    app.add_handler(
        CallbackQueryHandler(handle_vote)
    )

    logger.info("Bot started")

    app.run_polling()

# =========================================================

if __name__ == "__main__":
    main()
