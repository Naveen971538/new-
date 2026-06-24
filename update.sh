#!/bin/bash
#
# JARVIS updater — safe, atomic, idempotent. Built for a 2011 iMac on
# macOS High Sierra 10.13.6 (bash 3.2, BSD userland, NO Homebrew).
#
# What it does, every time you run it:
#   1. Downloads the newest jarvis.py to a temp file (fails loudly on 404/empty/timeout).
#   2. Verifies it is real, compilable Python BEFORE replacing the live copy.
#   3. Backs up the current jarvis.py to jarvis.py.bak (rollback safety net).
#   4. Atomically swaps the new code into place (validates the staged bytes).
#   5. Restarts JARVIS (launchd if installed, otherwise nohup background) and
#      VERIFIES it actually came back up before claiming success.
#
# It NEVER touches your .env or jarvis.db. Safe to re-run any number of times.
#
# Usage:  bash ~/jarvis/update.sh
#
# NOTE: no `set -e`. Every command that may legitimately return nonzero is
# handled explicitly so the script can never half-abort and leave JARVIS down.

# --- Configuration -----------------------------------------------------------

BRANCH="claude/setup-jarvis-macos-6nfhs"
RAW_BASE="https://raw.githubusercontent.com/naveen971538/new-/${BRANCH}"
RAW_URL="${RAW_BASE}/jarvis.py"

APP_DIR="${HOME}/jarvis"
TARGET="${APP_DIR}/jarvis.py"
BACKUP="${APP_DIR}/jarvis.py.bak"
LOG="${APP_DIR}/jarvis.log"
LOCK="${APP_DIR}/.update.lock"

PY="/usr/local/bin/python3.9"

PLIST="${HOME}/Library/LaunchAgents/com.jarvis.bot.plist"
LABEL="com.jarvis.bot"

# Bounded network behavior so a flaky 2011 wifi can't hang the updater forever.
CURL_OPTS="-fsSL --connect-timeout 15 --max-time 120 --retry 2 --retry-delay 2"

# --- Helpers -----------------------------------------------------------------

say() { printf '%s\n' "$*"; }
ok()  { printf '  ok  %s\n' "$*"; }
err() { printf 'FAIL  %s\n' "$*" 1>&2; }

# Is the launchd job currently registered (loaded) under our label?
label_loaded() { launchctl list 2>/dev/null | grep -q "$LABEL"; }

# Is a real python jarvis.py process alive? Matches every launch form:
#   /usr/local/bin/python3.9 /Users/x/jarvis/jarvis.py   (launchd / absolute)
#   python3.9 jarvis.py                                   (manual, relative)
#   python3 jarvis.py / ./jarvis.py                       (pasted variants)
# The [p] bracket trick stops grep from matching its own command line.
# We restrict to python interpreters so editors/tail/less on the file
# (and this updater itself, whose argv is "bash .../update.sh") are spared.
jarvis_pids() {
    ps -axo pid,command 2>/dev/null \
        | grep -E "[p]ython[0-9.]* .*jarvis[.]py" \
        | awk '{print $1}'
}

# Clean up temp artifacts and the lock on any exit (success, failure, Ctrl-C).
TMP=""
cleanup() {
    [ -n "$TMP" ] && [ -f "$TMP" ] && rm -f "$TMP"
    rmdir "$LOCK" 2>/dev/null
}
trap cleanup EXIT INT TERM

say "JARVIS updater"
say "=============="

# --- 0. Preflight ------------------------------------------------------------

if [ ! -x "$PY" ]; then
    err "Python not found at $PY"
    err "Install python.org 3.9.7 first, then re-run."
    exit 1
fi

# Make sure the app dir exists (first-run friendliness; harmless if present).
mkdir -p "$APP_DIR" 2>/dev/null

# Single-runner lock. mkdir is atomic on BSD/bash 3.2. Removed in cleanup().
if ! mkdir "$LOCK" 2>/dev/null; then
    err "Another update appears to be running (lock: $LOCK)."
    err "If you're sure none is, remove it:  rmdir \"$LOCK\""
    # Don't run cleanup's rmdir on someone else's lock: disarm the trap first.
    trap - EXIT INT TERM
    [ -n "$TMP" ] && [ -f "$TMP" ] && rm -f "$TMP"
    exit 1
fi

# --- 1. Download to a temp file (atomic + verified) --------------------------

