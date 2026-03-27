"""Enrich candidate social links and research summary using SerpAPI."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import or_, select

from app.core.runtime_config import ResearchRuntimeConfig, get_runtime_config
from app.core.settings import get_settings
from app.infra.database import get_async_session_factory
from app.model.applicant_application import ApplicantApplication

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class CandidateSeed:
    """Candidate fields required for online research."""

    id: UUID
    full_name: str
    role_selection: str
    linkedin_url: str | None
    twitter_url: str | None


@dataclass(slots=True)
class CandidateResearchResult:
    """Candidate-level enrichment result."""

    application_id: UUID
    linkedin_url: str | None
    twitter_url: str | None
    summary: str


class SerpApiClient:
    """Async wrapper around SerpAPI Google search endpoint."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str,
        engine: str,
        timeout_seconds: float,
        max_concurrency: int,
    ) -> None:
        """Initialize client with endpoint, auth, and concurrency bounds."""

        self._api_key = api_key
        self._endpoint = endpoint
        self._engine = engine
        self._timeout_seconds = timeout_seconds
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def search(self, *, query: str, num_results: int) -> dict[str, Any]:
        """Execute one search query and return parsed JSON payload."""

        async with self._semaphore:
            return await asyncio.to_thread(
                self._search_sync,
                query=query,
                num_results=num_results,
            )

    def _search_sync(self, *, query: str, num_results: int) -> dict[str, Any]:
        """Run one blocking HTTP call to SerpAPI."""

        params = {
            "engine": self._engine,
            "q": query,
            "num": max(1, num_results),
            "api_key": self._api_key,
        }
        url = f"{self._endpoint}?{urlencode(params)}"
        request = Request(
            url=url,
            headers={
                "Accept": "application/json",
                "User-Agent": "hireme-online-research/1.0",
            },
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
            data = response.read().decode("utf-8")
        payload = json.loads(data)
        if not isinstance(payload, dict):
            raise RuntimeError("SerpAPI response is not a JSON object")
        return payload


def _extract_hits(payload: dict[str, Any], *, max_hits: int) -> list[dict[str, str]]:
    """Extract top organic hits from one SerpAPI response payload."""

    organic_results = payload.get("organic_results")
    if not isinstance(organic_results, list):
        return []

    hits: list[dict[str, str]] = []
    for item in organic_results:
        if not isinstance(item, dict):
            continue
        link = item.get("link")
        if not isinstance(link, str) or not link.startswith(("http://", "https://")):
            continue
        title = item.get("title") if isinstance(item.get("title"), str) else ""
        snippet = item.get("snippet") if isinstance(item.get("snippet"), str) else ""
        hits.append({"link": link, "title": title.strip(), "snippet": snippet.strip()})
        if len(hits) >= max_hits:
            break
    return hits


def _matches_domain(url: str, allowed_domains: tuple[str, ...]) -> bool:
    """Return whether URL hostname belongs to any allowed domain suffix."""

    hostname = urlparse(url).hostname or ""
    hostname = hostname.casefold()
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in allowed_domains)


def _pick_first_link(
    hits: list[dict[str, str]],
    *,
    allowed_domains: tuple[str, ...],
) -> str | None:
    """Pick first link from hits that matches one of the allowed domains."""

    for hit in hits:
        link = hit.get("link")
        if isinstance(link, str) and _matches_domain(link, allowed_domains):
            return link
    return None


def _render_query(
    template: str,
    *,
    full_name: str,
    role_selection: str,
) -> str:
    """Render candidate-specific query string from template."""

    return template.format(full_name=full_name, role_selection=role_selection).strip()


def _build_summary(
    *,
    full_name: str,
    role_selection: str,
    linkedin_url: str | None,
    twitter_url: str | None,
    profile_hits: list[dict[str, str]],
    linkedin_hits: list[dict[str, str]],
    twitter_hits: list[dict[str, str]],
    max_chars: int,
) -> str:
    """Build compact plain-text research summary for DB storage."""

    lines: list[str] = [
        f"candidate: {full_name}",
        f"role: {role_selection}",
        f"linkedin_url: {linkedin_url or 'not_found'}",
        f"twitter_url: {twitter_url or 'not_found'}",
        "top_profile_results:",
    ]
    lines.extend(_hits_to_lines(profile_hits))
    lines.append("top_linkedin_results:")
    lines.extend(_hits_to_lines(linkedin_hits))
    lines.append("top_twitter_results:")
    lines.extend(_hits_to_lines(twitter_hits))
    summary = "\n".join(lines).strip()
    return summary[:max_chars]


