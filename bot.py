import stripe
import re
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

def get_session(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT session_data, compass_data FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return [], {}
    return row[0] or [], row[1] or {}

def save_session(user_id, session=None, compass=None):
    conn = get_db()
    cur = conn.cursor()
    if session is not None and compass is not None:
        cur.execute("UPDATE users SET session_data=%s, compass_data=%s WHERE user_id=%s",
                    (json.dumps(session), json.dumps(compass), user_id))
    elif session is not None:
        cur.execute("UPDATE users SET session_data=%s WHERE user_id=%s",
                    (json.dumps(session), user_id))
    elif compass is not None:
        cur.execute("UPDATE users SET compass_data=%s WHERE user_id=%s",
                    (json.dumps(compass), user_id))
    conn.commit()
    cur.close()
    conn.close()

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
            daily_usage JSONB DEFAULT '{}',
            remind_at DATE,
            session_data JSONB DEFAULT '[]',
            compass_data JSONB DEFAULT '{}'
        )
    """)
    # Добавляем колонки если не существуют
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS session_data JSONB DEFAULT '[]'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS compass_data JSONB DEFAULT '{}'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS data_changes INT DEFAULT 0")
        conn.commit()
    except:
        conn.rollback()
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id,name,birth_day,birth_month,birth_year,lang,agreed,trial_started_at,paid_until,gender,daily_usage,data_changes,referred_by,username,is_minor,about_work,about_finance,about_relations,about_personal,remind_at,name_changes,date_changes FROM users WHERE user_id=%s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    keys = ["user_id","name","day","month","year","lang","agreed","trial_started_at","paid_until","gender","daily_usage","data_changes","referred_by","username","is_minor","about_work","about_finance","about_relations","about_personal","remind_at","name_changes","date_changes"]
    return dict(zip(keys, row))

def save_user(user_id, **kwargs):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO users(user_id) VALUES(%s) ON CONFLICT(user_id) DO NOTHING", (user_id,))
    col_map = {"name":"name","gender":"gender","lang":"lang","birth_day":"birth_day","birth_month":"birth_month",
               "birth_year":"birth_year","agreed":"agreed","trial_started_at":"trial_started_at",
               "paid_until":"paid_until","daily_usage":"daily_usage","referred_by":"referred_by","username":"username","is_minor":"is_minor","about_work":"about_work","about_finance":"about_finance","about_relations":"about_relations","about_personal":"about_personal","name_changes":"name_changes","date_changes":"date_changes"}
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
    if is_trial_active(user) and not is_paid(user):
        return usage.get("trial", {})
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return usage.get(today, {})

def get_trial_usage(user_id):
    user = get_user(user_id)
    if not user:
        return {}
    usage = user.get("daily_usage") or {}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return usage.get(today, {})

def check_free_limit(user_id, section):
    usage = get_daily_usage(user_id) or {}
    limits = {"me": 1, "year": 1, "month": 1, "day": 1, "compass": 1}
    return usage.get(section, 0) < limits.get(section, 1)

def increment_usage(user_id, section):
    user = get_user(user_id)
    usage = user.get("daily_usage") or {}
    if is_trial_active(user) and not is_paid(user):
        day_key = "trial"
    else:
        day_key = datetime.datetime.now().strftime("%Y-%m-%d")
    today = day_key
    if today not in usage:
        usage = {today: {}}
    usage[today][section] = usage[today].get(section, 0) + 1
    save_user(user_id, daily_usage=usage)

RULES = {
    "ru": "Прежде чем начать, ознакомься с правилами:\n\n1. Анализ не заменяет врача, психолога, юриста или финансового консультанта.\n2. Ответственность за решения остаётся за тобой.\n3. Твои имя и дата рождения сохраняются и используются только для твоего анализа. Имя и дату рождения можно изменить по 1 разу самостоятельно, после этого только через администратора.\n4. Твои данные никому не передаются.\n5. Подписка продлевается автоматически. Отменить можно в любой момент — напиши администратору.",
    "de": "Bevor wir beginnen, bitte lies die Regeln:\n\n1. Die Analyse ersetzt keinen Arzt, Psychologen oder Rechtsberater.\n2. Die Verantwortung für Entscheidungen liegt bei dir.\n3. Dein Name und Geburtsdatum werden gespeichert und nur für deine Analyse verwendet. Name und Geburtsdatum können je einmal selbst geändert werden, danach nur durch den Administrator.\n4. Deine Daten werden nicht weitergegeben.\n5. Das Abonnement verlängert sich automatisch. Du kannst jederzeit kündigen — schreibe dem Administrator.",
    "en": "Before we start, please read the rules:\n\n1. The analysis does not replace a doctor, psychologist or legal advisor.\n2. Responsibility for decisions remains with you.\n3. Your name and date of birth are stored and used only for your analysis. Name and date of birth can each be changed once yourself, after that only through the administrator.\n4. Your data is not shared with anyone.\n5. Subscription renews automatically. You can cancel at any time — contact the administrator."
}

PAYPAL_LINKS = {
    "1m": "https://www.paypal.me/AlexandraEngel42/15EUR",
    "6m": "https://www.paypal.me/AlexandraEngel42/78EUR",
    "12m": "https://www.paypal.me/AlexandraEngel42/159EUR"
}
SEPA = "IBAN: DE28 5002 4024 4782 1216 01\nBank: C24 Bank\nEmpfänger: Alexandra Engel"

MENU_BUTTONS = {
    "ru": [
        [InlineKeyboardButton("💳 Подписка", callback_data="btn_pay")],
        [InlineKeyboardButton("ℹ️ Знакомство с Alvalori", callback_data="btn_info")],
        [InlineKeyboardButton("✍️ Оставить отзыв", callback_data="btn_feedback")],
        [InlineKeyboardButton("👤 Обо мне", callback_data="btn_about")],
        [InlineKeyboardButton("🌟 Основа моей личности", callback_data="btn_me")],
        [InlineKeyboardButton("🧭 Личный год", callback_data="btn_year")],
        [InlineKeyboardButton("📍 Личный месяц", callback_data="btn_month")],
        [InlineKeyboardButton("☀️ Личный день", callback_data="btn_day")],
        [InlineKeyboardButton("💡 Поговорим о главном", callback_data="btn_compass")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="btn_settings")],
    ],
    "de": [
        [InlineKeyboardButton("💳 Abonnement", callback_data="btn_pay")],
        [InlineKeyboardButton("ℹ️ Alvalori kennenlernen", callback_data="btn_info")],
        [InlineKeyboardButton("✍️ Feedback geben", callback_data="btn_feedback")],
        [InlineKeyboardButton("👤 Über mich", callback_data="btn_about")],
        [InlineKeyboardButton("🌟 Meine Persönlichkeit", callback_data="btn_me")],
        [InlineKeyboardButton("🧭 Persönliches Jahr", callback_data="btn_year")],
        [InlineKeyboardButton("📍 Persönlicher Monat", callback_data="btn_month")],
        [InlineKeyboardButton("☀️ Persönlicher Tag", callback_data="btn_day")],
        [InlineKeyboardButton("💡 Lass uns reden", callback_data="btn_compass")],
        [InlineKeyboardButton("⚙️ Einstellungen", callback_data="btn_settings")],
    ],
    "en": [
        [InlineKeyboardButton("💳 Subscription", callback_data="btn_pay")],
        [InlineKeyboardButton("ℹ️ Get to know Alvalori", callback_data="btn_info")],
        [InlineKeyboardButton("✍️ Leave feedback", callback_data="btn_feedback")],
        [InlineKeyboardButton("👤 About me", callback_data="btn_about")],
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
    menu = {"ru": "🏠 Меню", "de": "🏠 Menü", "en": "🏠 Menu"}
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(labels.get(lang, labels["ru"]), callback_data="btn_pay")],
        [InlineKeyboardButton(menu.get(lang, menu["ru"]), callback_data="btn_menu_home")]
    ])

def call_claude(model, max_tokens, system, messages, retries=2):
    import time
    for attempt in range(retries):
        try:
            return client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
        except Exception as e:
            print(f"Claude API error attempt {attempt+1}: {e}", flush=True)
            if attempt < retries - 1:
                time.sleep(3)
            else:
                raise

def clean_text(text):
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
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

def build_profile_context(user, lang="ru"):
    now = datetime.datetime.now()
    m = D.get_model(user["day"])
    py = D.get_year(user["day"], user["month"], now.year)
    pm = D.get_month(py, now.month)
    pd = D.get_day(pm, now.day)
    # Составные цифры личного дня: день+месяц рождения + текущая дата
    day_intermediate, day_result = D.get_day_components(user["day"], user["month"], now)
    mi_raw = D.MODELS.get(m, {})
    def get_field(d, field):
        v = d.get(field, "")
        if isinstance(v, dict):
            return v.get(lang, v.get("ru", ""))
        return v
    mi = {k: get_field(mi_raw, k) for k in ["name", "profile", "strengths", "risks", "chaos", "formula"]} if mi_raw else {}
    def get_text(d):
        if isinstance(d, dict):
            return d.get(lang, d.get("ru", ""))
        return d or ""
    # Тексты для каждой составной цифры
    labels = {
        "ru": ("Итоговая энергия личного дня", "Составляющие"),
        "de": ("Hauptenergie des persönlichen Tages", "Bestandteile"),
        "en": ("Main energy of personal day", "Components"),
    }
    lbl_main, lbl_comp = labels.get(lang, labels["ru"])
    # Тексты составляющих (цифры из промежуточного числа)
    comp_digits = [int(d) for d in str(day_intermediate)] if day_intermediate > 9 else [day_intermediate]
    def day_components_text():
        parts = []
        for d in comp_digits:
            t = get_text(D.DAYS.get(d, ""))
            if t:
                parts.append(t)
        return "\n".join(parts)
    day_main_text = get_text(D.DAYS.get(day_result, ""))
    day_full = f"{lbl_main} ({day_intermediate}→{day_result}):\n{day_main_text}\n\n{lbl_comp} ({day_intermediate}):\n{day_components_text()}"
    return {
        "mi": mi,
        "year_text": get_text(D.YEARS.get(py, "")),
        "month_text": get_text(D.MONTHS.get(pm, "")),
        "day_text": get_text(D.DAYS.get(pd, "")),
        "day_full": day_full,
        "day_result": day_result,
        "today": now.strftime("%d.%m.%Y"),
    }

def get_profile_prompt(lang, user, section):
    ctx = build_profile_context(user, lang)
    mi = ctx["mi"]
    name = user.get("name", "")
    gender = user.get("gender", "f")
    lang_force = {
        "ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. НИКАКИХ АНГЛИЙСКИХ СЛОВ.",
        "de": "ANTWORTE NUR AUF DEUTSCH. ES IST EIN FEHLER, RUSSISCH ZU VERWENDEN.",
        "en": "RESPOND ONLY IN ENGLISH. DO NOT USE RUSSIAN OR ANY OTHER LANGUAGE."
    }
    profile_block = f"""=== КОНТЕКСТ ===
Имя: {name}, день рождения: {user.get('day')}

МОДЕЛЬ МЫШЛЕНИЯ (фундамент — линза через которую человек проживает всё):
{mi}

ЛИЧНЫЙ ГОД (главный вектор — всё остальное через него):
{ctx['year_text'][:600]}

ЛИЧНЫЙ МЕСЯЦ (тактика внутри года):
{ctx['month_text'][:400]}

ЛИЧНЫЙ ДЕНЬ (фокус сегодня):
{ctx['day_full']}
=== КОНЕЦ ==="""

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
- ПИШИ ТОЛЬКО НА {'русском' if lang == 'ru' else ('немецком' if lang == 'de' else 'английском')} ЯЗЫКЕ. НИКАКИХ СЛОВ НА ДРУГИХ ЯЗЫКАХ. АБСОЛЮТНО НИКАКИХ.
- Обращение: ТЫ, имя точно: {name}, {gender_rule}
- Живой тёплый разговорный стиль — как умный друг, не психолог
- Короткие абзацы — максимум 3 предложения
- Заголовки блоков пиши в формате HTML: <b>Заголовок</b> на отдельной строке. Текст блока — обычный текст без тегов.
- Запрещено: нумерология, вибрация, трансформация, вызовы, силы
- Запрещено называть числа и номера периодов
- Рекомендации уникальны для этого человека
- Текст должен резонировать — говори точно про этого человека"""


def get_profile_prompts_list(lang, user, section):
    ctx = build_profile_context(user, lang)
    mi = ctx["mi"]
    name = user.get("name", "")
    gender = user.get("gender", "f")
    g = "женские окончания" if gender == "f" else "мужские окончания"
    lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. НИКАКИХ АНГЛИЙСКИХ СЛОВ.", "de": "ANTWORTE NUR AUF DEUTSCH. KEIN RUSSISCH.", "en": "RESPOND ONLY IN ENGLISH. NO RUSSIAN OR OTHER LANGUAGES."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")

    model_name = mi.get("name", "") if isinstance(mi, dict) else ""
    model_profile = mi.get("profile", "") if isinstance(mi, dict) else str(mi)
    model_strengths = mi.get("strengths", "") if isinstance(mi, dict) else ""
    model_risks = mi.get("risks", "") if isinstance(mi, dict) else ""
    model_chaos = mi.get("chaos", "") if isinstance(mi, dict) else ""
    model_formula = mi.get("formula", "") if isinstance(mi, dict) else ""

    year_text = ctx["year_text"]
    month_text = ctx["month_text"]
    day_text = ctx["day_text"]

    about_parts = []
    if user.get("about_work"): about_parts.append("Работа: " + user["about_work"])
    if user.get("about_finance"): about_parts.append("Финансы: " + user["about_finance"])
    if user.get("about_relations"): about_parts.append("Отношения: " + user["about_relations"])
    if user.get("about_personal"): about_parts.append("Личное: " + user["about_personal"])
    about_block = ("\n\nКОНТЕКСТ ЖИЗНИ:\n" + "\n".join(about_parts) + "\n\nВАЖНО: этот контекст — фон для понимания человека, не повестка дня. Не зацикливайся на проблемах из этого контекста. Используй его чтобы делать анализ точнее и персональнее — но каждый день опирайся прежде всего на энергию дня, месяца и года.") if about_parts else ""

    lang_warning = {"ru": "", "de": "WICHTIG: Die Kontextdaten unten sind auf Russisch — das ist normal. Deine GESAMTE Antwort muss AUF DEUTSCH sein. Kein einziges russisches Wort.", "en": "IMPORTANT: The context data below is in Russian — that is normal. Your ENTIRE response must be IN ENGLISH. Not a single Russian word."}
    system_name = {"ru": "Ты пишешь для системы Внутренний Компас.", "de": "Du schreibst für das System Innerer Kompass.", "en": "You are writing for the Inner Compass system."}
    base_personality = f"""{lf}
{lang_warning.get(lang, "")}
{system_name.get(lang, system_name["ru"])}
Имя: {name}. {g}.

МОДЕЛЬ МЫШЛЕНИЯ:
Название: {model_name}
Профиль: {model_profile}
Сильные стороны: {model_strengths}
Риски: {model_risks}
В хаосе: {model_chaos}
Формула: {model_formula}

ЭТАЛОН СТИЛЯ — пиши именно так:
"Ты из тех людей, рядом с которыми мир будто становится объёмнее. Пока другие привыкают к тому как всё устроено, ты невольно замечаешь где что-то можно сделать легче, живее, человечнее."
"Иногда кажется, будто внутри тебя постоянно живёт тихое движение — желание улучшить, пересобрать, придумать по-новому."
"Ты умеешь замечать слабые места ещё до того как они становятся очевидными для остальных. И дело не в критике — скорее в тонком ощущении того, где что-то перестало быть живым."

ТОНАЛЬНОСТЬ:
- Тёплая, глубокая, уважительная
- Как будто пишет человек который хорошо знает тебя и видит твою суть
- Читатель должен думать "это прям про меня, откуда они знают?"
- Узнавание себя — не диагноз, не оценка
- Каждый абзац открывает что-то новое — без повторов

ЗАПРЕЩЕНО:
- Слова: "трещины", "хаос", "нестабильность", "напряжение", "тормозит", "застреваешь", "зависаешь", "ломается", "проблема", "слабость", "сложно", "тяжело", "беспокойство", "тревога", "диагноз", "критиканство", "занудство", "раздражённость", "изъяны", "несовершенный"
- Всегда обращение на ТЫ — никогда не переходи на третье лицо (не "Александра думает", а "ты думаешь")
- Фразы: "ты склонна к", "твоя проблема", "это мешает тебе", "ты испытываешь"
- Клише: "суперсила", "рождена чтобы", "внутренний огонь", "навигационный компас"
- Упоминать год, месяц, день
- Markdown звёздочки решётки
- Вопросы в конце текста — это анализ, не диалог

Обращение: ТЫ. {g}.
ОТВЕЧАЙ ТОЛЬКО НА {"русском" if lang == "ru" else ("немецком" if lang == "de" else "английском")} ЯЗЫКЕ.{about_block}"""

    base_period = f"""{lf}
{lang_warning.get(lang, "")}
{system_name.get(lang, system_name["ru"])}
Имя: {name}. {g}.

МОДЕЛЬ МЫШЛЕНИЯ: {model_name}
Профиль: {model_profile}
Сильные стороны: {model_strengths}
Риски: {model_risks}

ЛИЧНЫЙ ГОД — главный вектор:
{year_text}

ЛИЧНЫЙ МЕСЯЦ — тактика внутри года:
{month_text}

ЛИЧНЫЙ ДЕНЬ:
{day_text}

СТИЛЬ — тёплый, глубокий, живой. Пиши связными абзацами — не списками и не отдельными короткими строками. Каждый абзац 2-3 предложения.

ЗАПРЕЩЕНО:
- Слова: трещины, штукатурить, изъяны, перепады, нестабильность, тормозит, перегрузка
- Списки из одного предложения — только связные абзацы
- Упоминание чисел периодов
- Markdown звёздочки решётки
- Обрезанные и неполные формы слов: "зафикси" вместо "зафиксируй", "форсажа" и подобные
- Разговорные сокращения глаголов в повелительном наклонении
- Вопросы в конце текста — это анализ, не диалог
Обращение: ТЫ. {g}.
ОТВЕЧАЙ ТОЛЬКО НА {"русском" if lang == "ru" else ("немецком" if lang == "de" else "английском")} ЯЗЫКЕ.{about_block}"""

    birth_day = user.get("day", "")
    birth_month = user.get("month", "")

    base_year = f"""{lf}
{lang_warning.get(lang, "")}
{system_name.get(lang, system_name["ru"])}
Имя: {name}. {g}.

МОДЕЛЬ МЫШЛЕНИЯ: {model_name}
Профиль: {model_profile}
Сильные стороны: {model_strengths}
Риски: {model_risks}

ЛИЧНЫЙ ГОД — главный вектор:
{year_text}

СТИЛЬ — тёплый, глубокий, живой. Пиши связными абзацами — не списками и не отдельными короткими строками. Каждый абзац 2-3 предложения.

ЗАПРЕЩЕНО:
- Слова: трещины, штукатурить, изъяны, перепады, нестабильность, тормозит, перегрузка
- Списки из одного предложения — только связные абзацы
- Упоминание чисел периодов
- Markdown звёздочки решётки
- Обрезанные и неполные формы слов
- Упоминание личного месяца и личного дня в анализе года
- Вопросы в конце текста — это анализ, не диалог
Обращение: ТЫ. {g}.
ОТВЕЧАЙ ТОЛЬКО НА {"русском" if lang == "ru" else ("немецком" if lang == "de" else "английском")} ЯЗЫКЕ.{about_block}"""

    base_month = f"""{lf}
{lang_warning.get(lang, "")}
{system_name.get(lang, system_name["ru"])}
Имя: {name}. {g}.

МОДЕЛЬ МЫШЛЕНИЯ: {model_name}
Профиль: {model_profile}
Сильные стороны: {model_strengths}
Риски: {model_risks}

ЛИЧНЫЙ ГОД — главный вектор:
{year_text}

ЛИЧНЫЙ МЕСЯЦ — тактика внутри года:
{month_text}

СТИЛЬ — тёплый, глубокий, живой. Пиши связными абзацами — не списками и не отдельными короткими строками. Каждый абзац 2-3 предложения.

ЗАПРЕЩЕНО:
- Слова: трещины, штукатурить, изъяны, перепады, нестабильность, тормозит, перегрузка
- Списки из одного предложения — только связные абзацы
- Упоминание чисел периодов
- Markdown звёздочки решётки
- Обрезанные и неполные формы слов
- Упоминание конкретного дня
- Вопросы в конце текста — это анализ, не диалог
Обращение: ТЫ. {g}.
ОТВЕЧАЙ ТОЛЬКО НА {"русском" if lang == "ru" else ("немецком" if lang == "de" else "английском")} ЯЗЫКЕ.{about_block}"""

    b = {
        "me": [
            base_personality + f"""

Напиши ТОЛЬКО этот блок: {"<b>✨ Кто ты есть на самом деле</b>" if lang=="ru" else ("<b>✨ Wer du wirklich bist</b>" if lang=="de" else "<b>✨ Who You Really Are</b>")}
Начни с этого тега на первой строке.
3 абзаца. Только о личности {name} — без года, месяца, дня.

ПРИМЕР ПРАВИЛЬНОГО СТИЛЯ (адаптируй под модель мышления {name}, не копируй):
"Ты из тех людей, рядом с которыми мир будто становится объёмнее. Пока другие привыкают к тому как всё устроено, ты невольно замечаешь где что-то можно сделать легче, живее, человечнее. Это не про недовольство — просто твой взгляд устроен так, что ты видишь не только то что есть сейчас, но и то каким всё могло бы быть."
"Иногда кажется будто внутри тебя постоянно живёт тихое движение — желание улучшить, пересобрать, придумать по-новому. Идеи приходят не через долгие размышления, а почти мгновенно. Как вспышка."

Первый абзац — точное наблюдение о том как {name} видит мир. Начни не с "ты" а с образа.
Второй абзац — как это проявляется изнутри, что происходит когда её природа работает.
Третий абзац — обратная сторона этого же качества без осуждения.""",

            base_personality + f"""

Напиши ТОЛЬКО этот блок: {"<b>🌿 Твои настоящие сильные стороны</b>" if lang=="ru" else ("<b>🌿 Deine wahren Stärken</b>" if lang=="de" else "<b>🌿 Your True Strengths</b>")}
Начни с этого тега на первой строке.
3-4 абзаца. Только о сильных сторонах личности {name} — без года и дня.
Покажи каждую силу через конкретную ситуацию или образ — не абстрактно.
Пример хорошего стиля: "Идеи у тебя не приходят как напряжённый поиск — они вспыхивают. Часто в самый неподходящий момент. И почти всегда работают, потому что строятся не на теории, а на твоём ощущении что именно здесь что-то не так."
Каждый абзац — отдельная сила, показанная в действии.""",

            base_personality + f"""

Напиши ТОЛЬКО этот блок: {"<b>🔺 Где ты можешь себе мешать</b>" if lang=="ru" else ("<b>🔺 Wo du dir selbst im Weg stehst</b>" if lang=="de" else "<b>🔺 Where You Can Block Yourself</b>")}
Начни с этого тега на первой строке.
3 абзаца. Только о личностных паттернах — без года и дня.
Покажи механизм — не "ты делаешь X" а "вот что происходит внутри когда..."
Пример хорошего стиля: "Та же критичность которая помогает видеть лучшее решение — легко переворачивается против своего же проекта. Когда первый импульс проходит, ты начинаешь замечать несовершенства. И вместо того чтобы довести до конца — хочется переделать с нуля."
Тепло, с пониманием, без осуждения.""",

            base_personality + f"""

Напиши ТОЛЬКО этот блок: {"<b>🌱 Как превратить это в силу</b>" if lang=="ru" else ("<b>🌱 Wie du das in Stärke verwandelst</b>" if lang=="de" else "<b>🌱 How to Turn This Into Strength</b>")}
Начни с этого тега на первой строке.
2-3 абзаца. Только о личности — без года и дня.
Покажи как именно {name} с её конкретной моделью мышления может превратить свои слабые стороны в сильные.
Не общие советы — а конкретный механизм для этого типа личности.
Пример: "Твоя критичность работает лучше всего когда направлена на процесс а не на результат. Вместо вопроса 'достаточно ли это хорошо?' — вопрос 'что именно здесь можно улучшить прямо сейчас, не останавливая движение?'"
Заканчивай на ощущении возможности, не обязанности.""",
        ],
        "year": [
            base_year + f"""

Напиши ТОЛЬКО этот блок: <b>🧭 Что происходит в этом году</b>
Начни с этого тега на первой строке.
Говори ТОЛЬКО о годе — без упоминания месяца и дня.
Покажи суть этого года через призму модели мышления {name} — как эти два вектора встречаются.
Тон: как умный близкий друг который хорошо знает тебя — тепло, точно, без поучений.
НЕ говори что человек "должен" или что его природа "работает против него".
Два-три связных абзаца.""",

            base_year + f"""

Напиши ТОЛЬКО этот блок: {"<b>✨ Где ты сейчас сильнее всего</b>" if lang=="ru" else ("<b>✨ Wo du jetzt am stärksten bist</b>" if lang=="de" else "<b>✨ Where You Are Strongest Right Now</b>")}
Начни с этого тега на первой строке.
Говори ТОЛЬКО о годе — без упоминания месяца и дня.
Что реально открывается для {name} в этот год — конкретные возможности через её сильные стороны.
Тон: вдохновляющий но реалистичный, без клише и общих слов.
Два-три связных абзаца.""",

            base_year + f"""

Напиши ТОЛЬКО этот блок: {"<b>🔺 На что обратить внимание</b>" if lang=="ru" else ("<b>🔺 Worauf du achten solltest</b>" if lang=="de" else "<b>🔺 What to Watch Out For</b>")}
Начни с этого тега на первой строке.
Говори ТОЛЬКО о годе — без упоминания месяца и дня.
Покажи где {name} с её моделью мышления может незаметно для себя создать сложности в этот год.
Тон: мягко, с пониманием, без "ты делаешь неправильно" — как наблюдение а не оценка.
Два-три связных абзаца.""",

            base_year + f"""

Напиши ТОЛЬКО этот блок: {"<b>🌱 Как использовать этот год</b>" if lang=="ru" else ("<b>🌱 Wie du dieses Jahr nutzt</b>" if lang=="de" else "<b>🌱 How to Use This Year</b>")}
Начни с этого тега на первой строке.
Говори ТОЛЬКО о годе — без упоминания месяца и дня.
2-3 конкретных подхода которые помогут {name} с её моделью мышления прожить этот год эффективно.
Не инструкции — а то что резонирует именно с этим человеком.
Заканчивай на ощущении возможности.""",
        ],
        "month": [
            base_month + f"""

Напиши ТОЛЬКО этот блок: {"<b>🧭 Тема этого месяца</b>" if lang=="ru" else ("<b>🧭 Thema dieses Monats</b>" if lang=="de" else "<b>🧭 This Month's Theme</b>")}
Начни с этого тега на первой строке.
Говори ТОЛЬКО о месяце — без упоминания конкретного дня.
Покажи как тема месяца раскрывается через модель мышления {name} и вектор года.
Тон: как умный близкий друг — тепло, точно, без поучений и без "ты должна".
Два-три связных абзаца.""",

            base_month + f"""

Напиши ТОЛЬКО этот блок: {"<b>✨ Что открывается в этом месяце</b>" if lang=="ru" else ("<b>✨ Was sich diesen Monat öffnet</b>" if lang=="de" else "<b>✨ What Opens Up This Month</b>")}
Начни с этого тега на первой строке.
Говори ТОЛЬКО о месяце — без упоминания конкретного дня.
Конкретные возможности для {name} в этом месяце через её сильные стороны.
Тон: вдохновляющий но реалистичный, без общих слов.
Два-три связных абзаца.""",

            base_month + f"""

Напиши ТОЛЬКО этот блок: {"<b>🔺 На что обратить внимание</b>" if lang=="ru" else ("<b>🔺 Worauf du achten solltest</b>" if lang=="de" else "<b>🔺 What to Watch Out For</b>")}
Начни с этого тега на первой строке.
Говори ТОЛЬКО о месяце — без упоминания конкретного дня.
Где {name} с её моделью мышления может незаметно создать сложности в этом месяце.
Тон: мягко, как наблюдение а не оценка. Два-три связных абзаца.""",

            base_month + f"""

Напиши ТОЛЬКО этот блок: {"<b>🌱 Фокус на ближайшие 2 недели</b>" if lang=="ru" else ("<b>🌱 Fokus für die nächsten 2 Wochen</b>" if lang=="de" else "<b>🌱 Focus for the Next 2 Weeks</b>")}
Начни с этого тега на первой строке.
Говори ТОЛЬКО о месяце — без упоминания конкретного дня.
2-3 конкретных подхода которые помогут {name} прожить этот месяц эффективно.
Не инструкции — а то что резонирует именно с этим человеком.""",
        ],
        "day": [
            base_period + f"""

Напиши ТОЛЬКО этот блок: {"<b>🧭 Энергия сегодняшнего дня</b>" if lang=="ru" else ("<b>🧭 Energie des heutigen Tages</b>" if lang=="de" else "<b>🧭 Energy of Today</b>")}
Начни с этого тега на первой строке.
Покажи как энергия сегодняшнего дня взаимодействует с моделью мышления {name} и текущим периодом.
Тон: как умный близкий друг — тепло, точно, без поучений.
Один-два связных абзаца.""",

            base_period + f"""

Напиши ТОЛЬКО этот блок: {"<b>🌿 Как использовать сегодняшний день</b>" if lang=="ru" else ("<b>🌿 Wie du den heutigen Tag nutzt</b>" if lang=="de" else "<b>🌿 How to Use Today</b>")}
Начни с этого тега на первой строке.
2-3 конкретных подхода для {name} сегодня — именно для её модели мышления и текущего периода.
Не общие советы — а то что резонирует с этим человеком прямо сейчас.
Тон: тепло, без поучений и без "ты должна".""",

            base_period + f"""

Напиши ТОЛЬКО этот блок: {"<b>🔺 На что обратить внимание сегодня</b>" if lang=="ru" else ("<b>🔺 Worauf du heute achten solltest</b>" if lang=="de" else "<b>🔺 What to Watch Out for Today</b>")}
Начни с этого тега на первой строке.
2-3 наблюдения — где {name} с её моделью мышления может незаметно создать сложности именно сегодня.
Тон: мягко, как наблюдение а не предупреждение.""",

            base_period + f"""

Напиши ТОЛЬКО этот блок: {"<b>🌱 Главный фокус дня</b>" if lang=="ru" else ("<b>🌱 Hauptfokus des Tages</b>" if lang=="de" else "<b>🌱 Main Focus of the Day</b>")}
Начни с этого тега на первой строке.
Одна конкретная вещь которая даст {name} наибольший результат именно сегодня.
Коротко и ясно — один абзац.""",
        ],
    }
    return b.get(section, b["me"])


