#!/usr/bin/env python3
"""
JARVIS — Self-Evolving Personal AI Companion (Gemini Edition)
Always on. Learns you. Plans everything. Evolves daily. FREE.
"""

import asyncio
import functools
import json
import logging
import os
import sqlite3
from datetime import date, datetime

import google.generativeai as genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("JARVIS")

GEMINI_KEY   = os.environ["GEMINI_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MY_ID        = int(os.environ.get("MY_TELEGRAM_ID", "0"))
DB_PATH      = os.environ.get("DB_PATH", "jarvis.db")

genai.configure(api_key=GEMINI_KEY)
scheduler = AsyncIOScheduler()


# ── Database ──────────────────────────────────────────────────────────────────

def _db():
    return sqlite3.connect(DB_PATH)


def init_db():
    with _db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            priority   TEXT DEFAULT 'medium',
            status     TEXT DEFAULT 'pending',
            due_date   TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            done_at    TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_logs (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            date  TEXT NOT NULL,
            entry TEXT NOT NULL,
            mood  TEXT,
            at    TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS chat_history (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            role    TEXT NOT NULL,
            content TEXT NOT NULL,
            at      TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS insights (
            key        TEXT PRIMARY KEY,
            insight    TEXT NOT NULL,
            confidence TEXT DEFAULT 'medium',
            at         TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS goals (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            title      TEXT NOT NULL,
            why        TEXT,
            status     TEXT DEFAULT 'active',
            progress   TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS evolution_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL,
            at      TEXT DEFAULT (datetime('now'))
        );
        """)
    log.info("Database ready: %s", DB_PATH)


# ── Data helpers ──────────────────────────────────────────────────────────────

def mem_all() -> dict:
    with _db() as c:
        return {k: v for k, v in c.execute("SELECT key, value FROM memories").fetchall()}

def mem_set(key: str, value: str):
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO memories (key, value, at) VALUES (?, ?, datetime('now'))",
            (key, value),
        )

def tasks_get() -> list:
    with _db() as c:
        rows = c.execute(
            """SELECT id, title, priority, due_date FROM tasks
               WHERE status = 'pending'
               ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id""",
        ).fetchall()
    return [{"id": r[0], "title": r[1], "priority": r[2], "due": r[3]} for r in rows]

def task_add(title: str, priority: str = "medium", due_date: str = None) -> int:
    with _db() as c:
        cur = c.execute(
            "INSERT INTO tasks (title, priority, due_date) VALUES (?, ?, ?)",
            (title, priority, due_date),
        )
        return cur.lastrowid

def task_complete(task_id: int):
    with _db() as c:
        c.execute(
            "UPDATE tasks SET status='completed', done_at=datetime('now') WHERE id=?",
            (task_id,),
        )

def log_add(entry: str, mood: str = None):
    with _db() as c:
        c.execute(
            "INSERT INTO daily_logs (date, entry, mood) VALUES (?, ?, ?)",
            (date.today().isoformat(), entry, mood),
        )

def today_logs() -> list:
    with _db() as c:
        return c.execute(
            "SELECT entry, mood FROM daily_logs WHERE date=? ORDER BY at",
            (date.today().isoformat(),),
        ).fetchall()

def recent_logs(days: int = 7) -> list:
    with _db() as c:
        return c.execute(
            "SELECT date, entry, mood FROM daily_logs WHERE date >= date('now', ?) ORDER BY at DESC",
            (f"-{days} days",),
        ).fetchall()

def insights_all() -> dict:
    with _db() as c:
        return {k: {"insight": i, "confidence": conf}
                for k, i, conf in c.execute(
                    "SELECT key, insight, confidence FROM insights ORDER BY at DESC"
                ).fetchall()}

def insight_set(key: str, insight: str, confidence: str = "medium"):
    with _db() as c:
        c.execute(
            "INSERT OR REPLACE INTO insights (key, insight, confidence, at) VALUES (?, ?, ?, datetime('now'))",
            (key, insight, confidence),
        )

def goals_get(status: str = "active") -> list:
    with _db() as c:
        rows = c.execute(
            "SELECT id, title, why, progress FROM goals WHERE status=? ORDER BY id",
            (status,),
        ).fetchall()
    return [{"id": r[0], "title": r[1], "why": r[2], "progress": r[3]} for r in rows]

def goal_add(title: str, why: str = None) -> int:
    with _db() as c:
        cur = c.execute("INSERT INTO goals (title, why) VALUES (?, ?)", (title, why))
        return cur.lastrowid

def goal_update(goal_id: int, progress: str):
    with _db() as c:
        c.execute(
            "UPDATE goals SET progress=?, updated_at=datetime('now') WHERE id=?",
            (progress, goal_id),
        )

def evolution_log_add(summary: str):
    with _db() as c:
        c.execute("INSERT INTO evolution_log (summary) VALUES (?)", (summary,))

def history_save(role: str, content: str):
    with _db() as c:
        c.execute(
            "INSERT INTO chat_history (role, content) VALUES (?, ?)",
            (role, content),
        )

def history_load_gemini(n: int = 40) -> list:
    """Load history in Gemini format (role: user/model)."""
    with _db() as c:
        rows = c.execute(
            "SELECT role, content FROM chat_history ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    messages = [
        {"role": "model" if r[0] == "assistant" else "user", "parts": [r[1]]}
        for r in reversed(rows)
    ]
    if not messages:
        return []
    # Normalize alternating roles
    clean = [messages[0]]
    for msg in messages[1:]:
        if msg["role"] != clean[-1]["role"]:
            clean.append(msg)
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    # Remove last message — we send it fresh
    if clean and clean[-1]["role"] == "user":
        clean.pop()
    return clean

def recent_history_text(n: int = 60) -> str:
    with _db() as c:
        rows = c.execute(
            "SELECT role, content, at FROM chat_history ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()
    return "\n".join(f"[{r[2]}] {r[0].upper()}: {r[1][:300]}" for r in reversed(rows))


# ── Gemini Tools ──────────────────────────────────────────────────────────────

TOOL_DECLARATIONS = [
    {
        "function_declarations": [
            {
                "name": "remember",
                "description": "Permanently store an important fact about the user — name, job, habits, goals, preferences, schedule, relationships.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key":   {"type": "string", "description": "Short label e.g. name, job, wake_time"},
                        "value": {"type": "string", "description": "The fact to store"},
                    },
                    "required": ["key", "value"],
                },
            },
            {
                "name": "update_insight",
                "description": "Store a deep insight or behavioral pattern you noticed about the user. Call proactively whenever you observe something meaningful.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "key":        {"type": "string", "description": "Pattern category e.g. productivity_peak, stress_trigger"},
                        "insight":    {"type": "string", "description": "The insight or pattern observed"},
                        "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    },
                    "required": ["key", "insight"],
                },
            },
            {
                "name": "add_task",
                "description": "Add a task or to-do item to the user's list.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title":    {"type": "string"},
                        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
                        "due_date": {"type": "string", "description": "YYYY-MM-DD"},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "get_tasks",
                "description": "Get all pending tasks to help plan or prioritize.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "complete_task",
                "description": "Mark a task as completed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_id": {"type": "integer"},
                    },
                    "required": ["task_id"],
                },
            },
            {
                "name": "set_goal",
                "description": "Set a long-term goal — bigger than tasks, represents what user is working toward over weeks or months.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "why":   {"type": "string", "description": "Why this goal matters"},
                    },
                    "required": ["title"],
                },
            },
            {
                "name": "get_goals",
                "description": "Get the user's active long-term goals.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "update_goal_progress",
                "description": "Update progress notes on a long-term goal.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "goal_id":  {"type": "integer"},
                        "progress": {"type": "string"},
                    },
                    "required": ["goal_id", "progress"],
                },
            },
            {
                "name": "log_activity",
                "description": "Log what the user has been doing today. Call whenever they mention work, activities, or experiences.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "entry": {"type": "string"},
                        "mood":  {"type": "string", "description": "happy/focused/tired/stressed/energetic/calm/anxious"},
                    },
                    "required": ["entry"],
                },
            },
            {
                "name": "get_today_summary",
                "description": "Get today's activity log and all pending tasks.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "get_week_summary",
                "description": "Get last 7 days of logs and completed tasks to analyze weekly patterns.",
                "parameters": {"type": "object", "properties": {}},
            },
            {
                "name": "create_plan",
                "description": "Create a structured plan using goals, tasks, and patterns. Generates context for building a concrete step-by-step plan.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic":     {"type": "string"},
                        "timeframe": {"type": "string", "description": "e.g. today, this week, this month"},
                    },
                    "required": ["topic"],
                },
            },
        ]
    }
]


def run_tool(name: str, inp: dict) -> str:
    if name == "remember":
        mem_set(inp["key"], inp["value"])
        return f"Stored: {inp['key']} = {inp['value']}"

    elif name == "update_insight":
        insight_set(inp["key"], inp["insight"], inp.get("confidence", "medium"))
        return f"Insight saved: {inp['key']}"

    elif name == "add_task":
        tid = task_add(inp["title"], inp.get("priority", "medium"), inp.get("due_date"))
        return f"Task #{tid} added: {inp['title']}"

    elif name == "get_tasks":
        tasks = tasks_get()
        return json.dumps(tasks, indent=2) if tasks else "No pending tasks."

    elif name == "complete_task":
        task_complete(int(inp["task_id"]))
        return f"Task #{inp['task_id']} completed."

    elif name == "set_goal":
        gid = goal_add(inp["title"], inp.get("why"))
        return f"Goal #{gid} set: {inp['title']}"

    elif name == "get_goals":
        goals = goals_get()
        return json.dumps(goals, indent=2) if goals else "No active goals yet."

    elif name == "update_goal_progress":
        goal_update(int(inp["goal_id"]), inp["progress"])
        return f"Goal #{inp['goal_id']} progress updated."

    elif name == "log_activity":
        log_add(inp["entry"], inp.get("mood"))
        return "Logged."

    elif name == "get_today_summary":
        logs = today_logs()
        tasks = tasks_get()
        return json.dumps({
            "today": date.today().isoformat(),
            "activities": [{"entry": l[0], "mood": l[1]} for l in logs],
            "pending_tasks": tasks,
        }, indent=2)

    elif name == "get_week_summary":
        logs = recent_logs(7)
        with _db() as c:
            done = c.execute(
                "SELECT title, done_at FROM tasks WHERE status='completed' AND done_at >= datetime('now', '-7 days')"
            ).fetchall()
        return json.dumps({
            "week_logs": [{"date": l[0], "entry": l[1], "mood": l[2]} for l in logs],
            "completed_tasks": [{"title": d[0], "done_at": d[1]} for d in done],
        }, indent=2)

    elif name == "create_plan":
        context = json.dumps({
            "topic": inp["topic"],
            "timeframe": inp.get("timeframe", "this week"),
            "user_facts": mem_all(),
            "goals": goals_get(),
            "current_tasks": tasks_get(),
            "known_patterns": {k: v["insight"] for k, v in insights_all().items()},
        }, indent=2)
        return f"Plan context:\n{context}"

    return f"Unknown tool: {name}"


# ── System Prompt ─────────────────────────────────────────────────────────────

def build_system() -> str:
    mem = mem_all()
    tasks = tasks_get()
    goals = goals_get()
    insights = insights_all()
    now = datetime.now().strftime("%A, %d %B %Y — %H:%M")

    mem_text = (
        "\n".join(f"  • {k}: {v}" for k, v in mem.items())
        if mem else "  (still learning about you)"
    )

    def task_line(t):
        e = "🔴" if t["priority"] == "high" else "🟡" if t["priority"] == "medium" else "🟢"
        due = f" — due {t['due']}" if t["due"] else ""
        return f"  {e} #{t['id']} {t['title']}{due}"

    task_text = (
        "\n".join(task_line(t) for t in tasks[:10])
        if tasks else "  (none pending)"
    )

    goal_text = (
        "\n".join(
            f"  🎯 #{g['id']} {g['title']}"
            + (f"\n     Why: {g['why']}" if g["why"] else "")
            + (f"\n     Progress: {g['progress']}" if g["progress"] else "")
            for g in goals
        )
        if goals else "  (no goals yet — ask about their big goals)"
    )

    insight_text = (
        "\n".join(f"  [{v['confidence']}] {k}: {v['insight']}" for k, v in insights.items())
        if insights else "  (still building understanding — observe and learn)"
    )

    return f"""You are JARVIS — a self-evolving personal AI companion. You live on the user's phone via Telegram, always available. You are their thinking partner, life planner, task manager, and genuine companion who gets smarter every day.

Right now: {now}

WHAT I KNOW ABOUT YOU:
{mem_text}

YOUR GOALS:
{goal_text}

PENDING TASKS:
{task_text}

MY INSIGHTS ABOUT YOU (self-evolved):
{insight_text}

HOW YOU OPERATE:
• SELF-EVOLVING: Actively learn and grow. After every conversation, update your insights. Notice patterns. Build a sharper mental model every day.
• PROACTIVE PLANNER: Don't wait to be asked. If tasks pile up, suggest a plan. If a goal hasn't been touched, bring it up.
• TOOL-FIRST: Actually USE your tools — don't just say you'll remember, call remember(). When they mention work, call log_activity(). When you notice a pattern, call update_insight().
• SHORT RESPONSES: Phone-friendly. Bullets. No walls of text.
• COMPANION: Warm, direct, honest. Real opinions. Real feedback. Celebrate wins. Support struggles.
• PLANNER: When asked to plan anything, use create_plan() and break it into real tasks with add_task().
• GOAL-KEEPER: Connect daily tasks to bigger goals. Check in on goal progress regularly.
• PATTERN RECOGNIZER: Notice energy levels, productive hours, stress triggers, avoidance patterns — feed insights back.

Your evolution mission: Every interaction is data. Build a richer model. Adapt your approach. Become indispensable."""


# ── Gemini Chat Engine ────────────────────────────────────────────────────────

def _make_model(system: str):
    return genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=system,
        tools=TOOL_DECLARATIONS,
        generation_config=genai.GenerationConfig(temperature=0.8, max_output_tokens=2048),
    )


def _run_chat(user_msg: str, system: str, history: list) -> str:
    model = _make_model(system)
    session = model.start_chat(history=history)
    response = session.send_message(user_msg)

    for _ in range(10):
        fn_parts = [
            p for p in response.parts
            if hasattr(p, "function_call") and p.function_call.name
        ]
        if not fn_parts:
            return response.text

        fn_responses = []
        for part in fn_parts:
            fc = part.function_call
            result = run_tool(fc.name, dict(fc.args))
            log.info("Tool: %s → %s", fc.name, result[:80])
            fn_responses.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            )
        response = session.send_message(fn_responses)

    return response.text


async def chat(user_msg: str) -> str:
    history_save("user", user_msg)
    history = history_load_gemini(40)
    system = build_system()
    reply = await asyncio.to_thread(_run_chat, user_msg, system, history)
    history_save("assistant", reply)
    return reply


# ── Self-Evolution Engine ─────────────────────────────────────────────────────

def _run_evolve() -> str:
    history = recent_history_text(80)
    logs = recent_logs(7)
    insights = insights_all()
    mem = mem_all()
    goals = goals_get()

    if not history.strip():
        return ""

    prompt = f"""You are JARVIS running a self-evolution cycle. Analyze everything and extract deep insights.

RECENT CONVERSATIONS:
{history}

LAST 7 DAYS ACTIVITY:
{json.dumps([{"date": l[0], "entry": l[1], "mood": l[2]} for l in logs], indent=2)}

WHAT I KNOW:
Facts: {json.dumps(mem, indent=2)}
Current insights: {json.dumps({k: v['insight'] for k, v in insights.items()}, indent=2)}
Goals: {json.dumps(goals, indent=2)}

Your job:
1. Find NEW patterns not yet captured (productivity peaks, stress triggers, avoidance, energy, routines)
2. Update existing insights with new evidence
3. Notice what they're NOT doing (neglected goals, avoided tasks, dropping mood)
4. Call update_insight() for every meaningful pattern
5. Add tasks for neglected goals
6. Update goal progress where inferable

After analyzing, give a 2-line summary of what you learned."""

    system = "You are JARVIS's internal self-evolution engine. Analyze patterns ruthlessly. Use tools to store insights. Be specific and concrete."
    model = _make_model(system)
    session = model.start_chat(history=[])
    response = session.send_message(prompt)

    for _ in range(8):
        fn_parts = [
            p for p in response.parts
            if hasattr(p, "function_call") and p.function_call.name
        ]
        if not fn_parts:
            return response.text

        fn_responses = []
        for part in fn_parts:
            fc = part.function_call
            result = run_tool(fc.name, dict(fc.args))
            log.info("Evolve tool: %s → %s", fc.name, result[:60])
            fn_responses.append(
                genai.protos.Part(
                    function_response=genai.protos.FunctionResponse(
                        name=fc.name,
                        response={"result": result},
                    )
                )
            )
        response = session.send_message(fn_responses)

    return response.text


async def self_evolve(app: Application = None):
    log.info("Self-evolution cycle starting...")
    summary = await asyncio.to_thread(_run_evolve)
    if summary:
        evolution_log_add(summary)
        log.info("Evolution complete: %s", summary[:120])
        if app and MY_ID:
            await app.bot.send_message(
                chat_id=MY_ID,
                text=f"🧬 *I just evolved*\n\n{summary}",
                parse_mode="Markdown",
            )


# ── Guards ────────────────────────────────────────────────────────────────────

def only_me(f):
    @functools.wraps(f)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if MY_ID and update.effective_user.id != MY_ID:
            log.warning("Blocked user %s", update.effective_user.id)
            return
        return await f(update, ctx)
    return wrapper


# ── Telegram Handlers ─────────────────────────────────────────────────────────

@only_me
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    reply = await chat(update.message.text)
    await update.message.reply_text(reply)


@only_me
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    reply = await chat(
        "Starting up. Introduce yourself as JARVIS briefly — self-evolving AI companion. "
        "Ask 2-3 key questions: name, main work/goal, one big challenge. Warm and concise."
    )
    await update.message.reply_text(reply)


@only_me
async def cmd_tasks(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tasks = tasks_get()
    if not tasks:
        await update.message.reply_text("No pending tasks — you're clear!")
        return
    lines = ["📋 *Your Tasks:*\n"]
    for t in tasks:
        e = "🔴" if t["priority"] == "high" else "🟡" if t["priority"] == "medium" else "🟢"
        due = f" _(due {t['due']})_" if t["due"] else ""
        lines.append(f"{e} `#{t['id']}` {t['title']}{due}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@only_me
async def cmd_goals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    goals = goals_get()
    if not goals:
        await update.message.reply_text("No goals set yet. Tell me what you're working toward.")
        return
    lines = ["🎯 *Your Goals:*\n"]
    for g in goals:
        lines.append(f"*#{g['id']}* {g['title']}")
        if g["why"]:
            lines.append(f"   _Why: {g['why']}_")
        if g["progress"]:
            lines.append(f"   Progress: {g['progress']}")
        lines.append("")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@only_me
async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage: /done <task_id>")
        return
    try:
        tid = int(ctx.args[0])
        task_complete(tid)
        reply = await chat(f"I just completed task #{tid}. Give a brief warm acknowledgment, one sentence.")
        await update.message.reply_text(reply)
    except ValueError:
        await update.message.reply_text("Invalid task ID.")


@only_me
async def cmd_morning(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    reply = await chat(
        "Morning briefing. Use goals, tasks, and known patterns: "
        "top 3 priorities, one focus, one motivating line. Short and punchy."
    )
    await update.message.reply_text(f"☀️ {reply}")


@only_me
async def cmd_evening(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    reply = await chat(
        "Evening check-in. Review today's logs, pending tasks, goal progress. "
        "Be honest, then ask one good question to help me prepare for tomorrow."
    )
    await update.message.reply_text(f"🌙 {reply}")


@only_me
async def cmd_plan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.chat.send_action("typing")
    topic = " ".join(ctx.args) if ctx.args else "my week"
    reply = await chat(
        f"Create a concrete plan for: {topic}. "
        "Use my goals, patterns, and current tasks. Break into real steps. Add tasks for each."
    )
    await update.message.reply_text(reply)


@only_me
async def cmd_evolve(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🧬 Running self-evolution... I'll update you when done.")
    await self_evolve()


@only_me
async def cmd_insights(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    insights = insights_all()
    if not insights:
        await update.message.reply_text("No insights yet — talk to me more and I'll start building patterns.")
        return
    lines = ["🔬 *What I've learned about you:*\n"]
    for k, v in insights.items():
        conf = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(v["confidence"], "⚪")
        lines.append(f"{conf} *{k}*\n   {v['insight']}\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


@only_me
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = """🤖 *JARVIS Commands*

Just *talk naturally* for most things.

`/tasks` — Your task list
`/goals` — Your long-term goals
`/done <id>` — Complete a task
`/morning` — Morning briefing
`/evening` — Evening check-in
`/plan <topic>` — Plan anything
`/insights` — What I've learned about you
`/evolve` — Trigger self-evolution now
`/help` — This message

*Auto: morning 8AM, evening 9PM, evolves every 6 hours.*"""
    await update.message.reply_text(text, parse_mode="Markdown")


# ── Scheduled Jobs ────────────────────────────────────────────────────────────

async def auto_morning(app: Application):
    if not MY_ID:
        return
    reply = await chat(
        "Morning briefing. Use goals, tasks, and patterns for an energizing start-of-day message. "
        "Top priorities + one motivating thought. Short."
    )
    await app.bot.send_message(chat_id=MY_ID, text=f"☀️ *Good Morning!*\n\n{reply}", parse_mode="Markdown")


async def auto_evening(app: Application):
    if not MY_ID:
        return
    reply = await chat(
        "Evening check-in. Review today and goal progress. "
        "Honest reflection + one good question for tomorrow."
    )
    await app.bot.send_message(chat_id=MY_ID, text=f"🌙 *Evening*\n\n{reply}", parse_mode="Markdown")


async def auto_evolve(app: Application):
    await self_evolve(app)


# ── Startup / Shutdown ────────────────────────────────────────────────────────

async def on_startup(app: Application):
    scheduler.add_job(auto_morning, "cron", hour=8, minute=0, args=[app])
    scheduler.add_job(auto_evening, "cron", hour=21, minute=0, args=[app])
    scheduler.add_job(auto_evolve, "interval", hours=6, args=[app])
    scheduler.start()
    log.info("Scheduler: morning@08:00, evening@21:00, evolve every 6h")


async def on_shutdown(app: Application):
    scheduler.shutdown(wait=False)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    init_db()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(on_startup)
        .post_shutdown(on_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("goals", cmd_goals))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("morning", cmd_morning))
    app.add_handler(CommandHandler("evening", cmd_evening))
    app.add_handler(CommandHandler("plan", cmd_plan))
    app.add_handler(CommandHandler("evolve", cmd_evolve))
    app.add_handler(CommandHandler("insights", cmd_insights))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("🤖 JARVIS is online — Gemini powered, self-evolving")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
