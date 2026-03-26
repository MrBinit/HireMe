"""Search LinkedIn via SerpAPI and cross-reference against parsed resume data."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import func, select

from app.core.runtime_config import get_runtime_config
from app.core.settings import get_settings
from app.infra.database import get_async_session_factory
from app.model.applicant_application import ApplicantApplication
from app.scripts.run_online_research import SerpApiClient, _extract_hits


@dataclass(slots=True)
class CandidateContext:
    """Candidate details needed for cross-reference."""

    id: UUID
    full_name: str
    role_selection: str
    parse_result: dict[str, Any]
    linkedin_url: str | None


def _is_bullet_line(line: str, bullet_prefixes: list[str]) -> bool:
    """Return whether line starts with a configured bullet prefix."""

    stripped = line.strip()
    return any(stripped.startswith(prefix) for prefix in bullet_prefixes if prefix)


def _strip_bullet_prefix(line: str, bullet_prefixes: list[str]) -> str:
    """Remove leading bullet marker and trim."""

    stripped = line.strip()
    for prefix in bullet_prefixes:
        if prefix and stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def _line_is_ignored(line: str, patterns: list[str]) -> bool:
    """Return whether line should be ignored by pattern rules."""

    value = line.strip()
    if not value:
        return True
    return any(re.search(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def _clean_profile_lines(raw_text: str, *, ignore_patterns: list[str]) -> list[str]:
    """Normalize LinkedIn pasted text into usable non-empty lines."""

    cleaned: list[str] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip().replace("\u200b", "")
        if not line:
            continue
        line = line.replace("… more", "").strip()
        if _line_is_ignored(line, ignore_patterns):
            continue
        cleaned.append(line)
    return cleaned


def _split_profile_sections(
    *,
    lines: list[str],
    section_headings: dict[str, list[str]],
    stop_headings: list[str],
) -> dict[str, list[str]]:
    """Split profile text into configured sections using heading markers."""

    heading_to_section: dict[str, str] = {}
    for section, headings in section_headings.items():
        for heading in headings:
            heading_to_section[heading.casefold().strip()] = section
    stop_set = {item.casefold().strip() for item in stop_headings}

    sections: dict[str, list[str]] = {section: [] for section in section_headings}
    current_section: str | None = None
    for line in lines:
        normalized = line.casefold().strip()
        if normalized in stop_set:
            break
        mapped = heading_to_section.get(normalized)
        if mapped:
            current_section = mapped
            continue
        if current_section:
            sections[current_section].append(line)
    return sections


def _is_period_line(line: str, *, month_names: list[str]) -> bool:
    """Return whether line contains a month-year range."""

    months = "|".join(re.escape(item) for item in month_names)
    pattern = (
        rf"^(?:{months})\s+\d{{4}}\s*[–-]\s*" rf"(?:Present|(?:{months})\s+\d{{4}})(?:\s*·\s*.+)?$"
    )
    return re.match(pattern, line, flags=re.IGNORECASE) is not None


def _parse_period_line(line: str) -> tuple[str | None, str | None, str | None]:
    """Parse start/end/duration from a standard period line."""

    parts = [item.strip() for item in line.split("·") if item.strip()]
    date_range = parts[0] if parts else ""
    duration_text = parts[1] if len(parts) > 1 else None
    if "–" in date_range:
        start, end = [item.strip() for item in date_range.split("–", 1)]
        return start, end, duration_text
    if "-" in date_range:
        start, end = [item.strip() for item in date_range.split("-", 1)]
        return start, end, duration_text
    return date_range or None, None, duration_text


def _parse_location_line(line: str) -> tuple[str | None, str | None]:
    """Parse location and work mode from line containing separator dot."""

    if "·" not in line:
        return line.strip() or None, None
    left, right = [item.strip() for item in line.split("·", 1)]
    mode = right if right else None
    location = left if left else None
    return location, mode


def _parse_experience(
    lines: list[str],
    *,
    month_names: list[str],
    bullet_prefixes: list[str],
    max_items: int,
) -> list[dict[str, Any]]:
    """Parse LinkedIn experience section lines into structured entries."""

    entries: list[dict[str, Any]] = []
    header_buffer: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_period_line(line, month_names=month_names):
            header = [
                item for item in header_buffer if item and not item.casefold().endswith(" logo")
            ]
            title = header[-2] if len(header) >= 2 else None
            company_line = header[-1] if header else ""
            company = company_line
            employment_type = None
            if "·" in company_line:
                company, employment_type = [item.strip() for item in company_line.split("·", 1)]

            start, end, duration_text = _parse_period_line(line)
            location = None
            work_mode = None
            next_index = index + 1
            preloaded_header: list[str] | None = None
            if next_index < len(lines) and "·" in lines[next_index]:
                location, work_mode = _parse_location_line(lines[next_index])
                next_index += 1

            highlights: list[str] = []
            while next_index < len(lines):
                nxt = lines[next_index]
                if _is_period_line(nxt, month_names=month_names):
                    break
                if not _is_bullet_line(nxt, bullet_prefixes):
                    if (
                        next_index + 2 < len(lines)
                        and "·" in lines[next_index + 1]
                        and _is_period_line(lines[next_index + 2], month_names=month_names)
                    ):
                        preloaded_header = [nxt, lines[next_index + 1]]
                        next_index += 2
                        break
                    if next_index + 1 < len(lines) and _is_period_line(
                        lines[next_index + 1], month_names=month_names
                    ):
                        preloaded_header = [nxt]
                        next_index += 1
                        break
                cleaned = _strip_bullet_prefix(nxt, bullet_prefixes=bullet_prefixes)
                if re.search(r"\+\d+\s*skills?$", cleaned, flags=re.IGNORECASE):
                    next_index += 1
                    continue
                if cleaned:
                    if (
                        highlights
                        and not _is_bullet_line(nxt, bullet_prefixes)
                        and not re.match(r"^[A-Z0-9]", cleaned)
                    ):
                        highlights[-1] = f"{highlights[-1]} {cleaned}".strip()
                    else:
                        highlights.append(cleaned)
                next_index += 1

            entries.append(
                {
                    "company": company or None,
                    "title": title,
                    "employment_type": employment_type,
                    "start": start,
                    "end": end,
                    "duration_text": duration_text,
                    "location": location,
                    "work_mode": work_mode,
                    "highlights": highlights,
                }
            )
            header_buffer = preloaded_header or []
            index = next_index
            if len(entries) >= max_items:
                break
            continue

        if not _is_bullet_line(line, bullet_prefixes):
            header_buffer.append(line)
        index += 1

    return entries


def _parse_education(lines: list[str], *, max_items: int) -> list[dict[str, Any]]:
    """Parse education section lines."""

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    year_range_re = re.compile(r"^\d{4}\s*[–-]\s*(?:\d{4}|Present)$", re.IGNORECASE)

    for line in lines:
        if line.casefold().endswith(" logo"):
            continue
        if current is None:
            current = {"institution": line}
            continue

        if year_range_re.match(line):
            current["year_range"] = line
            year_parts = re.split(r"\s*[–-]\s*", line, maxsplit=1)
            if len(year_parts) == 2:
                current["start_year"] = year_parts[0]
                current["end_year"] = year_parts[1]
            continue

        if line.casefold().startswith("grade:"):
            current["grade"] = line.split(":", 1)[1].strip()
            continue

        if "year_range" in current and line and not line.casefold().startswith("grade:"):
            entries.append(current)
            if len(entries) >= max_items:
                return entries
            current = {"institution": line}
            continue

        if "degree" not in current:
            if "," in line:
                degree, field = [item.strip() for item in line.split(",", 1)]
                current["degree"] = degree
                current["field"] = field
            else:
                current["degree"] = line

    if current:
        entries.append(current)
    return entries[:max_items]


def _split_skill_text(value: str) -> list[str]:
    """Split skill-list line into individual skill values."""

    cleaned = re.sub(r"\+\d+\s*skills?$", "", value, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\band\b\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s+and\s+|,\s*", cleaned)
    output = [item.strip() for item in parts if item.strip()]
    return output


def _is_cert_skill_line(line: str) -> bool:
    """Return whether line is likely a certification skills line."""

    normalized = line.strip()
    if not normalized:
        return False
    if re.search(r"\+\d+\s*skills?$", normalized, flags=re.IGNORECASE):
        return True
    if "(" in normalized and ")" in normalized and (" and " in normalized or "," in normalized):
        return True
    if normalized.casefold().startswith(
        (
            "python",
            "large language models",
            "machine learning",
            "data science",
            "aws",
            "llm",
        )
    ):
        return True
    return False


def _parse_licenses(lines: list[str], *, max_items: int) -> list[dict[str, Any]]:
    """Parse licenses and certifications section."""

    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in lines:
        if line.casefold().endswith(" logo"):
            continue
        if line.casefold().startswith("issued "):
            if current is None:
                continue
            parts = [item.strip() for item in line.split("·") if item.strip()]
            if parts:
                current["issued"] = parts[0].replace("Issued", "").strip()
            for part in parts[1:]:
                if part.casefold().startswith("expires"):
                    current["expires"] = part.replace("Expires", "").strip()
            continue
        if line.casefold().startswith("credential id"):
            if current is None:
                continue
            current["credential_id"] = line.split("Credential ID", 1)[1].strip()
            continue
        if _is_cert_skill_line(line):
            if current is None:
                continue
            skills = current.get("skills", [])
            skills.extend(_split_skill_text(line))
            current["skills"] = list(dict.fromkeys(skills))
            continue

        if current is None:
            current = {"name": line}
            continue
        if "issuer" not in current:
            current["issuer"] = line
            continue

        if "issued" in current or "credential_id" in current:
            entries.append(current)
            if len(entries) >= max_items:
                return entries
            current = {"name": line}
            continue

        entries.append(current)
        if len(entries) >= max_items:
            return entries
        current = {"name": line}

    if current:
        entries.append(current)
    return entries[:max_items]


def _parse_projects(
    lines: list[str],
    *,
    month_names: list[str],
    bullet_prefixes: list[str],
    max_items: int,
) -> list[dict[str, Any]]:
    """Parse projects section from LinkedIn profile text."""

    entries: list[dict[str, Any]] = []
    header_buffer: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_period_line(line, month_names=month_names):
            title = None
            for candidate in reversed(header_buffer):
                if candidate and not candidate.casefold().startswith("thumbnail for"):
                    title = candidate
                    break
            start, end, _ = _parse_period_line(line)

            index += 1
            summary_lines: list[str] = []
            skills: list[str] = []
            while index < len(lines):
                nxt = lines[index]
                if _is_period_line(nxt, month_names=month_names):
                    break
                if index + 1 < len(lines) and _is_period_line(
                    lines[index + 1], month_names=month_names
                ):
                    header_buffer = [nxt]
                    break
                clean_line = _strip_bullet_prefix(nxt, bullet_prefixes)
                if "skill" in clean_line.casefold():
                    skills.extend(_split_skill_text(clean_line))
                elif (
                    " and " in clean_line.casefold()
                    and len(clean_line.split()) <= 8
                    and not clean_line.endswith(".")
                ):
                    skills.extend(_split_skill_text(clean_line))
                elif clean_line and not clean_line.casefold().startswith("thumbnail for"):
                    if title and clean_line == title:
                        index += 1
                        continue
                    if clean_line not in summary_lines:
                        summary_lines.append(clean_line)
                index += 1

            entries.append(
                {
                    "name": title,
                    "start": start,
                    "end": end,
                    "summary": " ".join(summary_lines).strip() or None,
                    "skills": list(dict.fromkeys(skills)),
                }
            )
            if len(entries) >= max_items:
                break
            continue

        header_buffer.append(line)
        index += 1

    return entries


def _parse_skills(lines: list[str], *, max_items: int) -> list[str]:
    """Parse concise skills section lines."""

    skills: list[str] = []
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if len(value.split()) > 5:
            continue
        if any(char.isdigit() for char in value) and "api" not in value.casefold():
            continue
        skills.append(value)
        if len(skills) >= max_items:
            break
    return list(dict.fromkeys(skills))


def _extract_linkedin_profile_text(raw_text: str) -> dict[str, Any]:
    """Extract structured LinkedIn profile sections from pasted profile text."""

    cfg = get_runtime_config().research.linkedin_text_extract
    cleaned_lines = _clean_profile_lines(raw_text, ignore_patterns=cfg.ignore_line_patterns)
    sections = _split_profile_sections(
        lines=cleaned_lines,
        section_headings=cfg.section_headings,
        stop_headings=cfg.stop_headings,
    )
    experience_lines = sections.get("experience", [])
    education_lines = sections.get("education", [])
    license_lines = sections.get("licenses_and_certifications", [])
    projects_lines = sections.get("projects", [])
    skills_lines = sections.get("skills", [])

    return {
        "experience": _parse_experience(
            experience_lines,
            month_names=cfg.month_names,
            bullet_prefixes=cfg.bullet_prefixes,
            max_items=cfg.max_items_per_section,
        ),
        "education": _parse_education(
            education_lines,
            max_items=cfg.max_items_per_section,
        ),
        "licenses_and_certifications": _parse_licenses(
            license_lines,
            max_items=cfg.max_items_per_section,
        ),
        "projects": _parse_projects(
            projects_lines,
            month_names=cfg.month_names,
            bullet_prefixes=cfg.bullet_prefixes,
            max_items=cfg.max_items_per_section,
        ),
        "skills": _parse_skills(skills_lines, max_items=cfg.max_items_per_section),
    }


def _extract_linkedin_handle(linkedin_url: str) -> str:
    """Return LinkedIn handle from profile URL, if present."""

    path = urlparse(linkedin_url).path.strip("/")
    parts = [item for item in path.split("/") if item]
    if len(parts) >= 2 and parts[0].casefold() == "in":
        return parts[1]
    if parts:
        return parts[-1]
    return ""


def _normalize_text(value: str) -> str:
    """Normalize text for case-insensitive matching."""

    return re.sub(r"\s+", " ", value.strip().casefold())


def _tokens(value: str, min_length: int) -> list[str]:
    """Extract searchable tokens from text value."""

    return re.findall(rf"[a-z0-9\+#\.]{{{max(1, min_length)},}}", value.casefold())


def _skill_matches(
    resume_skills: list[str],
    corpus: str,
    *,
    min_token_length: int,
    max_output: int,
) -> tuple[list[str], list[str]]:
    """Return matched/unmatched resume skills against LinkedIn text corpus."""

    matched: list[str] = []
    unmatched: list[str] = []
    for raw_skill in resume_skills:
        skill = str(raw_skill).strip()
        if not skill:
            continue
        skill_norm = _normalize_text(skill)
        token_list = _tokens(skill, min_length=min_token_length)
        token_hits = [token for token in token_list if token in corpus]
        is_match = False
        if skill_norm and skill_norm in corpus:
            is_match = True
        elif len(token_list) >= 2 and len(token_hits) >= 2:
            is_match = True
        elif len(token_list) == 1 and len(token_hits) == 1:
            is_match = True

        if is_match:
            matched.append(skill)
        else:
            unmatched.append(skill)
        if len(matched) >= max_output:
            break
    return matched, unmatched[:max_output]


def _name_matches(
    items: list[str],
    corpus: str,
    *,
    min_token_length: int,
    max_output: int,
) -> tuple[list[str], list[str]]:
    """Return matched/unmatched names (company/position) against corpus."""

    matched: list[str] = []
    unmatched: list[str] = []
    for raw in items:
        value = str(raw).strip()
        if not value:
            continue
        norm_value = _normalize_text(value)
        token_list = _tokens(value, min_length=min_token_length)
        token_hits = [token for token in token_list if token in corpus]

        is_match = False
        if norm_value and norm_value in corpus:
            is_match = True
        elif len(token_list) >= 2 and len(token_hits) >= 2:
            is_match = True
        elif len(token_list) == 1 and len(token_hits) == 1:
            is_match = True

        if is_match:
            matched.append(value)
        else:
            unmatched.append(value)

        if len(matched) >= max_output:
            break

    return matched, unmatched[:max_output]


def _collect_resume_lists(
    parse_result: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """Extract resume skills, employers, and positions from parse_result."""

    skills_raw = parse_result.get("skills")
    work_raw = parse_result.get("work_experience")
    resume_skills = (
        [str(item).strip() for item in skills_raw] if isinstance(skills_raw, list) else []
    )

    employers: list[str] = []
    positions: list[str] = []
    if isinstance(work_raw, list):
        for item in work_raw:
            if not isinstance(item, dict):
                continue
            company = item.get("company")
            position = item.get("position")
            if isinstance(company, str) and company.strip():
                employers.append(company.strip())
            if isinstance(position, str) and position.strip():
                positions.append(position.strip())

    # Preserve order but de-duplicate.
    resume_skills = list(dict.fromkeys(resume_skills))
    employers = list(dict.fromkeys(employers))
    positions = list(dict.fromkeys(positions))
    return resume_skills, employers, positions


def _hits_to_evidence(hits: list[dict[str, str]], limit: int) -> list[str]:
    """Convert search hits to short evidence lines."""

    evidence: list[str] = []
    for hit in hits:
        title = hit.get("title", "").strip()
        snippet = hit.get("snippet", "").strip()
        link = hit.get("link", "").strip()
        parts = [part for part in (title, snippet, link) if part]
        if not parts:
            continue
        evidence.append(" | ".join(parts))
        if len(evidence) >= max(1, limit):
            break
    return evidence


def _select_primary_linkedin_hits(
    *,
    hits: list[dict[str, str]],
    linkedin_url: str,
    full_name: str,
) -> tuple[list[dict[str, str]], str | None]:
    """Prefer exact-handle matches, then name-congruent hits."""

    handle = _extract_linkedin_handle(linkedin_url).casefold()
    if handle:
        handle_hits = [hit for hit in hits if f"/in/{handle}" in (hit.get("link", "").casefold())]
        if handle_hits:
            return handle_hits, handle_hits[0].get("link")

    name_tokens = [token for token in _tokens(full_name, min_length=3) if token]
    if name_tokens:
        name_hits = []
        for hit in hits:
            title_snippet = _normalize_text(f"{hit.get('title', '')} {hit.get('snippet', '')}")
            if any(token in title_snippet for token in name_tokens):
                name_hits.append(hit)
        if name_hits:
            first_link = name_hits[0].get("link")
            return name_hits, first_link if isinstance(first_link, str) else None

    first_link = hits[0].get("link") if hits else None
    return hits, first_link if isinstance(first_link, str) else None


async def _load_candidate_context(
    *,
    linkedin_url: str,
    application_id: UUID | None,
) -> CandidateContext:
    """Load candidate row for given application id or LinkedIn URL."""

    runtime_config = get_runtime_config()
    session_factory = get_async_session_factory(runtime_config.postgres)
    handle = _extract_linkedin_handle(linkedin_url).casefold()
    normalized_url = linkedin_url.strip().rstrip("/").casefold()

    async with session_factory() as session:
        if application_id is not None:
            entity = await session.get(ApplicantApplication, application_id)
        else:
            statement = (
                select(ApplicantApplication)
                .where(ApplicantApplication.parse_result.is_not(None))
                .order_by(ApplicantApplication.created_at.desc())
            )
            if handle:
                statement = statement.where(
                    func.lower(ApplicantApplication.linkedin_url).contains(handle)
                )
            else:
                statement = statement.where(
                    func.lower(ApplicantApplication.linkedin_url) == normalized_url
                )
            entity = (await session.execute(statement.limit(1))).scalar_one_or_none()

    if entity is None:
        raise RuntimeError("No candidate found for provided LinkedIn URL/application id")
    if not isinstance(entity.parse_result, dict):
        raise RuntimeError("Candidate parse_result is missing; parse must complete first")

    return CandidateContext(
        id=entity.id,
        full_name=entity.full_name,
        role_selection=entity.role_selection,
        parse_result=entity.parse_result,
        linkedin_url=entity.linkedin_url,
    )


async def _search_linkedin_hits(
    *,
    linkedin_url: str,
    candidate: CandidateContext,
) -> list[dict[str, str]]:
    """Run configured SerpAPI queries and return LinkedIn-domain hits only."""

    runtime_config = get_runtime_config()
    settings = get_settings()
    if not settings.serpapi_api_key:
        raise RuntimeError("SERPAPI_API_KEY is required in .env")
    if not runtime_config.research.enabled:
        raise RuntimeError("research.enabled=false in YAML; enable it first")

    extract_cfg = runtime_config.research.linkedin_extract
    handle = _extract_linkedin_handle(linkedin_url)
    query_context = {
        "linkedin_url": linkedin_url,
        "linkedin_handle": handle,
        "full_name": candidate.full_name,
        "role_selection": candidate.role_selection,
    }
    queries: list[str] = []
    for template in extract_cfg.query_templates:
        query = template.format(**query_context).strip()
        if query:
            queries.append(query)
    queries = list(dict.fromkeys(queries))

    client = SerpApiClient(
        api_key=settings.serpapi_api_key,
        endpoint=runtime_config.research.google_search_url,
        engine=runtime_config.research.engine,
        timeout_seconds=runtime_config.research.request_timeout_seconds,
        max_concurrency=runtime_config.research.max_concurrency,
    )

    all_hits: list[dict[str, str]] = []
    for query in queries:
        payload = await client.search(
            query=query,
            num_results=extract_cfg.results_per_query,
        )
        hits = _extract_hits(payload, max_hits=extract_cfg.max_linkedin_hits)
        for hit in hits:
            link = hit.get("link", "")
            hostname = urlparse(link).hostname or ""
            host = hostname.casefold()
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
        if len(deduped) >= extract_cfg.max_linkedin_hits:
            break
    return deduped


async def _run(
    *,
    linkedin_url: str,
    application_id: UUID | None,
    linkedin_text: str | None,
) -> dict[str, Any]:
    """Execute LinkedIn search and resume cross-reference flow."""

    candidate = await _load_candidate_context(
        linkedin_url=linkedin_url, application_id=application_id
    )
    hits = await _search_linkedin_hits(
        linkedin_url=linkedin_url,
        candidate=candidate,
    )
    extract_cfg = get_runtime_config().research.linkedin_extract
    primary_hits, matched_profile_url = _select_primary_linkedin_hits(
        hits=hits,
        linkedin_url=linkedin_url,
        full_name=candidate.full_name,
    )

    corpus_parts: list[str] = []
    for hit in primary_hits:
        title = hit.get("title", "")
        snippet = hit.get("snippet", "")
        corpus_parts.extend([title, snippet])
    corpus = _normalize_text(" ".join(corpus_parts))

    resume_skills, resume_employers, resume_positions = _collect_resume_lists(
        candidate.parse_result
    )
    matched_skills, unmatched_skills = _skill_matches(
        resume_skills,
        corpus,
        min_token_length=extract_cfg.min_skill_token_length,
        max_output=extract_cfg.max_output_skills,
    )
    matched_employers, unmatched_employers = _name_matches(
        resume_employers,
        corpus,
        min_token_length=extract_cfg.min_position_token_length,
        max_output=extract_cfg.max_output_employers,
    )
    matched_positions, unmatched_positions = _name_matches(
        resume_positions,
        corpus,
        min_token_length=extract_cfg.min_position_token_length,
        max_output=extract_cfg.max_output_positions,
    )

    payload = {
        "candidate_id": str(candidate.id),
        "full_name": candidate.full_name,
        "role_selection": candidate.role_selection,
        "input_linkedin_url": linkedin_url,
        "candidate_linkedin_url_in_db": candidate.linkedin_url,
        "matched_profile_url": matched_profile_url,
        "linkedin_search_hits_count": len(hits),
        "linkedin_primary_hits_count": len(primary_hits),
        "cross_reference": {
            "skills": {
                "resume": resume_skills,
                "matched_on_linkedin": matched_skills,
                "unmatched_from_resume": unmatched_skills,
            },
            "employment_history": {
                "resume_employers": resume_employers,
                "matched_employers_on_linkedin": matched_employers,
                "unmatched_employers_from_resume": unmatched_employers,
                "resume_positions": resume_positions,
                "matched_positions_on_linkedin": matched_positions,
                "unmatched_positions_from_resume": unmatched_positions,
            },
        },
        "evidence": _hits_to_evidence(primary_hits, limit=extract_cfg.max_evidence_lines),
        "raw_linkedin_hits": hits,
    }
    if linkedin_text:
        payload["linkedin_extracted"] = _extract_linkedin_profile_text(linkedin_text)
    return payload


def _build_parser() -> argparse.ArgumentParser:
    """Build script CLI parser."""

    parser = argparse.ArgumentParser(
        description="Search LinkedIn via SerpAPI and cross-reference with candidate parsed resume."
    )
    parser.add_argument(
        "--linkedin-url",
        required=True,
        help="LinkedIn profile URL to search (example: https://www.linkedin.com/in/mrbinit/).",
    )
    parser.add_argument(
        "--application-id",
        default=None,
        help="Optional application UUID. If set, this row is used for resume cross-reference.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print full payload including raw hits and evidence.",
    )
    parser.add_argument(
        "--linkedin-text-file",
        default=None,
        help="Optional path to pasted LinkedIn profile text for section extraction.",
    )
    parser.add_argument(
        "--only-linkedin-extracted",
        action="store_true",
        help="Print only extracted LinkedIn sections when --linkedin-text-file is provided.",
    )
    return parser


def _to_minimal_output(payload: dict[str, Any]) -> dict[str, Any]:
    """Return compact extraction-only output."""

    cross = payload.get("cross_reference")
    skills = cross.get("skills", {}) if isinstance(cross, dict) else {}
    employment = cross.get("employment_history", {}) if isinstance(cross, dict) else {}
    output = {
        "matched_profile_url": payload.get("matched_profile_url"),
        "candidate": payload.get("full_name"),
        "resume_cross_reference": {
            "matched_employers": employment.get("matched_employers_on_linkedin", []),
            "matched_positions": employment.get("matched_positions_on_linkedin", []),
            "unmatched_employers_from_resume": employment.get(
                "unmatched_employers_from_resume", []
            ),
            "matched_skills": skills.get("matched_on_linkedin", []),
        },
    }
    if isinstance(payload.get("linkedin_extracted"), dict):
        output["linkedin_extracted"] = payload["linkedin_extracted"]
    return output


def main() -> None:
    """Entrypoint for `python -m app.scripts.extract_linkedin_cross_reference`."""

    parser = _build_parser()
    args = parser.parse_args()
    application_id = UUID(args.application_id) if args.application_id else None
    linkedin_text: str | None = None
    if args.linkedin_text_file:
        linkedin_text = Path(args.linkedin_text_file).read_text(encoding="utf-8")
    if args.only_linkedin_extracted and linkedin_text:
        print(json.dumps(_extract_linkedin_profile_text(linkedin_text), indent=2))
        return
    payload = asyncio.run(
        _run(
            linkedin_url=args.linkedin_url,
            application_id=application_id,
            linkedin_text=linkedin_text,
        )
    )
    if args.only_linkedin_extracted:
        extracted = payload.get("linkedin_extracted")
        if not isinstance(extracted, dict):
            raise RuntimeError(
                "--only-linkedin-extracted requires --linkedin-text-file with valid profile text"
            )
        print(json.dumps(extracted, indent=2))
        return
    if args.verbose:
        print(json.dumps(payload, indent=2))
    else:
        print(json.dumps(_to_minimal_output(payload), indent=2))


if __name__ == "__main__":
    main()
