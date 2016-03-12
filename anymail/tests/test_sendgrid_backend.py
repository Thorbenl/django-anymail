# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import json
from calendar import timegm
from datetime import date, datetime
from decimal import Decimal
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage

from django.core import mail
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase
from django.test.utils import override_settings
from django.utils.timezone import get_fixed_timezone, override as override_current_timezone

from anymail.exceptions import AnymailAPIError, AnymailSerializationError, AnymailUnsupportedFeature
from anymail.message import attach_inline_image

from .mock_requests_backend import RequestsBackendMockAPITestCase
from .utils import sample_image_content, sample_image_path, SAMPLE_IMAGE_FILENAME, AnymailTestMixin


@override_settings(EMAIL_BACKEND='anymail.backends.sendgrid.SendGridBackend',
                   ANYMAIL={'SENDGRID_API_KEY': 'test_api_key'})
class SendGridBackendMockAPITestCase(RequestsBackendMockAPITestCase):
    DEFAULT_RAW_RESPONSE = b"""{
        "message": "success"
    }"""

    def setUp(self):
        super(SendGridBackendMockAPITestCase, self).setUp()
        # Simple message useful for many tests
        self.message = mail.EmailMultiAlternatives('Subject', 'Text Body', 'from@example.com', ['to@example.com'])

    def get_smtpapi(self):
        """Returns the x-smtpapi data passed to the mock requests call"""
        data = self.get_api_call_data()
        return json.loads(data["x-smtpapi"])


