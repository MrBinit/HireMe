"""Email sender abstractions for application notifications."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class EmailSendError(RuntimeError):
    """Raised when an email notification cannot be delivered."""


@dataclass(frozen=True)
class ApplicationConfirmationEmail:
    """Payload for application confirmation email."""

    candidate_name: str
    candidate_email: str
    role_title: str


@dataclass(frozen=True)
class InitialScreeningRejectionEmail:
    """Payload for initial-screening rejection email."""

    candidate_name: str
    candidate_email: str
    role_title: str
    rejection_reason: str


class EmailSender(ABC):
    """Abstract email sender contract."""

    @abstractmethod
    async def send_application_confirmation(
        self,
        payload: ApplicationConfirmationEmail,
    ) -> None:
        """Send application-submitted confirmation email."""

        raise NotImplementedError

    @abstractmethod
    async def send_initial_screening_rejection(
        self,
        payload: InitialScreeningRejectionEmail,
    ) -> None:
        """Send initial-screening rejection email."""

        raise NotImplementedError


class NoopEmailSender(EmailSender):
    """No-op sender used when email is disabled or not configured."""

    async def send_application_confirmation(
        self,
        payload: ApplicationConfirmationEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_initial_screening_rejection(
        self,
        payload: InitialScreeningRejectionEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload
