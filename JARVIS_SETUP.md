# JARVIS — iMessage + Cerebras edition (macOS)

A self-evolving personal AI companion that lives on your Mac, talks to you over
iMessage, and is powered by the free, fast **Cerebras** API.

## Features

**Core**
- Reads your iMessages from `~/Library/Messages/chat.db`, replies via AppleScript
- Cerebras LLM brain with function/tool calling
- Persistent SQLite memory (facts, tasks, goals, chat history, insights)
- **Auto-wakes the Mac's display** the moment a message arrives (`caffeinate`)

**Easy wins**
- Location-aware morning briefing (IP geolocation + weather, no API key)
- Voice replies via macOS `say` (toggle with `/voice on`)
- `/screenshot` — captures the Mac screen and sends it back over iMessage
- Clipboard bridge — `/clipboard` to read, `/copy <text>` to write

**Productivity**
- Calendar.app integration (add/list events)
- Apple Reminders sync (`/remind`)
- Mail.app unread summariser (`/mail`)
- Free web-search tool so answers can use live facts

**Self-evolving**
- Self-evolve job derives new insights about you every 6h
- Weekly self-review ("what I noticed about you this week", Sun 18:00)
- Sentiment tracking of your messages over time
- Proactive nudges for tasks left undone 2+ days (daily 18:30)

**Reliability**
- Crash auto-recovery loop + iMessage crash alert (works with launchd KeepAlive)
- `/status` health check (uptime, last poll, DB size, counts)

## One-time setup on the Mac

1. **Download the latest code**
   ```bash
   curl -o ~/jarvis/jarvis.py "https://raw.githubusercontent.com/naveen971538/new-/claude/setup-jarvis-macos-6nfhs/jarvis.py"
   ```

2. **Install dependencies** (High Sierra uses python3.9)
   ```bash
   python3.9 -m pip install "openai>=1.30,<2" "apscheduler>=3.10.4" python-dotenv
   ```

3. **Create `~/jarvis/.env`**
   ```bash
   nano ~/jarvis/.env
   ```
   ```
   CEREBRAS_API_KEY=csk-...        # free at https://cloud.cerebras.ai
   MY_IMESSAGE_ID=+447823753000    # the number/Apple-ID you text FROM
   ```
   Save: `Ctrl+O`, `Enter`, `Ctrl+X`.

4. **Grant Full Disk Access to Terminal**
   System Preferences → Security & Privacy → Privacy → Full Disk Access →
   add `/Applications/Utilities/Terminal.app` and tick it. (Required to read
   the Messages database.)

5. **Open the Messages app** and make sure you're signed into iMessage.

6. **Keep the Mac awake even with the screen off** (so messages are never missed)
   ```bash
   sudo pmset -a sleep 0
   sudo pmset -a disksleep 0
   ```

## Run it
```bash
cd ~/jarvis && python3.9 jarvis.py
```
Then text yourself from another device. JARVIS replies, and the screen wakes
automatically on each incoming message.

## Auto-start on login
```bash
curl -o ~/Library/LaunchAgents/com.jarvis.bot.plist "https://raw.githubusercontent.com/naveen971538/new-/claude/setup-jarvis-macos-6nfhs/com.jarvis.bot.plist"
sed -i '' "s/YOUR_USERNAME/$(whoami)/g" ~/Library/LaunchAgents/com.jarvis.bot.plist
launchctl load ~/Library/LaunchAgents/com.jarvis.bot.plist
```

## Commands
`/help /status /tasks /task /done /goals /goal /remember /recall /weather
/screenshot /clipboard /copy /voice /say /calendar /remind /mail /briefing
/review /evolve` — anything else is a normal chat.