class SendGridBackendStandardEmailTests(SendGridBackendMockAPITestCase):
    """Test backend support for Django standard email features"""

    def test_send_mail(self):
        """Test basic API for simple send"""
        mail.send_mail('Subject here', 'Here is the message.',
                       'from@sender.example.com', ['to@example.com'], fail_silently=False)
        self.assert_esp_called('/api/mail.send.json')
        headers = self.get_api_call_headers()
        self.assertEqual(headers["Authorization"], "Bearer test_api_key")
        data = self.get_api_call_data()
        self.assertEqual(data['subject'], "Subject here")
        self.assertEqual(data['text'], "Here is the message.")
        self.assertEqual(data['from'], "from@sender.example.com")
        self.assertEqual(data['to'], ["to@example.com"])
        # make sure backend assigned a Message-ID for event tracking
        headers = json.loads(data['headers'])
        self.assertRegex(headers['Message-ID'], r'\<.+@sender\.example\.com\>')  # id uses from_email's domain

    def test_name_addr(self):
        """Make sure RFC2822 name-addr format (with display-name) is allowed

        (Test both sender and recipient addresses)
        """
        msg = mail.EmailMessage(
            'Subject', 'Message', 'From Name <from@example.com>',
            ['Recipient #1 <to1@example.com>', 'to2@example.com'],
            cc=['Carbon Copy <cc1@example.com>', 'cc2@example.com'],
            bcc=['Blind Copy <bcc1@example.com>', 'bcc2@example.com'])
        msg.send()
        data = self.get_api_call_data()
        self.assertEqual(data['from'], "from@example.com")
        self.assertEqual(data['fromname'], "From Name")
        self.assertEqual(data['to'], ['to1@example.com', 'to2@example.com'])
        self.assertEqual(data['toname'], ['Recipient #1', ' '])  # note space -- SendGrid balks on ''
        self.assertEqual(data['cc'], ['cc1@example.com', 'cc2@example.com'])
        self.assertEqual(data['ccname'], ['Carbon Copy', ' '])
        self.assertEqual(data['bcc'], ['bcc1@example.com', 'bcc2@example.com'])
        self.assertEqual(data['bccname'], ['Blind Copy', ' '])

    def test_email_message(self):
        email = mail.EmailMessage(
            'Subject', 'Body goes here', 'from@example.com',
            ['to1@example.com', 'Also To <to2@example.com>'],
            bcc=['bcc1@example.com', 'Also BCC <bcc2@example.com>'],
            cc=['cc1@example.com', 'Also CC <cc2@example.com>'],
            headers={'Reply-To': 'another@example.com',
                     'X-MyHeader': 'my value',
                     'Message-ID': 'mycustommsgid@sales.example.com'})  # should override backend msgid
        email.send()
        data = self.get_api_call_data()
        self.assertEqual(data['subject'], "Subject")
        self.assertEqual(data['text'], "Body goes here")
        self.assertEqual(data['from'], "from@example.com")
        self.assertEqual(data['to'], ['to1@example.com', 'to2@example.com'])
        self.assertEqual(data['toname'], [' ', 'Also To'])
        self.assertEqual(data['bcc'], ['bcc1@example.com', 'bcc2@example.com'])
        self.assertEqual(data['bccname'], [' ', 'Also BCC'])
        self.assertEqual(data['cc'], ['cc1@example.com', 'cc2@example.com'])
        self.assertEqual(data['ccname'], [' ', 'Also CC'])
        self.assertJSONEqual(data['headers'], {
            'Message-ID': 'mycustommsgid@sales.example.com',
            'Reply-To': 'another@example.com',
            'X-MyHeader': 'my value',
        })

    def test_html_message(self):
        text_content = 'This is an important message.'
        html_content = '<p>This is an <strong>important</strong> message.</p>'
        email = mail.EmailMultiAlternatives('Subject', text_content,
                                            'from@example.com', ['to@example.com'])
        email.attach_alternative(html_content, "text/html")
        email.send()
        data = self.get_api_call_data()
        self.assertEqual(data['text'], text_content)
        self.assertEqual(data['html'], html_content)
        # Don't accidentally send the html part as an attachment:
        files = self.get_api_call_files(required=False)
        self.assertIsNone(files)

    def test_html_only_message(self):
        html_content = '<p>This is an <strong>important</strong> message.</p>'
        email = mail.EmailMessage('Subject', html_content, 'from@example.com', ['to@example.com'])
        email.content_subtype = "html"  # Main content is now text/html
        email.send()
        data = self.get_api_call_data()
        self.assertNotIn('text', data)
        self.assertEqual(data['html'], html_content)

    def test_extra_headers(self):
        self.message.extra_headers = {'X-Custom': 'string', 'X-Num': 123}
        self.message.send()
        data = self.get_api_call_data()
        headers = json.loads(data['headers'])
        self.assertEqual(headers['X-Custom'], 'string')
        self.assertEqual(headers['X-Num'], '123')  # number converted to string (per SendGrid requirement)

    def test_extra_headers_serialization_error(self):
        self.message.extra_headers = {'X-Custom': Decimal(12.5)}
        with self.assertRaisesMessage(AnymailSerializationError, "Decimal('12.5')"):
            self.message.send()

    def test_reply_to(self):
        # reply_to is new in Django 1.8 -- before that, you can simply include it in headers
        try:
            # noinspection PyArgumentList
            email = mail.EmailMessage('Subject', 'Body goes here', 'from@example.com', ['to1@example.com'],
                                      reply_to=['reply@example.com', 'Other <reply2@example.com>'],
                                      headers={'X-Other': 'Keep'})
        except TypeError:
            # Pre-Django 1.8
            return self.skipTest("Django version doesn't support EmailMessage(reply_to)")
        email.send()
        data = self.get_api_call_data()
        self.assertNotIn('replyto', data)  # don't use SendGrid's replyto (it's broken); just use headers
        headers = json.loads(data['headers'])
        self.assertEqual(headers['Reply-To'], 'reply@example.com, Other <reply2@example.com>')
        self.assertEqual(headers['X-Other'], 'Keep')  # don't lose other headers

    def test_attachments(self):
        text_content = "* Item one\n* Item two\n* Item three"
        self.message.attach(filename="test.txt", content=text_content, mimetype="text/plain")

        # Should guess mimetype if not provided...
        png_content = b"PNG\xb4 pretend this is the contents of a png file"
        self.message.attach(filename="test.png", content=png_content)

        # Should work with a MIMEBase object (also tests no filename)...
        pdf_content = b"PDF\xb4 pretend this is valid pdf data"
        mimeattachment = MIMEBase('application', 'pdf')
        mimeattachment.set_payload(pdf_content)
        self.message.attach(mimeattachment)

        self.message.send()
        files = self.get_api_call_files()
        self.assertEqual(files, {
            'files[test.txt]': ('test.txt', text_content, 'text/plain'),
            'files[test.png]': ('test.png', png_content, 'image/png'),  # type inferred from filename
            'files[]': ('', pdf_content, 'application/pdf'),  # no filename
        })

    def test_attachment_name_conflicts(self):
        # It's not clear how to (or whether) supply multiple attachments with
        # the same name to SendGrid's API. Anymail treats this case as unsupported.
        self.message.attach('foo.txt', 'content', 'text/plain')
        self.message.attach('bar.txt', 'content', 'text/plain')
        self.message.attach('foo.txt', 'different content', 'text/plain')
        with self.assertRaisesMessage(AnymailUnsupportedFeature,
                                      "multiple attachments with the same filename") as cm:
            self.message.send()
        self.assertIn('foo.txt', str(cm.exception))  # say which filename

    def test_unnamed_attachment_conflicts(self):
        # Same as previous test, but with None/empty filenames
        self.message.attach(None, 'content', 'text/plain')
        self.message.attach('', 'different content', 'text/plain')
        with self.assertRaisesMessage(AnymailUnsupportedFeature, "multiple unnamed attachments"):
            self.message.send()

    def test_unicode_attachment_correctly_decoded(self):
        # Slight modification from the Django unicode docs:
        # http://django.readthedocs.org/en/latest/ref/unicode.html#email
        self.message.attach("Une pièce jointe.html", '<p>\u2019</p>', mimetype='text/html')
        self.message.send()
        files = self.get_api_call_files()
        self.assertEqual(files['files[Une pièce jointe.html]'],
                         ('Une pièce jointe.html', '<p>\u2019</p>', 'text/html'))

    def test_embedded_images(self):
        image_data = sample_image_content()  # Read from a png file
        cid = attach_inline_image(self.message, image_data)
        html_content = '<p>This has an <img src="cid:%s" alt="inline" /> image.</p>' % cid
        self.message.attach_alternative(html_content, "text/html")

        self.message.send()
        data = self.get_api_call_data()
        self.assertEqual(data['html'], html_content)

        files = self.get_api_call_files()
        filename = cid  # (for now)
        self.assertEqual(files, {
            'files[%s]' % filename: (filename, image_data, "image/png"),
        })
        self.assertEqual(data['content[%s]' % filename], cid)

    def test_attached_images(self):
        image_filename = SAMPLE_IMAGE_FILENAME
        image_path = sample_image_path(image_filename)
        image_data = sample_image_content(image_filename)

        self.message.attach_file(image_path)  # option 1: attach as a file

        image = MIMEImage(image_data)  # option 2: construct the MIMEImage and attach it directly
        self.message.attach(image)

        self.message.send()
        files = self.get_api_call_files()
        self.assertEqual(files, {
            'files[%s]' % image_filename: (image_filename, image_data, "image/png"),  # the named one
            'files[]': ('', image_data, "image/png"),  # the unnamed one
        })

    def test_multiple_html_alternatives(self):
        # Multiple alternatives not allowed
        self.message.attach_alternative("<p>First html is OK</p>", "text/html")
        self.message.attach_alternative("<p>But not second html</p>", "text/html")
        with self.assertRaises(AnymailUnsupportedFeature):
            self.message.send()

    def test_html_alternative(self):
        # Only html alternatives allowed
        self.message.attach_alternative("{'not': 'allowed'}", "application/json")
        with self.assertRaises(AnymailUnsupportedFeature):
            self.message.send()

    def test_alternatives_fail_silently(self):
        # Make sure fail_silently is respected
        self.message.attach_alternative("{'not': 'allowed'}", "application/json")
        sent = self.message.send(fail_silently=True)
        self.assert_esp_not_called("API should not be called when send fails silently")
        self.assertEqual(sent, 0)

    def test_suppress_empty_address_lists(self):
        """Empty to, cc, bcc, and reply_to shouldn't generate empty headers"""
        self.message.send()
        data = self.get_api_call_data()
        self.assertNotIn('cc', data)
        self.assertNotIn('ccname', data)
        self.assertNotIn('bcc', data)
        self.assertNotIn('bccname', data)
        headers = json.loads(data['headers'])
        self.assertNotIn('Reply-To', headers)

        # Test empty `to` -- but send requires at least one recipient somewhere (like cc)
        self.message.to = []
        self.message.cc = ['cc@example.com']
        self.message.send()
        data = self.get_api_call_data()
        self.assertNotIn('to', data)
        self.assertNotIn('toname', data)

    def test_api_failure(self):
        self.set_mock_response(status_code=400)
        with self.assertRaises(AnymailAPIError):
            sent = mail.send_mail('Subject', 'Body', 'from@example.com', ['to@example.com'])
            self.assertEqual(sent, 0)

        # Make sure fail_silently is respected
        self.set_mock_response(status_code=400)
        sent = mail.send_mail('Subject', 'Body', 'from@example.com', ['to@example.com'], fail_silently=True)
        self.assertEqual(sent, 0)

    def test_api_error_includes_details(self):
        """AnymailAPIError should include ESP's error message"""
        # JSON error response:
        error_response = b"""{
          "message": "error",
          "errors": [
            "Helpful explanation from SendGrid",
            "and more"
          ]
        }"""
        self.set_mock_response(status_code=200, raw=error_response)
        with self.assertRaisesRegex(AnymailAPIError,
                                    r"\bHelpful explanation from SendGrid\b.*and more\b"):
            self.message.send()

        # Non-JSON error response:
        self.set_mock_response(status_code=500, raw=b"Ack! Bad proxy!")
        with self.assertRaisesMessage(AnymailAPIError, "Ack! Bad proxy!"):
            self.message.send()

        # No content in the error response:
        self.set_mock_response(status_code=502, raw=None)
        with self.assertRaises(AnymailAPIError):
            self.message.send()


