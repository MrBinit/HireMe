"""Extract LinkedIn profile intelligence (search hits + optional resume cross-reference)."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.scripts.extract_linkedin_cross_reference import _extract_linkedin_profile_text
from app.scripts.extract_linkedin_cross_reference import _extract_linkedin_handle
from app.scripts.extract_linkedin_cross_reference import _hits_to_evidence
from app.scripts.extract_linkedin_cross_reference import _run as _run_cross_reference
from app.scripts.extract_linkedin_cross_reference import _select_primary_linkedin_hits
from app.scripts.run_online_research import SerpApiClient
from app.scripts.run_online_research import _extract_hits


def _search_only_query_context(
    *,
    linkedin_url: str,
    full_name: str,
    role_selection: str,
) -> dict[str, str]:
    """Build query context for LinkedIn-only extraction."""

    return {
        "linkedin_url": linkedin_url,
        "linkedin_handle": _extract_linkedin_handle(linkedin_url),
        "full_name": full_name,
        "role_selection": role_selection,
    }


async def _search_only_linkedin_hits(
    *,
    linkedin_url: str,
    full_name: str,
    role_selection: str,
) -> list[dict[str, str]]:
    """Search LinkedIn hits without candidate DB dependency."""

    runtime = get_runtime_config()
    settings = get_settings()
    if not settings.serpapi_api_key:
        raise RuntimeError("SERPAPI_API_KEY is required in .env")
    if not runtime.research.enabled:
        raise RuntimeError("research.enabled=false in YAML; enable it first")

    cfg = runtime.research.linkedin_extract
    context = _search_only_query_context(
        linkedin_url=linkedin_url,
        full_name=full_name,
        role_selection=role_selection,
    )

    queries: list[str] = []
    for template in cfg.query_templates:
        query = template.format(**context).strip()
        if query:
            queries.append(query)
    queries = list(dict.fromkeys(queries))

    client = SerpApiClient(
        api_key=settings.serpapi_api_key,
        endpoint=runtime.research.google_search_url,
        engine=runtime.research.engine,
        timeout_seconds=runtime.research.request_timeout_seconds,
        max_concurrency=runtime.research.max_concurrency,
    )

    all_hits: list[dict[str, str]] = []
    for query in queries:
        payload = await client.search(query=query, num_results=cfg.results_per_query)
        hits = _extract_hits(payload, max_hits=cfg.max_linkedin_hits)
        for hit in hits:
            link = hit.get("link", "")
            host = (urlparse(link).hostname or "").casefold()
            if host == "linkedin.com" or host.endswith(".linkedin.com"):
                all_hits.append(hit)

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
        if len(deduped) >= cfg.max_linkedin_hits:
            break
    return deduped


def _build_search_only_output(
    *,
    linkedin_url: str,
    full_name: str,
    role_selection: str,
    hits: list[dict[str, str]],
) -> dict[str, Any]:
    """Build compact LinkedIn extraction payload from search hits only."""

    cfg = get_runtime_config().research.linkedin_extract
    primary_hits, matched_profile_url = _select_primary_linkedin_hits(
        hits=hits,
        linkedin_url=linkedin_url,
        full_name=full_name,
    )
    return {
        "mode": "search_only",
        "input_linkedin_url": linkedin_url,
        "full_name_hint": full_name or None,
        "role_selection_hint": role_selection or None,
        "matched_profile_url": matched_profile_url,
        "linkedin_search_hits_count": len(hits),
        "linkedin_primary_hits_count": len(primary_hits),
        "evidence": _hits_to_evidence(primary_hits, limit=cfg.max_evidence_lines),
        "top_linkedin_hits": primary_hits[:6],
    }


async def _run(
    *,
    linkedin_url: str,
    application_id: UUID | None,
    full_name: str,
    role_selection: str,
    linkedin_text: str | None,
) -> dict[str, Any]:
    """Run LinkedIn extractor in either search-only or cross-reference mode."""

    if application_id is not None:
        payload = await _run_cross_reference(
            linkedin_url=linkedin_url,
            application_id=application_id,
            linkedin_text=linkedin_text,
        )
        payload["mode"] = "cross_reference"
        return payload

    hits = await _search_only_linkedin_hits(
        linkedin_url=linkedin_url,
        full_name=full_name,
        role_selection=role_selection,
    )
    payload = _build_search_only_output(
        linkedin_url=linkedin_url,
        full_name=full_name,
        role_selection=role_selection,
        hits=hits,
    )
    if linkedin_text:
        payload["linkedin_extracted"] = _extract_linkedin_profile_text(linkedin_text)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Extract LinkedIn profile intelligence. "
            "Use --application-id for resume cross-reference mode."
        )
    )
    parser.add_argument(
        "--linkedin-url",
        required=True,
        help="LinkedIn profile URL (example: https://www.linkedin.com/in/mrbinit/).",
    )
    parser.add_argument(
        "--application-id",
        default=None,
        help="Candidate UUID for resume cross-reference mode.",
    )
    parser.add_argument(
        "--full-name",
        default="",
        help="Optional name hint for search-only mode.",
    )
    parser.add_argument(
        "--role-selection",
        default="",
        help="Optional role hint for search-only mode.",
    )
    parser.add_argument(
        "--linkedin-text-file",
        default=None,
        help="Optional LinkedIn pasted text file for profile section extraction.",
    )
    return parser


def main() -> None:
    """Entrypoint for `python -m app.scripts.extract_linkedin_profile`."""

    parser = _build_parser()
    args = parser.parse_args()
    application_id = UUID(args.application_id) if args.application_id else None
    linkedin_text: str | None = None
    if args.linkedin_text_file:
        linkedin_text = Path(args.linkedin_text_file).read_text(encoding="utf-8")

    payload = asyncio.run(
        _run(
            linkedin_url=args.linkedin_url,
            application_id=application_id,
            full_name=args.full_name.strip(),
            role_selection=args.role_selection.strip(),
            linkedin_text=linkedin_text,
        )
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
