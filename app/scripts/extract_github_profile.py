"""Extract top GitHub repository intelligence for one profile."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import re
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.parse import urlparse
from urllib.request import Request
from urllib.request import urlopen

from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


def _safe_int(value: Any) -> int:
    """Convert unknown value to int safely."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_utc_datetime(value: Any) -> datetime | None:
    """Parse ISO datetime string into UTC."""

    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_github_username(value: str | None) -> str | None:
    """Parse GitHub username from URL or plain username."""

    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None

    if raw.startswith(("http://", "https://")):
        try:
            parsed = urlparse(raw)
        except ValueError:
            return None
        host = (parsed.hostname or "").casefold()
        if "github.com" not in host:
            return None
        parts = [item for item in parsed.path.strip("/").split("/") if item]
        if not parts:
            return None
        return parts[0]

    if re.match(r"^[A-Za-z0-9][A-Za-z0-9-]{0,38}$", raw):
        return raw
    return None


def _clip_text(value: str | None, *, max_chars: int) -> str | None:
    """Clip text to max length."""

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


def _strip_markdown(text: str) -> str:
    """Convert markdown-ish text to compact plain text."""

    value = text
    value = re.sub(r"```.*?```", " ", value, flags=re.DOTALL)
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\*([^*]+)\*", r"\1", value)
    value = re.sub(r"__([^_]+)__", r"\1", value)
    value = re.sub(r"_([^_]+)_", r"\1", value)
    value = re.sub(r"^#{1,6}\s*", "", value, flags=re.MULTILINE)
    value = re.sub(r"^>\s*", "", value, flags=re.MULTILINE)
    value = re.sub(r"^\s*[-*+]\s+", "", value, flags=re.MULTILINE)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _summarize_readme(text: str | None, *, max_chars: int = 260) -> str | None:
    """Extract concise README summary."""

    if not isinstance(text, str) or not text.strip():
        return None
    title: str | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = _strip_markdown(stripped)
            break

    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    source = text
    for paragraph in paragraphs:
        cleaned = _strip_markdown(paragraph)
        if cleaned and len(cleaned.split()) >= 4:
            source = paragraph
            break
    clean = _strip_markdown(source)
    if title and title.casefold() not in clean.casefold() and len(title.split()) <= 10:
        clean = f"{title}. {clean}".strip()
    return _clip_text(clean, max_chars=max_chars)


def _rank_top_repos(repos_payload: list[dict[str, Any]], *, max_items: int) -> list[dict[str, Any]]:
    """Rank repositories by stars/forks/recency."""

    filtered = [item for item in repos_payload if isinstance(item, dict) and not item.get("fork")]
    ranked = sorted(
        filtered,
        key=lambda repo: (
            _safe_int(repo.get("stargazers_count")),
            _safe_int(repo.get("forks_count")),
            str(repo.get("pushed_at") or ""),
        ),
        reverse=True,
    )
    return ranked[: max(1, max_items)]


def _derive_primary_languages(
    repos_payload: list[dict[str, Any]],
    *,
    max_items: int,
) -> list[str]:
    """Return top languages by repository frequency."""

    counts: dict[str, int] = {}
    for repo in repos_payload:
        language = repo.get("language") if isinstance(repo, dict) else None
        if not isinstance(language, str) or not language.strip():
            continue
        key = language.strip()
        counts[key] = counts.get(key, 0) + 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    return [name for name, _ in ranked[: max(1, max_items)]]


def _derive_activity_status(
    repos_payload: list[dict[str, Any]],
    *,
    active_within_days: int,
) -> str:
    """Classify profile as active/inactive from latest push timestamp."""

    timestamps = [
        parsed
        for parsed in (_parse_utc_datetime(repo.get("pushed_at")) for repo in repos_payload)
        if parsed is not None
    ]
    if not timestamps:
        return "inactive"
    latest = max(timestamps)
    threshold = datetime.now(tz=timezone.utc) - timedelta(days=max(1, active_within_days))
    return "active" if latest >= threshold else "inactive"


