"""Shortlisted-candidate research enrichment with LLM primary/fallback synthesis."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select

from app.core.runtime_config import BedrockRuntimeConfig
from app.core.runtime_config import ResearchRuntimeConfig
from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.bedrock_runtime import BedrockInvocationError
from app.infra.bedrock_runtime import BedrockRuntimeClient
from app.infra.database import get_async_session_factory
from app.model.applicant_application import ApplicantApplication
from app.scripts.extract_github_profile import _run as _run_github_extractor
from app.scripts.extract_linkedin_profile import _run as _run_linkedin_extractor
from app.scripts.extract_portfolio_profile import _run as _run_portfolio_extractor
from app.scripts.extract_twitter_profile import _run as _run_twitter_extractor

logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)


@dataclass(slots=True)
class CandidateSeed:
    """Candidate fields required for enrichment."""

    id: UUID
    full_name: str
    role_selection: str
    applicant_status: str
    linkedin_url: str | None
    twitter_url: str | None
    github_url: str | None
    portfolio_url: str | None
    parse_result: dict[str, Any] | None


def _normalize_text(value: str) -> str:
    """Normalize value for case-insensitive matching."""

    return re.sub(r"\s+", " ", value.strip().casefold())


def _coerce_text_list(value: Any, *, max_items: int) -> list[str]:
    """Normalize unknown value to compact string list."""

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


def _tokenize(value: str, *, min_len: int = 3) -> list[str]:
    """Extract searchable tokens from text."""

    return re.findall(rf"[a-z0-9\+#\.]{{{max(1, min_len)},}}", value.casefold())


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


def _clip_text(value: Any, *, max_chars: int) -> Any:
    """Clip long string values with ellipsis."""

    if not isinstance(value, str):
        return value
    text = value.strip()
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3].rstrip() + "..."


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


def _build_resume_snapshot(parse_result: dict[str, Any] | None) -> dict[str, Any]:
    """Build compact resume snapshot JSON."""

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

    projects = parse_result.get("projects")
    key_achievements = parse_result.get("key_achievements")
    project_values = []
    if isinstance(projects, list):
        project_values.extend(str(item).strip() for item in projects if isinstance(item, str))
    if isinstance(key_achievements, list):
        project_values.extend(str(item).strip() for item in key_achievements if isinstance(item, str))

    return {
        "skills": parse_result.get("skills"),
        "total_years_experience": parse_result.get("total_years_experience"),
        "work_experience": normalized_work,
        "projects": list(dict.fromkeys([item for item in project_values if item]))[:20],
        "education": parse_result.get("education"),
    }


def _resume_signal_lists(parse_result: dict[str, Any] | None) -> dict[str, list[str]]:
    """Extract normalized lists from resume parse_result."""

    snapshot = _build_resume_snapshot(parse_result)
    skills = _coerce_text_list(snapshot.get("skills"), max_items=80)
    work = snapshot.get("work_experience")
    employers: list[str] = []
    positions: list[str] = []
    if isinstance(work, list):
        for item in work:
            if not isinstance(item, dict):
                continue
            company = item.get("company")
            position = item.get("position")
            if isinstance(company, str) and company.strip():
                employers.append(company.strip())
            if isinstance(position, str) and position.strip():
                positions.append(position.strip())
    projects = _coerce_text_list(snapshot.get("projects"), max_items=60)
    return {
        "skills": list(dict.fromkeys(skills)),
        "employers": list(dict.fromkeys(employers)),
        "positions": list(dict.fromkeys(positions)),
        "projects": list(dict.fromkeys(projects)),
    }


def _match_items_against_corpus(items: list[str], corpus: str, *, max_items: int) -> tuple[list[str], list[str]]:
    """Match list values against normalized text corpus."""

    matched: list[str] = []
    unmatched: list[str] = []
    for raw in items:
        value = str(raw).strip()
        if not value:
            continue
        normalized = _normalize_text(value)
        tokens = _tokenize(value, min_len=3)
        token_hits = [token for token in tokens if token in corpus]
        is_match = False
        if normalized and normalized in corpus:
            is_match = True
        elif len(tokens) >= 2 and len(token_hits) >= 2:
            is_match = True
        elif len(tokens) == 1 and len(token_hits) == 1:
            is_match = True

        if is_match:
            matched.append(value)
        else:
            unmatched.append(value)
        if len(matched) >= max_items:
            break
    return matched, unmatched[:max_items]


def _cross_check_resume_vs_linkedin(
    *,
    resume_signals: dict[str, list[str]],
    linkedin_payload: dict[str, Any],
) -> dict[str, Any]:
    """Compare resume signals against LinkedIn extraction."""

    cross = linkedin_payload.get("cross_reference")
    skills_block = cross.get("skills", {}) if isinstance(cross, dict) else {}
    employment_block = cross.get("employment_history", {}) if isinstance(cross, dict) else {}

    matched_skills = _coerce_text_list(skills_block.get("matched_on_linkedin"), max_items=40)
    unmatched_skills = _coerce_text_list(skills_block.get("unmatched_from_resume"), max_items=40)
    matched_employers = _coerce_text_list(
        employment_block.get("matched_employers_on_linkedin"),
        max_items=20,
    )
    unmatched_employers = _coerce_text_list(
        employment_block.get("unmatched_employers_from_resume"),
        max_items=20,
    )
    matched_positions = _coerce_text_list(
        employment_block.get("matched_positions_on_linkedin"),
        max_items=20,
    )
    unmatched_positions = _coerce_text_list(
        employment_block.get("unmatched_positions_from_resume"),
        max_items=20,
    )

    # If LinkedIn extractor could not cross-reference, fallback to resume-side defaults.
    if not matched_skills and not unmatched_skills and resume_signals.get("skills"):
        unmatched_skills = resume_signals["skills"][:40]
    if not matched_employers and not unmatched_employers and resume_signals.get("employers"):
        unmatched_employers = resume_signals["employers"][:20]
    if not matched_positions and not unmatched_positions and resume_signals.get("positions"):
        unmatched_positions = resume_signals["positions"][:20]

    return {
        "matched_skills": matched_skills,
        "unmatched_skills": unmatched_skills,
        "matched_employers": matched_employers,
        "unmatched_employers": unmatched_employers,
        "matched_positions": matched_positions,
        "unmatched_positions": unmatched_positions,
        "experience_mismatch": bool(unmatched_employers or unmatched_positions),
        "skill_differences": bool(unmatched_skills),
    }


def _cross_check_resume_vs_github(
    *,
    resume_signals: dict[str, list[str]],
    github_payload: dict[str, Any],
) -> dict[str, Any]:
    """Compare resume skills/projects against GitHub evidence."""

    top_repositories = github_payload.get("top_repositories")
    repos = top_repositories if isinstance(top_repositories, list) else []

    corpus_parts: list[str] = []
    repo_names: list[str] = []
    for repo in repos:
        if not isinstance(repo, dict):
            continue
        name = repo.get("name")
        language = repo.get("language")
        description = repo.get("description")
        readme_summary = repo.get("readme_summary")
        topics = repo.get("topics")
        if isinstance(name, str) and name.strip():
            repo_names.append(name.strip())
            corpus_parts.append(name)
        if isinstance(language, str) and language.strip():
            corpus_parts.append(language)
        if isinstance(description, str) and description.strip():
            corpus_parts.append(description)
        if isinstance(readme_summary, str) and readme_summary.strip():
            corpus_parts.append(readme_summary)
        if isinstance(topics, list):
            for topic in topics:
                if isinstance(topic, str) and topic.strip():
                    corpus_parts.append(topic)

    aggregate = github_payload.get("aggregate")
    if isinstance(aggregate, dict):
        for item in _coerce_text_list(aggregate.get("top_languages"), max_items=10):
            corpus_parts.append(item)

    corpus = _normalize_text(" ".join(corpus_parts))
    matched_skills, unmatched_skills = _match_items_against_corpus(
        resume_signals.get("skills", []),
        corpus,
        max_items=40,
    )
    matched_projects, missing_projects = _match_items_against_corpus(
        resume_signals.get("projects", []),
        corpus,
        max_items=20,
    )

    return {
        "matched_skills": matched_skills,
        "unmatched_skills": unmatched_skills,
        "matched_projects": matched_projects,
        "missing_projects": missing_projects,
        "top_repo_names": list(dict.fromkeys(repo_names))[:10],
        "skill_differences": bool(unmatched_skills),
        "missing_projects_flag": bool(missing_projects),
    }


def _build_issue_flags(
    *,
    linkedin_check: dict[str, Any],
    github_check: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build structured issue flags from cross-check outputs."""

    flags: list[dict[str, Any]] = []

    if linkedin_check.get("experience_mismatch"):
        flags.append(
            {
                "type": "experience_mismatch",
                "severity": "high",
                "source": "linkedin",
                "details": {
                    "unmatched_employers": linkedin_check.get("unmatched_employers", []),
                    "unmatched_positions": linkedin_check.get("unmatched_positions", []),
                },
            }
        )

    missing_projects = _coerce_text_list(github_check.get("missing_projects"), max_items=20)
    if missing_projects:
        flags.append(
            {
                "type": "missing_projects",
                "severity": "medium",
                "source": "github",
                "details": {
                    "missing_projects": missing_projects,
                },
            }
        )

    skill_differences: list[str] = []
    skill_differences.extend(
        _coerce_text_list(linkedin_check.get("unmatched_skills"), max_items=20)
    )
    skill_differences.extend(
        _coerce_text_list(github_check.get("unmatched_skills"), max_items=20)
    )
    merged_skills = list(dict.fromkeys(skill_differences))
    if merged_skills:
        flags.append(
            {
                "type": "skill_differences",
                "severity": "medium",
                "source": "linkedin_github",
                "details": {
                    "unmatched_skills": merged_skills[:25],
                },
            }
        )

    return flags


