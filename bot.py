import os, datetime, asyncio
import anthropic
import psycopg2
import sys
import re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters

print("Bot starting", flush=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = 284968583

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
user_sessions = {}

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            birth_day INT,
            birth_month INT,
            birth_year INT,
            lang TEXT DEFAULT 'ru',
            agreed BOOLEAN DEFAULT FALSE,
            registered_at TIMESTAMP DEFAULT NOW(),
            trial_started_at TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, birth_day, birth_month, birth_year, lang, agreed, trial_started_at FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"name": row[0], "day": row[1], "month": row[2], "year": row[3], "lang": row[4], "agreed": row[5], "trial_started_at": row[6]}
    return None

def save_user(user_id, **kwargs):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    for key, val in kwargs.items():
        cur.execute(f"UPDATE users SET {key} = %s WHERE user_id = %s", (val, user_id))
    conn.commit()
    cur.close()
    conn.close()

def is_trial_active(user):
    if not user or not user.get("trial_started_at"):
        return True
    delta = datetime.datetime.now() - user["trial_started_at"].replace(tzinfo=None)
    return delta.total_seconds() < 86400

def get_system_prompt(lang, name=None, birth_day=None, birth_month=None, birth_year=None, is_paid=False):
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month
    day = datetime.datetime.now().day
    profile_block = ""
    if birth_day and birth_month and birth_year:
        m = D.get_model(birth_day)
        py = D.get_year(birth_day, birth_month, year)
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
ГОДОВОЙ ПЕРИОД — ТОЧНЫЙ ТЕКСТ:
{year_text}
МЕСЯЧНЫЙ ПЕРИОД — ТОЧНЫЙ ТЕКСТ:
{month_text}
ДНЕВНОЙ ПЕРИОД — ТОЧНЫЙ ТЕКСТ:
{day_text}
КРИТИЧЕСКИ ВАЖНО:
1. Не заменяй описания периодов своими словами с другим смыслом.
2. Если написано "закрепление" — говори про закрепление.
3. Если написано "завершение" — говори про завершение.
4. Рекомендации строй ТОЛЬКО на основе данных выше.
=== КОНЕЦ ПРОФИЛЯ ===
"""
    if is_paid:
        depth = "Полный глубокий анализ по структуре: модель мышления → годовой период → месячный период → риски → практические шаги на 7-14 дней."
    else:
        depth = "Короткий цепляющий анализ: 2-3 предложения о модели мышления, 1-2 предложения о годовом периоде, 2-3 конкретных шага. В конце скажи что полный анализ доступен в платной версии."

    ru_prompt = f"""Ты ассистент системы Внутренний Компас. Сегодня {today}.
{profile_block}
{depth}
Говори только по-русски. Обращайся на ТЫ. Имя используй точно как написано.
Определи пол по имени, используй правильные окончания.
Живой тёплый стиль. Только чистый текст.
Нельзя: вызовы, силы, трансформация, ресурс, энергия, вибрация, нумерология.
Нельзя называть числа и номера этапов.
Используй смайлики для структуры:
🧭 перед блоком анализа периода
✨ перед важным наблюдением
🌿 перед рекомендацией
🔺 перед риском
💬 перед вопросом
🌱 перед практическими шагами
Между блоками используй разделитель: ・・・・・・・・・・
Задай 4-5 вопросов строго по одному. После каждого ответа живой отклик потом следующий вопрос.
Заканчивай вопросом чтобы человек хотел продолжить."""

    if lang == "de":
        return f"Du bist der Assistent des Inneren Kompass. Heute ist {today}.\n{profile_block}\n{depth}\nDeutsch. DU. Warm. Kein Markdown. Emojis: 🧭✨🌿🔺💬🌱"
    elif lang == "en":
        return f"You are Inner Compass assistant. Today {today}.\n{profile_block}\n{depth}\nEnglish. YOU. Warm. No markdown. Emojis: 🧭✨🌿🔺💬🌱"
    return ru_prompt

RULES = {
    "ru": """Прежде чем начать, ознакомься с правилами:

1. 🌿 Анализ не заменяет врача, психолога, юриста или финансового консультанта.
2. 🌿 Ответственность за решения остаётся за тобой.
3. 🌿 Твои имя и дата рождения сохраняются один раз и не могут быть изменены самостоятельно.
4. 🌿 Твои данные используются только для твоего анализа и никому не передаются.

