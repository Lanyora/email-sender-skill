# 📧 qq-email-sender

一个给 Codex / DSH 用的 QQ 邮箱发信技能。你负责说清楚收件人和内容，脚本负责检查账号、授权码、SMTP、附件并发送。

## 📦 安装

`qq-email-sender` 文件夹就是完整技能。

把它放到你的 skills 目录，或者直接把当前路径交给你的 Agent，让它帮你安装。

## ✅ 依赖

- Python 3.10+
- `keyring`

```powershell
python -m pip install -r requirements.txt
```

> 🔐 你的 QQ 邮箱授权码只保存在本机系统钥匙串（Windows Credential Manager）。
> 它不会上传到 GitHub，不会写进 README 或配置文件，也不会作为邮件内容发送。

## 🚀 开始使用

1. 去 QQ 邮箱网页版开启 SMTP，生成授权码。
2. 在真实终端完成首次配置：

```powershell
cd qq-email-sender
python scripts/qq_mail.py --setup
```

3. 先预览，不发送：

```powershell
python scripts/qq_mail.py --dry-run --to "someone@example.com" --subject "测试" --body "你好"
```

4. 确认后发送：

```powershell
python scripts/qq_mail.py --to "someone@example.com" --subject "测试" --body "你好" --yes
```

附件加 `--attach "file.pdf"`；长正文用 `--body-file report.txt`。

## 👥 联系人

```powershell
python scripts/qq_mail.py --contacts
python scripts/qq_mail.py --set-contact "BYD工作邮箱" --email "name@company.com" --note "我的工作邮箱"
```

同名即更新。收件人不明确时，Agent 会先查联系人；地址已经在当前对话出现时不会重复查。

## 🧠 错误记录

遇到未分类错误时，脚本会显示原文、善后方式和下一步。修复验证成功后可以记录：

```powershell
python scripts/qq_mail.py --record-fix E_UNKNOWN_xxxxxxxx --fix "有效的处理方式"
```

下次相同错误会直接显示这条处理经验。

## 📮 可选反馈

默认反馈邮箱：

```text
lanyorax@foxmail.com
```

用户解决新错误并记录后，脚本会询问是否发送反馈。只有用户选择、预览、再次确认后才会发。

正文只包含错误代码、脱敏原文、处理方式和运行环境；不包含收件人、邮件正文、附件或联系人。反馈从用户 QQ 邮箱发出，维护者会看到发件地址。

## ⚠️ 边界

- 仅支持 QQ 邮箱 SMTP，固定一个发件账号
- 纯文本 + 附件，不支持 HTML
- 单封最多 20 个收件人，附件上限 20 MB
- 只发信，不读取收件箱
- 邮件不能撤回；新收件人、敏感内容或实质变化仍会先确认

## 🧪 测试

```powershell
python -m unittest discover -s tests -v
```