def _build_mock_twitter_payload(candidate: CandidateSeed) -> dict[str, Any]:
    """Return deterministic mock payload for Twitter extraction."""

    return {
        "mode": "mock",
        "profile_url": candidate.twitter_url,
        "recent_posts": [],
        "aggregate": {
            "posts_fetched": 0,
            "topic_signals": [],
        },
        "note": "Twitter extraction is mocked to avoid inaccurate user resolution.",
    }


async def _extract_twitter_payload(candidate: CandidateSeed) -> dict[str, Any]:
    """Extract Twitter payload when handle is available; fallback to mock/error payload."""

    if not candidate.twitter_url:
        return {
            "mode": "missing_link",
            "profile_url": None,
            "note": "twitter_url missing in candidate table",
            "recent_posts": [],
            "aggregate": {
                "posts_fetched": 0,
                "topic_signals": [],
            },
        }
    try:
        payload = await _run_twitter_extractor(handle=candidate.twitter_url, max_posts=15)
        if isinstance(payload, dict):
            payload["mode"] = "api"
            return payload
    except Exception as exc:
        logger.warning("twitter extractor failed candidate=%s error=%s", candidate.id, exc)
        return {
            "mode": "error",
            "profile_url": candidate.twitter_url,
            "note": f"twitter extractor failed: {exc}",
            "recent_posts": [],
            "aggregate": {
                "posts_fetched": 0,
                "topic_signals": [],
            },
        }
    return _build_mock_twitter_payload(candidate)


async def _extract_portfolio_payload(candidate: CandidateSeed) -> dict[str, Any]:
    """Extract portfolio payload when URL is available."""

    if not candidate.portfolio_url:
        return {
            "mode": "missing_link",
            "input_portfolio_url": None,
            "note": "portfolio_url missing in candidate table",
            "top_portfolio_hits": [],
            "technology_signals": [],
            "project_signals": [],
        }
    try:
        payload = await _run_portfolio_extractor(
            portfolio_url=candidate.portfolio_url,
            full_name=candidate.full_name,
            role_selection=candidate.role_selection,
        )
        if isinstance(payload, dict):
            payload["mode"] = "serpapi"
            return payload
    except Exception as exc:
        logger.warning("portfolio extractor failed candidate=%s error=%s", candidate.id, exc)
        return {
            "mode": "error",
            "input_portfolio_url": candidate.portfolio_url,
            "note": f"portfolio extractor failed: {exc}",
            "top_portfolio_hits": [],
            "technology_signals": [],
            "project_signals": [],
        }
    return {
        "mode": "empty",
        "input_portfolio_url": candidate.portfolio_url,
        "top_portfolio_hits": [],
        "technology_signals": [],
        "project_signals": [],
    }


