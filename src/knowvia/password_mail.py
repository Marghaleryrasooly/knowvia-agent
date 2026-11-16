"""Opt-in TLS SMTP delivery. No credentials or bearer links are logged."""
from dataclasses import dataclass
from email.message import EmailMessage
from html import escape
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
        # Render only the existing application-created HTTPS password link.
        parsed = urlsplit(link)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                or parsed.path != '/password' or parsed.query
                or any(c.isspace() or ord(c)<32 for c in link)):
            raise MailUnavailable('Email delivery could not be confirmed.')
        message = EmailMessage()
        message['From'] = self.sender
        message['To'] = normalize_work_email(recipient)
        message['Subject'] = {'invite': 'Set up your Knowvia password',
                              'reset': 'Reset your Knowvia password',
                              'verify': 'Verify your Knowvia recovery email'}[purpose]
        introduction = {
            'invite': 'Your administrator has invited you to Knowvia. Create your password using the link below. Your access is assigned by your administrator. This invitation expires after 24 hours.',
            'reset': 'A password reset was requested for your Knowvia account. Choose a new password using the link below. This link expires after 30 minutes. After a successful reset, your previous sessions will be signed out.',
            'verify': 'Confirm ownership of your Knowvia recovery email using the link below. This link expires after 30 minutes.',
        }[purpose]
        message.set_content(introduction + '\n\n' + link +
                            '\n\nThis link can be used only once. If you did not expect this message, ignore it. Never share this link or your password. Knowvia will not ask you to email your password.')
        action = {'invite':'Create your password', 'reset':'Reset your password', 'verify':'Verify your email'}[purpose]
        message.add_alternative(
            '<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>'+escape(message['Subject'])+'</title></head>'
            '<body style="margin:0;padding:24px 12px;background:#ffffff;color:#20242b;font-family:Arial,Helvetica,sans-serif">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center">'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:560px;border:1px solid #e7e9ed;border-radius:12px">'
            '<tr><td style="padding:28px"><p style="margin:0 0 24px;font-size:22px;font-weight:bold;color:#397a9d">Knowvia</p>'
            '<h1 style="margin:0 0 16px;font-size:22px;line-height:1.3">'+escape(message['Subject'])+'</h1>'
            '<p style="margin:0 0 24px;font-size:15px;line-height:1.7">'+escape(introduction)+'</p>'
            '<table role="presentation" cellspacing="0" cellpadding="0"><tr><td bgcolor="#397a9d" style="border-radius:8px">'
            '<a href="'+escape(link, quote=True)+'" style="display:inline-block;padding:14px 22px;color:#ffffff;font-size:15px;font-weight:bold;text-decoration:none">'+escape(action)+'</a>'
            '</td></tr></table><p style="margin:24px 0 0;font-size:13px;line-height:1.7;color:#596471">'
            'This link can be used only once. If you did not expect this message, ignore it. Never share this link or your password. Knowvia will not ask you to email your password.'
            '</p><p style="margin:16px 0 0;font-size:12px;line-height:1.6;color:#596471">If the button is unavailable, use the link in the plain-text version of this email.</p>'
            '</td></tr></table></td></tr></table></body></html>', subtype='html')
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
