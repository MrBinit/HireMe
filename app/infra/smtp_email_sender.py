"""SMTP-backed async email sender."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage
from html import escape

import anyio

from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSendError,
    EmailSender,
    InterviewBookingConfirmedEmail,
    InterviewRescheduleOptionsEmail,
    InterviewSlotOptionsEmail,
    InitialScreeningRejectionEmail,
)


class SmtpEmailSender(EmailSender):
    """Send confirmation emails through SMTP."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        use_starttls: bool,
        use_ssl: bool,
        sender_name: str,
        sender_email: str,
        confirmation_subject_template: str,
        confirmation_body_template: str,
        rejection_subject_template: str,
        rejection_body_template: str,
        interview_options_subject_template: str,
        interview_options_body_template: str,
        interview_reminder_subject_template: str,
        interview_reminder_body_template: str,
        interview_confirmed_subject_template: str,
        interview_confirmed_body_template: str,
        interview_reschedule_options_subject_template: str,
        interview_reschedule_options_body_template: str,
    ):
        """Initialize SMTP sender with server and template settings."""

        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._use_starttls = use_starttls
        self._use_ssl = use_ssl
        self._sender_name = sender_name
        self._sender_email = sender_email
        self._confirmation_subject_template = confirmation_subject_template
        self._confirmation_body_template = confirmation_body_template
        self._rejection_subject_template = rejection_subject_template
        self._rejection_body_template = rejection_body_template
        self._interview_options_subject_template = interview_options_subject_template
        self._interview_options_body_template = interview_options_body_template
        self._interview_reminder_subject_template = interview_reminder_subject_template
        self._interview_reminder_body_template = interview_reminder_body_template
        self._interview_confirmed_subject_template = interview_confirmed_subject_template
        self._interview_confirmed_body_template = interview_confirmed_body_template
        self._interview_reschedule_options_subject_template = (
            interview_reschedule_options_subject_template
        )
        self._interview_reschedule_options_body_template = interview_reschedule_options_body_template

    async def send_application_confirmation(
        self,
        payload: ApplicationConfirmationEmail,
    ) -> None:
        """Send one application confirmation email message."""

        variables = {
            "candidate_name": payload.candidate_name,
            "candidate_email": payload.candidate_email,
            "role_title": payload.role_title,
        }
        await self._send_templated_email(
            recipient_email=payload.candidate_email,
            variables=variables,
            subject_template=self._confirmation_subject_template,
            body_template=self._confirmation_body_template,
            error_message="failed to send application confirmation email",
        )

    async def send_initial_screening_rejection(
        self,
        payload: InitialScreeningRejectionEmail,
    ) -> None:
        """Send one initial-screening rejection email message."""

        variables = {
            "candidate_name": payload.candidate_name,
            "candidate_email": payload.candidate_email,
            "role_title": payload.role_title,
            "rejection_reason": payload.rejection_reason,
        }
        await self._send_templated_email(
            recipient_email=payload.candidate_email,
            variables=variables,
            subject_template=self._rejection_subject_template,
            body_template=self._rejection_body_template,
            error_message="failed to send rejection email",
        )

    async def send_interview_slot_options(
        self,
        payload: InterviewSlotOptionsEmail,
    ) -> None:
        """Send one interview-slot options email to shortlisted candidate."""

        options_text = "\n".join(payload.slot_options)
        variables = {
            "candidate_name": payload.candidate_name,
            "candidate_email": payload.candidate_email,
            "role_title": payload.role_title,
            "hold_expires_at": payload.hold_expires_at,
            "slot_options": options_text,
        }
        await self._send_templated_email(
            recipient_email=payload.candidate_email,
            variables=variables,
            subject_template=self._interview_options_subject_template,
            body_template=self._interview_options_body_template,
            html_body=self._build_interview_slots_html(
                payload=payload,
                intro_text=(
                    "Congratulations! You have cleared the screening for the "
                    f"{escape(payload.role_title)} role and moved to the technical interview round. "
                    "Please review the available interview slots below and click your preferred option:"
                ),
                footer_text=(
                    "These slots are held temporarily and will be released at "
                    f"{escape(payload.hold_expires_at)} if not confirmed.<br/>"
                    "Once you confirm one slot, we will finalize your technical interview booking."
                ),
            ),
            error_message="failed to send interview slot options email",
        )

    async def send_interview_slot_reminder(
        self,
        payload: InterviewSlotOptionsEmail,
    ) -> None:
        """Send reminder email when candidate has not selected a slot yet."""

        options_text = "\n".join(payload.slot_options)
        variables = {
            "candidate_name": payload.candidate_name,
            "candidate_email": payload.candidate_email,
            "role_title": payload.role_title,
            "hold_expires_at": payload.hold_expires_at,
            "slot_options": options_text,
        }
        await self._send_templated_email(
            recipient_email=payload.candidate_email,
            variables=variables,
            subject_template=self._interview_reminder_subject_template,
            body_template=self._interview_reminder_body_template,
            html_body=self._build_interview_slots_html(
                payload=payload,
                intro_text=(
                    "This is a reminder to confirm your technical interview slot for the "
                    f"{escape(payload.role_title)} role. "
                    "Please choose one of the held options below:"
                ),
                footer_text=(
                    "If you are interested, please confirm within the next 24 hours. "
                    f"Unconfirmed slots will expire at {escape(payload.hold_expires_at)}."
                ),
            ),
            error_message="failed to send interview slot reminder email",
        )

    async def send_interview_booking_confirmed(
        self,
        payload: InterviewBookingConfirmedEmail,
    ) -> None:
        """Send booking-confirmed email after candidate picks a slot."""

        action_links_text = "\n".join(
            f"{label}: {link}" for label, link in payload.action_links if link
        )
        variables = {
            "candidate_name": payload.recipient_name,
            "candidate_email": payload.recipient_email,
            "role_title": payload.role_title,
            "confirmed_slot": payload.confirmed_slot,
            "action_links": action_links_text,
        }
        await self._send_templated_email(
            recipient_email=payload.recipient_email,
            variables=variables,
            subject_template=self._interview_confirmed_subject_template,
            body_template=self._interview_confirmed_body_template,
            html_body=self._build_confirmed_html(payload),
            error_message="failed to send interview booking confirmation email",
        )

    async def send_interview_reschedule_options_to_manager(
        self,
        payload: InterviewRescheduleOptionsEmail,
    ) -> None:
        """Send manager approve/reject email for alternative interview options."""

        options_text = "\n".join(payload.slot_options)
        variables = {
            "candidate_name": payload.candidate_name,
            "role_title": payload.role_title,
            "hold_expires_at": payload.hold_expires_at,
            "slot_options": options_text,
            "reject_link": payload.reject_link or "-",
        }
        await self._send_templated_email(
            recipient_email=payload.manager_email,
            variables=variables,
            subject_template=self._interview_reschedule_options_subject_template,
            body_template=self._interview_reschedule_options_body_template,
            html_body=self._build_manager_reschedule_html(payload),
            error_message="failed to send interview reschedule options email",
        )

    async def _send_templated_email(
        self,
        *,
        recipient_email: str,
        variables: dict[str, str],
        subject_template: str,
        body_template: str,
        html_body: str | None = None,
        error_message: str,
    ) -> None:
        """Render one template email and send over SMTP."""

        subject = subject_template.format(**variables)
        body = body_template.format(**variables)
        message = EmailMessage()
        message["From"] = f"{self._sender_name} <{self._sender_email}>"
        message["To"] = recipient_email
        message["Subject"] = subject
        message.set_content(body)
        if html_body:
            message.add_alternative(html_body, subtype="html")

        try:
            await anyio.to_thread.run_sync(self._send_sync, message)
        except Exception as exc:
            raise EmailSendError(error_message) from exc

    def _build_interview_slots_html(
        self,
        *,
        payload: InterviewSlotOptionsEmail,
        intro_text: str,
        footer_text: str,
    ) -> str:
        """Build HTML variant with explicit clickable links for each option."""

        if payload.slot_option_links:
            option_rows = "".join(
                (
                    f"<li>{escape(label)} | "
                    f"<a href=\"{escape(link, quote=True)}\" target=\"_blank\" rel=\"noreferrer\">"
                    "Click here"
                    "</a></li>"
                )
                for label, link in payload.slot_option_links
            )
        else:
            option_rows = "".join(f"<li>{escape(option)}</li>" for option in payload.slot_options)
        return (
            f"<p>Hi {escape(payload.candidate_name)},</p>"
            f"<p>{intro_text}</p>"
            f"<ul>{option_rows}</ul>"
            f"<p>{footer_text}</p>"
            "<p>Regards,<br/>HireMe Team</p>"
        )

    def _build_confirmed_html(self, payload: InterviewBookingConfirmedEmail) -> str:
        """Build HTML confirmation email with action CTA links."""

        action_items = "".join(
            (
                f"<li><a href=\"{escape(link, quote=True)}\" target=\"_blank\" rel=\"noreferrer\">"
                f"{escape(label)}"
                "</a></li>"
            )
            for label, link in payload.action_links
            if link
        )
        action_block = f"<ul>{action_items}</ul>" if action_items else ""
        return (
            f"<p>Hi {escape(payload.recipient_name)},</p>"
            "<p>Your technical interview has been confirmed for:</p>"
            f"<p><strong>{escape(payload.confirmed_slot)}</strong></p>"
            f"{action_block}"
            "<p>A calendar invitation has been sent.</p>"
            "<p>Regards,<br/>HireMe Team</p>"
        )

    def _build_manager_reschedule_html(self, payload: InterviewRescheduleOptionsEmail) -> str:
        """Build HTML manager email with accept/reject CTA links for alternatives."""

        option_rows = "".join(
            (
                f"<li>{escape(label)} | "
                f"<a href=\"{escape(link, quote=True)}\" target=\"_blank\" rel=\"noreferrer\">"
                "Accept"
                "</a></li>"
            )
            for label, link in payload.slot_option_links
            if link
        )
        reject_html = (
            f"<p>If none work, <a href=\"{escape(payload.reject_link, quote=True)}\" "
            "target=\"_blank\" rel=\"noreferrer\">Reject and send new options</a>.</p>"
            if payload.reject_link
            else ""
        )
        return (
            "<p>Hi Hiring Manager,</p>"
            f"<p>{escape(payload.candidate_name)} requested to reschedule the technical interview "
            f"for {escape(payload.role_title)}. Please review the proposed options below:</p>"
            f"<ul>{option_rows}</ul>"
            f"{reject_html}"
            f"<p>These alternatives are held until {escape(payload.hold_expires_at)}.</p>"
            "<p>Regards,<br/>HireMe Team</p>"
        )

    def _send_sync(self, message: EmailMessage) -> None:
        """Perform blocking SMTP send."""

        if self._use_ssl:
            with smtplib.SMTP_SSL(self._host, self._port) as client:
                self._login_if_needed(client)
                client.send_message(message)
            return

        with smtplib.SMTP(self._host, self._port) as client:
            if self._use_starttls:
                client.starttls()
            self._login_if_needed(client)
            client.send_message(message)

    def _login_if_needed(self, client: smtplib.SMTP) -> None:
        """Authenticate with SMTP server when credentials are configured."""

        if not self._username:
            return
        if not self._password:
            raise EmailSendError("SMTP password is required when username is set")
        client.login(self._username, self._password)