def _build_fallback_strengths_and_risks(
    *,
    linkedin_check: dict[str, Any],
    github_check: dict[str, Any],
    issue_flags: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    """Build deterministic strengths/risks when LLM output is unavailable."""

    strengths: list[str] = []
    risks: list[str] = []

    matched_employers = _coerce_text_list(linkedin_check.get("matched_employers"), max_items=3)
    matched_positions = _coerce_text_list(linkedin_check.get("matched_positions"), max_items=3)
    matched_skills = _coerce_text_list(linkedin_check.get("matched_skills"), max_items=5)
    github_repo_names = _coerce_text_list(github_check.get("top_repo_names"), max_items=4)

    if matched_employers:
        strengths.append(
            "LinkedIn corroborates resume employers: " + ", ".join(matched_employers[:3]) + "."
        )
    if matched_positions:
        strengths.append(
            "LinkedIn corroborates role titles: " + ", ".join(matched_positions[:3]) + "."
        )
    if matched_skills:
        strengths.append(
            "Public profile evidence supports skills: " + ", ".join(matched_skills[:5]) + "."
        )
    if github_repo_names:
        strengths.append("GitHub shows shipped repositories: " + ", ".join(github_repo_names[:4]) + ".")

    for flag in issue_flags:
        flag_type = flag.get("type")
        if flag_type == "experience_mismatch":
            risks.append("Experience mismatch between resume and LinkedIn evidence.")
        elif flag_type == "missing_projects":
            risks.append("Resume projects are not clearly represented in GitHub repositories.")
        elif flag_type == "skill_differences":
            risks.append("Some resume skills were not corroborated on LinkedIn/GitHub.")

    if not strengths:
        strengths.append("Public profile signal is limited but candidate submitted complete resume data.")
    if not risks:
        risks.append("No major discrepancies were detected from available public evidence.")

    return strengths[:6], risks[:6]


def _build_fallback_brief(
    *,
    candidate: CandidateSeed,
    strengths: list[str],
    risks: list[str],
) -> str:
    """Build deterministic 3-5 sentence brief."""

    sentences = [
        f"{candidate.full_name} is shortlisted for {candidate.role_selection}.",
        strengths[0],
        risks[0],
    ]
    if len(strengths) > 1:
        sentences.append(strengths[1])
    if len(risks) > 1:
        sentences.append(risks[1])
    return " ".join(sentences[:5])


def _build_llm_prompt(
    *,
    candidate: CandidateSeed,
    resume_snapshot: dict[str, Any],
    extracted_payload: dict[str, Any],
    cross_checks: dict[str, Any],
    issue_flags: list[dict[str, Any]],
) -> str:
    """Build strict prompt for final synthesis."""

    return (
        "You are a hiring research analyst.\n"
        "Use only the provided JSON. Do not invent facts.\n\n"
        "TASKS:\n"
        "1) Validate resume vs LinkedIn and resume vs GitHub cross-checks.\n"
        "2) Keep/adjust issue flags for: experience_mismatch, missing_projects, skill_differences.\n"
        "3) Produce strengths and risks lists.\n"
        "4) Write a 3-5 sentence hiring-manager brief.\n\n"
        "RULES:\n"
        "- If evidence is missing, say 'insufficient public evidence'.\n"
        "- Keep output concise and factual.\n"
        "- Return strict JSON only.\n\n"
        f"CANDIDATE: {candidate.full_name}\n"
        f"ROLE: {candidate.role_selection}\n"
        f"RESUME_JSON: {json.dumps(resume_snapshot, ensure_ascii=True)}\n"
        f"EXTRACTED_JSON: {json.dumps(extracted_payload, ensure_ascii=True)}\n"
        f"CROSS_CHECK_JSON: {json.dumps(cross_checks, ensure_ascii=True)}\n"
        f"ISSUE_FLAGS_JSON: {json.dumps(issue_flags, ensure_ascii=True)}\n\n"
        "OUTPUT JSON SCHEMA:\n"
        "{\n"
        '  "cross_reference": {\n'
        '    "resume_vs_linkedin": {\n'
        '      "employment_alignment": ["..."],\n'
        '      "skills_alignment": ["..."]\n'
        "    },\n"
        '    "resume_vs_github": {\n'
        '      "project_alignment": ["..."],\n'
        '      "skills_alignment": ["..."]\n'
        "    }\n"
        "  },\n"
        '  "issues": [\n'
        '    {"type":"experience_mismatch|missing_projects|skill_differences","severity":"high|medium|low","evidence":"..."}\n'
        "  ],\n"
        '  "strengths": ["..."],\n'
        '  "risks": ["..."],\n'
        '  "summary": "3-5 sentence brief"\n'
        "}"
    )


async def _invoke_model_once(
    *,
    bedrock_client: BedrockRuntimeClient,
    model_id: str,
    bedrock_config: BedrockRuntimeConfig,
    max_tokens: int,
    prompt: str,
) -> dict[str, Any]:
    """Invoke one Bedrock model and parse strict JSON response."""

    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": min(bedrock_config.max_tokens, max_tokens),
        "temperature": 0.0,
        "top_p": bedrock_config.top_p,
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": prompt}],
            }
        ],
    }
    response = await asyncio.wait_for(
        bedrock_client.invoke_json(
            model_id=model_id,
            payload=payload,
        ),
        timeout=bedrock_config.request_timeout_seconds,
    )
    return _extract_first_json_object(_extract_model_text(response))


