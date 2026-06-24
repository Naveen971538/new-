# JARVIS — iMessage + Cerebras edition (macOS)

A self-evolving personal AI companion that lives on your Mac, talks to you over
iMessage, and is powered by the free, fast **Cerebras** API.

## Features

**Core**
- Reads your iMessages from `~/Library/Messages/chat.db`, replies via AppleScript
- Cerebras LLM brain with function/tool calling
- Persistent SQLite memory (facts, tasks, goals, chat history, insights)
- **Stays awake the whole time it's running** — holds a `caffeinate` assertion
  (system/disk/display sleep all blocked) for as long as JARVIS is alive, so it
  keeps polling and replying even if you never touch the Mac. Also nudges the
  display on the instant a message arrives.

**Easy wins**
- Location-aware morning briefing (IP geolocation + weather, no API key)
- Voice replies via macOS `say` (toggle with `/voice on`)
- `/screenshot` — captures the Mac screen and sends it back over iMessage
- Clipboard bridge — `/clipboard` to read, `/copy <text>` to write
- `/open <app|url|path>` — launch an app, open a link, or open a file remotely
- `/find <query>` — Spotlight search the Mac for files by name
- **Live visual dashboard** — a dark, auto-refreshing web UI at
  `http://localhost:8787` showing the conversation, mood chart, open tasks,
  goals, what JARVIS knows about you, and recent insights. Pure stdlib
  `http.server`, bound to localhost only (never exposed to the network).
  Open it any time with `/dashboard`.

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
- Messages.app watchdog — auto-relaunches it if it ever quits/crashes (checked
  every 5 min and at startup), since a dead Messages.app means silent send failures
- `/status` health check (uptime, last poll, DB size, disk free, Messages.app
  state, Wi-Fi network, counts)

**Security**
- **Strict single-sender allowlist** — only the one phone number/Apple ID set
  in `MY_IMESSAGE_ID` can talk to JARVIS. Messages from anyone else (a
  stranger, or even your own *other* Apple ID handle, e.g. your linked email,
  if it's not the one configured) are never processed as commands and never
  get a reply.
- **Intrusion logging + one-time alert** — every blocked attempt is recorded
  (`/security` to view, also shown on the dashboard), and you get a single
  iMessage alert the first time an unrecognized contact tries (no spam on
  repeat attempts from the same sender, until JARVIS restarts).
- Phone numbers are matched format-tolerantly (spaces/dashes/country code
  variations all match your number), so this can't accidentally lock you out.

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

## Updating later — one command

Install the updater **once** (sets up an `update-jarvis` shortcut):
```bash
mkdir -p ~/jarvis && curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 --retry-delay 2 "https://raw.githubusercontent.com/naveen971538/new-/claude/setup-jarvis-macos-6nfhs/update.sh" -o ~/jarvis/update.sh && [ -s ~/jarvis/update.sh ] && head -n1 ~/jarvis/update.sh | grep -q '^#!/bin/bash' && bash -n ~/jarvis/update.sh && chmod +x ~/jarvis/update.sh && { grep -q 'alias update-jarvis' ~/.bash_profile 2>/dev/null || echo "alias update-jarvis='bash ~/jarvis/update.sh'" >> ~/.bash_profile ; } && echo 'Installed. Open a NEW Terminal window, then type:  update-jarvis'
```

After that, **whenever you want the latest JARVIS, just open Terminal and type:**
```bash
update-jarvis
```
(or `bash ~/jarvis/update.sh` if the alias hasn't loaded yet). It safely
downloads the newest `jarvis.py`, verifies it compiles, backs up the old copy
to `jarvis.py.bak`, swaps it in atomically, and restarts JARVIS — all in one go.
It never touches your `.env` or `jarvis.db`, and is safe to run any time. If an
update ever misbehaves, roll back with:
```bash
cp ~/jarvis/jarvis.py.bak ~/jarvis/jarvis.py && bash ~/jarvis/update.sh
```

## Commands
`/help /status /tasks /task /done /goals /goal /remember /recall /weather
/screenshot /clipboard /copy /voice /say /calendar /setcalendar /delevent
/remind /mail /briefing /review /evolve /open /find /dashboard /security` —
anything else is a normal chat.
