"""Heuristic structured extraction from parsed resume text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any


@dataclass(frozen=True)
class StructuredResumeData:
    """Structured fields extracted from a resume."""

    skills: list[str]
    projects: list[str]
    position: str | None
    work_history: list[dict[str, Any]]
    total_years_experience: float | None
    education: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """Convert to JSON-ready dictionary."""

        return {
            "skills": self.skills,
            "projects": self.projects,
            "position": self.position,
            "work_history": self.work_history,
            "total_years_experience": self.total_years_experience,
            "education": self.education,
        }


class ResumeStructuredExtractor:
    """Extract section-based structured entities from plain resume text."""

    _YEARS_RE = re.compile(
        r"(?P<a>\d{1,2})(?:\s*(?:-|to)\s*(?P<b>\d{1,2}))?\s*(?:\+)?\s*(?:years?|yrs?)",
        re.IGNORECASE,
    )
    _DATE_RANGE_RE = re.compile(
        r"(?P<start>(?:[A-Za-z]{3,9}\s+)?\d{4}|\d{1,2}[/-]\d{4})\s*"
        r"(?:-|–|—|to)\s*"
        r"(?P<end>(?:[A-Za-z]{3,9}\s+)?\d{4}|\d{1,2}[/-]\d{4}|present|current|now)",
        re.IGNORECASE,
    )
    _ROLE_HINTS = (
        "engineer",
        "developer",
        "intern",
        "manager",
        "lead",
        "architect",
        "analyst",
        "consultant",
        "specialist",
        "scientist",
        "designer",
        "director",
        "officer",
        "coordinator",
        "administrator",
        "assistant",
        "qa",
        "sre",
        "devops",
        "founder",
    )
    _GENERIC_HEADING_RE = re.compile(r"^[A-Za-z][A-Za-z&/\- ]{1,40}$")
    _HEADING_KEYWORDS = {
        "summary",
        "profile",
        "objective",
        "skills",
        "experience",
        "employment",
        "work",
        "projects",
        "project",
        "education",
        "certifications",
        "awards",
        "languages",
        "contact",
        "activities",
        "publications",
        "references",
        "referrals",
    }
    _DEGREE_HINTS = (
        "bachelor",
        "master",
        "phd",
        "doctor",
        "b.sc",
        "m.sc",
        "bs",
        "ms",
        "mba",
        "diploma",
        "certificate",
    )
    _INSTITUTION_HINTS = (
        "university",
        "college",
        "campus",
        "school",
        "institute",
        "academy",
    )
    _MONTH_MAP = {
        "jan": 1,
        "january": 1,
        "feb": 2,
        "february": 2,
        "mar": 3,
        "march": 3,
        "apr": 4,
        "april": 4,
        "may": 5,
        "jun": 6,
        "june": 6,
        "jul": 7,
        "july": 7,
        "aug": 8,
        "august": 8,
        "sep": 9,
        "sept": 9,
        "september": 9,
        "oct": 10,
        "october": 10,
        "nov": 11,
        "november": 11,
        "dec": 12,
        "december": 12,
    }

    def __init__(
        self,
        *,
        section_aliases: dict[str, list[str]],
        link_rules: dict[str, list[str]],
        max_section_lines: int = 40,
    ):
        """Initialize extractor with section heading aliases and length guard."""

        _ = link_rules
        self._max_section_lines = max(10, max_section_lines)
        self._section_aliases = {
            key: {" ".join(alias.lower().replace(":", " ").split()) for alias in aliases}
            for key, aliases in section_aliases.items()
        }

    def extract(self, *, text: str, fallback_name: str | None = None) -> StructuredResumeData:
        """Extract structured information from raw resume text."""

        _ = fallback_name
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        sections = self._collect_sections(lines)

        skills = self._extract_skills_from_section(sections.get("skills", []))
        projects = sections.get("projects", [])[: self._max_section_lines]
        work_history = self._extract_work_history(
            experience_lines=sections.get("experience", []),
            all_lines=lines,
        )
        position = self._pick_latest_position(work_history) or self._infer_position_from_lines(
            sections.get("experience", []) or lines[:120]
        )
        total_years = self._calculate_total_years_experience(
            work_history=work_history,
            fallback_text="\n".join(sections.get("experience", [])) or text,
        )
        education = self._extract_education(
            education_lines=sections.get("education", []),
            all_lines=lines,
        )

        return StructuredResumeData(
            skills=skills,
            projects=projects,
            position=position,
            work_history=work_history,
            total_years_experience=total_years,
            education=education,
        )

    def _extract_skills_from_section(self, section_lines: list[str]) -> list[str]:
        """Extract skills from skills heading section only."""

        values: list[str] = []
        seen: set[str] = set()

        for line in section_lines[: self._max_section_lines]:
            cleaned = self._clean_prefix(line)
            chunks = re.split(r"[,;|•·]+", cleaned)
            for chunk in chunks:
                item = " ".join(chunk.split()).strip()
                if not item:
                    continue
                if len(item) > 50:
                    continue
                normalized = item.casefold()
                if normalized in seen:
                    continue
                seen.add(normalized)
                values.append(item)
        return values

    def _extract_work_history(
        self,
        *,
        experience_lines: list[str],
        all_lines: list[str],
    ) -> list[dict[str, Any]]:
        """Extract previous work entries with role, company, and date range."""

        source_lines = experience_lines[: self._max_section_lines] or all_lines[:200]
        entries: list[dict[str, Any]] = []
        seen_keys: set[tuple[str, str, str]] = set()

        for index, raw_line in enumerate(source_lines):
            line = self._clean_prefix(raw_line)
            if not line:
                continue

            for match in self._DATE_RANGE_RE.finditer(line):
                start_token = match.group("start")
                end_token = match.group("end")
                start_date = self._parse_date_token(start_token, is_end=False)
                end_date = self._parse_date_token(end_token, is_end=True)
                if start_date is None or end_date is None or end_date < start_date:
                    continue

                context, context_index = self._resolve_experience_context(
                    source_lines=source_lines,
                    index=index,
                    line=line,
                    match=match,
                )
                position, company = self._extract_position_and_company(context)
                if company is None:
                    company = self._infer_company_nearby(
                        source_lines=source_lines,
                        date_index=index,
                        context_index=context_index,
                        line=line,
                        date_match=match,
                        position=position,
                    )
                if position is None and not self._is_plausible_company(company):
                    continue
                duration_years = self._years_between(start_date, end_date)

                key = (
                    position or "",
                    company or "",
                    f"{start_date.isoformat()}:{end_date.isoformat()}",
                )
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                entries.append(
                    {
                        "position": position,
                        "company": company,
                        "start_date": start_date.isoformat(),
                        "end_date": end_date.isoformat(),
                        "duration_years": duration_years,
                    }
                )

        entries.sort(key=lambda item: item["end_date"], reverse=True)
        return entries[: self._max_section_lines]

    def _extract_position_and_company(self, context: str) -> tuple[str | None, str | None]:
        """Infer position and company from one experience line."""

        cleaned = " ".join(context.replace("\t", " ").split()).strip(" -|,:")
        cleaned = re.split(r"[•·]", cleaned, maxsplit=1)[0].strip(" -|,:")
        if not cleaned:
            return None, None

        lower = cleaned.lower()
        if " at " in lower:
            before, after = re.split(r"\bat\b", cleaned, maxsplit=1, flags=re.IGNORECASE)
            return self._normalize_text(before), self._normalize_text(after)

        for delimiter in (" | ", " - ", ", "):
            if delimiter in cleaned:
                left, right = cleaned.split(delimiter, 1)
                left_value = self._normalize_text(left)
                right_value = self._normalize_text(right)
                if self._looks_like_position(left_value):
                    return left_value, right_value
                if self._looks_like_position(right_value):
                    return right_value, left_value
                left_company = left_value if self._is_plausible_company(left_value) else None
                right_company = right_value if self._is_plausible_company(right_value) else None
                if left_company and not right_company:
                    return None, left_company
                if right_company and not left_company:
                    return None, right_company
                if left_company and right_company:
                    return None, left_company
                return None, None

        split_position, split_company = self._split_position_and_company_by_role_hint(cleaned)
        if split_position or split_company:
            return split_position, split_company

        normalized = self._normalize_text(cleaned)
        if self._looks_like_position(normalized):
            return normalized, None
        if self._is_plausible_company(normalized):
            return None, normalized
        return None, None

    def _calculate_total_years_experience(
        self,
        *,
        work_history: list[dict[str, Any]],
        fallback_text: str,
    ) -> float | None:
        """Compute total years from date ranges; fallback to year mentions."""

        intervals: list[tuple[date, date]] = []
        for row in work_history:
            try:
                start_date = date.fromisoformat(str(row["start_date"]))
                end_date = date.fromisoformat(str(row["end_date"]))
            except (TypeError, ValueError, KeyError):
                continue
            if end_date >= start_date:
                intervals.append((start_date, end_date))

        if intervals:
            return self._sum_non_overlapping_years(intervals)

        mentions: list[int] = []
        for match in self._YEARS_RE.finditer(fallback_text):
            left = int(match.group("a"))
            right = match.group("b")
            mentions.append(max(left, int(right)) if right else left)
        if mentions:
            return float(max(mentions))
        return None

    @staticmethod
    def _sum_non_overlapping_years(intervals: list[tuple[date, date]]) -> float:
        """Merge overlapping month-ranges and return total years."""

        month_spans: list[tuple[int, int]] = []
        for start_date, end_date in intervals:
            start_key = start_date.year * 12 + (start_date.month - 1)
            end_key = end_date.year * 12 + (end_date.month - 1)
            month_spans.append((start_key, end_key))

        month_spans.sort(key=lambda item: item[0])
        merged: list[tuple[int, int]] = []
        for start_key, end_key in month_spans:
            if not merged:
                merged.append((start_key, end_key))
                continue
            prev_start, prev_end = merged[-1]
            if start_key <= prev_end + 1:
                merged[-1] = (prev_start, max(prev_end, end_key))
            else:
                merged.append((start_key, end_key))

        total_months = sum((end_key - start_key + 1) for start_key, end_key in merged)
        return round(total_months / 12.0, 2)

    def _pick_latest_position(self, work_history: list[dict[str, Any]]) -> str | None:
        """Return most recent non-empty position from extracted work history."""

        for row in work_history:
            value = row.get("position")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _parse_date_token(self, token: str, *, is_end: bool) -> date | None:
        """Parse resume date token into a date object."""

        normalized = " ".join(token.strip().split()).lower()
        now = datetime.now(tz=timezone.utc).date()
        if normalized in {"present", "current", "now"}:
            return now

        month = 1
        year: int | None = None

        if re.fullmatch(r"\d{4}", normalized):
            year = int(normalized)
            month = 12 if is_end else 1
        else:
            mm_yyyy = re.fullmatch(r"(?P<m>\d{1,2})[/-](?P<y>\d{4})", normalized)
            if mm_yyyy:
                month = int(mm_yyyy.group("m"))
                year = int(mm_yyyy.group("y"))
            else:
                parts = normalized.split()
                if len(parts) == 2 and parts[0] in self._MONTH_MAP and parts[1].isdigit():
                    month = self._MONTH_MAP[parts[0]]
                    year = int(parts[1])

        if year is None:
            return None
        if not (1 <= month <= 12):
            return None
        return date(year=year, month=month, day=1)

    def _collect_sections(self, lines: list[str]) -> dict[str, list[str]]:
        """Build section map from heading lines."""

        sections: dict[str, list[str]] = {}
        current: str | None = None

        for line in lines:
            heading = self._resolve_section_heading(line)
            if heading is not None:
                current = heading
                sections.setdefault(heading, [])
                continue
            if self._looks_like_heading_line(line):
                current = None
                continue
            if current is None:
                continue
            target = sections.setdefault(current, [])
            if len(target) >= self._max_section_lines:
                continue
            target.append(self._clean_prefix(line))
        return sections

    def _resolve_section_heading(self, line: str) -> str | None:
        """Return canonical section key when line looks like a heading."""

        normalized = " ".join(line.lower().replace(":", " ").split())
        if not normalized:
            return None
        if len(normalized.split()) > 5:
            return None
        if "http://" in normalized or "https://" in normalized:
            return None
        if "@" in normalized:
            return None

        for section, aliases in self._section_aliases.items():
            for alias in aliases:
                if normalized == alias:
                    return section
        return None

    @staticmethod
    def _clean_prefix(line: str) -> str:
        """Remove common bullet/list prefixes from line."""

        return re.sub(r"^(?:[\-\*\u2022]+|\d+[\.\)])\s*", "", line).strip()

    def _resolve_experience_context(
        self,
        *,
        source_lines: list[str],
        index: int,
        line: str,
        match: re.Match[str],
    ) -> tuple[str, int]:
        """Resolve best role/company context around a matched date range."""

        left = self._clean_prefix(line[: match.start()])
        left = re.split(r"[•·]", left, maxsplit=1)[0].strip(" -|,:")
        left_position, _ = self._extract_position_and_company(left)
        if left_position:
            return left, index

        best_company_context: str | None = left if self._is_context_candidate(left) else None
        best_company_index = index
        for back in range(1, 4):
            prev_index = index - back
            if prev_index < 0:
                break
            candidate = self._clean_prefix(source_lines[prev_index])
            if not candidate:
                continue
            if self._DATE_RANGE_RE.search(candidate):
                continue
            if self._looks_like_heading_line(candidate):
                continue
            candidate = re.split(r"[•·]", candidate, maxsplit=1)[0].strip(" -|,:")
            position, _ = self._extract_position_and_company(candidate)
            if position:
                return candidate, prev_index
            if best_company_context is None and self._is_context_candidate(candidate):
                best_company_context = candidate
                best_company_index = prev_index

        for back in range(4, 13):
            prev_index = index - back
            if prev_index < 0:
                break
            candidate = self._clean_prefix(source_lines[prev_index])
            if not candidate:
                continue
            if self._DATE_RANGE_RE.search(candidate):
                break
            if self._looks_like_heading_line(candidate):
                break
            candidate = re.split(r"[•·]", candidate, maxsplit=1)[0].strip(" -|,:")
            position, _ = self._extract_position_and_company(candidate)
            if position:
                return candidate, prev_index

        return best_company_context or left, best_company_index

    def _is_context_candidate(self, value: str) -> bool:
        """Return True when text looks like role/company context, not bullet prose."""

        if not value:
            return False
        if self._looks_like_sentence(value):
            return False
        if self._looks_like_position(value):
            return True
        return self._is_plausible_company(value)

    def _looks_like_heading_line(self, line: str) -> bool:
        """Return True if line appears to be a section heading."""

        normalized = " ".join(line.replace(":", " ").split()).strip()
        if not normalized:
            return False
        if len(normalized.split()) > 4:
            return False
        if "@" in normalized or "http://" in normalized.lower() or "https://" in normalized.lower():
            return False
        if not self._GENERIC_HEADING_RE.fullmatch(normalized):
            return False
        tokens = {token.casefold() for token in normalized.split()}
        return any(token in self._HEADING_KEYWORDS for token in tokens)

    def _split_position_and_company_by_role_hint(self, text: str) -> tuple[str | None, str | None]:
        """Split line into position/company when role keyword appears mid-line."""

        words = [word for word in text.split() if word]
        if len(words) < 2:
            return None, None

        role_end_index = -1
        role_set = {hint.casefold() for hint in self._ROLE_HINTS}
        for index, word in enumerate(words):
            token = re.sub(r"[^a-z]", "", word.casefold())
            if token in role_set:
                role_end_index = index

        if role_end_index == -1:
            return None, None

        position = self._normalize_text(" ".join(words[: role_end_index + 1]))
        company = self._normalize_text(" ".join(words[role_end_index + 1 :]))
        if company and self._looks_like_sentence(company):
            company = None
        return position, company

    @staticmethod
    def _looks_like_sentence(value: str | None) -> bool:
        """Return True when text resembles prose sentence instead of entity name."""

        if not value:
            return False
        words = value.split()
        if len(words) >= 8:
            return True
        lower = value.casefold()
        sentence_markers = (" and ", " with ", " through ", " to ", " for ", " by ")
        return any(marker in lower for marker in sentence_markers) and len(words) >= 4

    def _is_plausible_company(self, value: str | None) -> bool:
        """Return True when text likely represents an employer name."""

        if not value:
            return False
        normalized = value.strip(" ,.;:|")
        words = normalized.split()
        if len(words) > 5:
            return False
        if self._looks_like_sentence(normalized):
            return False
        if not any(char.isupper() for char in normalized if char.isalpha()):
            return False
        stopwords = {"and", "or", "for", "with", "through", "using", "supporting"}
        if any(word.casefold() in stopwords for word in words):
            return False
        if self._is_location_like(normalized):
            return False
        return True

    def _looks_like_position(self, value: str | None) -> bool:
        """Return True if text appears to be a job title."""

        if not value:
            return False
        tokens = re.findall(r"[a-z]+", value.casefold())
        role_set = {hint.casefold() for hint in self._ROLE_HINTS}
        return any(token in role_set for token in tokens)

    @staticmethod
    def _normalize_text(value: str) -> str | None:
        """Normalize and clean a short text token."""

        cleaned = " ".join(value.strip().split()).strip(" -|,:")
        return cleaned or None

    def _infer_company_nearby(
        self,
        *,
        source_lines: list[str],
        date_index: int,
        context_index: int,
        line: str,
        date_match: re.Match[str],
        position: str | None,
    ) -> str | None:
        """Infer company from neighboring lines when context contains only role."""

        candidates: list[str] = []

        inline = self._extract_company_candidates_from_line(
            re.sub(self._DATE_RANGE_RE, "", line, count=1).strip(" -|,:"),
            position=position,
        )
        candidates.extend(inline)

        for idx in (
            context_index + 1,
            context_index - 1,
            date_index - 1,
            date_index + 1,
            date_index + 2,
        ):
            if idx < 0 or idx >= len(source_lines):
                continue
            if idx == date_index and date_match:
                continue
            raw = self._clean_prefix(source_lines[idx])
            if not raw:
                continue
            if self._DATE_RANGE_RE.search(raw):
                continue
            if self._looks_like_heading_line(raw):
                continue
            candidates.extend(self._extract_company_candidates_from_line(raw, position=position))

        seen: set[str] = set()
        for candidate in candidates:
            normalized = candidate.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            return candidate
        return None

    def _extract_company_candidates_from_line(
        self,
        line: str,
        *,
        position: str | None,
    ) -> list[str]:
        """Extract probable company names from one line."""

        parts = [segment.strip() for segment in re.split(r"[|]", line) if segment.strip()]
        if not parts:
            parts = [line]

        candidates: list[str] = []
        for part in parts:
            cleaned = re.sub(
                r"\b(?:part\s*time|full\s*time|contract|internship|freelance)\b",
                "",
                part,
                flags=re.IGNORECASE,
            ).strip(" -|,:")
            if not cleaned:
                continue
            if position and cleaned.casefold() == position.casefold():
                continue
            if self._looks_like_position(cleaned):
                continue
            if not self._is_plausible_company(cleaned):
                continue
            candidates.append(cleaned)
        return candidates

    @staticmethod
    def _is_location_like(value: str) -> bool:
        """Return True when token likely represents location rather than company."""

        lower = value.casefold()
        location_markers = (
            "remote",
            "kathmandu",
            "nepal",
            "usa",
            "us",
            "uk",
            "india",
            "canada",
        )
        if any(marker in lower.split() for marker in location_markers):
            return True
        if "," in lower and len(lower.split()) >= 2:
            return True
        return False

    def _infer_position_from_lines(self, lines: list[str]) -> str | None:
        """Fallback position inference from raw lines when work history is noisy."""

        for raw_line in lines[:50]:
            line = self._clean_prefix(raw_line)
            if not line:
                continue
            head = re.split(r"[•·|,]", line, maxsplit=1)[0].strip()
            position, _ = self._extract_position_and_company(head)
            if position:
                return position
        return None

    def _extract_education(
        self,
        *,
        education_lines: list[str],
        all_lines: list[str],
    ) -> list[dict[str, Any]]:
        """Extract degree and institution entries from education section."""

        source = education_lines[: self._max_section_lines]
        if not source:
            lines = [line.strip() for line in all_lines]
            start = None
            for idx, line in enumerate(lines):
                if self._resolve_section_heading(line) == "education":
                    start = idx + 1
                    break
            if start is not None:
                source = lines[start : start + self._max_section_lines]

        results: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None

        for raw in source:
            line = self._clean_prefix(raw)
            if not line:
                continue
            if self._looks_like_heading_line(line):
                continue

            lower = line.casefold()
            has_degree = any(hint in lower for hint in self._DEGREE_HINTS)
            has_institution = any(hint in lower for hint in self._INSTITUTION_HINTS)

            if has_degree:
                if current:
                    results.append(current)
                current = {"degree": line, "institution": None}
                continue

            if has_institution:
                if current is None:
                    current = {"degree": None, "institution": line}
                elif current.get("institution") is None:
                    current["institution"] = line
                else:
                    results.append(current)
                    current = {"degree": None, "institution": line}
                continue

            if current and "year_range" not in current and self._DATE_RANGE_RE.search(line):
                current["year_range"] = line

        if current:
            results.append(current)
        return results[: self._max_section_lines]

    @staticmethod
    def _years_between(start_date: date, end_date: date) -> float:
        """Return duration in years between two dates."""

        months = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month) + 1
        return round(max(0, months) / 12.0, 2)
