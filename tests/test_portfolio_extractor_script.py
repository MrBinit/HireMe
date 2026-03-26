"""Tests for portfolio extractor helper functions."""

from app.scripts.extract_portfolio_profile import _build_queries
from app.scripts.extract_portfolio_profile import _pick_primary_hits
from app.scripts.extract_portfolio_profile import _portfolio_domain


def test_portfolio_domain_normalizes_www_prefix() -> None:
    """Domain parser should normalize hostname consistently."""

    assert _portfolio_domain("https://www.flowcv.me/mrbinitsapkota") == "flowcv.me"
    assert _portfolio_domain("https://flowcv.me/mrbinitsapkota") == "flowcv.me"


def test_build_queries_includes_url_domain_name_and_role() -> None:
    """Query builder should include direct URL and site-based variants."""

    queries = _build_queries(
        portfolio_url="https://flowcv.me/mrbinitsapkota",
        full_name="Binit Sapkota",
        role_selection="AI Engineer",
    )
    assert '"https://flowcv.me/mrbinitsapkota"' in queries
    assert "site:flowcv.me" in queries
    assert 'site:flowcv.me "Binit Sapkota" "AI Engineer"' in queries


def test_pick_primary_hits_prefers_exact_portfolio_url() -> None:
    """Primary hit selector should prioritize exact URL match."""

    hits = [
        {"link": "https://flowcv.me/another", "title": "Another", "snippet": ""},
        {"link": "https://flowcv.me/mrbinitsapkota", "title": "Binit", "snippet": ""},
    ]
    primary, matched = _pick_primary_hits(
        hits=hits,
        portfolio_url="https://flowcv.me/mrbinitsapkota",
        max_items=4,
    )
    assert matched == "https://flowcv.me/mrbinitsapkota"
    assert len(primary) == 1
