"""Enrich shortlisted candidates with LinkedIn/X/GitHub/portfolio intelligence."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from uuid import UUID

from sqlalchemy import select

from app.core.runtime_config import BedrockRuntimeConfig, ResearchRuntimeConfig, get_runtime_config
from app.core.settings import get_settings
from app.infra.bedrock_runtime import BedrockInvocationError, BedrockRuntimeClient
from app.infra.database import get_async_session_factory
from app.model.applicant_application import ApplicantApplication
from app.scripts.run_online_research import SerpApiClient, _extract_hits

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@dataclass(slots=True)
class CandidateSeed:
    """Candidate fields needed for enrichment processing."""

    id: UUID
    full_name: str
    role_selection: str
    linkedin_url: str | None
    twitter_url: str | None
    github_url: str
    portfolio_url: str | None
    parse_result: dict[str, Any] | None


def _extract_resume_skills_and_employers(
    parse_result: dict[str, Any] | None,
) -> tuple[list[str], list[str]]:
    """Extract de-duplicated resume skills and employer names from parse_result."""

    if not isinstance(parse_result, dict):
        return [], []

    skills_raw = parse_result.get("skills")
    skills = [str(item).strip() for item in skills_raw] if isinstance(skills_raw, list) else []
    skills = [item for item in skills if item]

    employers: list[str] = []
    work_raw = parse_result.get("work_experience")
    if isinstance(work_raw, list):
        for item in work_raw:
            if not isinstance(item, dict):
                continue
            company = item.get("company")
            if isinstance(company, str) and company.strip():
                employers.append(company.strip())

    return list(dict.fromkeys(skills)), list(dict.fromkeys(employers))


def _normalize_text(value: str) -> str:
    """Normalize text for case-insensitive token matching."""

    return re.sub(r"\s+", " ", value.strip().casefold())


def _parse_github_username(github_url: str | None) -> str | None:
    """Extract GitHub username from URL."""

    if not github_url:
        return None
    try:
        parsed = urlparse(github_url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    if "github.com" not in host:
        return None
    parts = [item for item in parsed.path.strip("/").split("/") if item]
    if not parts:
        return None
    return parts[0]


def _domain_match(link: str, domains: tuple[str, ...]) -> bool:
    """Return whether URL host matches one of allowed domains."""

    host = (urlparse(link).hostname or "").casefold()
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def _pick_first_url_by_domain(hits: list[dict[str, str]], domains: tuple[str, ...]) -> str | None:
    """Pick first hit URL matching allowed domains."""

    for hit in hits:
        link = hit.get("link")
        if isinstance(link, str) and _domain_match(link, domains):
            return link
    return None


def _safe_int(value: Any) -> int:
    """Convert unknown values to int with a safe fallback."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_utc_datetime(value: Any) -> datetime | None:
    """Parse GitHub-style ISO timestamp to UTC datetime."""

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


def _coerce_text_list(value: Any, *, max_items: int) -> list[str]:
    """Normalize unknown input into a compact list of strings."""

    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [item for item in value if isinstance(item, str)]
    else:
        values = []

    output: list[str] = []
    for item in values:
        text = " ".join(item.split()).strip()
        if not text:
            continue
        output.append(text)
        if len(output) >= max(1, max_items):
            break
    return output


def _normalize_brief_text(
    *,
    text: str | None,
    min_sentences: int,
    max_sentences: int,
    fallback_text: str,
) -> str:
    """Clamp brief output to configured sentence bounds."""

    if not isinstance(text, str) or not text.strip():
        return fallback_text
    compact = " ".join(text.split()).strip()
    parts = [item.strip() for item in re.split(r"(?<=[.!?])\s+", compact) if item.strip()]
    if len(parts) < min_sentences:
        return fallback_text
    if len(parts) > max_sentences:
        return " ".join(parts[:max_sentences]).strip()
    return compact


def _extract_model_text(response: dict[str, Any]) -> str:
    """Extract assistant text from Bedrock Anthropic response shape."""

    content = response.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            text_value = item.get("text")
            if isinstance(text_value, str):
                parts.append(text_value)
        joined = "\n".join(parts).strip()
        if joined:
            return joined

    output_text = response.get("outputText")
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    raise RuntimeError("LLM response did not contain text output")


def _extract_first_json_object(text_value: str) -> dict[str, Any]:
    """Return first JSON object found in model output text."""

    decoder = json.JSONDecoder()
    for index, char in enumerate(text_value):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text_value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("LLM output is not strict JSON")


def _build_discrepancies(
    *,
    missing_employers: list[str],
    missing_skills: list[str],
    github_repo_count: int,
    portfolio_hit_count: int,
    max_items: int,
) -> list[str]:
    """Build prioritized discrepancy list."""

    discrepancies: list[str] = []
    if missing_employers:
        sample = ", ".join(missing_employers[:3])
        discrepancies.append(
            "Resume employers not clearly found on LinkedIn snippets: " f"{sample}."
        )
    if missing_skills:
        sample = ", ".join(missing_skills[:5])
        discrepancies.append(
            f"Resume skills not clearly found on public profile snippets: {sample}."
        )
    if github_repo_count == 0:
        discrepancies.append("No public repositories found for supplied GitHub profile.")
    if portfolio_hit_count == 0:
        discrepancies.append("No relevant public search hits found for supplied portfolio URL.")
    return discrepancies[:max_items]


