import unittest
from unittest import mock

from mailer import Mailer


class MailerTest(unittest.TestCase):
    def test_disabled_when_no_host(self):
        m = Mailer({"smtp": {}})
        self.assertFalse(m.enabled())

    def test_enabled_with_host(self):
        m = Mailer({"smtp": {"host": "smtp.example.com"}})
        self.assertTrue(m.enabled())

    @mock.patch("smtplib.SMTP_SSL")
    @mock.patch("smtplib.SMTP")
    def test_send_uses_tls(self, smtp, smtp_ssl):
        cfg = {"smtp": {"host": "h", "port": 465, "tls": True,
                        "user": "u", "password": "p",
                        "from": "a@x.com", "to": "b@x.com"}}
        m = Mailer(cfg)
        m.send("subj", "<b>html</b>")
        inst = smtp_ssl.return_value
        inst.login.assert_called_once_with("u", "p")
        inst.sendmail.assert_called_once()
        args = inst.sendmail.call_args
        self.assertEqual(args.args[0], "a@x.com")
        self.assertIn("b@x.com", args.args[1])

    @mock.patch("smtplib.SMTP")
    def test_send_plaintext(self, smtp):
        cfg = {"smtp": {"host": "h", "port": 587, "tls": False,
                        "user": "", "password": "", "from": "a@x.com",
                        "to": "b@x.com"}}
        Mailer(cfg).send("s", "<p>hi</p>")
        inst = smtp.return_value
        inst.starttls.assert_not_called()
        self.assertTrue(inst.sendmail.called)


if __name__ == "__main__":
    unittest.main()
