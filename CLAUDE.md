# JARVIS — build target & hard constraints

**READ THIS BEFORE TOUCHING `jarvis.py`.** Every change to JARVIS must run on the
user's target machine, NOT on whatever machine is being used to develop.

## Target hardware / OS (NON-NEGOTIABLE)

- **2011 iMac**, Intel **x86_64** (no Apple Silicon).
- **macOS High Sierra 10.13.6** — Apple's oldest still-limping OS. Build for THIS.
- Never assume a newer macOS, newer Python, or the developer's current machine.

## Python

- Use **Python 3.9.7** installed from python.org (the `macosx10.9.pkg` installer).
- Invoke as **`/usr/local/bin/python3.9`** (NOT `python3` — that may be a broken
  3.14 install on this machine).
- Do **not** use language features newer than 3.9:
  - No `match`/`case`. No `X | Y` runtime unions (use `typing.Optional`/`Union`).
  - `list[int]` annotations are OK (PEP 585 is in 3.9).

## Dependencies — the #1 source of pain on this machine

- **No Homebrew.** Homebrew dropped 10.13; it will not install.
- Only packages with **pre-built x86_64 wheels** install cleanly. Source builds
  fail: the system Clang is from Xcode 10.1 and there is no Rust toolchain.
- Known-good pins when these are ever needed:
  - `grpcio==1.46.5`, `cryptography==38.0.4` (both ship `macosx_10_10_x86_64` wheels)
- **Prefer ZERO third-party dependencies.** The most reliable build calls the
  LLM API with **stdlib `urllib`** and avoids pip entirely. Reach for a package
  only when stdlib genuinely can't do it, and verify a 3.9/x86_64/macOS wheel exists first.
- Anything reachable via a **built-in macOS command is always safe** (no install):
  `osascript`, `say`, `screencapture`, `caffeinate`, `pmset`, `pbcopy`/`pbpaste`,
  `sqlite3` (stdlib), `systemsetup`, `networksetup`, `mdfind`, `open`.

## Channel: iMessage (the user is staying on iMessage — do NOT push Telegram)

- Read incoming from `~/Library/Messages/chat.db` (SQLite, read-only).
  Requires **Full Disk Access** granted to Terminal / the launching process.
- Send via `osascript` AppleScript (pass text as `on run` argv — never string-interpolate).
- The user texts JARVIS in their **"Note to Self" self-chat**. In a self-chat
  **every message is `is_from_me = 1`** — the user's text AND the bot's replies.
  To tell them apart, tag the bot's own replies with an invisible marker and skip
  any message containing it. Auto-detect the user's own handles (number + email).

## LLM

- OpenAI-compatible endpoint (Cerebras / Groq). Model is configurable via `.env`.
- Free, fast models the user has used: `gpt-oss-120b`, `llama-3.3-70b`.

## Deployment on the Mac

- Lives in `~/jarvis/` with `jarvis.py`, `.env`, `jarvis.db`, `jarvis.log`.
- Auto-start via launchd (`com.jarvis.bot.plist`, `KeepAlive=true`).
- Keep the Mac awake: `sudo pmset -a sleep 0 disksleep 0`.
- `.env`: `CEREBRAS_API_KEY` (or provider key) + `MY_IMESSAGE_ID`.

## Distribution

- Ship code from the **GitHub repo raw URL on the working branch**, not paste.rs
  (ephemeral). Branch: `claude/setup-jarvis-macos-6nfhs`.