class SendGridBackendAnymailFeatureTests(SendGridBackendMockAPITestCase):
    """Test backend support for Anymail added features"""

    def test_metadata(self):
        # Note: SendGrid doesn't handle complex types in metadata
        self.message.metadata = {'user_id': "12345", 'items': 6}
        self.message.send()
        smtpapi = self.get_smtpapi()
        self.assertEqual(smtpapi['unique_args'], {'user_id': "12345", 'items': 6})

    def test_send_at(self):
        utc_plus_6 = get_fixed_timezone(6 * 60)
        utc_minus_8 = get_fixed_timezone(-8 * 60)

        with override_current_timezone(utc_plus_6):
            # Timezone-aware datetime converted to UTC:
            self.message.send_at = datetime(2016, 3, 4, 5, 6, 7, tzinfo=utc_minus_8)
            self.message.send()
            smtpapi = self.get_smtpapi()
            self.assertEqual(smtpapi['send_at'], timegm((2016, 3, 4, 13, 6, 7)))  # 05:06 UTC-8 == 13:06 UTC

            # Timezone-naive datetime assumed to be Django current_timezone
            self.message.send_at = datetime(2022, 10, 11, 12, 13, 14, 567)  # microseconds should get stripped
            self.message.send()
            smtpapi = self.get_smtpapi()
            self.assertEqual(smtpapi['send_at'], timegm((2022, 10, 11, 6, 13, 14)))  # 12:13 UTC+6 == 06:13 UTC

            # Date-only treated as midnight in current timezone
            self.message.send_at = date(2022, 10, 22)
            self.message.send()
            smtpapi = self.get_smtpapi()
            self.assertEqual(smtpapi['send_at'], timegm((2022, 10, 21, 18, 0, 0)))  # 00:00 UTC+6 == 18:00-1d UTC

            # POSIX timestamp
            self.message.send_at = 1651820889  # 2022-05-06 07:08:09 UTC
            self.message.send()
            smtpapi = self.get_smtpapi()
            self.assertEqual(smtpapi['send_at'], 1651820889)

    def test_tags(self):
        self.message.tags = ["receipt", "repeat-user"]
        self.message.send()
        smtpapi = self.get_smtpapi()
        self.assertCountEqual(smtpapi['category'], ["receipt", "repeat-user"])

    def test_tracking(self):
        # Test one way...
        self.message.track_clicks = False
        self.message.track_opens = True
        self.message.send()
        smtpapi = self.get_smtpapi()
        self.assertEqual(smtpapi['filters']['clicktrack'], {'settings': {'enable': 0}})
        self.assertEqual(smtpapi['filters']['opentrack'], {'settings': {'enable': 1}})

        # ...and the opposite way
        self.message.track_clicks = True
        self.message.track_opens = False
        self.message.send()
        smtpapi = self.get_smtpapi()
        self.assertEqual(smtpapi['filters']['clicktrack'], {'settings': {'enable': 1}})
        self.assertEqual(smtpapi['filters']['opentrack'], {'settings': {'enable': 0}})

    def test_default_omits_options(self):
        """Make sure by default we don't send any ESP-specific options.

        Options not specified by the caller should be omitted entirely from
        the API call (*not* sent as False or empty). This ensures
        that your ESP account settings apply by default.
        """
        self.message.send()
        data = self.get_api_call_data()
        self.assertNotIn('x-smtpapi', data)

    def test_esp_extra(self):
        self.message.tags = ["tag"]
        self.message.esp_extra = {
            'x-smtpapi': {'asm_group_id': 1},
            'newthing': "some param not supported by Anymail",
        }
        self.message.send()
        # Additional send params:
        data = self.get_api_call_data()
        self.assertEqual(data['newthing'], "some param not supported by Anymail")
        # Should merge x-smtpapi
        smtpapi = self.get_smtpapi()
        self.assertEqual(smtpapi['category'], ["tag"])
        self.assertEqual(smtpapi['asm_group_id'], 1)

    # noinspection PyUnresolvedReferences
    def test_send_attaches_anymail_status(self):
        """ The anymail_status should be attached to the message when it is sent """
        # the DEFAULT_RAW_RESPONSE above is the *only* success response SendGrid returns,
        # so no need to override it here
        msg = mail.EmailMessage('Subject', 'Message', 'from@example.com', ['to1@example.com'],)
        sent = msg.send()
        self.assertEqual(sent, 1)
        self.assertEqual(msg.anymail_status.status, {'queued'})
        self.assertRegex(msg.anymail_status.message_id, r'\<.+@example\.com\>')  # don't know exactly what it'll be
        self.assertEqual(msg.anymail_status.recipients['to1@example.com'].status, 'queued')
        self.assertEqual(msg.anymail_status.recipients['to1@example.com'].message_id,
                         msg.anymail_status.message_id)
        self.assertEqual(msg.anymail_status.esp_response.content, self.DEFAULT_RAW_RESPONSE)

    # noinspection PyUnresolvedReferences
    def test_send_failed_anymail_status(self):
        """ If the send fails, anymail_status should contain initial values"""
        self.set_mock_response(status_code=500)
        sent = self.message.send(fail_silently=True)
        self.assertEqual(sent, 0)
        self.assertIsNone(self.message.anymail_status.status)
        self.assertIsNone(self.message.anymail_status.message_id)
        self.assertEqual(self.message.anymail_status.recipients, {})
        self.assertIsNone(self.message.anymail_status.esp_response)

    # noinspection PyUnresolvedReferences
    def test_send_unparsable_response(self):
        """If the send succeeds, but a non-JSON API response, should raise an API exception"""
        mock_response = self.set_mock_response(status_code=200,
                                               raw=b"yikes, this isn't a real response")
        with self.assertRaises(AnymailAPIError):
            self.message.send()
        self.assertIsNone(self.message.anymail_status.status)
        self.assertIsNone(self.message.anymail_status.message_id)
        self.assertEqual(self.message.anymail_status.recipients, {})
        self.assertEqual(self.message.anymail_status.esp_response, mock_response)

    def test_json_serialization_errors(self):
        """Try to provide more information about non-json-serializable data"""
        self.message.metadata = {'total': Decimal('19.99')}
        with self.assertRaises(AnymailSerializationError) as cm:
            self.message.send()
            print(self.get_api_call_data())
        err = cm.exception
        self.assertIsInstance(err, TypeError)  # compatibility with json.dumps
        self.assertIn("Don't know how to send this data to SendGrid", str(err))  # our added context
        self.assertIn("Decimal('19.99') is not JSON serializable", str(err))  # original message