def get_free_prompt(lang, user, section):
    ctx = build_profile_context(user, lang)
    mi = ctx["mi"]
    name = user.get("name", "")
    gender = user.get("gender", "f")
    gender_rule = "женские окончания" if gender == "f" else "мужские окончания"
    lang_force = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. НИКАКИХ АНГЛИЙСКИХ СЛОВ.", "de": "ANTWORTE NUR AUF DEUTSCH. KEIN RUSSISCH. KEINE RUSSISCHEN WOERTER.", "en": "RESPOND ONLY IN ENGLISH. NO RUSSIAN OR OTHER LANGUAGES. ENGLISH ONLY."}
    if lang == "de":
        hints = {
            "me": f"Schreibe {name} ein warmes und genaues Persönlichkeitsportrait — 6-8 Sätze. Zeige die Hauptstärke und wie sie sich im Leben zeigt. Letzter Satz — eine offene Frage die zum Nachdenken einlädt.",
            "year": f"Schreibe {name} über das Hauptthema dieses Jahres — 6-8 Sätze. Konkret, ohne allgemeine Worte. Letzter Satz — ein Hinweis dass das vollständige Bild viel tiefer geht.",
            "month": f"Schreibe {name} über die Taktik dieses Monats — 5-6 Sätze. Ein konkreter Rat was jetzt zu tun ist.",
            "day": f"Schreibe {name} über die Energie des heutigen Tages — 4-5 Sätze. Ein sehr konkreter Rat für heute."
        }
    elif lang == "en":
        hints = {
            "me": f"Write {name} a warm and precise personality portrait — 6-8 sentences. Show the main strength and how it shows in life. Last sentence — an open question that invites reflection.",
            "year": f"Write {name} about the main theme of this year — 6-8 sentences. Concrete, no general words. Last sentence — a hint that the full picture goes much deeper.",
            "month": f"Write {name} about the tactics of this month — 5-6 sentences. One concrete advice on what to do right now.",
            "day": f"Write {name} about the energy of today — 4-5 sentences. One very concrete advice for today."
        }
    else:
        hints = {
            "me": f"Напиши {name} тёплый и точный портрет личности — 6-8 предложений. Покажи главную силу и как она проявляется в жизни. Без оценок, без слов 'тяжесть', 'трудно', 'противоречие', 'насилие'. Последнее предложение — живой вопрос который приглашает к размышлению.",
            "year": f"Напиши {name} о главной теме и задаче этого года — 6-8 предложений. Конкретно, без общих слов. Последнее предложение — намёк что полная картина гораздо глубже.",
            "month": f"Напиши {name} о тактике и фокусе этого месяца — 5-6 предложений. Один конкретный совет что делать прямо сейчас. Закончи намёком на то что упускается без полного анализа.",
            "day": f"Напиши {name} об энергии сегодняшнего дня — 4-5 предложений. Один очень конкретный совет на сегодня. Тон тёплый, как от друга который тебя хорошо знает."
        }
    return f"""Ты ассистент системы Внутренний Компас. Сегодня {ctx['today']}.
{lang_force.get(lang, lang_force["ru"])}
Имя: {name}.
Модель мышления:
{mi}.
Личный год: {ctx['year_text'][:120]}
Личный месяц: {ctx['month_text'][:120]}
{hints.get(section, hints['me'])}
ПРАВИЛА: ТЫ, {gender_rule}, живой стиль, чистый текст, без markdown, без нумерологии. Обращайся по имени {name}."""

