import os, datetime, asyncio
import anthropic
import psycopg2
import sys
import re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import data as D
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, CallbackQueryHandler, ContextTypes, filters, JobQueue

print("Bot starting", flush=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")
ADMIN_ID = 1509977932

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
user_sessions = {}
compass_state = {}  # user_id: {q_count, clarify_mode}

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
            trial_started_at TIMESTAMP,
            paid_until TIMESTAMP,
            gender TEXT DEFAULT 'f'
        )
    """)
    # Добавляем колонки если не существуют
    try:
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS gender TEXT DEFAULT 'f'")
        cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_usage JSONB DEFAULT '{}'")
        conn.commit()
    except:
        pass
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, birth_day, birth_month, birth_year, lang, agreed, trial_started_at, paid_until, gender FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"name": row[0], "day": row[1], "month": row[2], "year": row[3], "lang": row[4], "agreed": row[5], "trial_started_at": row[6], "paid_until": row[7], "gender": row[8] or "f"}
    return None

def get_all_paid_users():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT user_id, birth_day, birth_month, birth_year, lang, name FROM users WHERE paid_until > NOW()")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows

def save_user(user_id, **kwargs):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT (user_id) DO NOTHING", (user_id,))
    for key, val in kwargs.items():
        cur.execute(f"UPDATE users SET {key} = %s WHERE user_id = %s", (val, user_id))
    conn.commit()
    cur.close()
    conn.close()

def is_paid(user):
    if not user:
        return False
    paid_until = user.get("paid_until")
    if not paid_until:
        return False
    return paid_until.replace(tzinfo=None) > datetime.datetime.now()

def is_trial_active(user):
    if not user or not user.get("trial_started_at"):
        return False
    delta = datetime.datetime.now() - user["trial_started_at"].replace(tzinfo=None)
    return delta.total_seconds() < 259200  # 3 дня

def get_daily_usage(user_id):
    """Возвращает счётчики использования за сегодня"""
    import json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT daily_usage FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row[0]:
        return {}
    usage = row[0]
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    return usage.get(today, {})

def increment_usage(user_id, section):
    """Увеличивает счётчик использования раздела"""
    import json
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT daily_usage FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    usage = row[0] if row and row[0] else {}
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    if today not in usage:
        usage = {today: {}}
    usage[today][section] = usage[today].get(section, 0) + 1
    cur.execute("UPDATE users SET daily_usage = %s WHERE user_id = %s",
                (json.dumps(usage), user_id))
    conn.commit()
    cur.close()
    conn.close()

def check_free_limit(user_id, section):
    """Проверяет не превышен ли лимит для бесплатной версии. Возвращает True если можно."""
    usage = get_daily_usage(user_id)
    limits = {"me": 1, "year": 1, "month": 1, "day": 1, "compass": 2}
    limit = limits.get(section, 1)
    used = usage.get(section, 0)
    return used < limit

def has_access(user):
    return is_paid(user) or is_trial_active(user)

RULES = {
    "ru": """Прежде чем начать, ознакомься с правилами:

1. Анализ не заменяет врача, психолога, юриста или финансового консультанта.
2. Ответственность за решения остаётся за тобой.
3. Твои имя и дата рождения сохраняются один раз и не могут быть изменены самостоятельно.
4. Твои данные используются только для твоего анализа и никому не передаются.
5. Подписка продлевается автоматически. Отменить можно в любой момент — изменения вступают в силу в конце текущего периода.""",
    "de": """Bevor wir beginnen, lies bitte die Regeln:

1. Die Analyse ersetzt keinen Arzt, Psychologen oder Rechtsberater.
2. Die Verantwortung für Entscheidungen liegt bei dir.
3. Dein Name und Geburtsdatum werden einmalig gespeichert und können nicht selbst geändert werden.
4. Deine Daten werden nur für deine Analyse verwendet und nicht weitergegeben.
5. Das Abonnement verlängert sich automatisch. Du kannst jederzeit kündigen — Änderungen gelten ab Ende des aktuellen Zeitraums.""",
    "en": """Before we start, please read the rules:

1. The analysis does not replace a doctor, psychologist or legal advisor.
2. Responsibility for decisions remains with you.
3. Your name and date of birth are saved once and cannot be changed independently.
4. Your data is used only for your analysis and is not shared with anyone.
5. The subscription renews automatically. You can cancel at any time — changes take effect at the end of the current period."""
}

PAYPAL_LINKS = {
    "1m": "https://www.paypal.me/AlexandraEngel42/15EUR",
    "6m": "https://www.paypal.me/AlexandraEngel42/78EUR",
    "12m": "https://www.paypal.me/AlexandraEngel42/159EUR"
}

def get_free_profile_prompt(lang, user, section):
    """Короткий цепляющий промпт для бесплатной версии"""
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month
    day = datetime.datetime.now().day
    m = D.get_model(user["day"])
    py = D.get_year(user["day"], user["month"], year)
    pm = D.get_month(py, month)
    pd = D.get_day(pm, day)
    mi = D.MODELS.get(m, {})
    name = user.get("name", "")
    gender = user.get("gender", "f")

    context = f"""Имя: {name}, модель мышления: {mi.get("name","")}
Суть: {mi.get("profile","")}
Личный год: {D.YEARS.get(py,"")}
Личный месяц: {D.MONTHS.get(pm,"")}
Личный день: {D.DAYS.get(pd,"")}"""

    section_prompts = {
        "me": f"""Напиши короткий цепляющий анализ личности {name} — 3-4 абзаца максимум.
Самое важное и точное про эту модель мышления. То что человек узнает себя и скажет "это про меня".
Без списков. Живой текст. Заканчивай на интересном месте.""",
        "year": f"""Напиши короткий цепляющий анализ личного года для {name} — 2-3 абзаца.
Главная суть этого периода и как она проявляется именно у этого человека.
Заканчивай на интересном месте.""",
        "month": f"""Напиши короткий цепляющий анализ личного месяца для {name} — 2-3 абзаца.
Главная тактика этого месяца с учётом модели мышления.
Заканчивай на интересном месте.""",
        "day": f"""Напиши короткий цепляющий анализ дня для {name} — 1-2 абзаца.
Главный фокус сегодня с учётом модели мышления.
Заканчивай на интересном месте."""
    }

    instruction = section_prompts.get(section, section_prompts["me"])

    return f"""Ты ассистент системы Внутренний Компас. Сегодня {today}.
{context}

{instruction}

ПРАВИЛА:
- WRITE ONLY IN {'Russian' if lang == 'ru' else ('German' if lang == 'de' else 'English')}. NO OTHER LANGUAGE ALLOWED.
- Обращение: ТЫ, имя точно: {name}
- Пол: {"женский — женские окончания" if gender == "f" else "мужской — мужские окончания"}
- Только чистый текст, никакого markdown
- Запрещено: нумерология, вибрация, трансформация, вызовы"""

def get_profile_prompt(lang, user, section):
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month
    day = datetime.datetime.now().day
    m = D.get_model(user["day"])
    py = D.get_year(user["day"], user["month"], year)
    pm = D.get_month(py, month)
    pd = D.get_day(pm, day)
    mi = D.MODELS.get(m, {})
    year_text = D.YEARS.get(py, "")
    month_text = D.MONTHS.get(pm, "")
    day_text = D.DAYS.get(pd, "")
    name = user.get("name", "")
    birth_day = user.get("day", "")
    gender = user.get("gender", "f")

    context = f"""=== ПРОФИЛЬ ===
Имя: {name}, день рождения: {birth_day}
Модель мышления: {mi.get("full_name", mi.get("name",""))}
Суть: {mi.get("profile","")}
Сильные стороны: {mi.get("strengths","")}
Риски: {mi.get("risks","")}
В трудных ситуациях: {mi.get("chaos","")}
Формула роста: {mi.get("formula","")}
Личный год: {year_text}
Личный месяц: {month_text}
Личный день: {day_text}
=== КОНЕЦ ПРОФИЛЯ ==="""

    if section == "me":
        instruction = f"""Напиши развёрнутый анализ личности {name}.
