import contextlib
import io
import json
import smtplib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SKILL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_ROOT / "scripts"))

import qq_mail as qm  # noqa: E402


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        key = (service, username)
        if key not in self.store:
            raise KeyError(key)
        del self.store[key]


class FakeSMTP:
    def __init__(self):
        self.login_args = None
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, username, password):
        self.login_args = (username, password)

    def send_message(self, message, from_addr=None, to_addrs=None):
        self.sent = (message, from_addr, to_addrs)


class FakeTTY:
    def isatty(self):
        return True

    def write(self, text):
        return len(text)

    def flush(self):
        pass


def base_cfg():
    return {
        "account": "me@qq.com",
        "display_name": "Me",
        "contacts": [],
        "error_recipes": {},
    }


def message_args(*extra):
    return qm.build_parser().parse_args(
        ["--to", "alice@example.com", "--subject", "Hello", "--body", "Hi", *extra]
    )


class QQMailTests(unittest.TestCase):
    def test_default_feedback_email_is_author_inbox(self):
        self.assertEqual(
            qm.default_config()["feedback_email"],
            "lanyorax@foxmail.com",
        )
        self.assertEqual(
            qm.normalize_config({})["feedback_email"],
            "lanyorax@foxmail.com",
        )
        self.assertEqual(
            qm.normalize_config({"feedback_email": "  "})["feedback_email"],
            "lanyorax@foxmail.com",
        )

    def test_config_roundtrip_preserves_contacts_and_recipes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            cfg = base_cfg()
            cfg["feedback_email"] = "author@example.com"
            cfg["contacts"] = [{"name": "小号", "email": "test@qq.com", "note": "测试"}]
            cfg["error_recipes"] = {"E_X": {"fix": "do this", "raw": "raw"}}
            with patch.object(qm, "CONFIG_PATH", path):
                qm.save_config(cfg)
                loaded, issues = qm.load_config()
            self.assertEqual(issues, [])
            self.assertEqual(loaded["account"], "me@qq.com")
            self.assertEqual(loaded["feedback_email"], "author@example.com")
            self.assertEqual(loaded["contacts"][0]["name"], "小号")
            self.assertEqual(loaded["error_recipes"]["E_X"]["fix"], "do this")

    def test_contacts_create_and_update(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            with patch.object(qm, "CONFIG_PATH", path):
                args = qm.build_parser().parse_args(
                    ["--set-contact", "小号", "--email", "test@qq.com", "--note", "我的测试邮箱"]
                )
                self.assertEqual(qm.run_set_contact(args), 0)
                args2 = qm.build_parser().parse_args(
                    ["--set-contact", "小号", "--email", "test2@qq.com"]
                )
                self.assertEqual(qm.run_set_contact(args2), 0)
                loaded, _ = qm.load_config()
            self.assertEqual(len(loaded["contacts"]), 1)
            self.assertEqual(loaded["contacts"][0]["email"], "test2@qq.com")
            self.assertEqual(loaded["contacts"][0]["note"], "我的测试邮箱")

    def test_record_fix_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            with patch.object(qm, "CONFIG_PATH", path):
                args = qm.build_parser().parse_args(
                    ["--record-fix", "E_UNKNOWN_abcd1234", "--fix", "重新开启 SMTP"]
                )
                self.assertEqual(qm.run_record_fix(args), 0)
                loaded, _ = qm.load_config()
            self.assertEqual(
                loaded["error_recipes"]["E_UNKNOWN_abcd1234"]["fix"],
                "重新开启 SMTP",
            )

    def test_set_feedback_email(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            args = qm.build_parser().parse_args(
                ["--set-feedback-email", "author@example.com"]
            )
            with patch.object(qm, "CONFIG_PATH", path):
                self.assertEqual(qm.run_set_feedback_email(args), 0)
                loaded, issues = qm.load_config()
            self.assertEqual(issues, [])
            self.assertEqual(loaded["feedback_email"], "author@example.com")

    def test_record_fix_prints_feedback_hint(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            cfg = base_cfg()
            cfg["feedback_email"] = "author@example.com"
            with patch.object(qm, "CONFIG_PATH", path):
                qm.save_config(cfg)
                args = qm.build_parser().parse_args(
                    ["--record-fix", "E_UNKNOWN_abcd1234", "--fix", "已解决"]
                )
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = qm.run_record_fix(args)
            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("FEEDBACK", text)
            self.assertIn("NEXT", text)
            self.assertIn("1. 先预览反馈邮件", text)
            self.assertIn("author@example.com", text)
            self.assertIn("--feedback", text)
            self.assertIn("维护者会看到发件地址", text)

    def test_address_parsing(self):
        items = qm.parse_addresses(
            ["Alice <alice@example.com>, bob@example.com", "carol@example.com"]
        )
        self.assertEqual(
            [address for _, address in items],
            ["alice@example.com", "bob@example.com", "carol@example.com"],
        )
        self.assertEqual(qm.parse_addresses([]), [])
        with self.assertRaises(qm.ValidationError):
            qm.parse_addresses(["not-an-email"])

    def test_message_bcc_and_attachment(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "note.txt"
            path.write_text("hello", encoding="utf-8")
            args = qm.build_parser().parse_args(
                [
                    "--to",
                    "alice@example.com",
                    "--cc",
                    "cc@example.com",
                    "--bcc",
                    "bcc@example.com",
                    "--subject",
                    "Hi",
                    "--body",
                    "Hello",
                    "--attach",
                    str(path),
                ]
            )
            prepared, issues = qm.build_message(base_cfg(), args)
        self.assertEqual(issues, [])
        self.assertIsNotNone(prepared)
        self.assertNotIn("Bcc", prepared.message)
        self.assertIn("alice@example.com", prepared.message["To"])
        self.assertIn("bcc@example.com", prepared.recipients)
        self.assertEqual(len(prepared.attachments), 1)

    def test_subject_injection_is_rejected(self):
        args = qm.build_parser().parse_args(
            ["--to", "alice@example.com", "--subject", "Hi\nBcc: evil@example.com", "--body", "x"]
        )
        prepared, issues = qm.build_message(base_cfg(), args)
        self.assertIsNone(prepared)
        self.assertIn("E_SUBJECT_INVALID", [issue.code for issue in issues])

    def test_attachment_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "big.bin"
            path.write_bytes(b"x" * (21 * 1024 * 1024))
            args = qm.build_parser().parse_args(
                [
                    "--to",
                    "alice@example.com",
                    "--subject",
                    "Big",
                    "--body",
                    "x",
                    "--attach",
                    str(path),
                ]
            )
            prepared, issues = qm.build_message(base_cfg(), args)
        self.assertIsNone(prepared)
        self.assertIn("E_ATTACHMENT_TOO_LARGE", [issue.code for issue in issues])

    def test_preflight_reports_multiple_missing_items(self):
        args = qm.build_parser().parse_args([])
        issues, prepared, auth = qm.preflight(qm.default_config(), [], args)
        codes = {issue.code for issue in issues}
        self.assertIn("E_ACCOUNT_MISSING", codes)
        self.assertIn("E_AUTH_MISSING", codes)
        self.assertIn("E_TO_MISSING", codes)
        self.assertIn("E_SUBJECT_MISSING", codes)
        self.assertIn("E_BODY_MISSING", codes)
        self.assertIsNone(prepared)
        self.assertIsNone(auth)

    def test_preflight_auth_missing(self):
        fake = FakeKeyring()
        args = message_args()
        with patch.object(qm, "get_keyring", return_value=fake):
            issues, prepared, auth = qm.preflight(base_cfg(), [], args)
        self.assertIn("E_AUTH_MISSING", [issue.code for issue in issues])
        self.assertIsNone(auth)
        self.assertIsNotNone(prepared)  # message itself is valid

    def test_dry_run_with_fake_config_and_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            fake = FakeKeyring()
            fake.set_password(qm.KEYRING_SERVICE, "me@qq.com", "auth-code")
            args = qm.build_parser().parse_args(
                ["--dry-run", "--to", "alice@example.com", "--subject", "Hi", "--body", "x"]
            )
            with (
                patch.object(qm, "CONFIG_PATH", path),
                patch.object(qm, "get_keyring", return_value=fake),
                patch.object(qm, "check_smtp_login", return_value=None),
            ):
                qm.save_config(base_cfg())
                code = qm.run_message(args, dry_run=True)
            self.assertEqual(code, 0)

    def test_send_via_fake_smtp(self):
        args = qm.build_parser().parse_args(
            ["--to", "alice@example.com", "--bcc", "bcc@example.com", "--subject", "Hi", "--body", "x"]
        )
        prepared, issues = qm.build_message(base_cfg(), args)
        self.assertEqual(issues, [])
        fake = FakeSMTP()
        qm.send_via_smtp(base_cfg(), prepared, "auth-code", smtp_factory=lambda: fake)
        self.assertEqual(fake.login_args, ("me@qq.com", "auth-code"))
        self.assertEqual(fake.sent[1], "me@qq.com")
        self.assertIn("bcc@example.com", fake.sent[2])

    def test_render_issues_shows_raw_and_recorded_fix(self):
        cfg = {"error_recipes": {"E_SMTP_UNKNOWN": {"fix": "重启网络后成功"}}}
        issue = qm.Issue(
            "E_SMTP_UNKNOWN",
            "未分类 SMTP 错误",
            raw="SMTPDataError: something odd",
        )
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            qm.render_issues([issue], cfg)
        text = output.getvalue()
        self.assertIn("原文：", text)
        self.assertIn("善后：", text)
        self.assertIn("已记录的处理：重启网络后成功", text)

    def test_feedback_body_sanitizes_email_and_path(self):
        body = qm.build_feedback_body(
            "E_X",
            {
                "raw": "failed for secret@example.com at C:\\Users\\Lanyora\\secret.txt",
                "fix": "已重置授权码",
            },
        )
        self.assertNotIn("secret@example.com", body)
        self.assertNotIn("C:\\Users\\Lanyora", body)
        self.assertIn("<email>", body)
        self.assertIn("<path>", body)
        self.assertIn("已重置授权码", body)

    def test_feedback_dry_run_previews_without_sending(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            fake = FakeKeyring()
            fake.set_password(qm.KEYRING_SERVICE, "me@qq.com", "auth-code")
            cfg = base_cfg()
            cfg["feedback_email"] = "author@example.com"
            cfg["error_recipes"] = {
                "E_X": {
                    "raw": "failed for secret@example.com at C:\\Users\\Lanyora\\secret.txt",
                    "fix": "已重置授权码",
                }
            }
            args = qm.build_parser().parse_args(["--feedback", "E_X", "--dry-run"])
            with (
                patch.object(qm, "CONFIG_PATH", path),
                patch.object(qm, "get_keyring", return_value=fake),
                patch.object(qm, "check_smtp_login", return_value=None),
            ):
                qm.save_config(cfg)
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = qm.run_feedback(args, dry_run=True)
            self.assertEqual(code, 0)
            text = output.getvalue()
            self.assertIn("author@example.com", text)
            self.assertIn("DRY RUN", text)
            self.assertNotIn("secret@example.com", text)

    def test_feedback_send_requires_explicit_yes_and_uses_sender(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            fake = FakeKeyring()
            fake.set_password(qm.KEYRING_SERVICE, "me@qq.com", "auth-code")
            cfg = base_cfg()
            cfg["feedback_email"] = "author@example.com"
            cfg["error_recipes"] = {"E_X": {"raw": "raw", "fix": "fixed"}}
            args = qm.build_parser().parse_args(["--feedback", "E_X", "--yes"])
            send = Mock()
            with (
                patch.object(qm, "CONFIG_PATH", path),
                patch.object(qm, "get_keyring", return_value=fake),
                patch.object(qm, "check_smtp_login", return_value=None),
                patch.object(qm, "send_via_smtp", send),
            ):
                qm.save_config(cfg)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = qm.run_feedback(args, dry_run=False)
            self.assertEqual(code, 0)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0]["account"], "me@qq.com")

    def test_unknown_fingerprint_ignores_numbers(self):
        first = qm.unknown_code(RuntimeError("failed at 123456"))
        second = qm.unknown_code(RuntimeError("failed at 789012"))
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("E_UNKNOWN_"))

    def test_unclassified_smtp_error_uses_specific_fingerprint(self):
        issue = qm.issue_from_exception(
            smtplib.SMTPDataError(550, b"unexpected response"),
            phase="send",
        )
        self.assertTrue(issue.code.startswith("E_UNKNOWN_"))
        self.assertIn("SMTPDataError", issue.raw)

    def test_setup_flow_with_fake_keyring(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            fake = FakeKeyring()
            tty = FakeTTY()
            with (
                patch.object(qm, "CONFIG_PATH", path),
                patch.object(qm.sys, "stdin", tty),
                patch.object(qm.sys, "stdout", tty),
                patch.object(qm, "get_keyring", return_value=fake),
                patch.object(qm, "check_smtp_login", return_value=None),
                patch("builtins.input", side_effect=["me@qq.com", "Me"]),
                patch.object(qm.getpass, "getpass", side_effect=["auth-code", "auth-code"]),
            ):
                code = qm.run_setup()
                loaded, issues = qm.load_config()
            self.assertEqual(code, 0)
            self.assertEqual(issues, [])
            self.assertEqual(loaded["account"], "me@qq.com")
            self.assertEqual(
                fake.store[(qm.KEYRING_SERVICE, "me@qq.com")],
                "auth-code",
            )

    def test_forget_auth(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "config.json"
            fake = FakeKeyring()
            fake.set_password(qm.KEYRING_SERVICE, "me@qq.com", "auth-code")
            with (
                patch.object(qm, "CONFIG_PATH", path),
                patch.object(qm, "get_keyring", return_value=fake),
            ):
                qm.save_config(base_cfg())
                code = qm.run_forget_auth()
            self.assertEqual(code, 0)
            self.assertNotIn((qm.KEYRING_SERVICE, "me@qq.com"), fake.store)


if __name__ == "__main__":
    unittest.main(verbosity=2)
