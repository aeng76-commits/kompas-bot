# -*- coding: utf-8 -*-
import os, datetime, re, json
import anthropic
import psycopg2
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters

print("Bot starting", flush=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1509977932"))

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

user_sessions = {}
compass_state = {}

def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id BIGINT PRIMARY KEY,
            name TEXT,
            gender TEXT DEFAULT 'f',
            lang TEXT DEFAULT 'ru',
            birth_day INT,
            birth_month INT,
            birth_year INT,
            agreed BOOLEAN DEFAULT FALSE,
            trial_started_at TIMESTAMP,
            paid_until TIMESTAMP,
            daily_usage JSONB DEFAULT '{}'
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id,name,birth_day,birth_month,birth_year,lang,agreed,trial_started_at,paid_until,gender,daily_usage FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    keys = ["user_id","name","day","month","year","lang","agreed","trial_started_at","paid_until","gender","daily_usage"]
    return dict(zip(keys, row))

def save_user(user_id, **kwargs):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO users(user_id) VALUES(%s) ON CONFLICT(user_id) DO NOTHING", (user_id,))
    col_map = {"name":"name","gender":"gender","lang":"lang","birth_day":"birth_day","birth_month":"birth_month",
               "birth_year":"birth_year","agreed":"agreed","trial_started_at":"trial_started_at",
               "paid_until":"paid_until","daily_usage":"daily_usage"}
    for k, v in kwargs.items():
        col = col_map.get(k, k)
        if col == "daily_usage" and isinstance(v, dict):
            v = json.dumps(v)
        cur.execute(f"UPDATE users SET {col}=%s WHERE user_id=%s", (v, user_id))
    conn.commit()
    cur.close()
    conn.close()

def is_trial_active(user):
    if not user or not user.get("trial_started_at"):
        return False
    delta = datetime.datetime.now() - user["trial_started_at"].replace(tzinfo=None)
    return delta.total_seconds() < 259200

def is_paid(user):
    if not user or not user.get("paid_until"):
        return False
    return user["paid_until"].replace(tzinfo=None) > datetime.datetime.now()

def has_access(user):
    return is_paid(user) or is_trial_active(user)

def get_daily_usage(user_id):
    user = get_user(user_id)
    if not user:
        return {}
    usage = user.get("daily_usage") or {}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return usage.get(today, {})

def check_free_limit(user_id, section):
    usage = get_daily_usage(user_id)
    limits = {"me": 1, "year": 1, "month": 1, "day": 1, "compass": 2}
    return usage.get(section, 0) < limits.get(section, 1)

def increment_usage(user_id, section):
    user = get_user(user_id)
    usage = user.get("daily_usage") or {}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if today not in usage:
        usage = {today: {}}
    usage[today][section] = usage[today].get(section, 0) + 1
    save_user(user_id, daily_usage=usage)

RULES = {
    "ru": "Прежде чем начать, ознакомься с правилами:\n\n1. Анализ не заменяет врача, психолога, юриста или финансового консультанта.\n2. Ответственность за решения остаётся за тобой.\n3. Твои имя и дата рождения сохраняются один раз и не могут быть изменены самостоятельно.\n4. Твои данные используются только для твоего анализа и никому не передаются.\n5. Подписка продлевается автоматически. Отменить можно в любой момент — изменения вступают в силу в конце текущего периода.",
    "de": "Bevor wir beginnen, bitte lies die Regeln:\n\n1. Die Analyse ersetzt keinen Arzt, Psychologen oder Rechtsberater.\n2. Die Verantwortung fuer Entscheidungen liegt bei dir.\n3. Deine Daten werden nur fuer deine Analyse gespeichert.\n4. Das Abonnement wird manuell ueber @aeng0 eingerichtet.",
    "en": "Before we start, please read the rules:\n\n1. The analysis does not replace a doctor, psychologist or legal advisor.\n2. Responsibility for decisions remains with you.\n3. Your data is stored and used only for your analysis.\n4. Subscription is set up manually via @aeng0."
}

PAYPAL_LINKS = {
    "1m": "https://www.paypal.me/AlexandraEngel42/15EUR",
    "6m": "https://www.paypal.me/AlexandraEngel42/78EUR",
    "12m": "https://www.paypal.me/AlexandraEngel42/159EUR"
}
SEPA = "IBAN: DE28 5002 4024 4782 1216 01\nBank: C24 Bank\nEmpfaenger: Alexandra Engel"

MENU_BUTTONS = {
    "ru": [
        [InlineKeyboardButton("🌟 Основа моей личности", callback_data="btn_me")],
        [InlineKeyboardButton("🧭 Личный год", callback_data="btn_year")],
        [InlineKeyboardButton("📍 Личный месяц", callback_data="btn_month")],
        [InlineKeyboardButton("☀️ Личный день", callback_data="btn_day")],
        [InlineKeyboardButton("💡 Поговорим о главном", callback_data="btn_compass")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="btn_settings")],
    ],
    "de": [
        [InlineKeyboardButton("🌟 Meine Persoenlichkeit", callback_data="btn_me")],
        [InlineKeyboardButton("🧭 Persoenliches Jahr", callback_data="btn_year")],
        [InlineKeyboardButton("📍 Persoenlicher Monat", callback_data="btn_month")],
        [InlineKeyboardButton("☀️ Persoenlicher Tag", callback_data="btn_day")],
        [InlineKeyboardButton("💡 Lass uns reden", callback_data="btn_compass")],
        [InlineKeyboardButton("⚙️ Einstellungen", callback_data="btn_settings")],
    ],
    "en": [
        [InlineKeyboardButton("🌟 My Personality", callback_data="btn_me")],
        [InlineKeyboardButton("🧭 Personal Year", callback_data="btn_year")],
        [InlineKeyboardButton("📍 Personal Month", callback_data="btn_month")],
        [InlineKeyboardButton("☀️ Personal Day", callback_data="btn_day")],
        [InlineKeyboardButton("💡 Let's talk", callback_data="btn_compass")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")],
    ],
}

def get_upgrade_keyboard(lang):
    labels = {"ru": "💳 Открыть полный доступ", "de": "💳 Vollzugang freischalten", "en": "💳 Get full access"}
    return InlineKeyboardMarkup([[InlineKeyboardButton(labels.get(lang, labels["ru"]), callback_data="btn_pay")]])

def clean_text(text):
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    return text.strip()

def split_message(text, max_length=4000):
    text = clean_text(text)
    if "===БЛОК===" in text:
        segments = text.split("===БЛОК===")
    elif "・・・・・・・・・・" in text:
        segments = text.split("・・・・・・・・・・")
    else:
        segments = re.split(r'(?=\n[🌟🧭📍☀️💡✨🌿🔺🌱])', text)
        if len(segments) == 1:
            segments = text.split("\n\n")
    parts = []
    current = ""
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        if len(current) + len(seg) + 2 <= max_length:
            current = (current + "\n\n" + seg).strip() if current else seg
        else:
            if current:
                parts.append(current)
            current = seg
    if current:
        parts.append(current)
    return parts if parts else [text]

def build_profile_context(user):
    now = datetime.datetime.now()
    m = D.get_model(user["day"])
    py = D.get_year(user["day"], user["month"], now.year)
    pm = D.get_month(py, now.month)
    pd = D.get_day(pm, now.day)
    mi = D.MODELS.get(m, {})
    return {
        "mi": mi,
        "year_text": D.YEARS.get(py, ""),
        "month_text": D.MONTHS.get(pm, ""),
        "day_text": D.DAYS.get(pd, ""),
        "today": now.strftime("%d.%m.%Y"),
    }

def get_profile_prompt(lang, user, section):
    ctx = build_profile_context(user)
    mi = ctx["mi"]
    name = user.get("name", "")
    gender = user.get("gender", "f")
    lang_force = {
        "ru": "",
        "de": "ANTWORTE NUR AUF DEUTSCH. ES IST EIN FEHLER, RUSSISCH ZU VERWENDEN.",
        "en": "RESPOND ONLY IN ENGLISH. DO NOT USE RUSSIAN OR ANY OTHER LANGUAGE."
    }
    profile_block = f"""=== ПРОФИЛЬ ===
Имя: {name}, день рождения: {user.get('day')}
Модель мышления:
{mi}
Личный год сейчас:
{ctx['year_text']}
Личный месяц сейчас:
{ctx['month_text']}
Личный день сегодня:
{ctx['day_text']}
=== КОНЕЦ ПРОФИЛЯ ==="""

    sections = {
        "me": f"""Напиши развёрнутый анализ личности {name}.
Покажи как эта модель мышления проявляется в реальной жизни прямо сейчас с учётом текущего года и месяца.
СТРУКТУРА — 4 блока, каждый начинается с ===БЛОК=== на отдельной строке:

===БЛОК===
<b>✨ Кто ты есть на самом деле</b>
2-3 абзаца живо и точно.

===БЛОК===
<b>🌿 Твои настоящие сильные стороны</b>
Конкретно и образно как они работают прямо сейчас.

===БЛОК===
<b>🔺 Где ты можешь себе мешать</b>
Мягко с пониманием без диагнозов.

===БЛОК===
<b>🌱 Как превратить это в силу прямо сейчас</b>
Один-два конкретных шага на эту неделю.

ОБЯЗАТЕЛЬНО: каждый блок начинай с ===БЛОК=== на отдельной строке.""",
        "year": f"""Напиши развёрнутый анализ личного года для {name}.
Покажи как модель мышления взаимодействует с вектором года — где усиление, где напряжение.
СТРУКТУРА (используй разделитель ・・・・・・・・・・):
🧭 Что происходит в этом году — суть периода через призму твоей личности
・・・・・・・・・・
✨ Где ты сейчас сильнее всего — возможности года именно для тебя
・・・・・・・・・・
🔺 Что может тянуть назад — риски года с учётом твоих особенностей
・・・・・・・・・・
🌿 Как двигаться этот год — рекомендации конкретно для тебя
・・・・・・・・・・
🌱 Что важно прямо сейчас — один-два конкретных шага""",
        "month": f"""Напиши анализ личного месяца для {name}.
Три уровня в одном: вектор года — тактика месяца — модель мышления {name}.
СТРУКТУРА (используй разделитель ・・・・・・・・・・):
🧭 Тактика этого месяца в контексте твоего года
・・・・・・・・・・
✨ Возможности месяца именно для тебя
・・・・・・・・・・
🔺 Риски месяца — где твои особенности могут усилить сложности
・・・・・・・・・・
🌱 Шаги на ближайшие 7-14 дней""",
        "day": f"""Дай развёрнутый анализ личного дня для {name}.
СТРУКТУРА (используй разделитель ・・・・・・・・・・):
🧭 Энергия сегодняшнего дня
・・・・・・・・・・
🌿 2-3 рекомендации на сегодня
・・・・・・・・・・
🔺 2-3 риска на сегодня
・・・・・・・・・・
🌱 Главный фокус дня — одна конкретная задача"""
    }
    instruction = sections.get(section, sections["me"])
    gender_rule = "женские окончания: умная, сильная, готова" if gender == "f" else "мужские окончания: умный, сильный, готов"
    return f"""Ты ассистент системы Внутренний Компас. Сегодня {ctx['today']}.
{lang_force.get(lang, '')}
{profile_block}

{instruction}

ПРАВИЛА:
- WRITE ONLY IN {'Russian' if lang == 'ru' else ('German' if lang == 'de' else 'English')}. NO OTHER LANGUAGE.
- Обращение: ТЫ, имя точно: {name}, {gender_rule}
- Живой тёплый разговорный стиль — как умный друг, не психолог
- Короткие абзацы — максимум 3 предложения
- Заголовки блоков пиши в формате HTML: <b>Заголовок</b> на отдельной строке. Текст блока — обычный текст без тегов.
- Запрещено: нумерология, вибрация, трансформация, вызовы, силы
- Запрещено называть числа и номера периодов
- Рекомендации уникальны для этого человека
- Текст должен резонировать — говори точно про этого человека"""


def get_profile_prompts_list(lang, user, section):
    ctx = build_profile_context(user)
    mi = ctx["mi"]
    name = user.get("name", "")
    gender = user.get("gender", "f")
    g = "женские окончания" if gender == "f" else "мужские окончания"
    lf = {"ru": "", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}.get(lang, "")
    base = f"""{lf}
Ты ассистент Внутренний Компас. Сегодня {ctx['today']}.
Пользователь: {name}, {g}.
Модель мышления:
{mi}
Личный год: {ctx['year_text'][:300]}
Личный месяц: {ctx['month_text'][:300]}
Личный день: {ctx['day_text'][:200]}
Правила: ТЫ, живой стиль, без markdown звёздочек. Запрещено: нумерология, числа периодов."""
    b = {
        "me": [
            base + "\nНапиши только: <b>✨ Кто ты есть на самом деле</b>\n2-3 абзаца. Начни с этого тега.",
            base + "\nНапиши только: <b>🌿 Твои настоящие сильные стороны</b>\nКак работают сейчас. Начни с этого тега.",
            base + "\nНапиши только: <b>🔺 Где ты можешь себе мешать</b>\nМягко. Начни с этого тега.",
            base + "\nНапиши только: <b>🌱 Как превратить это в силу прямо сейчас</b>\n1-2 шага. Начни с этого тега.",
        ],
        "year": [
            base + "\nНапиши только: <b>🧭 Что происходит в этом году</b>\nСуть. Начни с этого тега.",
            base + "\nНапиши только: <b>✨ Где ты сейчас сильнее всего</b>\nВозможности. Начни с этого тега.",
            base + "\nНапиши только: <b>🔺 Что может тянуть назад</b>\nРиски. Начни с этого тега.",
            base + "\nНапиши только: <b>🌱 Что важно прямо сейчас</b>\n1-2 шага. Начни с этого тега.",
        ],
        "month": [
            base + "\nНапиши только: <b>🧭 Тактика этого месяца</b>\nВ контексте года. Начни с этого тега.",
            base + "\nНапиши только: <b>✨ Возможности месяца</b>\nДля тебя. Начни с этого тега.",
            base + "\nНапиши только: <b>🔺 Риски месяца</b>\nЧестно. Начни с этого тега.",
            base + "\nНапиши только: <b>🌱 Шаги на ближайшие 2 недели</b>\nКонкретно. Начни с этого тега.",
        ],
        "day": [
            base + "\nНапиши только: <b>🧭 Энергия сегодняшнего дня</b>\nСуть. Начни с этого тега.",
            base + "\nНапиши только: <b>🌿 Рекомендации на сегодня</b>\n2-3 штуки. Начни с этого тега.",
            base + "\nНапиши только: <b>🔺 Риски на сегодня</b>\n2-3 штуки. Начни с этого тега.",
            base + "\nНапиши только: <b>🌱 Главный фокус дня</b>\nОдна задача. Начни с этого тега.",
        ],
    }
    return b.get(section, b["me"])

def get_free_prompt(lang, user, section):
    ctx = build_profile_context(user)
    mi = ctx["mi"]
    name = user.get("name", "")
    gender = user.get("gender", "f")
    gender_rule = "женские окончания" if gender == "f" else "мужские окончания"
    lang_force = {"ru": "", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}
    hints = {
        "me": f"Дай короткий (3-4 предложения) но точный и цепляющий взгляд на личность {name}. Закончи на интересном месте.",
        "year": f"Дай короткий (3-4 предложения) взгляд на главную тему этого года для {name}. Закончи намёком что есть ещё много важного.",
        "month": f"Дай короткий (3-4 предложения) взгляд на тактику этого месяца для {name}. Закончи намёком.",
        "day": f"Дай короткий (2-3 предложения) взгляд на энергию сегодняшнего дня для {name}. Добавь один конкретный совет."
    }
    return f"""Ты ассистент системы Внутренний Компас. Сегодня {ctx['today']}.
{lang_force.get(lang, '')}
Модель мышления {name}:
{mi}.
Личный год: {ctx['year_text'][:120]}
Личный месяц: {ctx['month_text'][:120]}
{hints.get(section, hints['me'])}
ПРАВИЛА: ТЫ, {gender_rule}, живой стиль, чистый текст, без markdown, без нумерологии."""

def get_system_prompt(lang, user):
    name = user.get("name", "") if user else ""
    gender = user.get("gender", "f") if user else "f"
    gender_rule = "женские окончания: умная, сильная, готова" if gender == "f" else "мужские окончания: умный, сильный, готов"
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    lang_force = {"ru": "", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}
    profile_block = ""
    if user and user.get("day"):
        ctx = build_profile_context(user)
        mi = ctx["mi"]
        profile_block = f"""
=== ДАННЫЕ ПОЛЬЗОВАТЕЛЯ ===
Модель мышления:
{mi}.
Личный год: {ctx['year_text']}
Личный месяц: {ctx['month_text']}
=== КОНЕЦ ==="""
    return f"""Ты ассистент системы Внутренний Компас. Сегодня {today}.
{lang_force.get(lang, '')}
{profile_block}
Говори как умный тёплый друг который хорошо разбирается в людях.
Обращайся на ТЫ. Имя точно: {name}. {gender_rule}.
Никакого markdown. Никаких звёздочек и решёток. Только чистый текст.
Запрещено: нумерология, вибрация, трансформация, вызовы, силы.
Не называй числа и номера периодов.
Один вопрос за раз. После каждого ответа — живой отклик, потом следующий вопрос.
WRITE ONLY IN {'Russian' if lang == 'ru' else ('German' if lang == 'de' else 'English')}."""

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
    compass_state.pop(user_id, None)
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
    ]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))