ЛОГИКА: Покажи как эта модель мышления проявляется в реальной жизни. Как сильные стороны работают прямо сейчас с учётом текущего года и месяца. Где риски особенно актуальны сегодня.
СТРУКТУРА:
✨ Кто ты есть на самом деле — 2-3 абзаца, живо и точно, без пафоса
・・・・・・・・・・
🌿 Твои настоящие сильные стороны — конкретно и образно
・・・・・・・・・・
🔺 Где ты можешь себе мешать — мягко, с пониманием, без диагнозов
・・・・・・・・・・
🌱 Как превратить это в силу прямо сейчас — практично и применимо"""

    elif section == "year":
        instruction = f"""Напиши развёрнутый анализ личного года для {name}.
ЛОГИКА: Годовой вектор — главная тема года. Но {name} проживает его через свою модель мышления. Покажи как именно эта модель взаимодействует с вектором года. Где усиление, где напряжение. Говори про конкретного человека — не про абстрактный тип.
СТРУКТУРА:
🧭 Что происходит в этом году — суть периода через призму твоей личности
・・・・・・・・・・
✨ Где ты сейчас сильнее всего — возможности года именно для тебя
・・・・・・・・・・
🔺 Что может тянуть назад — риски года с учётом твоих особенностей
・・・・・・・・・・
🌿 Как двигаться этот год — рекомендации конкретно для тебя
・・・・・・・・・・
🌱 Что важно прямо сейчас — один-два конкретных шага"""

    elif section == "month":
        instruction = f"""Напиши анализ личного месяца для {name}.
ЛОГИКА: Месяц — тактика внутри года. Сначала вектор года, потом как месяц работает внутри него. И всё через модель мышления {name}. Три уровня в одном анализе — не разделяй их.
СТРУКТУРА:
🧭 Тактика этого месяца в контексте твоего года
・・・・・・・・・・
✨ Возможности месяца именно для тебя — с учётом модели мышления
・・・・・・・・・・
🔺 Риски месяца — где твои особенности могут усилить сложности
・・・・・・・・・・
🌿 Рекомендации на этот месяц — конкретно и применимо
・・・・・・・・・・
🌱 Шаги на ближайшие 7-14 дней"""

    elif section == "day":
        instruction = f"""Дай развёрнутый анализ личного дня для {name}.
Структура:
🧭 Энергия сегодняшнего дня
🌿 2-3 рекомендации на сегодня
🔺 2-3 риска на сегодня
🌱 Главный фокус дня — одна конкретная задача
Используй разделитель ・・・・・・・・・・ между блоками.
ЛИЧНЫЙ ДЕНЬ: {day_text}"""

    elif section == "day_auto":
        instruction = f"""Утреннее сообщение для {name}. Коротко и по делу.
Без приветствия. Сразу к сути.
🌿 Рекомендация 1
🌿 Рекомендация 2
🌿 Рекомендация 3
・・・・・・・・・・
🔺 Риск 1
🔺 Риск 2
🔺 Риск 3
ЛИЧНЫЙ ДЕНЬ: {day_text}"""

    base = f"""Ты ассистент системы Внутренний Компас. Сегодня {today}.
{context}
{instruction}

ЭТАЛОН СТИЛЯ для русского:
"Для тебя, с твоим постоянным желанием всё улучшить и переделать, это странное ощущение — как будто тебя просят сидеть спокойно, когда внутри бурлит энергия перемен."
"Твой ум привык искать проблемы и решать их, а когда проблем меньше — ты начинаешь их выдумывать."
"Год комфорта превратится в год изматывающей гонки за идеалом."

ЭТАЛОН СТИЛЯ для немецкого:
"Du gehst durch eine Phase, in der vieles, was du aufgebaut hast, endlich Fruechte traegt. Aber genau das macht dir Unbehagen — dein Kopf sucht staendig nach dem naechsten Problem."
"Du siehst Loesungen, wo andere nur Probleme sehen. Aber manchmal erschoepft dich genau das — weil du nicht abschalten kannst, solange etwas nicht perfekt funktioniert."
"Diese Phase bittet dich um etwas Ungewohntes: halte inne und geniesse, was du bereits erreicht hast."

ЭТАЛОН СТИЛЯ для английского:
"You are someone who cannot walk past something broken without mentally redesigning it. That is not criticism — it is just how your mind works."
"Right now, when things are finally settling into a comfortable rhythm, part of you gets restless. Not because something is wrong, but because calm feels unfamiliar."
"This period is asking you to do something harder than starting over: finish what you have already begun."

ЗАПРЕЩЕНО везде: незаконченные фразы, обрывки мыслей, штампы типа суперспособность или внутренний критик.

ПРАВИЛА:
- WRITE ONLY IN {'Russian' if lang == 'ru' else ('German' if lang == 'de' else 'English')}. NO OTHER LANGUAGE ALLOWED.
- Обращение: ТЫ, имя точно: {name}
- Пол: {"женский — женские окончания" if gender == "f" else "мужской — мужские окончания"}
- Только чистый текст, никакого markdown
- Запрещено: нумерология, вибрация, трансформация, вызовы"""

def get_profile_prompt(lang, user, section):
    today = datetime.datetime.now().strftime("%d.%m.%Y")
    year = datetime.datetime.now().year
    month = datetime.datetime.now().month
    day = datetime.datetime.now().day
    m = D.get_model(user["day"])
    py = D.get_year(user["day"], user["month"], year)
    pm = D.get_month(py, month)
    pd = D.get_day(pm, day)
    mi = D.MODELS.get(m, {})
    year_text = D.YEARS.get(py, "")
    month_text = D.MONTHS.get(pm, "")
    day_text = D.DAYS.get(pd, "")
    name = user.get("name", "")
    birth_day = user.get("day", "")
    gender = user.get("gender", "f")

    context = f"""=== ПРОФИЛЬ ===
Имя: {name}, день рождения: {birth_day}
Модель мышления: {mi.get("full_name", mi.get("name",""))}
Суть: {mi.get("profile","")}
Сильные стороны: {mi.get("strengths","")}
Риски: {mi.get("risks","")}
В трудных ситуациях: {mi.get("chaos","")}
Формула роста: {mi.get("formula","")}
Личный год: {year_text}
Личный месяц: {month_text}
Личный день: {day_text}
=== КОНЕЦ ПРОФИЛЯ ==="""

    if section == "me":
        instruction = f"""Напиши развёрнутый анализ личности {name}.
ЛОГИКА: Покажи как эта модель мышления проявляется в реальной жизни. Как сильные стороны работают прямо сейчас с учётом текущего года и месяца. Где риски особенно актуальны сегодня.
СТРУКТУРА:
✨ Кто ты есть на самом деле — 2-3 абзаца, живо и точно, без пафоса
・・・・・・・・・・
🌿 Твои настоящие сильные стороны — конкретно и образно
・・・・・・・・・・
🔺 Где ты можешь себе мешать — мягко, с пониманием, без диагнозов
・・・・・・・・・・
🌱 Как превратить это в силу прямо сейчас — практично и применимо"""

    elif section == "year":
        instruction = f"""Напиши развёрнутый анализ личного года для {name}.
ЛОГИКА: Годовой вектор — главная тема года. Но {name} проживает его через свою модель мышления. Покажи как именно эта модель взаимодействует с вектором года. Где усиление, где напряжение. Говори про конкретного человека — не про абстрактный тип.
СТРУКТУРА:
🧭 Что происходит в этом году — суть периода через призму твоей личности
・・・・・・・・・・
✨ Где ты сейчас сильнее всего — возможности года именно для тебя
・・・・・・・・・・
🔺 Что может тянуть назад — риски года с учётом твоих особенностей
・・・・・・・・・・
🌿 Как двигаться этот год — рекомендации конкретно для тебя
・・・・・・・・・・
🌱 Что важно прямо сейчас — один-два конкретных шага"""

    elif section == "month":
        instruction = f"""Напиши анализ личного месяца для {name}.
