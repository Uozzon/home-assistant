"""
Offer IMAP email listening automation rules.

For more details about this automation rule, please refer to the documentation
at https://home-assistant.io/components/automation/#imap-email-trigger

Example configuration:

    automation:
      trigger:
        platform: imap_email
        server: imap.gmail.com
        username: your_email@gmail.com
        password: your_app_password
        port: 993          # optional, default 993
        folder: INBOX      # optional, default INBOX
        senders:           # optional, filter by sender
          - important@example.com
        scan_interval: 60  # optional, seconds between checks, default 60
      action:
        service: notify.notify
        data_template:
          title: "New email from {{ trigger.from }}"
          message: "{{ trigger.subject }}"
"""
import asyncio
import logging

import voluptuous as vol

from homeassistant.core import callback
from homeassistant.const import (
    CONF_PLATFORM, CONF_USERNAME, CONF_PASSWORD, CONF_PORT)
import homeassistant.helpers.config_validation as cv

_LOGGER = logging.getLogger(__name__)

CONF_SERVER = 'server'
CONF_FOLDER = 'folder'
CONF_SENDERS = 'senders'
CONF_SCAN_INTERVAL = 'scan_interval'

DEFAULT_PORT = 993
DEFAULT_FOLDER = 'INBOX'
DEFAULT_SCAN_INTERVAL = 60

TRIGGER_SCHEMA = vol.Schema({
    vol.Required(CONF_PLATFORM): 'imap_email',
    vol.Required(CONF_SERVER): cv.string,
    vol.Required(CONF_USERNAME): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
    vol.Optional(CONF_FOLDER, default=DEFAULT_FOLDER): cv.string,
    vol.Optional(CONF_SENDERS): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(CONF_SCAN_INTERVAL,
                 default=DEFAULT_SCAN_INTERVAL): cv.positive_int,
})


def async_trigger(hass, config, action):
    """Listen for new emails via IMAP."""
    server = config[CONF_SERVER]
    port = config[CONF_PORT]
    username = config[CONF_USERNAME]
    password = config[CONF_PASSWORD]
    folder = config[CONF_FOLDER]
    senders = config.get(CONF_SENDERS)
    scan_interval = config[CONF_SCAN_INTERVAL]

    if senders:
        senders = [s.upper() for s in senders]

    checker = ImapEmailChecker(server, port, username, password, folder)
    task = hass.loop.create_task(
        _async_poll_emails(hass, checker, senders, scan_interval, action))

    @callback
    def async_remove():
        """Remove the email polling task."""
        task.cancel()
        hass.async_add_job(checker.disconnect)

    return async_remove


@asyncio.coroutine
def _async_poll_emails(hass, checker, senders, scan_interval, action):
    """Poll IMAP server for new emails."""
    connected = yield from hass.loop.run_in_executor(None, checker.connect)
    if not connected:
        _LOGGER.error("Failed to connect to IMAP server %s", checker.server)
        return

    yield from hass.loop.run_in_executor(None, checker.initialize_seen_ids)

    while True:
        try:
            yield from asyncio.sleep(scan_interval)
            messages = yield from hass.loop.run_in_executor(
                None, checker.check_new_emails)

            for msg_data in messages:
                if senders and msg_data['from'].upper() not in senders:
                    continue

                hass.async_run_job(action, {
                    'trigger': {
                        'platform': 'imap_email',
                        'from': msg_data['from'],
                        'subject': msg_data['subject'],
                        'date': msg_data['date'],
                        'body': msg_data['body'],
                    }
                })

        except asyncio.CancelledError:
            break
        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception("Error checking IMAP email")
            yield from asyncio.sleep(scan_interval)


class ImapEmailChecker:
    """Check IMAP server for new emails."""

    def __init__(self, server, port, username, password, folder):
        """Initialize the IMAP email checker."""
        self.server = server
        self._port = port
        self._username = username
        self._password = password
        self._folder = folder
        self._connection = None
        self._seen_ids = set()

    def connect(self):
        """Connect and login to the IMAP server."""
        import imaplib
        try:
            self._connection = imaplib.IMAP4_SSL(self.server, self._port)
            self._connection.login(self._username, self._password)
            return True
        except imaplib.IMAP4.error:
            _LOGGER.error("Failed to login to %s", self.server)
            return False

    def disconnect(self):
        """Disconnect from the IMAP server."""
        if self._connection:
            try:
                self._connection.logout()
            except Exception:  # pylint: disable=broad-except
                pass

    def initialize_seen_ids(self):
        """Mark all current emails as seen to only trigger on new ones."""
        import imaplib
        try:
            self._connection.select(self._folder)
            _, data = self._connection.uid('search', None, 'ALL')
            if data[0]:
                self._seen_ids = set(data[0].split())
        except imaplib.IMAP4.error:
            _LOGGER.warning("Could not initialize seen email IDs")

    def check_new_emails(self):
        """Check for new emails and return list of new message data."""
        import imaplib
        import email as email_lib

        messages = []
        try:
            self._connection.select(self._folder)
            _, data = self._connection.uid('search', None, 'ALL')
            if not data[0]:
                return messages

            current_ids = set(data[0].split())
            new_ids = current_ids - self._seen_ids
            self._seen_ids = current_ids

            for uid in new_ids:
                try:
                    _, msg_data = self._connection.uid('fetch', uid, '(RFC822)')
                    raw_email = msg_data[0][1]
                    email_message = email_lib.message_from_bytes(raw_email)

                    messages.append({
                        'from': _get_sender(email_message),
                        'subject': _get_subject(email_message),
                        'date': email_message.get('Date', ''),
                        'body': _get_body(email_message),
                    })
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.warning("Failed to fetch email UID %s", uid)

        except imaplib.IMAP4.error:
            _LOGGER.info("Connection to %s lost, reconnecting", self.server)
            self.connect()

        return messages


def _get_sender(email_message):
    """Extract sender address from email."""
    import email.utils
    return str(email.utils.parseaddr(email_message['From'])[1])


def _get_subject(email_message):
    """Decode and return the email subject."""
    import email.header
    decoded = email.header.decode_header(email_message.get('Subject', ''))
    return str(email.header.make_header(decoded))


def _get_body(email_message):
    """Extract the text body from an email message."""
    text_body = None
    html_body = None

    if email_message.is_multipart():
        for part in email_message.walk():
            content_type = part.get_content_type()
            if content_type == 'text/plain' and text_body is None:
                text_body = part.get_payload(decode=True)
                if text_body:
                    text_body = text_body.decode('utf-8', errors='replace')
            elif content_type == 'text/html' and html_body is None:
                html_body = part.get_payload(decode=True)
                if html_body:
                    html_body = html_body.decode('utf-8', errors='replace')
    else:
        payload = email_message.get_payload(decode=True)
        if payload:
            text_body = payload.decode('utf-8', errors='replace')

    return text_body or html_body or ''
