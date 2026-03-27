"""Extract portfolio intelligence using SerpAPI web search."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from collections import Counter
from typing import Any
from urllib.parse import urlparse

from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.scripts.run_online_research import SerpApiClient
from app.scripts.run_online_research import _extract_hits


TECH_KEYWORDS = (
    "python",
    "fastapi",
    "django",
    "flask",
    "typescript",
    "javascript",
    "react",
    "next.js",
    "nextjs",
    "node",
    "postgresql",
    "mongodb",
    "redis",
    "docker",
    "kubernetes",
    "aws",
    "gcp",
    "azure",
    "llm",
    "rag",
    "pytorch",
    "tensorflow",
    "machine learning",
    "data science",
)


def _normalize_url(value: str) -> str:
    """Normalize URL by trimming trailing slash."""

    return value.strip().rstrip("/")


def _portfolio_domain(value: str) -> str:
    """Extract normalized portfolio domain."""

    host = (urlparse(value).hostname or "").casefold()
    return host.lstrip("www.")


def _build_queries(
    *,
    portfolio_url: str,
    full_name: str,
    role_selection: str,
) -> list[str]:
    """Build search query set for portfolio extraction."""

    domain = _portfolio_domain(portfolio_url)
    queries = [
        f'"{portfolio_url}"',
        f"site:{domain}",
    ]
    if full_name:
        queries.append(f'site:{domain} "{full_name}"')
    if full_name and role_selection:
        queries.append(f'site:{domain} "{full_name}" "{role_selection}"')
    return list(dict.fromkeys(query.strip() for query in queries if query.strip()))


def _pick_primary_hits(
    *,
    hits: list[dict[str, str]],
    portfolio_url: str,
    max_items: int,
) -> tuple[list[dict[str, str]], str | None]:
    """Pick best matching portfolio hits."""

    target_url = _normalize_url(portfolio_url).casefold()
    domain = _portfolio_domain(portfolio_url)

    exact_hits = [
        hit
        for hit in hits
        if isinstance(hit.get("link"), str) and _normalize_url(hit["link"]).casefold() == target_url
    ]
    if exact_hits:
        return exact_hits[:max_items], exact_hits[0].get("link")

    prefixed_hits = [
        hit
        for hit in hits
        if isinstance(hit.get("link"), str) and _normalize_url(hit["link"]).casefold().startswith(target_url)
    ]
    if prefixed_hits:
        return prefixed_hits[:max_items], prefixed_hits[0].get("link")

    domain_hits: list[dict[str, str]] = []
    for hit in hits:
        link = hit.get("link")
        if not isinstance(link, str):
            continue
        host = (urlparse(link).hostname or "").casefold().lstrip("www.")
        if host == domain:
            domain_hits.append(hit)
    if domain_hits:
        first_link = domain_hits[0].get("link")
        return domain_hits[:max_items], first_link if isinstance(first_link, str) else None

    first_link = hits[0].get("link") if hits else None
    return hits[:max_items], first_link if isinstance(first_link, str) else None


def _hits_to_evidence(hits: list[dict[str, str]], *, max_lines: int) -> list[str]:
    """Convert hits to concise evidence lines."""

    lines: list[str] = []
    for hit in hits:
        title = (hit.get("title") or "").strip()
        snippet = (hit.get("snippet") or "").strip()
        link = (hit.get("link") or "").strip()
        parts = [part for part in (title, snippet, link) if part]
        if not parts:
            continue
        lines.append(" | ".join(parts))
        if len(lines) >= max(1, max_lines):
            break
    return lines


def _extract_tech_signals(hits: list[dict[str, str]], *, max_items: int) -> list[str]:
    """Infer technology signals from hit titles/snippets."""

    corpus = " ".join(
        f"{hit.get('title', '')} {hit.get('snippet', '')}".casefold()
        for hit in hits
        if isinstance(hit, dict)
    )
    counts: Counter[str] = Counter()
    for keyword in TECH_KEYWORDS:
        if keyword in corpus:
            counts[keyword] += len(re.findall(re.escape(keyword), corpus))
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [item[0] for item in ranked[: max(1, max_items)]]


def _extract_project_signals(hits: list[dict[str, str]], *, max_items: int) -> list[str]:
    """Extract simple project keywords from hit titles."""

    candidates: list[str] = []
    for hit in hits:
        title = hit.get("title")
        if not isinstance(title, str):
            continue
        cleaned = re.sub(r"\s+", " ", title).strip()
        if not cleaned:
            continue
        cleaned = re.sub(r"\s*[-|].*$", "", cleaned).strip()
        if cleaned:
            candidates.append(cleaned)
    # De-dupe preserving order.
    return list(dict.fromkeys(candidates))[: max(1, max_items)]


async def _run(
    *,
    portfolio_url: str,
    full_name: str,
    role_selection: str,
) -> dict[str, Any]:
    """Execute portfolio extraction using SerpAPI."""

    runtime = get_runtime_config()
    settings = get_settings()
    if not settings.serpapi_api_key:
        raise RuntimeError("SERPAPI_API_KEY is required in .env")
    if not runtime.research.enabled:
        raise RuntimeError("research.enabled=false in YAML; enable it first")

    client = SerpApiClient(
        api_key=settings.serpapi_api_key,
        endpoint=runtime.research.google_search_url,
        engine=runtime.research.engine,
        timeout_seconds=runtime.research.request_timeout_seconds,
        max_concurrency=runtime.research.max_concurrency,
    )

    queries = _build_queries(
        portfolio_url=portfolio_url,
        full_name=full_name,
        role_selection=role_selection,
    )
    all_hits: list[dict[str, str]] = []
    for query in queries:
        payload = await client.search(query=query, num_results=runtime.research.results_per_query)
        hits = _extract_hits(payload, max_hits=runtime.research.enrichment.max_portfolio_hits)
        all_hits.extend(hits)

    deduped: list[dict[str, str]] = []
    seen_links: set[str] = set()
    for hit in all_hits:
        link = hit.get("link")
        if not isinstance(link, str) or not link:
            continue
        if link in seen_links:
            continue
        seen_links.add(link)
        deduped.append(hit)
        if len(deduped) >= runtime.research.enrichment.max_portfolio_hits * 2:
            break

    primary_hits, matched_portfolio_url = _pick_primary_hits(
        hits=deduped,
        portfolio_url=portfolio_url,
        max_items=runtime.research.enrichment.max_portfolio_hits,
    )
    evidence = _hits_to_evidence(primary_hits, max_lines=8)
    return {
        "input_portfolio_url": portfolio_url,
        "matched_portfolio_url": matched_portfolio_url,
        "portfolio_domain": _portfolio_domain(portfolio_url),
        "queries": queries,
        "portfolio_search_hits_count": len(deduped),
        "portfolio_primary_hits_count": len(primary_hits),
        "technology_signals": _extract_tech_signals(primary_hits, max_items=10),
        "project_signals": _extract_project_signals(primary_hits, max_items=6),
        "evidence": evidence,
        "top_portfolio_hits": primary_hits,
    }


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(
        description="Extract portfolio profile intelligence using SerpAPI."
    )
    parser.add_argument(
        "--portfolio-url",
        required=True,
        help="Portfolio URL (example: https://flowcv.me/mrbinitsapkota).",
    )
    parser.add_argument("--full-name", default="", help="Optional candidate full name hint.")
    parser.add_argument("--role-selection", default="", help="Optional role hint.")
    return parser


def main() -> None:
    """Entrypoint for `python -m app.scripts.extract_portfolio_profile`."""

    parser = _build_parser()
    args = parser.parse_args()
    payload = asyncio.run(
        _run(
            portfolio_url=args.portfolio_url.strip(),
            full_name=args.full_name.strip(),
            role_selection=args.role_selection.strip(),
        )
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