Нажми кнопку ниже чтобы продолжить.""",
    "de": """Bevor wir beginnen, lies bitte die Regeln:

1. 🌿 Die Analyse ersetzt keinen Arzt, Psychologen oder Rechtsberater.
2. 🌿 Die Verantwortung für Entscheidungen liegt bei dir.
3. 🌿 Dein Name und Geburtsdatum werden einmalig gespeichert und können nicht selbst geändert werden.
4. 🌿 Deine Daten werden nur für deine Analyse verwendet und nicht weitergegeben.

Drücke die Taste unten um fortzufahren.""",
    "en": """Before we start, please read the rules:

1. 🌿 The analysis does not replace a doctor, psychologist or legal advisor.
2. 🌿 Responsibility for decisions remains with you.
3. 🌿 Your name and date of birth are saved once and cannot be changed independently.
4. 🌿 Your data is used only for your analysis and is not shared with anyone.

Press the button below to continue."""
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
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
    save_user(user_id, lang=lang)
    user_sessions[user_id] = []
    user = get_user(user_id)
    if user and user.get("agreed"):
        if user.get("name") and user.get("day"):
            greet = {"ru": f"С возвращением, {user['name']}! Чем могу помочь сегодня?", "de": f"Willkommen zurück, {user['name']}!", "en": f"Welcome back, {user['name']}!"}
            msg = greet.get(lang, greet["ru"])
            user_sessions[user_id] = [{"role": "assistant", "content": msg}]
            await query.edit_message_text(msg)
        else:
            greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Das ist der Innere Kompass. Wie heißt du?", "en": "Hi! This is Inner Compass. What is your name?"}
            msg = greet.get(lang, greet["ru"])
            user_sessions[user_id] = [{"role": "assistant", "content": msg}]
            await query.edit_message_text(msg)
    else:
        keyboard = [
            [InlineKeyboardButton("✅ Принимаю" if lang == "ru" else ("✅ Ich stimme zu" if lang == "de" else "✅ I agree"), callback_data="agree")],
            [InlineKeyboardButton("❌ Не принимаю" if lang == "ru" else ("❌ Ich lehne ab" if lang == "de" else "❌ I decline"), callback_data="disagree")]
        ]
        await query.edit_message_text(RULES.get(lang, RULES["ru"]), reply_markup=InlineKeyboardMarkup(keyboard))

async def agree_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if query.data == "agree":
        save_user(user_id, agreed=True)
        greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Das ist der Innere Kompass. Wie heißt du?", "en": "Hi! This is Inner Compass. What is your name?"}
        msg = greet.get(lang, greet["ru"])
        user_sessions[user_id] = [{"role": "assistant", "content": msg}]
        await query.edit_message_text(msg)
    else:
        keyboard = [
            [InlineKeyboardButton("✅ Принимаю" if lang == "ru" else ("✅ Ich stimme zu" if lang == "de" else "✅ I agree"), callback_data="agree")],
            [InlineKeyboardButton("❌ Не принимаю" if lang == "ru" else ("❌ Ich lehne ab" if lang == "de" else "❌ I decline"), callback_data="disagree")]
        ]
        await query.edit_message_text(RULES.get(lang, RULES["ru"]), reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /reset_user ID")
        return
    target_id = int(args[0])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"✅ Данные пользователя {target_id} сброшены.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if not user or not user.get("agreed"):
        await update.message.reply_text("Пожалуйста, начни с /start")
        return
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})
    if user and not user.get("day"):
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", user_text)
        if m:
            save_user(user_id, birth_day=int(m.group(1)), birth_month=int(m.group(2)), birth_year=int(m.group(3)), trial_started_at=datetime.datetime.now())
            user = get_user(user_id)
        elif not user.get("name"):
            save_user(user_id, name=user_text.strip())
            user = get_user(user_id)
    paid = not is_trial_active(user)
    sys_prompt = get_system_prompt(lang, user.get("name") if user else None, user.get("day") if user else None, user.get("month") if user else None, user.get("year") if user else None, is_paid=paid)
    response = client.messages.create(model="claude-haiku-4-5", max_tokens=1500, system=sys_prompt, messages=user_sessions[user_id])
    reply = response.content[0].text
    user_sessions[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

if __name__ == "__main__":
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset_user", admin_reset))
    app.add_handler(CallbackQueryHandler(agree_cb, pattern="^(agree|disagree)$"))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None)