def _hits_to_lines(hits: list[dict[str, str]]) -> list[str]:
    """Serialize hit list into concise lines."""

    if not hits:
        return ["- none"]
    lines: list[str] = []
    for hit in hits:
        title = hit.get("title", "")
        snippet = hit.get("snippet", "")
        link = hit.get("link", "")
        parts = [title, snippet, link]
        text = " | ".join(part for part in parts if part)
        lines.append(f"- {text}" if text else "- none")
    return lines


async def _research_candidate(
    *,
    candidate: CandidateSeed,
    client: SerpApiClient,
    config: ResearchRuntimeConfig,
) -> CandidateResearchResult:
    """Run candidate research queries and return discovered links + summary."""

    linkedin_queries = [
        _render_query(
            config.linkedin_query_template,
            full_name=candidate.full_name,
            role_selection=candidate.role_selection,
        )
    ]
    twitter_queries = [
        _render_query(
            config.twitter_query_template,
            full_name=candidate.full_name,
            role_selection=candidate.role_selection,
        )
    ]
    profile_query = _render_query(
        config.profile_query_template,
        full_name=candidate.full_name,
        role_selection=candidate.role_selection,
    )

    if config.retrieval_loop_use_llm:
        # Fallback pass uses name-only search terms if role-specific query misses.
        linkedin_queries.append(f'site:linkedin.com/in "{candidate.full_name}"')
        twitter_queries.append(f'(site:x.com OR site:twitter.com) "{candidate.full_name}"')

    linkedin_hits: list[dict[str, str]] = []
    twitter_hits: list[dict[str, str]] = []
    profile_hits: list[dict[str, str]] = []

    if config.always_web_retrieval_enabled:
        profile_payload = await client.search(
            query=profile_query,
            num_results=config.results_per_query,
        )
        profile_hits = _extract_hits(profile_payload, max_hits=config.links_limit_per_query)

    linkedin_url = candidate.linkedin_url
    if not linkedin_url:
        for query in linkedin_queries:
            payload = await client.search(query=query, num_results=config.results_per_query)
            hits = _extract_hits(payload, max_hits=config.links_limit_per_query)
            if not linkedin_hits:
                linkedin_hits = hits
            linkedin_url = _pick_first_link(hits, allowed_domains=("linkedin.com",))
            if linkedin_url:
                break

    twitter_url = candidate.twitter_url
    if not twitter_url:
        for query in twitter_queries:
            payload = await client.search(query=query, num_results=config.results_per_query)
            hits = _extract_hits(payload, max_hits=config.links_limit_per_query)
            if not twitter_hits:
                twitter_hits = hits
            twitter_url = _pick_first_link(
                hits,
                allowed_domains=("x.com", "twitter.com"),
            )
            if twitter_url:
                break

    summary = _build_summary(
        full_name=candidate.full_name,
        role_selection=candidate.role_selection,
        linkedin_url=linkedin_url,
        twitter_url=twitter_url,
        profile_hits=profile_hits,
        linkedin_hits=linkedin_hits,
        twitter_hits=twitter_hits,
        max_chars=config.max_summary_chars,
    )
    return CandidateResearchResult(
        application_id=candidate.id,
        linkedin_url=linkedin_url,
        twitter_url=twitter_url,
        summary=summary,
    )


