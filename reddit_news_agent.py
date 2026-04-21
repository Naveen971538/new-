"""Agentic Reddit news AI.

Give it a topic; it decides which subreddits to search, which posts to read
deeply, and when it has enough material to write a news-style briefing.

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

import anthropic
from dotenv import load_dotenv

from agent_tools import TOOLS, FinalizeSignal, dispatch

load_dotenv()

MODEL = "claude-sonnet-4-6"
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


def _print_trace(step: int, block: Any) -> None:
    if block.type == "text" and block.text.strip():
        print(f"  [step {step}] thought: {block.text.strip()[:200]}", file=sys.stderr)
    elif block.type == "tool_use":
        args_preview = json.dumps(block.input, ensure_ascii=False)[:200]
        print(
            f"  [step {step}] tool: {block.name}({args_preview})",
            file=sys.stderr,
        )


def run_agent(
    topic: str,
    subreddits: list[str] | None = None,
    max_steps: int = 15,
    trace: bool = False,
) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set. Add it to .env or your shell.")

    client = anthropic.Anthropic(api_key=api_key)
    messages: list[dict] = [
        {"role": "user", "content": _build_user_message(topic, subreddits)}
    ]
    system_blocks = [
        {
            "type": "text",
            "text": SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }
    ]

    for step in range(1, max_steps + 1):
        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system_blocks,
            tools=TOOLS,
            messages=messages,
        )

        if trace:
            for block in response.content:
                _print_trace(step, block)

        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return _force_finalize(client, messages, system_blocks, trace, reason="model stopped without calling finalize_briefing")

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                result = dispatch(block.name, block.input)
            except FinalizeSignal as finalize:
                if trace:
                    print(f"  [step {step}] finalize_briefing received", file=sys.stderr)
                return finalize.briefing
            tool_results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result}
            )

        messages.append({"role": "user", "content": tool_results})

    return _force_finalize(
        client,
        messages,
        system_blocks,
        trace,
        reason="research budget exhausted",
    )


def _force_finalize(
    client: anthropic.Anthropic,
    messages: list[dict],
    system_blocks: list[dict],
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
    response = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=system_blocks,
        tools=TOOLS,
        tool_choice={"type": "tool", "name": "finalize_briefing"},
        messages=messages,
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "finalize_briefing":
            return block.input.get("briefing_markdown", "").strip() or (
                "# Briefing\n\n_No briefing content returned._"
            )
    return "# Briefing\n\n_Agent failed to produce a briefing._"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Agentic Reddit news AI — Claude plans its own research and writes a briefing."
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
