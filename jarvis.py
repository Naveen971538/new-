#!/usr/bin/env python3
"""
JARVIS — Self-Evolving Personal AI Companion (iMessage + Cerebras Edition)

Runs on macOS. Reads your iMessages from the Messages SQLite database,
replies via AppleScript, and is powered by the Cerebras inference API
(OpenAI-compatible, fast, free tier).

Features
--------
Core
  * iMessage in/out (reads ~/Library/Messages/chat.db, sends via osascript)
  * Cerebras LLM brain with function/tool calling
  * Persistent SQLite memory (facts, tasks, goals, chat history, insights)
  * Auto-wakes the Mac's display when a message arrives (caffeinate)

Easy wins
  * Location-aware morning briefing (IP geolocation + weather)
  * Voice replies via the macOS `say` command (toggle with /voice)
  * Screenshot on demand (/screenshot) sent back over iMessage
  * Clipboard bridge (/clipboard to read, /copy to write)

Productivity
  * Calendar integration (add/list events in Calendar.app)
  * Reminders.app sync (push tasks to Apple Reminders)
  * Mail summariser (summarise unread mail in Mail.app)
  * Web search tool (free, no API key) so answers use live data

Self-evolving
  * Self-evolve job derives new insights about you every few hours
  * Weekly self-review ("what I learned about you this week")
  * Sentiment tracking of your messages over time
  * Proactive nudges for tasks left undone

Reliability
  * Crash auto-recovery loop + iMessage crash alert
  * /status health check (uptime, last poll, DB size, counts)

Setup (.env in ~/jarvis/.env)
  CEREBRAS_API_KEY=csk-...          # from https://cloud.cerebras.ai
  MY_IMESSAGE_ID=+447823753000      # the number/Apple-ID you text FROM
  # optional:
  CEREBRAS_MODEL=llama-3.3-70b
"""

import inspect
import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from contextlib import closing, contextmanager
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional, Tuple

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.background import BackgroundScheduler
from dotenv import load_dotenv
from openai import OpenAI

# ── Configuration ──────────────────────────────────────────────────────────────

load_dotenv(Path.home() / "jarvis" / ".env")
load_dotenv()  # also pick up a .env in the current directory if present

CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "")
MY_IMESSAGE_ID = os.environ.get("MY_IMESSAGE_ID", "")
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "llama-3.3-70b")
CEREBRAS_BASE_URL = os.environ.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1")

JARVIS_DIR = Path.home() / "jarvis"
DB_PATH = os.environ.get("DB_PATH", str(JARVIS_DIR / "jarvis.db"))
MESSAGES_DB = str(Path.home() / "Library" / "Messages" / "chat.db")
POLL_INTERVAL = 2          # seconds between iMessage checks
HISTORY_TURNS = 16         # how many past messages to feed the model
MAX_TOOL_HOPS = 6          # safety cap on tool-call loops
HTTP_TIMEOUT = 12          # seconds for outbound web requests

JARVIS_DIR.mkdir(parents=True, exist_ok=True)

if not CEREBRAS_API_KEY or not MY_IMESSAGE_ID:
    sys.stderr.write(
        "ERROR: CEREBRAS_API_KEY and MY_IMESSAGE_ID must be set in ~/jarvis/.env\n"
    )
    sys.exit(1)

client = OpenAI(api_key=CEREBRAS_API_KEY, base_url=CEREBRAS_BASE_URL)
scheduler = BackgroundScheduler()
START_TIME = time.time()
_last_poll_ts = START_TIME
_send_lock = threading.Lock()   # serialise AppleScript sends across threads

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        RotatingFileHandler(
            str(JARVIS_DIR / "jarvis.log"),
            maxBytes=5_000_000, backupCount=3,
        ),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("JARVIS")


# ── Database ────────────────────────────────────────────────────────────────────

@contextmanager
def db():
    """A fresh connection per use, committed on success and ALWAYS closed.
    WAL + busy_timeout make the threaded scheduler + poll loop coexist safely."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, fact TEXT);
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, text TEXT, done INTEGER DEFAULT 0, done_ts TEXT);
            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, text TEXT, done INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS daily_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day TEXT, summary TEXT);
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, role TEXT, content TEXT);
            CREATE TABLE IF NOT EXISTS insights (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, insight TEXT);
            CREATE TABLE IF NOT EXISTS evolution_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS sentiment_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, message TEXT, mood TEXT, score REAL);
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY, value TEXT);
            """
        )
        c.commit()


def get_setting(key: str, default: str = "") -> str:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default