async def _run_research(
    *,
    limit: int,
    offset: int,
    dry_run: bool,
    include_existing: bool,
    application_ids: list[UUID] | None,
) -> None:
    """Load candidate set, run SerpAPI enrichment, and persist updates."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    research = runtime_config.research

    if not research.enabled:
        raise RuntimeError("Research pipeline is disabled (research.enabled=false)")
    if research.provider != "serpapi":
        raise RuntimeError("Only research.provider=serpapi is supported")
    if not settings.serpapi_api_key:
        raise RuntimeError("SERPAPI_API_KEY is required")

    client = SerpApiClient(
        api_key=settings.serpapi_api_key,
        endpoint=research.google_search_url,
        engine=research.engine,
        timeout_seconds=research.request_timeout_seconds,
        max_concurrency=research.max_concurrency,
    )
    session_factory = get_async_session_factory(runtime_config.postgres)

    async with session_factory() as session:
        statement = select(
            ApplicantApplication.id,
            ApplicantApplication.full_name,
            ApplicantApplication.role_selection,
            ApplicantApplication.linkedin_url,
            ApplicantApplication.twitter_url,
        ).order_by(ApplicantApplication.created_at.desc())

        if application_ids:
            statement = statement.where(ApplicantApplication.id.in_(application_ids))
        else:
            statement = statement.where(
                ApplicantApplication.applicant_status.in_(research.target_statuses)
            )
            only_missing = research.only_when_missing_urls and not include_existing
            if only_missing:
                statement = statement.where(
                    or_(
                        ApplicantApplication.linkedin_url.is_(None),
                        ApplicantApplication.twitter_url.is_(None),
                        ApplicantApplication.online_research_summary.is_(None),
                    )
                )
            statement = statement.offset(max(0, offset)).limit(max(1, limit))

        rows = (await session.execute(statement)).all()
        seeds = [CandidateSeed(*row) for row in rows]

    logger.info("research candidates selected=%s", len(seeds))
    if not seeds:
        return

    semaphore = asyncio.Semaphore(max(1, research.max_concurrency))
    processed = 0
    updated = 0

    async def process(seed: CandidateSeed) -> None:
        nonlocal processed, updated
        async with semaphore:
            try:
                result = await _research_candidate(candidate=seed, client=client, config=research)
            except Exception:
                logger.exception("research failed for application_id=%s", seed.id)
                processed += 1
                return

            if dry_run:
                logger.info(
                    "dry-run application_id=%s linkedin=%s twitter=%s",
                    seed.id,
                    result.linkedin_url,
                    result.twitter_url,
                )
                processed += 1
                return

            async with session_factory() as session:
                entity = await session.get(ApplicantApplication, seed.id)
                if entity is None:
                    processed += 1
                    return
                if not entity.linkedin_url and result.linkedin_url:
                    entity.linkedin_url = result.linkedin_url
                if not entity.twitter_url and result.twitter_url:
                    entity.twitter_url = result.twitter_url
                entity.online_research_summary = result.summary
                await session.commit()
                updated += 1
            processed += 1

    await asyncio.gather(*(process(seed) for seed in seeds))
    logger.info("research finished processed=%s updated=%s dry_run=%s", processed, updated, dry_run)


def _build_parser() -> argparse.ArgumentParser:
    """Build script CLI parser."""

    parser = argparse.ArgumentParser(
        description="Run SerpAPI-based candidate online research and enrich DB records."
    )
    parser.add_argument("--limit", type=int, default=100, help="Max candidates to process.")
    parser.add_argument("--offset", type=int, default=0, help="Candidates offset.")
    parser.add_argument(
        "--application-id",
        action="append",
        default=None,
        help="Specific candidate application id. Can be repeated.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run searches but do not persist DB updates.",
    )
    parser.add_argument(
        "--include-existing",
        action="store_true",
        help="Process rows even when linkedin/twitter/research_summary already exists.",
    )
    return parser


async def _main_async() -> None:
    """Run script async entrypoint."""

    parser = _build_parser()
    args = parser.parse_args()
    application_ids = None
    if args.application_id:
        application_ids = [UUID(value) for value in args.application_id]
    await _run_research(
        limit=args.limit,
        offset=args.offset,
        dry_run=bool(args.dry_run),
        include_existing=bool(args.include_existing),
        application_ids=application_ids,
    )


def main() -> None:
    """Entrypoint for `python -m app.scripts.run_online_research`."""

    asyncio.run(_main_async())


if __name__ == "__main__":
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