def get_system_prompt(lang, user):
    name = user.get("name", "") if user else ""
    gender = user.get("gender", "f") if user else "f"
    gender_rule = "женские окончания: умная, сильная, готова" if gender == "f" else "мужские окончания: умный, сильный, готов"
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    lang_force = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. НИКАКИХ АНГЛИЙСКИХ СЛОВ.", "de": "ANTWORTE NUR AUF DEUTSCH. KEIN RUSSISCH.", "en": "RESPOND ONLY IN ENGLISH. NO RUSSIAN OR OTHER LANGUAGES."}
    profile_block = ""
    if user and user.get("day"):
        ctx = build_profile_context(user, lang)
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
ПИШИ ТОЛЬКО НА {'русском' if lang == 'ru' else ('немецком' if lang == 'de' else 'английском')} ЯЗЫКЕ. НИКАКИХ СЛОВ НА ДРУГИХ ЯЗЫКАХ."""

def get_universal_day(date):
    """Вычисляет общий день для даты. Возвращает (итог, промежуточное, has_zero)"""
    digits = [int(d) for d in date.strftime('%d%m%Y')]
    total = sum(digits)
    intermediate = total  # например 21
    while total > 9:
        total = sum(int(d) for d in str(total))
    has_zero = '0' in date.strftime('%d')  # 10, 20, 30
    return total, intermediate, has_zero

UNIVERSAL_DAY_TIPS = {
    1: {
        "ru": "Сегодня энергия поддерживает первые шаги и личные инициативы — если есть что-то что давно откладываешь, именно сегодня стоит сдвинуть с места. Финансовые и юридические вопросы лучше оставить на другой день.",
        "de": "Heute unterstützt die Energie erste Schritte und persönliche Initiativen — wenn du etwas lange aufgeschoben hast, ist heute der richtige Moment. Finanz- und Rechtsfragen besser auf einen anderen Tag verschieben.",
        "en": "Today's energy supports first steps and personal initiatives — if there's something you've been putting off, today is the right moment to start. Financial and legal matters are better left for another day."
    },
    2: {
        "ru": "Сегодня особенно хорошо идут разговоры где важно найти общий язык — переговоры, деловые встречи, личные договорённости. Избегай давления и ультиматумов: компромисс сейчас принесёт больше чем жёсткая позиция.",
        "de": "Heute laufen Gespräche besonders gut, bei denen es darum geht, eine gemeinsame Basis zu finden — Verhandlungen, Geschäftstreffen, persönliche Vereinbarungen. Druck und Ultimaten vermeiden: Kompromisse bringen jetzt mehr als eine harte Haltung.",
        "en": "Today conversations flow especially well when the goal is finding common ground — negotiations, business meetings, personal agreements. Avoid pressure and ultimatums: compromise will bring more today than a rigid position."
    },
    3: {
        "ru": "День активного общения и новых контактов — идеи которые сегодня возникнут в разговоре могут оказаться неожиданно ценными. Не торопись принимать решения на основе только что услышанного: сначала переспи с этим.",
        "de": "Tag aktiver Kommunikation und neuer Kontakte — Ideen die heute im Gespräch entstehen können sich als überraschend wertvoll erweisen. Keine Entscheidungen auf Basis von gerade Gehörtem überstürzen: erst eine Nacht darüber schlafen.",
        "en": "A day of active communication and new contacts — ideas that come up in conversation today may turn out to be surprisingly valuable. Don't rush to make decisions based on what you just heard: sleep on it first."
    },
    4: {
        "ru": "Сегодня хорошо идёт работа с деталями — проверка документов, исправление ошибок, доработка того что уже начато. Запускать что-то новое сейчас не лучшая идея: энергия дня работает на углубление а не на расширение.",
        "de": "Heute läuft die Arbeit mit Details gut — Dokumentenprüfung, Fehlerbehebung, Weiterentwicklung von bereits Begonnenem. Jetzt etwas Neues zu starten ist keine gute Idee: die Energie des Tages arbeitet auf Vertiefung nicht auf Erweiterung.",
        "en": "Today is good for working with details — checking documents, fixing errors, refining what's already started. Launching something new is not ideal right now: today's energy works toward deepening rather than expanding."
    },
    5: {
        "ru": "Сегодня хорошо работает всё что связано с общением, продвижением и новыми знакомствами. Финансовые решения принятые на импульсе сегодня могут оказаться невыгодными — лучше отложить до завтра.",
        "de": "Heute funktioniert alles gut was mit Kommunikation, Werbung und neuen Bekanntschaften zu tun hat. Finanzentscheidungen die heute impulsiv getroffen werden könnten sich als unvorteilhaft erweisen — besser bis morgen warten.",
        "en": "Today works well for everything related to communication, promotion and new connections. Financial decisions made impulsively today may turn out to be disadvantageous — better to wait until tomorrow."
    },
    6: {
        "ru": "День когда стоит уделить внимание тому что уже создано — улучшить, довести до порядка, позаботиться о близких. Крупные сделки и серьёзные финансовые шаги сегодня лучше не форсировать.",
        "de": "Ein Tag um dem Aufmerksamkeit zu schenken was bereits geschaffen wurde — verbessern, in Ordnung bringen, sich um Nahestehende kümmern. Große Geschäfte und ernsthafte Finanzschritte heute besser nicht forcieren.",
        "en": "A day to pay attention to what's already been created — improve it, put it in order, take care of those close to you. Major deals and serious financial steps are better not forced today."
    },
    7: {
        "ru": "Сегодня хорошо идут размышления, анализ и работа в одиночестве. Если нужно подписать важный документ или принять серьёзное решение — не торопись, перечитай ещё раз и проверь детали.",
        "de": "Heute laufen Überlegungen, Analysen und Alleinarbeit gut. Wenn du ein wichtiges Dokument unterschreiben oder eine ernsthafte Entscheidung treffen musst — nicht überstürzen, noch einmal lesen und Details prüfen.",
        "en": "Today is good for reflection, analysis and working alone. If you need to sign an important document or make a serious decision — don't rush, read it again and check the details."
    },
    8: {
        "ru": "Один из самых сильных дней для финансовых шагов, переговоров о деньгах и закрытия важных договорённостей. Используй эту энергию осознанно — то что подписано или решено сегодня будет иметь вес.",
        "de": "Einer der stärksten Tage für finanzielle Schritte, Geldverhandlungen und den Abschluss wichtiger Vereinbarungen. Diese Energie bewusst nutzen — was heute unterzeichnet oder entschieden wird hat Gewicht.",
        "en": "One of the strongest days for financial steps, money negotiations and closing important agreements. Use this energy consciously — what is signed or decided today will carry weight."
    },
    9: {
        "ru": "Сегодня хорошо завершать — дела, разговоры, договорённости которые давно висят незакрытыми. Начинать что-то новое сейчас не лучшее время: день работает на закрытие а не на открытие.",
        "de": "Heute ist gut zum Abschließen — Dinge, Gespräche, Vereinbarungen die lange offen geblieben sind. Jetzt etwas Neues zu beginnen ist keine gute Zeit: der Tag arbeitet auf Schließung nicht auf Öffnung.",
        "en": "Today is good for completing — tasks, conversations, agreements that have been left open for a long time. Starting something new now is not the best time: the day works toward closing not opening."
    },
    0: {
        "ru": "Важные решения, подписание документов и финансовые операции рекомендуется принимать после дополнительной проверки обстоятельств или перенести на другой день.",
        "de": "Wichtige Entscheidungen, Dokumentenunterzeichnung und Finanztransaktionen sollten nach zusätzlicher Prüfung oder auf einen anderen Tag verschoben werden.",
        "en": "Important decisions, document signing and financial transactions should be made after additional verification or postponed to another day."
    }
}

def log_action(user_id, section, action, lang="ru"):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT INTO analytics (user_id, section, action, lang) VALUES (%s, %s, %s, %s)",
                    (user_id, section, action, lang))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Analytics error: {e}", flush=True)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = []
    compass_state.pop(user_id, None)
    save_session(user_id, session=[], compass={})
    # Сохраняем реферера если есть
    if context.args:
        arg = context.args[0]
        if arg.startswith("refid_"):
            try:
                ref_id = int(arg[6:])
                if ref_id != user_id:
                    save_user(user_id, referred_by=ref_id)
            except:
                pass
        elif arg.startswith("ref_"):
            ref_name = arg[4:]
            try:
                import urllib.parse
                ref_name = urllib.parse.unquote(ref_name)
                conn = get_db()
                cur = conn.cursor()
                cur.execute("SELECT user_id FROM users WHERE name ILIKE %s LIMIT 1", (ref_name,))
                row = cur.fetchone()
                cur.close()
                conn.close()
                if row and row[0] != user_id:
                    save_user(user_id, referred_by=row[0])
            except:
                pass
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("day"):
        lang = user.get("lang", "ru")
        await update.message.reply_text(
            {"ru": "Выбери раздел:", "de": "Wähle einen Bereich:", "en": "Choose a section:"}.get(lang, "Выбери раздел:"),
            reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"]))
        )
        return
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
    # Не сбрасываем сессию если идёт активный диалог
    if user_id not in compass_state:
        user_sessions[user_id] = []
    save_session(user_id, session=[], compass={})
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("day"):
        await query.edit_message_text(
            {"ru": "Выбери раздел:", "de": "Wähle einen Bereich:", "en": "Choose a section:"}.get(lang, "Выбери раздел:"),
            reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"]))
        )
        return
    age_msg = {
        "ru": "Мне уже 18 лет?",
        "de": "Bin ich bereits 18 Jahre alt?",
        "en": "Am I already 18 years old?"
    }
    age_btns = [
        [InlineKeyboardButton("✅ Да" if lang=="ru" else ("✅ Ja" if lang=="de" else "✅ Yes"), callback_data="age_yes")],
        [InlineKeyboardButton("❌ Нет" if lang=="ru" else ("❌ Nein" if lang=="de" else "❌ No"), callback_data="age_no")],
    ]
    await query.edit_message_text(age_msg.get(lang, age_msg["ru"]), reply_markup=InlineKeyboardMarkup(age_btns))

async def age_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if query.data == "age_yes":
        agree_btns = [
            [InlineKeyboardButton("✅ Принимаю" if lang=="ru" else ("✅ Ich stimme zu" if lang=="de" else "✅ I agree"), callback_data="agree")],
            [InlineKeyboardButton("❌ Не принимаю" if lang=="ru" else ("❌ Ich lehne ab" if lang=="de" else "❌ I decline"), callback_data="disagree")],
        ]
        await query.edit_message_text(RULES[lang], reply_markup=InlineKeyboardMarkup(agree_btns))
    else:
        # Несовершеннолетний — показываем правила с пометкой minor
        save_user(user_id, lang=lang, is_minor=True)
        agree_btns = [
            [InlineKeyboardButton("✅ Принимаю" if lang=="ru" else ("✅ Ich stimme zu" if lang=="de" else "✅ I agree"), callback_data="agree_minor")],
            [InlineKeyboardButton("❌ Не принимаю" if lang=="ru" else ("❌ Ich lehne ab" if lang=="de" else "❌ I decline"), callback_data="disagree")],
        ]
        await query.edit_message_text(RULES[lang], reply_markup=InlineKeyboardMarkup(agree_btns))

async def agree_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if query.data == "agree_minor":
        save_user(user_id, agreed=True, is_minor=True)
        minor_msg = {
            "ru": "Привет! Это Alvalori. Как тебя зовут?",
            "de": "Hallo! Das ist Alvalori. Wie heißt du?",
            "en": "Hi! This is Alvalori. What is your name?"
        }
        msg = minor_msg.get(lang, minor_msg["ru"])
        user_sessions[user_id] = [{"role": "assistant", "content": msg}]
        await query.edit_message_text(msg)
        return

    if query.data == "disagree":
        texts = {"ru": "Понятно. Если передумаешь — напиши /start.", "de": "Okay. Wenn du es dir anders überlegst — schreibe /start.", "en": "Okay. If you change your mind — write /start."}
        await query.edit_message_text(texts.get(lang, texts["ru"]))
        return
    save_user(user_id, agreed=True)
    trial_msg = {
        "ru": "🌟 У тебя есть <b>72 часа бесплатного доступа</b> ко всем разделам.\n\nПосле этого доступ можно продлить по подписке от 15€/месяц.",
        "de": "🌟 Du hast <b>72 Stunden kostenlosen Zugang</b> zu allen Bereichen.\n\nDanach kannst du den Zugang ab 15€/Monat verlängern.",
        "en": "🌟 You have <b>72 hours of free access</b> to all sections.\n\nAfterwards you can continue with a subscription from 15€/month."
    }
    trial_btns = InlineKeyboardMarkup([
        [InlineKeyboardButton("🆓 Продолжить бесплатно" if lang=="ru" else ("🆓 Kostenlos weitermachen" if lang=="de" else "🆓 Continue for free"), callback_data="trial_start")],
        [InlineKeyboardButton("💳 Купить сразу" if lang=="ru" else ("💳 Jetzt kaufen" if lang=="de" else "💳 Buy now"), callback_data="btn_pay")],
    ])
    await query.edit_message_text(trial_msg.get(lang, trial_msg["ru"]), reply_markup=trial_btns, parse_mode="HTML")

async def show_menu(context, user_id, lang, text=None):
    texts = {"ru": "Выбери раздел:", "de": "Wähle einen Bereich:", "en": "Choose a section:"}
    await context.bot.send_message(user_id, text or texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"])))

async def menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    data = query.data

    no_date_msg = {"ru": "Сначала введи дату рождения — напиши /start", "de": "Bitte gib dein Geburtsdatum ein — schreibe /start", "en": "Please enter your birthdate — write /start"}
    upsell_msg = {"ru": "Это лишь начало. Полный анализ — в подписке.", "de": "Das ist nur der Anfang. Vollständige Analyse mit Abonnement.", "en": "This is just the beginning. Full analysis with subscription."}
    limit_msg = {"ru": "Этот раздел уже открыт сегодня — загляни завтра 🌙", "de": "Diesen Bereich hast du heute schon geöffnet — schau morgen wieder rein 🌙", "en": "You've already opened this section today — come back tomorrow 🌙"}

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
        if is_paid(user):
            log_action(user_id, section, "paid_open", lang)
            prompts = get_profile_prompts_list(lang, user, section)
            tokens = {"me": 1500, "year": 1500, "month": 1200, "day": 1000}
            for p in prompts:
                try:
                    import asyncio
                    resp = await asyncio.to_thread(lambda: client.messages.create(model="claude-sonnet-4-6", max_tokens=tokens.get(section, 1200), system=p, messages=[{"role": "user", "content": "Напиши"}]))
                    txt = clean_text(resp.content[0].text)
                    if txt:
                        await context.bot.send_message(user_id, txt, parse_mode="HTML")
                except Exception as e:
                    import traceback
                    print(f"Paid analysis error: {e}", flush=True)
                    traceback.print_exc()
            await show_menu(context, user_id, lang)
        else:
            # Триал: каждый раздел 1 раз за весь триал, полное качество
            if not check_free_limit(user_id, section):
                limit_trial_msg = {
                    "ru": "Этот раздел ты уже открывала в пробный период 🌙\nОткрой полный доступ чтобы пользоваться без ограничений.",
                    "de": "Diesen Bereich hast du bereits im Testzeitraum geöffnet 🌙\nSchalte den vollen Zugang frei.",
                    "en": "You've already opened this section during your trial 🌙\nUnlock full access to use without limits."
                }
                await query.edit_message_text(limit_trial_msg.get(lang, limit_trial_msg["ru"]), reply_markup=get_upgrade_keyboard(lang))
                return
            increment_usage(user_id, section)
            log_action(user_id, section, "trial_open", lang)
            prompts = get_profile_prompts_list(lang, user, section)
            tokens = {"me": 3000, "year": 3000, "month": 2500, "day": 2000}
            for p in prompts:
                try:
                    import asyncio
                    resp = await asyncio.to_thread(lambda: client.messages.create(model="claude-sonnet-4-6", max_tokens=tokens.get(section, 2000), system=p, messages=[{"role": "user", "content": "Напиши"}]))
                    txt = clean_text(resp.content[0].text)
                    await context.bot.send_message(user_id, txt, parse_mode="HTML")
                except Exception as e:
                    print(f"Trial error: {e}", flush=True)
            await show_menu(context, user_id, lang)

    elif data == "btn_compass":
        log_action(user_id, "compass", "open", lang)
        if not user or not user.get("day"):
            await query.edit_message_text(no_date_msg.get(lang, no_date_msg["ru"]))
            return
        if not has_access(user):
            had_trial = user.get("trial_started_at") is not None
            if had_trial:
                expired_msg = {
                    "ru": "✨ Твои 72 часа бесплатного доступа завершились.\n\nЕсли было полезно — можешь продолжить. Полный доступ открывает все разделы без ограничений.",
                    "de": "✨ Deine 72 Stunden kostenlosen Zugangs sind abgelaufen.\n\nWenn es hilfreich war — du kannst weitermachen. Voller Zugang öffnet alle Bereiche ohne Einschränkungen.",
                    "en": "✨ Your 72 hours of free access have ended.\n\nIf it was helpful — you can continue. Full access opens all sections without limits."
                }
            else:
                expired_msg = {
                    "ru": "Этот раздел доступен по подписке.",
                    "de": "Dieser Bereich ist nur mit Abonnement verfügbar.",
                    "en": "This section requires a subscription."
                }
            await query.edit_message_text(
                expired_msg.get(lang, expired_msg["ru"]),
                reply_markup=get_upgrade_keyboard(lang)
            )
            return
        is_trial = is_trial_active(user) and not is_paid(user)
        is_trial = is_trial_active(user) and not is_paid(user)
        if is_trial:
            if get_daily_usage(user_id).get("compass", 0) >= 1:
                limit_trial_msg = {
                    "ru": "Компас ты уже открывала в пробный период 🌙\nОткрой полный доступ чтобы пользоваться без ограничений.",
                    "de": "Den Kompass hast du bereits im Testzeitraum genutzt 🌙\nSchalte den vollen Zugang frei.",
                    "en": "You've already used the Compass during your trial 🌙\nUnlock full access to use without limits."
                }
                await query.edit_message_text(limit_trial_msg.get(lang, limit_trial_msg["ru"]), reply_markup=get_upgrade_keyboard(lang))
                return
            increment_usage(user_id, "compass")
        compass_state[user_id] = {"stage": "initial", "q_count": 0, "clarify_count": 0, "topic": "", "trial": is_trial}
        user_sessions[user_id] = []
        save_session(user_id, session=[], compass=compass_state[user_id])
        start_q = {
            "ru": "Расскажи — что сейчас занимает твои мысли больше всего? Какую ситуацию или вопрос ты хочешь прояснить?",
            "de": "Erzähl mir — was beschaeftigt dich gerade am meisten? Welche Situation oder Frage moechtest du klären?",
            "en": "Tell me — what's been on your mind the most lately? What situation or question would you like to clarify?"
        }
        msg = start_q.get(lang, start_q["ru"])
        user_sessions[user_id].append({"role": "assistant", "content": msg})
        await query.edit_message_text(msg)


    elif data == "about_skip_step":
        state = compass_state.get(user_id, {})
        about_step = state.get("about_step", "work")
        if about_step == "work":
            state["about_step"] = "finance"
            compass_state[user_id] = state
            save_session(user_id, session=user_sessions.get(user_id, []), compass=state)
            q = {"ru": "💰 Финансы\n\nКак выглядит твоя финансовая ситуация сейчас — что стабильно, что беспокоит, к чему стремишься?", "de": "💰 Finanzen\n\nWie sieht deine finanzielle Situation gerade aus — was ist stabil, was bereitet dir Sorgen, wonach strebst du?", "en": "💰 Finances\n\nHow does your financial situation look right now — what is stable, what concerns you, what are you aiming for?"}
            skip = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить" if lang=="ru" else ("⏭️ Überspringen" if lang=="de" else "⏭️ Skip"), callback_data="about_skip_step")]])
            await query.edit_message_text(q.get(lang, q["ru"]), reply_markup=skip)
        elif about_step == "finance":
            state["about_step"] = "relations"
            compass_state[user_id] = state
            save_session(user_id, session=user_sessions.get(user_id, []), compass=state)
            q = {"ru": "💑 Отношения\n\nРасскажи о своих отношениях — партнёр, семья, друзья, коллеги. Что сейчас радует и что даётся непросто в общении с близкими и окружением?", "de": "💑 Beziehungen\n\nErzähl mir von deinen Beziehungen — Partner, Familie, Freunde, Kollegen. Was freut dich gerade und was fällt schwer im Umgang mit deinem Umfeld?", "en": "💑 Relationships\n\nTell me about your relationships — partner, family, friends, colleagues. What makes you happy right now and what is challenging in your interactions with others?"}
            skip = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить" if lang=="ru" else ("⏭️ Überspringen" if lang=="de" else "⏭️ Skip"), callback_data="about_skip_step")]])
            await query.edit_message_text(q.get(lang, q["ru"]), reply_markup=skip)
        elif about_step == "relations":
            state["about_step"] = "personal"
            compass_state[user_id] = state
            save_session(user_id, session=user_sessions.get(user_id, []), compass=state)
            q = {"ru": "🌱 Личное\n\nЕсть ли что-то важное что сейчас занимает твои мысли больше всего — цель, вопрос, ситуация?", "de": "🌱 Persönliches\n\nGibt es etwas Wichtiges das dich gerade am meisten beschäftigt — ein Ziel, eine Frage, eine Situation?", "en": "🌱 Personal\n\nIs there something important that occupies your mind the most right now — a goal, a question, a situation?"}
            skip = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить" if lang=="ru" else ("⏭️ Überspringen" if lang=="de" else "⏭️ Skip"), callback_data="about_skip_step")]])
            await query.edit_message_text(q.get(lang, q["ru"]), reply_markup=skip)
        elif about_step == "personal":
            compass_state.pop(user_id, None)
            save_session(user_id, session=[], compass={})
            thanks = {"ru": "Спасибо! Теперь Alvalori знает тебя лучше 🌟", "de": "Danke! Jetzt kennt Alvalori dich besser 🌟", "en": "Thank you! Now Alvalori knows you better 🌟"}
            await query.edit_message_text(thanks.get(lang, thanks["ru"]))
            await show_menu(context, user_id, lang)
        return

    elif data == "about_skip":
        skip_msg = {"ru": "Хорошо! Ты всегда можешь добавить информацию о себе в разделе 👤 Обо мне 🌟", "de": "Alles klar! Du kannst jederzeit Informationen über dich im Bereich 👤 Über mich hinzufügen 🌟", "en": "Alright! You can always add information about yourself in the 👤 About me section 🌟"}
        await query.edit_message_text(skip_msg.get(lang, skip_msg["ru"]))
        await show_menu(context, user_id, lang)

    elif data == "about_start":
        gender = user.get("gender", "f") if user else "f"
        privacy_msg = {
            "ru": "🔒 Важно: эти данные используются только для твоего индивидуального анализа. Они никуда не передаются. Видеть и изменять их можешь только ты " + ("сама" if gender == "f" else "сам") + ".",
            "de": "🔒 Wichtig: Diese Daten werden nur für deine individuelle Analyse verwendet. Sie werden nicht weitergegeben. Nur du kannst sie sehen und ändern.",
            "en": "🔒 Important: this data is used only for your individual analysis. It is not shared with anyone. Only you can see and change it."
        }
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Продолжим" if lang=="ru" else ("✅ Weiter" if lang=="de" else "✅ Continue"), callback_data="about_go"),
            InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="about_skip")
        ]])
        await query.edit_message_text(privacy_msg.get(lang, privacy_msg["ru"]), reply_markup=btns)

    elif data == "about_go":
        log_action(user_id, "about", "open", lang)
        compass_state[user_id] = {"stage": "about", "about_step": "work"}
        save_session(user_id, session=user_sessions.get(user_id, []), compass=compass_state[user_id])
        q = {"ru": "💼 Работа/карьера\n\nРасскажи о своей работе или занятии — что сейчас происходит в этой сфере и как ты себя в ней чувствуешь?", "de": "💼 Arbeit/Karriere\n\nErzähl mir von deiner Arbeit oder Tätigkeit — was passiert gerade in diesem Bereich und wie fühlst du dich dabei?", "en": "💼 Work/Career\n\nTell me about your work or occupation — what is happening in this area right now and how do you feel about it?"}
        skip = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить" if lang=="ru" else ("⏭️ Überspringen" if lang=="de" else "⏭️ Skip"), callback_data="about_skip_step")]])
        await query.edit_message_text(q.get(lang, q["ru"]), reply_markup=skip)

    elif data == "btn_about":
        work = user.get("about_work") or "—"
        finance = user.get("about_finance") or "—"
        relations = user.get("about_relations") or "—"
        personal = user.get("about_personal") or "—"
        filled = any(user.get(k) for k in ["about_work","about_finance","about_relations","about_personal","remind_at"])
        if filled:
            about_text = "👤 " + ("" if lang!="ru" else "Обо мне") + ("" if lang!="de" else "Über mich") + ("" if lang!="en" else "About me") + "\n\n💼 " + ("" if lang!="ru" else "Работа") + ("" if lang!="de" else "Arbeit") + ("" if lang!="en" else "Work") + ": " + work + "\n\n💰 " + ("" if lang!="ru" else "Финансы") + ("" if lang!="de" else "Finanzen") + ("" if lang!="en" else "Finances") + ": " + finance + "\n\n💑 " + ("" if lang!="ru" else "Отношения") + ("" if lang!="de" else "Beziehungen") + ("" if lang!="en" else "Relationships") + ": " + relations + "\n\n🌱 " + ("" if lang!="ru" else "Личное") + ("" if lang!="de" else "Persönliches") + ("" if lang!="en" else "Personal") + ": " + personal
        else:
            about_text = ("Секция пуста — нажми Обновить чтобы рассказать о себе" if lang=="ru" else ("Noch keine Angaben — drücke Aktualisieren um mehr über dich zu erzählen" if lang=="de" else "No information yet — press Update to tell about yourself"))
        edit_btns = InlineKeyboardMarkup([[InlineKeyboardButton("✏️ " + ("Обновить" if lang=="ru" else ("Aktualisieren" if lang=="de" else "Update")), callback_data="about_start"), InlineKeyboardButton("◄️ " + ("Меню" if lang=="ru" else ("Menü" if lang=="de" else "Menu")), callback_data="btn_menu")]])
        await query.edit_message_text(about_text, reply_markup=edit_btns)

    elif data == "btn_feedback":
        name = user.get("name", "") if user else ""
        fb_invite = {
            "ru": f"Привет, {name}! Чтобы Alvalori становился лучше — мне важно твоё мнение. Буду признательна если найдёшь 2 минуты и ответишь на 3 вопроса 🙏",
            "de": f"Hallo, {name}! Damit Alvalori besser wird — ist mir deine Meinung wichtig. Ich wäre dankbar wenn du 2 Minuten findest und 3 Fragen beantwortest 🙏",
            "en": f"Hi, {name}! To make Alvalori better — your opinion matters to me. I'd be grateful if you find 2 minutes to answer 3 questions 🙏"
        }
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да" if lang=="ru" else ("✅ Ja" if lang=="de" else "✅ Yes"), callback_data="feedback_yes"),
            InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="feedback_no")
        ]])
        await query.edit_message_text(fb_invite.get(lang, fb_invite["ru"]), reply_markup=btns)

    elif data == "feedback_yes":
        name = user.get("name", "") if user else ""
        gender = user.get("gender", "f") if user else "f"
        thanks_word_ru = "нашла" if gender == "f" else "нашёл"
        fb_ru = f"Спасибо, {name}! Твоё мнение очень важно.\n\nОтветь пожалуйста на три вопроса одним сообщением:\n\n1. Что изменилось в твоём понимании себя после того как ты начал(а) пользоваться Alvalori?\n2. Какой момент или анализ тебя удивил или попал точно в цель?\n3. Кому бы ты порекомендовал(а) Alvalori и почему?"
        fb_de = f"Danke, {name}! Deine Meinung ist sehr wichtig.\n\nBitte beantworte drei Fragen in einer Nachricht:\n\n1. Was hat sich in deinem Selbstverständnis verändert, seit du Alvalori nutzt?\n2. Welcher Moment oder welche Analyse hat dich überrascht oder genau getroffen?\n3. Wem würdest du Alvalori empfehlen und warum?"
        fb_en = f"Thank you, {name}! Your opinion matters a lot.\n\nPlease answer three questions in one message:\n\n1. What changed in your self-understanding since you started using Alvalori?\n2. Which moment or analysis surprised you or hit exactly right?\n3. Who would you recommend Alvalori to and why?"
        fb_text = {"ru": fb_ru, "de": fb_de, "en": fb_en}
        compass_state[user_id] = {"stage": "feedback", "q_num": 1}
        save_session(user_id, session=user_sessions.get(user_id, []), compass=compass_state[user_id])
        await query.edit_message_text(fb_text.get(lang, fb_text["ru"]))

    elif data == "feedback_no":
        no_msg = {"ru": "Спасибо, может в другой раз 🌟", "de": "Danke, vielleicht ein anderes Mal 🌟", "en": "Thank you, maybe another time 🌟"}
        await query.edit_message_text(no_msg.get(lang, no_msg["ru"]))
        await show_menu(context, user_id, lang)

    elif data == "announce_nav_open":
        gender = user.get("gender", "f") if user else "f"
        info_ru = (
            "<b>ℹ️ Знакомство с Alvalori</b>\n\n"
            "Alvalori анализирует твою дату рождения — и на её основе определяет модель мышления, сильные стороны и текущие жизненные циклы. Каждый анализ строится индивидуально только для тебя.\n\n"
            "<b>🌟 Основа моей личности</b>\n"
            "Как ты думаешь, принимаешь решения и реагируешь на давление. Это твой фундамент.\n\n"
            "<b>🧭 Личный год / 📍 Месяц / ☀️ День</b>\n"
            "Что сейчас происходит в твоей жизни, на что обратить внимание и чего избегать. Год — общий вектор, месяц — тактика, день — фокус на сегодня.\n\n"
            "<b>💡 Поговорим о главном</b>\n"
            "Расскажи о любой ситуации — в работе, отношениях, жизни. Alvalori задаст вопросы и даст персональный анализ именно для тебя.\n\n"
            "<b>👤 Обо мне</b>\n"
            "Чем больше Alvalori знает о тебе — тем глубже и точнее становится каждый анализ. Не общие слова, а то что актуально именно для тебя прямо сейчас.\n\n"
            "<b>💳 Подписка</b>\n"
            "Пробный период — 72 часа, каждый раздел доступен 1 раз за весь пробный период. Подписка открывает все разделы без ограничений — включая индивидуальный обзор в начале каждого месяца. От 15€/месяц.\n\n"
            "<b>⚙️ Настройки</b>\n"
            "🌐 Сменить язык\n"
            "✏️ Изменить имя\n"
            "📅 Изменить дату рождения\n"
            "📋 Правила\n"
            "❓ Помощь\n"
            "💬 Написать администратору\n\n"
            "<b>✍️ Оставить отзыв</b>\n"
            "Расскажи что понравилось и что можно улучшить — это помогает Alvalori становиться лучше."
        )
        info_de = (
            "<b>ℹ️ Alvalori kennenlernen</b>\n\n"
            "Alvalori analysiert dein Geburtsdatum — und bestimmt daraus dein Denkmodell, deine Stärken und aktuelle Lebenszyklen. Jede Analyse wird individuell nur für dich erstellt.\n\n"
            "<b>🌟 Meine Persönlichkeit</b>\n"
            "Wie du denkst, Entscheidungen triffst und auf Druck reagierst. Das ist dein Fundament.\n\n"
            "<b>🧭 Persönliches Jahr / 📍 Monat / ☀️ Tag</b>\n"
            "Was gerade in deinem Leben passiert, worauf du achten solltest und was du vermeiden solltest. Jahr — Gesamtvektor, Monat — Taktik, Tag — Fokus für heute.\n\n"
            "<b>💡 Lass uns reden</b>\n"
            "Erzähl von einer Situation — Arbeit, Beziehungen, Leben. Alvalori stellt Fragen und gibt dir eine persönliche Analyse.\n\n"
            "<b>👤 Über mich</b>\n"
            "Je mehr Alvalori über dich weiß — desto tiefer und präziser wird jede Analyse. Nicht allgemeine Worte, sondern was gerade für dich relevant ist.\n\n"
            "<b>💳 Abonnement</b>\n"
            "Testzugang — 72 Stunden, jeder Bereich 1x im gesamten Testzeitraum. Das Abonnement öffnet alle Bereiche ohne Einschränkungen — einschließlich einer individuellen Monatsübersicht. Ab 15€/Monat.\n\n"
            "<b>⚙️ Einstellungen</b>\n"
            "🌐 Sprache ändern\n"
            "✏️ Name ändern\n"
            "📅 Geburtsdatum ändern\n"
            "📋 Regeln\n"
            "❓ Hilfe\n"
            "💬 Administrator schreiben\n\n"
            "<b>✍️ Feedback hinterlassen</b>\n"
            "Erzähl uns was dir gefallen hat und was verbessert werden kann — das hilft Alvalori besser zu werden."
        )
        info_en = (
            "<b>ℹ️ Get to know Alvalori</b>\n\n"
            "Alvalori analyses your date of birth — and based on it determines your thinking model, strengths and current life cycles. Every analysis is built individually just for you.\n\n"
            "<b>🌟 My Personality</b>\n"
            "How you think, make decisions and respond to pressure. This is your foundation.\n\n"
            "<b>🧭 Personal Year / 📍 Month / ☀️ Day</b>\n"
            "What is happening in your life right now, what to pay attention to and what to avoid. Year — overall vector, month — tactics, day — focus for today.\n\n"
            "<b>💡 Let\'s talk</b>\n"
            "Tell about any situation — work, relationships, life. Alvalori will ask questions and give a personal analysis just for you.\n\n"
            "<b>👤 About me</b>\n"
            "The more Alvalori knows about you — the deeper and more precise each analysis becomes. Not general words, but what is relevant for you right now.\n\n"
            "<b>💳 Subscription</b>\n"
            "Trial period — 72 hours, each section available 1 time during the entire trial. Subscription opens all sections without limits — including an individual monthly overview. From 15€/month.\n\n"
            "<b>⚙️ Settings</b>\n"
            "🌐 Change language\n"
            "✏️ Change name\n"
            "📅 Change date of birth\n"
            "📋 Rules\n"
            "❓ Help\n"
            "💬 Contact administrator\n\n"
            "<b>✍️ Leave feedback</b>\n"
            "Tell us what you liked and what can be improved — it helps Alvalori get better."
        )
        info_text = {"ru": info_ru, "de": info_de, "en": info_en}
        menu_btn = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Меню" if lang=="ru" else ("◀️ Menü" if lang=="de" else "◀️ Menu"), callback_data="btn_menu")]])
        await query.edit_message_text(info_text.get(lang, info_text["ru"]), reply_markup=menu_btn, parse_mode="HTML")
        await show_menu(context, user_id, lang)

    elif data == "announce_nav_no":
        no_msg = {"ru": "Хорошо! Найдёшь в меню когда понадобится 🌟", "de": "Alles klar! Du findest es im Menü wenn du es brauchst 🌟", "en": "Alright! You'll find it in the menu when you need it 🌟"}
        await query.edit_message_text(no_msg.get(lang, no_msg["ru"]))
        await show_menu(context, user_id, lang)

    elif data == "paid_churn_share":
        gender = user.get("gender", "f") if user else "f"
        q = {"ru": "Что остановило от продления?", "de": "Was hat dich von der Verlängerung abgehalten?", "en": "What stopped you from renewing?"}
        btns_ru = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Дорого", callback_data="churn_price")],
            [InlineKeyboardButton("⏰ Попробую позже", callback_data="churn_later")],
            [InlineKeyboardButton("🔍 Хочу другой функционал", callback_data="churn_func")],
            [InlineKeyboardButton("❌ Другое", callback_data="churn_other")],
        ])
        btns_de = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Zu teuer", callback_data="churn_price")],
            [InlineKeyboardButton("⏰ Später versuchen", callback_data="churn_later")],
            [InlineKeyboardButton("🔍 Andere Funktionen gewünscht", callback_data="churn_func")],
            [InlineKeyboardButton("❌ Anderes", callback_data="churn_other")],
        ])
        btns_en = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Too expensive", callback_data="churn_price")],
            [InlineKeyboardButton("⏰ Will try later", callback_data="churn_later")],
            [InlineKeyboardButton("🔍 Want different features", callback_data="churn_func")],
            [InlineKeyboardButton("❌ Other", callback_data="churn_other")],
        ])
        btns = {"ru": btns_ru, "de": btns_de, "en": btns_en}
        await query.edit_message_text(q.get(lang, q["ru"]), reply_markup=btns.get(lang, btns["ru"]))

    elif data == "churn_share":
        gender = user.get("gender", "f") if user else "f"
        q_ru = "Что остановило от подписки?"
        q_de = "Was hat dich von einem Abonnement abgehalten?"
        q_en = "What stopped you from subscribing?"
        btns_ru = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Дорого", callback_data="churn_price")],
            [InlineKeyboardButton("🤔 Не " + ("поняла" if gender=="f" else "понял") + " ценность", callback_data="churn_value")],
            [InlineKeyboardButton("⏰ Попробую позже", callback_data="churn_later")],
            [InlineKeyboardButton("🔍 Не хватило функционала", callback_data="churn_func")],
            [InlineKeyboardButton("❌ Другое", callback_data="churn_other")],
        ])
        btns_de = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Zu teuer", callback_data="churn_price")],
            [InlineKeyboardButton("🤔 Wert nicht verstanden", callback_data="churn_value")],
            [InlineKeyboardButton("⏰ Später versuchen", callback_data="churn_later")],
            [InlineKeyboardButton("🔍 Funktionen fehlten", callback_data="churn_func")],
            [InlineKeyboardButton("❌ Anderes", callback_data="churn_other")],
        ])
        btns_en = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Too expensive", callback_data="churn_price")],
            [InlineKeyboardButton("🤔 Didn't understand the value", callback_data="churn_value")],
            [InlineKeyboardButton("⏰ Will try later", callback_data="churn_later")],
            [InlineKeyboardButton("🔍 Missing features", callback_data="churn_func")],
            [InlineKeyboardButton("❌ Other", callback_data="churn_other")],
        ])
        btns = {"ru": btns_ru, "de": btns_de, "en": btns_en}
        q = {"ru": q_ru, "de": q_de, "en": q_en}
        await query.edit_message_text(q.get(lang, q["ru"]), reply_markup=btns.get(lang, btns["ru"]))

    elif data == "churn_no":
        msg = {"ru": "Хорошо! Если передумаешь — кнопка Оставить отзыв всегда в меню 🌟", "de": "Alles klar! Wenn du es dir anders überlegst — die Schaltfläche Feedback ist immer im Menü 🌟", "en": "Alright! If you change your mind — the Leave feedback button is always in the menu 🌟"}
        await query.edit_message_text(msg.get(lang, msg["ru"]))
        await show_menu(context, user_id, lang)

    elif data in ("churn_price", "churn_value", "churn_func", "churn_other"):
        gender = user.get("gender", "f") if user else "f"
        questions = {
            "churn_price": {"ru": "Один честный ответ поможет сделать Alvalori лучше — при какой цене ты бы подписалась?", "de": "Eine ehrliche Antwort hilft Alvalori besser zu machen — bei welchem Preis hättest du abonniert?", "en": "One honest answer helps make Alvalori better — at what price would you have subscribed?"},
            "churn_value": {"ru": "Один честный ответ поможет сделать Alvalori лучше — что именно не попало в точку или осталось непонятным?", "de": "Eine ehrliche Antwort hilft Alvalori besser zu machen — was genau hat nicht getroffen oder war unklar?", "en": "One honest answer helps make Alvalori better — what exactly didn't resonate or remained unclear?"},
            "churn_func": {"ru": "Один честный ответ поможет сделать Alvalori лучше — чего именно не хватило?", "de": "Eine ehrliche Antwort hilft Alvalori besser zu machen — was genau hat gefehlt?", "en": "One honest answer helps make Alvalori better — what exactly was missing?"},
            "churn_other": {"ru": "Один честный ответ поможет сделать Alvalori лучше — что именно не сработало для тебя?", "de": "Eine ehrliche Antwort hilft Alvalori besser zu machen — was genau hat für dich nicht funktioniert?", "en": "One honest answer helps make Alvalori better — what exactly didn't work for you?"},
        }
        q = questions.get(data, {})
        compass_state[user_id] = {"stage": "churn_answer", "reason": data}
        save_session(user_id, session=user_sessions.get(user_id, []), compass=compass_state[user_id])
        await query.edit_message_text(q.get(lang, q["ru"]))

    elif data == "churn_later":
        gender = user.get("gender", "f") if user else "f"
        remind_q = {"ru": "Напомнить тебе через месяц?", "de": "Soll ich dich in einem Monat erinnern?", "en": "Should I remind you in a month?"}
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да" if lang=="ru" else ("✅ Ja" if lang=="de" else "✅ Yes"), callback_data="churn_remind_yes"),
            InlineKeyboardButton("❌ Нет" if lang=="ru" else ("❌ Nein" if lang=="de" else "❌ No"), callback_data="churn_remind_no"),
        ]])
        await query.edit_message_text(remind_q.get(lang, remind_q["ru"]), reply_markup=btns)

    elif data == "churn_remind_yes":
        import datetime
        remind_date = (datetime.datetime.now() + datetime.timedelta(days=30)).date()
        save_user(user_id, remind_at=remind_date)
        thanks = {"ru": "Отлично! Напомню через месяц 🌟", "de": "Super! Ich erinnere dich in einem Monat 🌟", "en": "Great! I'll remind you in a month 🌟"}
        await query.edit_message_text(thanks.get(lang, thanks["ru"]))
        await show_menu(context, user_id, lang)

    elif data == "churn_remind_no":
        thanks = {"ru": "Хорошо! Если передумаешь — всегда ждём 🌟", "de": "Alles klar! Wenn du es dir anders überlegst — wir warten 🌟", "en": "Alright! If you change your mind — we're always here 🌟"}
        await query.edit_message_text(thanks.get(lang, thanks["ru"]))
        await show_menu(context, user_id, lang)

    elif data == "remind_no":
        no_msg = {"ru": "Хорошо! Если передумаешь — я здесь 🌟", "de": "Alles klar! Wenn du es dir anders überlegst — ich bin hier 🌟", "en": "Alright! If you change your mind — I'm here 🌟"}
        await query.edit_message_text(no_msg.get(lang, no_msg["ru"]))

    elif data == "btn_info":
        info_ru = (
            "<b>ℹ️ Знакомство с Alvalori</b>\n\n"
            "Alvalori анализирует твою дату рождения — и на её основе определяет модель мышления, сильные стороны и текущие жизненные циклы. Каждый анализ строится индивидуально только для тебя.\n\n"
            "<b>🌟 Основа моей личности</b>\n"
            "Как ты думаешь, принимаешь решения и реагируешь на давление. Это твой фундамент.\n\n"
            "<b>🧭 Личный год / 📍 Месяц / ☀️ День</b>\n"
            "Что сейчас происходит в твоей жизни, на что обратить внимание и чего избегать. Год — общий вектор, месяц — тактика, день — фокус на сегодня.\n\n"
            "<b>💡 Поговорим о главном</b>\n"
            "Расскажи о любой ситуации — в работе, отношениях, жизни. Alvalori задаст вопросы и даст персональный анализ именно для тебя.\n\n"
            "<b>👤 Обо мне</b>\n"
            "Чем больше Alvalori знает о тебе — тем глубже и точнее становится каждый анализ. Не общие слова, а то что актуально именно для тебя прямо сейчас.\n\n"
            "<b>💳 Подписка</b>\n"
            "Пробный период — 72 часа, каждый раздел доступен 1 раз за весь пробный период. Подписка открывает все разделы без ограничений — включая индивидуальный обзор в начале каждого месяца. От 15€/месяц.\n\n"
            "<b>⚙️ Настройки</b>\n"
            "🌐 Сменить язык\n"
            "✏️ Изменить имя\n"
            "📅 Изменить дату рождения\n"
            "📋 Правила\n"
            "❓ Помощь\n"
            "💬 Написать администратору\n\n"
            "<b>✍️ Оставить отзыв</b>\n"
            "Расскажи что понравилось и что можно улучшить — это помогает Alvalori становиться лучше."
        )
        info_de = (
            "<b>ℹ️ Alvalori kennenlernen</b>\n\n"
            "Alvalori analysiert dein Geburtsdatum — und bestimmt daraus dein Denkmodell, deine Stärken und aktuelle Lebenszyklen. Jede Analyse wird individuell nur für dich erstellt.\n\n"
            "<b>🌟 Meine Persönlichkeit</b>\n"
            "Wie du denkst, Entscheidungen triffst und auf Druck reagierst. Das ist dein Fundament.\n\n"
            "<b>🧭 Persönliches Jahr / 📍 Monat / ☀️ Tag</b>\n"
            "Was gerade in deinem Leben passiert, worauf du achten solltest und was du vermeiden solltest. Jahr — Gesamtvektor, Monat — Taktik, Tag — Fokus für heute.\n\n"
            "<b>💡 Lass uns reden</b>\n"
            "Erzähl von einer Situation — Arbeit, Beziehungen, Leben. Alvalori stellt Fragen und gibt dir eine persönliche Analyse.\n\n"
            "<b>👤 Über mich</b>\n"
            "Je mehr Alvalori über dich weiß — desto tiefer und präziser wird jede Analyse. Nicht allgemeine Worte, sondern was gerade für dich relevant ist.\n\n"
            "<b>💳 Abonnement</b>\n"
            "Testzugang — 72 Stunden, jeder Bereich 1x im gesamten Testzeitraum. Das Abonnement öffnet alle Bereiche ohne Einschränkungen — einschließlich einer individuellen Monatsübersicht. Ab 15€/Monat.\n\n"
            "<b>⚙️ Einstellungen</b>\n"
            "🌐 Sprache ändern\n"
            "✏️ Name ändern\n"
            "📅 Geburtsdatum ändern\n"
            "📋 Regeln\n"
            "❓ Hilfe\n"
            "💬 Administrator schreiben\n\n"
            "<b>✍️ Feedback hinterlassen</b>\n"
            "Erzähl uns was dir gefallen hat und was verbessert werden kann — das hilft Alvalori besser zu werden."
        )
        info_en = (
            "<b>ℹ️ Get to know Alvalori</b>\n\n"
            "Alvalori analyses your date of birth — and based on it determines your thinking model, strengths and current life cycles. Every analysis is built individually just for you.\n\n"
            "<b>🌟 My Personality</b>\n"
            "How you think, make decisions and respond to pressure. This is your foundation.\n\n"
            "<b>🧭 Personal Year / 📍 Month / ☀️ Day</b>\n"
            "What is happening in your life right now, what to pay attention to and what to avoid. Year — overall vector, month — tactics, day — focus for today.\n\n"
            "<b>💡 Let\'s talk</b>\n"
            "Tell about any situation — work, relationships, life. Alvalori will ask questions and give a personal analysis just for you.\n\n"
            "<b>👤 About me</b>\n"
            "The more Alvalori knows about you — the deeper and more precise each analysis becomes. Not general words, but what is relevant for you right now.\n\n"
            "<b>💳 Subscription</b>\n"
            "Trial period — 72 hours, each section available 1 time during the entire trial. Subscription opens all sections without limits — including an individual monthly overview. From 15€/month.\n\n"
            "<b>⚙️ Settings</b>\n"
            "🌐 Change language\n"
            "✏️ Change name\n"
            "📅 Change date of birth\n"
            "📋 Rules\n"
            "❓ Help\n"
            "💬 Contact administrator\n\n"
            "<b>✍️ Leave feedback</b>\n"
            "Tell us what you liked and what can be improved — it helps Alvalori get better."
        )
        info_text = {"ru": info_ru, "de": info_de, "en": info_en}
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Меню" if lang=="ru" else ("◀️ Menü" if lang=="de" else "◀️ Menu"), callback_data="btn_menu")
        ]])
        await query.edit_message_text(info_text.get(lang, info_text["ru"]), reply_markup=btns, parse_mode="HTML")

    elif data == "btn_help":
        help_ru = "❓ Помощь\n\n🆓 Пробный доступ\n72 часа бесплатно — все разделы открыты. Просто перестань пользоваться — ничего отменять не нужно.\n\n💳 Платная подписка\nПодписка продлевается автоматически. Отменить можно в любой момент — напиши администратору.\n\n✏️ Изменить имя или дату рождения\nНастройки → Изменить имя / Изменить дату рождения. Каждое можно изменить 1 раз самостоятельно.\n\n💬 Остались вопросы?\nНапиши администратору"
        help_de = "❓ Hilfe\n\n🆓 Testzugang\n72 Stunden kostenlos — alle Bereiche verfügbar. Keine Kündigung notwendig.\n\n💳 Bezahltes Abonnement\nDas Abonnement verlängert sich automatisch. Du kannst jederzeit kündigen — schreibe dem Administrator.\n\n✏️ Name oder Geburtsdatum ändern\nEinstellungen → Name ändern / Geburtsdatum ändern. Jeweils einmal selbst möglich.\n\n💬 Noch Fragen?\nSchreibe dem Administrator @aeng0"
        help_en = "❓ Help\n\n🆓 Trial access\n72 hours free — all sections available. Just stop using it — no cancellation needed.\n\n💳 Paid subscription\nSubscription renews automatically. You can cancel at any time — contact the administrator.\n\n✏️ Change name or date of birth\nSettings → Change name / Change date of birth. Each can be changed once.\n\n💬 Still have questions?\nWrite to the administrator @aeng0"
        help_text = {"ru": help_ru, "de": help_de, "en": help_en}
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✍️ Написать администратору" if lang=="ru" else ("✍️ Administrator schreiben" if lang=="de" else "✍️ Contact administrator"), url="https://t.me/aeng0"),
            InlineKeyboardButton("◀️ Меню" if lang=="ru" else ("◀️ Menü" if lang=="de" else "◀️ Menu"), callback_data="btn_menu")
        ]])
        await query.edit_message_text(help_text.get(lang, help_text["ru"]), reply_markup=btns)

    elif data == "btn_menu":
        await show_menu(context, user_id, lang)
        return

    elif data == "trial_not_now":
        about_invite = {
            "ru": "Хочешь рассказать о себе подробнее? Это поможет Alvalori давать более точный анализ.",
            "de": "Möchtest du mehr über dich erzählen? Das hilft Alvalori genauere Analysen zu erstellen.",
            "en": "Would you like to tell more about yourself? This helps Alvalori provide more accurate analysis."
        }
        about_btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да" if lang=="ru" else ("✅ Ja" if lang=="de" else "✅ Yes"), callback_data="about_start"),
            InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="about_skip")
        ]])
        await query.edit_message_text(about_invite.get(lang, about_invite["ru"]), reply_markup=about_btns)
        return

    elif data == "btn_settings":
        if not user:
            await query.edit_message_text("Напиши /start")
            return
        status_map = {
            "ru": ("Полный доступ ✅" if is_paid(user) else ("Пробный период 🌿" if is_trial_active(user) else "Доступ завершён 🔺")),
            "de": ("Vollzugang ✅" if is_paid(user) else ("Testphase 🌿" if is_trial_active(user) else "Zugang beendet 🔺")),
            "en": ("Full access ✅" if is_paid(user) else ("Trial 🌿" if is_trial_active(user) else "Access ended 🔺")),
        }
        changes_left = 1 - (user.get("data_changes") or 0)
        change_note = {"ru": f"Изменить данные: {'1 раз осталось' if changes_left > 0 else 'только через @aeng0'}", "de": f"Daten ändern: {'1x möglich' if changes_left > 0 else 'nur über @aeng0'}", "en": f"Change data: {'1 time left' if changes_left > 0 else 'only via @aeng0'}"}
        ru_info = "⚙️ Настройки\n\nИмя: " + str(user.get("name","-")) + "\nДата рождения: " + str(user.get("day")) + "." + str(user.get("month")) + "." + str(user.get("year")) + "\nСтатус: " + status_map["ru"] + "\n" + change_note["ru"]
        de_info = "⚙️ Einstellungen\n\nName: " + str(user.get("name","-")) + "\nGeburtsdatum: " + str(user.get("day")) + "." + str(user.get("month")) + "." + str(user.get("year")) + "\nStatus: " + status_map["de"] + "\n" + change_note["de"]
        en_info = "⚙️ Settings\n\nName: " + str(user.get("name","-")) + "\nDate of birth: " + str(user.get("day")) + "." + str(user.get("month")) + "." + str(user.get("year")) + "\nStatus: " + status_map["en"] + "\n" + change_note["en"]
        info = {"ru": ru_info, "de": de_info, "en": en_info}
        btns = [
            [InlineKeyboardButton("📋 Правила" if lang=="ru" else ("📋 Regeln" if lang=="de" else "📋 Rules"), callback_data="btn_rules")],
            [InlineKeyboardButton("✏️ Изменить имя" if lang=="ru" else ("✏️ Name ändern" if lang=="de" else "✏️ Change name"), callback_data="btn_change_name")],
            [InlineKeyboardButton("📅 Изменить дату рождения" if lang=="ru" else ("📅 Geburtsdatum ändern" if lang=="de" else "📅 Change date of birth"), callback_data="btn_change_date")],
            [InlineKeyboardButton("🌐 Сменить язык" if lang=="ru" else ("🌐 Sprache ändern" if lang=="de" else "🌐 Change language"), callback_data="btn_lang")],
            [InlineKeyboardButton("❓ Помощь" if lang=="ru" else ("❓ Hilfe" if lang=="de" else "❓ Help"), callback_data="btn_help")],
            [InlineKeyboardButton("💬 Написать администратору" if lang=="ru" else ("💬 Admin schreiben" if lang=="de" else "💬 Contact admin"), url="https://t.me/aeng0")],
            [InlineKeyboardButton("◀️ Меню" if lang=="ru" else ("◀️ Menü" if lang=="de" else "◀️ Menu"), callback_data="btn_menu")],
        ]
        await query.edit_message_text(info.get(lang, info["ru"]), reply_markup=InlineKeyboardMarkup(btns))

    elif data == "btn_rules":
        await query.edit_message_text(RULES[lang], reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад" if lang=="ru" else ("◀️ Zurück" if lang=="de" else "◀️ Back"), callback_data="btn_settings")]]))

    elif data == "btn_change_name":
        name_changes = user.get("name_changes") or 0
        if name_changes >= 1:
            msg = {"ru": "Ты уже изменила имя. Для повторного изменения напиши @aeng0.", "de": "Du hast den Namen bereits geändert. Schreibe @aeng0.", "en": "You have already changed your name. Contact @aeng0."}
            await query.edit_message_text(msg.get(lang, msg["ru"]), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад" if lang=="ru" else "◀️ Zurück", callback_data="btn_settings")]]))
            return
        msg = {"ru": "Напиши новое имя:", "de": "Schreibe deinen neuen Namen:", "en": "Write your new name:"}
        user_sessions[user_id] = [{"role": "assistant", "content": "change_name"}]
        save_session(user_id, session=user_sessions[user_id], compass={})
        await query.edit_message_text(msg.get(lang, msg["ru"]))

    elif data == "btn_change_date":
        date_changes = user.get("date_changes") or 0
        if date_changes >= 1:
            msg = {"ru": "Ты уже изменила дату рождения. Для повторного изменения напиши @aeng0.", "de": "Du hast das Geburtsdatum bereits geändert. Schreibe @aeng0.", "en": "You have already changed your date of birth. Contact @aeng0."}
            await query.edit_message_text(msg.get(lang, msg["ru"]), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад" if lang=="ru" else "◀️ Zurück", callback_data="btn_settings")]]))
            return
        msg = {"ru": "Напиши дату рождения в формате ДД.ММ.ГГГГ:", "de": "Schreibe dein Geburtsdatum im Format TT.MM.JJJJ:", "en": "Write your date of birth in format DD.MM.YYYY:"}
        user_sessions[user_id] = [{"role": "assistant", "content": "change_date"}]
        save_session(user_id, session=user_sessions[user_id], compass={})
        await query.edit_message_text(msg.get(lang, msg["ru"]))

    elif data == "btn_change_data":
        changes_left = 1 - (user.get("data_changes") or 0)
        if changes_left <= 0:
            msg = {"ru": "Ты уже использовала возможность изменить данные. Для изменений напиши @aeng0.", "de": "Du hast die Möglichkeit bereits genutzt. Schreibe @aeng0.", "en": "You have already used your one change. Contact @aeng0."}
            await query.edit_message_text(msg.get(lang, msg["ru"]), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад" if lang=="ru" else "◀️ Back", callback_data="btn_settings")]]))
            return
        msg = {"ru": "Напиши новое имя или дату рождения (ДД.ММ.ГГГГ). Это можно сделать только 1 раз.", "de": "Schreibe deinen neuen Namen oder dein Geburtsdatum (TT.MM.JJJJ). Dies ist nur 1x möglich.", "en": "Write your new name or date of birth (DD.MM.YYYY). This can only be done once."}
        save_user(user_id, lang=lang)
        user_sessions[user_id] = [{"role": "assistant", "content": "change_data"}]
        await query.edit_message_text(msg.get(lang, msg["ru"]))

    elif data == "btn_menu_home":
        await show_menu(context, user_id, lang)

    elif data == "btn_lang":
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
            [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
        ]
        await query.edit_message_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "trial_choice":
        trial_msg = {
            "ru": "🌟 У тебя есть <b>3 дня бесплатного доступа</b> ко всем разделам.\n\nПосле этого доступ можно продлить по подписке от 15€/месяц.",
            "de": "🌟 Du hast <b>3 Tage kostenlosen Zugang</b> zu allen Bereichen.\n\nDanach kannst du den Zugang ab 15€/Monat verlängern.",
            "en": "🌟 You have <b>3 days of free access</b> to all sections.\n\nAfterwards you can continue with a subscription from 15€/month."
        }
        trial_btns = InlineKeyboardMarkup([
            [InlineKeyboardButton("🆓 Продолжить бесплатно" if lang=="ru" else ("🆓 Kostenlos weitermachen" if lang=="de" else "🆓 Continue for free"), callback_data="trial_start")],
            [InlineKeyboardButton("💳 Купить сразу" if lang=="ru" else ("💳 Jetzt kaufen" if lang=="de" else "💳 Buy now"), callback_data="btn_pay")],
        ])
        await query.edit_message_text(trial_msg.get(lang, trial_msg["ru"]), reply_markup=trial_btns, parse_mode="HTML")
        return

    elif data == "trial_start":
        if user and user.get("agreed") and user.get("day"):
            # Существующий пользователь — просто показываем меню
            await show_menu(context, user_id, lang)
            return
        # Новый пользователь — начинаем регистрацию
        import datetime
        save_user(user_id, trial_started_at=datetime.datetime.now(datetime.timezone.utc), agreed=True)
        greet = {"ru": "Привет! Это Alvalori. Как тебя зовут?", "de": "Hallo! Das ist Alvalori. Wie heißt du?", "en": "Hi! This is Alvalori. What is your name?"}
        msg = greet.get(lang, greet["ru"])
        user_sessions[user_id] = [{"role": "assistant", "content": msg}]
        await query.edit_message_text(msg)
        return

    elif data == "btn_manage_subscription":
        portal_url = "https://billing.stripe.com/p/login/eVq3cpfbB7ww26aakwcMM00"
        btn_text = {"ru": "🔗 Открыть портал управления подпиской", "de": "🔗 Abonnement-Portal öffnen", "en": "🔗 Open subscription portal"}
        msg = {"ru": "Нажми кнопку ниже чтобы управлять своей подпиской — там можно отменить или посмотреть историю платежей.", "de": "Klicke auf den Button um dein Abonnement zu verwalten — dort kannst du kündigen oder die Zahlungshistorie einsehen.", "en": "Click the button below to manage your subscription — you can cancel or view payment history."}
        await query.edit_message_text(msg.get(lang, msg["ru"]), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(btn_text.get(lang, btn_text["ru"]), url=portal_url)], [InlineKeyboardButton("◀️ Меню" if lang=="ru" else ("◀️ Menü" if lang=="de" else "◀️ Menu"), callback_data="btn_menu")]]))

    elif data == "btn_pay":
        if user and user.get("is_minor"):
            minor_pay_msg = {
                "ru": "Эта функция доступна с 18 лет 🌟",
                "de": "Diese Funktion ist ab 18 Jahren verfügbar 🌟",
                "en": "This feature is available from age 18 🌟"
            }
            await query.edit_message_text(minor_pay_msg.get(lang, minor_pay_msg["ru"]), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Меню" if lang=="ru" else ("◀️ Menü" if lang=="de" else "◀️ Menu"), callback_data="btn_menu")]]))
            return
        if user and is_paid(user) and user.get("stripe_customer_id"):
            paid_until_str = user.get("paid_until")
            if paid_until_str:
                paid_until_fmt = paid_until_str.replace(tzinfo=None).strftime("%d.%m.%Y")
            else:
                paid_until_fmt = "—"
            active_msg = {
                "ru": "✅ Твоя подписка активна до " + paid_until_fmt + ". Через портал ты можешь отменить подписку или посмотреть историю платежей.",
                "de": "Das Abo ist aktiv bis " + paid_until_fmt + ". Im Portal kannst du es kuendigen oder die Zahlungshistorie einsehen.",
                "en": "Your subscription is active until " + paid_until_fmt + ". Through the portal you can cancel or view payment history.",
            }
            manage_btn = {
                "ru": "⚙️ Управление подпиской",
                "de": "⚙️ Abonnement verwalten",
                "en": "⚙️ Manage subscription"
            }
            await query.edit_message_text(active_msg.get(lang, active_msg["ru"]), reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(manage_btn.get(lang, manage_btn["ru"]), callback_data="btn_manage_subscription")],
                [InlineKeyboardButton("◀️ Меню" if lang=="ru" else ("◀️ Menü" if lang=="de" else "◀️ Menu"), callback_data="btn_menu")]
            ]))
            return
        descriptions = {
            "ru": {"1m": "1 месяц — 15€", "6m": "6 месяцев — 84€", "12m": "12 месяцев — 162€"},
            "de": {"1m": "1 Monat — 15€", "6m": "6 Monate — 84€", "12m": "12 Monate — 162€"},
            "en": {"1m": "1 month — 15€", "6m": "6 months — 84€", "12m": "12 months — 162€"}
        }
        header = {"ru": "Выбери тариф:", "de": "Wähle einen Tarif:", "en": "Choose a plan:"}
        btns = [
            [InlineKeyboardButton(descriptions[lang]["1m"], callback_data="pay_1m")],
            [InlineKeyboardButton(f"⭐ {descriptions[lang]['6m']}", callback_data="pay_6m")],
            [InlineKeyboardButton(descriptions[lang]["12m"], callback_data="pay_12m")],
            [InlineKeyboardButton("🇷🇺 Оплата из России" if lang=="ru" else ("🇷🇺 Zahlung aus Russland" if lang=="de" else "🇷🇺 Payment from Russia"), url="https://t.me/aeng0")],
            [InlineKeyboardButton("🏠 Меню" if lang=="ru" else ("🏠 Menü" if lang=="de" else "🏠 Menu"), callback_data="btn_menu_home")],
        ]
        await query.edit_message_text(header.get(lang, header["ru"]), reply_markup=InlineKeyboardMarkup(btns))

    elif data.startswith("pay_"):
        plan = data.replace("pay_", "")
        price_ids = {
            "1m": "price_1TlCHp8xIqd4ykush9BOGMGe",
            "6m": "price_1TlCHp8xIqd4ykusli9sZyEb",
            "12m": "price_1TlCHp8xIqd4ykusd0fCdojN"
        }
        descriptions = {
            "ru": {"1m": "1 месяц — 15€", "6m": "6 месяцев — 84€", "12m": "12 месяцев — 162€"},
            "de": {"1m": "1 Monat — 15€", "6m": "6 Monate — 84€", "12m": "12 Monate — 162€"},
            "en": {"1m": "1 month — 15€", "6m": "6 months — 84€", "12m": "12 months — 162€"}
        }
        label = descriptions[lang][plan]
        try:
            stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
            session = stripe.checkout.Session.create(
                payment_method_types=["card"],
                line_items=[{"price": price_ids[plan], "quantity": 1}],
                mode="subscription",
                success_url=f"https://t.me/alvalori_bot?start=paid_{user_id}_{plan}",
                cancel_url=f"https://t.me/alvalori_bot",
                metadata={"user_id": str(user_id), "plan": plan}
            )
            pay_msg = {"ru": f"Тариф: {label}\n\nНажми кнопку для оплаты картой через Stripe:", "de": f"Tarif: {label}\n\nKlicke auf die Schaltfläche zur Kartenzahlung über Stripe:", "en": f"Plan: {label}\n\nClick the button to pay by card via Stripe:"}
            btns = [
                [InlineKeyboardButton("💳 Оплатить картой" if lang=="ru" else ("💳 Mit Karte bezahlen" if lang=="de" else "💳 Pay by card"), url=session.url)],
                [InlineKeyboardButton("🇷🇺 Оплата из России" if lang=="ru" else ("🇷🇺 Zahlung aus Russland" if lang=="de" else "🇷🇺 Payment from Russia"), url="https://t.me/aeng0")],
                [InlineKeyboardButton("🏠 Меню" if lang=="ru" else ("🏠 Menü" if lang=="de" else "🏠 Menu"), callback_data="btn_menu_home")],
            ]
            await query.edit_message_text(pay_msg.get(lang, pay_msg["ru"]), reply_markup=InlineKeyboardMarkup(btns))
        except Exception as e:
            print(f"Stripe error: {e}", flush=True)
            await query.edit_message_text({"ru": "Ошибка создания платежа. Напиши администратору.", "de": "Fehler bei der Zahlungserstellung. Schreibe dem Administrator.", "en": "Payment error. Contact administrator."}.get(lang, "Payment error."), reply_markup=get_upgrade_keyboard(lang))




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
        await query.edit_message_text({"ru": "Рада помочь! 🌿", "de": "Gerne!", "en": "Glad to help!"}.get(lang, ""))
        await show_menu(context, user_id, lang)
    else:
        state = compass_state.get(user_id, {})
        state["stage"] = "clarify"
        state["clarify_count"] = 0
        compass_state[user_id] = state
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

    db_session, db_compass = get_session(user_id)
    if not isinstance(db_session, list):
        db_session = []
    user_sessions[user_id] = db_session
    if db_compass:
        compass_state[user_id] = db_compass

    # Проверяем change_name и change_date ДО добавления в сессию
    session_marker = user_sessions[user_id][0].get("content") if user_sessions.get(user_id) and user_sessions[user_id] else None

    if session_marker == "change_name":
        save_user(user_id, name=user_text.strip(), name_changes=1)
        user_sessions[user_id] = []
        ok = {"ru": f"✅ Имя обновлено на {user_text.strip()}!", "de": f"✅ Name auf {user_text.strip()} aktualisiert!", "en": f"✅ Name updated to {user_text.strip()}!"}
        await update.message.reply_text(ok.get(lang, ok["ru"]))
        await show_menu(context, user_id, lang)
        return

    if session_marker == "change_date":
        date_match = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", user_text.strip())
        if date_match:
            d, m, y = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            save_user(user_id, birth_day=d, birth_month=m, birth_year=y, date_changes=1)
            user_sessions[user_id] = []
            ok = {"ru": "✅ Дата рождения обновлена!", "de": "✅ Geburtsdatum aktualisiert!", "en": "✅ Date of birth updated!"}
            await update.message.reply_text(ok.get(lang, ok["ru"]))
            await show_menu(context, user_id, lang)
        else:
            err = {"ru": "Напиши дату в формате ДД.ММ.ГГГГ", "de": "Bitte im Format TT.MM.JJJJ", "en": "Please use format DD.MM.YYYY"}
            await update.message.reply_text(err.get(lang, err["ru"]))
        return

    user_sessions[user_id].append({"role": "user", "content": user_text})

    # Компас: режим диалога
    if user_id in compass_state and compass_state[user_id]:
        state = compass_state[user_id]
        stage = state.get("stage", "initial")
        ctx = build_profile_context(user, lang) if user.get("day") else {}
        mi = ctx.get("mi", {})
        model_name = mi.get("name", "") if isinstance(mi, dict) else ""
        model_profile = mi.get("profile", "") if isinstance(mi, dict) else str(mi)
        model_risks = mi.get("risks", "") if isinstance(mi, dict) else ""
        lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. НИКАКИХ АНГЛИЙСКИХ СЛОВ.", "de": "ANTWORTE NUR AUF DEUTSCH. KEIN RUSSISCH.", "en": "RESPOND ONLY IN ENGLISH. NO RUSSIAN OR OTHER LANGUAGES."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")
        g = "женские окончания" if user.get("gender","f") == "f" else "мужские окончания"

        about_parts = []
        if user.get("about_work"): about_parts.append("Работа: " + user["about_work"])
        if user.get("about_finance"): about_parts.append("Финансы: " + user["about_finance"])
        if user.get("about_relations"): about_parts.append("Отношения: " + user["about_relations"])
        if user.get("about_personal"): about_parts.append("Личное: " + user["about_personal"])
        about_block = ("\n\nКОНТЕКСТ ЖИЗНИ:\n" + "\n".join(about_parts) + "\n\nВАЖНО: этот контекст — фон для понимания человека, не повестка дня. Опирайся прежде всего на энергию периода — не зацикливайся на проблемах из контекста.") if about_parts else ""
        lang_warn = {"ru": "", "de": "WICHTIG: Kontextdaten sind auf Russisch — antworte VOLLSTÄNDIG AUF DEUTSCH.", "en": "IMPORTANT: Context data is in Russian — respond ENTIRELY IN ENGLISH."}
        system_name = {"ru": "Ты ассистент Внутренний Компас.", "de": "Du bist der Assistent Innerer Kompass.", "en": "You are the Inner Compass assistant."}
        context_block = f"""Имя: {user.get('name','')}. {g}.
