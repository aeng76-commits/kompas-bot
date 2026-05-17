import os, datetime
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters
print("Bot starting", flush=True)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
user_sessions = {}
user_languages = {}
def get_system_prompt(lang):
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month
    if lang == "de":
        return f"Du bist Inner Compass Assistent. Heute {today}. Sage DU. Kein Fettdruck. Frage Name dann Geburtsdatum einmal. Berechne intern. Zeige keine Zahlen. 3-4 Fragen dann Analyse."
    elif lang == "en":
        return f"You are Inner Compass. Today {today}. Use YOU. No bold. Ask name then birthdate once. Calculate internally. Never show numbers. 3-4 questions then analysis."
    else:
        return f"Ты ассистент Внутреннего Компаса. Сегодня {today}. Обращайся на ТЫ всегда. Живой разговорный русский без терминов вызовы силы трансформация. Никаких звёздочек жирного текста. Определи пол по имени и не меняй окончания никогда. Спроси имя. Получив имя в том же ответе спроси дату рождения один раз и больше не спрашивай. Получив дату рождения рассчитай внутренне: модель = день 1-9 как есть 10-31 сложи цифры; личный год = день+месяц+{year} всё до однозначного; личный месяц = личный год+{month} до однозначного. Никогда не называй числа и цифры пользователю. После каждого ответа дай живой отклик что понял и потом один вопрос. Задай 3-4 вопроса о ситуации. Потом анализ: что происходит почему так складывается на что обратить внимание шаги на 7-14 дней. Дисклеймер один раз в начале что не заменяет мед психол юр консультацию."
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
    keyboard = [[InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],[InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],[InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))
async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    user_languages[user_id] = lang
    user_sessions[user_id] = []
    greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Innerer Kompass. Wie heisst du?", "en": "Hi! Inner Compass. What is your name?"}
    await query.edit_message_text(greet[lang])
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    lang = user_languages.get(user_id, "ru")
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})
    response = client.messages.create(model="claude-haiku-4-5", max_tokens=1500, system=get_system_prompt(lang), messages=user_sessions[user_id])
    reply = response.content[0].text
    user_sessions[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None)
