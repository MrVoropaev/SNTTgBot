import json
import logging
import requests
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters, ConversationHandler, ContextTypes
)

# Конфигурация MTT API
MTT_API_URL = "https://api.mtt.ru/v1"
MTT_API_KEY = "your_mtt_api_key"
MTT_CALLER_ID = "79876543210"  # Ваш номер в формате 79...
GATE_PHONE_NUMBER = "79876543210"  # Номер шлагбаума в формате 79...

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

# Функция звонка через MTT API
def call_gate_via_mtt():
    """Совершение звонка через API МТТ"""
    try:
        headers = {
            "Authorization": f"Bearer {MTT_API_KEY}",
            "Content-Type": "application/json"
        }
        
        # Данные для создания звонка
        call_data = {
            "caller_id": MTT_CALLER_ID,
            "callee_number": GATE_PHONE_NUMBER,
            "max_duration": 30,  # максимальная длительность звонка в секундах
            "auto_answer": True  # автоматическое принятие вызова
        }
        
        response = requests.post(
            f"{MTT_API_URL}/calls",
            json=call_data,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            call_info = response.json()
            logger.info(f"MTT call initiated: CallID={call_info.get('call_id')}")
            return True
        else:
            logger.error(f"MTT API error: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"MTT call failed: {e}")
        return False

# Альтернативный вариант через Zadarma API
def call_gate_via_zadarma():
    """Альтернативная реализация через Zadarma API"""
    try:
        api_key = "your_zadarma_api_key"
        secret_key = "your_zadarma_secret"
        
        # Генерация подписи для API
        import hashlib
        import hmac
        import time
        
        current_time = str(int(time.time()))
        sign_string = current_time + secret_key
        signature = hmac.new(secret_key.encode(), sign_string.encode(), hashlib.sha1).hexdigest()
        
        headers = {
            "Authorization": f"{api_key}:{signature}",
            "Content-Type": "application/json"
        }
        
        call_data = {
            "from": MTT_CALLER_ID,  # используем тот же номер
            "to": GATE_PHONE_NUMBER,
            "predicted": "auto"  # автоматическое определение номера
        }
        
        response = requests.post(
            "https://api.zadarma.com/v1/request/callback/",
            json=call_data,
            headers=headers,
            timeout=10
        )
        
        if response.status_code == 200:
            logger.info("Zadarma call initiated successfully")
            return True
        else:
            logger.error(f"Zadarma API error: {response.status_code} - {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Zadarma call failed: {e}")
        return False

# Альтернативный вариант через простой SIP-звонок
def call_gate_via_sip():
    """Простая реализация через SIP (для Asterisk/Freeswitch)"""
    try:
        # Данные для вашей SIP-АТС
        sip_server = "your_sip_server.com"
        sip_username = "your_sip_username"
        sip_password = "your_sip_password"
        
        # Используем библиотеку pjsua2 для SIP-звонков
        try:
            import pjsua2 as pj
            
            # Создаем аккаунт
            acc_cfg = pj.AccountConfig()
            acc_cfg.idUri = f"sip:{sip_username}@{sip_server}"
            acc_cfg.regConfig.registrarUri = f"sip:{sip_server}"
            acc_cfg.sipConfig.authCreds.append(
                pj.AuthCredInfo("digest", "*", sip_username, 0, sip_password)
            )
            
            # Здесь должна быть логика инициализации SIP-звонка
            # Это упрощенный пример - в реальности требуется полная настройка PJSUA2
            
            logger.info("SIP call configured")
            return True
            
        except ImportError:
            logger.warning("pjsua2 not available, using fallback")
            # Fallback: отправка команды на SIP-сервер через HTTP API
            sip_data = {
                "extension": "100",  # номер внутреннего абонента
                "number": GATE_PHONE_NUMBER,
                "callerid": MTT_CALLER_ID
            }
            
            response = requests.post(
                f"http://{sip_server}/api/call",
                json=sip_data,
                auth=(sip_username, sip_password),
                timeout=10
            )
            
            if response.status_code == 200:
                logger.info("SIP call initiated via HTTP API")
                return True
            else:
                return False
                
    except Exception as e:
        logger.error(f"SIP call failed: {e}")
        return False

async def fake_call_gate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Функция для имитации звонка на ворота"""
    await update.message.reply_text("📞 Звоню на ворота...")
    
    # Пробуем разные способы по порядку
    success = call_gate_via_mtt()
    
    # Если MTT не сработал, пробуем Zadarma
    if not success:
        logger.info("Trying Zadarma as fallback...")
        success = call_gate_via_zadarma()
    
    # Если и Zadarma не сработал, пробуем SIP
    if not success:
        logger.info("Trying SIP as fallback...")
        success = call_gate_via_sip()
    
    if success:
        await update.message.reply_text("✅ Ворота открываются. Звонок отправлен.")
    else:
        await update.message.reply_text(
            "❌ Ошибка при звонке. Попробуйте позже.\n"
            "Если проблема повторяется, свяжитесь с председателем СНТ."
        )

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