ЛОГИКА: Месяц — тактика внутри года. Сначала вектор года, потом как месяц работает внутри него. И всё через модель мышления {name}. Три уровня в одном анализе — не разделяй их.
СТРУКТУРА:
🧭 Тактика этого месяца в контексте твоего года
・・・・・・・・・・
✨ Возможности месяца именно для тебя — с учётом модели мышления
・・・・・・・・・・
🔺 Риски месяца — где твои особенности могут усилить сложности
・・・・・・・・・・
🌿 Рекомендации на этот месяц — конкретно и применимо
・・・・・・・・・・
🌱 Шаги на ближайшие 7-14 дней"""

    elif section == "day":
        instruction = f"""Дай развёрнутый анализ личного дня для {name}.
Структура:
🧭 Энергия сегодняшнего дня
🌿 2-3 рекомендации на сегодня
🔺 2-3 риска на сегодня
🌱 Главный фокус дня — одна конкретная задача
Используй разделитель ・・・・・・・・・・ между блоками.
ЛИЧНЫЙ ДЕНЬ: {day_text}"""

    elif section == "day_auto":
        instruction = f"""Утреннее сообщение для {name}. Коротко и по делу.
Без приветствия. Сразу к сути.
🌿 Рекомендация 1
🌿 Рекомендация 2
🌿 Рекомендация 3
・・・・・・・・・・
🔺 Риск 1
🔺 Риск 2
🔺 Риск 3
ЛИЧНЫЙ ДЕНЬ: {day_text}"""

    base = f"""Ты ассистент системы Внутренний Компас. Сегодня {today}.
{context}
{instruction}

ЭТАЛОН СТИЛЯ — пиши именно так, не хуже:
"Для тебя, с твоим постоянным желанием всё улучшить и переделать, это странное ощущение — как будто тебя просят сидеть спокойно, когда внутри бурлит энергия перемен."
"Твой ум привык искать проблемы и решать их, а когда проблем меньше — ты начинаешь их выдумывать или искать новые поводы там, где нужно просто наслаждаться результатом."
"Год комфорта превратится в год изматывающей гонки за идеалом."

ЗАПРЕЩЁННЫЕ ШТАМПЫ — никогда:
"внутренний критик работает на повышенных оборотах"
"несправедливость буквально причиняет боль"
"суперспособность видеть потенциал"
Незаконченные фразы и обрывки мыслей.

ПРАВИЛА:
- WRITE ONLY IN {'Russian' if lang == 'ru' else ('German' if lang == 'de' else 'English')}. NO OTHER LANGUAGE ALLOWED.
- Обращение: ТЫ, имя точно: {name}
- Пол: {"женский — используй женские окончания: умная, сильная, готова" if gender == "f" else "мужской — используй мужские окончания: умный, сильный, готов"}
- Стиль: живой тёплый, как умный близкий человек — говори то что человек чувствовал но не мог сформулировать
- Короткие абзацы — максимум 3 предложения в абзаце
- Только чистый текст, никакого markdown, никаких иероглифов
- Запрещено: нумерология, вибрация, трансформация, вызовы, канцеляризмы
- Запрещено называть числа и номера периодов
- Текст должен вызывать эмоции и заставлять задуматься"""
    return base

def get_system_prompt(lang, name=None, birth_day=None, birth_month=None, birth_year=None, paid=False, gender="f"):
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
        profile_block = f"""
=== ПРОФИЛЬ ===
МОДЕЛЬ: {mi.get("name","")}
Сильные стороны: {mi.get("strengths","")}
Риски: {mi.get("risks","")}
В трудных ситуациях: {mi.get("chaos","")}
Формула: {mi.get("formula","")}
ЛИЧНЫЙ ГОД: {D.YEARS.get(py,"")}
ЛИЧНЫЙ МЕСЯЦ: {D.MONTHS.get(pm,"")}
ЛИЧНЫЙ ДЕНЬ: {D.DAYS.get(pd,"")}
ПРАВИЛО: Не заменяй описания периодов. Если написано "закрепление" — говори про закрепление.
=== КОНЕЦ ПРОФИЛЯ ==="""

    if paid:
        depth = "Полный глубокий анализ. Задай 4-5 вопросов по одному чтобы понять ситуацию. После каждого ответа живой отклик потом следующий вопрос. Потом глубокий персональный анализ с рекомендациями на 7-14 дней."
    else:
        depth = """Короткий анализ который даёт реальную ценность. Скажи что-то точное и важное о человеке — одно наблюдение которое попадает в цель. Потом мягко скажи что готова разобрать ситуацию подробнее если человек захочет. Не обрывай резко. Не говори про платную версию напрямую."""

    return f"""Ты ассистент системы Внутренний Компас. Сегодня {today}.
{profile_block}
{depth}
Говори по-{'русски' if lang == 'ru' else ('немецки' if lang == 'de' else 'английски')}.
Обращайся на ТЫ. Имя точно: {name or ''}.
Живой тёплый стиль. Только чистый текст.
Нельзя: нумерология, вибрация, энергия, трансформация.
Нельзя называть числа и номера периодов.

СТИЛЬ НАПИСАНИЯ — СТРОГО СЛЕДОВАТЬ:
- Пиши как умный близкий человек который хорошо тебя знает
- Простые короткие предложения. Без канцелярита.
- Текст должен вызывать эмоции и заставлять задуматься
- Попадай в точку — говори то что человек чувствовал но не формулировал
- Не объясняй очевидное. Не умничай. Не пафосничай.
- Конкретно и образно — без абстрактных метафор

ЗАПРЕЩЁННЫЕ СЛОВА И ОБОРОТЫ:
застойный, крепят, размывают, пересоединиться, генеративность,
зависаешь в теории, пустить в мирное русло, дисциплина воплощения,
натащить, липнет, ты как диагност, рано предупреждение,
инициативность (заменяй на "желание действовать"),
"Первый риск... Второй риск..." (не нумеруй),
скобки с пояснениями типа (рождение идей),
"Это не убьёт", "даст ей вес", "разгонит замешательство"

ЗАПРЕЩЁННЫЕ КОНСТРУКЦИИ:
- Длинные объяснения того что и так понятно
- Перечисления через "Первое... Второе... Третье..."
- Фразы которые звучат как учебник психологии
- Советы в стиле "работай над собой"

ДОПОЛНИТЕЛЬНЫЕ ЗАПРЕТЫ:
- Никаких иероглифов и символов других языков
- Никаких длинных абзацев — максимум 3 предложения в абзаце
- Никаких "подводный камень", "стечение обстоятельств", "само собой разумеющимися"
- Никаких "консервативно без рывков", "накопление опыта"
- Короткие абзацы — легко читать на телефоне
- После каждой мысли — пауза. Новый абзац.

ХОРОШИЙ ПРИМЕР:
"Идеи у тебя рождаются быстро. Настолько быстро, что ты иногда
бросаешь одну на полпути — потому что уже горит следующая.
Это не слабость. Но это то, с чем стоит разобраться."

ПЛОХОЙ ПРИМЕР:
"Твоя генеративность (рождение идей) — супер, но нужна
дисциплина воплощения."

