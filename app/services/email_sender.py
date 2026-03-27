"""Email sender abstractions for application notifications."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


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


@dataclass(frozen=True)
class InterviewSlotOptionsEmail:
    """Payload for shortlisted candidate interview slot options email."""

    candidate_name: str
    candidate_email: str
    role_title: str
    hold_expires_at: str
    slot_options: list[str]
    slot_option_links: list[tuple[str, str]] = field(default_factory=list)
    action_links: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class InterviewBookingConfirmedEmail:
    """Payload for confirmed interview booking notification."""

    recipient_name: str
    recipient_email: str
    role_title: str
    confirmed_slot: str
    action_links: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class InterviewParticipationThanksEmail:
    """Payload for thank-you email after interview transcript is captured."""

    candidate_name: str
    candidate_email: str
    role_title: str


@dataclass(frozen=True)
class InterviewRescheduleOptionsEmail:
    """Payload for manager approval on alternative interview slots."""

    candidate_name: str
    manager_email: str
    role_title: str
    hold_expires_at: str
    slot_options: list[str]
    slot_option_links: list[tuple[str, str]] = field(default_factory=list)
    reject_link: str | None = None


@dataclass(frozen=True)
class OfferLetterCandidateEmail:
    """Payload for sending candidate offer letter email with PDF attachment."""

    candidate_name: str
    candidate_email: str
    role_title: str
    attachment_filename: str
    offer_letter_pdf_bytes: bytes


@dataclass(frozen=True)
class ManagerDecisionRejectionEmail:
    """Payload for manager-final rejection email after interview stage."""

    candidate_name: str
    candidate_email: str
    role_title: str


@dataclass(frozen=True)
class OfferLetterSignedAlertEmail:
    """Payload for manager alert when candidate signs offer letter."""

    manager_email: str
    manager_name: str
    candidate_name: str
    candidate_email: str
    role_title: str


@dataclass(frozen=True)
class SlackWorkspaceInviteEmail:
    """Payload for fallback Slack workspace invite-link email."""

    candidate_name: str
    candidate_email: str
    role_title: str
    slack_invite_link: str


@dataclass(frozen=True)
class SlackJoinManagerAlertEmail:
    """Payload for manager alert when candidate joins Slack onboarding."""

    manager_email: str
    manager_name: str
    candidate_name: str
    candidate_email: str
    role_title: str
    start_date: str
    slack_joined_at: str


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

    async def send_interview_slot_options(
        self,
        payload: InterviewSlotOptionsEmail,
    ) -> None:
        """Send interview slot options email for shortlisted candidates."""

        _ = payload

    async def send_interview_slot_reminder(
        self,
        payload: InterviewSlotOptionsEmail,
    ) -> None:
        """Send follow-up reminder for still-open interview slot options."""

        _ = payload

    async def send_interview_booking_confirmed(
        self,
        payload: InterviewBookingConfirmedEmail,
    ) -> None:
        """Send interview booking confirmation email."""

        _ = payload

    async def send_interview_participation_thanks(
        self,
        payload: InterviewParticipationThanksEmail,
    ) -> None:
        """Send thank-you email after interview completion."""

        _ = payload

    async def send_interview_reschedule_options_to_manager(
        self,
        payload: InterviewRescheduleOptionsEmail,
    ) -> None:
        """Send alternative interview options to manager for approve/reject decision."""

        _ = payload

    async def send_offer_letter_to_candidate(
        self,
        payload: OfferLetterCandidateEmail,
    ) -> None:
        """Send offer letter email with attached PDF."""

        _ = payload

    async def send_manager_rejection_notice(
        self,
        payload: ManagerDecisionRejectionEmail,
    ) -> None:
        """Send final manager rejection email to candidate."""

        _ = payload

    async def send_offer_letter_signed_alert(
        self,
        payload: OfferLetterSignedAlertEmail,
    ) -> None:
        """Send immediate alert to manager once offer letter is signed."""

        _ = payload

    async def send_slack_workspace_invite(
        self,
        payload: SlackWorkspaceInviteEmail,
    ) -> None:
        """Send fallback Slack workspace invite-link email to candidate."""

        _ = payload

    async def send_slack_join_manager_alert(
        self,
        payload: SlackJoinManagerAlertEmail,
    ) -> None:
        """Send manager alert when candidate has joined Slack."""

        _ = payload


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

    async def send_interview_slot_options(
        self,
        payload: InterviewSlotOptionsEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_interview_slot_reminder(
        self,
        payload: InterviewSlotOptionsEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_interview_booking_confirmed(
        self,
        payload: InterviewBookingConfirmedEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_interview_participation_thanks(
        self,
        payload: InterviewParticipationThanksEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_interview_reschedule_options_to_manager(
        self,
        payload: InterviewRescheduleOptionsEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_offer_letter_to_candidate(
        self,
        payload: OfferLetterCandidateEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_manager_rejection_notice(
        self,
        payload: ManagerDecisionRejectionEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_offer_letter_signed_alert(
        self,
        payload: OfferLetterSignedAlertEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_slack_workspace_invite(
        self,
        payload: SlackWorkspaceInviteEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload

    async def send_slack_join_manager_alert(
        self,
        payload: SlackJoinManagerAlertEmail,
    ) -> None:
        """Accept payload without sending."""

        _ = payload
