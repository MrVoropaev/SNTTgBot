import json
import logging
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes
)

# Конфигурация Plivo (аналог Twilio)
PLIVO_AUTH_ID = "your_auth_id"
PLIVO_AUTH_TOKEN = "your_auth_token"
PLIVO_PHONE_NUMBER = "+12345678900"
GATE_PHONE_NUMBER = "+79876543210"

# Логирование
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Состояния
ASK_PHONE, MAIN_MENU = range(2)

# Загрузка списка пользователей
try:
    with open("users.json", "r", encoding='utf-8') as f:
        USERS = json.load(f)
except FileNotFoundError:
    logger.error("Файл users.json не найден")
    USERS = {}
except json.JSONDecodeError:
    logger.error("Ошибка чтения users.json")
    USERS = {}

# Константы
CHAT_LINK = "https://t.me/+your_chat_link_here"
PAYMENT_LINK = "https://your-payment-link"
REKVIZITY = "Реквизиты СНТ «Победа»:\nИНН: ХХХХХХ\nБИК: ХХХХХХ\n..."

# Функция звонка через Plivo
def call_gate_via_plivo():
    try:
        import plivo
        
        # Создаем клиент Plivo
        client = plivo.RestClient(PLIVO_AUTH_ID, PLIVO_AUTH_TOKEN)
        
        # Параметры звонка
        call_params = {
            'from': PLIVO_PHONE_NUMBER,
            'to': GATE_PHONE_NUMBER,
            'answer_url': "https://s3.amazonaws.com/static.plivo.com/answer.xml",  # XML с инструкцией
            'answer_method': "GET"
        }
        
        # Инициируем звонок
        response = client.calls.create(**call_params)
        
        logger.info(f"Call initiated: RequestUUID={response.request_uuid}")
        return True
    except Exception as e:
        logger.error(f"Plivo call failed: {e}")
        return False

# Альтернативный вариант через Telnyx (другой аналог Twilio)
def call_gate_via_telnyx():
    """
    Альтернативная реализация через Telnyx
    Раскомментируйте, если хотите использовать Telnyx вместо Plivo
    """
    try:
        from telnyx import Telnyx
        telnyx = Telnyx(api_key="your_telnyx_api_key")
        
        call = telnyx.Call.create(
            from_=PLIVO_PHONE_NUMBER,  # используем ту же переменную для номера
            to=GATE_PHONE_NUMBER,
            connection_id="your_telnyx_connection_id"  # ID голосового соединения
        )
        
        logger.info(f"Call initiated: CallID={call.id}")
        return True
    except Exception as e:
        logger.error(f"Telnyx call failed: {e}")
        return False

async def fake_call_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Функция для имитации звонка на ворота"""
    await update.message.reply_text("📞 Звоню на ворота...")
    
    # Используем Plivo (можно заменить на call_gate_via_telnyx())
    success = call_gate_via_plivo()
    
    if success:
        await update.message.reply_text("✅ Ворота открываются. Звонок отправлен.")
    else:
        await update.message.reply_text("❌ Ошибка при звонке. Попробуйте позже.")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало работы с ботом"""
    contact_button = KeyboardButton("Поделиться номером", request_contact=True)
    reply_markup = ReplyKeyboardMarkup([[contact_button]], resize_keyboard=True)
    await update.message.reply_text(
        "Здравствуйте! Пожалуйста, подтвердите свой номер телефона:",
        reply_markup=reply_markup
    )
    return ASK_PHONE

async def ask_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка номера телефона"""
    contact = update.message.contact
    if not contact:
        await update.message.reply_text("Пожалуйста, поделитесь номером телефона через кнопку.")
        return ASK_PHONE
    
    phone = contact.phone_number

    if not phone.startswith("+"):
        phone = "+" + phone  # форматируем

    if phone in USERS:
        USERS[phone]["telegram_id"] = update.effective_user.id
        context.user_data["phone"] = phone
        await update.message.reply_text(
            f"Добро пожаловать, {USERS[phone]['name']}!",
            reply_markup=ReplyKeyboardRemove()
        )
        return await show_main_menu(update, context)
    else:
        await update.message.reply_text(
            "Ваш номер не найден в базе СНТ. Доступ запрещён.",
            reply_markup=ReplyKeyboardRemove()
        )
        return ConversationHandler.END

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать главное меню"""
    menu = [
        ["💰 Взносы", "📰 Новости"],
        ["💬 Чат", "🚪 Открыть ворота"],
        ["⚠️ Должники"]
    ]
    reply_markup = ReplyKeyboardMarkup(menu, resize_keyboard=True)
    await update.message.reply_text("Выберите раздел:", reply_markup=reply_markup)
    return MAIN_MENU

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка выбора в меню"""
    text = update.message.text

    if text == "💰 Взносы":
        msg = (f"💰 *Информация о взносах 2025 года:*\n"
               f"- Членский: 5000₽\n"
               f"- Целевой: 3000₽\n\n"
               f"{REKVIZITY}\n\n"
               f"[Перейти к оплате]({PAYMENT_LINK})")
        await update.message.reply_markdown(msg)
    
    elif text == "📰 Новости":
        try:
            with open("data/news.txt", "r", encoding="utf-8") as f:
                news = f.read()
            await update.message.reply_text(f"📰 Последние новости СНТ:\n\n{news}")
        except FileNotFoundError:
            await update.message.reply_text("📰 Новости временно недоступны.")
    
    elif text == "💬 Чат":
        await update.message.reply_text(f"💬 Перейдите в чат СНТ: {CHAT_LINK}")
    
    elif text == "🚪 Открыть ворота":
        await fake_call_gate(update, context)
    
    elif text == "⚠️ Должники":
        try:
            with open("data/debtors.txt", "r", encoding="utf-8") as f:
                debtors = f.read()
            await update.message.reply_text(f"⚠️ Должники СНТ:\n\n{debtors}")
        except FileNotFoundError:
            await update.message.reply_text("⚠️ Информация о должниках временно недоступна.")
    
    else:
        await update.message.reply_text("Выберите раздел из меню.")
    
    return MAIN_MENU

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Завершение диалога"""
    await update.message.reply_text("До свидания!", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

def main():
    """Основная функция запуска бота"""
    # Замените "YOUR_BOT_TOKEN" на реальный токен вашего бота
    app = Application.builder().token("YOUR_BOT_TOKEN").build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [MessageHandler(filters.CONTACT, ask_phone)],
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    app.add_handler(conv_handler)
    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