Модель мышления: {model_name}. {model_profile}
Риски: {model_risks}
Личный год: {ctx.get('year_text','')}
Личный месяц: {ctx.get('month_text','')}
Личный день: {ctx.get('day_text','')}{about_block}"""

        if stage == "questions":
            q_count = state.get("q_count", 0)
            topic = state.get("topic", "")

            max_q = 7
            if q_count >= max_q:
                # Анализ после вопросов
                lang_warn = {"ru": "", "de": "WICHTIG: Kontextdaten sind auf Russisch — antworte VOLLSTÄNDIG AUF DEUTSCH.", "en": "IMPORTANT: Context data is in Russian — respond ENTIRELY IN ENGLISH."}
                compass_intro = {"ru": "Ты ассистент Внутренний Компас.", "de": "Du bist der Assistent Innerer Kompass.", "en": "You are the Inner Compass assistant."}
                topic_label = {"ru": f"Тема которую человек хочет прояснить: {topic}", "de": f"Das Thema das die Person klären möchte: {topic}", "en": f"The topic the person wants to clarify: {topic}"}
                structure_label = {
                    "ru": "СТРУКТУРА: 2-3 абзаца анализа — что происходит на самом деле, как модель мышления влияет на ситуацию. Затем 3-4 конкретных рекомендации. СТИЛЬ: тёплый, глубокий, без диагнозов, как умный друг. ЗАПРЕЩЕНО: клише, оценки, markdown.",
                    "de": "STRUKTUR: 2-3 Absätze Analyse — was wirklich passiert, wie das Denkmodell die Situation beeinflusst. Dann 3-4 konkrete Empfehlungen. STIL: warm, tiefgründig, ohne Diagnosen, wie ein kluger Freund. VERBOTEN: Klischees, Bewertungen, markdown.",
                    "en": "STRUCTURE: 2-3 paragraphs of analysis — what is really happening, how the thinking model influences the situation. Then 3-4 concrete recommendations. STYLE: warm, deep, without diagnoses, like a smart friend. FORBIDDEN: clichés, judgments, markdown."
                }
                analysis_sys = f"""{lf}
{lang_warn.get(lang, "")}
{compass_intro.get(lang, compass_intro["ru"])}
{context_block}