def _normalize_llm_analysis(
    *,
    parsed_payload: dict[str, Any],
    config: ResearchRuntimeConfig,
    fallback_strengths: list[str],
    fallback_risks: list[str],
    fallback_brief: str,
    fallback_issue_flags: list[dict[str, Any]],
) -> dict[str, Any]:
    """Normalize model output with deterministic fallback values."""

    cross = parsed_payload.get("cross_reference")
    cross_ref = cross if isinstance(cross, dict) else {}
    rv_linkedin = (
        cross_ref.get("resume_vs_linkedin")
        if isinstance(cross_ref.get("resume_vs_linkedin"), dict)
        else {}
    )
    rv_github = (
        cross_ref.get("resume_vs_github")
        if isinstance(cross_ref.get("resume_vs_github"), dict)
        else {}
    )
    issues_raw = parsed_payload.get("issues")
    issues: list[dict[str, Any]] = []
    if isinstance(issues_raw, list):
        for item in issues_raw:
            if not isinstance(item, dict):
                continue
            issue_type = item.get("type")
            if not isinstance(issue_type, str):
                continue
            issues.append(
                {
                    "type": issue_type.strip(),
                    "severity": str(item.get("severity") or "medium").strip(),
                    "evidence": str(item.get("evidence") or "").strip(),
                }
            )

    if not issues:
        # Convert structured fallback issue flags to simple llm schema.
        for item in fallback_issue_flags:
            if not isinstance(item, dict):
                continue
            issues.append(
                {
                    "type": str(item.get("type") or "").strip(),
                    "severity": str(item.get("severity") or "medium").strip(),
                    "evidence": json.dumps(item.get("details") or {}, ensure_ascii=True),
                }
            )

    return {
        "cross_reference": {
            "resume_vs_linkedin": {
                "employment_alignment": _coerce_text_list(
                    rv_linkedin.get("employment_alignment"),
                    max_items=6,
                ),
                "skills_alignment": _coerce_text_list(
                    rv_linkedin.get("skills_alignment"),
                    max_items=8,
                ),
            },
            "resume_vs_github": {
                "project_alignment": _coerce_text_list(
                    rv_github.get("project_alignment"),
                    max_items=6,
                ),
                "skills_alignment": _coerce_text_list(
                    rv_github.get("skills_alignment"),
                    max_items=8,
                ),
            },
        },
        "issues": issues[:12],
        "strengths": _coerce_text_list(parsed_payload.get("strengths"), max_items=8)
        or fallback_strengths,
        "risks": _coerce_text_list(parsed_payload.get("risks"), max_items=8) or fallback_risks,
        "summary": _normalize_brief_text(
            text=parsed_payload.get("summary") if isinstance(parsed_payload.get("summary"), str) else None,
            min_sentences=config.enrichment.min_brief_sentences,
            max_sentences=config.enrichment.max_brief_sentences,
            fallback_text=fallback_brief,
        ),
    }


