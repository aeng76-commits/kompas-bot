import os
import anthropic
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

print("Bot starting", flush=True)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY")

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

SYSTEM_PROMPT = "You are Inner Compass assistant. Speak Russian only. Start with warm greeting, ask name, then birthdate. Calculate personal year: sum birth day + birth month + 2026, reduce to single digit. Personal month: personal year + current month 5, reduce. Ask 3-5 context questions before analysis. Warm tone, no mysticism, no predictions."

user_sessions = {}

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_text = update.message.text
    if user_id not in user_sessions:
        user_sessions[user_id] = []
    user_sessions[user_id].append({"role": "user", "content": user_text})
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=user_sessions[user_id]
    )
    reply = response.content[0].text
    user_sessions[user_id].append({"role": "assistant", "content": reply})
    await update.message.reply_text(reply)

if __name__ == "__main__":
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling(stop_signals=None)
