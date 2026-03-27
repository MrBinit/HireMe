"""API routes for Slack onboarding integration callbacks."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import get_application_service_dep
from app.services.application_service import ApplicationService, ApplicationValidationError

router = APIRouter(tags=["slack"])


@router.post(
    "/integrations/slack/events",
    status_code=status.HTTP_200_OK,
)
async def slack_events_callback(
    request: Request,
    service: ApplicationService = Depends(get_application_service_dep),
) -> dict[str, object]:
    """Handle Slack Events API callbacks for onboarding flow."""

    try:
        result = await service.handle_slack_webhook(
            raw_body=await request.body(),
            headers=dict(request.headers.items()),
        )
    except ApplicationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    challenge = result.get("challenge")
    if isinstance(challenge, str) and challenge.strip():
        return {"challenge": challenge}
    return {"processed": bool(result.get("processed"))}
