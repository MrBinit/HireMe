"""Tests for heading-based resume structured extractor."""

from __future__ import annotations

from app.services.resume_structured_extractor import ResumeStructuredExtractor


def _extractor() -> ResumeStructuredExtractor:
    """Build extractor with test section aliases."""

    return ResumeStructuredExtractor(
        section_aliases={
            "skills": ["skills", "technical skills"],
            "experience": ["experience", "work experience"],
            "projects": ["projects"],
            "contact": ["contact"],
            "education": ["education"],
        },
        link_rules={},
        max_section_lines=50,
    )


def test_extract_skills_projects_and_experience_fields() -> None:
    """Extractor should return only required fields and compute work history."""

    text = """
    Skills
    Python, FastAPI, PostgreSQL

    Experience
    Senior Backend Engineer at Acme Corp | Jan 2021 - Mar 2023
    Backend Engineer, Beta Labs | Apr 2023 - Present

    Projects
    Hiring API Platform
    Resume Parser Pipeline
    """

    structured = _extractor().extract(text=text).to_dict()

    assert set(structured.keys()) == {
        "skills",
        "projects",
        "position",
        "work_history",
        "total_years_experience",
        "education",
    }
    assert "Python" in structured["skills"]
    assert "FastAPI" in structured["skills"]
    assert structured["projects"] == ["Hiring API Platform", "Resume Parser Pipeline"]
    assert structured["position"] in {"Backend Engineer", "Senior Backend Engineer"}
    assert len(structured["work_history"]) >= 2
    assert isinstance(structured["total_years_experience"], float)
    assert structured["total_years_experience"] >= 3.0
    assert structured["education"] == []


def test_extract_total_experience_from_year_mentions_when_dates_absent() -> None:
    """Extractor should fallback to years mention when no date ranges are present."""

    text = """
    Skills
    Python

    Experience
    4+ years of backend engineering experience.

    Projects
    API Gateway Refactor
    """

    structured = _extractor().extract(text=text).to_dict()
    assert structured["total_years_experience"] == 4.0


def test_extract_education_entries() -> None:
    """Extractor should capture degree and institution from education section."""

    text = """
    Education
    Bachelor of Information Technology
    Kathmandu University
    2019 - 2023
    """

    structured = _extractor().extract(text=text).to_dict()
    assert len(structured["education"]) == 1
    assert structured["education"][0]["degree"] == "Bachelor of Information Technology"
    assert structured["education"][0]["institution"] == "Kathmandu University"
