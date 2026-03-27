"""Extract X/Twitter profile and recent post intelligence using Twitter API v2."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen

from app.core.settings import get_settings


def _parse_username(value: str) -> str:
    """Parse Twitter username from @handle, URL, or plain input."""

    raw = value.strip()
    if not raw:
        raise RuntimeError("username/handle is required")

    if raw.startswith("@"):
        raw = raw[1:]
    elif raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
        except ValueError as exc:
            raise RuntimeError("invalid twitter URL") from exc
        host = (parsed.hostname or "").casefold()
        if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            raise RuntimeError("URL is not an X/Twitter domain")
        path_parts = [item for item in parsed.path.split("/") if item]
        if not path_parts:
            raise RuntimeError("twitter URL does not include username")
        raw = path_parts[0]

    username = raw.strip()
    if not re.match(r"^[A-Za-z0-9_]{1,15}$", username):
        raise RuntimeError("invalid twitter username format")
    return username


def _safe_int(value: Any) -> int:
    """Convert unknown value to int."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _clip_text(value: str | None, *, max_chars: int) -> str | None:
    """Clip text safely to max length."""

    if not isinstance(value, str):
        return None
    text = " ".join(value.split()).strip()
    if not text:
        return None
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _extract_topics(posts: list[dict[str, Any]], *, max_items: int) -> list[str]:
    """Extract simple hashtag/topic signals from posts."""

    hashtag_counts: Counter[str] = Counter()
    token_counts: Counter[str] = Counter()

    stop_tokens = {
        "https",
        "http",
        "co",
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "have",
        "your",
        "about",
        "just",
    }

    for post in posts:
        text = post.get("text")
        if not isinstance(text, str):
            continue
        for tag in re.findall(r"#([A-Za-z0-9_]+)", text):
            hashtag_counts[tag.casefold()] += 1
        for token in re.findall(r"[A-Za-z][A-Za-z0-9\-\+]{2,}", text.casefold()):
            if token in stop_tokens:
                continue
            if token.startswith("http"):
                continue
            token_counts[token] += 1

    ranked: list[str] = []
    for tag, _ in sorted(hashtag_counts.items(), key=lambda item: (-item[1], item[0])):
        ranked.append(f"#{tag}")
        if len(ranked) >= max_items:
            return ranked

    for token, _ in sorted(token_counts.items(), key=lambda item: (-item[1], item[0])):
        ranked.append(token)
        if len(ranked) >= max_items:
            break
    return ranked


