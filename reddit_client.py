"""PRAW wrapper that returns plain JSON-serialisable dicts.

The agent layer never touches PRAW objects directly; this keeps tool results
stable and cheaply printable into Claude's context.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

import praw
from dotenv import load_dotenv

load_dotenv()

_SELFTEXT_PREVIEW_CHARS = 400
_SELFTEXT_FULL_CHARS = 4000
_COMMENT_CHARS = 800

_VALID_SEARCH_SORTS = {"relevance", "hot", "top", "new", "comments"}
_VALID_LISTING_SORTS = {"hot", "top", "new", "rising"}
_VALID_TIME_FILTERS = {"hour", "day", "week", "month", "year", "all"}


@lru_cache(maxsize=1)
def _reddit() -> praw.Reddit:
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    user_agent = os.environ.get("REDDIT_USER_AGENT")
    if not (client_id and client_secret and user_agent):
        raise RuntimeError(
            "Missing Reddit credentials. Set REDDIT_CLIENT_ID, "
            "REDDIT_CLIENT_SECRET and REDDIT_USER_AGENT in your environment."
        )
    return praw.Reddit(
        client_id=client_id,
        client_secret=client_secret,
        user_agent=user_agent,
        check_for_async=False,
    )


def _summarise_post(submission: Any) -> dict:
    selftext = submission.selftext or ""
    return {
        "id": submission.id,
        "title": submission.title,
        "subreddit": str(submission.subreddit),
        "author": str(submission.author) if submission.author else "[deleted]",
        "score": submission.score,
        "upvote_ratio": getattr(submission, "upvote_ratio", None),
        "num_comments": submission.num_comments,
        "created_utc": int(submission.created_utc),
        "url": submission.url,
        "permalink": f"https://www.reddit.com{submission.permalink}",
        "is_self": submission.is_self,
        "over_18": submission.over_18,
        "selftext_preview": selftext[:_SELFTEXT_PREVIEW_CHARS]
        + ("…" if len(selftext) > _SELFTEXT_PREVIEW_CHARS else ""),
    }


def search(
    query: str,
    subreddit: str | None = None,
    sort: str = "relevance",
    time_filter: str = "week",
    limit: int = 10,
) -> list[dict]:
    if sort not in _VALID_SEARCH_SORTS:
        raise ValueError(f"sort must be one of {_VALID_SEARCH_SORTS}")
    if time_filter not in _VALID_TIME_FILTERS:
        raise ValueError(f"time_filter must be one of {_VALID_TIME_FILTERS}")
    limit = max(1, min(int(limit), 25))

    target = _reddit().subreddit(subreddit.lstrip("r/") if subreddit else "all")
    results = target.search(query, sort=sort, time_filter=time_filter, limit=limit)
    return [_summarise_post(s) for s in results]


def list_posts(
    subreddit: str,
    sort: str = "hot",
    time_filter: str = "day",
    limit: int = 10,
) -> list[dict]:
    if sort not in _VALID_LISTING_SORTS:
        raise ValueError(f"sort must be one of {_VALID_LISTING_SORTS}")
    if time_filter not in _VALID_TIME_FILTERS:
        raise ValueError(f"time_filter must be one of {_VALID_TIME_FILTERS}")
    limit = max(1, min(int(limit), 25))

    sub = _reddit().subreddit(subreddit.lstrip("r/"))
    if sort == "hot":
        listing = sub.hot(limit=limit)
    elif sort == "new":
        listing = sub.new(limit=limit)
    elif sort == "rising":
        listing = sub.rising(limit=limit)
    else:
        listing = sub.top(time_filter=time_filter, limit=limit)
    return [_summarise_post(s) for s in listing]


def post_details(post_id: str, comment_limit: int = 10) -> dict:
    comment_limit = max(1, min(int(comment_limit), 25))
    submission = _reddit().submission(id=post_id)
    submission.comment_sort = "top"
    submission.comments.replace_more(limit=0)

    top_comments = []
    for comment in submission.comments[: comment_limit * 2]:
        body = getattr(comment, "body", "") or ""
        if not body or body in ("[removed]", "[deleted]"):
            continue
        if getattr(comment, "stickied", False):
            continue
        top_comments.append(
            {
                "author": str(comment.author) if comment.author else "[deleted]",
                "score": comment.score,
                "body": body[:_COMMENT_CHARS]
                + ("…" if len(body) > _COMMENT_CHARS else ""),
            }
        )
        if len(top_comments) >= comment_limit:
            break

    summary = _summarise_post(submission)
    selftext = submission.selftext or ""
    summary["selftext"] = selftext[:_SELFTEXT_FULL_CHARS] + (
        "…" if len(selftext) > _SELFTEXT_FULL_CHARS else ""
    )
    summary["top_comments"] = top_comments
    summary.pop("selftext_preview", None)
    return summary