def _build_candidate_brief(
    *,
    full_name: str,
    role_selection: str,
    years_experience: float | None,
    matched_employers: list[str],
    github_repo_count: int,
    github_top_repos: list[str],
    twitter_hit_count: int,
    portfolio_hit_count: int,
    discrepancies: list[str],
    min_sentences: int,
    max_sentences: int,
) -> str:
    """Build a compact 3-5 sentence hiring brief."""

    sentences: list[str] = []
    years_text = (
        f"{years_experience:.2f}" if isinstance(years_experience, (int, float)) else "unknown"
    )
    sentences.append(
        f"{full_name} is shortlisted for {role_selection} with parsed experience "
        f"around {years_text} years."
    )
    if matched_employers:
        employers_text = ", ".join(matched_employers[:3])
        sentences.append(
            f"LinkedIn snippets align with prior employers including {employers_text}."
        )
    else:
        sentences.append("LinkedIn snippet alignment with resume employers is limited.")

    if github_repo_count > 0:
        repo_text = ", ".join(github_top_repos[:3]) if github_top_repos else "recent repositories"
        sentences.append(
            f"GitHub profile shows {github_repo_count} public repositories "
            f"with notable work in {repo_text}."
        )
    else:
        sentences.append("GitHub public repository signal is currently limited.")

    sentences.append(
        f"Public web signals include {twitter_hit_count} relevant X/Twitter hits "
        f"and {portfolio_hit_count} portfolio hits."
    )

    if discrepancies:
        sentences.append(f"Potential discrepancy flagged: {discrepancies[0]}")

    if len(sentences) < min_sentences:
        sentences.append("Further manual review is recommended before interview scheduling.")
    return " ".join(sentences[:max_sentences])


class GithubApiClient:
    """Minimal async GitHub REST API client using urllib in worker threads."""

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str,
        timeout_seconds: float,
        user_agent: str,
        max_concurrency: int,
    ) -> None:
        """Initialize client."""

        self._token = token
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._user_agent = user_agent
        self._semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def get_json(self, path: str, params: dict[str, str] | None = None) -> Any:
        """GET one GitHub API endpoint and return parsed JSON."""

        async with self._semaphore:
            return await asyncio.to_thread(self._get_json_sync, path, params or {})

    def _get_json_sync(self, path: str, params: dict[str, str]) -> Any:
        """Blocking GitHub request."""

        query = urlencode(params)
        url = f"{self._api_base_url}{path}"
        if query:
            url = f"{url}?{query}"
        request = Request(
            url=url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": self._user_agent,
            },
        )
        with urlopen(request, timeout=self._timeout_seconds) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        return payload


async def _search_linkedin_profile(
    *,
    serp_client: SerpApiClient,
    config: ResearchRuntimeConfig,
    candidate: CandidateSeed,
) -> tuple[str | None, list[dict[str, str]], str]:
    """Search LinkedIn profile hits and return selected URL + hits + corpus."""

    query = config.linkedin_query_template.format(
        full_name=candidate.full_name,
        role_selection=candidate.role_selection,
    )
    payload = await serp_client.search(
        query=query,
        num_results=config.results_per_query,
    )
    hits = _extract_hits(payload, max_hits=config.enrichment.max_profile_hits)
    linkedin_hits = [hit for hit in hits if _domain_match(hit.get("link", ""), ("linkedin.com",))]
    selected_url = candidate.linkedin_url or _pick_first_url_by_domain(
        linkedin_hits,
        ("linkedin.com",),
    )
    corpus = _normalize_text(
        " ".join(f"{hit.get('title', '')} {hit.get('snippet', '')}" for hit in linkedin_hits)
    )
    return selected_url, linkedin_hits, corpus


async def _search_twitter_profile_and_posts(
    *,
    serp_client: SerpApiClient,
    config: ResearchRuntimeConfig,
    candidate: CandidateSeed,
) -> tuple[str | None, list[dict[str, str]]]:
    """Search Twitter/X and return selected profile URL + relevant hits."""

    query = config.twitter_query_template.format(
        full_name=candidate.full_name,
        role_selection=candidate.role_selection,
    )
    payload = await serp_client.search(query=query, num_results=config.results_per_query)
    hits = _extract_hits(payload, max_hits=config.enrichment.max_twitter_hits)
    twitter_hits = [
        hit for hit in hits if _domain_match(hit.get("link", ""), ("x.com", "twitter.com"))
    ]
    selected_url = candidate.twitter_url or _pick_first_url_by_domain(
        twitter_hits, ("x.com", "twitter.com")
    )
    return selected_url, twitter_hits


