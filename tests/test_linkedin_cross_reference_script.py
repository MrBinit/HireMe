"""Tests for LinkedIn cross-reference helper functions."""

from app.scripts.extract_linkedin_cross_reference import (
    _collect_resume_lists,
    _extract_linkedin_handle,
    _extract_linkedin_profile_text,
    _name_matches,
    _select_primary_linkedin_hits,
    _skill_matches,
)


def test_extract_linkedin_handle_parses_profile_url() -> None:
    """Handle extractor should parse /in/<handle> path."""

    assert _extract_linkedin_handle("https://www.linkedin.com/in/mrbinit/") == "mrbinit"


def test_collect_resume_lists_reads_skills_employers_positions() -> None:
    """Collector should read expected lists from parse_result dict."""

    parse_result = {
        "skills": ["Python", "FastAPI"],
        "work_experience": [
            {"company": "Acme", "position": "Backend Engineer"},
            {"company": "Beta", "position": "ML Engineer"},
        ],
    }
    skills, employers, positions = _collect_resume_lists(parse_result)
    assert skills == ["Python", "FastAPI"]
    assert employers == ["Acme", "Beta"]
    assert positions == ["Backend Engineer", "ML Engineer"]


def test_skill_matches_detects_tokens_in_corpus() -> None:
    """Skill matching should identify overlapping skills."""

    matched, unmatched = _skill_matches(
        ["Python", "FastAPI", "Kubernetes"],
        "experienced in python fastapi services and api design",
        min_token_length=3,
        max_output=10,
    )
    assert "Python" in matched
    assert "FastAPI" in matched
    assert "Kubernetes" in unmatched


def test_name_matches_detects_employer_overlap() -> None:
    """Name matching should detect employer mentions in corpus."""

    matched, unmatched = _name_matches(
        ["Wiseyak", "Qualz.AI"],
        "worked at wiseyak and led backend projects",
        min_token_length=3,
        max_output=10,
    )
    assert "Wiseyak" in matched
    assert "Qualz.AI" in unmatched


def test_select_primary_linkedin_hits_prefers_exact_handle() -> None:
    """Primary hit selector should prefer exact LinkedIn handle matches."""

    hits = [
        {"link": "https://www.linkedin.com/in/binitp", "title": "Other", "snippet": ""},
        {"link": "https://np.linkedin.com/in/mrbinit", "title": "Binit Sapkota", "snippet": ""},
    ]
    selected, profile = _select_primary_linkedin_hits(
        hits=hits,
        linkedin_url="https://www.linkedin.com/in/mrbinit/",
        full_name="Binit Sapkota",
    )
    assert profile == "https://np.linkedin.com/in/mrbinit"
    assert len(selected) == 1


def test_extract_linkedin_profile_text_parses_required_sections() -> None:
    """Profile parser should extract experience, education, certifications, projects, and skills."""

    raw_text = """
Experience
Qualz logo
AI Engineer
Qualz · Full-time
Jul 2025 - Present · 9 mos
Ohio, United States · Remote
• Built production LLM evaluation platform

Education
Westcliff University logo
Westcliff University
BSIT, Data Science
2020 – 2025
Grade: 3.75 / 4.0

Licenses & certifications
DataCamp logo
AI Engineer for Developers Associate
DataCamp
Issued Mar 2026 · Expires Mar 2028
Credential ID AIEDA0014302046377
Python (Programming Language) and Large Language Models (LLM)

Projects
HR Genie
Aug 2025 – Present
Built end-to-end resume parsing and candidate scoring.
Large Language Models (LLM), Google API and +1 skill

Skills
Google API
PostgreSQL

Interests
Top Voices
"""

    extracted = _extract_linkedin_profile_text(raw_text)
    assert extracted["experience"]
    assert extracted["experience"][0]["company"] == "Qualz"
    assert extracted["education"]
    assert extracted["education"][0]["institution"] == "Westcliff University"
    assert extracted["licenses_and_certifications"]
    assert extracted["licenses_and_certifications"][0]["issuer"] == "DataCamp"
    assert extracted["projects"]
    assert extracted["projects"][0]["name"] == "HR Genie"
    assert "Google API" in extracted["skills"]