# Explicit XXXXXX template works identically on BSD (macOS) and GNU mktemp.
TMP=`mktemp "${TMPDIR:-/tmp}/jarvis_update.XXXXXX"` || TMP="${APP_DIR}/.jarvis.py.download.$$"

say "Downloading latest jarvis.py ..."
# -f  : fail (nonzero, empty body) on HTTP errors like 404 — so we never
#       write GitHub's HTML error page into jarvis.py.
# -sS : quiet but still show real errors.   -L : follow redirects.
# timeouts/retries: bounded so a stalled connection can't hang forever.
if ! curl $CURL_OPTS "$RAW_URL" -o "$TMP"; then
    err "Download failed (no network, timeout, or URL/branch wrong)."
    err "URL: $RAW_URL"
    err "Your existing JARVIS was left untouched."
    exit 1
fi

# Non-empty?
if [ ! -s "$TMP" ]; then
    err "Downloaded file is empty. Aborting; existing JARVIS untouched."
    exit 1
fi

# --- 2. Validate it is really jarvis.py and compiles -------------------------

# Cheap sanity check: it must mention JARVIS. (The compile below is the
# authoritative gate; we deliberately drop the weak shebang heuristic.)
if ! grep -q "JARVIS" "$TMP"; then
    err "Downloaded file doesn't mention JARVIS — looks wrong. Aborting."
    exit 1
fi

# Authoritative check: does it parse/compile under the target interpreter?
# We use the builtin compile() (syntax-checks in memory, writes NOTHING) rather
# than py_compile — py_compile refuses a /dev/null cfile (FileExistsError) and
# otherwise litters a .pyc. SyntaxError -> nonzero exit.
say "Verifying the download compiles ..."
if ! "$PY" -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$TMP" 2>/dev/null; then
    err "Downloaded jarvis.py failed to compile — it may be corrupt or truncated."
    err "Refusing to install it. Existing JARVIS untouched."
    exit 1
fi
ok "Download verified."

# --- 3. Skip work if identical (idempotent / no needless restart) ------------

if [ -f "$TARGET" ] && cmp -s "$TMP" "$TARGET"; then
    ok "Already up to date — jarvis.py is unchanged."
    say "Nothing to install; not restarting a healthy bot."
    exit 0
fi

# --- 4. Back up the current copy, then atomically install --------------------

if [ -f "$TARGET" ]; then
    if cp -p "$TARGET" "$BACKUP"; then
        ok "Backed up previous version -> $BACKUP"
    else
        err "Could not write backup at $BACKUP. Aborting to stay safe."
        exit 1
    fi
fi

# Atomic install: copy to a sibling temp in the SAME dir, then mv (rename
# within one filesystem is atomic) so jarvis.py is never half-written.
STAGE="${APP_DIR}/.jarvis.py.new.$$"
if ! cp "$TMP" "$STAGE"; then
    err "Could not stage new file in $APP_DIR (disk full?). Aborting; old version intact."
    rm -f "$STAGE" 2>/dev/null
    exit 1
fi
# Confirm the staged bytes are byte-identical to the validated download —
# closes the gap where a short write (disk full) installs unvalidated bytes.
if ! cmp -s "$TMP" "$STAGE"; then
    err "Staged copy differs from the verified download (disk full?). Aborting; old version intact."
    rm -f "$STAGE" 2>/dev/null
    exit 1
fi
chmod 644 "$STAGE" 2>/dev/null
if ! mv -f "$STAGE" "$TARGET"; then
    err "Could not move new file into place. Attempting to restore backup ..."
    if [ -f "$BACKUP" ]; then
        if cp -p "$BACKUP" "$TARGET"; then
            err "Restored previous jarvis.py from backup. Update aborted; bot unchanged."
        else
            err "RESTORE FAILED. JARVIS MAY BE DOWN. Manually run:"
            err "    cp \"$BACKUP\" \"$TARGET\""
        fi
    else
        err "No backup exists (first run). JARVIS MAY HAVE NO CODE. Re-run the updater."
    fi
    rm -f "$STAGE" 2>/dev/null
    exit 1
fi
ok "Installed new jarvis.py"

# Drop any stale bytecode cache so the new code can't be shadowed.
rm -rf "${APP_DIR}/__pycache__" 2>/dev/null