async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    save_user(user_id, lang=lang)
    user_sessions[user_id] = []
    compass_state.pop(user_id, None)
    agree_btns = [
        [InlineKeyboardButton("✅ Принимаю" if lang=="ru" else ("✅ Ich stimme zu" if lang=="de" else "✅ I agree"), callback_data="agree")],
        [InlineKeyboardButton("❌ Не принимаю" if lang=="ru" else ("❌ Ich lehne ab" if lang=="de" else "❌ I decline"), callback_data="disagree")],
    ]
    await query.edit_message_text(RULES[lang], reply_markup=InlineKeyboardMarkup(agree_btns))

async def agree_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if query.data == "disagree":
        texts = {"ru": "Понятно. Если передумаешь — напиши /start.", "de": "Okay. Wenn du es dir anders ueberlegst — schreibe /start.", "en": "Okay. If you change your mind — write /start."}
        await query.edit_message_text(texts.get(lang, texts["ru"]))
        return
    save_user(user_id, agreed=True)
    greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Das ist der Innere Kompass. Wie heisst du?", "en": "Hi! This is Inner Compass. What is your name?"}
    msg = greet[lang]
    user_sessions[user_id] = [{"role": "assistant", "content": msg}]
    await query.edit_message_text(msg)

async def show_menu(context, user_id, lang, text=None):
    texts = {"ru": "Выбери раздел:", "de": "Waehle einen Bereich:", "en": "Choose a section:"}
    await context.bot.send_message(user_id, text or texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"])))

async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    data = query.data

    no_date_msg = {"ru": "Сначала введи дату рождения — напиши /start", "de": "Bitte gib dein Geburtsdatum ein — schreibe /start", "en": "Please enter your birthdate — write /start"}
    upsell_msg = {"ru": "Это лишь начало. Полный анализ — в подписке.", "de": "Das ist nur der Anfang. Vollstaendige Analyse mit Abonnement.", "en": "This is just the beginning. Full analysis with subscription."}
    limit_msg = {"ru": "Сегодня ты уже открывала этот раздел. Загляни завтра или открой полный доступ.", "de": "Du hast diesen Bereich heute schon geoeffnet.", "en": "You've already opened this section today."}

    if data in ("btn_me", "btn_year", "btn_month", "btn_day"):
        section = data.replace("btn_", "")
        loading = {
            "btn_me": {"ru": "🌟 Загружаю твой профиль...", "de": "🌟 Lade dein Profil...", "en": "🌟 Loading your profile..."},
            "btn_year": {"ru": "🧭 Анализирую твой год...", "de": "🧭 Analysiere dein Jahr...", "en": "🧭 Analysing your year..."},
            "btn_month": {"ru": "📍 Анализирую твой месяц...", "de": "📍 Analysiere deinen Monat...", "en": "📍 Analysing your month..."},
            "btn_day": {"ru": "☀️ Анализирую твой день...", "de": "☀️ Analysiere deinen Tag...", "en": "☀️ Analysing your day..."},
        }
        tokens = {"btn_me": 3000, "btn_year": 3000, "btn_month": 2500, "btn_day": 2000}
        await query.edit_message_text(loading[data].get(lang, loading[data]["ru"]))
        if not user or not user.get("day"):
            await context.bot.send_message(user_id, no_date_msg.get(lang, no_date_msg["ru"]))
            return
        if has_access(user):
            prompts = get_profile_prompts_list(lang, user, section)
            for p in prompts:
                resp = client.messages.create(model="claude-haiku-4-5", max_tokens=600, system=p, messages=[{"role": "user", "content": "Напиши"}])
                txt = clean_text(resp.content[0].text)
                if txt:
                    await context.bot.send_message(user_id, txt, parse_mode="HTML")
        else:
            if not check_free_limit(user_id, section):
                await context.bot.send_message(user_id, limit_msg.get(lang, limit_msg["ru"]), reply_markup=get_upgrade_keyboard(lang))
                await show_menu(context, user_id, lang)
                return
            increment_usage(user_id, section)
            free_tokens = {"me": 600, "year": 500, "month": 500, "day": 400}
            prompt = get_free_prompt(lang, user, section)
            response = client.messages.create(model="claude-haiku-4-5", max_tokens=free_tokens[section], system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
            await context.bot.send_message(user_id, clean_text(response.content[0].text))
            await context.bot.send_message(user_id, upsell_msg.get(lang, upsell_msg["ru"]), reply_markup=get_upgrade_keyboard(lang))
        await show_menu(context, user_id, lang)

    elif data == "btn_compass":
        if not user or not user.get("day"):
            await query.edit_message_text(no_date_msg.get(lang, no_date_msg["ru"]))
            return
        if not has_access(user):
            await query.edit_message_text(
                {"ru": "Этот раздел доступен по подписке.", "de": "Dieser Bereich ist nur mit Abonnement verfuegbar.", "en": "This section requires a subscription."}.get(lang, ""),
                reply_markup=get_upgrade_keyboard(lang)
            )
            return
        compass_state[user_id] = {"q_count": 0, "clarify_mode": False}
        user_sessions[user_id] = []
        start_q = {"ru": "Расскажи — что сейчас происходит в твоей жизни? С чем хочешь разобраться?", "de": "Erzaehl mir — was passiert gerade in deinem Leben? Womit moechtest du dich auseinandersetzen?", "en": "Tell me — what's happening in your life right now? What would you like to figure out?"}
        msg = start_q.get(lang, start_q["ru"])
        user_sessions[user_id].append({"role": "assistant", "content": msg})
        await query.edit_message_text(msg)

    elif data == "btn_settings":
        if not user:
            await query.edit_message_text("Напиши /start")
            return
        status_map = {
            "ru": ("Полный доступ ✅" if is_paid(user) else ("Пробный период 🌿" if is_trial_active(user) else "Доступ завершён 🔺")),
            "de": ("Vollzugang ✅" if is_paid(user) else ("Testphase 🌿" if is_trial_active(user) else "Zugang beendet 🔺")),
            "en": ("Full access ✅" if is_paid(user) else ("Trial 🌿" if is_trial_active(user) else "Access ended 🔺")),
        }
        info = {
            "ru": f"Имя: {user.get('name','-')}\nДата рождения: {user.get('day')}.{user.get('month')}.{user.get('year')}\nСтатус: {status_map['ru']}",
            "de": f"Name: {user.get('name','-')}\nGeburtsdatum: {user.get('day')}.{user.get('month')}.{user.get('year')}\nStatus: {status_map['de']}",
            "en": f"Name: {user.get('name','-')}\nDate of birth: {user.get('day')}.{user.get('month')}.{user.get('year')}\nStatus: {status_map['en']}"
        }
        btns = [
            [InlineKeyboardButton("💳 Подписка" if lang=="ru" else ("💳 Abonnement" if lang=="de" else "💳 Subscription"), callback_data="btn_pay")],
            [InlineKeyboardButton("🌐 Сменить язык" if lang=="ru" else ("🌐 Sprache aendern" if lang=="de" else "🌐 Change language"), callback_data="btn_lang")],
        ]
        await query.edit_message_text(info.get(lang, info["ru"]), reply_markup=InlineKeyboardMarkup(btns))

    elif data == "btn_lang":
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
            [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        ]
        await query.edit_message_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "btn_pay":
        descriptions = {
            "ru": {"1m": "1 месяц — 15€", "6m": "6 месяцев — 78€", "12m": "12 месяцев — 159€"},
            "de": {"1m": "1 Monat — 15€", "6m": "6 Monate — 78€", "12m": "12 Monate — 159€"},
            "en": {"1m": "1 month — 15€", "6m": "6 months — 78€", "12m": "12 months — 159€"}
        }
        header = {"ru": "Выбери тариф:", "de": "Waehle einen Tarif:", "en": "Choose a plan:"}
        btns = [
            [InlineKeyboardButton(descriptions[lang]["1m"], callback_data="pay_1m")],
            [InlineKeyboardButton(f"⭐ {descriptions[lang]['6m']}", callback_data="pay_6m")],
            [InlineKeyboardButton(descriptions[lang]["12m"], callback_data="pay_12m")],
        ]
        await query.edit_message_text(header.get(lang, header["ru"]), reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("pay_"):
        plan = data.replace("pay_", "")
        descriptions = {
            "ru": {"1m": "1 месяц — 15€", "6m": "6 месяцев — 78€", "12m": "12 месяцев — 159€"},
            "de": {"1m": "1 Monat — 15€", "6m": "6 Monate — 78€", "12m": "12 Monate — 159€"},
            "en": {"1m": "1 month — 15€", "6m": "6 months — 78€", "12m": "12 months — 159€"}
        }
        label = descriptions[lang][plan]
        paypal_url = PAYPAL_LINKS[plan]
        texts = {
            "ru": f"Тариф: {label}\n\nСпособ 1 — PayPal:\n{paypal_url}\n\nСпособ 2 — Банковский перевод (SEPA):\n{SEPA}\nНазначение: {label}\n\nПосле оплаты напиши @aeng0 — активирую в течение 24 часов.",
            "de": f"Tarif: {label}\n\nOption 1 — PayPal:\n{paypal_url}\n\nOption 2 — Bankueberweisung (SEPA):\n{SEPA}\nVerwendungszweck: {label}\n\nNach der Zahlung schreibe @aeng0 — Zugang wird innerhalb von 24 Stunden aktiviert.",
            "en": f"Plan: {label}\n\nOption 1 — PayPal:\n{paypal_url}\n\nOption 2 — Bank transfer (SEPA):\n{SEPA}\nReference: {label}\n\nAfter payment write @aeng0 — access will be activated within 24 hours."
        }
        btns = [[InlineKeyboardButton("◀️ Назад" if lang=="ru" else ("◀️ Zurueck" if lang=="de" else "◀️ Back"), callback_data="btn_pay")]]
        await query.edit_message_text(texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(btns))

async def gender_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    gender = query.data.replace("gender_", "")
    save_user(user_id, gender=gender)
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    date_q = {
        "ru": "Отлично! Напиши дату рождения в формате ДД.ММ.ГГГГ",
        "de": "Super! Schreibe dein Geburtsdatum im Format TT.MM.JJJJ",
        "en": "Great! Write your date of birth in format DD.MM.YYYY"
    }
    msg = date_q.get(lang, date_q["ru"])
    user_sessions[user_id] = [{"role": "assistant", "content": msg}]
    await query.edit_message_text(msg)

async def compass_yn_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if query.data == "compass_yes":
        compass_state.pop(user_id, None)
        user_sessions[user_id] = []
        await query.edit_message_text({"ru": "Рада помочь!", "de": "Gerne!", "en": "Glad to help!"}.get(lang, ""))
        await show_menu(context, user_id, lang)
    else:
        compass_state[user_id] = compass_state.get(user_id, {})
        compass_state[user_id]["clarify_mode"] = True
        compass_state[user_id]["clarify_count"] = 0
        texts = {"ru": "Хорошо. Что именно осталось непонятным?", "de": "Okay. Was genau ist unklar geblieben?", "en": "Okay. What exactly is still unclear?"}
        msg = texts.get(lang, texts["ru"])
        user_sessions[user_id].append({"role": "assistant", "content": msg})
        await query.edit_message_text(msg)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"

    if not user or not user.get("agreed"):
        await update.message.reply_text("Пожалуйста, начни с /start" if lang=="ru" else ("Bitte beginne mit /start" if lang=="de" else "Please start with /start"))
        return

    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})

    # Компас: режим диалога
    if user_id in compass_state:
        state = compass_state[user_id]
        q_count = state.get("q_count", 0)
        clarify_mode = state.get("clarify_mode", False)
        ctx = build_profile_context(user) if user.get("day") else {}
        mi = ctx.get("mi", {})
        profile_context = f"Имя: {user.get('name','')}, тип: {mi.get('name','')}.\nСильные стороны: {mi.get('strengths','')}. Риски: {mi.get('risks','')}.\nГод: {ctx.get('year_text','')}. Месяц: {ctx.get('month_text','')}."
        lang_only = f"RESPOND IN {'Russian' if lang=='ru' else ('German' if lang=='de' else 'English')} ONLY."

        if clarify_mode:
            clarify_count = state.get("clarify_count", 0)
            if clarify_count >= 2:
                compass_state.pop(user_id, None)
                await show_menu(context, user_id, lang)
                return
            sys = f"{profile_context}\nЧеловек сказал что ему что-то непонятно: \"{user_text}\"\nЗадай ОДИН мягкий уточняющий вопрос. ТЫ. Коротко. {lang_only}"
            resp = client.messages.create(model="claude-haiku-4-5", max_tokens=200, system=sys, messages=user_sessions[user_id])
            reply = clean_text(resp.content[0].text)
            state["clarify_count"] = clarify_count + 1
            compass_state[user_id] = state
            user_sessions[user_id].append({"role": "assistant", "content": reply})
            await update.message.reply_text(reply, parse_mode="HTML")
            return

        if q_count >= 5:
            analysis_sys = f"""{profile_context}
Человек рассказал о своей ситуации. Дай глубокий персональный анализ.
СТРУКТУРА (используй ・・・・・・・・・・ между блоками):
🧭 Что сейчас происходит — назови суть ситуации точно
・・・・・・・・・・
✨ Где твоя сила в этом — конкретно для этого человека
・・・・・・・・・・
🔺 Что может мешать — честно и с пониманием
・・・・・・・・・・
🌱 Конкретные шаги на эту неделю — 2-3 действия
ТЫ. Живой стиль. Чистый текст. {lang_only}"""
            resp = client.messages.create(model="claude-haiku-4-5", max_tokens=3000, system=analysis_sys, messages=user_sessions[user_id])
            for part in split_message(resp.content[0].text):
                await update.message.reply_text(part, parse_mode="HTML")
            understood = {"ru": "Всё понятно?", "de": "Ist alles klar?", "en": "Is everything clear?"}
            btns = [[
                InlineKeyboardButton("✅ Да" if lang=="ru" else ("✅ Ja" if lang=="de" else "✅ Yes"), callback_data="compass_yes"),
                InlineKeyboardButton("❓ Нет" if lang=="ru" else ("❓ Nein" if lang=="de" else "❓ No"), callback_data="compass_no")
            ]]
            await update.message.reply_text(understood.get(lang, understood["ru"]), reply_markup=InlineKeyboardMarkup(btns))
            state["q_count"] = 99
            compass_state[user_id] = state
            return

        q_sys = f"""{profile_context}
Ты ведёшь диалог с {user.get('name','')} чтобы понять её ситуацию. Это вопрос {q_count+1} из 5.
Сначала дай короткий живой отклик на то что человек сказал (1-2 предложения).
Потом задай ОДИН следующий вопрос — логично вытекающий из ответа.
ТЫ. Просто. Тепло. {lang_only}"""
        resp = client.messages.create(model="claude-haiku-4-5", max_tokens=200, system=q_sys, messages=user_sessions[user_id])
        reply = clean_text(resp.content[0].text)
        state["q_count"] = q_count + 1
        compass_state[user_id] = state
        user_sessions[user_id].append({"role": "assistant", "content": reply})
        await update.message.reply_text(reply, parse_mode="HTML")
        return

    # Имя
    if not user.get("name"):
        name = user_text.strip()
        save_user(user_id, name=name)
        gender_q = {
            "ru": f"Приятно познакомиться, {name}! Как мне к тебе обращаться?",
            "de": f"Schoen, dich kennenzulernen, {name}! Wie soll ich dich ansprechen?",
            "en": f"Nice to meet you, {name}! How should I address you?"
        }
        gender_btns = {
            "ru": [InlineKeyboardButton("👩 Женский", callback_data="gender_f"), InlineKeyboardButton("👨 Мужской", callback_data="gender_m")],
            "de": [InlineKeyboardButton("👩 Weiblich", callback_data="gender_f"), InlineKeyboardButton("👨 Maennlich", callback_data="gender_m")],
            "en": [InlineKeyboardButton("👩 She/her", callback_data="gender_f"), InlineKeyboardButton("👨 He/him", callback_data="gender_m")],
        }
        await update.message.reply_text(gender_q.get(lang, gender_q["ru"]), reply_markup=InlineKeyboardMarkup([gender_btns.get(lang, gender_btns["ru"])]))
        return

    # Дата рождения
    if not user.get("day"):
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", user_text)
        if m:
            save_user(user_id, birth_day=int(m.group(1)), birth_month=int(m.group(2)), birth_year=int(m.group(3)), trial_started_at=datetime.datetime.now())
            welcome = {"ru": "Готово! Выбери с чего начнём:", "de": "Fertig! Waehle, womit wir beginnen:", "en": "Done! Choose where to start:"}
            await update.message.reply_text(welcome.get(lang, welcome["ru"]), reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"])))
        else:
            err = {"ru": "Напиши дату в формате ДД.ММ.ГГГГ, например 04.10.1976", "de": "Schreibe das Datum im Format TT.MM.JJJJ, z.B. 04.10.1976", "en": "Write the date in format DD.MM.YYYY, e.g. 04.10.1976"}
            await update.message.reply_text(err.get(lang, err["ru"]))
        return

    # Обычный диалог
    sys_prompt = get_system_prompt(lang, user)
    response = client.messages.create(model="claude-haiku-4-5", max_tokens=1500, system=sys_prompt, messages=user_sessions[user_id])
    reply = clean_text(response.content[0].text)
    user_sessions[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply, parse_mode="HTML")

async def admin_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if not args:
        await update.message.reply_text("Использование: /reset_user ID")
        return
    target_id = int(args[0])
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE user_id=%s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"OK: данные {target_id} сброшены.")

async def admin_grant(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Использование: /grant_access ID дней")
        return
    target_id = int(args[0])
    days = int(args[1])
    paid_until = datetime.datetime.now() + datetime.timedelta(days=days)
    save_user(target_id, paid_until=paid_until)
    await update.message.reply_text(f"OK: доступ для {target_id} на {days} дней до {paid_until.strftime('%d.%m.%Y')}.")

if __name__ == "__main__":
    init_db()
    try:
        import urllib.request
        urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
    except:
        pass
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset_user", admin_reset))
    app.add_handler(CommandHandler("grant_access", admin_grant))
    app.add_handler(CallbackQueryHandler(agree_cb, pattern="^(agree|disagree)$"))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(gender_cb, pattern="^gender_"))
    app.add_handler(CallbackQueryHandler(compass_yn_cb, pattern="^compass_"))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^(btn_|pay_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None, drop_pending_updates=True)
