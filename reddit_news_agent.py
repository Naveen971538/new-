"""Agentic Reddit news AI (OpenRouter edition).

Give it a topic; it decides which subreddits to search, which posts to read
deeply, and when it has enough material to write a news-style briefing.

Uses OpenRouter (OpenAI-compatible API) so you can run on free models.

Usage:
    python reddit_news_agent.py "latest AI news"
    python reddit_news_agent.py "nuclear fusion progress" --subreddits fusion,energy
    python reddit_news_agent.py "AI news" --max-steps 20 --trace
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI

from agent_tools import TOOLS, FinalizeSignal, dispatch

load_dotenv()

DEFAULT_MODEL = "meta-llama/llama-3.3-70b-instruct:free"
DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
MAX_TOKENS = 4096

SYSTEM_PROMPT = """\
You are a senior Reddit news analyst. Your job is to produce a concise,
trustworthy news briefing on a topic the user provides, sourced entirely from
recent Reddit discussion.

# Research strategy
1. Start broad. Use `search_reddit` across all of Reddit to see which
   communities and threads are most active on the topic.
2. Identify 2-4 relevant subreddits from the search results. Use
   `list_subreddit_posts` (sort=hot or top/day) on the best 1-2 of them to see
   what's front-page right now.
3. Pick the 3-6 most newsworthy posts. Call `get_post_details` on each to read
   the full selftext and the top comments before writing anything. Comments
   often contain the real story (corrections, context, skepticism).
4. When you have enough material, call `finalize_briefing` exactly once with
   the full markdown briefing.

# Quality bar
- Every claim must be traceable to a post returned by your tools. Never invent
  posts, users, quotes, or permalinks.
- Cite each story with a markdown link to the permalink, and include the
  subreddit, score, and comment count.
- Dedupe: if multiple subreddits discuss the same event, merge into one entry.
- Be honest about sample size and bias. Reddit is not a news wire; flag
  low-score items, single-source claims, and ideologically skewed subs.
- Prefer items from the last week unless the user asked otherwise.

# Output format (pass this as `briefing_markdown`)
```
# <Topic> — Reddit Briefing
_As of <UTC date>; based on <N> posts across <M> subreddits._

## TL;DR
- 3-5 single-sentence bullets.

## Top Stories
### <Story title>
2-4 sentences of summary. What happened, why it matters, what commenters are saying.
**Source:** [r/<sub> — <post title>](<permalink>) · score <N> · <N> comments

(repeat per story, 3-6 total)

## Also Notable
- One-line items with inline source links.

## Caveats
- Sample bias / echo-chamber notes.
- Anything you couldn't verify.
```

# Budget
You have at most 15 tool calls total. Plan accordingly: don't waste calls on
duplicate searches or posts you've already fetched. When in doubt, write the
briefing with what you have rather than over-researching.
"""


def _build_user_message(topic: str, subreddits: list[str] | None) -> str:
    lines = [f"Topic: {topic}"]
    if subreddits:
        lines.append(
            "Subreddit hints (consider these, but you can also discover others): "
            + ", ".join(f"r/{s.lstrip('r/').lstrip('/')}" for s in subreddits)
        )
    lines.append(
        "Research the topic on Reddit and produce the briefing by calling "
        "`finalize_briefing` when ready."
    )
    return "\n".join(lines)


def _print_trace(step: int, message: Any) -> None:
    content = getattr(message, "content", None)
    if content:
        print(f"  [step {step}] thought: {content.strip()[:200]}", file=sys.stderr)
    for tc in getattr(message, "tool_calls", None) or []:
        args_preview = (tc.function.arguments or "")[:200]
        print(
            f"  [step {step}] tool: {tc.function.name}({args_preview})",
            file=sys.stderr,
        )


def _assistant_message_to_dict(message: Any) -> dict:
    out: dict = {"role": "assistant", "content": message.content or ""}
    if getattr(message, "tool_calls", None):
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,
                },
            }
            for tc in message.tool_calls
        ]
    return out


def _parse_tool_args(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def run_agent(
    topic: str,
    subreddits: list[str] | None = None,
    max_steps: int = 15,
    trace: bool = False,
) -> str:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Add it to .env or your shell. "
            "Get a free key at https://openrouter.ai/keys"
        )

    base_url = os.environ.get("OPENROUTER_BASE_URL", DEFAULT_BASE_URL)
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL)

    client = OpenAI(
        api_key=api_key,
        base_url=base_url,
        default_headers={
            "HTTP-Referer": "https://github.com/naveen971538/new-",
            "X-Title": "Reddit News Agent",
        },
    )

    messages: list[dict] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(topic, subreddits)},
    ]

    for step in range(1, max_steps + 1):
        response = client.chat.completions.create(
            model=model,
            max_tokens=MAX_TOKENS,
            tools=TOOLS,
            messages=messages,
        )
        choice = response.choices[0]
        message = choice.message

        if trace:
            _print_trace(step, message)

        messages.append(_assistant_message_to_dict(message))

        tool_calls = message.tool_calls or []
        if not tool_calls:
            return _force_finalize(
                client,
                model,
                messages,
                trace,
                reason="model stopped without calling finalize_briefing",
            )

        for tc in tool_calls:
            args = _parse_tool_args(tc.function.arguments)
            try:
                result = dispatch(tc.function.name, args)
            except FinalizeSignal as finalize:
                if trace:
                    print(f"  [step {step}] finalize_briefing received", file=sys.stderr)
                return finalize.briefing
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                }
            )

    return _force_finalize(
        client,
        model,
        messages,
        trace,
        reason="research budget exhausted",
    )


def _force_finalize(
    client: OpenAI,
    model: str,
    messages: list[dict],
    trace: bool,
    reason: str,
) -> str:
    if trace:
        print(f"  [force-finalize] {reason}", file=sys.stderr)
    messages.append(
        {
            "role": "user",
            "content": (
                f"Stop researching ({reason}). Call `finalize_briefing` NOW with "
                "a best-effort briefing based only on the observations you have. "
                "If coverage is thin, say so explicitly in the Caveats section."
            ),
        }
    )
    response = client.chat.completions.create(
        model=model,
        max_tokens=MAX_TOKENS,
        tools=TOOLS,
        tool_choice={"type": "function", "function": {"name": "finalize_briefing"}},
        messages=messages,
    )
    message = response.choices[0].message
    for tc in message.tool_calls or []:
        if tc.function.name == "finalize_briefing":
            args = _parse_tool_args(tc.function.arguments)
            text = (args.get("briefing_markdown") or "").strip()
            if text:
                return text
    # Some free models ignore tool_choice and answer in plain text — fall back.
    if message.content:
        return message.content.strip()
    return "# Briefing\n\n_Agent failed to produce a briefing._"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agentic Reddit news AI — the model plans its own research and writes a briefing (OpenRouter)."
    )
    parser.add_argument("topic", help="What to research (e.g. 'latest AI news').")
    parser.add_argument(
        "--subreddits",
        help="Comma-separated subreddit hints, e.g. 'MachineLearning,LocalLLaMA'.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=15,
        help="Maximum tool-call iterations (default 15).",
    )
    parser.add_argument(
        "--trace",
        action="store_true",
        help="Print the agent's thoughts and tool calls to stderr.",
    )
    args = parser.parse_args()

    subs = (
        [s.strip() for s in args.subreddits.split(",") if s.strip()]
        if args.subreddits
        else None
    )

    try:
        briefing = run_agent(
            topic=args.topic,
            subreddits=subs,
            max_steps=args.max_steps,
            trace=args.trace,
        )
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(briefing)
    return 0


if __name__ == "__main__":
    sys.exit(main())
