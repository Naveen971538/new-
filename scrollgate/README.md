# ScrollGate

A small web app for controlling short-video watching (YouTube Shorts, Reels, TikTok) from an iPhone.
It doesn't block anything by force — it puts a deliberate gate in front of the thing you open on
reflex, and keeps an honest record of what happened.

## How it works

Every session has to pass through three questions before anything opens:

1. **Where** — one app, chosen up front.
2. **How long** — 5 to 30 minutes, capped by what's left of today's budget.
3. **Why** — a typed reason, minimum eight characters. The unlock button stays disabled until you write one.

Then you hold the unlock button for five seconds. That hold is the point: it's long enough that a
reflex-opening never survives it. When it completes, the app opens in a new tab and a countdown
starts.

When the timer runs out the phone vibrates and the clock goes red and counts up, so overtime is
visible rather than invisible. Pressing "I'm done" ends the session and asks one question —
worth it, meh, or regret — which is what makes the stats worth reading later.

Three limits close the gate on their own:

- a **daily minute budget** (default 30),
- a **session cap** per day (default 3),
- a **cooldown** between sessions (default 45 minutes), which is what stops the "just one more" loop.

The Today screen shows minutes left, sessions used, and a streak of days under budget. Stats shows
a seven-day bar chart with over-budget days in red, your regret rate, and the list of reasons you
actually gave — that list is usually the most useful screen in the app.

## Running it on an iPhone

Everything is static — no build step, no server-side code, no accounts. All data stays in the
browser's `localStorage` on that device, and the Settings screen can export it as JSON.

The repository has a workflow that publishes its root to GitHub Pages on every push to `main`
(`.github/workflows/deploy-pages.yml`). That workflow is currently failing, because Pages itself
has never been enabled on the repository — `actions/configure-pages` exits within seconds when
there's no Pages site to configure. Enabling it is a one-time setting:

1. Repository **Settings → Pages**.
2. Set **Source** to **GitHub Actions**.
3. Re-run the failed workflow (or push anything to `main`).

After that, every push to `main` publishes, and this app lives at:

```
https://naveen971538.github.io/new-/scrollgate/
```

Then on the iPhone:

1. Open that URL in Safari.
2. Share → **Add to Home Screen**.

iOS only permits Add to Home Screen and service workers over HTTPS or on `localhost`, so the
installed, offline-capable version needs a real HTTPS host — a `python3 -m http.server` on your
LAN is fine for a quick look in the browser, but it won't install.

It then launches full-screen like a native app and works offline via the service worker.

## Making it actually binding

The app is a speed bump, not a lock — you can always ignore it. To give it teeth, pair it with
iOS Screen Time: set an app limit on the short-video apps themselves, and treat ScrollGate as the
only route you allow yourself to take to unlock one. The friction then lands where the habit is,
and ScrollGate stays the place where the decision and the record live.
