"""The tests for the IMAP email automation trigger."""
import email
import unittest
from collections import deque
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from unittest.mock import patch, MagicMock

from homeassistant.core import callback
from homeassistant.bootstrap import setup_component
import homeassistant.components.automation as automation
from homeassistant.components.automation import imap_email

from tests.common import get_test_home_assistant


class FakeImapConnection:
    """Fake IMAP connection for testing."""

    def __init__(self, messages=None):
        """Initialize with optional messages."""
        self._messages = messages or {}
        self._all_uids = set()
        self._logged_in = True

    def login(self, user, password):
        """Simulate login."""
        return ('OK', [b'Logged in'])

    def logout(self):
        """Simulate logout."""
        self._logged_in = False

    def select(self, folder='INBOX'):
        """Simulate folder selection."""
        return ('OK', [b'1'])

    def uid(self, command, *args):
        """Simulate UID commands."""
        if command == 'search':
            uids = b' '.join(sorted(self._messages.keys()))
            return ('OK', [uids])
        elif command == 'fetch':
            uid = args[0]
            if uid in self._messages:
                return ('OK', [(b'1 (RFC822 {100}',
                                self._messages[uid])])
            return ('OK', [(b'', b'')])

    def add_message(self, uid, raw_email):
        """Add a message to the fake server."""
        self._messages[uid] = raw_email


def _build_raw_email(sender, subject, body, date=None):
    """Build a raw email bytes object."""
    msg = MIMEText(body, 'plain')
    msg['From'] = sender
    msg['Subject'] = subject
    msg['Date'] = date or 'Wed, 11 Mar 2026 10:00:00 +0000'
    return msg.as_bytes()


class TestImapEmailChecker(unittest.TestCase):
    """Test the ImapEmailChecker class."""

    @patch('imaplib.IMAP4_SSL')
    def test_connect_success(self, mock_imap):
        """Test successful connection."""
        checker = imap_email.ImapEmailChecker(
            'imap.test.com', 993, 'user@test.com', 'password', 'INBOX')
        self.assertTrue(checker.connect())

    @patch('imaplib.IMAP4_SSL')
    def test_connect_failure(self, mock_imap):
        """Test connection failure."""
        import imaplib
        mock_imap.side_effect = imaplib.IMAP4.error('Connection refused')
        checker = imap_email.ImapEmailChecker(
            'imap.test.com', 993, 'user@test.com', 'password', 'INBOX')
        self.assertFalse(checker.connect())

    def test_check_new_emails(self):
        """Test checking for new emails."""
        fake_conn = FakeImapConnection()
        checker = imap_email.ImapEmailChecker(
            'imap.test.com', 993, 'user@test.com', 'password', 'INBOX')
        checker._connection = fake_conn

        checker.initialize_seen_ids()
        self.assertEqual(set(), checker._seen_ids)

        raw = _build_raw_email('sender@test.com', 'Test Subject', 'Hello!')
        fake_conn.add_message(b'1', raw)

        messages = checker.check_new_emails()
        self.assertEqual(1, len(messages))
        self.assertEqual('sender@test.com', messages[0]['from'])
        self.assertEqual('Test Subject', messages[0]['subject'])
        self.assertEqual('Hello!', messages[0]['body'])

    def test_check_no_duplicate_emails(self):
        """Test that already seen emails are not returned again."""
        fake_conn = FakeImapConnection()
        checker = imap_email.ImapEmailChecker(
            'imap.test.com', 993, 'user@test.com', 'password', 'INBOX')
        checker._connection = fake_conn

        raw = _build_raw_email('sender@test.com', 'Test', 'Body')
        fake_conn.add_message(b'1', raw)

        checker.initialize_seen_ids()
        self.assertEqual({b'1'}, checker._seen_ids)

        messages = checker.check_new_emails()
        self.assertEqual(0, len(messages))

    def test_check_multiple_new_emails(self):
        """Test multiple new emails are detected."""
        fake_conn = FakeImapConnection()
        checker = imap_email.ImapEmailChecker(
            'imap.test.com', 993, 'user@test.com', 'password', 'INBOX')
        checker._connection = fake_conn

        checker.initialize_seen_ids()

        fake_conn.add_message(
            b'1', _build_raw_email('a@test.com', 'Subject A', 'Body A'))
        fake_conn.add_message(
            b'2', _build_raw_email('b@test.com', 'Subject B', 'Body B'))

        messages = checker.check_new_emails()
        self.assertEqual(2, len(messages))

        senders = {m['from'] for m in messages}
        self.assertIn('a@test.com', senders)
        self.assertIn('b@test.com', senders)


class TestImapEmailHelpers(unittest.TestCase):
    """Test helper functions."""

    def test_get_sender(self):
        """Test sender extraction."""
        msg = email.message.Message()
        msg['From'] = 'Test User <user@test.com>'
        self.assertEqual('user@test.com', imap_email._get_sender(msg))

    def test_get_subject(self):
        """Test subject extraction."""
        msg = email.message.Message()
        msg['Subject'] = 'Hello World'
        self.assertEqual('Hello World', imap_email._get_subject(msg))

    def test_get_body_plain_text(self):
        """Test body extraction from plain text email."""
        msg = MIMEText('Plain text body', 'plain')
        msg['From'] = 'test@test.com'
        msg['Subject'] = 'Test'
        self.assertEqual('Plain text body', imap_email._get_body(msg))

    def test_get_body_multipart_prefers_text(self):
        """Test body extraction prefers plain text in multipart."""
        msg = MIMEMultipart('alternative')
        msg['From'] = 'test@test.com'
        msg['Subject'] = 'Test'
        msg.attach(MIMEText('Plain text', 'plain'))
        msg.attach(MIMEText('<b>HTML text</b>', 'html'))
        self.assertEqual('Plain text', imap_email._get_body(msg))

    def test_get_body_multipart_html_fallback(self):
        """Test body extraction falls back to HTML."""
        msg = MIMEMultipart('alternative')
        msg['From'] = 'test@test.com'
        msg['Subject'] = 'Test'
        msg.attach(MIMEText('<b>HTML only</b>', 'html'))
        self.assertEqual('<b>HTML only</b>', imap_email._get_body(msg))


class TestTriggerSchema(unittest.TestCase):
    """Test the trigger schema validation."""

    def test_valid_config(self):
        """Test valid configuration."""
        config = imap_email.TRIGGER_SCHEMA({
            'platform': 'imap_email',
            'server': 'imap.gmail.com',
            'username': 'user@gmail.com',
            'password': 'secret',
        })
        self.assertEqual('imap.gmail.com', config['server'])
        self.assertEqual(993, config['port'])
        self.assertEqual('INBOX', config['folder'])
        self.assertEqual(60, config['scan_interval'])

    def test_config_with_options(self):
        """Test configuration with optional fields."""
        config = imap_email.TRIGGER_SCHEMA({
            'platform': 'imap_email',
            'server': 'imap.example.com',
            'username': 'user@example.com',
            'password': 'secret',
            'port': 143,
            'folder': 'Important',
            'senders': ['boss@company.com'],
            'scan_interval': 30,
        })
        self.assertEqual(143, config['port'])
        self.assertEqual('Important', config['folder'])
        self.assertEqual(['boss@company.com'], config['senders'])
        self.assertEqual(30, config['scan_interval'])

    def test_missing_required_fields(self):
        """Test that missing required fields raise errors."""
        import voluptuous as vol
        with self.assertRaises(vol.MultipleInvalid):
            imap_email.TRIGGER_SCHEMA({
                'platform': 'imap_email',
                'server': 'imap.test.com',
            })
