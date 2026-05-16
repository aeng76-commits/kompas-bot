import os
import anthropic
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters
print("Bot starting", flush=True)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
PROMPTS = {
"ru": "Ты AI-ассистент системы Внутренний Компас. Говори только по-русски. Спроси имя, затем дату рождения. Рассчитай: день 1-9 как есть, 10-31 сложи цифры. Личный год = день+месяц рождения+2026, до однозначного. Личный месяц = личный год+5, до однозначного. Задавай 3-5 вопросов для контекста перед анализом. Стиль: спокойный, тёплый, без мистики.",
"de": "Du bist der KI-Assistent des Inneren Kompass Systems. Sprich nur Deutsch. Frage nach dem Namen, dann nach dem Geburtsdatum. Berechne: Tag 1-9 wie er ist, 10-31 Ziffern addieren. Persoenliches Jahr = Geburtstag+Geburtsmonat+2026, auf einstellig reduzieren. Persoenlicher Monat = persoenliches Jahr+5, reduziert. Stelle 3-5 Kontextfragen vor der Analyse. Stil: ruhig, warm, keine Mystik.",
"en": "You are AI assistant of Inner Compass system. Speak English only. Ask name then birthdate. Calculate: day 1-9 use as is, 10-31 sum digits. Personal year = birth day+birth month+2026, reduce to single digit. Personal month = personal year+5, reduce. Ask 3-5 context questions before analysis. Style: calm, warm, no mysticism."
}
user_sessions = {}
user_languages = {}
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
user_sessions[user_id] = []
keyboard = [
[InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
[InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
[InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
]
reply_markup = InlineKeyboardMarkup(keyboard)
await update.message.reply_text("Выберите язык / Sprache wählen / Choose language:", reply_markup=reply_markup)
async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
query = update.callback_query
await query.answer()
user_id = query.from_user.id
lang = query.data.replace("lang_", "")
user_languages[user_id] = lang
user_sessions[user_id] = []
greetings = {
"ru": "Добро пожаловать в Внутренний Компас! Как вас зовут?",
"de": "Willkommen beim Inneren Kompass! Wie heißen Sie?",
"en": "Welcome to Inner Compass! What is your name?"
}
await query.edit_message_text(greetings[lang])
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
user_id = update.effective_user.id
user_text = update.message.text
lang = user_languages.get(user_id, "ru")
if user_id not in user_sessions:
user_sessions[user_id] = []
user_sessions[user_id].append({"role": "user", "content": user_text})
response = client.messages.create(
model="claude-haiku-4-5",
max_tokens=1000,
system=PROMPTS[lang],
messages=user_sessions[user_id]
)
reply = response.content[0].text
user_sessions[user_id].append({"role": "assistant", "content": reply})
await update.message.reply_text(reply)
if name == "main": app = ApplicationBuilder().token(TELEGRAM_TOKEN).build() app.add_handler(CommandHandler("start", start)) app.add_handler(CallbackQueryHandler(language_callback, pattern="^lang_")) app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)) app.run_polling(stop_signals=None) ENDBOT