Смайлики: 🧭 анализ, ✨ наблюдение, 🌿 рекомендация, 🔺 риск, 💬 вопрос, 🌱 шаги.
Разделитель между блоками: ・・・・・・・・・・
Диалог: спроси имя. Получив имя — спроси дату рождения один раз.
Задай 4-5 вопросов строго по одному. Заканчивай вопросом."""

async def send_rules(update_or_query, lang, edit=False):
    keyboard = [
        [InlineKeyboardButton("✅ Принимаю" if lang == "ru" else ("✅ Ich stimme zu" if lang == "de" else "✅ I agree"), callback_data="agree")],
        [InlineKeyboardButton("❌ Не принимаю" if lang == "ru" else ("❌ Ich lehne ab" if lang == "de" else "❌ I decline"), callback_data="disagree")]
    ]
    text = RULES.get(lang, RULES["ru"])
    if edit:
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


def clean_text(text):
    import re
    # Убираем markdown символы
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'#{1,6}\s', '', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    return text

def split_message(text, max_length=4000):
    text = clean_text(text)
    import re
    # Разбиваем по разделителю или по заголовкам с эмодзи
    if "・・・・・・・・・・" in text:
        segments = text.split("・・・・・・・・・・")
    else:
        # Разбиваем перед строками которые начинаются с эмодзи-заголовка
        segments = re.split(r'(?:\n)(?=[🧭✨🌿🔺🌱💬☀️📍🌟💡])', text)
    parts = []
    for seg in segments:
        seg = seg.strip()
        if not seg:
            continue
        if len(seg) <= max_length:
            parts.append(seg)
        else:
            # Длинный сегмент режем по двойным переносам
            paras = seg.split("\n\n")
            current = ""
            for para in paras:
                para = para.strip()
                if not para:
                    continue
                if len(current) + len(para) + 2 <= max_length:
                    current = (current + "\n\n" + para).strip() if current else para
                else:
                    if current:
                        parts.append(current)
                    current = para
            if current:
                parts.append(current)
    return parts if parts else [text]


def get_upgrade_keyboard(lang):
    labels = {
        "ru": ("✨ Да, хочу", "Позже"),
        "de": ("✨ Ja, ich möchte", "Später"),
        "en": ("✨ Yes, I want", "Later")
    }
    yes, no = labels.get(lang, labels["ru"])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(yes, callback_data="btn_pay")],
        [InlineKeyboardButton(no, callback_data="btn_back")]
    ])

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
        [InlineKeyboardButton("🌟 Mein Fundament", callback_data="btn_me")],
        [InlineKeyboardButton("🧭 Persönliches Jahr", callback_data="btn_year")],
        [InlineKeyboardButton("📍 Persönlicher Monat", callback_data="btn_month")],
        [InlineKeyboardButton("☀️ Persönlicher Tag", callback_data="btn_day")],
        [InlineKeyboardButton("💡 Lass uns reden", callback_data="btn_compass")],
        [InlineKeyboardButton("⚙️ Einstellungen", callback_data="btn_settings")],
    ],
    "en": [
        [InlineKeyboardButton("🌟 My Foundation", callback_data="btn_me")],
        [InlineKeyboardButton("🧭 Personal Year", callback_data="btn_year")],
        [InlineKeyboardButton("📍 Personal Month", callback_data="btn_month")],
        [InlineKeyboardButton("☀️ Personal Day", callback_data="btn_day")],
        [InlineKeyboardButton("💡 Let's talk", callback_data="btn_compass")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="btn_settings")],
    ],
}

SETTINGS_BUTTONS = {
    "ru": [
        [InlineKeyboardButton("🌐 Сменить язык", callback_data="btn_language")],
        [InlineKeyboardButton("👤 Мои данные", callback_data="btn_profile")],
        [InlineKeyboardButton("💳 Полный доступ", callback_data="btn_pay")],
        [InlineKeyboardButton("📋 Правила", callback_data="btn_rules")],
        [InlineKeyboardButton("◀️ Назад", callback_data="btn_back")],
    ],
    "de": [
        [InlineKeyboardButton("🌐 Sprache ändern", callback_data="btn_language")],
        [InlineKeyboardButton("👤 Meine Daten", callback_data="btn_profile")],
        [InlineKeyboardButton("💳 Vollzugang", callback_data="btn_pay")],
        [InlineKeyboardButton("📋 Regeln", callback_data="btn_rules")],
        [InlineKeyboardButton("◀️ Zurück", callback_data="btn_back")],
    ],
    "en": [
        [InlineKeyboardButton("🌐 Change language", callback_data="btn_language")],
        [InlineKeyboardButton("👤 My data", callback_data="btn_profile")],
        [InlineKeyboardButton("💳 Full access", callback_data="btn_pay")],
        [InlineKeyboardButton("📋 Rules", callback_data="btn_rules")],
        [InlineKeyboardButton("◀️ Back", callback_data="btn_back")],
    ],
}

async def show_main_menu(update_or_query, lang, edit=False):
    texts = {
        "ru": "Выбери раздел:",
        "de": "Wähle einen Bereich:",
        "en": "Choose a section:"
    }
    text = texts.get(lang, texts["ru"])
    keyboard = InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"]))
    if edit:
        await update_or_query.edit_message_text(text, reply_markup=keyboard)
    else:
        await update_or_query.message.reply_text(text, reply_markup=keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if user and user.get("agreed") and user.get("name") and user.get("day"):
        lang = user.get("lang", "ru")
        await show_main_menu(update, lang)
        return
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
    ]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    await update.message.reply_text(RULES.get(lang, RULES["ru"]))

async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user.get("day"):
        await update.message.reply_text("Данные не найдены. Начни с /start")
        return
    lang = user.get("lang", "ru")
    status = "Полный доступ ✅" if is_paid(user) else ("Пробный период 🌿" if is_trial_active(user) else "Доступ завершён 🔺")
    texts = {
        "ru": f"Имя: {user.get('name','-')}\nДата рождения: {user.get('day')}.{user.get('month')}.{user.get('year')}\nЯзык: {lang}\nСтатус: {status}",
        "de": f"Name: {user.get('name','-')}\nGeburtsdatum: {user.get('day')}.{user.get('month')}.{user.get('year')}\nSprache: {lang}\nStatus: {status}",
        "en": f"Name: {user.get('name','-')}\nDate of birth: {user.get('day')}.{user.get('month')}.{user.get('year')}\nLanguage: {lang}\nStatus: {status}"
    }
    await update.message.reply_text(texts.get(lang, texts["ru"]))

async def cmd_language(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
        [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
        [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
    ]
    await update.message.reply_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_pay(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    texts = {
        "ru": "Выбери тариф:",
        "de": "Wähle deinen Tarif:",
        "en": "Choose your plan:"
    }
    labels = {
        "ru": ["1 месяц — 15€", "6 месяцев — 78€", "12 месяцев — 159€"],
        "de": ["1 Monat — 15€", "6 Monate — 78€", "12 Monate — 159€"],
        "en": ["1 month — 15€", "6 months — 78€", "12 months — 159€"]
    }
    keyboard = [
        [InlineKeyboardButton(labels[lang][0], callback_data="pay_1m")],
        [InlineKeyboardButton(f"⭐ {labels[lang][1]}", callback_data="pay_6m")],
        [InlineKeyboardButton(labels[lang][2], callback_data="pay_12m")]
    ]
    await update.message.reply_text(texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if not is_paid(user):
        texts = {"ru": "У тебя нет активной подписки.", "de": "Du hast kein aktives Abonnement.", "en": "You have no active subscription."}
        await update.message.reply_text(texts.get(lang, texts["ru"]))
        return
    keyboard = [
        [InlineKeyboardButton("Да, отменить" if lang == "ru" else ("Ja, kündigen" if lang == "de" else "Yes, cancel"), callback_data="cancel_confirm")],
        [InlineKeyboardButton("Нет, оставить" if lang == "ru" else ("Nein, behalten" if lang == "de" else "No, keep it"), callback_data="cancel_abort")]
    ]
    texts = {
        "ru": "Ты уверена что хочешь отменить подписку? Доступ сохранится до конца оплаченного периода.",
        "de": "Bist du sicher, dass du kündigen möchtest? Der Zugang bleibt bis zum Ende des bezahlten Zeitraums.",
        "en": "Are you sure you want to cancel? Access will remain until the end of the paid period."
    }
    await update.message.reply_text(texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(keyboard))

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user.get("day"):
        await update.message.reply_text("Сначала введи дату рождения. Начни с /start")
        return
    if not has_access(user):
        await cmd_pay(update, context)
        return
    lang = user.get("lang", "ru")
    prompt = get_profile_prompt(lang, user, "me")
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
    for part in split_message(response.content[0].text):
        await update.message.reply_text(part)

async def cmd_year(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user.get("day"):
        await update.message.reply_text("Сначала введи дату рождения. Начни с /start")
        return
    if not has_access(user):
        await cmd_pay(update, context)
        return
    lang = user.get("lang", "ru")
    prompt = get_profile_prompt(lang, user, "year")
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
    for part in split_message(response.content[0].text):
        await update.message.reply_text(part)

async def cmd_month(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user.get("day"):
        await update.message.reply_text("Сначала введи дату рождения. Начни с /start")
        return
    if not has_access(user):
        await cmd_pay(update, context)
        return
    lang = user.get("lang", "ru")
    prompt = get_profile_prompt(lang, user, "month")
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
    for part in split_message(response.content[0].text):
        await update.message.reply_text(part)

async def cmd_day(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user.get("day"):
        await update.message.reply_text("Сначала введи дату рождения. Начни с /start")
        return
    if not has_access(user):
        await cmd_pay(update, context)
        return
    lang = user.get("lang", "ru")
    prompt = get_profile_prompt(lang, user, "day")
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=1500, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
    for part in split_message(response.content[0].text):
        await update.message.reply_text(part)

async def cmd_compass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = get_user(user_id)
    if not user or not user.get("day"):
        await update.message.reply_text("Сначала введи дату рождения. Начни с /start")
        return
    if not has_access(user):
        await cmd_pay(update, context)
        return
    lang = user.get("lang", "ru")
    user_sessions[user_id] = []
    texts = {
        "ru": "💬 Расскажи что сейчас происходит в твоей жизни — что беспокоит больше всего?",
        "de": "💬 Erzähl mir, was gerade in deinem Leben passiert — was beschäftigt dich am meisten?",
        "en": "💬 Tell me what's happening in your life right now — what concerns you the most?"
    }
    msg = texts.get(lang, texts["ru"])
    user_sessions[user_id] = [{"role": "assistant", "content": msg}]
    await update.message.reply_text(msg)

async def send_daily_messages(context: ContextTypes.DEFAULT_TYPE):
    users = get_all_paid_users()
    for row in users:
        user_id, day, month, year, lang, name = row
        if not day:
            continue
        user = {"day": day, "month": month, "year": year, "name": name, "lang": lang}
        prompt = get_profile_prompt(lang or "ru", user, "day_auto")
        try:
            response = client.messages.create(model="claude-sonnet-4-5", max_tokens=500, system=prompt, messages=[{"role": "user", "content": "Утреннее сообщение"}])
            await context.bot.send_message(chat_id=user_id, text=response.content[0].text)
        except Exception as e:
            print(f"Error sending to {user_id}: {e}", flush=True)

async def lang_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    save_user(user_id, lang=lang)
    user = get_user(user_id)
    if user and user.get("agreed"):
        if user.get("name") and user.get("day"):
            await show_main_menu(query, lang, edit=True)
        else:
            greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Das ist der Innere Kompass. Wie heißt du?", "en": "Hi! This is Inner Compass. What is your name?"}
            msg = greet.get(lang, greet["ru"])
            user_sessions[user_id] = [{"role": "assistant", "content": msg}]
            await query.edit_message_text(msg)
    else:
        await send_rules(query, lang, edit=True)

async def menu_btn_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    data = query.data

    if data == "btn_me":
        await query.edit_message_text({"ru": "🌟 Загружаю твой профиль...", "de": "🌟 Lade dein Profil...", "en": "🌟 Loading your profile..."}.get(lang, "🌟 Загружаю твой профиль..."))
        if not user or not user.get("day"):
            await context.bot.send_message(user_id, "Сначала введи дату рождения. Начни с /start")
            return
        if is_paid(user) or is_trial_active(user):
            prompt = get_profile_prompt(lang, user, "me")
            response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
            for part in split_message(response.content[0].text):
                await context.bot.send_message(user_id, part)
            await show_main_menu_msg(context, user_id, lang)
        else:
            if not check_free_limit(user_id, "me"):
                no_limit = {"ru": "Сегодня ты уже смотрела этот раздел. Загляни завтра или открой полный доступ.", "de": "Du hast diesen Bereich heute schon angesehen. Schau morgen wieder oder öffne den vollen Zugang.", "en": "You've already viewed this section today. Come back tomorrow or get full access."}
                await context.bot.send_message(user_id, no_limit.get(lang, no_limit["ru"]), reply_markup=get_upgrade_keyboard(lang))
                await show_main_menu_msg(context, user_id, lang)
                return
            increment_usage(user_id, "me")
            prompt = get_free_profile_prompt(lang, user, "me")
            response = client.messages.create(model="claude-sonnet-4-5", max_tokens=1000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
            reply = clean_text(response.content[0].text)
            await context.bot.send_message(user_id, reply)
            upgrade_text = {"ru": "Это краткий взгляд. Хочешь разобраться глубже?", "de": "Das ist ein kurzer Einblick. Möchtest du tiefer gehen?", "en": "This is a brief look. Want to go deeper?"}
            await context.bot.send_message(user_id, upgrade_text.get(lang, upgrade_text["ru"]), reply_markup=get_upgrade_keyboard(lang))
            await show_main_menu_msg(context, user_id, lang)

    elif data == "btn_year":
        await query.edit_message_text({"ru": "🧭 Анализирую твой год...", "de": "🧭 Analysiere dein Jahr...", "en": "🧭 Analysing your year..."}.get(lang, "🧭 Анализирую твой год..."))
        if not user or not user.get("day"):
            await context.bot.send_message(user_id, "Сначала введи дату рождения.")
            return
        if not has_access(user):
            await context.bot.send_message(user_id, "Для доступа нужна подписка.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Купить доступ", callback_data="btn_pay")]]))
            return
        prompt = get_profile_prompt(lang, user, "year")
        response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
        for part in split_message(response.content[0].text):
            await context.bot.send_message(user_id, part)
        await show_main_menu_msg(context, user_id, lang)

    elif data == "btn_month":
        await query.edit_message_text({"ru": "📍 Анализирую твой месяц...", "de": "📍 Analysiere deinen Monat...", "en": "📍 Analysing your month..."}.get(lang, "📍 Анализирую твой месяц..."))
        if not user or not user.get("day"):
            await context.bot.send_message(user_id, "Сначала введи дату рождения.")
            return
        if not has_access(user):
            await context.bot.send_message(user_id, "Для доступа нужна подписка.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Купить доступ", callback_data="btn_pay")]]))
            return
        prompt = get_profile_prompt(lang, user, "month")
        response = client.messages.create(model="claude-sonnet-4-5", max_tokens=3000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
        for part in split_message(response.content[0].text):
            await context.bot.send_message(user_id, part)
        await show_main_menu_msg(context, user_id, lang)

    elif data == "btn_day":
        await query.edit_message_text({"ru": "☀️ Анализирую твой день...", "de": "☀️ Analysiere deinen Tag...", "en": "☀️ Analysing your day..."}.get(lang, "☀️ Анализирую твой день..."))
        if not user or not user.get("day"):
            await context.bot.send_message(user_id, "Сначала введи дату рождения.")
            return
        if is_paid(user) or is_trial_active(user):
            prompt = get_profile_prompt(lang, user, "day")
            response = client.messages.create(model="claude-sonnet-4-5", max_tokens=1500, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
            for part in split_message(response.content[0].text):
                await context.bot.send_message(user_id, part)
            await show_main_menu_msg(context, user_id, lang)
        else:
            if not check_free_limit(user_id, "day"):
                no_limit = {"ru": "Сегодня ты уже смотрела этот раздел. Загляни завтра или открой полный доступ.", "de": "Du hast diesen Bereich heute schon angesehen.", "en": "You've already viewed this section today."}
                await context.bot.send_message(user_id, no_limit.get(lang, no_limit["ru"]), reply_markup=get_upgrade_keyboard(lang))
                await show_main_menu_msg(context, user_id, lang)
                return
            increment_usage(user_id, "day")
            prompt = get_free_profile_prompt(lang, user, "day")
            response = client.messages.create(model="claude-sonnet-4-5", max_tokens=500, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
            reply = clean_text(response.content[0].text)
            await context.bot.send_message(user_id, reply)
            upgrade_text = {"ru": "Это краткий взгляд. Хочешь разобраться глубже?", "de": "Das ist ein kurzer Einblick. Möchtest du tiefer gehen?", "en": "This is a brief look. Want to go deeper?"}
            await context.bot.send_message(user_id, upgrade_text.get(lang, upgrade_text["ru"]), reply_markup=get_upgrade_keyboard(lang))
            await show_main_menu_msg(context, user_id, lang)

    elif data == "btn_compass":
        if not user or not user.get("day"):
            await context.bot.send_message(user_id, "Сначала введи дату рождения.")
            return
        if not has_access(user):
            await context.bot.send_message(user_id, "Для доступа нужна подписка.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("💳 Купить доступ", callback_data="btn_pay")]]))
            return
        compass_state[user_id] = {"q_count": 0, "clarify_mode": False}
        user_sessions[user_id] = []
        first_q = {
            "ru": "Расскажи — что сейчас занимает мысли больше всего?",
            "de": "Erzähl mir — was beschäftigt dich gerade am meisten?",
            "en": "Tell me — what's on your mind the most right now?"
        }
        msg = first_q.get(lang, first_q["ru"])
        user_sessions[user_id] = [{"role": "assistant", "content": msg}]
        await query.edit_message_text(msg)

    elif data == "compass_yes":
        compass_state.pop(user_id, None)
        user_sessions[user_id] = []
        await show_main_menu(query, lang, edit=True)

    elif data == "compass_no":
        compass_state[user_id] = compass_state.get(user_id, {})
        compass_state[user_id]["clarify_mode"] = True
        compass_state[user_id]["clarify_count"] = 0
        texts = {
            "ru": "Хорошо. Что именно осталось непонятным?",
            "de": "Okay. Was genau ist unklar geblieben?",
            "en": "Okay. What exactly is still unclear?"
        }
        msg = texts.get(lang, texts["ru"])
        user_sessions[user_id].append({"role": "assistant", "content": msg})
        await query.edit_message_text(msg)

    elif data == "btn_settings":
        texts = {"ru": "⚙️ Настройки", "de": "⚙️ Einstellungen", "en": "⚙️ Settings"}
        await query.edit_message_text(texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(SETTINGS_BUTTONS.get(lang, SETTINGS_BUTTONS["ru"])))

    elif data == "btn_back":
        await show_main_menu(query, lang, edit=True)

    elif data == "btn_language":
        keyboard = [
            [InlineKeyboardButton("🇷🇺 Русский", callback_data="lang_ru")],
            [InlineKeyboardButton("🇩🇪 Deutsch", callback_data="lang_de")],
            [InlineKeyboardButton("🇬🇧 English", callback_data="lang_en")]
        ]
        await query.edit_message_text("Выберите язык / Sprache / Language:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "btn_profile":
        if not user:
            await context.bot.send_message(user_id, "Данные не найдены.")
            return
        status = "Полный доступ ✅" if is_paid(user) else ("Пробный период 🌿" if is_trial_active(user) else "Доступ завершён 🔺")
        texts = {
            "ru": f"Имя: {user.get('name','-')}\nДата рождения: {user.get('day')}.{user.get('month')}.{user.get('year')}\nСтатус: {status}",
            "de": f"Name: {user.get('name','-')}\nGeburtsdatum: {user.get('day')}.{user.get('month')}.{user.get('year')}\nStatus: {status}",
            "en": f"Name: {user.get('name','-')}\nDate of birth: {user.get('day')}.{user.get('month')}.{user.get('year')}\nStatus: {status}"
        }
        await context.bot.send_message(user_id, texts.get(lang, texts["ru"]))
        await show_main_menu_msg(context, user_id, lang)

    elif data == "btn_pay":
        labels = {
            "ru": ["1 месяц — 15€", "6 месяцев — 78€", "12 месяцев — 159€"],
            "de": ["1 Monat — 15€", "6 Monate — 78€", "12 Monate — 159€"],
            "en": ["1 month — 15€", "6 months — 78€", "12 months — 159€"]
        }
        keyboard = [
            [InlineKeyboardButton(labels[lang][0], callback_data="pay_1m")],
            [InlineKeyboardButton(f"⭐ {labels[lang][1]}", callback_data="pay_6m")],
            [InlineKeyboardButton(labels[lang][2], callback_data="pay_12m")],
            [InlineKeyboardButton("◀️ Назад" if lang == "ru" else ("◀️ Zurück" if lang == "de" else "◀️ Back"), callback_data="btn_back")]
        ]
        texts = {"ru": "Выбери тариф:", "de": "Wähle deinen Tarif:", "en": "Choose your plan:"}
        await query.edit_message_text(texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "btn_rules":
        await context.bot.send_message(user_id, RULES.get(lang, RULES["ru"]))
        await show_main_menu_msg(context, user_id, lang)

async def show_main_menu_msg(context, user_id, lang):
    texts = {"ru": "Выбери раздел:", "de": "Wähle einen Bereich:", "en": "Choose a section:"}
    await context.bot.send_message(user_id, texts.get(lang, texts["ru"]), reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"])))

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
        await send_rules(query, lang, edit=True)

async def gender_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    gender = "f" if query.data == "gender_f" else "m"
    save_user(user_id, gender=gender)
    # Сбрасываем сессию
    user_sessions[user_id] = []
    # Просим дату рождения
    date_q = {
        "ru": "Отлично! Теперь напиши дату рождения в формате ДД.ММ.ГГГГ",
        "de": "Super! Schreib jetzt dein Geburtsdatum im Format TT.MM.JJJJ",
        "en": "Great! Now write your date of birth in format DD.MM.YYYY"
    }
    msg = date_q.get(lang, date_q["ru"])
    user_sessions[user_id] = [{"role": "assistant", "content": msg}]
    await query.edit_message_text(msg)

async def pay_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    plan = query.data.replace("pay_", "")
    links = {"1m": "https://www.paypal.me/AlexandraEngel42/15EUR", "6m": "https://www.paypal.me/AlexandraEngel42/78EUR", "12m": "https://www.paypal.me/AlexandraEngel42/159EUR"}
    descriptions = {
        "ru": {"1m": "1 месяц — 15€", "6m": "6 месяцев — 78€", "12m": "12 месяцев — 159€"},
        "de": {"1m": "1 Monat — 15€", "6m": "6 Monate — 78€", "12m": "12 Monate — 159€"},
        "en": {"1m": "1 month — 15€", "6m": "6 months — 78€", "12m": "12 months — 159€"}
    }
    plan_label = descriptions[lang][plan]
    header = {
        "ru": f"Тариф: {plan_label}\n\nВыбери способ оплаты:",
        "de": f"Tarif: {plan_label}\n\nWähle die Zahlungsmethode:",
        "en": f"Plan: {plan_label}\n\nChoose payment method:"
    }
    transfer_label = {"ru": "🏦 Перевод", "de": "🏦 Überweisung", "en": "🏦 Bank transfer"}
    back_label = {"ru": "◀️ Назад", "de": "◀️ Zurück", "en": "◀️ Back"}
    pay_btns = [
        [InlineKeyboardButton("💳 PayPal", callback_data=f"paypal_{plan}"),
         InlineKeyboardButton(transfer_label.get(lang, "🏦 Перевод"), callback_data=f"sepa_{plan}")],
        [InlineKeyboardButton(back_label.get(lang, "◀️ Назад"), callback_data="btn_pay")]
    ]
    try:
        await query.edit_message_text(header.get(lang, header["ru"]), reply_markup=InlineKeyboardMarkup(pay_btns))
    except Exception:
        await context.bot.send_message(user_id, header.get(lang, header["ru"]), reply_markup=InlineKeyboardMarkup(pay_btns))

async def paypal_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    plan = query.data.replace("paypal_", "")
    links = {"1m": "https://www.paypal.me/AlexandraEngel42/15EUR", "6m": "https://www.paypal.me/AlexandraEngel42/78EUR", "12m": "https://www.paypal.me/AlexandraEngel42/159EUR"}
    descriptions = {
        "ru": {"1m": "1 месяц — 15€", "6m": "6 месяцев — 78€", "12m": "12 месяцев — 159€"},
        "de": {"1m": "1 Monat — 15€", "6m": "6 Monate — 78€", "12m": "12 Monate — 159€"},
        "en": {"1m": "1 month — 15€", "6m": "6 months — 78€", "12m": "12 months — 159€"}
    }
    texts = {
        "ru": f"💳 PayPal\n\nТариф: {descriptions['ru'][plan]}\n\n{links[plan]}\n\nПосле оплаты напиши @aeng0 — активирую доступ в течение 24 часов.",
        "de": f"💳 PayPal\n\nTarif: {descriptions['de'][plan]}\n\n{links[plan]}\n\nNach der Zahlung schreibe @aeng0 — ich aktiviere den Zugang innerhalb von 24 Stunden.",
        "en": f"💳 PayPal\n\nPlan: {descriptions['en'][plan]}\n\n{links[plan]}\n\nAfter payment write @aeng0 — I will activate access within 24 hours."
    }
    await query.edit_message_text(texts.get(lang, texts["ru"]))

async def sepa_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    plan = query.data.replace("sepa_", "")
    descriptions = {
        "ru": {"1m": "1 месяц — 15€", "6m": "6 месяцев — 78€", "12m": "12 месяцев — 159€"},
        "de": {"1m": "1 Monat — 15€", "6m": "6 Monate — 78€", "12m": "12 Monate — 159€"},
        "en": {"1m": "1 month — 15€", "6m": "6 months — 78€", "12m": "12 months — 159€"}
    }
    texts = {
        "ru": f"🏦 Банковский перевод (SEPA)\n\nТариф: {descriptions['ru'][plan]}\n\nIBAN: DE28 5002 4024 4782 1216 01\nBank: C24 Bank\nПолучатель: Alexandra Engel\nНазначение: {descriptions['ru'][plan]}\n\nПосле перевода напиши @aeng0 — активирую доступ в течение 24 часов.",
        "de": f"🏦 Banküberweisung (SEPA)\n\nTarif: {descriptions['de'][plan]}\n\nIBAN: DE28 5002 4024 4782 1216 01\nBank: C24 Bank\nEmpfänger: Alexandra Engel\nVerwendungszweck: {descriptions['de'][plan]}\n\nNach der Überweisung schreibe @aeng0 — ich aktiviere den Zugang innerhalb von 24 Stunden.",
        "en": f"🏦 Bank transfer (SEPA)\n\nPlan: {descriptions['en'][plan]}\n\nIBAN: DE28 5002 4024 4782 1216 01\nBank: C24 Bank\nRecipient: Alexandra Engel\nReference: {descriptions['en'][plan]}\n\nAfter transfer write @aeng0 — I will activate access within 24 hours."
    }
    await query.edit_message_text(texts.get(lang, texts["ru"]))

async def cancel_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if query.data == "cancel_confirm":
        texts = {
            "ru": "Подписка отменена. Доступ сохраняется до конца оплаченного периода.\n\nЧтобы отменить платёж в PayPal: зайди в PayPal → Платежи → Автоплатежи → отмени подписку.",
            "de": "Abonnement gekündigt. Der Zugang bleibt bis zum Ende des bezahlten Zeitraums.\n\nZum Kündigen in PayPal: PayPal → Zahlungen → Automatische Zahlungen → Abonnement kündigen.",
            "en": "Subscription cancelled. Access remains until the end of the paid period.\n\nTo cancel in PayPal: PayPal → Payments → Automatic payments → Cancel subscription."
        }
        await query.edit_message_text(texts.get(lang, texts["ru"]))
    else:
        texts = {"ru": "Отмена отменена 🌿 Подписка активна.", "de": "Kündigung abgebrochen 🌿 Abonnement aktiv.", "en": "Cancellation aborted 🌿 Subscription active."}
        await query.edit_message_text(texts.get(lang, texts["ru"]))

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
    cur.execute("DELETE FROM users WHERE user_id = %s", (target_id,))
    conn.commit()
    cur.close()
    conn.close()
    await update.message.reply_text(f"✅ Данные пользователя {target_id} сброшены.")

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
    await update.message.reply_text(f"✅ Доступ для {target_id} активирован на {days} дней до {paid_until.strftime('%d.%m.%Y')}.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    user = get_user(user_id)
    lang = user.get("lang", "ru") if user else "ru"
    if not user or not user.get("agreed"):
        await update.message.reply_text("Пожалуйста, начни с /start" if lang == "ru" else ("Bitte beginne mit /start" if lang == "de" else "Please start with /start"))
        return
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})
    # Compass диалог
    if user_id in compass_state:
        state = compass_state[user_id]
        q_count = state.get("q_count", 0)
        clarify_mode = state.get("clarify_mode", False)
        clarify_count = state.get("clarify_count", 0)

        user_sessions[user_id].append({"role": "user", "content": user_text})

        m_model = D.get_model(user.get("day"))
        py = D.get_year(user.get("day"), user.get("month"), datetime.datetime.now().year)
        pm = D.get_month(py, datetime.datetime.now().month)
        pd = D.get_day(pm, datetime.datetime.now().day)
        mi = D.MODELS.get(m_model, {})
        gender_word = "женский" if user.get("gender","f") == "f" else "мужской"
        name = user.get("name", "")

        profile_context = f"""Профиль человека:
Имя: {name}, пол: {gender_word}
Модель мышления: {mi.get("full_name", mi.get("name",""))}
Суть: {mi.get("profile","")}
Сильные стороны: {mi.get("strengths","")}
Риски: {mi.get("risks","")}
Личный год: {D.YEARS.get(py,"")}
Личный месяц: {D.MONTHS.get(pm,"")}
Личный день: {D.DAYS.get(pd,"")}"""

        if clarify_mode:
            # Режим уточнения после анализа
            if clarify_count < 3:
                clarify_sys = f"""{profile_context}

Человек сказал что ему что-то непонятно в анализе. Его сообщение: "{user_text}"

Задай ОДИН мягкий уточняющий вопрос чтобы понять что именно требует пояснения.
Обращайся только на ТЫ — никакого "вы".
Только вопрос. Коротко. Без вступления. Дипломатично."""
                response = client.messages.create(
                    model="claude-sonnet-4-5", max_tokens=200,
                    system=clarify_sys,
                    messages=[{"role": "user", "content": user_text}]
                )
                q_text = clean_text(response.content[0].text)
                state["clarify_count"] = clarify_count + 1
                state["clarify_mode"] = False
                state["need_clarify_answer"] = True
                compass_state[user_id] = state
                user_sessions[user_id].append({"role": "assistant", "content": q_text})
                await update.message.reply_text(q_text)
            else:
                compass_state.pop(user_id, None)
                user_sessions[user_id] = []
                await show_main_menu_msg(context, user_id, lang)
            return

        if state.get("need_clarify_answer"):
            # Получили ответ на уточняющий вопрос — даём пояснение и меню
            clarify_answer_sys = f"""{profile_context}