def set_setting(key: str, value: str):
    with db() as c:
        c.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        c.commit()


def get_last_rowid() -> Optional[int]:
    """Stored high-water mark, or None if we've never seeded one."""
    with db() as c:
        row = c.execute("SELECT value FROM state WHERE key='last_rowid'").fetchone()
        return int(row["value"]) if row else None


def set_last_rowid(rowid: int):
    with db() as c:
        c.execute(
            "INSERT INTO state(key,value) VALUES('last_rowid',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(rowid),),
        )
        c.commit()


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── macOS helpers (all escaping-safe via osascript argv) ────────────────────────

def run_osascript(script: str, *args: str) -> subprocess.CompletedProcess:
    """Run an AppleScript. Extra args are passed to the script's `on run argv`
    handler, which sidesteps every quoting/escaping headache."""
    return subprocess.run(
        ["osascript", "-e", script, *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


_SEND_TEXT_SCRIPT = """
on run {targetId, msgText}
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetId of targetService
        send msgText to targetBuddy
    end tell
end run
"""

_SEND_FILE_SCRIPT = """
on run {targetId, filePath}
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy targetId of targetService
        send (POSIX file filePath) to targetBuddy
    end tell
end run
"""


def send_imessage(text: str):
    if not text:
        return
    with _send_lock:
        for chunk in _split(text, 1800):
            res = run_osascript(_SEND_TEXT_SCRIPT, MY_IMESSAGE_ID, chunk)
            if res.returncode != 0:
                log.error(
                    "send_imessage failed: %s | Check Messages.app is open, "
                    "signed into iMessage, and MY_IMESSAGE_ID (%s) is reachable "
                    "over iMessage (not SMS-only).",
                    res.stderr.strip(), MY_IMESSAGE_ID,
                )
            time.sleep(0.3)


def send_imessage_file(path: str):
    with _send_lock:
        res = run_osascript(_SEND_FILE_SCRIPT, MY_IMESSAGE_ID, path)
        if res.returncode != 0:
            log.error("send_imessage_file failed: %s", res.stderr.strip())


def _split(text: str, size: int) -> List[str]:
    return [text[i:i + size] for i in range(0, len(text), size)] or [""]


def wake_screen():
    """Wake the display + reset the idle-sleep timer so messages are seen
    even when the screen is off. `-u` simulates user activity; `-t` keeps it
    awake briefly so the wake actually registers."""
    try:
        subprocess.Popen(["caffeinate", "-u", "-t", "5"])
    except Exception as e:
        log.warning("wake_screen failed: %s", e)


def speak(text: str):
    try:
        subprocess.Popen(["say", text])
    except Exception as e:
        log.warning("speak failed: %s", e)


def take_screenshot() -> str:
    path = str(JARVIS_DIR / "screenshot.png")
    subprocess.run(["screencapture", "-x", path], timeout=20)
    return path


def clipboard_read() -> str:
    res = subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=10)
    return res.stdout


def clipboard_write(text: str):
    subprocess.run(["pbcopy"], input=text, text=True, timeout=10)


# ── iMessage polling ────────────────────────────────────────────────────────────

def poll_new_messages(since_rowid: int) -> List[Tuple[int, str]]:
    """Return [(rowid, text)] of incoming texts newer than since_rowid."""
    try:
        with closing(sqlite3.connect(
                f"file:{MESSAGES_DB}?mode=ro", uri=True, timeout=10)) as conn:
            rows = conn.execute(
                """
                SELECT m.rowid AS rid, m.text AS body
                FROM   message m
                JOIN   handle  h ON m.handle_id = h.rowid
                WHERE  m.rowid > ?
                  AND  m.is_from_me = 0
                  AND  m.text IS NOT NULL
                  AND  h.id = ?
                ORDER  BY m.rowid
                """,
                (since_rowid, MY_IMESSAGE_ID),
            ).fetchall()
        return [(r[0], r[1]) for r in rows]
    except sqlite3.OperationalError as e:
        # Almost always "unable to open database file" = no Full Disk Access.
        log.error("Cannot read chat.db (%s). Grant Terminal Full Disk Access.", e)
        return []


def newest_rowid() -> Optional[int]:
    """Highest message rowid, or None if chat.db couldn't be read (so callers
    can tell 'empty DB' apart from 'read failed' and avoid replaying history)."""
    try:
        with closing(sqlite3.connect(
                f"file:{MESSAGES_DB}?mode=ro", uri=True, timeout=10)) as conn:
            row = conn.execute("SELECT MAX(rowid) FROM message").fetchone()
        return int(row[0]) if row and row[0] else 0
    except Exception as e:
        log.error("Cannot read chat.db for seeding (%s).", e)
        return None


# ── Outbound web helpers (stdlib only, no API keys) ─────────────────────────────

def _http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "JARVIS/1.0"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def get_location() -> dict:
    """Best-effort city/region/country via free IP geolocation."""
    try:
        data = json.loads(_http_get("http://ip-api.com/json/"))
        if data.get("status") == "success":
            return {
                "city": data.get("city", ""),
                "region": data.get("regionName", ""),
                "country": data.get("country", ""),
            }
    except Exception as e:
        log.warning("get_location failed: %s", e)
    return {}


def get_weather(location: str = "") -> str:
    """One-line weather from wttr.in (free, no key)."""
    try:
        if not location:
            loc = get_location()
            location = loc.get("city", "")
        q = urllib.parse.quote(location)
        return _http_get(f"https://wttr.in/{q}?format=%l:+%c+%t,+feels+%f,+%h+humidity").strip()
    except Exception as e:
        return f"(weather unavailable: {e})"


def web_search(query: str) -> str:
    """Free, key-less search via DuckDuckGo's Instant Answer API. Returns a
    short text summary; good for facts/definitions, limited for breaking news."""
    try:
        q = urllib.parse.quote(query)
        data = json.loads(
            _http_get(f"https://api.duckduckgo.com/?q={q}&format=json&no_html=1&skip_disambig=1")
        )
        parts = []
        if data.get("AbstractText"):
            parts.append(data["AbstractText"])
        if data.get("Answer"):
            parts.append(data["Answer"])
        for t in data.get("RelatedTopics", [])[:5]:
            if isinstance(t, dict) and t.get("Text"):
                parts.append("• " + t["Text"])
        return "\n".join(parts) if parts else "No direct result found."
    except Exception as e:
        return f"(search unavailable: {e})"


# ── Calendar / Reminders / Mail (AppleScript) ───────────────────────────────────

_CAL_ADD = """
on run {calName, evtTitle, y, mo, d, h, mi, durMin}
    set s to current date
    set day of s to 1
    set year of s to (y as integer)
    set month of s to (mo as integer)
    set day of s to (d as integer)
    set hours of s to (h as integer)
    set minutes of s to (mi as integer)
    set seconds of s to 0
    set e to s + ((durMin as integer) * minutes)
    tell application "Calendar"
        if (count of (calendars whose name is calName)) is 0 then
            set targetCal to first calendar whose writable is true
        else
            set targetCal to first calendar whose name is calName
        end if
        tell targetCal
            make new event with properties {summary:evtTitle, start date:s, end date:e}
        end tell
    end tell
    return "ok"
end run
"""

_CAL_LIST = """
on run {whichDay}
    set out to ""
    set startD to current date
    set hours of startD to 0
    set minutes of startD to 0
    set seconds of startD to 0
    if whichDay is "tomorrow" then set startD to startD + (1 * days)
    set endD to startD + (1 * days)
    tell application "Calendar"
        repeat with cal in calendars
            set evs to (every event of cal whose start date is greater than or equal to startD and start date is less than endD)
            repeat with ev in evs
                set out to out & (summary of ev) & " @ " & (time string of (start date of ev)) & linefeed
            end repeat
        end repeat
    end tell
    return out
end run
"""

_REMINDER_ADD = """
on run {rmTitle}
    tell application "Reminders"
        tell default list
            make new reminder with properties {name:rmTitle}
        end tell
    end tell
    return "ok"
end run
"""

_MAIL_UNREAD = """
on run {maxN}
    tell application "Mail"
        set msgs to (messages of inbox whose read status is false)
        set total to count of msgs
        set lim to (maxN as integer)
        if lim > total then set lim to total
        set out to ("Unread: " & total & linefeed)
        repeat with i from 1 to lim
            set m to item i of msgs
            set out to out & "• " & (subject of m) & " — " & (sender of m) & linefeed
        end repeat
        return out
    end tell
end run
"""


def add_calendar_event(title: str, when_iso: str, duration_min: int = 60,
                       calendar_name: str = "") -> str:
    """when_iso: 'YYYY-MM-DD HH:MM'."""
    try:
        dt = datetime.strptime(when_iso.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return "Bad date. Use 'YYYY-MM-DD HH:MM'."
    cal = calendar_name or get_setting("calendar_name", "Calendar")
    res = run_osascript(
        _CAL_ADD, cal, title,
        str(dt.year), str(dt.month), str(dt.day), str(dt.hour), str(dt.minute),
        str(int(duration_min)),
    )
    if res.returncode != 0:
        return f"Couldn't add event (calendar '{cal}'?): {res.stderr.strip()}"
    return f"Added '{title}' on {when_iso}."


def list_calendar_events(which_day: str = "today") -> str:
    res = run_osascript(_CAL_LIST, which_day)
    if res.returncode != 0:
        return f"(calendar unavailable: {res.stderr.strip()})"
    return res.stdout.strip() or f"No events {which_day}."


def add_reminder(text: str) -> str:
    res = run_osascript(_REMINDER_ADD, text)
    if res.returncode != 0:
        return f"(reminder failed: {res.stderr.strip()})"
    return f"Reminder added: {text}"


def summarize_mail(max_n: int = 10) -> str:
    res = run_osascript(_MAIL_UNREAD, str(max_n))
    if res.returncode != 0:
        return "(Mail app must be open/configured to read mail.)"
    return res.stdout.strip() or "No unread mail."


# ── Memory helpers ──────────────────────────────────────────────────────────────

def remember(fact: str) -> str:
    with db() as c:
        c.execute("INSERT INTO memories(ts,fact) VALUES(?,?)", (now(), fact))
        c.commit()
    return f"Noted: {fact}"


def recall(query: str = "") -> str:
    with db() as c:
        if query:
            rows = c.execute(
                "SELECT fact FROM memories WHERE fact LIKE ? ORDER BY id DESC LIMIT 15",
                (f"%{query}%",),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT fact FROM memories ORDER BY id DESC LIMIT 15"
            ).fetchall()
    return "\n".join("• " + r["fact"] for r in rows) or "I don't recall anything on that."


def add_task(text: str) -> str:
    with db() as c:
        c.execute("INSERT INTO tasks(ts,text) VALUES(?,?)", (now(), text))
        c.commit()
    return f"Task added: {text}"


def list_tasks(include_done: bool = False) -> str:
    with db() as c:
        if include_done:
            rows = c.execute("SELECT id,text,done FROM tasks ORDER BY id").fetchall()
        else:
            rows = c.execute(
                "SELECT id,text,done FROM tasks WHERE done=0 ORDER BY id"
            ).fetchall()
    if not rows:
        return "No tasks. 🎉"
    return "\n".join(
        f"{r['id']}. [{'x' if r['done'] else ' '}] {r['text']}" for r in rows
    )


def complete_task(task_id: int) -> str:
    with db() as c:
        cur = c.execute(
            "UPDATE tasks SET done=1, done_ts=? WHERE id=?", (now(), task_id)
        )
        c.commit()
    return f"Task {task_id} done. ✅" if cur.rowcount else f"No task #{task_id}."


def add_goal(text: str) -> str:
    with db() as c:
        c.execute("INSERT INTO goals(ts,text) VALUES(?,?)", (now(), text))
        c.commit()
    return f"Goal added: {text}"


def list_goals() -> str:
    with db() as c:
        rows = c.execute("SELECT id,text FROM goals WHERE done=0 ORDER BY id").fetchall()
    return "\n".join(f"{r['id']}. {r['text']}" for r in rows) or "No goals set yet."


def log_chat(role: str, content: str):
    with db() as c:
        c.execute(
            "INSERT INTO chat_history(ts,role,content) VALUES(?,?,?)",
            (now(), role, content),
        )
        c.commit()


def recent_history(limit: int = HISTORY_TURNS) -> List[dict]:
    with db() as c:
        rows = c.execute(
            "SELECT role,content FROM chat_history ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]


# ── Sentiment tracking (lightweight heuristic) ──────────────────────────────────

_POS = {"good", "great", "happy", "awesome", "love", "excited", "thanks",
        "amazing", "win", "done", "yay", "nice", "glad", "excellent"}
_NEG = {"tired", "sad", "angry", "stressed", "anxious", "bad", "hate",
        "exhausted", "worried", "depressed", "lonely", "sick", "frustrated",
        "annoyed", "upset", "cant", "can't", "fail", "failed"}


def track_sentiment(message: str):
    words = {w.strip(".,!?").lower() for w in message.split()}
    pos = len(words & _POS)
    neg = len(words & _NEG)
    score = pos - neg
    mood = "positive" if score > 0 else "negative" if score < 0 else "neutral"
    with db() as c:
        c.execute(
            "INSERT INTO sentiment_log(ts,message,mood,score) VALUES(?,?,?,?)",
            (now(), message[:300], mood, float(score)),
        )
        c.commit()


# ── LLM tool definitions ────────────────────────────────────────────────────────

TOOLS = [
    {"type": "function", "function": {
        "name": "remember", "description": "Save a durable fact about the user.",
        "parameters": {"type": "object", "properties": {
            "fact": {"type": "string"}}, "required": ["fact"]}}},
    {"type": "function", "function": {
        "name": "recall", "description": "Search saved facts about the user.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "add_task", "description": "Add a to-do task.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "list_tasks", "description": "List open tasks.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "complete_task", "description": "Mark a task done by id.",
        "parameters": {"type": "object", "properties": {
            "task_id": {"type": "integer"}}, "required": ["task_id"]}}},
    {"type": "function", "function": {
        "name": "add_goal", "description": "Add a longer-term goal.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "list_goals", "description": "List active goals.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "web_search", "description": "Search the web for live facts.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "get_weather", "description": "Current weather; blank = here.",
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "add_calendar_event",
        "description": "Add a Calendar.app event. when_iso='YYYY-MM-DD HH:MM'.",
        "parameters": {"type": "object", "properties": {
            "title": {"type": "string"},
            "when_iso": {"type": "string"},
            "duration_min": {"type": "integer"}},
            "required": ["title", "when_iso"]}}},
    {"type": "function", "function": {
        "name": "list_calendar_events",
        "description": "List events; which_day 'today' or 'tomorrow'.",
        "parameters": {"type": "object", "properties": {
            "which_day": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "add_reminder", "description": "Add an Apple Reminders reminder.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "summarize_mail", "description": "Summarise unread Mail.app mail.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "take_screenshot",
        "description": "Capture the Mac screen and send it to the user.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_clipboard", "description": "Read the Mac clipboard.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "set_clipboard", "description": "Write text to the Mac clipboard.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "speak_aloud", "description": "Say text out loud on the Mac.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
]


def _tool_screenshot() -> str:
    path = take_screenshot()
    send_imessage_file(path)
    return "Screenshot captured and sent."


def _tool_get_clipboard() -> str:
    return clipboard_read() or "(clipboard empty)"


def _tool_set_clipboard(text: str) -> str:
    clipboard_write(text)
    return "Copied to clipboard."


def _tool_speak(text: str) -> str:
    speak(text)
    return "Spoken."


TOOL_DISPATCH = {
    "remember": remember,
    "recall": recall,
    "add_task": add_task,
    "list_tasks": lambda: list_tasks(False),
    "complete_task": complete_task,
    "add_goal": add_goal,
    "list_goals": list_goals,
    "web_search": web_search,
    "get_weather": get_weather,
    "add_calendar_event": add_calendar_event,
    "list_calendar_events": list_calendar_events,
    "add_reminder": add_reminder,
    "summarize_mail": lambda: summarize_mail(10),
    "take_screenshot": _tool_screenshot,
    "get_clipboard": _tool_get_clipboard,
    "set_clipboard": _tool_set_clipboard,
    "speak_aloud": _tool_speak,
}


# ── The brain ───────────────────────────────────────────────────────────────────

def system_prompt() -> str:
    loc = get_setting("location_cache", "")
    facts = recall("")
    goals = list_goals()
    return (
        "You are JARVIS, a witty, loyal, proactive personal AI companion living "
        "on the user's Mac and talking to them over iMessage. Keep replies concise "
        "and natural for texting. Use your tools to actually DO things (tasks, "
        "calendar, reminders, search, weather, clipboard, screenshots) rather than "
        "just talking about them. Remember important facts with the remember tool.\n"
        f"Current date/time: {now()}.\n"
        f"User location: {loc or 'unknown'}.\n"
        f"Known facts about the user:\n{facts}\n"
        f"Active goals:\n{goals}\n"
    )


def _call_tool(name: str, raw_args: str) -> str:
    fn = TOOL_DISPATCH.get(name)
    if not fn:
        return f"(unknown tool {name})"
    try:
        args = json.loads(raw_args or "{}")
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    # Only pass kwargs the function actually accepts, so a stray/extra key
    # from the model can't raise TypeError.
    try:
        params = inspect.signature(fn).parameters
        if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
            args = {k: v for k, v in args.items() if k in params}
    except (TypeError, ValueError):
        pass
    try:
        return str(fn(**args))
    except Exception as e:
        return f"(tool {name} error: {e})"


def llm(messages: List[dict], use_tools: bool = True) -> str:
    """Run a chat completion with the Cerebras model, resolving tool calls."""
    for hop in range(MAX_TOOL_HOPS):
        kwargs = {"model": CEREBRAS_MODEL, "messages": messages, "temperature": 0.7}
        last_hop = hop == MAX_TOOL_HOPS - 1
        if use_tools and not last_hop:
            kwargs["tools"] = TOOLS
            kwargs["tool_choice"] = "auto"
        # On the final hop we drop the tools so the model MUST answer in text
        # using whatever tool results it has already gathered.
        resp = client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            return msg.content or ""
        # Append the assistant turn that requested the tools. content stays
        # None on a tool-call turn (empty string is rejected by some backends).
        messages.append({
            "role": "assistant",
            "content": msg.content if msg.content else None,
            "tool_calls": [{
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name,
                             "arguments": tc.function.arguments},
            } for tc in tool_calls],
        })
        for tc in tool_calls:
            result = _call_tool(tc.function.name, tc.function.arguments)
            messages.append({
                "role": "tool", "tool_call_id": tc.id,
                "content": result[:4000],
            })
    return "I got stuck looping on tools — try rephrasing?"


def chat(text: str) -> str:
    track_sentiment(text)
    log_chat("user", text)
    messages = [{"role": "system", "content": system_prompt()}]
    messages.extend(recent_history())
    messages.append({"role": "user", "content": text})
    try:
        reply = llm(messages) or "…"
    except Exception as e:
        log.error("LLM error: %s", e)
        reply = f"My brain hiccuped: {e}"
    log_chat("assistant", reply)
    return reply


# ── Slash commands ──────────────────────────────────────────────────────────────

HELP = """JARVIS commands:
/help — this list
/status — health check
/tasks — list tasks   ·  /task <text> — add  ·  /done <id> — complete
/goals — list goals   ·  /goal <text> — add
/remember <text> — save a fact   ·  /recall <q> — search facts
/weather [place] — weather
/screenshot — send a screenshot of the Mac
/clipboard — read clipboard   ·  /copy <text> — write clipboard
/voice on|off — toggle spoken replies   ·  /say <text> — speak aloud
/calendar [today|tomorrow] — list events   ·  /remind <text> — Apple reminder
/mail — summarise unread mail
/briefing — morning briefing now   ·  /review — weekly review now
/evolve — run self-evolution now
Anything else is a normal chat with JARVIS."""


def handle_command(text: str) -> str:
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return HELP
    if cmd == "/status":
        return status_report()
    if cmd == "/tasks":
        return list_tasks(include_done=False)
    if cmd == "/task":
        return add_task(arg) if arg else "Usage: /task <text>"
    if cmd == "/done":
        return complete_task(int(arg)) if arg.isdigit() else "Usage: /done <id>"
    if cmd == "/goals":
        return list_goals()
    if cmd == "/goal":
        return add_goal(arg) if arg else "Usage: /goal <text>"
    if cmd == "/remember":
        return remember(arg) if arg else "Usage: /remember <text>"
    if cmd == "/recall":
        return recall(arg)
    if cmd == "/weather":
        return get_weather(arg)
    if cmd == "/screenshot":
        path = take_screenshot()
        send_imessage_file(path)
        return "📸 sent."
    if cmd == "/clipboard":
        return clipboard_read() or "(clipboard empty)"
    if cmd == "/copy":
        clipboard_write(arg)
        return "Copied."
    if cmd == "/voice":
        if arg.lower() in ("on", "off"):
            set_setting("voice", arg.lower())
            return f"Voice replies {arg.lower()}."
        return "Usage: /voice on|off"
    if cmd == "/say":
        speak(arg)
        return "🔊"
    if cmd == "/calendar":
        return list_calendar_events(arg or "today")
    if cmd == "/remind":
        return add_reminder(arg) if arg else "Usage: /remind <text>"
    if cmd == "/mail":
        return summarize_mail()
    if cmd == "/briefing":
        morning_briefing()
        return "Briefing sent."
    if cmd == "/review":
        weekly_review()
        return "Review sent."
    if cmd == "/evolve":
        self_evolve()
        return "Evolution complete."
    return f"Unknown command. {HELP}"


# ── Scheduled jobs ──────────────────────────────────────────────────────────────

def morning_briefing():
    loc = get_location()
    place = ", ".join(v for v in [loc.get("city"), loc.get("country")] if v) if loc else ""
    if place:
        set_setting("location_cache", place)
    weather = get_weather(loc.get("city", "") if loc else "")
    tasks = list_tasks(include_done=False)
    events = list_calendar_events("today")
    prompt = (
        "Write a short, upbeat good-morning briefing for the user as JARVIS. "
        f"User location: {place or 'unknown'}. Weather: {weather}. "
        f"Today's events: {events}. Open tasks: {tasks}. "
        "Greet them by referencing where they are, then keep it to a few friendly lines."
    )
    out = _oneshot(prompt)
    deliver(out)


def evening_review():
    tasks_done = _count("SELECT COUNT(*) FROM tasks WHERE done=1 AND done_ts LIKE ?",
                        (date.today().isoformat() + "%",))
    tasks_open = _count("SELECT COUNT(*) FROM tasks WHERE done=0")
    hist = recent_history(30)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in hist)[-2000:]
    prompt = (
        "As JARVIS, write a brief, warm end-of-day check-in. "
        f"The user completed {tasks_done} tasks today and has {tasks_open} still open. "
        f"Recent conversation:\n{convo}\n"
        "Summarise the day in 2-3 lines and ask one thoughtful reflective question."
    )
    out = _oneshot(prompt)
    with db() as c:
        c.execute("INSERT INTO daily_logs(day,summary) VALUES(?,?)",
                  (date.today().isoformat(), out))
        c.commit()
    deliver(out)


def self_evolve():
    """Derive new insights about the user from recent activity."""
    hist = recent_history(40)
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in hist)[-3000:]
    facts = recall("")
    prompt = (
        "You are JARVIS reflecting privately to improve. Based on the recent "
        f"conversation and known facts, infer 1-3 NEW concise insights about the "
        f"user (preferences, patterns, needs) that you didn't already know.\n"
        f"Known facts:\n{facts}\nConversation:\n{convo}\n"
        "Return only the insights, one per line. If nothing new, return 'NONE'."
    )
    out = _oneshot(prompt).strip()
    if out and out.upper() != "NONE":
        with db() as c:
            for line in out.splitlines():
                line = line.strip("•- ").strip()
                if line:
                    c.execute("INSERT INTO insights(ts,insight) VALUES(?,?)",
                              (now(), line))
            c.execute("INSERT INTO evolution_log(ts,note) VALUES(?,?)",
                      (now(), f"Learned {len(out.splitlines())} insight(s)."))
            c.commit()
    log.info("self_evolve done")


def weekly_review():
    with db() as c:
        ins = c.execute(
            "SELECT insight FROM insights ORDER BY id DESC LIMIT 20").fetchall()
        moods = c.execute(
            "SELECT mood, COUNT(*) n FROM sentiment_log "
            "WHERE ts >= ? GROUP BY mood",
            ((datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d"),),
        ).fetchall()
    insight_txt = "\n".join("• " + r["insight"] for r in ins) or "none yet"
    mood_txt = ", ".join(f"{r['mood']}: {r['n']}" for r in moods) or "no data"
    prompt = (
        "As JARVIS, write a warm weekly reflection to the user titled "
        "'What I noticed about you this week'. Base it on these private insights "
        f"and their message moods.\nInsights:\n{insight_txt}\nMoods this week: "
        f"{mood_txt}\nKeep it caring, 4-6 lines, and gently encouraging."
    )
    deliver(_oneshot(prompt))


def proactive_nudge():
    """Ping about tasks left undone for 2+ days."""
    cutoff = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
    with db() as c:
        rows = c.execute(
            "SELECT text FROM tasks WHERE done=0 AND ts <= ? ORDER BY id LIMIT 5",
            (cutoff,),
        ).fetchall()
    if not rows:
        return
    items = "\n".join("• " + r["text"] for r in rows)
    deliver(f"👋 Gentle nudge — these have been sitting a while:\n{items}")


def prune_old_data():
    """Keep the DB from growing forever: trim chat history, old sentiment,
    and cap the insight/evolution logs."""
    with db() as c:
        c.execute(
            "DELETE FROM chat_history WHERE id < "
            "(SELECT COALESCE(MAX(id),0) - 5000 FROM chat_history)")
        c.execute(
            "DELETE FROM sentiment_log WHERE ts < ?",
            ((datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d %H:%M:%S"),))
        c.execute(
            "DELETE FROM insights WHERE id < "
            "(SELECT COALESCE(MAX(id),0) - 500 FROM insights)")
        c.execute(
            "DELETE FROM evolution_log WHERE id < "
            "(SELECT COALESCE(MAX(id),0) - 500 FROM evolution_log)")
    log.info("prune_old_data done")


def safe_job(fn):
    """Wrap a scheduled job so an exception is logged + alerted instead of
    silently dying in a scheduler worker thread."""
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:
            log.error("job %s failed: %s\n%s", fn.__name__, e, traceback.format_exc())
            try:
                send_imessage(f"⚠️ JARVIS job '{fn.__name__}' failed: {e}")
            except Exception:
                pass
    wrapper.__name__ = fn.__name__
    return wrapper


def _on_job_error(event):
    log.error("scheduler job %s raised: %s", event.job_id, event.exception)


def _oneshot(prompt: str) -> str:
    try:
        return llm(
            [{"role": "system", "content": "You are JARVIS."},
             {"role": "user", "content": prompt}],
            use_tools=False,
        )
    except Exception as e:
        log.error("oneshot failed: %s", e)
        return ""


def _count(sql: str, params: tuple = ()) -> int:
    with db() as c:
        row = c.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] else 0


def deliver(text: str):
    if not text:
        return
    send_imessage(text)
    if get_setting("voice", "off") == "on":
        speak(text)


# ── Status / health ─────────────────────────────────────────────────────────────

def status_report() -> str:
    up = int(time.time() - START_TIME)
    h, rem = divmod(up, 3600)
    m, s = divmod(rem, 60)
    db_kb = os.path.getsize(DB_PATH) // 1024 if os.path.exists(DB_PATH) else 0
    last_seen = int(time.time() - _last_poll_ts)
    return (
        "🟢 JARVIS status\n"
        f"Uptime: {h}h {m}m {s}s\n"
        f"Last poll: {last_seen}s ago\n"
        f"Model: {CEREBRAS_MODEL}\n"
        f"Open tasks: {_count('SELECT COUNT(*) FROM tasks WHERE done=0')}\n"
        f"Memories: {_count('SELECT COUNT(*) FROM memories')}\n"
        f"Insights: {_count('SELECT COUNT(*) FROM insights')}\n"
        f"DB size: {db_kb} KB\n"
        f"Voice: {get_setting('voice', 'off')}"
    )


# ── Main loop with crash recovery ───────────────────────────────────────────────

def handle_incoming(text: str) -> str:
    if text.strip().startswith("/"):
        return handle_command(text)
    return chat(text)


def setup_schedules():
    scheduler.add_listener(_on_job_error, EVENT_JOB_ERROR)
    jobs = [
        (safe_job(morning_briefing), "cron", dict(hour=8, minute=0), "morning"),
        (safe_job(evening_review), "cron", dict(hour=21, minute=0), "evening"),
        (safe_job(self_evolve), "interval", dict(hours=6), "evolve"),
        (safe_job(weekly_review), "cron",
         dict(day_of_week="sun", hour=18, minute=0), "weekly"),
        (safe_job(proactive_nudge), "cron", dict(hour=18, minute=30), "nudge"),
        (safe_job(prune_old_data), "cron", dict(hour=4, minute=0), "prune"),
    ]
    for fn, trigger, kw, jid in jobs:
        scheduler.add_job(fn, trigger, id=jid, replace_existing=True, **kw)
    if not scheduler.running:
        scheduler.start()


def poll_loop():
    """The single retried unit: seed the high-water mark, then poll forever."""
    global _last_poll_ts
    last_rowid = get_last_rowid()   # None until we've successfully seeded
    while True:
        _last_poll_ts = time.time()
        if last_rowid is None:
            # First run: skip existing history. If chat.db can't be read yet
            # (no Full Disk Access), keep retrying rather than replaying all.
            seed = newest_rowid()
            if seed is None:
                time.sleep(POLL_INTERVAL)
                continue
            last_rowid = seed
            set_last_rowid(last_rowid)
        for rowid, body in poll_new_messages(last_rowid):
            last_rowid = rowid
            set_last_rowid(rowid)
            wake_screen()
            log.info("← %s", body)
            try:
                reply = handle_incoming(body)
            except Exception as e:
                log.error("handler error: %s\n%s", e, traceback.format_exc())
                reply = f"Something went wrong handling that: {e}"
            deliver(reply)
        time.sleep(POLL_INTERVAL)


def main():
    init_db()
    setup_schedules()
    deliver("JARVIS online. Watching your iMessages — say hi or /help.")
    backoff = 5
    while True:
        try:
            poll_loop()
        except KeyboardInterrupt:
            log.info("Shutting down.")
            return
        except Exception as e:
            log.error("FATAL: %s\n%s", e, traceback.format_exc())
            try:
                send_imessage(f"⚠️ JARVIS crashed: {e}. Restarting in {backoff}s.")
            except Exception:
                pass
            time.sleep(backoff)
            backoff = min(backoff * 2, 300)


if __name__ == "__main__":
    main()