class GitHubApiClient:
    """Async GitHub client using urllib in worker threads."""

    def __init__(
        self,
        *,
        api_base_url: str,
        timeout_seconds: float,
        user_agent: str,
        token: str | None,
        max_concurrency: int,
    ) -> None:
        """Initialize client."""

        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent
        self._token = token.strip() if isinstance(token, str) and token.strip() else None
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        accept: str = "application/vnd.github+json",
        allow_not_found: bool = False,
    ) -> Any | None:
        """GET JSON endpoint and decode payload."""

        async with self._semaphore:
            payload = await asyncio.to_thread(
                self._request_sync,
                path,
                params or {},
                accept,
                allow_not_found,
            )
        if payload is None:
            return None
        return json.loads(payload.decode("utf-8"))

    async def get_text(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        accept: str = "application/vnd.github.raw+json",
        allow_not_found: bool = False,
    ) -> str | None:
        """GET text endpoint and decode UTF-8 payload."""

        async with self._semaphore:
            payload = await asyncio.to_thread(
                self._request_sync,
                path,
                params or {},
                accept,
                allow_not_found,
            )
        if payload is None:
            return None
        return payload.decode("utf-8", errors="replace")

    def _request_sync(
        self,
        path: str,
        params: dict[str, str],
        accept: str,
        allow_not_found: bool,
    ) -> bytes | None:
        """Run one blocking HTTP request."""

        query = urlencode(params)
        url = f"{self._api_base_url}{path}"
        if query:
            url = f"{url}?{query}"

        headers = {
            "Accept": accept,
            "User-Agent": self._user_agent,
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        request = Request(url=url, headers=headers)
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
                return response.read()
        except HTTPError as exc:
            if allow_not_found and exc.code in {404, 409}:
                return None
            raise


async def _fetch_readme_summary(
    client: GitHubApiClient,
    *,
    owner: str,
    repo_name: str,
) -> str | None:
    """Fetch README and return compact summary."""

    raw = await client.get_text(
        f"/repos/{owner}/{repo_name}/readme",
        allow_not_found=True,
    )
    if raw:
        return _summarize_readme(raw)

    payload = await client.get_json(
        f"/repos/{owner}/{repo_name}/readme",
        allow_not_found=True,
    )
    if not isinstance(payload, dict):
        return None

    content = payload.get("content")
    encoding = payload.get("encoding")
    if isinstance(content, str) and isinstance(encoding, str) and encoding.casefold() == "base64":
        try:
            decoded = base64.b64decode(content.encode("utf-8")).decode("utf-8", errors="replace")
        except Exception:
            return None
        return _summarize_readme(decoded)
    return None


async def _fetch_repo_commit_activity(
    client: GitHubApiClient,
    *,
    owner: str,
    repo_name: str,
    window_days: int,
) -> dict[str, Any]:
    """Return recent commit activity for one repository."""

    since_dt = datetime.now(tz=timezone.utc) - timedelta(days=max(1, window_days))
    since_iso = since_dt.isoformat().replace("+00:00", "Z")
    payload = await client.get_json(
        f"/repos/{owner}/{repo_name}/commits",
        params={
            "per_page": "100",
            "since": since_iso,
        },
        allow_not_found=True,
    )
    if not isinstance(payload, list):
        return {
            "window_days": max(1, window_days),
            "commits_in_window": 0,
            "latest_commit_at": None,
            "is_capped_at_100": False,
        }

    latest_commit_at: str | None = None
    if payload:
        first = payload[0]
        if isinstance(first, dict):
            commit_obj = first.get("commit")
            if isinstance(commit_obj, dict):
                author_obj = commit_obj.get("author")
                if isinstance(author_obj, dict) and isinstance(author_obj.get("date"), str):
                    latest_commit_at = author_obj["date"]

    return {
        "window_days": max(1, window_days),
        "commits_in_window": len(payload),
        "latest_commit_at": latest_commit_at,
        "is_capped_at_100": len(payload) == 100,
    }


async def _analyze_repo(
    client: GitHubApiClient,
    *,
    owner: str,
    repo_payload: dict[str, Any],
    commit_window_days: int,
) -> dict[str, Any]:
    """Build one repo extraction row."""

    repo_name = str(repo_payload.get("name") or "").strip()
    readme_summary, commit_activity = await asyncio.gather(
        _fetch_readme_summary(
            client,
            owner=owner,
            repo_name=repo_name,
        ),
        _fetch_repo_commit_activity(
            client,
            owner=owner,
            repo_name=repo_name,
            window_days=commit_window_days,
        ),
    )

    return {
        "name": repo_name,
        "description": _clip_text(repo_payload.get("description"), max_chars=220),
        "html_url": repo_payload.get("html_url"),
        "stars": _safe_int(repo_payload.get("stargazers_count")),
        "forks": _safe_int(repo_payload.get("forks_count")),
        "language": repo_payload.get("language"),
        "topics": repo_payload.get("topics") if isinstance(repo_payload.get("topics"), list) else [],
        "pushed_at": repo_payload.get("pushed_at"),
        "updated_at": repo_payload.get("updated_at"),
        "readme_summary": readme_summary,
        "commit_activity": commit_activity,
    }


async def _run(
    *,
    github_url: str | None,
    username: str | None,
    top_repos: int,
    commit_window_days: int,
) -> dict[str, Any]:
    """Run extraction for one GitHub profile."""

    resolved_username = _parse_github_username(github_url) or _parse_github_username(username)
    if not resolved_username:
        raise RuntimeError("Provide --github-url or --username for a valid GitHub profile.")

    runtime_config = get_runtime_config().research.github
    settings = get_settings()
    client = GitHubApiClient(
        api_base_url=runtime_config.api_base_url,
        timeout_seconds=runtime_config.request_timeout_seconds,
        user_agent=runtime_config.user_agent,
        token=settings.github_api_token,
        max_concurrency=get_runtime_config().research.max_concurrency,
    )

    user_payload = await client.get_json(f"/users/{resolved_username}", allow_not_found=True)
    if not isinstance(user_payload, dict):
        raise RuntimeError(f"GitHub profile not found for username={resolved_username}")

    repos_payload = await client.get_json(
        f"/users/{resolved_username}/repos",
        params={
            "sort": "updated",
            "direction": "desc",
            "per_page": "100",
            "type": "owner",
        },
        allow_not_found=True,
    )
    repos = repos_payload if isinstance(repos_payload, list) else []
    ranked_repos = _rank_top_repos(repos, max_items=max(1, top_repos))

    analyzed = await asyncio.gather(
        *(
            _analyze_repo(
                client,
                owner=resolved_username,
                repo_payload=repo,
                commit_window_days=commit_window_days,
            )
            for repo in ranked_repos
        )
    )
    total_stars = sum(_safe_int(repo.get("stars")) for repo in analyzed)
    commits_window_total = sum(
        _safe_int((repo.get("commit_activity") or {}).get("commits_in_window"))
        for repo in analyzed
    )

    return {
        "username": resolved_username,
        "profile_url": user_payload.get("html_url") or f"https://github.com/{resolved_username}",
        "profile": {
            "name": user_payload.get("name"),
            "bio": user_payload.get("bio"),
            "public_repos": _safe_int(user_payload.get("public_repos")),
            "followers": _safe_int(user_payload.get("followers")),
            "following": _safe_int(user_payload.get("following")),
        },
        "top_repositories": analyzed,
        "aggregate": {
            "top_languages": _derive_primary_languages(
                analyzed,
                max_items=max(1, get_runtime_config().research.github.max_primary_languages),
            ),
            "activity_status": _derive_activity_status(
                repos,
                active_within_days=get_runtime_config().research.github.activity_active_within_days,
            ),
            "total_stars_top_repos": total_stars,
            "commits_in_window_top_repos": commits_window_total,
            "commit_window_days": max(1, commit_window_days),
        },
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Extract GitHub intelligence (top repos, stars, languages, "
            "commit activity, and README summaries)."
        )
    )
    parser.add_argument("--github-url", default=None, help="GitHub profile URL.")
    parser.add_argument("--username", default=None, help="GitHub username.")
    parser.add_argument("--top-repos", type=int, default=5, help="Max top repositories.")
    parser.add_argument(
        "--commit-window-days",
        type=int,
        default=90,
        help="Commit activity lookback window in days.",
    )
    return parser


def main() -> None:
    """Entrypoint for `python -m app.scripts.extract_github_profile`."""

    parser = _build_parser()
    args = parser.parse_args()
    payload = asyncio.run(
        _run(
            github_url=args.github_url,
            username=args.username,
            top_repos=max(1, args.top_repos),
            commit_window_days=max(1, args.commit_window_days),
        )
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