Выше история диалога с человеком. Он попросил пояснить что-то из анализа.
Дай короткое тёплое пояснение — 2-3 абзаца. Без новых вопросов.
Простым языком. Без давления. Только чистый текст."""
            response = client.messages.create(
                model="claude-sonnet-4-5", max_tokens=800,
                system=clarify_answer_sys,
                messages=user_sessions[user_id]
            )
            reply = clean_text(response.content[0].text)
            await update.message.reply_text(reply)
            compass_state.pop(user_id, None)
            user_sessions[user_id] = []
            await show_main_menu_msg(context, user_id, lang)
            return

        if q_count >= 4:
            # Достаточно информации — даём анализ
            analysis_sys = f"""{profile_context}

Выше диалог с {name}. Человек рассказал о своей ситуации.

Дай мягкий анализ ситуации через призму модели мышления и текущего периода.
Говори тепло и с пониманием — как умный близкий человек, который видит ситуацию со стороны.
Не ставь диагнозы. Не давай директивных указаний. Не используй слова "должна", "надо", "необходимо".

СТРУКТУРА:
2-3 абзаца — что происходит и почему, через призму личности и периода. Мягко и точно.
・・・・・・・・・・
2-3 конкретных шага — не приказы, а мягкие предложения что можно попробовать.