async def _search_portfolio(
    *,
    serp_client: SerpApiClient,
    config: ResearchRuntimeConfig,
    candidate: CandidateSeed,
) -> list[dict[str, str]]:
    """Search portfolio domain and return top hits."""

    if not candidate.portfolio_url:
        return []
    domain = (urlparse(candidate.portfolio_url).hostname or "").casefold().lstrip("www.")
    if not domain:
        return []
    query = f'site:{domain} "{candidate.full_name}" "{candidate.role_selection}"'
    payload = await serp_client.search(query=query, num_results=config.results_per_query)
    hits = _extract_hits(payload, max_hits=config.enrichment.max_portfolio_hits)
    return hits


def _build_github_repo_info(
    *,
    repos_payload: list[dict[str, Any]],
    config: ResearchRuntimeConfig,
) -> list[dict[str, Any]]:
    """Normalize GitHub repository payload to requested repo-level fields."""

    repo_info: list[dict[str, Any]] = []
    for repo in repos_payload:
        topics_raw = repo.get("topics")
        topics: list[str] = []
        if isinstance(topics_raw, list):
            topics = [
                str(item).strip()
                for item in topics_raw[: config.github.max_topics_per_repo]
                if isinstance(item, str) and item.strip()
            ]

        description = repo.get("description")
        description_text = description.strip() if isinstance(description, str) else None
        max_desc = max(20, config.github.max_repo_description_chars)
        if isinstance(description_text, str) and len(description_text) > max_desc:
            description_text = description_text[: max_desc - 3].rstrip() + "..."

        repo_info.append(
            {
                "name": repo.get("name"),
                "description": description_text,
                "stars": _safe_int(repo.get("stargazers_count")),
                "forks": _safe_int(repo.get("forks_count")),
                "language": repo.get("language"),
                "updated_at": repo.get("updated_at"),
                "topics": topics,
                "html_url": repo.get("html_url"),
            }
        )
        if len(repo_info) >= max(1, config.github.max_repo_items):
            break
    return repo_info


def _derive_primary_languages(
    *,
    repo_info: list[dict[str, Any]],
    max_items: int,
) -> list[str]:
    """Derive primary language stack from repository language frequency."""

    counts: Counter[str] = Counter()
    for repo in repo_info:
        language = repo.get("language")
        if isinstance(language, str) and language.strip():
            counts[language.strip()] += 1

    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold()))
    return [item[0] for item in ranked[: max(1, max_items)]]


def _derive_activity_status(
    *,
    repo_info: list[dict[str, Any]],
    active_within_days: int,
) -> str:
    """Classify GitHub profile as active/inactive from latest repo update timestamp."""

    timestamps = [
        parsed
        for parsed in (
            _parse_utc_datetime(repo.get("updated_at")) if isinstance(repo, dict) else None
            for repo in repo_info
        )
        if parsed is not None
    ]
    if not timestamps:
        return "inactive"
    latest_update = max(timestamps)
    threshold = datetime.now(tz=timezone.utc) - timedelta(days=max(1, active_within_days))
    return "active" if latest_update >= threshold else "inactive"


def _rank_top_projects(
    *,
    repo_info: list[dict[str, Any]],
    max_items: int,
) -> list[dict[str, Any]]:
    """Rank repositories by stars/forks/update recency."""

    ranked = sorted(
        repo_info,
        key=lambda repo: (
            _safe_int(repo.get("stars")),
            _safe_int(repo.get("forks")),
            str(repo.get("updated_at") or ""),
        ),
        reverse=True,
    )
    output: list[dict[str, Any]] = []
    for repo in ranked[: max(1, max_items)]:
        output.append(
            {
                "name": repo.get("name"),
                "stars": _safe_int(repo.get("stars")),
                "forks": _safe_int(repo.get("forks")),
                "language": repo.get("language"),
                "updated_at": repo.get("updated_at"),
                "html_url": repo.get("html_url"),
            }
        )
    return output


def _build_github_enrichment_payload(
    *,
    github_url: str,
    username: str | None,
    user_payload: dict[str, Any] | None,
    repos_payload: list[dict[str, Any]],
    config: ResearchRuntimeConfig,
) -> dict[str, Any]:
    """Build GitHub payload with profile info, per-repo info, and derived summary."""

    profile_info = {
        "username": username,
        "bio": user_payload.get("bio") if isinstance(user_payload, dict) else None,
        "public_repos": (
            _safe_int(user_payload.get("public_repos")) if isinstance(user_payload, dict) else 0
        ),
        "followers": (
            _safe_int(user_payload.get("followers")) if isinstance(user_payload, dict) else 0
        ),
    }
    repo_info = _build_github_repo_info(repos_payload=repos_payload, config=config)

    topics: set[str] = set()
    for repo in repo_info:
        repo_topics = repo.get("topics")
        if not isinstance(repo_topics, list):
            continue
        for topic in repo_topics:
            if isinstance(topic, str) and topic.strip():
                topics.add(topic.strip())

    top_projects = _rank_top_projects(repo_info=repo_info, max_items=3)
    primary_languages = _derive_primary_languages(
        repo_info=repo_info,
        max_items=config.github.max_primary_languages,
    )
    activity_status = _derive_activity_status(
        repo_info=repo_info,
        active_within_days=config.github.activity_active_within_days,
    )

    # Keep legacy aliases for backward compatibility in downstream consumers.
    return {
        "profile_url": github_url,
        "profile_info": profile_info,
        "repo_info": repo_info,
        "final_derived": {
            "top_3_projects": top_projects,
            "primary_languages": primary_languages,
            "activity_status": activity_status,
        },
        "username": profile_info["username"],
        "public_repos_count": profile_info["public_repos"],
        "top_repositories": top_projects[: config.github.max_repos_in_summary],
        "languages": primary_languages,
        "topics": sorted(topics),
    }