{topic_label.get(lang, topic_label["ru"])}

{structure_label.get(lang, structure_label["ru"])}

{lf}"""

                try:
                    analysis_prompt = {"ru": "Дай полный персональный анализ этой ситуации с рекомендациями.", "de": "Gib eine vollständige persönliche Analyse dieser Situation mit Empfehlungen.", "en": "Give a full personal analysis of this situation with recommendations."}
                    analysis_messages = user_sessions[user_id] + [{"role": "user", "content": analysis_prompt.get(lang, analysis_prompt["ru"])}]
                    resp = client.messages.create(
                        model="claude-sonnet-4-6", max_tokens=1500,
                        system=analysis_sys, messages=analysis_messages
                    )
                    analysis_text = clean_text(resp.content[0].text)
                    await context.bot.send_message(user_id, analysis_text)
                    if is_trial_active(user) and not is_paid(user):
                        compass_state.pop(user_id, None)
                        user_sessions[user_id] = []
                        save_session(user_id, session=[], compass={})
                        await show_menu(context, user_id, lang)
                    else:
                        compass_state.pop(user_id, None)
                        user_sessions[user_id] = []
                        save_session(user_id, session=[], compass={})
                        await show_menu(context, user_id, lang)
                        state["stage"] = "after_analysis"
                        compass_state[user_id] = state
                        save_session(user_id, session=user_sessions[user_id], compass=compass_state.get(user_id, {}))
                    return
                except Exception as e:
                    print(f"Analysis error: {e}", flush=True)
                    err_msg = {"ru": "Произошла ошибка. Попробуй ещё раз.", "de": "Ein Fehler ist aufgetreten. Bitte versuche es erneut.", "en": "An error occurred. Please try again."}
                    await update.message.reply_text(err_msg.get(lang, err_msg["ru"]))

                compass_state.pop(user_id, None)
                user_sessions[user_id] = []
                save_session(user_id, session=[], compass={})
                await show_menu(context, user_id, lang)
                return

            # Задаём следующий вопрос
            q_intro = {"ru": f"Ты ассистент Внутренний Компас. Ведёшь диалог с {user.get('name','')}.", "de": f"Du bist der Assistent Innerer Kompass. Du führst einen Dialog mit {user.get('name','')}.", "en": f"You are the Inner Compass assistant. You are having a dialogue with {user.get('name','')}."}
            q_task = {"ru": f"Тема: {topic}. Это вопрос {q_count + 1} из {max_q}. Задай ОДИН глубокий вопрос. Сначала короткий отклик на предыдущий ответ (1 предложение), потом вопрос. Учитывай модель мышления. НЕ спрашивай готова ли к анализу. ТЫ. {g}. Тепло и без оценок. Без markdown.", "de": f"Thema: {topic}. Dies ist Frage {q_count + 1} von {max_q}. Stelle EINE tiefe Frage. Zuerst kurze Reaktion auf die letzte Antwort (1 Satz), dann die Frage. Denkmodell berücksichtigen. DU. {g}. Warm und ohne Bewertungen. Kein markdown.", "en": f"Topic: {topic}. This is question {q_count + 1} of {max_q}. Ask ONE deep question. First a short response to the previous answer (1 sentence), then the question. Consider the thinking model. YOU. {g}. Warm and without judgments. No markdown."}
            q_sys = f"""{lf}
{lang_warn.get(lang, "")}
{q_intro.get(lang, q_intro["ru"])}
{context_block}

{q_task.get(lang, q_task["ru"])}

{lf}"""

            resp = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=300,
                system=q_sys, messages=user_sessions[user_id]
            )
            reply = clean_text(resp.content[0].text)
            state["q_count"] = q_count + 1
            compass_state[user_id] = state
            user_sessions[user_id].append({"role": "assistant", "content": reply})
            save_session(user_id, session=user_sessions[user_id], compass=compass_state.get(user_id, {}))
            await update.message.reply_text(reply)
            return

        elif stage == "initial":
            # Первый ответ — тема определена
            state["topic"] = user_text
            state["stage"] = "questions"
            state["q_count"] = 0

            fq_intro = {"ru": f"Ты ассистент Внутренний Компас. Ведёшь диалог с {user.get('name','')}.", "de": f"Du bist der Assistent Innerer Kompass. Du führst einen Dialog mit {user.get('name','')}.", "en": f"You are the Inner Compass assistant. You are having a dialogue with {user.get('name','')}."}
            fq_task = {"ru": f"Человек хочет прояснить: {user_text}. Задай ПЕРВЫЙ глубокий вопрос. Сначала покажи что услышал (1 предложение), потом вопрос. Учитывай модель мышления. ТЫ. {g}. Тепло, без оценок. Без markdown.", "de": f"Die Person möchte klären: {user_text}. Stelle die ERSTE tiefe Frage. Zuerst zeige dass du gehört hast (1 Satz), dann die Frage. Denkmodell berücksichtigen. DU. {g}. Warm, ohne Bewertungen. Kein markdown.", "en": f"The person wants to clarify: {user_text}. Ask the FIRST deep question. First show you heard them (1 sentence), then the question. Consider the thinking model. YOU. {g}. Warm, without judgments. No markdown."}
            first_q_sys = f"""{lf}
{lang_warn.get(lang, "")}
{fq_intro.get(lang, fq_intro["ru"])}
{context_block}

{fq_task.get(lang, fq_task["ru"])}

{lf}"""

            resp = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=300,
                system=first_q_sys, messages=user_sessions[user_id]
            )
            reply = clean_text(resp.content[0].text)
            state["q_count"] = 1
            compass_state[user_id] = state
            user_sessions[user_id].append({"role": "assistant", "content": reply})
            save_session(user_id, session=user_sessions[user_id], compass=compass_state.get(user_id, {}))
            await update.message.reply_text(reply)
            return

        elif stage == "about":
            about_step = state.get("about_step", "work")
            if about_step == "work":
                save_user(user_id, about_work=user_text)
                state["about_step"] = "finance"
                compass_state[user_id] = state
                save_session(user_id, session=user_sessions.get(user_id, []), compass=state)
                q = {"ru": "💰 Финансы\n\nКак выглядит твоя финансовая ситуация сейчас — что стабильно, что беспокоит, к чему стремишься?", "de": "💰 Finanzen\n\nWie sieht deine finanzielle Situation gerade aus — was ist stabil, was bereitet dir Sorgen, wonach strebst du?", "en": "💰 Finances\n\nHow does your financial situation look right now — what is stable, what concerns you, what are you aiming for?"}
                skip = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить" if lang=="ru" else ("⏭️ Überspringen" if lang=="de" else "⏭️ Skip"), callback_data="about_skip_step")]])
                await update.message.reply_text(q.get(lang, q["ru"]), reply_markup=skip)
            elif about_step == "finance":
                save_user(user_id, about_finance=user_text)
                state["about_step"] = "relations"
                compass_state[user_id] = state
                save_session(user_id, session=user_sessions.get(user_id, []), compass=state)
                q = {"ru": "💑 Отношения\n\nРасскажи о своих отношениях — партнёр, семья, друзья, коллеги. Что сейчас радует и что даётся непросто в общении с близкими и окружением?", "de": "💑 Beziehungen\n\nErzähl mir von deinen Beziehungen — Partner, Familie, Freunde, Kollegen. Was freut dich gerade und was fällt schwer im Umgang mit deinem Umfeld?", "en": "💑 Relationships\n\nTell me about your relationships — partner, family, friends, colleagues. What makes you happy right now and what is challenging in your interactions with others?"}
                skip = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить" if lang=="ru" else ("⏭️ Überspringen" if lang=="de" else "⏭️ Skip"), callback_data="about_skip_step")]])
                await update.message.reply_text(q.get(lang, q["ru"]), reply_markup=skip)
            elif about_step == "relations":
                save_user(user_id, about_relations=user_text)
                state["about_step"] = "personal"
                compass_state[user_id] = state
                save_session(user_id, session=user_sessions.get(user_id, []), compass=state)
                q = {"ru": "🌱 Личное\n\nЕсть ли что-то важное что сейчас занимает твои мысли больше всего — цель, вопрос, ситуация?", "de": "🌱 Persönliches\n\nGibt es etwas Wichtiges das dich gerade am meisten beschäftigt — ein Ziel, eine Frage, eine Situation?", "en": "🌱 Personal\n\nIs there something important that occupies your mind the most right now — a goal, a question, a situation?"}
                skip = InlineKeyboardMarkup([[InlineKeyboardButton("⏭️ Пропустить" if lang=="ru" else ("⏭️ Überspringen" if lang=="de" else "⏭️ Skip"), callback_data="about_skip_step")]])
                await update.message.reply_text(q.get(lang, q["ru"]), reply_markup=skip)
            elif about_step == "personal":
                save_user(user_id, about_personal=user_text)
                compass_state.pop(user_id, None)
                save_session(user_id, session=[], compass={})
                thanks = {"ru": "Спасибо! Теперь Alvalori знает тебя лучше и будет давать более точный анализ 🌟", "de": "Danke! Jetzt kennt Alvalori dich besser und wird genauere Analysen erstellen 🌟", "en": "Thank you! Now Alvalori knows you better and will provide more accurate analysis 🌟"}
                await update.message.reply_text(thanks.get(lang, thanks["ru"]))
                await show_menu(context, user_id, lang)
            return

        elif stage == "churn_answer":
            reason = state.get("reason", "")
            gender = user.get("gender", "f") if user else "f"
            await context.bot.send_message(ADMIN_ID, "📊 Опрос от " + str(user.get("name")) + " (" + lang + "). Причина: " + reason + "\nОтвет: " + user_text)
            thanks = {
                "ru": "Спасибо, " + str(user.get("name","")) + "! Это очень помогает развитию Alvalori 🙏\n\nКогда буд" + ("ешь готова" if gender=="f" else "ешь готов") + " — возвращайся 🌟",
                "de": "Danke, " + str(user.get("name","")) + "! Das hilft sehr bei der Entwicklung von Alvalori 🙏\n\nWenn du bereit bist — komm wieder 🌟",
                "en": "Thank you, " + str(user.get("name","")) + "! This really helps Alvalori development 🙏\n\nWhen you're ready — come back 🌟",
            }
            compass_state.pop(user_id, None)
            save_session(user_id, session=[], compass={})
            await update.message.reply_text(thanks.get(lang, thanks["ru"]))
            await show_menu(context, user_id, lang)
            return

        elif stage == "feedback":
            # Один ответ — сразу администратору
            await context.bot.send_message(ADMIN_ID, "📝 Отзыв от " + str(user.get("name")) + " (" + lang + "):\n\n" + user_text)
            thanks = {"ru": "Спасибо большое! Твоё мнение очень важно для развития Alvalori 🙏🌟", "de": "Vielen Dank! Deine Meinung ist sehr wichtig für die Entwicklung von Alvalori 🙏🌟", "en": "Thank you so much! Your opinion is very important for the development of Alvalori 🙏🌟"}
            compass_state.pop(user_id, None)
            save_session(user_id, session=[], compass={})
            await update.message.reply_text(thanks.get(lang, thanks["ru"]))
            await show_menu(context, user_id, lang)
            return

        elif stage == "clarify":
            # Уточняющие вопросы после "нет"
            clarify_count = state.get("clarify_count", 0)

            # Один ответ на уточнение — сразу финальный анализ
            final_sys = f"""{lf}