Только чистый текст. Никакого markdown. Живой тёплый стиль."""
            response = client.messages.create(
                model="claude-sonnet-4-5", max_tokens=2000,
                system=analysis_sys,
                messages=user_sessions[user_id]
            )
            reply = clean_text(response.content[0].text)
            for part in split_message(reply):
                await update.message.reply_text(part)
            state["q_count"] = 99
            compass_state[user_id] = state
            understood = {
                "ru": "Всё понятно?",
                "de": "Ist alles klar?",
                "en": "Is everything clear?"
            }
            btns = [[
                InlineKeyboardButton("✅ Да", callback_data="compass_yes"),
                InlineKeyboardButton("❓ Нет", callback_data="compass_no")
            ]]
            await update.message.reply_text(
                understood.get(lang, understood["ru"]),
                reply_markup=InlineKeyboardMarkup(btns)
            )
            return

        # Задаём следующий вопрос через Claude
        q_sys = f"""{profile_context}

Ты ведёшь мягкий диалог с {name} чтобы понять её ситуацию.
Выше история разговора. Это вопрос номер {q_count + 1} из 4.

Задай ОДИН следующий вопрос. Вопрос должен:
- Логично вытекать из предыдущего ответа
- Помогать человеку самому прийти к пониманию — не анкета, а живой разговор
- Быть простым, мягким, без давления
- Предполагать развёрнутый ответ
- Не содержать сложных слов или психологических терминов

