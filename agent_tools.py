"""Claude tool schemas and dispatcher for the Reddit news agent."""

from __future__ import annotations

import json
from typing import Any

import reddit_client


class FinalizeSignal(Exception):
    """Raised when the model calls finalize_briefing to terminate the loop."""

    def __init__(self, briefing: str) -> None:
        super().__init__("finalize_briefing called")
        self.briefing = briefing


TOOLS: list[dict] = [
    {
        "name": "search_reddit",
        "description": (
            "Search Reddit for posts matching a query. Use this FIRST when you "
            "need a broad landscape scan, or to discover which subreddits are "
            "most active on a topic. Restrict to a specific subreddit by "
            "passing `subreddit`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "subreddit": {
                    "type": "string",
                    "description": "Optional subreddit (with or without 'r/'). Omit to search all of Reddit.",
                },
                "sort": {
                    "type": "string",
                    "enum": ["relevance", "hot", "top", "new", "comments"],
                    "description": "Result ordering. Default: relevance.",
                },
                "time_filter": {
                    "type": "string",
                    "enum": ["hour", "day", "week", "month", "year", "all"],
                    "description": "Time window. Default: week.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many results to return (1-25, default 10).",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_subreddit_posts",
        "description": (
            "List posts from a single subreddit (hot/top/new/rising). Use this "
            "once you've identified a relevant community and want its current "
            "front page."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "subreddit": {
                    "type": "string",
                    "description": "Subreddit name (with or without 'r/').",
                },
                "sort": {
                    "type": "string",
                    "enum": ["hot", "top", "new", "rising"],
                    "description": "Listing order. Default: hot.",
                },
                "time_filter": {
                    "type": "string",
                    "enum": ["hour", "day", "week", "month", "year", "all"],
                    "description": "Only used with sort=top. Default: day.",
                },
                "limit": {
                    "type": "integer",
                    "description": "How many posts to return (1-25, default 10).",
                },
            },
            "required": ["subreddit"],
        },
    },
    {
        "name": "get_post_details",
        "description": (
            "Fetch a post's full selftext and its top comments. Use this to go "
            "deeper on a headline that looks newsworthy before writing it up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "post_id": {
                    "type": "string",
                    "description": "Reddit post id (the 'id' field from a previous result).",
                },
                "comment_limit": {
                    "type": "integer",
                    "description": "Number of top comments to return (1-25, default 10).",
                },
            },
            "required": ["post_id"],
        },
    },
    {
        "name": "finalize_briefing",
        "description": (
            "TERMINAL: call exactly once when research is complete. Submit the "
            "final markdown news briefing. After this is called, no further "
            "tools will run."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "briefing_markdown": {
                    "type": "string",
                    "description": "The complete news briefing in markdown.",
                }
            },
            "required": ["briefing_markdown"],
        },
    },
]


def _encode(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def dispatch(tool_name: str, tool_input: dict) -> str:
    """Run a tool call. Returns a JSON string to put in a tool_result block.

    Raises FinalizeSignal when the model calls finalize_briefing.
    """
    try:
        if tool_name == "search_reddit":
            return _encode(
                reddit_client.search(
                    query=tool_input["query"],
                    subreddit=tool_input.get("subreddit"),
                    sort=tool_input.get("sort", "relevance"),
                    time_filter=tool_input.get("time_filter", "week"),
                    limit=tool_input.get("limit", 10),
                )
            )
        if tool_name == "list_subreddit_posts":
            return _encode(
                reddit_client.list_posts(
                    subreddit=tool_input["subreddit"],
                    sort=tool_input.get("sort", "hot"),
                    time_filter=tool_input.get("time_filter", "day"),
                    limit=tool_input.get("limit", 10),
                )
            )
        if tool_name == "get_post_details":
            return _encode(
                reddit_client.post_details(
                    post_id=tool_input["post_id"],
                    comment_limit=tool_input.get("comment_limit", 10),
                )
            )
        if tool_name == "finalize_briefing":
            raise FinalizeSignal(tool_input["briefing_markdown"])
        return _encode({"error": f"unknown tool: {tool_name}"})
    except FinalizeSignal:
        raise
    except Exception as exc:
        return _encode({"error": f"{type(exc).__name__}: {exc}"})