# --- 5. Restart JARVIS -------------------------------------------------------
#
# Golden rule to avoid DOUBLE-START (two bots both polling chat.db and double-
# replying): if a launchd job/plist is involved at all, drive it ONLY through
# launchctl — never also nohup-launch a manual copy.

# Stop any manually-launched python jarvis.py, escalating TERM -> KILL.
stop_manual_procs() {
    local pids i
    pids=`jarvis_pids`
    [ -z "$pids" ] && return 0
    kill $pids 2>/dev/null                 # SIGTERM, graceful
    for i in 1 2 3 4 5; do
        pids=`jarvis_pids`
        [ -z "$pids" ] && return 0
        sleep 1
    done
    pids=`jarvis_pids`
    [ -n "$pids" ] && kill -9 $pids 2>/dev/null   # SIGKILL stragglers
    sleep 1
    [ -z "`jarvis_pids`" ]
}

restart_launchd() {
    # Tear down whatever is loaded, ignoring nonzero (job may not be loaded).
    launchctl unload "$PLIST" 2>/dev/null
    # Wait for asynchronous teardown to actually complete before reloading.
    local i
    for i in 1 2 3 4 5; do
        label_loaded || break
        sleep 1
    done
    # Belt-and-suspenders: if still registered, force-remove the job.
    if label_loaded; then
        launchctl remove "$LABEL" 2>/dev/null
        sleep 1
    fi
    # Load. "already loaded" exits nonzero on 10.13, so we IGNORE the exit
    # code and judge success purely by launchctl list state.
    launchctl load "$PLIST" 2>/dev/null
    sleep 2
    if label_loaded; then
        ok "Restarted via launchd ($LABEL)."
        return 0
    fi
    err "launchd did not register $LABEL after load."
    err "Most common cause: this updater was NOT run from a Terminal window in"
    err "the logged-in desktop session (e.g. it was run over SSH), or Full Disk"
    err "Access isn't granted. Run it again from the Mac's own Terminal app."
    return 1
}

restart_manual() {
    # Even on the manual path, an ORPHANED launchd job (plist deleted but job
    # still loaded) would have KeepAlive respawn the OLD code -> double-start.
    # So unconditionally tear any loaded label down first.
    if label_loaded; then
        launchctl unload "$PLIST" 2>/dev/null
        launchctl remove "$LABEL" 2>/dev/null
        local i
        for i in 1 2 3 4 5; do
            label_loaded || break
            sleep 1
        done
    fi
    if label_loaded; then
        err "A launchd job '$LABEL' is still loaded and could respawn old code."
        err "Refusing to start a second manual copy. Unload it first with:"
        err "    launchctl remove \"$LABEL\""
        return 1
    fi

    # Now stop any manually-running copies and wait for them to die.
    if ! stop_manual_procs; then
        err "Could not stop an existing jarvis.py process; not starting another."
        err "Find it with:  ps -axo pid,command | grep jarvis.py"
        return 1
    fi

    # Relaunch fully detached: nohup survives terminal close; </dev/null so a
    # closing Terminal can't feed it EOF/errors on stdin.
    ( cd "$APP_DIR" && nohup "$PY" "$TARGET" </dev/null >>"$LOG" 2>&1 & )
    sleep 2
    if [ -n "`jarvis_pids`" ]; then
        ok "Started JARVIS in the background (logging to $LOG)."
        return 0
    fi
    err "JARVIS did not stay running after launch (check $LOG, e.g. bad .env)."
    return 1
}

say "Restarting JARVIS ..."
RESTART_OK=1
if [ -f "$PLIST" ]; then
    # Plist present -> launchd is the source of truth. Do NOT fall back to a
    # manual nohup launch (that is exactly what causes two bots).
    restart_launchd && RESTART_OK=0
else
    restart_manual && RESTART_OK=0
fi

# --- Done --------------------------------------------------------------------

say ""
if [ "$RESTART_OK" -eq 0 ]; then
    say "Update complete. JARVIS is running the latest code."
else
    err "Update installed the new code, but JARVIS did NOT come back up."
    say "  - Check the log:        tail -n 40 \"$LOG\""
    say "  - The previous code is: $BACKUP"
fi
say ""
say "If something looks wrong, roll back with:"
say "    cp \"$BACKUP\" \"$TARGET\"  &&  bash \"$APP_DIR/update.sh\""
exit "$RESTART_OK"