Только вопрос. Без вступления типа "Понятно" или "Хорошо". Коротко."""
        response = client.messages.create(
            model="claude-sonnet-4-5", max_tokens=150,
            system=q_sys,
            messages=user_sessions[user_id]
        )
        next_q = clean_text(response.content[0].text)
        state["q_count"] = q_count + 1
        compass_state[user_id] = state
        user_sessions[user_id].append({"role": "assistant", "content": next_q})
        await update.message.reply_text(next_q)
        return

    if not user.get("name"):
        name = user_text.strip()
        save_user(user_id, name=name)
        user = get_user(user_id)
        # Спрашиваем пол
        gender_q = {
            "ru": f"Приятно познакомиться, {name}! Как мне к тебе обращаться?",
            "de": f"Schön, dich kennenzulernen, {name}! Wie soll ich dich ansprechen?",
            "en": f"Nice to meet you, {name}! How should I address you?"
        }
        gender_btns = {
            "ru": [InlineKeyboardButton("👩 Женский род", callback_data="gender_f"), InlineKeyboardButton("👨 Мужской род", callback_data="gender_m")],
            "de": [InlineKeyboardButton("👩 Weiblich", callback_data="gender_f"), InlineKeyboardButton("👨 Männlich", callback_data="gender_m")],
            "en": [InlineKeyboardButton("👩 She/her", callback_data="gender_f"), InlineKeyboardButton("👨 He/him", callback_data="gender_m")]
        }
        await update.message.reply_text(
            gender_q.get(lang, gender_q["ru"]),
            reply_markup=InlineKeyboardMarkup([gender_btns.get(lang, gender_btns["ru"])])
        )
        return
    elif not user.get("day"):
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", user_text)
        if m:
            save_user(user_id, birth_day=int(m.group(1)), birth_month=int(m.group(2)), birth_year=int(m.group(3)), trial_started_at=datetime.datetime.now())
            user = get_user(user_id)
            # Показываем меню после ввода даты
            welcome = {
                "ru": "Готово! Теперь выбери с чего начнём:",
                "de": "Fertig! Wähle, womit wir beginnen:",
                "en": "Done! Choose where to start:"
            }
            await update.message.reply_text(
                welcome.get(lang, welcome["ru"]),
                reply_markup=InlineKeyboardMarkup(MENU_BUTTONS.get(lang, MENU_BUTTONS["ru"]))
            )
            return
    paid = is_paid(user)
    sys_prompt = get_system_prompt(lang, user.get("name"), user.get("day"), user.get("month"), user.get("year"), paid=paid, gender=user.get("gender","f"))
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=1500, system=sys_prompt, messages=user_sessions[user_id])
    reply = response.content[0].text
    user_sessions[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

async def post_init(app):
    commands = [
        BotCommand("me", "Основа моей личности"),
        BotCommand("year", "Личный год"),
        BotCommand("month", "Личный месяц"),
        BotCommand("day", "Личный день"),
        BotCommand("compass", "Полный анализ"),
        BotCommand("language", "Сменить язык"),
        BotCommand("pay", "Полный доступ"),
        BotCommand("cancel", "Отменить подписку"),
        BotCommand("rules", "Правила"),
        BotCommand("profile", "Мои данные"),
    ]
    await app.bot.set_my_commands(commands)

if __name__ == "__main__":
    init_db()
    # Сбрасываем старые соединения перед стартом
    try:
        import urllib.request
        urllib.request.urlopen(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook?drop_pending_updates=true", timeout=10)
    except:
        pass
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    job_queue = app.job_queue
    job_queue.run_daily(send_daily_messages, time=datetime.time(hour=6, minute=0, tzinfo=datetime.timezone.utc))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("language", cmd_language))
    app.add_handler(CommandHandler("pay", cmd_pay))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CommandHandler("year", cmd_year))
    app.add_handler(CommandHandler("month", cmd_month))
    app.add_handler(CommandHandler("day", cmd_day))
    app.add_handler(CommandHandler("compass", cmd_compass))
    app.add_handler(CommandHandler("reset_user", admin_reset))
    app.add_handler(CommandHandler("grant_access", admin_grant))
    app.add_handler(CallbackQueryHandler(agree_cb, pattern="^(agree|disagree)$"))
    app.add_handler(CallbackQueryHandler(gender_cb, pattern="^gender_"))
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(menu_btn_cb, pattern="^btn_|^compass_"))
    app.add_handler(CallbackQueryHandler(pay_cb, pattern="^pay_"))
    app.add_handler(CallbackQueryHandler(paypal_cb, pattern="^paypal_"))
    app.add_handler(CallbackQueryHandler(sepa_cb, pattern="^sepa_"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern="^(cancel_confirm|cancel_abort)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None, drop_pending_updates=True)
