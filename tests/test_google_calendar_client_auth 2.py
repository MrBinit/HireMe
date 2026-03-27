"""Auth-mode resolution tests for Google Calendar client."""

from app.infra.google_calendar_client import GoogleCalendarAuthError, GoogleCalendarClient


def test_missing_service_account_file_falls_back_to_oauth_credentials() -> None:
    """When file path is invalid but OAuth creds exist, use OAuth mode."""

    client = GoogleCalendarClient(
        service_account_json=None,
        service_account_file="/tmp/does-not-exist-service-account.json",
        oauth_client_id="client-id",
        oauth_client_secret="client-secret",
        oauth_refresh_token="refresh-token",
    )

    assert client._auth_mode == "oauth_refresh_token"


def test_missing_service_account_file_raises_without_oauth_credentials() -> None:
    """When file path is invalid and OAuth creds are absent, raise config error."""

    error = None
    try:
        GoogleCalendarClient(
            service_account_json=None,
            service_account_file="/tmp/does-not-exist-service-account.json",
            oauth_client_id=None,
            oauth_client_secret=None,
            oauth_refresh_token=None,
        )
    except GoogleCalendarAuthError as exc:
        error = exc

    assert error is not None
    assert "GOOGLE_SERVICE_ACCOUNT_FILE not found" in str(error)