Ты ассистент Внутренний Компас.
{context_block}
Человек уточнил что именно не понятно: "{user_text}"
На основе всего разговора и этого уточнения дай развёрнутый анализ — что стало яснее, и 2-3 практических шага.
Тепло, без оценок. ТЫ. {g}."""
            resp_final = client.messages.create(
                model="claude-sonnet-4-6", max_tokens=800,
                system=final_sys, messages=user_sessions[user_id]
            )
            await update.message.reply_text(clean_text(resp_final.content[0].text))
            compass_state.pop(user_id, None)
            user_sessions[user_id] = []
            save_session(user_id, session=[], compass={})
            await show_menu(context, user_id, lang)
            return


    # Изменение имени
    if user_sessions.get(user_id) and user_sessions[user_id] and user_sessions[user_id][0].get("content") == "change_name":
        save_user(user_id, name=user_text.strip(), name_changes=1)
        user_sessions[user_id] = []
        ok = {"ru": f"✅ Имя обновлено на {user_text.strip()}!", "de": f"✅ Name auf {user_text.strip()} aktualisiert!", "en": f"✅ Name updated to {user_text.strip()}!"}
        await update.message.reply_text(ok.get(lang, ok["ru"]))
        await show_menu(context, user_id, lang)
        return

    # Изменение даты рождения
    if user_sessions.get(user_id) and user_sessions[user_id] and user_sessions[user_id][0].get("content") == "change_date":
        date_match = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", user_text.strip())
        if date_match:
            d, m, y = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
            save_user(user_id, birth_day=d, birth_month=m, birth_year=y, date_changes=1)
            user_sessions[user_id] = []
            ok = {"ru": "✅ Дата рождения обновлена!", "de": "✅ Geburtsdatum aktualisiert!", "en": "✅ Date of birth updated!"}
            await update.message.reply_text(ok.get(lang, ok["ru"]))
            await show_menu(context, user_id, lang)
        else:
            err = {"ru": "Напиши дату в формате ДД.ММ.ГГГГ", "de": "Bitte im Format TT.MM.JJJJ", "en": "Please use format DD.MM.YYYY"}
            await update.message.reply_text(err.get(lang, err["ru"]))
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
            "de": [InlineKeyboardButton("👩 Weiblich", callback_data="gender_f"), InlineKeyboardButton("👨 Männlich", callback_data="gender_m")],
            "en": [InlineKeyboardButton("👩 She/her", callback_data="gender_f"), InlineKeyboardButton("👨 He/him", callback_data="gender_m")],
        }
        await update.message.reply_text(gender_q.get(lang, gender_q["ru"]), reply_markup=InlineKeyboardMarkup([gender_btns.get(lang, gender_btns["ru"])]))
        return

    # Дата рождения
    if not user.get("day"):
        date_m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", user_text)
        if date_m:
            tg_username = update.effective_user.username or ""
            save_user(user_id, birth_day=int(date_m.group(1)), birth_month=int(date_m.group(2)), birth_year=int(date_m.group(3)), trial_started_at=datetime.datetime.now(), username=tg_username)
            # Уведомление админу
            try:
                tg_name = update.effective_user.username or update.effective_user.first_name or "?"
                reg_name = user_sessions[user_id][0]["content"] if user_sessions.get(user_id) else "?"
                await context.bot.send_message(ADMIN_ID, f"🆕 Новый пользователь:\nИмя: {reg_name}\nTelegram: @{tg_name}\nID: {user_id}\nЯзык: {lang}")
            except:
                pass
            welcome_info = {
                "ru": "🌿 У тебя есть 72 часа бесплатного доступа.\n\nКаждый раздел можно открыть 1 раз за весь пробный период:\n• 🌟 Основа моей личности\n• 🧭 Личный год\n• 📍 Личный месяц\n• ☀️ Личный день\n• 💡 Поговорим о главном\n\n💎 С подпиской все разделы открываются без ограничений — включая индивидуальный обзор в начале каждого месяца.",
                "de": "🌿 Du hast 72 Stunden kostenlosen Zugang.\n\nJeden Bereich kannst du einmal im Testzeitraum öffnen:\n• 🌟 Meine Persönlichkeit\n• 🧭 Persönliches Jahr\n• 📍 Persönlicher Monat\n• ☀️ Persönlicher Tag\n• 💡 Lass uns reden\n\n💎 Mit dem Abonnement sind alle Bereiche ohne Einschränkungen verfügbar — einschließlich einer individuellen Monatsübersicht.",
                "en": "🌿 You have 72 hours of free access.\n\nEach section can be opened once during the trial period:\n• 🌟 My Personality\n• 🧭 Personal Year\n• 📍 Personal Month\n• ☀️ Personal Day\n• 💡 Let's talk\n\n💎 With a subscription all sections are available without limits — including an individual monthly overview."
            }
            sub_btns = InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 Подписка" if lang=="ru" else ("💳 Abonnement" if lang=="de" else "💳 Subscription"), callback_data="btn_pay"),
                InlineKeyboardButton("◄️ Не сейчас" if lang=="ru" else ("◄️ Nicht jetzt" if lang=="de" else "◄️ Not now"), callback_data="trial_not_now")
            ]])
            await update.message.reply_text(welcome_info.get(lang, welcome_info["ru"]), reply_markup=sub_btns)
        else:
            err = {"ru": "Напиши дату в формате ДД.ММ.ГГГГ", "de": "Schreibe das Datum im Format TT.MM.JJJJ", "en": "Write the date in format DD.MM.YYYY"}
            await update.message.reply_text(err.get(lang, err["ru"]))
        return

    # Если нет активной сессии — показываем меню
    await show_menu(context, user_id, lang)

async def my_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user.get("name"):
        await update.message.reply_text("Сначала зарегистрируйся — напиши /start")
        return
    lang = user.get("lang", "ru")
    name = user.get("name", "")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users WHERE referred_by=%s", (user_id,))
    count = cur.fetchone()[0]
    cur.close()
    conn.close()
    link = f"https://t.me/alvalori_bot?start=ref_{name}"
    msgs = {
        "ru": f"Твоя реферальная ссылка:\n{link}\n\nПо ней зарегистрировалось: {count} чел.",
        "de": f"Dein Empfehlungslink:\n{link}\n\nRegistriert über deinen Link: {count} Personen.",
        "en": f"Your referral link:\n{link}\n\nRegistered via your link: {count} people."
    }
    await update.message.reply_text(msgs.get(lang, msgs["ru"]))

async def admin_refs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT u.name, u.user_id, r.name, r.user_id FROM users u JOIN users r ON u.referred_by = r.user_id WHERE u.referred_by IS NOT NULL ORDER BY r.name")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        await update.message.reply_text("Рефералов пока нет.")
        return
    result = "Реферальная статистика:\n"
    current_ref = None
    for name, uid, ref_name, ref_id in rows:
        if ref_name != current_ref:
            current_ref = ref_name
            result += "\n" + str(ref_name) + " (" + str(ref_id) + "):\n"
        result += "  -> " + str(name) + " (" + str(uid) + ")\n"
    await update.message.reply_text(result[:4000])

async def admin_makeref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /makeref Имя")
        return
    name = " ".join(context.args)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE name ILIKE %s LIMIT 1", (name,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        link = "https://t.me/alvalori_bot?start=refid_" + str(row[0])
        await update.message.reply_text("Реферальная ссылка для " + name + ":\n" + link)
    else:
        await update.message.reply_text("Пользователь " + name + " не найден в базе.")

async def admin_news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    if not context.args:
        await update.message.reply_text("Использование: /news Текст новости")
        return
    text_ru = " ".join(context.args)
    await update.message.reply_text("Перевожу и рассылаю...")
    # Переводим через Claude
    try:
        resp_de = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=500,
            system="Переведи текст на немецкий язык. Только перевод без пояснений.",
            messages=[{"role": "user", "content": text_ru}]
        )
        text_de = resp_de.content[0].text.strip()
        resp_en = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=500,
            system="Переведи текст на английский язык. Только перевод без пояснений.",
            messages=[{"role": "user", "content": text_ru}]
        )
        text_en = resp_en.content[0].text.strip()
    except Exception as e:
        await update.message.reply_text(f"Ошибка перевода: {e}")
        return
    # Рассылаем
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, lang FROM users WHERE agreed=TRUE")
    users = cur.fetchall()
    cur.close()
    conn.close()
    sent = 0
    errors = 0
    failed = []
    texts = {"ru": text_ru, "de": text_de, "en": text_en}
    for uid, lang in users:
        try:
            await context.bot.send_message(uid, texts.get(lang, text_ru))
            sent += 1
            await asyncio.sleep(0.5)
        except Exception:
            errors += 1
    result = "Отправлено: " + str(sent) + ", ошибок: " + str(errors)
    if failed:
        conn2 = get_db()
        cur2 = conn2.cursor()
        cur2.execute("SELECT user_id, name FROM users WHERE user_id = ANY(%s)", (failed,))
        failed_users = cur2.fetchall()
        cur2.close()
        conn2.close()
        failed_names = [str(name) + " (" + str(uid) + ")" for uid, name in failed_users]
        result += "\nНе доставлено:\n" + "\n".join(failed_names)
    await update.message.reply_text(result)

async def admin_announce_nav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, lang FROM users WHERE agreed=TRUE AND birth_day IS NOT NULL")
    users = cur.fetchall()
    cur.close()
    conn.close()
    msg_ru = "🌟 Alvalori обновился!\n\nВ меню появилась новая кнопка — ℹ️ Знакомство с Alvalori.\n\nТам коротко описано для чего каждый раздел и что находится в настройках. Если ещё не разобрался(лась) как всё устроено — загляни туда."
    msg_de = "🌟 Alvalori wurde aktualisiert!\n\nIm Menü gibt es eine neue Schaltfläche — ℹ️ Alvalori kennenlernen.\n\nDort wird kurz beschrieben, wofür jeder Bereich da ist. Schau gerne rein."
    msg_en = "🌟 Alvalori has been updated!\n\nA new button appeared in the menu — ℹ️ Get to know Alvalori.\n\nIt briefly describes what each section is for. Feel free to check it out."
    msgs = {"ru": msg_ru, "de": msg_de, "en": msg_en}
    sent = 0
    failed = []
    for uid, name, lang in users:
        try:
            btns = InlineKeyboardMarkup([[
                InlineKeyboardButton("ℹ️ Открыть" if lang=="ru" else ("ℹ️ Öffnen" if lang=="de" else "ℹ️ Open"), callback_data="announce_nav_open"),
                InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="announce_nav_no")
            ]])
            await context.bot.send_message(uid, msgs.get(lang, msg_ru), reply_markup=btns)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            failed.append(str(uid))
            print(f"announce_nav error {uid}: {e}", flush=True)
    result = f"✅ Отправлено: {sent}\nОшибок: {len(failed)}"
    if failed:
        result += "\nНе доставлено ID: " + ", ".join(failed)
    await update.message.reply_text(result)


async def admin_announce_about(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""SELECT user_id, name, lang FROM users
        WHERE agreed=TRUE AND birth_day IS NOT NULL
        AND (about_work IS NULL AND about_finance IS NULL AND about_relations IS NULL AND about_personal IS NULL)""")
    users = cur.fetchall()
    cur.close()
    conn.close()
    msg_ru = "👤 В Alvalori появился новый раздел — Обо мне.\n\nЧем больше Alvalori знает о тебе — тем глубже и точнее становится каждый анализ. Не общие слова, а то что актуально именно для тебя прямо сейчас.\n\n🔒 Эти данные используются только для твоего анализа. Посмотреть и изменить их можешь только ты.\n\nХочешь попробовать?"
    msg_de = "👤 In Alvalori gibt es einen neuen Bereich — Über mich.\n\nJe mehr Alvalori über dich weiß — desto tiefer und präziser wird jede Analyse. Nicht allgemeine Worte, sondern was gerade für dich relevant ist.\n\n🔒 Diese Daten werden nur für deine Analyse verwendet. Ansehen und ändern kannst nur du.\n\nMöchtest du es ausprobieren?"
    msg_en = "👤 A new section appeared in Alvalori — About me.\n\nThe more Alvalori knows about you — the deeper and more precise each analysis becomes. Not general words, but what is relevant for you right now.\n\n🔒 This data is used only for your analysis. Only you can view and change it.\n\nWant to try?"
    msgs = {"ru": msg_ru, "de": msg_de, "en": msg_en}
    sent = 0
    failed = []
    for uid, name, lang in users:
        try:
            btns = InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Да, заполнить" if lang=="ru" else ("✅ Ja, ausfüllen" if lang=="de" else "✅ Yes, fill in"), callback_data="about_start"),
                InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="about_announce_skip")
            ]])
            await context.bot.send_message(uid, msgs.get(lang, msg_ru), reply_markup=btns)
            sent += 1
            await asyncio.sleep(0.5)
        except Exception as e:
            failed.append(str(uid))
            print(f"announce_about error {uid}: {e}", flush=True)
    result = f"✅ Отправлено: {sent}\nОшибок: {len(failed)}"
    if failed:
        result += "\nНе доставлено ID: " + ", ".join(failed)
    await update.message.reply_text(result)


async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    import datetime
    conn = get_db()
    cur = conn.cursor()

    # Пользователи
    cur.execute("SELECT COUNT(*) FROM users WHERE agreed=TRUE AND birth_day IS NOT NULL")
    total_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE paid_until > NOW()")
    paid_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE trial_started_at IS NOT NULL AND paid_until IS NULL OR paid_until < NOW()")
    trial_users = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users WHERE about_work IS NOT NULL OR about_finance IS NOT NULL")
    about_filled = cur.fetchone()[0]

    # Статистика за 30 дней
    cur.execute("SELECT section, COUNT(*) FROM analytics WHERE created_at > NOW() - INTERVAL '30 days' GROUP BY section ORDER BY COUNT(*) DESC")
    sections_30 = cur.fetchall()

    # Статистика за всё время
    cur.execute("SELECT section, COUNT(*) FROM analytics GROUP BY section ORDER BY COUNT(*) DESC")
    sections_all = cur.fetchall()

    # Активность по месяцам
    cur.execute("""SELECT TO_CHAR(created_at, 'MM.YYYY') as month, COUNT(DISTINCT user_id) as active_users, COUNT(*) as actions
        FROM analytics GROUP BY month ORDER BY month DESC LIMIT 6""")
    monthly = cur.fetchall()

    # Финансы
    cur.execute("SELECT COUNT(*), SUM(EXTRACT(EPOCH FROM (paid_until - NOW()))/86400) FROM users WHERE paid_until > NOW()")
    paid_data = cur.fetchone()
    active_paid = paid_data[0] or 0

    cur.execute("SELECT COUNT(*) FROM analytics WHERE created_at > NOW() - INTERVAL '30 days'")
    actions_30 = cur.fetchone()[0]

    cur.close()
    conn.close()

    EUR_RATE = 0.858
    PRICE = 15.0
    API_PER_PAID = 3.0
    API_PER_USER = 0.5
    HOSTING = 7.0

    revenue_eur = active_paid * PRICE
    api_cost_eur = active_paid * API_PER_PAID + total_users * API_PER_USER
    total_cost_eur = api_cost_eur + HOSTING
    profit_eur = revenue_eur - total_cost_eur
    revenue_usd = revenue_eur / EUR_RATE
    api_cost_usd = api_cost_eur / EUR_RATE
    profit_usd = profit_eur / EUR_RATE

    section_names = {
        "me": "🌟 Grundlage meiner Persönlichkeit",
        "year": "🧭 Persönliches Jahr",
        "month": "📍 Persönlicher Monat",
        "day": "☀️ Persönlicher Tag",
        "compass": "💡 Lass uns reden",
        "about": "👤 Über mich",
    }

    msg = "📊 <b>ALVALORI — Statistik & Finanzen</b>\n"
    msg += f"Stand: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}\n\n"

    msg += "👥 <b>Benutzer</b>\n"
    msg += f"Gesamt: {total_users}\n"
    msg += f"Aktive Abonnenten: {active_paid}\n"
    msg += f"Testnutzer: {trial_users}\n"
    msg += f"Konversionsrate: {round(active_paid/total_users*100,1) if total_users > 0 else 0}%\n"
    msg += f"Profil ausgefüllt (Über mich): {about_filled}\n\n"

    msg += "📈 <b>Beliebtheit der Bereiche (letzte 30 Tage)</b>\n"
    for section, cnt in sections_30:
        name = section_names.get(section, section)
        msg += f"{name}: {cnt}×\n"

    msg += "\n📅 <b>Monatliche Aktivität</b>\n"
    for month, active, actions in monthly:
        msg += f"{month}: {active} aktive Nutzer, {actions} Aktionen\n"

    msg += "\n📊 <b>Gesamtstatistik (alle Zeit)</b>\n"
    for section, cnt in sections_all:
        name = section_names.get(section, section)
        msg += f"{name}: {cnt}×\n"

    msg += f"\n💰 <b>Finanzen (aktueller Monat, Schätzung)</b>\n"
    msg += f"Einnahmen: {revenue_eur:.2f}€ / ${revenue_usd:.2f}\n"
    msg += f"API-Kosten: {api_cost_eur:.2f}€ / ${api_cost_usd:.2f}\n"
    msg += f"Hosting: {HOSTING:.2f}€\n"
    msg += f"Gesamtkosten: {total_cost_eur:.2f}€\n"
    msg += f"<b>Nettogewinn: {profit_eur:.2f}€ / ${profit_usd:.2f}</b>\n"
    msg += f"Marge: {round(profit_eur/revenue_eur*100,1) if revenue_eur > 0 else 0}%"

    await update.message.reply_text(msg, parse_mode="HTML")


async def admin_send_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    await update.message.reply_text("Отправляю обзор месяца...")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, birth_day, birth_month, birth_year, lang, gender, trial_started_at, paid_until, remind_at, about_work, about_finance, about_relations, about_personal FROM users WHERE birth_day IS NOT NULL AND agreed=TRUE")
    users_list = cur.fetchall()
    cur.close()
    conn.close()
    today = dt2.datetime.now()
    sent = 0
    errors = 0
    for row in users_list:
        uid, name, day, month, year, lang, gender, trial_started_at, paid_until, remind_at, *_ = row
        if not day:
            continue
        user_obj = {"name": name, "day": day, "month": month, "year": year, "lang": lang, "gender": gender or "f"}
        if not (is_trial_active(user_obj) or (paid_until and paid_until.replace(tzinfo=None) > today)):
            continue
        try:
            ctx = build_profile_context(user_obj, lang)
            lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")
            g = "женские окончания" if (gender or "f") == "f" else "мужские окончания"
            month_sys = f"""{lf}
Ты ассистент Alvalori. Сегодня 1-е число — начало нового месяца.
Имя: {name}. {g}.
Модель мышления: {ctx["mi"]}
Личный месяц: {ctx["month_text"]}
Личный год: {ctx["year_text"][:200]}

Напиши обзор месяца в трёх блоках:
1. О чём этот месяц — 2-3 предложения через призму модели мышления и личного месяца.
2. 4-5 рекомендаций — как эффективно прожить этот месяц. Каждая рекомендация — отдельный абзац, конкретно.
3. 4-5 рисков — что может мешать. Каждый риск — отдельный абзац, мягко.
ЗАПРЕЩЕНО: клише, упоминание чисел периодов, markdown звёздочки.
ТЫ. {g}. Тепло и конкретно."""
            resp = call_claude("claude-sonnet-4-6", 1200, month_sys, [{"role": "user", "content": "Напиши обзор месяца"}])
            header = {"ru": "🗓 Обзор месяца", "de": "🗓 Monatsübersicht", "en": "🗓 Monthly Overview"}
            await context.bot.send_message(uid, header.get(lang, header["ru"]))
            await context.bot.send_message(uid, clean_text(resp.content[0].text))
            sent += 1
        except Exception as e:
            print(f"Monthly manual error {uid}: {e}", flush=True)
            errors += 1
    await update.message.reply_text(f"✅ Отправлено: {sent}, ошибок: {errors}")

