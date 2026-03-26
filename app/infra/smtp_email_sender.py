"""SMTP-backed async email sender."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

import anyio

from app.services.email_sender import (
    ApplicationConfirmationEmail,
    EmailSendError,
    EmailSender,
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

    async def _send_templated_email(
        self,
        *,
        recipient_email: str,
        variables: dict[str, str],
        subject_template: str,
        body_template: str,
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

        try:
            await anyio.to_thread.run_sync(self._send_sync, message)
        except Exception as exc:
            raise EmailSendError(error_message) from exc

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