class SendGridBackendRecipientsRefusedTests(SendGridBackendMockAPITestCase):
    """Should raise AnymailRecipientsRefused when *all* recipients are rejected or invalid"""

    # SendGrid doesn't check email bounce or complaint lists at time of send --
    # it always just queues the message. You'll need to listen for the "rejected"
    # and "failed" events to detect refused recipients.

    pass


@override_settings(ANYMAIL_SEND_DEFAULTS={
    'metadata': {'global': 'globalvalue', 'other': 'othervalue'},
    'tags': ['globaltag'],
    'track_clicks': True,
    'track_opens': True,
    'esp_extra': {'globaloption': 'globalsetting'},
})
class SendGridBackendSendDefaultsTests(SendGridBackendMockAPITestCase):
    """Tests backend support for global SEND_DEFAULTS"""

    def test_send_defaults(self):
        """Test that global send defaults are applied"""
        self.message.send()
        data = self.get_api_call_data()
        smtpapi = self.get_smtpapi()
        # All these values came from ANYMAIL_SEND_DEFAULTS:
        self.assertEqual(smtpapi['unique_args'], {'global': 'globalvalue', 'other': 'othervalue'})
        self.assertEqual(smtpapi['category'], ['globaltag'])
        self.assertEqual(smtpapi['filters']['clicktrack']['settings']['enable'], 1)
        self.assertEqual(smtpapi['filters']['opentrack']['settings']['enable'], 1)
        self.assertEqual(data['globaloption'], 'globalsetting')

    def test_merge_message_with_send_defaults(self):
        """Test that individual message settings are *merged into* the global send defaults"""
        self.message.metadata = {'message': 'messagevalue', 'other': 'override'}
        self.message.tags = ['messagetag']
        self.message.track_clicks = False
        self.message.esp_extra = {'messageoption': 'messagesetting'}

        self.message.send()
        data = self.get_api_call_data()
        smtpapi = self.get_smtpapi()
        # All these values came from ANYMAIL_SEND_DEFAULTS + message.*:
        self.assertEqual(smtpapi['unique_args'], {
            'global': 'globalvalue',
            'message': 'messagevalue',  # additional metadata
            'other': 'override',  # override global value
        })
        self.assertCountEqual(smtpapi['category'], ['globaltag', 'messagetag'])  # tags concatenated
        self.assertEqual(smtpapi['filters']['clicktrack']['settings']['enable'], 0)  # message overrides
        self.assertEqual(smtpapi['filters']['opentrack']['settings']['enable'], 1)
        self.assertEqual(data['globaloption'], 'globalsetting')
        self.assertEqual(data['messageoption'], 'messagesetting')  # additional esp_extra

    @override_settings(ANYMAIL_SENDGRID_SEND_DEFAULTS={
        'tags': ['esptag'],
        'metadata': {'esp': 'espvalue'},
        'track_opens': False,
    })
    def test_esp_send_defaults(self):
        """Test that ESP-specific send defaults override individual global defaults"""
        self.message.send()
        data = self.get_api_call_data()
        smtpapi = self.get_smtpapi()
        # All these values came from ANYMAIL_SEND_DEFAULTS plus ANYMAIL_SENDGRID_SEND_DEFAULTS:
        self.assertEqual(smtpapi['unique_args'], {'esp': 'espvalue'})  # entire metadata overridden
        self.assertCountEqual(smtpapi['category'], ['esptag'])  # entire tags overridden
        self.assertEqual(smtpapi['filters']['clicktrack']['settings']['enable'], 1)  # no override
        self.assertEqual(smtpapi['filters']['opentrack']['settings']['enable'], 0)  # esp override
        self.assertEqual(data['globaloption'], 'globalsetting')  # we didn't override the global esp_extra


@override_settings(EMAIL_BACKEND="anymail.backends.sendgrid.SendGridBackend")
class SendGridBackendImproperlyConfiguredTests(SimpleTestCase, AnymailTestMixin):
    """Test ESP backend without required settings in place"""

    def test_missing_api_key(self):
        with self.assertRaises(ImproperlyConfigured) as cm:
            mail.send_mail('Subject', 'Message', 'from@example.com', ['to@example.com'])
        errmsg = str(cm.exception)
        self.assertRegex(errmsg, r'\bSENDGRID_API_KEY\b')
        self.assertRegex(errmsg, r'\bANYMAIL_SENDGRID_API_KEY\b')