async def admin_active(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    import datetime as dt2
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, lang, trial_started_at, paid_until FROM users WHERE birth_day IS NOT NULL AND agreed=TRUE ORDER BY trial_started_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    now = dt2.datetime.now()
    lines = []
    paid_count = 0
    trial_count = 0
    for uid, name, lang, trial_started_at, paid_until in rows:
        user_obj = {"trial_started_at": trial_started_at, "paid_until": paid_until}
        if paid_until and paid_until.replace(tzinfo=None) > now:
            status = f"Платный до {paid_until.strftime('%d.%m.%Y')}"
            paid_count += 1
        elif trial_started_at and (now - trial_started_at.replace(tzinfo=None)).total_seconds() < 259200:
            hours_left = round(72 - (now - trial_started_at.replace(tzinfo=None)).total_seconds() / 3600)
            status = f"Триал {hours_left}ч"
            trial_count += 1
        else:
            continue
        lines.append(f"{name} ({lang}) — {status} — {uid}")
    total = paid_count + trial_count
    header = f"✅ Активных: {total} (платных: {paid_count}, триал: {trial_count})\n\n"
    msg = header + "\n".join(lines) if lines else header + "Нет активных пользователей"
    await update.message.reply_text(msg[:4000])

async def admin_export(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT u.user_id, u.name, u.lang, u.birth_day, u.birth_month, u.birth_year, u.trial_started_at, u.paid_until, u.username FROM users u ORDER BY u.trial_started_at DESC")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    now = datetime.datetime.now()
    lines = ["ID | Имя | Язык | Дата рождения | Регистрация | Статус\n" + "="*70]
    for r in rows:
        uid, name, lang, d, m, y, trial, paid, uname = r
        dob = f"{d:02d}.{m:02d}.{y}" if d else "—"
        reg = trial.strftime("%d.%m.%Y %H:%M") if trial else "—"
        uname = "@" + uname if uname else "—"
        if paid and paid.replace(tzinfo=None) > now:
            status = f"Платный до {paid.strftime('%d.%m.%Y')}"
        elif trial and (now - trial.replace(tzinfo=None)).total_seconds() < 259200:
            status = "Пробный (активен)"
        else:
            status = "Истёк"
        lines.append(f"{uid} | {name} | {uname} | {lang} | {dob} | {reg} | {status}")
    text = "\n".join(lines)
    with open("/tmp/users_export.txt", "w", encoding="utf-8") as f:
        f.write(text)
    await update.message.reply_document(document=open("/tmp/users_export.txt", "rb"), filename="users.txt")

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("day"):
        await update.message.reply_text("🌟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🌟 Основа моей личности", callback_data="btn_me")]]))
    else:
        await update.message.reply_text("Напиши /start")

async def cmd_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("day"):
        await update.message.reply_text("🧭", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🧭 Личный год", callback_data="btn_year")]]))
    else:
        await update.message.reply_text("Напиши /start")

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("day"):
        await update.message.reply_text("📍", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📍 Личный месяц", callback_data="btn_month")]]))
    else:
        await update.message.reply_text("Напиши /start")

async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("day"):
        await update.message.reply_text("☀️", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("☀️ Личный день", callback_data="btn_day")]]))
    else:
        await update.message.reply_text("Напиши /start")

async def cmd_compass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("day"):
        await update.message.reply_text("💡", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💡 Поговорим о главном", callback_data="btn_compass")]]))
    else:
        await update.message.reply_text("Напиши /start")

async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")],
    ]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT user_id, name, lang, trial_started_at, paid_until FROM users ORDER BY trial_started_at DESC LIMIT 100')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        await update.message.reply_text('Пользователей нет.')
        return
    text = "Пользователи:\n\n"
    now = datetime.datetime.now()
    for row in rows:
        uid, name, lang, trial, paid = row
        expires = trial.replace(tzinfo=None) + datetime.timedelta(hours=72) if trial else None
        if paid and paid.replace(tzinfo=None) > now:
            status = f"Платный до {paid.strftime('%d.%m.%Y')}"
        elif expires and expires > now:
            status = f"Пробный до {expires.strftime('%d.%m %H:%M')}"
        else:
            status = f"Истёк {expires.strftime('%d.%m.%Y') if expires else '—'}"
        reg = trial.strftime('%d.%m.%Y') if trial else '—'
        text += f"{name} ({lang}) — {uid} — {status} — рег. {reg}\n"
    # Разбиваем на части если длинно
    if len(text) <= 4000:
        await update.message.reply_text(text)
    else:
        lines = text.split("\n")
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 4000:
                await update.message.reply_text(chunk)
                chunk = line + "\n"
            else:
                chunk += line + "\n"
        if chunk:
            await update.message.reply_text(chunk)

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
    await update.message.reply_text(f"OK: доступ для {target_id} на {days} дней до {paid_until.strftime('%d.%m.%Y %H:%M')}.")
    # Уведомление пользователю
    target_user = get_user(target_id)
    if target_user:
        target_lang = target_user.get("lang", "ru")
        notify_msg = {
            "ru": f"✅ Доступ открыт до {paid_until.strftime('%d.%m.%Y')} 🌟",
            "de": f"✅ Zugang geöffnet bis {paid_until.strftime('%d.%m.%Y')} 🌟",
            "en": f"✅ Access open until {paid_until.strftime('%d.%m.%Y')} 🌟"
        }
        try:
            await context.bot.send_message(target_id, notify_msg.get(target_lang, notify_msg["ru"]))
            await context.bot.send_message(target_id, {"ru": "Выбери раздел:", "de": "Wähle einen Bereich:", "en": "Choose a section:"}.get(target_lang, "Выбери раздел:"), reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(target_lang, MENU_BUTTONS["ru"])))
        except:
            pass

def is_last_sunday_of_month(dt):
    # Проверяем что сегодня воскресенье
    if dt.weekday() != 6:
        return False
    # Проверяем что следующее воскресенье уже в следующем месяце
    next_sunday = dt + datetime.timedelta(days=7)
    return next_sunday.month != dt.month

async def send_feedback_request(context):
    if not is_last_sunday_of_month(datetime.datetime.now()):
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, lang FROM users WHERE agreed=TRUE AND birth_day IS NOT NULL")
    users_list = cur.fetchall()
    cur.close()
    conn.close()
    for row in users_list:
        uid, name, lang = row
        feedback_msg = {
            "ru": f"Привет, {name}! Ты попробовал(а) Alvalori. Чтобы он становился лучше — мне важно твоё мнение. Буду признательна если найдёшь 2 минуты и ответишь на 3 вопроса 🙏",
            "de": f"Hallo, {name}! Du hast Alvalori ausprobiert. Damit er besser wird — ist mir deine Meinung wichtig. Ich wäre dankbar wenn du 2 Minuten findest und 3 Fragen beantwortest 🙏",
            "en": f"Hi, {name}! You've tried Alvalori. To make it better — your opinion matters to me. I'd be grateful if you find 2 minutes to answer 3 questions 🙏"
        }
        btns = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Да" if lang=="ru" else ("✅ Ja" if lang=="de" else "✅ Yes"), callback_data="feedback_yes"),
            InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="feedback_no")
        ]])
        try:
            await context.bot.send_message(uid, feedback_msg.get(lang, feedback_msg["ru"]), reply_markup=btns)
            await asyncio.sleep(1)
        except Exception as e:
            print(f"Feedback error {uid}: {e}", flush=True)

async def send_birthday_messages(context):
    import datetime as dt2
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, birth_day, birth_month, birth_year, lang, gender FROM users WHERE birth_day IS NOT NULL AND agreed=TRUE")
    users_list = cur.fetchall()
    cur.close()
    conn.close()
    today = dt2.datetime.now()
    for row in users_list:
        uid, name, day, month, year, lang, gender = row[:7]
        if not day:
            continue
        if day == today.day and month == today.month:
            bday_text = {
                "ru": f"🎂 С днём рождения, {name}!\n\nПусть этот день принесёт тебе радость, тепло и всё что ты хочешь.",
                "de": f"🎂 Herzlichen Glückwunsch zum Geburtstag, {name}!\n\nMöge dieser Tag dir Freude, Wärme und alles bringen, was du dir wünschst.",
                "en": f"🎂 Happy Birthday, {name}!\n\nMay this day bring you joy, warmth and everything you wish for."
            }
            try:
                await context.bot.send_animation(uid, animation="CgACAgIAAxkDAAIQtmoV2C1332tJt-TwcooI1sFi1CDQAAKAmQACbEiwSFxl2P1fOg9kOwQ")
                await context.bot.send_message(uid, bday_text.get(lang, bday_text["ru"]))
                user_obj = {"name": name, "day": day, "month": month, "year": year, "lang": lang, "gender": gender or "f"}
                ctx = build_profile_context(user_obj, lang)
                lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")
                g = "женские окончания" if (gender or "f") == "f" else "мужские окончания"
                bday_sys = f"""{lf}
Ты ассистент Alvalori. День рождения {name}.
Модель мышления: {ctx["mi"]}

Напиши ровно 3 предложения по шаблону — меняй только качества:
1. "[Одно сильное качество этого человека образно и точно, начни с Ты] — и это редкий дар."
2. "[Второе сильное качество, начни с Твоя/Твоё/Твой] заслуживает уважения."
3. "Пусть рядом с тобой будут те кто ценит тебя."

ЗАПРЕЩЕНО: слова "год", "день", "сегодня", "время", "проект", "успех", "достижение", "суперсила", "рождена чтобы".
ТЫ. {g}. Без markdown."""
                resp = client.messages.create(model="claude-sonnet-4-6", max_tokens=200, system=bday_sys, messages=[{"role": "user", "content": "Поздравь"}])
                await context.bot.send_message(uid, clean_text(resp.content[0].text))
            except Exception as e:
                print(f"Birthday error {uid}: {e}", flush=True)

async def send_monthly_overview(context):
    import datetime as dt2
    today = dt2.datetime.now()
    if today.day != 1:
        return
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, birth_day, birth_month, birth_year, lang, gender, trial_started_at, paid_until, about_work, about_finance, about_relations, about_personal FROM users WHERE birth_day IS NOT NULL AND agreed=TRUE")
    users_list = cur.fetchall()
    cur.close()
    conn.close()
    for row in users_list:
        uid, name, day, month, year, lang, gender, trial_started_at, paid_until = row[:9]
        about_work = row[9] if len(row) > 9 else None
        about_finance = row[10] if len(row) > 10 else None
        about_relations = row[11] if len(row) > 11 else None
        about_personal = row[12] if len(row) > 12 else None
        if not day:
            continue
        user_obj = {"name": name, "day": day, "month": month, "year": year, "lang": lang, "gender": gender or "f", "trial_started_at": trial_started_at, "paid_until": paid_until}
        if not (is_trial_active(user_obj) or (paid_until and paid_until.replace(tzinfo=None) > today)):
            continue
        try:
            import data as D
            m = D.get_model(day)
            mi = D.MODELS.get(m, {})
            py = D.get_year(day, month, today.year)
            pm = D.get_month(py, today.month)
            year_text = D.YEARS.get(py, "")
            month_text = D.MONTHS.get(pm, "")
            model_name = mi.get("name", "") if isinstance(mi, dict) else ""
            model_profile = mi.get("profile", "") if isinstance(mi, dict) else ""
            model_strengths = mi.get("strengths", "") if isinstance(mi, dict) else ""
            model_risks_text = mi.get("risks", "") if isinstance(mi, dict) else ""
            lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")
            g = "женские окончания" if (gender or "f") == "f" else "мужские окончания"
            about_parts = []
            if about_work: about_parts.append("Работа: " + about_work)
            if about_finance: about_parts.append("Финансы: " + about_finance)
            if about_relations: about_parts.append("Отношения: " + about_relations)
            if about_personal: about_parts.append("Личное: " + about_personal)
            about_block = ("\n\nКОНТЕКСТ ЖИЗНИ:\n" + "\n".join(about_parts) + "\n\nВАЖНО: этот контекст — фон для понимания человека, не повестка дня. Опирайся прежде всего на энергию периода — не зацикливайся на проблемах из контекста.") if about_parts else ""
            month_names = {"ru": ["январе","феврале","марте","апреле","мае","июне","июле","августе","сентябре","октябре","ноябре","декабре"],
                          "de": ["Januar","Februar","März","April","Mai","Juni","Juli","August","September","Oktober","November","Dezember"],
                          "en": ["January","February","March","April","May","June","July","August","September","October","November","December"]}
            month_name = month_names.get(lang, month_names["ru"])[today.month - 1]
            month_sys = f"""{lf}
Ты ассистент Alvalori. Пишешь обзор на этот месяц для {name}.
{g}. День рождения: {day}.

МОДЕЛЬ МЫШЛЕНИЯ: {model_name}
{model_profile}
Сильные стороны: {model_strengths}
Риски модели: {model_risks_text}

ЛИЧНЫЙ ГОД — главный вектор:
{year_text}

ЛИЧНЫЙ МЕСЯЦ — тактика:
{month_text}{about_block}

Напиши обзор месяца в трёх блоках:
1. О чём этот месяц — 2-3 предложения через призму модели мышления и личного месяца.
2. 3-4 рекомендации — конкретно, каждая отдельным абзацем.
3. 3-4 момента на которые стоит обратить внимание — мягко, каждый отдельным абзацем.

ЗАПРЕЩЕНО: клише, числа периодов, markdown звёздочки, вопросы в конце, поучительный тон.
ТЫ. {g}. Тепло и конкретно."""
            resp = client.messages.create(model="claude-sonnet-4-6", max_tokens=1500, system=month_sys, messages=[{"role": "user", "content": "Напиши обзор"}])
            header = {"ru": f"🗓 Обзор на этот месяц", "de": f"🗓 Monatsübersicht", "en": f"🗓 Monthly Overview"}
            await context.bot.send_message(uid, header.get(lang, header["ru"]))
            await context.bot.send_message(uid, clean_text(resp.content[0].text))
        except Exception as e:
            print(f"Monthly overview error {uid}: {e}", flush=True)