class TwitterApiClient:
    """Minimal async wrapper for Twitter API v2."""

    def __init__(self, *, bearer_token: str, timeout_seconds: float) -> None:
        """Initialize API client."""

        self._bearer_token = bearer_token
        self._timeout_seconds = timeout_seconds

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET one API endpoint and parse JSON."""

        return await asyncio.to_thread(self._get_json_sync, path, params or {})

    def _get_json_sync(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        """Run blocking HTTP request against Twitter API."""

        query = urlencode(params)
        url = f"https://api.twitter.com/2{path}"
        if query:
            url = f"{url}?{query}"
        request = Request(
            url=url,
            headers={
                "Authorization": f"Bearer {self._bearer_token}",
                "Accept": "application/json",
                "User-Agent": "hireme-twitter-extractor/1.0",
            },
        )
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"twitter api request failed status={exc.code} body={body}") from exc

        if not isinstance(payload, dict):
            raise RuntimeError("twitter api response is not a JSON object")
        return payload


async def _run(
    *,
    handle: str,
    max_posts: int,
) -> dict[str, Any]:
    """Run Twitter profile + recent post extraction."""

    settings = get_settings()
    bearer = settings.twitter_bearer_token
    if not bearer:
        raise RuntimeError("TWITTER_BEARER_TOKEN is required in .env")

    username = _parse_username(handle)
    client = TwitterApiClient(bearer_token=bearer, timeout_seconds=12.0)

    user_payload = await client.get_json(
        f"/users/by/username/{username}",
        params={
            "user.fields": "description,public_metrics,created_at,location,verified,url",
        },
    )
    user_data = user_payload.get("data")
    if not isinstance(user_data, dict):
        raise RuntimeError(f"twitter user not found for username={username}")

    user_id = str(user_data.get("id") or "").strip()
    if not user_id:
        raise RuntimeError("twitter user id missing in API response")

    tweets_payload = await client.get_json(
        f"/users/{user_id}/tweets",
        params={
            "max_results": str(max(5, min(max_posts, 100))),
            "exclude": "retweets,replies",
            "tweet.fields": "created_at,public_metrics,lang",
        },
    )
    posts_raw = tweets_payload.get("data")
    posts_data = posts_raw if isinstance(posts_raw, list) else []

    recent_posts: list[dict[str, Any]] = []
    total_engagement = 0
    for item in posts_data:
        if not isinstance(item, dict):
            continue
        metrics = item.get("public_metrics")
        metrics_dict = metrics if isinstance(metrics, dict) else {}
        like_count = _safe_int(metrics_dict.get("like_count"))
        retweet_count = _safe_int(metrics_dict.get("retweet_count"))
        reply_count = _safe_int(metrics_dict.get("reply_count"))
        quote_count = _safe_int(metrics_dict.get("quote_count"))
        engagement = like_count + retweet_count + reply_count + quote_count
        total_engagement += engagement
        recent_posts.append(
            {
                "id": item.get("id"),
                "created_at": item.get("created_at"),
                "lang": item.get("lang"),
                "text": _clip_text(item.get("text"), max_chars=280),
                "metrics": {
                    "like_count": like_count,
                    "retweet_count": retweet_count,
                    "reply_count": reply_count,
                    "quote_count": quote_count,
                    "engagement_total": engagement,
                },
            }
        )

    public_metrics = user_data.get("public_metrics")
    public_metrics_dict = public_metrics if isinstance(public_metrics, dict) else {}
    posts_count = len(recent_posts)

    return {
        "username": user_data.get("username") or username,
        "profile_url": f"https://x.com/{user_data.get('username') or username}",
        "profile": {
            "id": user_data.get("id"),
            "name": user_data.get("name"),
            "description": _clip_text(user_data.get("description"), max_chars=300),
            "location": user_data.get("location"),
            "url": user_data.get("url"),
            "verified": bool(user_data.get("verified")),
            "created_at": user_data.get("created_at"),
            "public_metrics": {
                "followers_count": _safe_int(public_metrics_dict.get("followers_count")),
                "following_count": _safe_int(public_metrics_dict.get("following_count")),
                "tweet_count": _safe_int(public_metrics_dict.get("tweet_count")),
                "listed_count": _safe_int(public_metrics_dict.get("listed_count")),
            },
        },
        "recent_posts": recent_posts,
        "aggregate": {
            "posts_fetched": posts_count,
            "average_engagement_per_post": (total_engagement / posts_count) if posts_count else 0.0,
            "last_post_at": recent_posts[0].get("created_at") if recent_posts else None,
            "topic_signals": _extract_topics(recent_posts, max_items=8),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(
        description="Extract X/Twitter profile and recent-post intelligence via API v2."
    )
    parser.add_argument(
        "--handle",
        required=True,
        help="Twitter handle (e.g. @BinitSapkota1 or https://x.com/BinitSapkota1).",
    )
    parser.add_argument("--max-posts", type=int, default=15, help="Max recent posts to fetch.")
    return parser


def main() -> None:
    """Entrypoint for `python -m app.scripts.extract_twitter_profile`."""

    parser = _build_parser()
    args = parser.parse_args()
    payload = asyncio.run(
        _run(
            handle=args.handle,
            max_posts=max(5, min(args.max_posts, 100)),
        )
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