async def _load_candidates(
    *,
    config: ResearchRuntimeConfig,
    offset: int,
    limit: int,
    application_ids: list[UUID] | None,
) -> list[CandidateSeed]:
    """Load target candidates from DB."""

    runtime = get_runtime_config()
    session_factory = get_async_session_factory(runtime.postgres)
    async with session_factory() as session:
        statement = select(
            ApplicantApplication.id,
            ApplicantApplication.full_name,
            ApplicantApplication.role_selection,
            ApplicantApplication.applicant_status,
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


async def _persist_payload(
    *,
    candidate_id: UUID,
    payload: dict[str, Any],
    max_chars: int,
) -> None:
    """Persist compacted payload into online_research_summary."""

    runtime = get_runtime_config()
    session_factory = get_async_session_factory(runtime.postgres)
    compact_payload = _build_compact_storage_payload(payload)
    serialized = _serialize_compact_payload_with_limit(compact_payload, max_chars=max_chars)
    brief_raw = payload.get("brief")
    brief = _clip_text(brief_raw, max_chars=1500) if isinstance(brief_raw, str) else None
    async with session_factory() as session:
        entity = await session.get(ApplicantApplication, candidate_id)
        if entity is None:
            return
        entity.online_research_summary = serialized
        entity.candidate_brief = brief
        await session.commit()


def _build_compact_storage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Keep high-signal structured fields while fitting DB size constraints."""

    extractors = payload.get("extractors") if isinstance(payload.get("extractors"), dict) else {}
    linkedin = extractors.get("linkedin") if isinstance(extractors.get("linkedin"), dict) else {}
    github = extractors.get("github") if isinstance(extractors.get("github"), dict) else {}
    twitter = extractors.get("twitter") if isinstance(extractors.get("twitter"), dict) else {}
    portfolio = extractors.get("portfolio") if isinstance(extractors.get("portfolio"), dict) else {}

    linkedin_cross = (
        linkedin.get("cross_reference") if isinstance(linkedin.get("cross_reference"), dict) else {}
    )
    linkedin_skills = (
        linkedin_cross.get("skills") if isinstance(linkedin_cross.get("skills"), dict) else {}
    )
    linkedin_employment = (
        linkedin_cross.get("employment_history")
        if isinstance(linkedin_cross.get("employment_history"), dict)
        else {}
    )

    repos_raw = github.get("top_repositories")
    repos = repos_raw if isinstance(repos_raw, list) else []
    compact_repos: list[dict[str, Any]] = []
    for repo in repos[:4]:
        if not isinstance(repo, dict):
            continue
        compact_repos.append(
            {
                "name": repo.get("name"),
                "stars": repo.get("stars"),
                "language": repo.get("language"),
                "readme_summary": repo.get("readme_summary"),
            }
        )

    portfolio_hits_raw = portfolio.get("top_portfolio_hits")
    portfolio_hits = portfolio_hits_raw if isinstance(portfolio_hits_raw, list) else []
    compact_portfolio_hits: list[dict[str, Any]] = []
    for hit in portfolio_hits[:3]:
        if not isinstance(hit, dict):
            continue
        compact_portfolio_hits.append(
            {
                "title": _clip_text(hit.get("title"), max_chars=120),
                "link": hit.get("link"),
                "snippet": _clip_text(hit.get("snippet"), max_chars=180),
            }
        )

    llm = payload.get("llm_analysis") if isinstance(payload.get("llm_analysis"), dict) else {}
    llm_cross = llm.get("cross_reference") if isinstance(llm.get("cross_reference"), dict) else {}
    llm_li = (
        llm_cross.get("resume_vs_linkedin")
        if isinstance(llm_cross.get("resume_vs_linkedin"), dict)
        else {}
    )
    llm_gh = (
        llm_cross.get("resume_vs_github")
        if isinstance(llm_cross.get("resume_vs_github"), dict)
        else {}
    )

    resume_snapshot_raw = payload.get("resume_snapshot")
    resume_snapshot = resume_snapshot_raw if isinstance(resume_snapshot_raw, dict) else {}
    compact_work = resume_snapshot.get("work_experience")
    compact_work_list = compact_work if isinstance(compact_work, list) else []

    cross_checks_raw = payload.get("cross_checks")
    cross_checks = cross_checks_raw if isinstance(cross_checks_raw, dict) else {}
    check_li = (
        cross_checks.get("resume_vs_linkedin")
        if isinstance(cross_checks.get("resume_vs_linkedin"), dict)
        else {}
    )
    check_gh = (
        cross_checks.get("resume_vs_github")
        if isinstance(cross_checks.get("resume_vs_github"), dict)
        else {}
    )

    issue_flags_raw = payload.get("issue_flags")
    issue_flags_in = issue_flags_raw if isinstance(issue_flags_raw, list) else []
    compact_issue_flags: list[dict[str, Any]] = []
    for item in issue_flags_in[:6]:
        if not isinstance(item, dict):
            continue
        details = item.get("details")
        details_text = _clip_text(json.dumps(details or {}, ensure_ascii=False), max_chars=180)
        compact_issue_flags.append(
            {
                "type": item.get("type"),
                "severity": item.get("severity"),
                "source": item.get("source"),
                "details": details_text,
            }
        )

    llm_issues_raw = llm.get("issues")
    llm_issues = llm_issues_raw if isinstance(llm_issues_raw, list) else []
    compact_llm_issues: list[dict[str, Any]] = []
    for item in llm_issues[:6]:
        if not isinstance(item, dict):
            continue
        compact_llm_issues.append(
            {
                "type": item.get("type"),
                "severity": item.get("severity"),
                "evidence": _clip_text(item.get("evidence"), max_chars=140),
            }
        )

    return {
        "generated_at": payload.get("generated_at"),
        "candidate_id": payload.get("candidate_id"),
        "candidate_name": payload.get("candidate_name"),
        "role_selection": payload.get("role_selection"),
        "links_from_candidate_table": payload.get("links_from_candidate_table"),
        "resume_snapshot": {
            "skills": _coerce_text_list(resume_snapshot.get("skills"), max_items=12),
            "projects": _coerce_text_list(resume_snapshot.get("projects"), max_items=8),
            "work_experience": compact_work_list[:4],
            "total_years_experience": resume_snapshot.get("total_years_experience"),
        },
        "extractors": {
            "linkedin": {
                "matched_profile_url": linkedin.get("matched_profile_url"),
                "skills_matched": _coerce_text_list(
                    linkedin_skills.get("matched_on_linkedin"),
                    max_items=12,
                ),
                "skills_unmatched": _coerce_text_list(
                    linkedin_skills.get("unmatched_from_resume"),
                    max_items=12,
                ),
                "matched_employers": _coerce_text_list(
                    linkedin_employment.get("matched_employers_on_linkedin"),
                    max_items=8,
                ),
                "unmatched_employers": _coerce_text_list(
                    linkedin_employment.get("unmatched_employers_from_resume"),
                    max_items=8,
                ),
            },
            "github": {
                "profile_url": github.get("profile_url"),
                "top_repositories": compact_repos,
                "aggregate": github.get("aggregate"),
            },
            "twitter": {
                "mode": twitter.get("mode"),
                "profile_url": twitter.get("profile_url"),
                "note": twitter.get("note"),
            },
            "portfolio": {
                "mode": portfolio.get("mode"),
                "input_portfolio_url": portfolio.get("input_portfolio_url"),
                "matched_portfolio_url": portfolio.get("matched_portfolio_url"),
                "technology_signals": _coerce_text_list(
                    portfolio.get("technology_signals"),
                    max_items=8,
                ),
                "project_signals": _coerce_text_list(
                    portfolio.get("project_signals"),
                    max_items=8,
                ),
                "top_portfolio_hits": compact_portfolio_hits,
                "note": portfolio.get("note"),
            },
        },
        "cross_checks": {
            "resume_vs_linkedin": {
                "matched_skills": _coerce_text_list(check_li.get("matched_skills"), max_items=8),
                "unmatched_skills": _coerce_text_list(check_li.get("unmatched_skills"), max_items=8),
                "matched_employers": _coerce_text_list(
                    check_li.get("matched_employers"),
                    max_items=5,
                ),
                "unmatched_employers": _coerce_text_list(
                    check_li.get("unmatched_employers"),
                    max_items=5,
                ),
                "matched_positions": _coerce_text_list(
                    check_li.get("matched_positions"),
                    max_items=5,
                ),
                "unmatched_positions": _coerce_text_list(
                    check_li.get("unmatched_positions"),
                    max_items=5,
                ),
                "experience_mismatch": bool(check_li.get("experience_mismatch")),
                "skill_differences": bool(check_li.get("skill_differences")),
            },
            "resume_vs_github": {
                "matched_skills": _coerce_text_list(check_gh.get("matched_skills"), max_items=8),
                "unmatched_skills": _coerce_text_list(check_gh.get("unmatched_skills"), max_items=8),
                "matched_projects": _coerce_text_list(check_gh.get("matched_projects"), max_items=6),
                "missing_projects": _coerce_text_list(check_gh.get("missing_projects"), max_items=6),
                "top_repo_names": _coerce_text_list(check_gh.get("top_repo_names"), max_items=5),
                "skill_differences": bool(check_gh.get("skill_differences")),
                "missing_projects_flag": bool(check_gh.get("missing_projects_flag")),
            },
        },
        "issue_flags": compact_issue_flags,
        "llm_analysis": {
            "source": llm.get("source"),
            "model_id": llm.get("model_id"),
            "cross_reference": {
                "resume_vs_linkedin": {
                    "employment_alignment": _coerce_text_list(
                        llm_li.get("employment_alignment"),
                        max_items=4,
                    ),
                    "skills_alignment": _coerce_text_list(
                        llm_li.get("skills_alignment"),
                        max_items=5,
                    ),
                },
                "resume_vs_github": {
                    "project_alignment": _coerce_text_list(
                        llm_gh.get("project_alignment"),
                        max_items=4,
                    ),
                    "skills_alignment": _coerce_text_list(
                        llm_gh.get("skills_alignment"),
                        max_items=5,
                    ),
                },
            },
            "issues": compact_llm_issues,
            "strengths": _coerce_text_list(llm.get("strengths"), max_items=5),
            "risks": _coerce_text_list(llm.get("risks"), max_items=5),
            "summary": llm.get("summary"),
        },
        "discrepancies": payload.get("discrepancies"),
        "brief": payload.get("brief"),
    }


def _serialize_compact_payload_with_limit(payload: dict[str, Any], *, max_chars: int) -> str:
    """Serialize compact payload while retaining core sections."""

    candidate = copy.deepcopy(payload)
    serialized = json.dumps(candidate, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized

    # Pass 1: shrink long prose fields.
    llm = candidate.get("llm_analysis")
    if isinstance(llm, dict):
        llm["summary"] = _clip_text(llm.get("summary"), max_chars=350)
        llm["issues"] = (llm.get("issues") or [])[:6]
        llm["strengths"] = _coerce_text_list(llm.get("strengths"), max_items=3)
        llm["risks"] = _coerce_text_list(llm.get("risks"), max_items=3)
    candidate["brief"] = _clip_text(candidate.get("brief"), max_chars=350)
    candidate["discrepancies"] = _coerce_text_list(candidate.get("discrepancies"), max_items=6)
    serialized = json.dumps(candidate, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized

    # Pass 2: shrink resume and repository detail.
    resume_snapshot = candidate.get("resume_snapshot")
    if isinstance(resume_snapshot, dict):
        resume_snapshot["skills"] = _coerce_text_list(resume_snapshot.get("skills"), max_items=10)
        resume_snapshot["projects"] = _coerce_text_list(resume_snapshot.get("projects"), max_items=6)
        work = resume_snapshot.get("work_experience")
        if isinstance(work, list):
            resume_snapshot["work_experience"] = work[:3]

    extractors = candidate.get("extractors")
    if isinstance(extractors, dict):
        github = extractors.get("github")
        if isinstance(github, dict):
            repos = github.get("top_repositories")
            if isinstance(repos, list):
                trimmed: list[dict[str, Any]] = []
                for repo in repos[:2]:
                    if not isinstance(repo, dict):
                        continue
                    trimmed.append(
                        {
                            "name": repo.get("name"),
                            "stars": repo.get("stars"),
                            "language": repo.get("language"),
                            "readme_summary": _clip_text(repo.get("readme_summary"), max_chars=140),
                        }
                    )
                github["top_repositories"] = trimmed
        linkedin = extractors.get("linkedin")
        if isinstance(linkedin, dict):
            linkedin["skills_unmatched"] = _coerce_text_list(linkedin.get("skills_unmatched"), max_items=8)
            linkedin["unmatched_employers"] = _coerce_text_list(
                linkedin.get("unmatched_employers"),
                max_items=5,
            )
        portfolio = extractors.get("portfolio")
        if isinstance(portfolio, dict):
            portfolio["technology_signals"] = _coerce_text_list(
                portfolio.get("technology_signals"),
                max_items=5,
            )
            portfolio["project_signals"] = _coerce_text_list(
                portfolio.get("project_signals"),
                max_items=5,
            )
            top_hits = portfolio.get("top_portfolio_hits")
            if isinstance(top_hits, list):
                trimmed_hits: list[dict[str, Any]] = []
                for hit in top_hits[:2]:
                    if not isinstance(hit, dict):
                        continue
                    trimmed_hits.append(
                        {
                            "title": _clip_text(hit.get("title"), max_chars=90),
                            "link": hit.get("link"),
                        }
                    )
                portfolio["top_portfolio_hits"] = trimmed_hits

    candidate["issue_flags"] = (candidate.get("issue_flags") or [])[:5]
    serialized = json.dumps(candidate, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized

    # Final pass: keep schema with aggressively compacted sections.
    extractors = candidate.get("extractors") if isinstance(candidate.get("extractors"), dict) else {}
    linkedin = extractors.get("linkedin") if isinstance(extractors.get("linkedin"), dict) else {}
    github = extractors.get("github") if isinstance(extractors.get("github"), dict) else {}
    twitter = extractors.get("twitter") if isinstance(extractors.get("twitter"), dict) else {}
    portfolio = extractors.get("portfolio") if isinstance(extractors.get("portfolio"), dict) else {}

    github_repos = github.get("top_repositories") if isinstance(github.get("top_repositories"), list) else []
    compact_repos: list[dict[str, Any]] = []
    for repo in github_repos[:2]:
        if not isinstance(repo, dict):
            continue
        compact_repos.append(
            {
                "name": repo.get("name"),
                "stars": repo.get("stars"),
                "language": repo.get("language"),
            }
        )

    cross_checks = candidate.get("cross_checks") if isinstance(candidate.get("cross_checks"), dict) else {}
    cross_li = (
        cross_checks.get("resume_vs_linkedin")
        if isinstance(cross_checks.get("resume_vs_linkedin"), dict)
        else {}
    )
    cross_gh = (
        cross_checks.get("resume_vs_github")
        if isinstance(cross_checks.get("resume_vs_github"), dict)
        else {}
    )

    llm = candidate.get("llm_analysis") if isinstance(candidate.get("llm_analysis"), dict) else {}
    llm_cross = llm.get("cross_reference") if isinstance(llm.get("cross_reference"), dict) else {}
    llm_li = (
        llm_cross.get("resume_vs_linkedin")
        if isinstance(llm_cross.get("resume_vs_linkedin"), dict)
        else {}
    )
    llm_gh = (
        llm_cross.get("resume_vs_github")
        if isinstance(llm_cross.get("resume_vs_github"), dict)
        else {}
    )

    issue_flags_raw = candidate.get("issue_flags")
    issue_flags = issue_flags_raw if isinstance(issue_flags_raw, list) else []
    compact_issue_flags = []
    for item in issue_flags[:4]:
        if not isinstance(item, dict):
            continue
        compact_issue_flags.append(
            {
                "type": item.get("type"),
                "severity": item.get("severity"),
                "details": _clip_text(item.get("details"), max_chars=90),
            }
        )

    compact_minimal = {
        "generated_at": candidate.get("generated_at"),
        "candidate_id": candidate.get("candidate_id"),
        "candidate_name": candidate.get("candidate_name"),
        "role_selection": candidate.get("role_selection"),
        "links_from_candidate_table": candidate.get("links_from_candidate_table"),
        "extractors": {
            "linkedin": {
                "matched_profile_url": linkedin.get("matched_profile_url"),
                "skills_unmatched": _coerce_text_list(linkedin.get("skills_unmatched"), max_items=2),
                "unmatched_employers": _coerce_text_list(
                    linkedin.get("unmatched_employers"),
                    max_items=2,
                ),
            },
            "github": {
                "profile_url": github.get("profile_url"),
                "top_repositories": compact_repos,
            },
            "twitter": {
                "mode": twitter.get("mode"),
                "profile_url": twitter.get("profile_url"),
            },
            "portfolio": {
                "mode": portfolio.get("mode"),
                "matched_portfolio_url": portfolio.get("matched_portfolio_url"),
                "technology_signals": _coerce_text_list(
                    portfolio.get("technology_signals"),
                    max_items=2,
                ),
                "project_signals": _coerce_text_list(
                    portfolio.get("project_signals"),
                    max_items=2,
                ),
            },
        },
        "cross_checks": {
            "resume_vs_linkedin": {
                "experience_mismatch": bool(cross_li.get("experience_mismatch")),
                "skill_differences": bool(cross_li.get("skill_differences")),
                "unmatched_employers": _coerce_text_list(
                    cross_li.get("unmatched_employers"),
                    max_items=2,
                ),
                "unmatched_skills": _coerce_text_list(cross_li.get("unmatched_skills"), max_items=2),
            },
            "resume_vs_github": {
                "missing_projects_flag": bool(cross_gh.get("missing_projects_flag")),
                "skill_differences": bool(cross_gh.get("skill_differences")),
                "missing_projects": _coerce_text_list(cross_gh.get("missing_projects"), max_items=2),
                "unmatched_skills": _coerce_text_list(cross_gh.get("unmatched_skills"), max_items=2),
            },
        },
        "issue_flags": compact_issue_flags,
        "llm_analysis": {
            "source": llm.get("source"),
            "model_id": llm.get("model_id"),
            "cross_reference": {
                "resume_vs_linkedin": {
                    "employment_alignment": _coerce_text_list(
                        llm_li.get("employment_alignment"),
                        max_items=2,
                    ),
                    "skills_alignment": _coerce_text_list(
                        llm_li.get("skills_alignment"),
                        max_items=2,
                    ),
                },
                "resume_vs_github": {
                    "project_alignment": _coerce_text_list(
                        llm_gh.get("project_alignment"),
                        max_items=2,
                    ),
                    "skills_alignment": _coerce_text_list(
                        llm_gh.get("skills_alignment"),
                        max_items=2,
                    ),
                },
            },
            "strengths": _coerce_text_list(llm.get("strengths"), max_items=2),
            "risks": _coerce_text_list(llm.get("risks"), max_items=2),
            "summary": _clip_text(llm.get("summary"), max_chars=180),
        },
        "discrepancies": _coerce_text_list(candidate.get("discrepancies"), max_items=2),
        "brief": _clip_text(candidate.get("brief"), max_chars=220),
    }

    serialized = json.dumps(compact_minimal, ensure_ascii=False)
    if len(serialized) <= max_chars:
        return serialized

    return json.dumps(
        {
            "candidate_id": candidate.get("candidate_id"),
            "brief": _clip_text(candidate.get("brief"), max_chars=220),
            "issue_flags": (candidate.get("issue_flags") or [])[:3],
        },
        ensure_ascii=False,
    )


async def _enrich_one_candidate(
    *,
    candidate: CandidateSeed,
    config: ResearchRuntimeConfig,
    bedrock_client: BedrockRuntimeClient | None,
    bedrock_config: BedrockRuntimeConfig,
) -> dict[str, Any]:
    """Run extractor pipeline + cross-checks + LLM synthesis."""

    logger.info("research candidate=%s start", candidate.id)
    resume_snapshot = _build_resume_snapshot(candidate.parse_result)
    resume_signals = _resume_signal_lists(candidate.parse_result)
    logger.info("research candidate=%s resume snapshot built", candidate.id)

    if candidate.linkedin_url:
        linkedin_payload = await _run_linkedin_extractor(
            linkedin_url=candidate.linkedin_url,
            application_id=candidate.id,
            full_name=candidate.full_name,
            role_selection=candidate.role_selection,
            linkedin_text=None,
        )
    else:
        linkedin_payload = {
            "mode": "missing_link",
            "error": "linkedin_url missing in candidate table",
            "cross_reference": {
                "skills": {
                    "matched_on_linkedin": [],
                    "unmatched_from_resume": resume_signals.get("skills", []),
                },
                "employment_history": {
                    "matched_employers_on_linkedin": [],
                    "unmatched_employers_from_resume": resume_signals.get("employers", []),
                    "matched_positions_on_linkedin": [],
                    "unmatched_positions_from_resume": resume_signals.get("positions", []),
                },
            },
        }
    logger.info("research candidate=%s linkedin done", candidate.id)

    if candidate.github_url:
        github_payload = await _run_github_extractor(
            github_url=candidate.github_url,
            username=None,
            top_repos=config.github.max_repos_in_summary,
            commit_window_days=90,
        )
    else:
        github_payload = {
            "mode": "missing_link",
            "error": "github_url missing in candidate table",
            "top_repositories": [],
            "aggregate": {"top_languages": [], "activity_status": "inactive"},
        }
    logger.info("research candidate=%s github done", candidate.id)

    twitter_payload = _build_mock_twitter_payload(candidate)
    portfolio_payload = await _extract_portfolio_payload(candidate)
    logger.info("research candidate=%s twitter mocked", candidate.id)
    logger.info(
        "research candidate=%s portfolio done mode=%s",
        candidate.id,
        portfolio_payload.get("mode"),
    )

    linkedin_check = _cross_check_resume_vs_linkedin(
        resume_signals=resume_signals,
        linkedin_payload=linkedin_payload,
    )
    github_check = _cross_check_resume_vs_github(
        resume_signals=resume_signals,
        github_payload=github_payload,
    )
    issue_flags = _build_issue_flags(
        linkedin_check=linkedin_check,
        github_check=github_check,
    )
    cross_checks = {
        "resume_vs_linkedin": linkedin_check,
        "resume_vs_github": github_check,
    }
    logger.info("research candidate=%s cross-check done", candidate.id)

    strengths_fallback, risks_fallback = _build_fallback_strengths_and_risks(
        linkedin_check=linkedin_check,
        github_check=github_check,
        issue_flags=issue_flags,
    )
    brief_fallback = _build_fallback_brief(
        candidate=candidate,
        strengths=strengths_fallback,
        risks=risks_fallback,
    )

    extracted_payload = {
        "linkedin": linkedin_payload,
        "github": github_payload,
        "twitter": twitter_payload,
        "portfolio": portfolio_payload,
    }

    llm_analysis = {
        "source": "heuristic_fallback",
        "cross_reference": {
            "resume_vs_linkedin": {
                "employment_alignment": [],
                "skills_alignment": [],
            },
            "resume_vs_github": {
                "project_alignment": [],
                "skills_alignment": [],
            },
        },
        "issues": [
            {
                "type": item.get("type"),
                "severity": item.get("severity"),
                "evidence": json.dumps(item.get("details") or {}, ensure_ascii=True),
            }
            for item in issue_flags
        ],
        "strengths": strengths_fallback,
        "risks": risks_fallback,
        "summary": brief_fallback,
    }

    if config.enrichment.llm_analysis_enabled and bedrock_client is not None:
        prompt = _build_llm_prompt(
            candidate=candidate,
            resume_snapshot=resume_snapshot,
            extracted_payload=extracted_payload,
            cross_checks=cross_checks,
            issue_flags=issue_flags,
        )
        parsed_payload: dict[str, Any] | None = None
        model_used: str | None = None
        try:
            parsed_payload = await _invoke_model_once(
                bedrock_client=bedrock_client,
                model_id=bedrock_config.primary_model_id,
                bedrock_config=bedrock_config,
                max_tokens=config.enrichment.llm_max_tokens,
                prompt=prompt,
            )
            model_used = bedrock_config.primary_model_id
        except (TimeoutError, BedrockInvocationError, RuntimeError, Exception):
            logger.exception("primary model failed candidate=%s", candidate.id)
            try:
                parsed_payload = await _invoke_model_once(
                    bedrock_client=bedrock_client,
                    model_id=bedrock_config.fallback_model_id,
                    bedrock_config=bedrock_config,
                    max_tokens=config.enrichment.llm_max_tokens,
                    prompt=prompt,
                )
                model_used = bedrock_config.fallback_model_id
            except (TimeoutError, BedrockInvocationError, RuntimeError, Exception):
                logger.exception("fallback model failed candidate=%s", candidate.id)

        if isinstance(parsed_payload, dict):
            normalized = _normalize_llm_analysis(
                parsed_payload=parsed_payload,
                config=config,
                fallback_strengths=strengths_fallback,
                fallback_risks=risks_fallback,
                fallback_brief=brief_fallback,
                fallback_issue_flags=issue_flags,
            )
            llm_analysis = {
                "source": "model",
                "model_id": model_used,
                **normalized,
            }
            logger.info(
                "research candidate=%s llm done source=model model_id=%s",
                candidate.id,
                model_used,
            )
        else:
            logger.info("research candidate=%s llm fallback used", candidate.id)
    else:
        logger.info("research candidate=%s llm disabled", candidate.id)

    issue_list = llm_analysis.get("issues")
    normalized_issues = issue_list if isinstance(issue_list, list) else []
    discrepancies = [
        f"{item.get('type')}: {item.get('evidence')}"
        for item in normalized_issues
        if isinstance(item, dict) and item.get("type")
    ][: config.enrichment.max_discrepancies]

    brief = _normalize_brief_text(
        text=llm_analysis.get("summary") if isinstance(llm_analysis.get("summary"), str) else None,
        min_sentences=config.enrichment.min_brief_sentences,
        max_sentences=config.enrichment.max_brief_sentences,
        fallback_text=brief_fallback,
    )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "candidate_id": str(candidate.id),
        "candidate_name": candidate.full_name,
        "role_selection": candidate.role_selection,
        "resume_snapshot": resume_snapshot,
        "links_from_candidate_table": {
            "linkedin_url": candidate.linkedin_url,
            "github_url": candidate.github_url,
            "twitter_url": candidate.twitter_url,
            "portfolio_url": candidate.portfolio_url,
        },
        "extractors": extracted_payload,
        "cross_checks": cross_checks,
        "issue_flags": issue_flags,
        "llm_analysis": llm_analysis,
        "discrepancies": discrepancies,
        "brief": brief,
    }


async def _run(
    *,
    offset: int,
    limit: int,
    dry_run: bool,
    application_ids: list[UUID] | None,
) -> None:
    """Run enrichment batch for shortlisted candidates."""

    runtime = get_runtime_config()
    settings = get_settings()
    config = runtime.research
    if not config.enabled:
        raise RuntimeError("research.enabled=false")
    if not settings.serpapi_api_key:
        raise RuntimeError("SERPAPI_API_KEY is required in .env")

    bedrock_config = runtime.bedrock
    bedrock_client: BedrockRuntimeClient | None = None
    if config.enrichment.llm_analysis_enabled and bedrock_config.enabled:
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
    logger.info("llm-profile-enrichment candidates=%s", len(candidates))
    if not candidates:
        return

    semaphore = asyncio.Semaphore(max(1, config.max_concurrency))

    async def process(candidate: CandidateSeed) -> None:
        async with semaphore:
            try:
                payload = await _enrich_one_candidate(
                    candidate=candidate,
                    config=config,
                    bedrock_client=bedrock_client,
                    bedrock_config=bedrock_config,
                )
                if dry_run:
                    logger.info("dry-run candidate=%s brief=%s", candidate.id, payload.get("brief"))
                    return

                await _persist_payload(
                    candidate_id=candidate.id,
                    payload=payload,
                    max_chars=config.enrichment.max_research_json_chars,
                )
                logger.info("enriched candidate=%s", candidate.id)
            except Exception:
                logger.exception("failed enriching candidate=%s", candidate.id)

    await asyncio.gather(*(process(candidate) for candidate in candidates))


def _build_parser() -> argparse.ArgumentParser:
    """Build CLI parser."""

    parser = argparse.ArgumentParser(
        description=(
            "Run shortlisted enrichment with LinkedIn/GitHub/Twitter/portfolio extractors, "
            "resume cross-checks, issue flags, and LLM primary->fallback synthesis."
        )
    )
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--application-id",
        action="append",
        default=None,
        help="Specific candidate UUID to process (repeatable).",
    )
    return parser


def main() -> None:
    """Entrypoint for `python -m app.scripts.enrich_shortlisted_llm_profiles`."""

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
    from app.scripts.error import run_script_entrypoint

    raise SystemExit(run_script_entrypoint(main))
