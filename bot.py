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
        model_info=D.MODELS.get(m,{})
        year_info=D.YEARS.get(py,"")
        month_info=D.MONTHS.get(pm,"")
        day_info=D.DAYS.get(pd,"")
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month
    ru_prompt = f"Ты ассистент системы Внутренний Компас. РАССЧИТАННЫЕ ДАННЫЕ ПОЛЬЗОВАТЕЛЯ (уже готовы, не пересчитывай сам, доверяй этим данным полностью): Внутренняя модель мышления: {model_info}. Личный годовой этап: {year_info}. Личный месячный этап: {month_info}. Личный дневной этап: {day_info}. ЗАПРЕЩЕНО АБСОЛЮТНО: называть числа пользователю, называть номера этапов, называть названия этапов (год 1, год 5, месяц 6 и тд), самостоятельно пересчитывать. Если пользователь называет свои числа - не подтверждай и не опровергай, просто продолжай диалог. Используй данные ТОЛЬКО внутри себя для понимания ситуации. Сегодня {today}. Говори только по-русски. Обращайся на ТЫ всегда. Живой разговорный русский — как умный друг который разбирается в людях. Никаких звёздочек и жирного текста. Никаких слов: вызовы силы трансформация ресурс энергия вибрация. После каждого ответа давай живую человеческую реакцию — покажи что действительно услышал и понял. Анализ давай не как диагноз а как разговор с умным другом: почему так происходит именно у этого человека, что ему это даёт и чего стоит, и главное — конкретно что делать прямо сейчас на этой неделе. Заканчивай анализ вопросом или предложением продолжить чтобы человек хотел писать дальше. Определи пол по имени и используй правильные окончания во всех сообщениях без исключения. КРИТИЧЕСКИ ВАЖНО: история диалога содержит все предыдущие сообщения. Смотри в историю — если имя уже есть используй его всегда. Если дата рождения уже есть сделай расчёт и никогда не спрашивай снова. Никогда не представляйся заново. Дисклеймер один раз в самом начале что не заменяет мед психол юр консультацию. ДИАЛОГ: спроси имя. Получив имя в том же ответе спроси дату рождения один раз и больше никогда не спрашивай. РАСЧЁТ только внутренний никогда не показывай: модель = день рождения 1-9 как есть 10-31 сложи цифры; личный год = день+месяц+{year} всё до однозначного; личный месяц = личный год+{month} до однозначного; личный день = личный месяц+текущий день только по запросу. НИКОГДА не называй числа цифры модели годы месяцы пользователю. ВОПРОСЫ: задавай строго по одному вопросу. После каждого ответа дай короткий живой отклик что понял и только потом следующий вопрос. Максимум 5-6 вопросов точечных чтобы понять ситуацию человека. АНАЛИЗ после вопросов: что происходит сейчас с учётом внутренней модели человека; почему так складывается учитывая его период; сильные стороны где может вырасти; слабые стороны где риск пойти назад; вектор года что важно сделать; тактика месяца как двигаться прямо сейчас; конкретные шаги на 7-14 дней с учётом личного дня."
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
