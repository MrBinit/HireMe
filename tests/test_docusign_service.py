"""Tests for DocuSign service configuration helpers."""

from __future__ import annotations

import os
from pathlib import Path

from app.services.docusign_service import DocusignService


def test_resolve_private_key_supports_local_fallback_from_app_path(tmp_path: Path) -> None:
    """Resolve /app/... key path via workspace-local fallback in local runs."""

    key_file = tmp_path / "docusign-private.key"
    key_file.write_text("PRIVATE_KEY_CONTENT", encoding="utf-8")
    previous_cwd = Path.cwd()
    try:
        # emulate running from project root where key sits locally
        os.chdir(tmp_path)
        value = DocusignService._resolve_private_key(
            private_key=None,
            private_key_path="/app/docusign-private.key",
        )
    finally:
        os.chdir(previous_cwd)

    assert value == "PRIVATE_KEY_CONTENT"


def test_resolve_private_key_prefers_inline_value() -> None:
    """Inline key takes precedence over any file path lookup."""

    value = DocusignService._resolve_private_key(
        private_key="line1\\nline2",
        private_key_path="/app/docusign-private.key",
    )
    assert value == "line1\nline2"
