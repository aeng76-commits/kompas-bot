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
def get_prompt(lang):
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    prompts = {
        "ru": f"Ты ассистент Внутренний Компас. Сегодня {today}. Говори по-русски. Спроси имя потом дату рождения. Расчёт: день+месяц+год до однозначного. Не показывай расчёты. 3-5 вопросов. Без мистики. Не заменяет врача.",
        "de": f"Du bist Inner Compass. Heute ist {today}. Sprich Deutsch. Frage Name dann Geburtsdatum. Berechnung: Tag+Monat+Jahr einstellig. Keine Berechnungen zeigen. 3-5 Fragen. Keine Mystik. Kein Arztersatz.",
        "en": f"You are Inner Compass. Today is {today}. Speak English. Ask name then birthdate. Calculate: day+month+year to single digit. Do not show calculations. Ask 3-5 questions. No mysticism. Not medical advice."
    }
    return prompts[lang]
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
    keyboard = [[InlineKeyboardButton("🇷🇺 Русский", 
callback_data="lang_ru")],[InlineKeyboardButton("🇩🇪 Deutsch",
callback_data="lang_de")],[InlineKeyboardButton("🇬🇧 English", 
callback_data="lang_en")]]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))
async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    user_languages[user_id] = lang
    user_sessions[user_id] = []
    greet = {"ru": "Добро пожаловать! Как вас зовут?", "de": "Willkommen! Wie heissen Sie?", "en": "Welcome! What is your name?"}
    await query.edit_message_text(greet[lang])
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    lang = user_languages.get(user_id, "ru")
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})
    response = client.messages.create(model="claude-haiku-4-5", max_tokens=1000, system=get_prompt(lang), messages=user_sessions[user_id])
    reply = response.content[0].text
    user_sessions[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None)