async def _fetch_github_repos(
    *,
    github_client: GithubApiClient,
    username: str,
    max_repo_items: int,
) -> list[dict[str, Any]]:
    """Fetch one or more pages of repositories up to configured item cap."""

    page_size = min(100, max(1, max_repo_items))
    repos: list[dict[str, Any]] = []
    page = 1

    while len(repos) < max_repo_items:
        payload = await github_client.get_json(
            f"/users/{username}/repos",
            params={
                "sort": "updated",
                "per_page": str(page_size),
                "page": str(page),
                "type": "owner",
            },
        )
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if isinstance(item, dict):
                repos.append(item)
            if len(repos) >= max_repo_items:
                break
        if len(payload) < page_size:
            break
        page += 1

    return repos[:max_repo_items]


async def _enrich_github(
    *,
    github_client: GithubApiClient | None,
    config: ResearchRuntimeConfig,
    github_url: str,
) -> dict[str, Any]:
    """Fetch GitHub profile + repos and return structured extraction JSON."""

    username = _parse_github_username(github_url)
    if not username or github_client is None:
        return _build_github_enrichment_payload(
            github_url=github_url,
            username=username,
            user_payload=None,
            repos_payload=[],
            config=config,
        )

    try:
        user_payload = await github_client.get_json(f"/users/{username}")
        repos_payload = await _fetch_github_repos(
            github_client=github_client,
            username=username,
            max_repo_items=max(1, config.github.max_repo_items),
        )
    except Exception:
        logger.exception("github enrichment failed for username=%s", username)
        return _build_github_enrichment_payload(
            github_url=github_url,
            username=username,
            user_payload=None,
            repos_payload=[],
            config=config,
        )

    return _build_github_enrichment_payload(
        github_url=github_url,
        username=username,
        user_payload=user_payload if isinstance(user_payload, dict) else None,
        repos_payload=repos_payload,
        config=config,
    )


def _build_llm_resume_snapshot(parse_result: dict[str, Any] | None) -> dict[str, Any]:
    """Build compact resume snapshot JSON for LLM cross-reference."""

    if not isinstance(parse_result, dict):
        return {}

    work_experience = parse_result.get("work_experience")
    normalized_work: list[dict[str, Any]] = []
    if isinstance(work_experience, list):
        for item in work_experience[:10]:
            if not isinstance(item, dict):
                continue
            normalized_work.append(
                {
                    "position": item.get("position"),
                    "company": item.get("company"),
                    "start_date": item.get("start_date"),
                    "end_date": item.get("end_date"),
                    "duration_years": item.get("duration_years"),
                }
            )

    return {
        "skills": parse_result.get("skills"),
        "total_years_experience": parse_result.get("total_years_experience"),
        "work_experience": normalized_work,
        "education": parse_result.get("education"),
    }


def _build_llm_prompt(
    *,
    candidate: CandidateSeed,
    config: ResearchRuntimeConfig,
    resume_snapshot: dict[str, Any],
    extracted_payload: dict[str, Any],
) -> str:
    """Render one-shot enrichment analysis prompt for the primary model."""

    template = config.enrichment.llm_prompt_template.strip()
    if not template:
        raise RuntimeError("research.enrichment.llm_prompt_template is empty")

    replacements = {
        "{candidate_name}": candidate.full_name,
        "{role_selection}": candidate.role_selection,
        "{resume_json}": json.dumps(resume_snapshot, ensure_ascii=True),
        "{extracted_json}": json.dumps(extracted_payload, ensure_ascii=True),
    }

    prompt = template
    for key, value in replacements.items():
        prompt = prompt.replace(key, value)
    return prompt


def _build_fallback_analysis(
    *,
    matched_employers: list[str],
    matched_skills: list[str],
    github_top_repo_names: list[str],
    discrepancies: list[str],
    brief: str,
) -> dict[str, Any]:
    """Build deterministic fallback analysis when LLM is unavailable."""

    employment_alignment = (
        [f"Matched employers in public signals: {', '.join(matched_employers[:3])}."]
        if matched_employers
        else ["Employment alignment is limited from publicly available snippets."]
    )
    skills_alignment = (
        [f"Matched resume skills on public profiles: {', '.join(matched_skills[:5])}."]
        if matched_skills
        else ["Skills alignment is limited from publicly available snippets."]
    )
    project_alignment = (
        [f"Top project signals from GitHub: {', '.join(github_top_repo_names[:3])}."]
        if github_top_repo_names
        else ["Project signal from GitHub is limited."]
    )

    return {
        "source": "heuristic_fallback",
        "cross_reference": {
            "employment_alignment": employment_alignment,
            "skills_alignment": skills_alignment,
            "project_alignment": project_alignment,
        },
        "discrepancies": discrepancies,
        "summary": brief,
    }


