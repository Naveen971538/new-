import os
import asyncio
import logging
import datetime
from dotenv import load_dotenv
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from apscheduler.schedulers.asyncio import AsyncIOScheduler

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MY_TELEGRAM_ID = int(os.getenv("MY_TELEGRAM_ID"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("jarvis.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

MEMORY_FILE = "jarvis_memory.txt"
EVOLUTION_LOG = "jarvis_evolution.log"

def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return f.read()
    return ""

def save_memory(content: str):
    with open(MEMORY_FILE, "w") as f:
        f.write(content)

def log_evolution(entry: str):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(EVOLUTION_LOG, "a") as f:
        f.write(f"[{timestamp}] {entry}\n")

async def ask_gemini(prompt: str, context: str = "") -> str:
    try:
        system_context = (
            "You are JARVIS, a highly intelligent, self-evolving AI companion. "
            "You are helpful, witty, and deeply personal. You remember past conversations "
            "and grow smarter over time. Always be concise yet insightful.\n\n"
        )
        if context:
            system_context += f"Memory context:\n{context}\n\n"
        full_prompt = system_context + prompt
        response = model.generate_content(full_prompt)
        return response.text
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return f"I encountered an error: {e}"

async def evolve_self():
    memory = load_memory()
    prompt = (
        "Based on your recent interactions and memory, suggest one small improvement "
        "to how JARVIS should respond or behave. Be specific and actionable. "
        "Keep it under 100 words."
    )
    suggestion = await ask_gemini(prompt, memory)
    log_evolution(suggestion)
    logger.info(f"Self-evolution suggestion: {suggestion}")

async def daily_check_in(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.datetime.now().strftime("%A, %B %d at %I:%M %p")
    prompt = f"It's {now}. Send a brief, motivating good morning message to your user. Keep it under 50 words."
    message = await ask_gemini(prompt)
    await context.bot.send_message(chat_id=MY_TELEGRAM_ID, text=f"Good morning! {message}")
    await evolve_self()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_TELEGRAM_ID:
        await update.message.reply_text("Unauthorized access.")
        return
    await update.message.reply_text(
        "JARVIS online. I am your self-evolving AI companion, powered by Gemini. "
        "How can I assist you today?"
    )

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_TELEGRAM_ID:
        return
    memory = load_memory()
    memory_size = len(memory.split("\n")) if memory else 0
    evolution_count = 0
    if os.path.exists(EVOLUTION_LOG):
        with open(EVOLUTION_LOG, "r") as f:
            evolution_count = len(f.readlines())
    await update.message.reply_text(
        f"JARVIS Status:\n"
        f"Memory entries: {memory_size} lines\n"
        f"Evolution cycles: {evolution_count}\n"
        f"Model: gemini-1.5-flash\n"
        f"Status: Online"
    )

async def clear_memory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_TELEGRAM_ID:
        return
    save_memory("")
    await update.message.reply_text("Memory cleared.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != MY_TELEGRAM_ID:
        await update.message.reply_text("Unauthorized.")
        return

    user_message = update.message.text
    memory = load_memory()

    response = await ask_gemini(user_message, memory)

    new_memory_entry = (
        f"User: {user_message}\nJARVIS: {response}\n"
        f"Time: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n---\n"
    )
    updated_memory = memory + new_memory_entry
    lines = updated_memory.split("\n")
    if len(lines) > 500:
        updated_memory = "\n".join(lines[-500:])
    save_memory(updated_memory)

    await update.message.reply_text(response)

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("clearmemory", clear_memory))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        daily_check_in,
        "cron",
        hour=8,
        minute=0,
        args=[app],
    )
    scheduler.start()

    logger.info("JARVIS is starting up...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
