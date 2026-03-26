"""Tests for LinkedIn profile extractor helper functions."""

from app.scripts.extract_linkedin_profile import _build_search_only_output
from app.scripts.extract_linkedin_profile import _search_only_query_context


def test_search_only_query_context_builds_expected_fields() -> None:
    """Context builder should derive LinkedIn handle and preserve hints."""

    context = _search_only_query_context(
        linkedin_url="https://www.linkedin.com/in/mrbinit/",
        full_name="Binit Sapkota",
        role_selection="Backend Engineer",
    )
    assert context["linkedin_handle"] == "mrbinit"
    assert context["full_name"] == "Binit Sapkota"
    assert context["role_selection"] == "Backend Engineer"


def test_build_search_only_output_picks_handle_match() -> None:
    """Output builder should prioritize exact handle profile match."""

    hits = [
        {
            "link": "https://www.linkedin.com/in/otherperson",
            "title": "Other Person",
            "snippet": "Random",
        },
        {
            "link": "https://np.linkedin.com/in/mrbinit",
            "title": "Binit Sapkota",
            "snippet": "AI Engineer",
        },
    ]
    payload = _build_search_only_output(
        linkedin_url="https://www.linkedin.com/in/mrbinit/",
        full_name="Binit Sapkota",
        role_selection="Backend Engineer",
        hits=hits,
    )
    assert payload["mode"] == "search_only"
    assert payload["matched_profile_url"] == "https://np.linkedin.com/in/mrbinit"
    assert payload["linkedin_search_hits_count"] == 2
