import os, datetime
import anthropic
import sys
import re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters
print("Bot starting", flush=True)
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
user_sessions = {}
user_languages = {}
user_birthdata = {}
def get_system_prompt(lang, birth_day=None, birth_month=None, birth_year=None):
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month
    day = datetime.datetime.now().day
    profile_block = ""
    if birth_day and birth_month and birth_year:
        m = D.get_model(birth_day)
        py = D.get_year(birth_day, birth_month, datetime.datetime.now().year)
        pm = D.get_month(py, month)
        pd = D.get_day(pm, day)
        mi = D.MODELS.get(m, {})
        year_text = D.YEARS.get(py, "")
        month_text = D.MONTHS.get(pm, "")
        day_text = D.DAYS.get(pd, "")
        profile_block = f"""
=== ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ — ДАННЫЕ ТОЛЬКО ОТСЮДА ===
МОДЕЛЬ МЫШЛЕНИЯ: {mi.get("name", "")}
Сильные стороны: {mi.get("strengths", "")}
Риски: {mi.get("risks", "")}
В трудных ситуациях: {mi.get("chaos", "")}
Формула роста: {mi.get("formula", "")}
ГОДОВОЙ ПЕРИОД — ТОЧНЫЙ ТЕКСТ (используй только это, не перефразируй суть):
{year_text}
МЕСЯЧНЫЙ ПЕРИОД — ТОЧНЫЙ ТЕКСТ:
{month_text}
ДНЕВНОЙ ПЕРИОД — ТОЧНЫЙ ТЕКСТ:
{day_text}
КРИТИЧЕСКИ ВАЖНО:
1. Описание периодов выше — это ТОЧНЫЕ данные системы. Не заменяй их своими словами с другим смыслом.
2. Если в годовом периоде написано "закрепление" — говори про закрепление, не про "новые начинания".
3. Если написано "завершение" — говори про завершение, не про "переосмысление".
4. Рекомендации строй ТОЛЬКО на основе данных выше — никаких общих советов.
5. Советы уникальны для каждого человека — опирайся на конкретные сильные стороны и риски.
=== КОНЕЦ ПРОФИЛЯ ===
"""
    ru_prompt = f"""Ты ассистент системы Внутренний Компас. Сегодня {today}.
{profile_block}
Говори только по-русски. Обращайся на ТЫ всегда.
Используй имя ТОЧНО как человек написал. Определи пол по имени.
Живой тёплый разговорный стиль. Только чистый текст без markdown.
Нельзя использовать слова: вызовы, силы, трансформация, ресурс, энергия, вибрация, нумерология.
Нельзя называть числа, номера и названия этапов пользователю.
Диалог: тёплое приветствие + дисклеймер одной фразой. Спроси имя.
Получив имя — спроси дату рождения один раз и никогда больше.
Задай 4-5 вопросов строго по одному. После каждого ответа живой отклик потом следующий вопрос.
Анализ: модель мышления + текущий период года и месяца. Сильные стороны. Риски. Шаги на 7-14 дней.
Заканчивай вопросом чтобы человек хотел продолжить."""
    if lang == "de":
        return f"Du bist der Assistent des Inneren Kompass Systems. Heute ist {today}.\n{profile_block}\nSprich nur Deutsch. DU nie Sie. Kein Markdown. Frage nach Namen dann Geburtsdatum einmal. Eine Frage nach der anderen. Analyse mit Modell und Perioden."
    elif lang == "en":
        return f"You are the Inner Compass assistant. Today is {today}.\n{profile_block}\nEnglish only. No markdown. Ask name then birthdate once. One question at a time. Analysis with model and periods."
    else:
        return ru_prompt
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
    user_birthdata[user_id] = {}
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
    ]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))
async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    user_languages[user_id] = lang
    user_sessions[user_id] = []
    greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Das ist der Innere Kompass. Wie heißt du?", "en": "Hi! This is Inner Compass. What is your name?"}
    first_msg = greet[lang]
    user_sessions[user_id] = [{"role": "assistant", "content": first_msg}]
    await query.edit_message_text(first_msg)
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    lang = user_languages.get(user_id, "ru")
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})
    bd = user_birthdata.get(user_id, {})
    if not bd:
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", user_text)
        if m:
            user_birthdata[user_id] = {"day": int(m.group(1)), "month": int(m.group(2)), "year": int(m.group(3))}
            bd = user_birthdata[user_id]
    sys_prompt = get_system_prompt(lang, bd.get("day"), bd.get("month"), bd.get("year"))
    response = client.messages.create(model="claude-haiku-4-5", max_tokens=1500, system=sys_prompt, messages=user_sessions[user_id])
    reply = response.content[0].text
    user_sessions[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)
if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None)