def _normalize_llm_analysis_payload(
    *,
    parsed_payload: dict[str, Any],
    config: ResearchRuntimeConfig,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Normalize model JSON payload to strict expected analysis fields."""

    cross_reference_raw = parsed_payload.get("cross_reference")
    cross_reference = cross_reference_raw if isinstance(cross_reference_raw, dict) else {}

    employment_alignment = _coerce_text_list(
        cross_reference.get("employment_alignment") or cross_reference.get("employment"),
        max_items=6,
    )
    skills_alignment = _coerce_text_list(
        cross_reference.get("skills_alignment") or cross_reference.get("skills"),
        max_items=8,
    )
    project_alignment = _coerce_text_list(
        cross_reference.get("project_alignment") or cross_reference.get("projects"),
        max_items=6,
    )
    discrepancies = _coerce_text_list(
        parsed_payload.get("discrepancies"),
        max_items=config.enrichment.max_discrepancies,
    )

    fallback_cross_reference = fallback.get("cross_reference", {})
    fallback_summary = str(fallback.get("summary", "")).strip()
    summary = _normalize_brief_text(
        text=(
            parsed_payload.get("summary")
            if isinstance(parsed_payload.get("summary"), str)
            else None
        ),
        min_sentences=config.enrichment.min_brief_sentences,
        max_sentences=config.enrichment.max_brief_sentences,
        fallback_text=fallback_summary,
    )

    return {
        "source": "primary_model",
        "cross_reference": {
            "employment_alignment": employment_alignment
            or _coerce_text_list(fallback_cross_reference.get("employment_alignment"), max_items=6),
            "skills_alignment": skills_alignment
            or _coerce_text_list(fallback_cross_reference.get("skills_alignment"), max_items=8),
            "project_alignment": project_alignment
            or _coerce_text_list(fallback_cross_reference.get("project_alignment"), max_items=6),
        },
        "discrepancies": discrepancies
        or _coerce_text_list(
            fallback.get("discrepancies"),
            max_items=config.enrichment.max_discrepancies,
        ),
        "summary": summary,
    }


async def _analyze_candidate_with_primary_model_once(
    *,
    candidate: CandidateSeed,
    config: ResearchRuntimeConfig,
    extracted_payload: dict[str, Any],
    bedrock_client: BedrockRuntimeClient | None,
    bedrock_config: BedrockRuntimeConfig,
    fallback: dict[str, Any],
) -> dict[str, Any]:
    """Run one LLM call for cross-reference + discrepancies + concise summary."""

    if not config.enrichment.llm_analysis_enabled or bedrock_client is None:
        return fallback

    prompt = _build_llm_prompt(
        candidate=candidate,
        config=config,
        resume_snapshot=_build_llm_resume_snapshot(candidate.parse_result),
        extracted_payload=extracted_payload,
    )
    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": min(bedrock_config.max_tokens, config.enrichment.llm_max_tokens),
        "temperature": 0.0,
        "top_p": bedrock_config.top_p,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
    }

    try:
        response = await asyncio.wait_for(
            bedrock_client.invoke_json(
                model_id=bedrock_config.primary_model_id,
                payload=payload,
            ),
            timeout=bedrock_config.request_timeout_seconds,
        )
        response_text = _extract_model_text(response)
        parsed_payload = _extract_first_json_object(response_text)
        normalized = _normalize_llm_analysis_payload(
            parsed_payload=parsed_payload,
            config=config,
            fallback=fallback,
        )
        normalized["model_id"] = bedrock_config.primary_model_id
        return normalized
    except (TimeoutError, BedrockInvocationError, RuntimeError, Exception):
        logger.exception("primary-model enrichment analysis failed candidate=%s", candidate.id)
        return fallback


async def _enrich_candidate(
    *,
    candidate: CandidateSeed,
    config: ResearchRuntimeConfig,
    serp_client: SerpApiClient,
    github_client: GithubApiClient | None,
    bedrock_client: BedrockRuntimeClient | None,
    bedrock_config: BedrockRuntimeConfig,
) -> dict[str, Any]:
    """Run full enrichment workflow for one candidate."""

    resume_skills, resume_employers = _extract_resume_skills_and_employers(candidate.parse_result)
    years_experience = None
    if isinstance(candidate.parse_result, dict):
        raw_years = candidate.parse_result.get("total_years_experience")
        if isinstance(raw_years, (int, float)):
            years_experience = float(raw_years)

    linkedin_url, linkedin_hits, linkedin_corpus = await _search_linkedin_profile(
        serp_client=serp_client,
        config=config,
        candidate=candidate,
    )
    twitter_url, twitter_hits = await _search_twitter_profile_and_posts(
        serp_client=serp_client,
        config=config,
        candidate=candidate,
    )
    portfolio_hits = await _search_portfolio(
        serp_client=serp_client,
        config=config,
        candidate=candidate,
    )
    github_data = await _enrich_github(
        github_client=github_client,
        config=config,
        github_url=candidate.github_url,
    )

    matched_employers = [
        item for item in resume_employers if _normalize_text(item) in linkedin_corpus
    ]
    missing_employers = [item for item in resume_employers if item not in matched_employers]
    matched_skills = [item for item in resume_skills if _normalize_text(item) in linkedin_corpus]
    missing_skills = [item for item in resume_skills if item not in matched_skills]
    github_profile_info = github_data.get("profile_info")
    github_repo_count = (
        _safe_int(github_profile_info.get("public_repos"))
        if isinstance(github_profile_info, dict)
        else _safe_int(github_data.get("public_repos_count"))
    )

    fallback_discrepancies = _build_discrepancies(
        missing_employers=missing_employers,
        missing_skills=missing_skills,
        github_repo_count=github_repo_count,
        portfolio_hit_count=len(portfolio_hits),
        max_items=config.enrichment.max_discrepancies,
    )
    github_top_projects = []
    github_final_derived = github_data.get("final_derived")
    if isinstance(github_final_derived, dict):
        github_top_projects = github_final_derived.get("top_3_projects", [])
    github_top_repo_names = [
        str(item.get("name"))
        for item in github_top_projects
        if isinstance(item, dict) and item.get("name")
    ]
    fallback_brief = _build_candidate_brief(
        full_name=candidate.full_name,
        role_selection=candidate.role_selection,
        years_experience=years_experience,
        matched_employers=matched_employers,
        github_repo_count=github_repo_count,
        github_top_repos=github_top_repo_names,
        twitter_hit_count=len(twitter_hits),
        portfolio_hit_count=len(portfolio_hits),
        discrepancies=fallback_discrepancies,
        min_sentences=config.enrichment.min_brief_sentences,
        max_sentences=config.enrichment.max_brief_sentences,
    )

    extracted_payload = {
        "linkedin": {
            "profile_url": linkedin_url,
            "hits": linkedin_hits,
            "matched_employers": matched_employers,
            "matched_skills": matched_skills,
        },
        "twitter": {
            "profile_url": twitter_url,
            "hits": twitter_hits,
        },
        "github": github_data,
        "portfolio": {
            "url": candidate.portfolio_url,
            "hits": portfolio_hits,
        },
    }
    fallback_analysis = _build_fallback_analysis(
        matched_employers=matched_employers,
        matched_skills=matched_skills,
        github_top_repo_names=github_top_repo_names,
        discrepancies=fallback_discrepancies,
        brief=fallback_brief,
    )
    llm_analysis = await _analyze_candidate_with_primary_model_once(
        candidate=candidate,
        config=config,
        extracted_payload=extracted_payload,
        bedrock_client=bedrock_client,
        bedrock_config=bedrock_config,
        fallback=fallback_analysis,
    )
    discrepancies = (
        _coerce_text_list(
            llm_analysis.get("discrepancies"),
            max_items=config.enrichment.max_discrepancies,
        )
        or fallback_discrepancies
    )
    brief = _normalize_brief_text(
        text=llm_analysis.get("summary") if isinstance(llm_analysis.get("summary"), str) else None,
        min_sentences=config.enrichment.min_brief_sentences,
        max_sentences=config.enrichment.max_brief_sentences,
        fallback_text=fallback_brief,
    )
    llm_analysis["discrepancies"] = discrepancies
    llm_analysis["summary"] = brief

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate_id": str(candidate.id),
        "candidate_name": candidate.full_name,
        "role_selection": candidate.role_selection,
        "linkedin": extracted_payload["linkedin"],
        "twitter": extracted_payload["twitter"],
        "github": github_data,
        "portfolio": extracted_payload["portfolio"],
        "llm_analysis": llm_analysis,
        "discrepancies": discrepancies,
        "brief": brief,
    }


async def _load_candidates(
    *,
    config: ResearchRuntimeConfig,
    offset: int,
    limit: int,
    application_ids: list[UUID] | None,
) -> list[CandidateSeed]:
    """Load target candidate rows from DB."""

    runtime_config = get_runtime_config()
    session_factory = get_async_session_factory(runtime_config.postgres)
    async with session_factory() as session:
        statement = select(
            ApplicantApplication.id,
            ApplicantApplication.full_name,
            ApplicantApplication.role_selection,
            ApplicantApplication.linkedin_url,
            ApplicantApplication.twitter_url,
            ApplicantApplication.github_url,
            ApplicantApplication.portfolio_url,
            ApplicantApplication.parse_result,
        ).order_by(ApplicantApplication.created_at.desc())
        if application_ids:
            statement = statement.where(ApplicantApplication.id.in_(application_ids))
        else:
            statement = statement.where(
                ApplicantApplication.applicant_status.in_(config.enrichment.target_statuses)
            )
            statement = statement.offset(max(0, offset)).limit(
                max(1, min(limit, config.enrichment.max_candidates_per_run))
            )

        rows = (await session.execute(statement)).all()
        return [CandidateSeed(*row) for row in rows]


def _clip_text(value: Any, *, max_chars: int) -> Any:
    """Clip long string values to bound serialized payload size."""

    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


def _trim_hits(block: dict[str, Any], *, keep_items: int, max_text_chars: int) -> None:
    """Trim search hits list in-place to fit DB storage constraints."""

    hits = block.get("hits")
    if not isinstance(hits, list):
        return
    trimmed_hits: list[dict[str, Any]] = []
    for item in hits[: max(0, keep_items)]:
        if not isinstance(item, dict):
            continue
        trimmed_hits.append(
            {
                "link": item.get("link"),
                "title": _clip_text(item.get("title"), max_chars=max_text_chars),
                "snippet": _clip_text(item.get("snippet"), max_chars=max_text_chars),
            }
        )
    block["hits"] = trimmed_hits


def _trim_github(github_block: dict[str, Any], *, keep_repos: int, max_text_chars: int) -> None:
    """Trim GitHub repo payload in-place to fit DB storage constraints."""

    repo_info = github_block.get("repo_info")
    if isinstance(repo_info, list):
        trimmed_repos: list[dict[str, Any]] = []
        for repo in repo_info[: max(0, keep_repos)]:
            if not isinstance(repo, dict):
                continue
            topics = repo.get("topics")
            trimmed_repos.append(
                {
                    "name": repo.get("name"),
                    "description": _clip_text(repo.get("description"), max_chars=max_text_chars),
                    "stars": _safe_int(repo.get("stars")),
                    "forks": _safe_int(repo.get("forks")),
                    "language": repo.get("language"),
                    "updated_at": repo.get("updated_at"),
                    "topics": topics[:4] if isinstance(topics, list) else [],
                    "html_url": repo.get("html_url"),
                }
            )
        github_block["repo_info"] = trimmed_repos

    final_derived = github_block.get("final_derived")
    if isinstance(final_derived, dict):
        top_projects = final_derived.get("top_3_projects")
        if isinstance(top_projects, list):
            final_derived["top_3_projects"] = top_projects[:3]


def _build_minimal_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Build guaranteed-small JSON payload fallback for DB column limit."""

    linkedin = payload.get("linkedin") if isinstance(payload.get("linkedin"), dict) else {}
    twitter = payload.get("twitter") if isinstance(payload.get("twitter"), dict) else {}
    github = payload.get("github") if isinstance(payload.get("github"), dict) else {}
    github_profile = (
        github.get("profile_info")
        if isinstance(github.get("profile_info"), dict)
        else {"username": github.get("username")}
    )
    return {
        "generated_at": payload.get("generated_at"),
        "candidate_id": payload.get("candidate_id"),
        "candidate_name": payload.get("candidate_name"),
        "role_selection": payload.get("role_selection"),
        "linkedin_profile_url": linkedin.get("profile_url"),
        "twitter_profile_url": twitter.get("profile_url"),
        "github_profile_info": github_profile,
        "discrepancies": _coerce_text_list(payload.get("discrepancies"), max_items=5),
        "brief": _clip_text(payload.get("brief"), max_chars=500),
    }


def _serialize_payload_with_limit(payload: dict[str, Any], *, max_chars: int) -> str:
    """Serialize payload without corrupting JSON when trimming for DB size."""

    candidate = copy.deepcopy(payload)
    serialized = json.dumps(candidate, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized

    for keep_hits, keep_repos, max_text in [(4, 10, 180), (2, 5, 120), (1, 3, 80)]:
        linkedin = candidate.get("linkedin")
        twitter = candidate.get("twitter")
        portfolio = candidate.get("portfolio")
        github = candidate.get("github")
        llm_analysis = candidate.get("llm_analysis")
        if isinstance(linkedin, dict):
            _trim_hits(linkedin, keep_items=keep_hits, max_text_chars=max_text)
        if isinstance(twitter, dict):
            _trim_hits(twitter, keep_items=keep_hits, max_text_chars=max_text)
        if isinstance(portfolio, dict):
            _trim_hits(portfolio, keep_items=keep_hits, max_text_chars=max_text)
        if isinstance(github, dict):
            _trim_github(github, keep_repos=keep_repos, max_text_chars=max_text)
        if isinstance(llm_analysis, dict):
            llm_analysis["summary"] = _clip_text(llm_analysis.get("summary"), max_chars=500)
            llm_analysis["discrepancies"] = _coerce_text_list(
                llm_analysis.get("discrepancies"),
                max_items=6,
            )
        candidate["brief"] = _clip_text(candidate.get("brief"), max_chars=500)
        serialized = json.dumps(candidate, ensure_ascii=False)
        if len(serialized) <= max_chars:
            return serialized

    minimal = _build_minimal_payload(candidate)
    serialized = json.dumps(minimal, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized

    # Last-resort guaranteed valid JSON fallback.
    return json.dumps(
        {
            "candidate_id": payload.get("candidate_id"),
            "brief": _clip_text(payload.get("brief"), max_chars=200),
        },
        ensure_ascii=False,
    )


async def _persist_enrichment(
    *,
    candidate_id: UUID,
    payload: dict[str, Any],
    linkedin_url: str | None,
    twitter_url: str | None,
    max_chars: int,
) -> None:
    """Persist enrichment JSON into online_research_summary and update missing links."""

    runtime_config = get_runtime_config()
    session_factory = get_async_session_factory(runtime_config.postgres)
    serialized = _serialize_payload_with_limit(payload, max_chars=max_chars)
    async with session_factory() as session:
        entity = await session.get(ApplicantApplication, candidate_id)
        if entity is None:
            return
        if not entity.linkedin_url and linkedin_url:
            entity.linkedin_url = linkedin_url
        if not entity.twitter_url and twitter_url:
            entity.twitter_url = twitter_url
        entity.online_research_summary = serialized
        await session.commit()


async def _run(
    *,
    offset: int,
    limit: int,
    dry_run: bool,
    application_ids: list[UUID] | None,
) -> None:
    """Run shortlisted-candidate enrichment batch."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    config = runtime_config.research
    if not config.enabled:
        raise RuntimeError("research.enabled=false")
    if not settings.serpapi_api_key:
        raise RuntimeError("SERPAPI_API_KEY is required in .env")

    serp_client = SerpApiClient(
        api_key=settings.serpapi_api_key,
        endpoint=config.google_search_url,
        engine=config.engine,
        timeout_seconds=config.request_timeout_seconds,
        max_concurrency=config.max_concurrency,
    )
    github_client = None
    if settings.github_api_token:
        github_client = GithubApiClient(
            token=settings.github_api_token,
            api_base_url=config.github.api_base_url,
            timeout_seconds=config.github.request_timeout_seconds,
            user_agent=config.github.user_agent,
            max_concurrency=config.max_concurrency,
        )
    else:
        logger.warning("GITHUB_API_TOKEN missing; GitHub enrichment will be limited")

    bedrock_config = runtime_config.bedrock
    bedrock_client = None
    if config.enrichment.llm_analysis_enabled:
        if not bedrock_config.enabled:
            logger.warning(
                "Bedrock disabled; shortlisted enrichment will use deterministic fallback"
            )
        else:
            bedrock_client = BedrockRuntimeClient(
                region=bedrock_config.region,
                max_retries=bedrock_config.max_retries,
                aws_access_key_id=settings.aws_access_key_id,
                aws_secret_access_key=settings.aws_secret_access_key,
                aws_session_token=settings.aws_session_token,
                endpoint_url=settings.bedrock_endpoint_url,
            )

    candidates = await _load_candidates(
        config=config,
        offset=offset,
        limit=limit,
        application_ids=application_ids,
    )
    logger.info("shortlisted enrichment candidates=%s", len(candidates))
    if not candidates:
        return

    semaphore = asyncio.Semaphore(max(1, config.max_concurrency))

    async def process(candidate: CandidateSeed) -> None:
        async with semaphore:
            try:
                enriched = await _enrich_candidate(
                    candidate=candidate,
                    config=config,
                    serp_client=serp_client,
                    github_client=github_client,
                    bedrock_client=bedrock_client,
                    bedrock_config=bedrock_config,
                )
                if dry_run:
                    logger.info(
                        "dry-run candidate=%s brief=%s",
                        candidate.id,
                        enriched.get("brief", ""),
                    )
                    return

                linkedin_profile = None
                twitter_profile = None
                linkedin_block = enriched.get("linkedin")
                twitter_block = enriched.get("twitter")
                if isinstance(linkedin_block, dict):
                    linkedin_profile = linkedin_block.get("profile_url")
                if isinstance(twitter_block, dict):
                    twitter_profile = twitter_block.get("profile_url")
                await _persist_enrichment(
                    candidate_id=candidate.id,
                    payload=enriched,
                    linkedin_url=linkedin_profile if isinstance(linkedin_profile, str) else None,
                    twitter_url=twitter_profile if isinstance(twitter_profile, str) else None,
                    max_chars=config.enrichment.max_research_json_chars,
                )
                logger.info("enriched candidate=%s", candidate.id)
            except Exception:
                logger.exception("failed enriching candidate=%s", candidate.id)

    await asyncio.gather(*(process(candidate) for candidate in candidates))


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(description="Run shortlisted candidate research enrichment.")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--application-id",
        action="append",
        default=None,
        help="Specific candidate UUID to enrich (repeatable).",
    )
    return parser


def main() -> None:
    """Entrypoint for `python -m app.scripts.enrich_shortlisted_candidates`."""

    parser = _build_parser()
    args = parser.parse_args()
    application_ids = [UUID(item) for item in args.application_id] if args.application_id else None
    asyncio.run(
        _run(
            offset=max(0, args.offset),
            limit=max(1, args.limit),
            dry_run=bool(args.dry_run),
            application_ids=application_ids,
        )
    )


if __name__ == "__main__":
    main()