async def send_daily_messages(context):
    try:
        import datetime as dt2
        conn0 = get_db()
        cur0 = conn0.cursor()
        today_str = dt2.datetime.now(dt2.timezone.utc).strftime('%Y-%m-%d')
        cur0.execute("INSERT INTO settings (key, value) VALUES ('last_daily_date', %s) ON CONFLICT (key) DO UPDATE SET value=%s", (today_str, today_str))
        conn0.commit()
        cur0.close()
        conn0.close()
    except Exception as e:
        print(f"Settings save error: {e}", flush=True)
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, name, birth_day, birth_month, birth_year, lang, gender, trial_started_at, paid_until, remind_at, about_work, about_finance, about_relations, about_personal FROM users WHERE birth_day IS NOT NULL AND agreed=TRUE")
    users_list = cur.fetchall()
    cur.close()
    conn.close()
    today = dt2.datetime.now()
    # Напоминание неактивным 1-го числа
    if today.day == 1:
        conn2 = get_db()
        cur2 = conn2.cursor()
        cur2.execute("SELECT user_id, name, birth_day, birth_month, birth_year, lang, gender, trial_started_at, paid_until FROM users WHERE birth_day IS NOT NULL AND agreed=TRUE")
        all_users = cur2.fetchall()
        cur2.close()
        conn2.close()
        for row in all_users:
            row_data = list(row)
            uid, name, day, month, year, lang, gender, trial_started_at, paid_until = row_data[:9]
            remind_at = row_data[9] if len(row_data) > 9 else None
            about_work = row_data[10] if len(row_data) > 10 else None
            about_finance = row_data[11] if len(row_data) > 11 else None
            about_relations = row_data[12] if len(row_data) > 12 else None
            about_personal = row_data[13] if len(row_data) > 13 else None
            user_obj = {"name": name, "day": day, "month": month, "year": year, "lang": lang, "gender": gender or "f", "trial_started_at": trial_started_at, "paid_until": paid_until, "about_work": about_work, "about_finance": about_finance, "about_relations": about_relations, "about_personal": about_personal}
            if is_trial_active(user_obj) or (paid_until and paid_until.replace(tzinfo=None) > today):
                continue
            if not day:
                continue
            try:
                ctx = build_profile_context(user_obj, lang)
                lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")
                g = "женские окончания" if (gender or "f") == "f" else "мужские окончания"
                remind_sys = f"""{lf}
Ты ассистент Alvalori. Имя: {name}. {g}.
Личный месяц: {ctx["month_text"][:150]}

Напиши одно предложение — название этого периода в 2-3 словах. Только название, без пояснений. Например: "период внутреннего переосмысления" или "время новых контактов". Без кавычек."""
                resp = client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=30,
                    system=remind_sys,
                    messages=[{{"role": "user", "content": "Назови период"}}]
                )
                period = clean_text(resp.content[0].text).strip()
                remind_text = {
                    "ru": f"Привет, {name}! В этом месяце для тебя начинается {period}. Ежедневные персональные рекомендации помогут использовать эту энергию максимально эффективно.",
                    "de": f"Hallo, {name}! Diesen Monat beginnt für dich {period}. Tägliche persönliche Empfehlungen helfen dir, diese Energie optimal zu nutzen.",
                    "en": f"Hi, {name}! This month marks the beginning of {period} for you. Daily personal recommendations will help you make the most of this energy."
                }
                sub_btn = {
                    "ru": "✨ Узнать подробнее",
                    "de": "✨ Mehr erfahren",
                    "en": "✨ Learn more"
                }
                no_btn = {
                    "ru": "❌ Спасибо, не сейчас",
                    "de": "❌ Danke, nicht jetzt",
                    "en": "❌ Thanks, not now"
                }
                btns = InlineKeyboardMarkup([[
                    InlineKeyboardButton(sub_btn.get(lang, sub_btn["ru"]), callback_data="btn_subscribe"),
                    InlineKeyboardButton(no_btn.get(lang, no_btn["ru"]), callback_data="remind_no")
                ]])
                await context.bot.send_message(uid, remind_text.get(lang, remind_text["ru"]), reply_markup=btns)
            except Exception as e:
                print(f"Remind error {{uid}}: {{e}}", flush=True)

    # Проверка окончания подписки за 24 часа
    for row in users_list:
        uid, name, day, month, year, lang, gender, trial_started_at, paid_until, *_ = row
        if paid_until:
            hours_left = (paid_until.replace(tzinfo=None) - today).total_seconds() / 3600
            if 23 <= hours_left <= 25:
                expire_msg = {
                    "ru": f"⏰ Через 24 часа твой доступ закроется.\n\nЧтобы продолжить — напиши администратору заранее.",
                    "de": f"⏰ In 24 Stunden wird dein Zugang geschlossen.\n\nUm fortzufahren — schreibe dem Administrator rechtzeitig.",
                    "en": f"⏰ In 24 hours your access will close.\n\nTo continue — contact the administrator in advance."
                }
                try:
                    await context.bot.send_message(uid, expire_msg.get(lang, expire_msg["ru"]), reply_markup=get_upgrade_keyboard(lang))
                except:
                    pass

    for row in users_list:
        uid, name, day, month, year, lang, gender, trial_started_at, paid_until, *_ = row
        if not day:
            continue
        user_obj = {"name": name, "day": day, "month": month, "year": year, "lang": lang, "gender": gender or "f", "trial_started_at": trial_started_at, "paid_until": paid_until}
        try:
            # Поздравление перенесено в send_birthday_messages (7:30)
            if day == today.day and month == today.month:
                try:
                    bday_user = {"name": name, "day": day, "month": month, "year": year, "lang": lang, "gender": gender or "f"}
                    ctx = build_profile_context(bday_user, lang)
                    lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.", "de": "ANTWORTE NUR AUF DEUTSCH.", "en": "RESPOND ONLY IN ENGLISH."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")
                    g = "женские окончания" if (gender or "f") == "f" else "мужские окончания"
                    bday_sys = f"""{lf}
Ты ассистент Alvalori. День рождения {name}.
Модель мышления: {ctx["mi"]}

Напиши ровно 3 предложения по шаблону — меняй только качества:
1. "[Одно сильное качество этого человека образно и точно, начни с Ты] — и это редкий дар."
2. "[Второе сильное качество, начни с Твоя/Твоё/Твой] заслуживает уважения."
3. "Пусть рядом с тобой будут те кто ценит тебя."

ЗАПРЕЩЕНО: слова "год", "день", "сегодня", "время", "проект", "успех", "достижение", "суперсила", "рождена чтобы".
ТЫ. {g}. Без markdown."""
                    resp = client.messages.create(
                        model="claude-sonnet-4-6", max_tokens=200,
                        system=bday_sys,
                        messages=[{{"role": "user", "content": "Поздравь"}}]
                    )
                    await context.bot.send_message(uid, clean_text(resp.content[0].text))
                except Exception as e:
                    print(f"Birthday personal error: {{e}}", flush=True)
                continue
            user = {"name": name, "day": day, "month": month, "year": year, "lang": lang, "gender": gender or "f", "trial_started_at": trial_started_at, "paid_until": paid_until}
            # Только активным пользователям
            if not (is_trial_active(user_obj) or is_paid(user_obj)):
                continue
            # Вычисляем оставшиеся часы триала
            trial_hours_left = None
            if trial_started_at and not (paid_until and paid_until.replace(tzinfo=None) > datetime.datetime.now()):
                delta = datetime.datetime.now() - trial_started_at.replace(tzinfo=None)
                hours_passed = delta.total_seconds() / 3600
                trial_hours_left = max(0, round(72 - hours_passed))
            ctx = build_profile_context(user, lang)
            mi = ctx["mi"]
            model_name = mi.get("name", "") if isinstance(mi, dict) else ""
            model_profile = mi.get("profile", "") if isinstance(mi, dict) else ""
            model_risks = mi.get("risks", "") if isinstance(mi, dict) else ""
            lf = {"ru": "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ. НИКАКИХ АНГЛИЙСКИХ СЛОВ.", "de": "ANTWORTE AUSSCHLIESSLICH AUF DEUTSCH. KEIN RUSSISCH. KEIN ENGLISCH.", "en": "RESPOND EXCLUSIVELY IN ENGLISH. NO RUSSIAN. NO GERMAN. EVERY SINGLE SENTENCE MUST BE IN ENGLISH."}.get(lang, "ПИШИ ТОЛЬКО НА РУССКОМ ЯЗЫКЕ.")
            g = "женские окончания" if (gender or "f") == "f" else "мужские окончания"
            about_parts = []
            if user_obj.get("about_work"): about_parts.append("Работа: " + user_obj["about_work"])
            if user_obj.get("about_finance"): about_parts.append("Финансы: " + user_obj["about_finance"])
            if user_obj.get("about_relations"): about_parts.append("Отношения: " + user_obj["about_relations"])
            if user_obj.get("about_personal"): about_parts.append("Личное: " + user_obj["about_personal"])
            about_block = ("\n\nКОНТЕКСТ ЖИЗНИ:\n" + "\n".join(about_parts) + "\n\nВАЖНО: этот контекст — фон для понимания человека, не повестка дня. Каждый день опирайся прежде всего на энергию дня, месяца и года — не зацикливайся на проблемах из контекста.") if about_parts else ""

            sys_intro = {"ru": f"Ты ассистент Alvalori. Пишешь утреннее сообщение для {name}. Сегодня {ctx['today']}.", "de": f"Du bist Alvalori-Assistent. Du schreibst eine Morgennachricht für {name}. Heute ist {ctx['today']}.", "en": f"You are Alvalori assistant. You are writing a morning message for {name}. Today is {ctx['today']}."}
            model_strengths = mi.get("strengths", "") if isinstance(mi, dict) else ""
            model_chaos = mi.get("chaos", "") if isinstance(mi, dict) else ""
            model_formula = mi.get("formula", "") if isinstance(mi, dict) else ""
            sys_rec = {"ru": f"""Напиши ровно 3 рекомендации на сегодня для {name}. Начни с тега <b>🌿 Рекомендации на сегодня</b>.

ЛОГИКА: Личный день задаёт главную тему дня. Составляющие личного дня — дополнительные оттенки. Личный месяц и год — фон. Модель мышления — как человек это проживает.

Каждая рекомендация = конкретное действие которое человек может сделать СЕГОДНЯ чтобы использовать энергию дня максимально эффективно. Не общие советы — а точные действия под этот конкретный день и этого конкретного человека.

Формат: 1. 2. 3. — каждый пункт 1-2 предложения.
ЗАПРЕЩЕНО: называть числа и энергии по номерам. ЗАПРЕЩЕНО: прислушайся к себе, найди баланс, позаботься о себе, доверяй процессу. ЗАПРЕЩЕНО: поучительный тон. ТЫ. {g}. Без markdown звёздочек.""", "de": f"""Schreibe genau 3 Empfehlungen für heute für {name}. Beginne mit dem Tag <b>🌿 Empfehlungen für heute</b>.

LOGIK: Der persönliche Tag gibt das Hauptthema vor. Die Bestandteile sind zusätzliche Nuancen. Monat und Jahr bilden den Hintergrund. Das Denkmodell zeigt wie der Mensch das erlebt.

Jede Empfehlung = eine konkrete Handlung die {name} HEUTE tun kann um die Energie des Tages maximal zu nutzen. Keine allgemeinen Ratschläge — präzise Aktionen für diesen Tag und diesen Menschen.

Format: 1. 2. 3. — jeder Punkt 1-2 Sätze.
VERBOTEN: Zahlen oder Energienummern nennen. VERBOTEN: höre auf dich, finde Balance. VERBOTEN: belehrender Ton. DU. {g}. Ohne Markdown-Sterne.""", "en": f"""Write exactly 3 recommendations for today for {name}. Start with the tag <b>🌿 Recommendations for Today</b>.

LOGIC: The personal day sets the main theme. Components are additional nuances. Month and year are the background. The thinking model shows how this person experiences it.

Each recommendation = a concrete action {name} can take TODAY to use the day's energy most effectively. Not general advice — precise actions for this specific day and this specific person.

Format: 1. 2. 3. — each point 1-2 sentences.
FORBIDDEN: mention numbers or energy numbers. FORBIDDEN: listen to yourself, find balance. FORBIDDEN: preachy tone. YOU. {g}. No markdown asterisks."""}
            lang_warning = {"ru": "", "de": "WICHTIG: Die Kontextdaten unten sind auf Russisch — das ist normal. Deine Antwort muss VOLLSTÄNDIG AUF DEUTSCH sein.", "en": "IMPORTANT: The context data below is in Russian — that is normal. Your response must be ENTIRELY IN ENGLISH."}
            sys1 = f"""{lf}
{lang_warning.get(lang, "")}
{sys_intro.get(lang, sys_intro["ru"])}

МОДЕЛЬ МЫШЛЕНИЯ: {model_name}
Профиль: {model_profile}
Сильные стороны: {model_strengths}
В стрессе: {model_chaos}
Формула: {model_formula}
ЛИЧНЫЙ ГОД: {ctx['year_text'][:200]}
ЛИЧНЫЙ МЕСЯЦ: {ctx['month_text'][:150]}
ЛИЧНЫЙ ДЕНЬ: {ctx['day_full']}{about_block}

{sys_rec.get(lang, sys_rec["ru"])}

{lf}"""

            sys_risk = {"ru": f"""Напиши ровно 3 риска на сегодня для {name}. Начни с тега <b>🔺 Риски на сегодня</b>.

ЛОГИКА: Риски — это конкретные ловушки которые возникают когда энергия этого дня используется неосознанно. Учитывай: что может пойти не так именно сегодня с учётом личного дня, его составляющих, и модели мышления этого человека.

Каждый риск = конкретная ситуация или паттерн поведения который может возникнуть СЕГОДНЯ. Не абстрактные предупреждения — а точные ловушки под этот день и этого человека.

Формат: 1. 2. 3. — каждый пункт 1-2 предложения. Мягко, без морализаторства.
ЗАПРЕЩЕНО: называть числа и энергии по номерам. ЗАПРЕЩЕНО: будь осторожен, не забывай, обрати внимание. ТЫ. {g}. Без markdown звёздочек.""", "de": f"""Schreibe genau 3 Risiken für heute für {name}. Beginne mit dem Tag <b>🔺 Risiken für heute</b>.

LOGIK: Risiken sind konkrete Fallen die entstehen wenn die Energie dieses Tages unbewusst genutzt wird. Berücksichtige: was heute schiefgehen kann angesichts des persönlichen Tages, seiner Bestandteile und des Denkmodells.

Jedes Risiko = eine konkrete Situation oder ein Verhaltensmuster das HEUTE entstehen kann. Keine abstrakten Warnungen — präzise Fallen für diesen Tag und diesen Menschen.

Format: 1. 2. 3. — jeder Punkt 1-2 Sätze. Sanft, ohne Moralisieren.
VERBOTEN: Zahlen oder Energienummern nennen. VERBOTEN: sei vorsichtig, vergiss nicht. DU. {g}. Ohne Markdown-Sterne.""", "en": f"""Write exactly 3 risks for today for {name}. Start with the tag <b>🔺 Risks for Today</b>.

LOGIC: Risks are concrete traps that arise when today's energy is used unconsciously. Consider: what could go wrong today given the personal day, its components, and this person's thinking model.

Each risk = a concrete situation or behavior pattern that may arise TODAY. Not abstract warnings — precise traps for this day and this person.

Format: 1. 2. 3. — each point 1-2 sentences. Gentle, without moralizing.
FORBIDDEN: mention numbers or energy numbers. FORBIDDEN: be careful, dont forget. YOU. {g}. No markdown asterisks."""}
            sys2 = f"""{lf}
{lang_warning.get(lang, "")}
{sys_intro.get(lang, sys_intro["ru"])}

МОДЕЛЬ МЫШЛЕНИЯ: {model_name}
Риски модели: {model_risks}
В стрессе: {model_chaos}
ЛИЧНЫЙ ГОД: {ctx['year_text'][:200]}
ЛИЧНЫЙ МЕСЯЦ: {ctx['month_text'][:150]}
ЛИЧНЫЙ ДЕНЬ: {ctx['day_full']}{about_block}

{sys_risk.get(lang, sys_risk["ru"])}

{lf}"""

            r1 = client.messages.create(model="claude-sonnet-4-6", max_tokens=500, system=sys1, messages=[{"role": "user", "content": "Напиши"}])
            await context.bot.send_message(uid, clean_text(r1.content[0].text), parse_mode="HTML")

            r2 = client.messages.create(model="claude-sonnet-4-6", max_tokens=500, system=sys2, messages=[{"role": "user", "content": "Напиши"}])
            risk_text = clean_text(r2.content[0].text)
            await context.bot.send_message(uid, risk_text, parse_mode="HTML")

            # Совет дня
            ud_num, ud_intermediate, has_zero = get_universal_day(dt2.datetime.now(dt2.timezone.utc))
            ud_base = UNIVERSAL_DAY_TIPS.get(ud_num, {}).get(lang, "")
            ud_zero = UNIVERSAL_DAY_TIPS.get(0, {}).get(lang, "") if has_zero else ""
            ud_components_label = {"ru": f"Составляющие общего дня: {ud_intermediate} → {ud_num}", "de": f"Bestandteile des allgemeinen Tages: {ud_intermediate} → {ud_num}", "en": f"Components of the universal day: {ud_intermediate} → {ud_num}"}
            ud_components = ud_components_label.get(lang, ud_components_label["ru"])
            sys_tip = {"ru": f"""Напиши один практический совет — 2-3 предложения. Начни с тега <b>📋 Совет дня</b>.

ЛОГИКА: Личный день влияет на личные дела и внутренние решения. Общий день влияет на внешние события, деловые вопросы, взаимодействие с миром. Если энергии совпадают — усиль главное действие дня. Если противоречат — объясни как действовать в обоих направлениях одновременно.

Совет должен быть практичным: что именно делать сегодня чтобы использовать оба потока энергии. ЗАПРЕЩЕНО: называть числа. ТЫ. {g}. Без markdown звёздочек.""", "de": f"""Schreibe einen praktischen Tipp — 2-3 Sätze. Beginne mit dem Tag <b>📋 Tipp des Tages</b>.

LOGIK: Der persönliche Tag beeinflusst persönliche Angelegenheiten. Der allgemeine Tag beeinflusst externe Ereignisse und Geschäftliches. Wenn die Energien übereinstimmen — verstärke die Hauptaktion. Wenn sie sich widersprechen — erkläre wie man in beiden Richtungen gleichzeitig handelt.

Der Tipp soll praktisch sein: was genau heute zu tun ist um beide Energieströme zu nutzen. VERBOTEN: Zahlen nennen. DU. {g}. Ohne Markdown-Sterne.""", "en": f"""Write one practical tip — 2-3 sentences. Start with the tag <b>📋 Daily Tip</b>.

LOGIC: The personal day influences personal matters and inner decisions. The universal day influences external events, business matters, interaction with the world. If energies align — amplify the main action. If they contradict — explain how to act in both directions simultaneously.

The tip should be practical: what exactly to do today to use both energy streams. FORBIDDEN: mention numbers. YOU. {g}. No markdown asterisks."""}
            sys3 = f"""{lf}
{sys_intro.get(lang, sys_intro["ru"])}

ЛИЧНЫЙ ДЕНЬ (влияет на личные дела и решения): {ctx['day_full']}
ОБЩИЙ ДЕНЬ {ud_components} (влияет на внешние события, документы, бизнес, госдела): {ud_base}
{"ОСОБОЕ УСЛОВИЕ: " + ud_zero if ud_zero else ""}
ВАЖНО: если энергии личного и общего дня противоречат (например личный день 1=начало, общий день 9=завершение) — укажи это в совете и объясни как действовать.

{sys_tip.get(lang, sys_tip["ru"])}

{lf}"""
            r3 = client.messages.create(model="claude-sonnet-4-6", max_tokens=200, system=sys3, messages=[{"role": "user", "content": "Напиши"}])
            await context.bot.send_message(uid, clean_text(r3.content[0].text), parse_mode="HTML")

            # Опрос через 24ч после окончания триала
            trial_end = None
            if user_trial_started := trial_started_at:
                if hasattr(user_trial_started, 'date'):
                    trial_end = user_trial_started + datetime.timedelta(hours=72)
                if trial_end and not is_paid(user_obj):
                    hours_since_end = (datetime.datetime.now(datetime.timezone.utc) - trial_end.replace(tzinfo=datetime.timezone.utc)).total_seconds() / 3600
                    if 24 <= hours_since_end < 48:
                        gender = gender if True else "f"
                        churn_msg = {
                            "ru": name + ", твой пробный период завершился.\n\nБуду признательна если поделишься — что остановило от подписки? Это поможет сделать Alvalori лучше 🙏",
                            "de": name + ", dein Testzeitraum ist abgelaufen.\n\nIch wäre dankbar wenn du teilst — was hat dich vom Abonnement abgehalten? Das hilft Alvalori besser zu machen 🙏",
                            "en": name + ", your trial period has ended.\n\nI'd be grateful if you share — what stopped you from subscribing? This will help make Alvalori better 🙏",
                        }
                        btns = InlineKeyboardMarkup([[
                            InlineKeyboardButton("✅ Поделиться" if lang=="ru" else ("✅ Teilen" if lang=="de" else "✅ Share"), callback_data="churn_share"),
                            InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="churn_no"),
                        ]])
                        try:
                            await context.bot.send_message(uid, churn_msg.get(lang, churn_msg["ru"]), reply_markup=btns)
                        except Exception as e:
                            print(f"Churn survey error {uid}: {e}", flush=True)

            # Часы триала
            if trial_hours_left is not None and trial_hours_left > 0:
                hours_msg = {
                    "ru": f"⏱ У тебя осталось {trial_hours_left} ч. бесплатного доступа.",
                    "de": f"⏱ Du hast noch {trial_hours_left} Std. kostenlosen Zugang.",
                    "en": f"⏱ You have {trial_hours_left} hrs of free access left."
                }
                await context.bot.send_message(uid, hours_msg.get(lang, hours_msg["ru"]))

            # Опрос через 24ч после окончания платной подписки
            try:
                paid_until_val = paid_until
                if paid_until_val and not is_paid(user_obj):
                    if hasattr(paid_until_val, 'tzinfo') and paid_until_val.tzinfo is None:
                        paid_until_val = paid_until_val.replace(tzinfo=dt2.timezone.utc)
                    hours_since_end = (dt2.datetime.now(dt2.timezone.utc) - paid_until_val).total_seconds() / 3600
                    if 24 <= hours_since_end < 48:
                        gender_val = gender if True else "f"
                        paid_churn_msg = {
                            "ru": name + ", твоя подписка завершилась.\n\nБуду рада видеть тебя снова 🌟 Если что-то остановило от продления — поделись, это важно.",
                            "de": name + ", dein Abonnement ist abgelaufen.\n\nIch würde mich freuen, dich wiederzusehen 🌟 Wenn etwas dich von der Verlängerung abgehalten hat — teile es mit mir.",
                            "en": name + ", your subscription has ended.\n\nI'd love to see you again 🌟 If something stopped you from renewing — please share, it's important.",
                        }
                        btns_paid = InlineKeyboardMarkup([
                            [InlineKeyboardButton("💳 Продлить подписку" if lang=="ru" else ("💳 Abonnement verlängern" if lang=="de" else "💳 Renew subscription"), callback_data="btn_pay")],
                            [InlineKeyboardButton("✍️ Поделиться причиной" if lang=="ru" else ("✍️ Grund mitteilen" if lang=="de" else "✍️ Share reason"), callback_data="paid_churn_share")],
                            [InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="churn_no")],
                        ])
                        await context.bot.send_message(uid, paid_churn_msg.get(lang, paid_churn_msg["ru"]), reply_markup=btns_paid)
            except Exception as e:
                print(f"Paid churn survey error {uid}: {e}", flush=True)

            await show_menu(context, uid, lang)

            # Напоминание тем кто попросил
            try:
                remind_at = remind_at if 'remind_at' in dir() else None
                if remind_at:
                    today_date = dt2.date.today()
                    remind_date = remind_at
                    if remind_date == today_date:
                        gender_val = gender if True else "f"
                        remind_msg = {
                            "ru": name + ", ты просил" + ("а" if gender_val=="f" else "") + " напомнить о Alvalori 🌟\n\nГотов" + ("а" if gender_val=="f" else "") + " попробовать снова?",
                            "de": name + ", du hast gebeten, dich an Alvalori zu erinnern 🌟\n\nBereit es nochmal zu versuchen?",
                            "en": name + ", you asked me to remind you about Alvalori 🌟\n\nReady to try again?",
                        }
                        btns_r = InlineKeyboardMarkup([[
                            InlineKeyboardButton("💳 Оформить подписку" if lang=="ru" else ("💳 Abonnieren" if lang=="de" else "💳 Subscribe"), callback_data="btn_pay"),
                            InlineKeyboardButton("❌ Не сейчас" if lang=="ru" else ("❌ Nicht jetzt" if lang=="de" else "❌ Not now"), callback_data="churn_remind_no"),
                        ]])
                        await context.bot.send_message(uid, remind_msg.get(lang, remind_msg["ru"]), reply_markup=btns_r)
                        save_user(uid, remind_at=None)
            except Exception as e:
                print(f"Remind error {uid}: {e}", flush=True)

        except Exception as e:
            print(f"Daily msg error {uid}: {e}", flush=True)
            try:
                await context.bot.send_message(ADMIN_ID, "Не доставлено: " + str(name) + " (" + str(uid) + ")")
            except:
                pass

        import asyncio as _asyncio
        await _asyncio.sleep(1)

if __name__ == "__main__":
    import os
    init_db()
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    import datetime as dt
    app.job_queue.run_daily(send_birthday_messages, time=dt.time(hour=5, minute=30, tzinfo=dt.timezone.utc))
    app.job_queue.run_daily(send_monthly_overview, time=dt.time(hour=8, minute=0, tzinfo=dt.timezone.utc))
    app.job_queue.run_daily(send_daily_messages, time=dt.time(hour=6, minute=0, tzinfo=dt.timezone.utc))

    # При старте проверяем — если рассылка сегодня ещё не отправлялась — отправляем
    import datetime as dt2
    now_utc = dt2.datetime.now(dt2.timezone.utc)
    if 6 <= now_utc.hour < 23:
        try:
            conn0 = get_db()
            cur0 = conn0.cursor()
            cur0.execute("SELECT value FROM settings WHERE key='last_daily_date'")
            row = cur0.fetchone()
            cur0.close()
            conn0.close()
            last_date = row[0] if row else None
            today_str = now_utc.strftime('%Y-%m-%d')
            if last_date != today_str:
                print("Startup: daily not sent yet today, scheduling in 30s", flush=True)
                app.job_queue.run_once(send_daily_messages, when=30)
        except Exception as e:
            print(f"Startup daily check error: {e}", flush=True)
    # feedback рассылка отключена
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("year", cmd_year))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("compass", cmd_compass))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("stats", admin_stats))
    app.add_handler(CommandHandler("send_monthly", admin_send_monthly))
    app.add_handler(CommandHandler("announce_nav", admin_announce_nav))
    app.add_handler(CommandHandler("announce_about", admin_announce_about))
    app.add_handler(CommandHandler("news", admin_news))
    app.add_handler(CommandHandler("export", admin_export))
    app.add_handler(CommandHandler("myref", my_ref))
    app.add_handler(CommandHandler("makeref", admin_makeref))
    app.add_handler(CommandHandler("users", admin_users))
    app.add_handler(CommandHandler("active", admin_active))
    app.add_handler(CommandHandler("refs", admin_refs))
    app.add_handler(CommandHandler("reset_user", admin_reset))
    app.add_handler(CommandHandler("grant_access", admin_grant))
    app.add_handler(CallbackQueryHandler(age_cb, pattern="^age_"))
    app.add_handler(CallbackQueryHandler(agree_cb, pattern="^(agree|disagree|agree_minor|parent_yes|parent_no)$"))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(gender_cb, pattern="^gender_"))
    app.add_handler(CallbackQueryHandler(compass_yn_cb, pattern="^compass_"))
    app.add_handler(CallbackQueryHandler(menu_cb, pattern="^(btn_|pay_|sepa_|trial_start|trial_choice|trial_not_now|btn_menu|about_|feedback_yes|feedback_no|remind_no|announce_nav_|churn_|paid_churn_|btn_info|btn_subscribe|age_)"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()
    PORT = int(os.environ.get("PORT", 10000))
    if WEBHOOK_URL:
        import asyncio
        from aiohttp import web
        from telegram import Update

        async def handle(request):
            data = await request.json()
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
            return web.Response(text="ok")

        async def health(request):
            return web.Response(text="ok")

        async def stripe_webhook(request):
            payload = await request.read()
            sig_header = request.headers.get("Stripe-Signature", "")
            webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
            try:
                if webhook_secret:
                    event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
                else:
                    import json
                    event = json.loads(payload)
            except Exception as e:
                print(f"Stripe webhook error: {e}", flush=True)
                return web.Response(status=400)
            if event["type"] in ("customer.subscription.created", "invoice.payment_succeeded"):
                try:
                    if event["type"] == "customer.subscription.created":
                        obj = event["data"]["object"]
                        metadata = obj.get("metadata", {})
                    else:
                        obj = event["data"]["object"]
                        sub_id = obj.get("subscription")
                        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
                        sub = stripe.Subscription.retrieve(sub_id)
                        metadata = sub.get("metadata", {})
                    user_id = int(metadata.get("user_id", 0))
                    plan = metadata.get("plan", "1m")
                    days = {"1m": 30, "6m": 180, "12m": 365}.get(plan, 30)
                    if user_id:
                        import datetime
                        paid_until = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
                        save_user(user_id, paid_until=paid_until)
                        user = get_user(user_id)
                        lang = user.get("lang", "ru") if user else "ru"
                        success_msg = {"ru": f"✅ Оплата прошла! Доступ открыт на {days} дней.", "de": f"✅ Zahlung erfolgreich! Zugang für {days} Tage.", "en": f"✅ Payment successful! Access for {days} days."}
                        await app.bot.send_message(user_id, success_msg.get(lang, success_msg["ru"]))
                        print(f"✅ Access granted: user {user_id}, plan {plan}", flush=True)
                except Exception as e:
                    print(f"Webhook processing error: {e}", flush=True)
            elif event["type"] == "customer.subscription.deleted":
                try:
                    obj = event["data"]["object"]
                    metadata = obj.get("metadata", {})
                    user_id = int(metadata.get("user_id", 0))
                    if user_id:
                        user = get_user(user_id)
                        lang = user.get("lang", "ru") if user else "ru"
                        end_msg = {"ru": "Подписка завершилась. Для продления — раздел Подписка.", "de": "Abonnement abgelaufen. Zur Verlängerung — Bereich Abonnement.", "en": "Subscription ended. To renew — Subscription section."}
                        await app.bot.send_message(user_id, end_msg.get(lang, end_msg["ru"]))
                except Exception as e:
                    print(f"Webhook deletion error: {e}", flush=True)
            return web.Response(text="ok")

        async def run():
            await app.initialize()
            await app.bot.delete_webhook(drop_pending_updates=True)
            from telegram import BotCommand, BotCommandScopeDefault
            await app.bot.set_my_commands([
                BotCommand("start", "Начать / Start / Starten"),
            ], scope=BotCommandScopeDefault())
            wh_url = f"{WEBHOOK_URL}/webhook"
            print(f"Setting webhook: [{wh_url}]", flush=True)
            await app.bot.set_webhook(url=wh_url)
            await app.start()
            print(f"Webhook set: {WEBHOOK_URL}/webhook", flush=True)
            server = web.Application()
            server.router.add_post("/webhook", handle)
            server.router.add_post("/stripe-webhook", stripe_webhook)
            server.router.add_get("/health", health)
            runner = web.AppRunner(server)
            await runner.setup()
            site = web.TCPSite(runner, "0.0.0.0", PORT)
            await site.start()
            print(f"Server started on port {PORT}", flush=True)

            async def keepalive():
                import aiohttp as aiohttp_client
                url = f"http://127.0.0.1:{PORT}/health"
                while True:
                    await asyncio.sleep(600)
                    try:
                        async with aiohttp_client.ClientSession() as session:
                            await session.get(url, timeout=aiohttp_client.ClientTimeout(total=10))
                        print("Keepalive ping OK", flush=True)
                    except Exception as e:
                        print(f"Keepalive error: {e}", flush=True)

            asyncio.ensure_future(keepalive())
            await asyncio.Event().wait()

        asyncio.run(run())
    else:
        app.run_polling(stop_signals=None, drop_pending_updates=True)
