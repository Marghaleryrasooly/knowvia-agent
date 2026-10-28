"""Opt-in TLS SMTP delivery. No credentials or bearer links are logged."""
from dataclasses import dataclass
from email.message import EmailMessage
import os
import smtplib
import ssl
from urllib.parse import urlsplit

from knowvia.employee_directory import normalize_work_email


class MailUnavailable(ValueError):
    pass


class DisabledMail:
    enabled = False

    def send(self, recipient, purpose, link):
        raise MailUnavailable('Email delivery is not configured.')


@dataclass(repr=False)
class SmtpMail:
    host: str
    port: int
    username: str
    password: str
    sender: str
    enabled = True

    def send(self, recipient, purpose, link):
        message = EmailMessage()
        message['From'] = self.sender
        message['To'] = normalize_work_email(recipient)
        message['Subject'] = {'invite': 'Set up your Knowvia password',
                              'reset': 'Reset your Knowvia password',
                              'verify': 'Verify your Knowvia recovery email'}[purpose]
        message.set_content('Use this single-use Knowvia link:\n\n' + link +
                            '\n\nIf you did not expect this message, ignore it. Never share this link.')
        try:
            context = ssl.create_default_context()
            if self.port == 465:
                client = smtplib.SMTP_SSL(self.host, self.port, timeout=10, context=context)
            else:
                client = smtplib.SMTP(self.host, self.port, timeout=10)
            with client:
                if self.port == 587:
                    client.ehlo()
                    client.starttls(context=context)
                    client.ehlo()
                client.login(self.username, self.password)
                if client.send_message(message):
                    raise MailUnavailable('Email delivery could not be confirmed.')
        except Exception:
            raise MailUnavailable('Email delivery could not be confirmed.') from None


def callback_origin(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path not in {'', '/'} or parsed.query or parsed.fragment
                or parsed.hostname in {'localhost', '127.0.0.1', '::1'}
                or any(c.isspace() for c in value)):
            raise ValueError
        _ = parsed.port
        return value.rstrip('/')
    except Exception:
        raise MailUnavailable('An operator-configured public HTTPS origin is required.') from None


def mail_from_environment(values=None):
    values = os.environ if values is None else values
    selected = values.get('KNOWVIA_MAIL_TRANSPORT', 'disabled').strip().lower()
    if selected == 'disabled':
        return DisabledMail(), None
    if selected != 'smtp':
        raise MailUnavailable('Email transport configuration is invalid.')
    origin = callback_origin(values.get('KNOWVIA_PUBLIC_BASE_URL', ''))
    allowed = {h.strip().lower() for h in values.get('KNOWVIA_ALLOWED_HOSTS','').split(',')}
    if urlsplit(origin).hostname.lower() not in allowed:
        raise MailUnavailable('The email callback hostname must be explicitly trusted.')
    try:
        port = int(values.get('KNOWVIA_SMTP_PORT', '465'))
        host = values.get('KNOWVIA_SMTP_HOST', '')
        username = values.get('KNOWVIA_SMTP_USERNAME', '')
        password = values.get('KNOWVIA_SMTP_PASSWORD', '')
        sender = normalize_work_email(values.get('KNOWVIA_MAIL_FROM', ''))
        if (port not in {465, 587} or not host or len(host) > 253
                or any(c.isspace() for c in host) or not username or not password
                or values.get('KNOWVIA_MAIL_SENDER_VERIFIED', '').lower() != 'true'):
            raise ValueError
    except Exception:
        raise MailUnavailable('TLS SMTP credentials and verified-sender acknowledgement are required.') from None
    return SmtpMail(host, port, username, password, sender), origin
