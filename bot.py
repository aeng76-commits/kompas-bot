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
            paid_until TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

def get_user(user_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name, birth_day, birth_month, birth_year, lang, agreed, trial_started_at, paid_until FROM users WHERE user_id = %s", (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"name": row[0], "day": row[1], "month": row[2], "year": row[3], "lang": row[4], "agreed": row[5], "trial_started_at": row[6], "paid_until": row[7]}
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
        return True
    delta = datetime.datetime.now() - user["trial_started_at"].replace(tzinfo=None)
    return delta.total_seconds() < 86400

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

ПРАВИЛА:
- Язык: {'русский' if lang == 'ru' else ('немецкий' if lang == 'de' else 'английский')}
- Обращение: ТЫ, имя точно: {name}, правильные окончания по полу
- Стиль: живой тёплый, как умный близкий человек — говори то что человек чувствовал но не мог сформулировать
- Короткие абзацы — максимум 3 предложения в абзаце
- Только чистый текст, никакого markdown, никаких иероглифов
- Запрещено: нумерология, вибрация, трансформация, вызовы, канцеляризмы
- Запрещено называть числа и номера периодов
- Текст должен вызывать эмоции и заставлять задуматься"""
    return base

def get_system_prompt(lang, name=None, birth_day=None, birth_month=None, birth_year=None, paid=False):
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


def split_message(text, max_length=4000):
    # Разбиваем по разделителю блоков
    blocks = text.split("・・・・・・・・・・")
    parts = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if len(block) <= max_length:
            parts.append(block)
        else:
            # Если блок слишком длинный — режем по абзацам
            while block:
                if len(block) <= max_length:
                    parts.append(block)
                    break
                split_at = block.rfind("\n\n", 0, max_length)
                if split_at == -1:
                    split_at = block.rfind("\n", 0, max_length)
                if split_at == -1:
                    split_at = max_length
                parts.append(block[:split_at].strip())
                block = block[split_at:].strip()
    return parts

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
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
        "ru": f"Имя: {user.get('name','—')}\nДата рождения: {user.get('day')}.{user.get('month')}.{user.get('year')}\nЯзык: {lang}\nСтатус: {status}",
        "de": f"Name: {user.get('name','—')}\nGeburtsdatum: {user.get('day')}.{user.get('month')}.{user.get('year')}\nSprache: {lang}\nStatus: {status}",
        "en": f"Name: {user.get('name','—')}\nDate of birth: {user.get('day')}.{user.get('month')}.{user.get('year')}\nLanguage: {lang}\nStatus: {status}"
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
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=2000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
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
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=2000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
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
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=2000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
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
    response = client.messages.create(model="claude-sonnet-4-5", max_tokens=1000, system=prompt, messages=[{"role": "user", "content": "Дай анализ"}])
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
            texts = {"ru": f"Язык изменён 🌿", "de": "Sprache geändert 🌿", "en": "Language changed 🌿"}
            await query.edit_message_text(texts.get(lang, texts["ru"]))
        else:
            greet = {"ru": "Привет! Это Внутренний Компас. Как тебя зовут?", "de": "Hallo! Das ist der Innere Kompass. Wie heißt du?", "en": "Hi! This is Inner Compass. What is your name?"}
            msg = greet.get(lang, greet["ru"])
            user_sessions[user_id] = [{"role": "assistant", "content": msg}]
            await query.edit_message_text(msg)
    else:
        await send_rules(query, lang, edit=True)

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
    texts = {
        "ru": f"Для оплаты тарифа {descriptions['ru'][plan]} перейди по ссылке:\n{links[plan]}\n\nПосле оплаты напиши @твой_username для активации доступа.",
        "de": f"Um den Tarif {descriptions['de'][plan]} zu bezahlen, folge dem Link:\n{links[plan]}\n\nNach der Zahlung schreibe @твой_username zur Aktivierung.",
        "en": f"To pay for {descriptions['en'][plan]} follow the link:\n{links[plan]}\n\nAfter payment write @твой_username to activate access."
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
    if not user.get("name"):
        save_user(user_id, name=user_text.strip())
        user = get_user(user_id)
    elif not user.get("day"):
        m = re.search(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", user_text)
        if m:
            save_user(user_id, birth_day=int(m.group(1)), birth_month=int(m.group(2)), birth_year=int(m.group(3)), trial_started_at=datetime.datetime.now())
            user = get_user(user_id)
    paid = is_paid(user)
    sys_prompt = get_system_prompt(lang, user.get("name"), user.get("day"), user.get("month"), user.get("year"), paid=paid)
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
    app.add_handler(CallbackQueryHandler(lang_cb, pattern="^lang_"))
    app.add_handler(CallbackQueryHandler(pay_cb, pattern="^pay_"))
    app.add_handler(CallbackQueryHandler(cancel_cb, pattern="^(cancel_confirm|cancel_abort)$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None, drop_pending_updates=True)
