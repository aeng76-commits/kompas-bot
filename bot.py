import os, datetime
import anthropic
import sys
import os
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
    model_info=""
    year_info=""
    month_info=""
    day_info=""
    if birth_day and birth_month and birth_year:
        m=D.get_model(birth_day)
        py=D.get_year(birth_day,birth_month,birth_year)
        pm=D.get_month(py,datetime.datetime.now().month)
        pd=D.get_day(pm,datetime.datetime.now().day)
        mi=D.MODELS.get(m,{})
        model_info="Тип: "+mi.get("name","")+". Сильные стороны: "+mi.get("strengths","")+". Риски: "+mi.get("risks","")+". В трудных ситуациях: "+mi.get("chaos","")+". Формула: "+mi.get("formula","")
        year_info=D.YEARS.get(py,"")
        month_info=D.MONTHS.get(pm,"")
        day_info=D.DAYS.get(pd,"")
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month

    ru_prompt = f"Ты ассистент системы Внутренний Компас. Сегодня {today}. ДАННЫЕ только для внутреннего анализа не показывай пользователю: модель мышления {model_info} --- годовой период {year_info} --- месячный период {month_info} --- дневной период {day_info}. Говори только по-русски. Обращайся на ТЫ всегда. Используй имя ТОЧНО как человек написал — никаких сокращений и уменьшительных. Если написал Александра — всегда Александра, не Саша. Определи пол по имени и используй правильные окончания. Если человек задаёт вопрос или уходит в сторону — сначала ответь на его вопрос искренне и тепло, потом мягко вернись к разговору. Никогда не обрывай и не игнорируй вопросы человека. Будь гибким — это живой разговор а не анкета. Живой тёплый разговорный стиль как умный друг который разбирается в людях. Только чистый текст без звёздочек решёток жирного markdown. Нельзя использовать слова: вызовы силы трансформация ресурс энергия вибрация нумерология. Нельзя называть числа номера и названия этапов пользователю. Если пользователь называет числа просто продолжай диалог. Диалог: тёплое приветствие плюс дисклеймер одной фразой что не заменяет врача психолога юриста. Спроси имя. Получив имя в том же ответе спроси дату рождения один раз и никогда больше не спрашивай. Задай 4-5 вопросов о ситуации строго по одному. После каждого ответа живой отклик что услышал и понял потом следующий вопрос. Анализ: что происходит именно с этим человеком с учётом его модели мышления и текущего периода года и месяца. Сильные стороны где вырастет. Риски где пойдёт назад. Конкретные шаги на 7-14 дней. Заканчивай вопросом чтобы человек хотел продолжить. ВАЖНО: рекомендации уникальны для каждого человека. Используй конкретные сильные стороны и риски из модели мышления. Для года завершения фокус на закрытии и итогах. Для года запуска фокус на новых целях. Для модели с риском выгорания физическая разрядка и границы. Для модели с риском нерешительности конкретные решения и сроки. Никогда не давай одинаковые советы разным людям."

    de_prompt = f"Du bist der Assistent des Inneren Kompass Systems. Heute ist {today}. Sprich nur Deutsch. Sage immer DU nie Sie. Lebendiges umgangssprachliches Deutsch. Kein Fettdruck keine Sternchen. Bestimme das Geschlecht am Namen und verwende immer die richtigen Endungen. Einmal Disclaimer dass kein Arzt oder Anwalt ersetzt wird. DIALOG: Frage nach dem Namen. Mit dem Namen zusammen frage nach dem Geburtsdatum einmal nie wieder. BERECHNUNG nur intern nie zeigen: Modell = Geburtstag 1-9 wie ist 10-31 Ziffern addieren; Persoenliches Jahr = Tag+Monat+{year} alles einstellig; Persoenlicher Monat = Jahr+{month} einstellig. NIEMALS Zahlen oder Ziffern nennen. FRAGEN: immer nur eine Frage. Nach jeder Antwort kurze lebendige Reaktion dann naechste Frage. Maximal 5-6 Fragen. ANALYSE: aktuelle Situation mit innerer Struktur; Staerken und Risiken; Jahresvektor; Monatstaktik; konkrete Schritte 7-14 Tage."
    en_prompt = f"You are the Inner Compass assistant. Today is {today}. Speak English only. Always use YOU never formal. Natural conversational English. No bold no asterisks. Determine gender from name and use correct pronouns always. Once add disclaimer not replacing medical legal psychological advice. DIALOG: ask name. With name in same message ask birthdate once never again. CALCULATION internal never show: model = birth day 1-9 as is 10-31 sum digits; personal year = day+month+{year} all reduced; personal month = year+{month} reduced. NEVER mention numbers or digits. QUESTIONS: strictly one question at a time. After each answer give short warm response showing understanding then next question. Max 5-6 questions. ANALYSIS: current situation with inner model; strengths and risks; year vector; month tactics; concrete steps 7-14 days."
    if lang == "de": return de_prompt
    elif lang == "en": return en_prompt
    else: return ru_prompt
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
    user_birthdata[user_id] = {}
    keyboard = [[InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],[InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],[InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))
async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    user_languages[user_id] = lang
    user_sessions[user_id] = []
    greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Das ist der Innere Kompass. Wie heisst du?", "en": "Hi! This is Inner Compass. What is your name?"}
    await query.edit_message_text(greet[lang])
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    lang = user_languages.get(user_id, "ru")
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})
    import re
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
