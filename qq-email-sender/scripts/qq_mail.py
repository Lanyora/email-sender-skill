#!/usr/bin/env python3
"""QQ Mail SMTP sender with fixed config, keyring auth, contacts, and error recipes.

The script is intentionally non-interactive except for --setup. It never
accepts an authorization code on the command line or stores one in config.json.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import mimetypes
import os
import platform
import re
import smtplib
import ssl
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from pathlib import Path
from typing import Callable, Iterable

if os.name == "nt":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

APP_NAME = "qq-email-sender"
CONFIG_PATH = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME / "config.json"
KEYRING_SERVICE = APP_NAME

QQ_DOMAINS = {"qq.com", "foxmail.com", "vip.qq.com"}
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
MAX_RECIPIENTS = 20
MAX_ATTACHMENT_MB = 20.0
MAX_SUBJECT_LENGTH = 300
SKILL_VERSION = "2.0.0"
DEFAULT_FEEDBACK_EMAIL = "lanyorax@foxmail.com"

EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
# Keep the invoked path, including directory junctions, so error hints match
# the skill path the user actually launched. Do not call resolve() here.
SCRIPT_PATH = Path(__file__).absolute()
PYTHON_CMD = f'python "{SCRIPT_PATH}"'


class ValidationError(ValueError):
    pass


@dataclass
class Issue:
    code: str
    title: str
    raw: str = ""
    fix: str = ""
    next_step: str = ""


@dataclass
class Prepared:
    message: EmailMessage
    recipients: list[str]
    attachments: list[tuple[Path, int]]
    to: list[tuple[str, str]]
    cc: list[tuple[str, str]]
    bcc: list[tuple[str, str]]
    body: str


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def default_config() -> dict:
    return {
        "account": "",
        "display_name": "",
        "feedback_email": DEFAULT_FEEDBACK_EMAIL,
        "contacts": [],
        "error_recipes": {},
    }


def normalize_config(data) -> dict:
    result = default_config()
    if not isinstance(data, dict):
        return result
    result["account"] = str(data.get("account") or "").strip()
    result["display_name"] = str(data.get("display_name") or "").strip()
    result["feedback_email"] = (
        str(data.get("feedback_email") or "").strip() or DEFAULT_FEEDBACK_EMAIL
    )

    contacts = data.get("contacts")
    if isinstance(contacts, list):
        for contact in contacts:
            if not isinstance(contact, dict):
                continue
            name = str(contact.get("name") or "").strip()
            email = str(contact.get("email") or "").strip()
            if name and email:
                result["contacts"].append(
                    {"name": name, "email": email, "note": str(contact.get("note") or "").strip()}
                )

    recipes = data.get("error_recipes")
    if isinstance(recipes, dict):
        result["error_recipes"] = {
            str(code): dict(recipe)
            for code, recipe in recipes.items()
            if isinstance(recipe, dict)
        }
    return result


def load_config() -> tuple[dict, list[Issue]]:
    if not CONFIG_PATH.exists():
        return default_config(), []
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return default_config(), [
            Issue(
                "E_CONFIG_INVALID",
                "配置文件损坏",
                raw=f"{type(exc).__name__}: {exc}",
                fix=f"备份或修复 {CONFIG_PATH}，然后运行 {PYTHON_CMD} --setup 重建。",
                next_step="修复配置后重试。",
            )
        ]
    if not isinstance(data, dict):
        return default_config(), [
            Issue(
                "E_CONFIG_INVALID",
                "配置文件顶层不是 JSON 对象",
                fix=f"备份或修复 {CONFIG_PATH}，然后运行 {PYTHON_CMD} --setup 重建。",
                next_step="修复配置后重试。",
            )
        ]
    return normalize_config(data), []


def save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    temp.write_text(
        json.dumps(normalize_config(config), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp, CONFIG_PATH)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def valid_email(raw: str) -> str:
    value = (raw or "").strip()
    if "\r" in value or "\n" in value or not EMAIL_RE.match(value):
        raise ValidationError(f"无效邮箱地址：{raw!r}")
    return value


def validate_qq_account(raw: str) -> str:
    value = valid_email(raw)
    domain = value.rsplit("@", 1)[-1].lower()
    if domain not in QQ_DOMAINS:
        raise ValidationError(
            "发件账号必须是 QQ 邮箱（@qq.com、@foxmail.com 或 @vip.qq.com）"
        )
    return value


def parse_addresses(values: Iterable[str] | None) -> list[tuple[str, str]]:
    raw_text = ", ".join(str(value) for value in values or [])
    if not raw_text.strip():
        return []
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for display, address in getaddresses([raw_text]):
        if "\r" in display or "\n" in display:
            raise ValidationError("显示名称中不能包含换行符")
        address = (address or "").strip()
        if not address or not EMAIL_RE.match(address):
            raise ValidationError(f"无效邮箱地址：{address or raw_text!r}")
        key = address.lower()
        if key not in seen:
            seen.add(key)
            result.append((display.strip(), address))
    return result


def header_addresses(items: list[tuple[str, str]]) -> str:
    return ", ".join(formataddr((name, address)) if name else address for name, address in items)


def envelope_addresses(items: list[tuple[str, str]]) -> list[str]:
    return [address for _, address in items]


def get_keyring():
    try:
        import keyring  # type: ignore
    except ImportError:
        return None
    return keyring


def read_body(args: argparse.Namespace) -> tuple[str | None, list[Issue]]:
    if args.body is not None:
        return args.body, []
    if args.body_file is None:
        return None, [
            Issue(
                "E_BODY_MISSING",
                "缺少邮件正文",
                fix="加 --body \"...\" 或 --body-file path。",
                next_step="补充正文后重试。",
            )
        ]
    try:
        return args.body_file.read_text(encoding="utf-8"), []
    except OSError as exc:
        return None, [
            Issue(
                "E_BODY_FILE_MISSING",
                "正文文件读取失败",
                raw=f"{type(exc).__name__}: {exc}",
                fix=f"检查文件是否存在且可读：{args.body_file}",
                next_step="修复文件路径后重试。",
            )
        ]


def build_message(cfg: dict, args: argparse.Namespace) -> tuple[Prepared | None, list[Issue]]:
    issues: list[Issue] = []
    try:
        to = parse_addresses(args.to)
    except ValidationError as exc:
        to = []
        issues.append(
            Issue("E_TO_INVALID", "收件人地址无效", raw=str(exc), fix="检查 --to 地址。")
        )
    try:
        cc = parse_addresses(args.cc)
    except ValidationError as exc:
        cc = []
        issues.append(
            Issue("E_CC_INVALID", "抄送地址无效", raw=str(exc), fix="检查 --cc 地址。")
        )
    try:
        bcc = parse_addresses(args.bcc)
    except ValidationError as exc:
        bcc = []
        issues.append(
            Issue("E_BCC_INVALID", "密送地址无效", raw=str(exc), fix="检查 --bcc 地址。")
        )

    if not to:
        issues.append(
            Issue(
                "E_TO_MISSING",
                "缺少收件人",
                fix="加 --to \"someone@example.com\"。",
                next_step="补充收件人后重试。",
            )
        )
    if len(to) + len(cc) + len(bcc) > MAX_RECIPIENTS:
        issues.append(
            Issue(
                "E_TOO_MANY_RECIPIENTS",
                "收件人过多",
                raw=f"共 {len(to) + len(cc) + len(bcc)} 个，上限 {MAX_RECIPIENTS}",
                fix="分批发送，或把多人放到 BCC 并控制在 20 人以内。",
            )
        )

    subject = (args.subject or "").strip()
    if not subject:
        issues.append(
            Issue(
                "E_SUBJECT_MISSING",
                "缺少邮件主题",
                fix="加 --subject \"...\"。",
                next_step="补充主题后重试。",
            )
        )
    elif "\r" in subject or "\n" in subject:
        issues.append(
            Issue(
                "E_SUBJECT_INVALID",
                "主题包含换行符",
                fix="把主题改成单行文本。",
                next_step="修改主题后重试。",
            )
        )
    elif len(subject) > MAX_SUBJECT_LENGTH:
        issues.append(
            Issue(
                "E_SUBJECT_TOO_LONG",
                "主题过长",
                raw=f"{len(subject)} 个字符",
                fix=f"把主题控制在 {MAX_SUBJECT_LENGTH} 个字符以内。",
            )
        )

    body, body_issues = read_body(args)
    issues.extend(body_issues)

    attachments: list[tuple[Path, bytes]] = []
    total_size = 0
    for path in args.attach:
        if not path.exists() or not path.is_file():
            issues.append(
                Issue(
                    "E_ATTACHMENT_MISSING",
                    "附件不存在",
                    raw=str(path),
                    fix="检查 --attach 的路径。",
                    next_step="修正附件路径后重试。",
                )
            )
            continue
        data = path.read_bytes()
        total_size += len(data)
        attachments.append((path, data))
    if total_size > MAX_ATTACHMENT_MB * 1024 * 1024:
        issues.append(
            Issue(
                "E_ATTACHMENT_TOO_LARGE",
                "附件总大小超限",
                raw=f"{total_size / 1048576:.1f} MB",
                fix=f"附件总大小上限为 {MAX_ATTACHMENT_MB:g} MB；请压缩、拆分或改用文件链接。",
            )
        )

    if issues:
        return None, issues

    # Validation must still run when the account is missing; use a harmless
    # placeholder for message construction. Such a message is never sent.
    account = cfg.get("account") or "unknown@qq.com"
    message = EmailMessage()
    display_name = cfg.get("display_name", "")
    message["From"] = formataddr((display_name, account)) if display_name else account
    message["To"] = header_addresses(to)
    if cc:
        message["Cc"] = header_addresses(cc)
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=account.rsplit("@", 1)[-1])
    message.set_content(body or "", charset="utf-8")

    added: list[tuple[Path, int]] = []
    for path, data in attachments:
        mime, _ = mimetypes.guess_type(path.name)
        maintype, _, subtype = (mime or "application/octet-stream").partition("/")
        message.add_attachment(
            data,
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=path.name,
        )
        added.append((path, len(data)))

    return (
        Prepared(
            message=message,
            recipients=envelope_addresses(to + cc + bcc),
            attachments=added,
            to=to,
            cc=cc,
            bcc=bcc,
            body=body or "",
        ),
        [],
    )


def normalize_error_text(text: str) -> str:
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "<email>", text)
    text = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", text)
    # Keep short SMTP response codes (550/554, etc.) meaningful; normalize
    # long numbers such as timestamps and message IDs.
    text = re.sub(r"\b\d{4,}\b", "<n>", text)
    return text.strip()


def unknown_code(exc: BaseException) -> str:
    digest = hashlib.sha256(
        f"{type(exc).__name__}:{normalize_error_text(str(exc))}".encode("utf-8")
    ).hexdigest()[:8]
    return f"E_UNKNOWN_{digest}"


def issue_from_exception(exc: BaseException, phase: str) -> Issue:
    raw = f"{type(exc).__name__}: {exc}"
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return Issue(
            "E_SMTP_AUTH",
            "QQ 邮箱认证失败",
            raw=raw,
            fix="在 QQ 邮箱开启 SMTP 并重新生成授权码；不要使用登录密码。",
            next_step=f"运行 {PYTHON_CMD} --setup 重新保存授权码，然后重试。",
        )
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return Issue(
            "E_RECIPIENTS_REFUSED",
            "QQ 邮箱拒绝部分收件人",
            raw=raw,
            fix="检查收件人地址、域名和账户限制；确认地址后重试。",
            next_step="如果已经发出一部分，先检查收件箱，避免重复发送。",
        )
    if isinstance(exc, (smtplib.SMTPServerDisconnected, TimeoutError)):
        return Issue(
            "E_SMTP_NETWORK",
            "无法连接 QQ 邮箱 SMTP",
            raw=raw,
            fix="检查网络、代理和防火墙；确认 smtp.qq.com:465 可访问。",
            next_step=(
                "先检查是否已经收到邮件，再重试，避免重复发送。"
                if phase == "send"
                else "检查网络后重新运行 --dry-run。"
            ),
        )
    if isinstance(exc, smtplib.SMTPException):
        return Issue(
            unknown_code(exc),
            "QQ 邮箱返回未分类 SMTP 错误",
            raw=raw,
            fix="根据原文分析；如果已经解决，用 --record-fix 记录处理方式。",
            next_step=(
                "先检查是否已经收到邮件，再决定是否重试，避免重复发送。"
                if phase == "send"
                else "处理后可重新运行 --dry-run。"
            ),
        )
    if isinstance(exc, OSError):
        return Issue(
            "E_SMTP_NETWORK",
            "无法连接 QQ 邮箱 SMTP",
            raw=raw,
            fix="检查网络、代理和防火墙；确认 smtp.qq.com:465 可访问。",
            next_step=(
                "先检查是否已经收到邮件，再重试，避免重复发送。"
                if phase == "send"
                else "检查网络后重新运行 --dry-run。"
            ),
        )
    return Issue(
        unknown_code(exc),
        "未分类错误",
        raw=raw,
        fix="根据原文分析；如果已经解决，用 --record-fix 记录处理方式。",
        next_step="处理后可重新运行原命令。",
    )


def check_smtp_login(account: str, auth_code: str) -> Issue | None:
    try:
        with smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=20,
            context=ssl.create_default_context(),
        ) as server:
            server.login(account, auth_code)
    except Exception as exc:  # noqa: BLE001
        return issue_from_exception(exc, phase="login")
    return None


def send_via_smtp(
    cfg: dict,
    prepared: Prepared,
    auth_code: str,
    smtp_factory: Callable[[], object] | None = None,
) -> None:
    factory = smtp_factory or (
        lambda: smtplib.SMTP_SSL(
            SMTP_HOST,
            SMTP_PORT,
            timeout=30,
            context=ssl.create_default_context(),
        )
    )
    with factory() as server:
        server.login(cfg["account"], auth_code)
        server.send_message(
            prepared.message,
            from_addr=cfg["account"],
            to_addrs=prepared.recipients,
        )


def preflight(
    cfg: dict,
    config_issues: list[Issue],
    args: argparse.Namespace,
) -> tuple[list[Issue], Prepared | None, str | None]:
    issues = list(config_issues)
    if any(issue.code == "E_CONFIG_INVALID" for issue in config_issues):
        prepared, message_issues = build_message(cfg, args)
        return issues + message_issues, prepared, None

    account = cfg.get("account", "").strip()
    account_ok = False
    if not account:
        issues.append(
            Issue(
                "E_ACCOUNT_MISSING",
                "未配置 QQ 邮箱账号",
                fix=f"请用户在本机终端运行：{PYTHON_CMD} --setup",
                next_step="账号配置完成后重试。",
            )
        )
        issues.append(
            Issue(
                "E_AUTH_MISSING",
                "未找到 QQ 邮箱授权码（账号未配置，无法定位）",
                fix=f"运行：{PYTHON_CMD} --setup（会同时配置账号和授权码）",
                next_step="运行 --setup 后重试。",
            )
        )
    else:
        try:
            validate_qq_account(account)
            account_ok = True
        except ValidationError as exc:
            issues.append(
                Issue(
                    "E_ACCOUNT_INVALID",
                    "QQ 邮箱账号无效",
                    raw=str(exc),
                    fix=f"运行：{PYTHON_CMD} --setup，输入完整的 QQ 邮箱地址。",
                    next_step="修复账号后重试。",
                )
            )

    auth_code: str | None = None
    if account_ok:
        keyring = get_keyring()
        if keyring is None:
            issues.append(
                Issue(
                    "E_KEYRING_MISSING",
                    "未安装 keyring",
                    fix=f"在终端运行 python -m pip install keyring，然后运行 {PYTHON_CMD} --setup。",
                    next_step="安装后重试。",
                )
            )
        else:
            try:
                auth_code = keyring.get_password(KEYRING_SERVICE, account)
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    Issue(
                        "E_KEYRING_READ",
                        "系统钥匙串读取失败",
                        raw=f"{type(exc).__name__}: {exc}",
                        fix="解锁或修复 Windows 凭据管理器，然后运行 --setup。",
                        next_step="修复后重试。",
                    )
                )
            if not auth_code:
                issues.append(
                    Issue(
                        "E_AUTH_MISSING",
                        "未找到 QQ 邮箱授权码",
                        fix=f"在 QQ 邮箱开启 SMTP 并生成授权码，然后运行：{PYTHON_CMD} --setup",
                        next_step="运行 --setup 后重试。",
                    )
                )

    if account_ok and auth_code:
        smtp_issue = check_smtp_login(account, auth_code)
        if smtp_issue:
            issues.append(smtp_issue)

    prepared, message_issues = build_message(cfg, args)
    issues.extend(message_issues)
    return issues, prepared, auth_code


def print_preview(prepared: Prepared, cfg: dict) -> None:
    print("=== 邮件预览 ===")
    print(f"发件人: {cfg['account']}")
    print(f"收件人: {header_addresses(prepared.to)}")
    if prepared.cc:
        print(f"抄送: {header_addresses(prepared.cc)}")
    if prepared.bcc:
        print(f"密送: {header_addresses(prepared.bcc)}")
    print(f"主题: {prepared.message['Subject']}")
    preview = re.sub(r"\s+", " ", prepared.body).strip()
    print(f"正文: {preview[:300]}{'...' if len(preview) > 300 else ''}")
    if prepared.attachments:
        print("附件:")
        for path, size in prepared.attachments:
            print(f"  - {path.name} ({size / 1024:.1f} KB)")


def recipe_for(cfg: dict, code: str) -> dict | None:
    recipes = cfg.get("error_recipes", {})
    recipe = recipes.get(code)
    return recipe if isinstance(recipe, dict) else None


def is_unknown_code(code: str) -> bool:
    return code.startswith("E_UNKNOWN_") or code in {"E_SMTP_UNKNOWN"}


def render_issues(issues: list[Issue], cfg: dict) -> None:
    for issue in issues:
        print(f"[FAIL] {issue.code}  {issue.title}")
        if issue.raw:
            raw = textwrap.indent(str(issue.raw), "              ")
            print(f"       原文：{raw.lstrip()}")
        fixes: list[str] = []
        if issue.fix:
            fixes.append(issue.fix)
        recipe = recipe_for(cfg, issue.code)
        if recipe and recipe.get("fix"):
            fixes.append(f"已记录的处理：{recipe['fix']}")
        if fixes:
            print("       善后：" + "\n              ".join(fixes))
        if issue.next_step:
            print(f"       NEXT：{issue.next_step}")
        if is_unknown_code(issue.code) and not recipe:
            print("       如果本次处理有效，运行：")
            print(
                f'         {PYTHON_CMD} --record-fix {issue.code} '
                f'--fix "你的处理方式"'
            )
        print()


def exit_code_for(issues: list[Issue]) -> int:
    codes = [issue.code for issue in issues]
    if any(code == "E_SMTP_AUTH" or code.startswith("E_AUTH") or code.startswith("E_KEYRING") for code in codes):
        return 3
    if any(
        code.startswith("E_SMTP")
        or code.startswith("E_SEND")
        or code.startswith("E_RECIPIENTS")
        or code.startswith("E_UNKNOWN")
        for code in codes
    ):
        return 4
    return 2


def run_message(args: argparse.Namespace, dry_run: bool) -> int:
    cfg, config_issues = load_config()
    issues, prepared, auth_code = preflight(cfg, config_issues, args)
    if issues:
        render_issues(issues, cfg)
        print("修复后重新运行原命令。")
        return exit_code_for(issues)
    assert prepared is not None

    if dry_run:
        print_preview(prepared, cfg)
        print("DRY RUN：未发送，未修改收件人。")
        return 0

    if not args.yes:
        render_issues(
            [
                Issue(
                    "E_CONFIRM_REQUIRED",
                    "正式发送必须加 --yes",
                    fix="确认内容后，在发送命令末尾加 --yes。",
                    next_step="用户确认后重试。",
                )
            ],
            cfg,
        )
        return 2

    print_preview(prepared, cfg)
    try:
        send_via_smtp(cfg, prepared, auth_code or "")
    except Exception as exc:  # noqa: BLE001
        issue = issue_from_exception(exc, phase="send")
        render_issues([issue], cfg)
        return exit_code_for([issue])

    print(f"已发送：{prepared.message['Subject']} -> {', '.join(prepared.recipients)}")
    print(f"Message-ID: {prepared.message['Message-ID']}")
    return 0


def run_contacts() -> int:
    cfg, issues = load_config()
    if issues:
        render_issues(issues, cfg)
        return 2
    contacts = cfg.get("contacts", [])
    if not contacts:
        print("联系人列表为空。")
        print(f'添加示例：{PYTHON_CMD} --set-contact "小号" --email test@qq.com --note "我的测试邮箱"')
        return 0
    print("联系人：")
    for index, contact in enumerate(contacts, 1):
        note = f"  {contact['note']}" if contact.get("note") else ""
        print(f"{index}. {contact['name']}  {contact['email']}{note}")
    return 0


def run_set_contact(args: argparse.Namespace) -> int:
    cfg, issues = load_config()
    if issues:
        render_issues(issues, cfg)
        return 2
    name = (args.set_contact or "").strip()
    if not name:
        print("错误: --set-contact 需要一个联系人名称。")
        return 2
    if not args.email:
        print("错误: --set-contact 需要 --email。")
        return 2
    try:
        email = valid_email(args.email)
    except ValidationError as exc:
        print(f"错误: {exc}")
        return 2
    note = args.note.strip() if args.note is not None else None
    contacts = cfg.setdefault("contacts", [])
    existing = next(
        (contact for contact in contacts if str(contact.get("name", "")).casefold() == name.casefold()),
        None,
    )
    if existing:
        existing["email"] = email
        if note is not None:
            existing["note"] = note
        action = "已更新"
    else:
        contacts.append({"name": name, "email": email, "note": note or ""})
        action = "已添加"
    save_config(cfg)
    print(f"{action}联系人：{name} <{email}>{('  ' + note) if note else ''}")
    return 0


def run_set_feedback_email(args: argparse.Namespace) -> int:
    cfg, issues = load_config()
    if issues:
        render_issues(issues, cfg)
        return 2
    try:
        email = valid_email(args.set_feedback_email or "")
    except ValidationError as exc:
        print(f"错误: {exc}")
        return 2
    cfg["feedback_email"] = email
    save_config(cfg)
    print(f"已设置错误反馈邮箱：{email}")
    print("之后记录错误时，脚本会给出 FEEDBACK 预览提示；不会自动发送。")
    return 0


def sanitize_feedback(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+", "<email>", value)
    value = re.sub(r"[A-Za-z]:\\[^\s]+", "<path>", value)
    value = re.sub(r"(?<!\w)/(?:Users|home|var|tmp|opt)/\S+", "<path>", value)
    value = re.sub(r"\b[A-Za-z0-9_-]{24,}\b", "<token>", value)
    return value.strip() or "（无原文）"


def build_feedback_body(code: str, recipe: dict) -> str:
    raw = sanitize_feedback(recipe.get("raw", ""))
    fix = sanitize_feedback(recipe.get("fix", ""))
    return (
        "qq-email-sender feedback\n\n"
        f"Error code: {code}\n"
        f"Error (sanitized):\n{raw}\n\n"
        f"Resolution:\n{fix}\n\n"
        f"App version: {SKILL_VERSION}\n"
        f"Python: {sys.version.split()[0]}\n"
        f"Platform: {platform.platform()}\n"
        f"Time: {now_iso()}\n"
    )


def run_feedback(args: argparse.Namespace, dry_run: bool) -> int:
    cfg, config_issues = load_config()
    if config_issues:
        render_issues(config_issues, cfg)
        return 2

    feedback_email = cfg.get("feedback_email", "").strip()
    if not feedback_email:
        render_issues(
            [
                Issue(
                    "E_FEEDBACK_EMAIL_MISSING",
                    "未配置错误反馈邮箱",
                    fix=f"运行：{PYTHON_CMD} --set-feedback-email \"作者邮箱\"",
                    next_step="配置后再预览反馈邮件。",
                )
            ],
            cfg,
        )
        return 2

    code = (args.feedback or "").strip()
    recipe = recipe_for(cfg, code)
    if not recipe:
        render_issues(
            [
                Issue(
                    "E_FEEDBACK_RECIPE_MISSING",
                    "没有找到这条错误的记录",
                    raw=code,
                    fix=f"先用 {PYTHON_CMD} --record-fix {code} --fix \"处理方式\" 记录。",
                    next_step="记录后再预览反馈邮件。",
                )
            ],
            cfg,
        )
        return 2

    feedback_args = argparse.Namespace(
        to=[feedback_email],
        cc=[],
        bcc=[],
        subject=f"[qq-email-sender] {code}",
        body=build_feedback_body(code, recipe),
        body_file=None,
        attach=[],
    )
    issues, prepared, auth_code = preflight(cfg, [], feedback_args)
    if issues:
        render_issues(issues, cfg)
        print("修复后重新运行反馈命令。")
        return exit_code_for(issues)
    assert prepared is not None

    if dry_run:
        print_preview(prepared, cfg)
        print("DRY RUN：反馈邮件未发送。确认后加 --yes。")
        return 0

    if not args.yes:
        render_issues(
            [
                Issue(
                    "E_CONFIRM_REQUIRED",
                    "反馈邮件需要用户确认",
                    fix="用户确认反馈内容后，在命令末尾加 --yes。",
                    next_step="确认后重试。",
                )
            ],
            cfg,
        )
        return 2

    print_preview(prepared, cfg)
    try:
        send_via_smtp(cfg, prepared, auth_code or "")
    except Exception as exc:  # noqa: BLE001
        issue = issue_from_exception(exc, phase="send")
        render_issues([issue], cfg)
        return exit_code_for([issue])

    print(f"反馈邮件已发送：{code} -> {feedback_email}")
    print(f"Message-ID: {prepared.message['Message-ID']}")
    return 0


def run_forget_auth() -> int:
    cfg, issues = load_config()
    if issues:
        render_issues(issues, cfg)
        return 2
    account = cfg.get("account", "").strip()
    if not account:
        print("错误: 未配置 QQ 邮箱账号，无法删除授权码。")
        return 2
    keyring = get_keyring()
    if keyring is None:
        print("错误: 未安装 keyring。")
        return 3
    try:
        keyring.delete_password(KEYRING_SERVICE, account)
    except Exception as exc:  # noqa: BLE001
        print(f"删除授权码失败（可能本来没有保存）：{type(exc).__name__}: {exc}")
        return 3
    print(f"已删除授权码：{account}")
    return 0


def run_record_fix(args: argparse.Namespace) -> int:
    code = (args.record_fix or "").strip()
    fix = (args.fix or "").strip()
    if not code or not fix:
        print("错误: --record-fix 需要错误代码和 --fix 处理方式。")
        return 2
    cfg, issues = load_config()
    if issues:
        render_issues(issues, cfg)
        return 2
    recipes = cfg.setdefault("error_recipes", {})
    existing = recipes.get(code, {})
    recipes[code] = {
        "fix": fix,
        "raw": args.raw or existing.get("raw", ""),
        "updated": now_iso(),
    }
    save_config(cfg)
    print(f"问题已记录，下次相同错误会直接显示这条善后方式：{code}")
    feedback_email = cfg.get("feedback_email", "").strip()
    if feedback_email:
        print(f"FEEDBACK: 可以把本次错误和解决方式发给维护者 {feedback_email}")
        print("NEXT: 询问用户是否发送反馈：")
        print("  1. 先预览反馈邮件")
        print("  2. 不发送")
        print(f"用户选 1 后运行：{PYTHON_CMD} --feedback {code} --dry-run")
        print(f"用户确认预览后再运行：{PYTHON_CMD} --feedback {code} --yes")
        print("反馈邮件会从用户的 QQ 邮箱发出，维护者会看到发件地址；")
        print("正文只包含错误代码、脱敏原文和处理方式，不含收件人、邮件正文或附件。")
    return 0


def run_setup() -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        render_issues(
            [
                Issue(
                    "E_SETUP_TTY",
                    "--setup 需要真实终端",
                    fix=f"请用户在本机 PowerShell 或 Windows Terminal 中运行 {PYTHON_CMD} --setup。",
                    next_step="用户完成配置后重试。",
                )
            ],
            {},
        )
        return 2

    cfg, issues = load_config()
    if issues:
        if any(issue.code == "E_CONFIG_INVALID" for issue in issues):
            backup = CONFIG_PATH.with_name(
                f"config.invalid-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
            )
            try:
                CONFIG_PATH.replace(backup)
                print(f"[WARN] 配置损坏，已备份到：{backup}")
                cfg = default_config()
            except OSError as exc:
                render_issues(issues, cfg)
                print(f"无法备份损坏配置：{exc}")
                return 2
        else:
            render_issues(issues, cfg)
            return 2

    keyring = get_keyring()
    if keyring is None:
        render_issues(
            [
                Issue(
                    "E_KEYRING_MISSING",
                    "未安装 keyring",
                    fix=f"在终端运行 python -m pip install keyring，然后重新运行 {PYTHON_CMD} --setup。",
                )
            ],
            cfg,
        )
        return 3

    current = cfg.get("account", "")
    account_prompt = f"QQ 邮箱账号{f' [{current}]' if current else ''}: "
    account = input(account_prompt).strip() or current
    try:
        account = validate_qq_account(account)
    except ValidationError as exc:
        render_issues(
            [
                Issue(
                    "E_ACCOUNT_INVALID",
                    "QQ 邮箱账号无效",
                    raw=str(exc),
                    fix="输入完整的 @qq.com、@foxmail.com 或 @vip.qq.com 地址。",
                )
            ],
            cfg,
        )
        return 2

    current_display = cfg.get("display_name", "")
    display_prompt = f"显示名{f' [{current_display}]' if current_display else ''}: "
    display_name = input(display_prompt).strip() or current_display

    auth_code = getpass.getpass("QQ 邮箱授权码（输入不会回显）: ").strip()
    confirm = getpass.getpass("再输入一次确认: ").strip()
    if not auth_code or auth_code != confirm:
        render_issues(
            [
                Issue(
                    "E_SETUP_MISMATCH",
                    "两次授权码不一致或为空",
                    fix=f"重新运行 {PYTHON_CMD} --setup，输入同一个授权码。",
                )
            ],
            cfg,
        )
        return 2

    smtp_issue = check_smtp_login(account, auth_code)
    if smtp_issue:
        render_issues([smtp_issue], cfg)
        return exit_code_for([smtp_issue])

    try:
        keyring.set_password(KEYRING_SERVICE, account, auth_code)
    except Exception as exc:  # noqa: BLE001
        render_issues(
            [
                Issue(
                    "E_KEYRING_WRITE",
                    "系统钥匙串写入失败",
                    raw=f"{type(exc).__name__}: {exc}",
                    fix="解锁 Windows 凭据管理器，或检查 keyring 后端。",
                )
            ],
            cfg,
        )
        return 3

    cfg["account"] = account
    cfg["display_name"] = display_name
    try:
        save_config(cfg)
    except OSError as exc:
        render_issues(
            [
                Issue(
                    "E_CONFIG_WRITE",
                    "配置文件写入失败",
                    raw=f"{type(exc).__name__}: {exc}",
                    fix=f"检查目录权限：{CONFIG_PATH.parent}",
                )
            ],
            cfg,
        )
        return 2

    print(f"配置完成：{account}")
    print(f"配置文件：{CONFIG_PATH}")
    print("现在可以运行 --dry-run。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过 QQ 邮箱 SMTP 发信；账号/联系人配置固定，授权码存系统钥匙串。"
    )
    parser.add_argument("--setup", action="store_true", help="交互式配置账号和授权码")
    parser.add_argument("--contacts", action="store_true", help="列出已保存联系人")
    parser.add_argument("--set-contact", metavar="NAME", help="新增或更新联系人")
    parser.add_argument("--email", help="联系人邮箱（配合 --set-contact）")
    parser.add_argument("--note", help="联系人备注（配合 --set-contact）")
    parser.add_argument("--forget-auth", action="store_true", help="删除当前账号的授权码")
    parser.add_argument("--set-feedback-email", metavar="EMAIL", help="设置错误反馈收件邮箱")
    parser.add_argument("--record-fix", metavar="CODE", help="记录某个错误的处理方式")
    parser.add_argument("--feedback", metavar="CODE", help="把已记录的错误反馈发给维护者")
    parser.add_argument("--fix", help="处理方式文本（配合 --record-fix）")
    parser.add_argument("--raw", help="原始错误文本（可选）")

    parser.add_argument("--to", action="append", default=[], help="收件人，可重复或逗号分隔")
    parser.add_argument("--cc", action="append", default=[])
    parser.add_argument("--bcc", action="append", default=[])
    parser.add_argument("--subject")
    body = parser.add_mutually_exclusive_group()
    body.add_argument("--body")
    body.add_argument("--body-file", type=Path)
    parser.add_argument("--attach", action="append", type=Path, default=[])
    parser.add_argument("--dry-run", action="store_true", help="只检查并预览，不发送")
    parser.add_argument("--yes", action="store_true", help="确认正式发送")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    modes = [
        args.setup,
        args.contacts,
        bool(args.set_contact),
        args.forget_auth,
        bool(args.set_feedback_email),
        bool(args.record_fix),
        bool(args.feedback),
    ]
    if sum(1 for mode in modes if mode) > 1:
        print(
            "错误: --setup、--contacts、--set-contact、--forget-auth、"
            "--set-feedback-email、--record-fix、--feedback 只能选一个。"
        )
        return 2

    try:
        if args.setup:
            return run_setup()
        if args.contacts:
            return run_contacts()
        if args.set_contact:
            return run_set_contact(args)
        if args.forget_auth:
            return run_forget_auth()
        if args.set_feedback_email:
            return run_set_feedback_email(args)
        if args.record_fix:
            return run_record_fix(args)
        if args.feedback:
            return run_feedback(args, dry_run=args.dry_run)
        if (argv is None and len(sys.argv) == 1) or argv == []:
            print("用法示例：")
            print(f'  {PYTHON_CMD} --dry-run --to "a@b.com" --subject "..." --body "..."')
            print(f'  {PYTHON_CMD} --to "a@b.com" --subject "..." --body "..." --yes')
            print(f"  {PYTHON_CMD} --contacts")
            return 2
        return run_message(args, dry_run=args.dry_run)
    except KeyboardInterrupt:
        print("已取消。", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        issue = issue_from_exception(exc, phase="internal")
        cfg, _ = load_config()
        render_issues([issue], cfg)
        return exit_code_for([issue])


if __name__ == "__main__":
    raise SystemExit(main())
