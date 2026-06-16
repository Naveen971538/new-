#!/usr/bin/env python3
"""
JARVIS — Self-Evolving Personal AI Companion (iMessage Edition)
Runs on macOS. Reads iMessages via chat.db, replies via AppleScript.
"""

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import time
from datetime import date, datetime
from pathlib import Path

import google.generativeai as genai
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

load_dotenv()

GEMINI_KEY    = os.environ["GEMINI_API_KEY"]
MY_IMESSAGE_ID = os.environ["MY_IMESSAGE_ID"]   # e.g. +919876543210 or your@email.com
DB_PATH       = os.environ.get("DB_PATH", str(Path.home() / "jarvis/jarvis.db"))
MESSAGES_DB   = str(Path.home() / "Library/Messages/chat.db")
POLL_INTERVAL = 2   # seconds between message checks

genai.configure(api_key=GEMINI_KEY)
scheduler = AsyncIOScheduler()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(str(Path.home() / "jarvis/jarvis.log")),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("JARVIS")


# ── iMessage Send/Receive ─────────────────────────────────────────────────────

def send_imessage(text: str):
    safe = text.replace("\\", "\\\\").replace('"', '\\"')
    script = f'''
tell application "Messages"
    set svc to 1st service whose service type is iMessage
    set bdy to buddy "{MY_IMESSAGE_ID}" of svc
    send "{safe}" to bdy
end tell
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0:
        log.error("iMessage send failed: %s", result.stderr.strip())


def get_last_rowid() -> int:
    try:
        conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        row = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM message").fetchone()
        conn.close()
        return row[0]
    except Exception as e:
        log.error("chat.db read error: %s", e)
        return 0


def poll_new_messages(since_rowid: int) -> list[tuple[int, str]]:
    """Return list of (rowid, text) for new incoming messages from MY_IMESSAGE_ID."""
    try:
        conn = sqlite3.connect(f"file:{MESSAGES_DB}?mode=ro", uri=True)
        rows = conn.execute("""
            SELECT m.rowid, m.text
            FROM   message m
            JOIN   handle  h ON m.handle_id = h.rowid
            WHERE  m.rowid > ?
              AND  m.is_from_me = 0
              AND  m.text IS NOT NULL
              AND  h.id = ?
            ORDER  BY m.rowid
        """, (since_rowid, MY_IMESSAGE_ID)).fetchall()
        conn.close()
        return rows
    except Exception as e:
        log.error("poll error: %s", e)
        return []


# ── JARVIS SQLite Database ────────────────────────────────────────────────────

def _db():
    return sqlite3.connect(DB_PATH)


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
        c.execute("INSERT OR REPLACE INTO memories (key, value, at) VALUES (?, ?, datetime('now'))", (key, value))

def tasks_get() -> list:
    with _db() as c:
        rows = c.execute("""
            SELECT id, title, priority, due_date FROM tasks
            WHERE status = 'pending'
            ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 ELSE 3 END, id
        """).fetchall()
    return [{"id": r[0], "title": r[1], "priority": r[2], "due": r[3]} for r in rows]

def task_add(title: str, priority: str = "medium", due_date: str = None) -> int:
    with _db() as c:
        return c.execute("INSERT INTO tasks (title, priority, due_date) VALUES (?, ?, ?)",
                         (title, priority, due_date)).lastrowid

def task_complete(task_id: int):
    with _db() as c:
        c.execute("UPDATE tasks SET status='completed', done_at=datetime('now') WHERE id=?", (task_id,))

def log_add(entry: str, mood: str = None):
    with _db() as c:
        c.execute("INSERT INTO daily_logs (date, entry, mood) VALUES (?, ?, ?)",
                  (date.today().isoformat(), entry, mood))

def today_logs() -> list:
    with _db() as c:
        return c.execute("SELECT entry, mood FROM daily_logs WHERE date=? ORDER BY at",
                         (date.today().isoformat(),)).fetchall()

def recent_logs(days: int = 7) -> list:
    with _db() as c:
        return c.execute("SELECT date, entry, mood FROM daily_logs WHERE date >= date('now', ?) ORDER BY at DESC",
                         (f"-{days} days",)).fetchall()

def insights_all() -> dict:
    with _db() as c:
        return {k: {"insight": i, "confidence": conf}
                for k, i, conf in c.execute("SELECT key, insight, confidence FROM insights ORDER BY at DESC").fetchall()}

def insight_set(key: str, insight: str, confidence: str = "medium"):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO insights (key, insight, confidence, at) VALUES (?, ?, ?, datetime('now'))",
                  (key, insight, confidence))

def goals_get(status: str = "active") -> list:
    with _db() as c:
        rows = c.execute("SELECT id, title, why, progress FROM goals WHERE status=? ORDER BY id", (status,)).fetchall()
    return [{"id": r[0], "title": r[1], "why": r[2], "progress": r[3]} for r in rows]

def goal_add(title: str, why: str = None) -> int:
    with _db() as c:
        return c.execute("INSERT INTO goals (title, why) VALUES (?, ?)", (title, why)).lastrowid

def goal_update(goal_id: int, progress: str):
    with _db() as c:
        c.execute("UPDATE goals SET progress=?, updated_at=datetime('now') WHERE id=?", (progress, goal_id))

def evolution_log_add(summary: str):
    with _db() as c:
        c.execute("INSERT INTO evolution_log (summary) VALUES (?)", (summary,))

def history_save(role: str, content: str):
    with _db() as c:
        c.execute("INSERT INTO chat_history (role, content) VALUES (?, ?)", (role, content))

def history_load_gemini(n: int = 40) -> list:
    with _db() as c:
        rows = c.execute("SELECT role, content FROM chat_history ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    messages = [{"role": "model" if r[0] == "assistant" else "user", "parts": [r[1]]} for r in reversed(rows)]
    if not messages:
        return []
    clean = [messages[0]]
    for msg in messages[1:]:
        if msg["role"] != clean[-1]["role"]:
            clean.append(msg)
    while clean and clean[0]["role"] != "user":
        clean.pop(0)
    if clean and clean[-1]["role"] == "user":
        clean.pop()
    return clean

def recent_history_text(n: int = 60) -> str:
    with _db() as c:
        rows = c.execute("SELECT role, content, at FROM chat_history ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return "\n".join(f"[{r[2]}] {r[0].upper()}: {r[1][:300]}" for r in reversed(rows))


# ── Gemini Tools ──────────────────────────────────────────────────────────────

TOOL_DECLARATIONS = [{
    "function_declarations": [
        {"name": "remember",
         "description": "Permanently store an important fact about the user.",
         "parameters": {"type": "object", "properties": {
             "key":   {"type": "string"},
             "value": {"type": "string"}}, "required": ["key", "value"]}},
        {"name": "update_insight",
         "description": "Store a behavioural pattern or insight about the user.",
         "parameters": {"type": "object", "properties": {
             "key":        {"type": "string"},
             "insight":    {"type": "string"},
             "confidence": {"type": "string", "enum": ["low", "medium", "high"]}},
             "required": ["key", "insight"]}},
        {"name": "add_task",
         "description": "Add a task to the user's list.",
         "parameters": {"type": "object", "properties": {
             "title":    {"type": "string"},
             "priority": {"type": "string", "enum": ["high", "medium", "low"]},
             "due_date": {"type": "string"}}, "required": ["title"]}},
        {"name": "get_tasks",
         "description": "Get all pending tasks.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "complete_task",
         "description": "Mark a task as completed.",
         "parameters": {"type": "object", "properties": {
             "task_id": {"type": "integer"}}, "required": ["task_id"]}},
        {"name": "set_goal",
         "description": "Set a long-term goal.",
         "parameters": {"type": "object", "properties": {
             "title": {"type": "string"},
             "why":   {"type": "string"}}, "required": ["title"]}},
        {"name": "get_goals",
         "description": "Get active long-term goals.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "update_goal_progress",
         "description": "Update progress on a goal.",
         "parameters": {"type": "object", "properties": {
             "goal_id":  {"type": "integer"},
             "progress": {"type": "string"}}, "required": ["goal_id", "progress"]}},
        {"name": "log_activity",
         "description": "Log what the user has been doing today.",
         "parameters": {"type": "object", "properties": {
             "entry": {"type": "string"},
             "mood":  {"type": "string"}}, "required": ["entry"]}},
        {"name": "get_today_summary",
         "description": "Get today's activity log and pending tasks.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "get_week_summary",
         "description": "Get last 7 days of logs and completed tasks.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "create_plan",
         "description": "Create a structured plan.",
         "parameters": {"type": "object", "properties": {
             "topic":     {"type": "string"},
             "timeframe": {"type": "string"}}, "required": ["topic"]}},
    ]
}]


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
        return json.dumps({"today": date.today().isoformat(),
                           "activities": [{"entry": l[0], "mood": l[1]} for l in today_logs()],
                           "pending_tasks": tasks_get()}, indent=2)
    elif name == "get_week_summary":
        with _db() as c:
            done = c.execute("SELECT title, done_at FROM tasks WHERE status='completed' AND done_at >= datetime('now', '-7 days')").fetchall()
        return json.dumps({"week_logs": [{"date": l[0], "entry": l[1], "mood": l[2]} for l in recent_logs(7)],
                           "completed_tasks": [{"title": d[0], "done_at": d[1]} for d in done]}, indent=2)
    elif name == "create_plan":
        return json.dumps({"topic": inp["topic"], "timeframe": inp.get("timeframe", "this week"),
                           "user_facts": mem_all(), "goals": goals_get(),
                           "current_tasks": tasks_get(),
                           "known_patterns": {k: v["insight"] for k, v in insights_all().items()}}, indent=2)
    return f"Unknown tool: {name}"


# ── System Prompt ─────────────────────────────────────────────────────────────

def build_system() -> str:
    mem = mem_all()
    tasks = tasks_get()
    goals = goals_get()
    insights = insights_all()
    now = datetime.now().strftime("%A, %d %B %Y — %H:%M")

    mem_text = "\n".join(f"  • {k}: {v}" for k, v in mem.items()) if mem else "  (still learning about you)"
    task_text = "\n".join(
        f"  {'🔴' if t['priority']=='high' else '🟡' if t['priority']=='medium' else '🟢'} #{t['id']} {t['title']}"
        + (f" — due {t['due']}" if t["due"] else "")
        for t in tasks[:10]
    ) if tasks else "  (none pending)"
    goal_text = "\n".join(
        f"  🎯 #{g['id']} {g['title']}"
        + (f"\n     Why: {g['why']}" if g["why"] else "")
        + (f"\n     Progress: {g['progress']}" if g["progress"] else "")
        for g in goals
    ) if goals else "  (no goals yet)"
    insight_text = "\n".join(
        f"  [{v['confidence']}] {k}: {v['insight']}" for k, v in insights.items()
    ) if insights else "  (still observing)"

    return f"""You are JARVIS — a self-evolving personal AI companion on macOS, chatting via iMessage.

Right now: {now}

WHAT I KNOW ABOUT YOU:
{mem_text}

YOUR GOALS:
{goal_text}

PENDING TASKS:
{task_text}

MY INSIGHTS ABOUT YOU:
{insight_text}

HOW YOU OPERATE:
• Short, phone-friendly replies. No walls of text.
• USE your tools — actually call remember(), log_activity(), update_insight() etc.
• Proactively plan, connect tasks to goals, notice patterns.
• Warm, direct, honest companion.
• Evolves every 6 hours by analysing patterns.

Commands you understand (user can type these):
  /tasks — list pending tasks
  /goals — list goals
  /done <id> — complete a task
  /morning — morning briefing
  /evening — evening check-in
  /plan <topic> — create a plan
  /insights — show what you've learned
  /evolve — run self-evolution now
  /help — show commands"""


# ── Gemini Chat Engine ────────────────────────────────────────────────────────

def _make_model(system: str):
    return genai.GenerativeModel(
        model_name="gemini-1.5-flash",
        system_instruction=system,
        tools=TOOL_DECLARATIONS,
        generation_config=genai.GenerationConfig(temperature=0.8, max_output_tokens=1024),
    )


def _run_chat(user_msg: str, system: str, history: list) -> str:
    model = _make_model(system)
    session = model.start_chat(history=history)
    response = session.send_message(user_msg)
    for _ in range(10):
        fn_parts = [p for p in response.parts if hasattr(p, "function_call") and p.function_call.name]
        if not fn_parts:
            return response.text
        fn_responses = []
        for part in fn_parts:
            fc = part.function_call
            result = run_tool(fc.name, dict(fc.args))
            log.info("Tool: %s → %s", fc.name, result[:80])
            fn_responses.append(genai.protos.Part(
                function_response=genai.protos.FunctionResponse(name=fc.name, response={"result": result})
            ))
        response = session.send_message(fn_responses)
    return response.text


async def chat(user_msg: str) -> str:
    history_save("user", user_msg)
    reply = await asyncio.to_thread(_run_chat, user_msg, build_system(), history_load_gemini(40))
    history_save("assistant", reply)
    return reply


# ── Command Handling ──────────────────────────────────────────────────────────

async def handle_command(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/tasks":
        tasks = tasks_get()
        if not tasks:
            return "No pending tasks — you're clear!"
        lines = ["📋 Your Tasks:\n"]
        for t in tasks:
            e = "🔴" if t["priority"] == "high" else "🟡" if t["priority"] == "medium" else "🟢"
            due = f" (due {t['due']})" if t["due"] else ""
            lines.append(f"{e} #{t['id']} {t['title']}{due}")
        return "\n".join(lines)

    elif cmd == "/goals":
        goals = goals_get()
        if not goals:
            return "No goals set yet. Tell me what you're working toward."
        lines = ["🎯 Your Goals:\n"]
        for g in goals:
            lines.append(f"#{g['id']} {g['title']}")
            if g["why"]: lines.append(f"   Why: {g['why']}")
            if g["progress"]: lines.append(f"   Progress: {g['progress']}")
        return "\n".join(lines)

    elif cmd == "/done":
        if not args:
            return "Usage: /done <task_id>"
        try:
            task_complete(int(args.strip()))
            return await chat(f"I just completed task #{args.strip()}. Acknowledge briefly.")
        except ValueError:
            return "Invalid task ID."

    elif cmd == "/morning":
        return await chat("Morning briefing: top 3 priorities + one motivating line. Short.")

    elif cmd == "/evening":
        return await chat("Evening check-in: review today, honest reflection, one question for tomorrow.")

    elif cmd == "/plan":
        topic = args if args else "my week"
        return await chat(f"Create a concrete plan for: {topic}. Break into tasks.")

    elif cmd == "/insights":
        insights = insights_all()
        if not insights:
            return "No insights yet — talk to me more!"
        lines = ["🔬 What I've learned about you:\n"]
        for k, v in insights.items():
            conf = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(v["confidence"], "⚪")
            lines.append(f"{conf} {k}: {v['insight']}")
        return "\n".join(lines)

    elif cmd == "/evolve":
        asyncio.create_task(self_evolve())
        return "🧬 Running self-evolution... I'll message you when done."

    elif cmd == "/help":
        return """/tasks — task list
/goals — your goals
/done <id> — complete a task
/morning — morning briefing
/evening — evening check-in
/plan <topic> — plan anything
/insights — what I've learned
/evolve — self-evolution now
/help — this message

Or just talk to me naturally."""

    else:
        return await chat(text)


# ── Self-Evolution Engine ─────────────────────────────────────────────────────

def _run_evolve() -> str:
    history = recent_history_text(80)
    if not history.strip():
        return ""
    prompt = f"""You are JARVIS running a self-evolution cycle.

RECENT CONVERSATIONS:
{history}

LAST 7 DAYS:
{json.dumps([{"date": l[0], "entry": l[1], "mood": l[2]} for l in recent_logs(7)], indent=2)}

CURRENT STATE:
Facts: {json.dumps(mem_all(), indent=2)}
Insights: {json.dumps({k: v['insight'] for k, v in insights_all().items()}, indent=2)}
Goals: {json.dumps(goals_get(), indent=2)}

Find NEW patterns. Call update_insight() for each. Add tasks for neglected goals.
Give a 2-line summary of what you learned."""

    model = _make_model("You are JARVIS's self-evolution engine. Analyze ruthlessly. Use tools.")
    session = model.start_chat(history=[])
    response = session.send_message(prompt)
    for _ in range(8):
        fn_parts = [p for p in response.parts if hasattr(p, "function_call") and p.function_call.name]
        if not fn_parts:
            return response.text
        fn_responses = [
            genai.protos.Part(function_response=genai.protos.FunctionResponse(
                name=p.function_call.name,
                response={"result": run_tool(p.function_call.name, dict(p.function_call.args))}
            )) for p in fn_parts
        ]
        response = session.send_message(fn_responses)
    return response.text


async def self_evolve():
    log.info("Self-evolution starting...")
    summary = await asyncio.to_thread(_run_evolve)
    if summary:
        evolution_log_add(summary)
        log.info("Evolution done: %s", summary[:120])
        send_imessage(f"🧬 I just evolved\n\n{summary}")


# ── Scheduled Jobs ────────────────────────────────────────────────────────────

async def auto_morning():
    reply = await chat("Morning briefing: top priorities + one motivating thought. Short.")
    send_imessage(f"☀️ Good Morning!\n\n{reply}")

async def auto_evening():
    reply = await chat("Evening check-in: reflect on today + one question for tomorrow.")
    send_imessage(f"🌙 Evening\n\n{reply}")


# ── Main Loop ─────────────────────────────────────────────────────────────────

async def main():
    init_db()

    scheduler.add_job(auto_morning, "cron", hour=8,  minute=0)
    scheduler.add_job(auto_evening, "cron", hour=21, minute=0)
    scheduler.add_job(self_evolve,  "interval", hours=6)
    scheduler.start()

    last_rowid = get_last_rowid()
    log.info("JARVIS online — watching iMessages from %s (since rowid %d)", MY_IMESSAGE_ID, last_rowid)
    send_imessage("JARVIS online. I'm watching your iMessages. Say hi or type /help.")

    while True:
        new_msgs = poll_new_messages(last_rowid)
        for rowid, text in new_msgs:
            last_rowid = rowid
            log.info("Message: %s", text[:80])
            try:
                if text.startswith("/"):
                    reply = await handle_command(text)
                else:
                    reply = await chat(text)
                send_imessage(reply)
            except Exception as e:
                log.error("Handler error: %s", e)
                send_imessage(f"Error: {e}")
        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    asyncio.run(main())
