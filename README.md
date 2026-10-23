# qq-email-sender

一个面向 Codex / DSH 的 QQ 邮箱发信技能。技能负责收件人判断、内容审查和风险确认；Python 脚本负责账号、授权码、联系人、SMTP 预检、错误食谱和发送。

## 特性

- 仅支持 QQ 邮箱 SMTP（`smtp.qq.com:465` + SSL）。
- 固定一个发件账号，纯文本 + 附件。
- `--dry-run` 同时检查账号、授权码、SMTP 登录、收件人、主题和附件，不发送。
- 正式发送必须加 `--yes`，脚本不会弹出交互确认。
- 联系人支持名称、邮箱和备注；同名即更新。
- 授权码存入系统钥匙串，不写入仓库或配置文件。
- 未知错误会显示原文和善后方式，并支持 `--record-fix` 记录处理经验。
- 可选错误反馈：用户确认、脱敏预览后才发送给维护者。

## 依赖

- Python 3.10+
- `keyring`（用于保存 QQ 邮箱授权码）

```powershell
python -m pip install -r requirements.txt
```

## 目录

```text
email-sender-skill/
  README.md
  requirements.txt
  .gitignore
  qq-email-sender/
    SKILL.md
    scripts/qq_mail.py
    tests/test_qq_mail.py
```

`SKILL.md` 是给 agent 的说明；`scripts/qq_mail.py` 是实际运行的发信脚本。

## 安装

先克隆仓库，然后把**内层的 `qq-email-sender` 文件夹**复制到技能目录。不要把仓库根目录直接放进 `skills`，否则会多一层，部分客户端找不到 `SKILL.md`。

### Codex

```powershell
git clone https://github.com/Lanyora/email-sender-skill.git
Copy-Item -Recurse .\email-sender-skill\qq-email-sender "$env:USERPROFILE\.codex\skills\qq-email-sender"
```

新开一个 Codex 会话后生效。

### DSH

```powershell
Copy-Item -Recurse .\email-sender-skill\qq-email-sender "$env:USERPROFILE\.dsh\skills\qq-email-sender"
```

也可以复制到项目的 `.agents/skills/qq-email-sender`。DSH 会从这些目录发现技能。

### 一份源文件，多处使用

如果希望只维护一份代码，可以把技能放在项目 `.agents/skills/qq-email-sender`，再建立目录联接：

```bat
mklink /J "%USERPROFILE%\.codex\skills\qq-email-sender" "C:\path\to\email-sender-skill\qq-email-sender"
mklink /J "%USERPROFILE%\.dsh\skills\qq-email-sender" "C:\path\to\email-sender-skill\qq-email-sender"
```

## 首次配置

1. 登录 QQ 邮箱网页版：设置 → 账号 → POP3/IMAP/SMTP 服务。
2. 开启 SMTP，生成授权码。
3. 在真实终端运行：

```powershell
cd .\qq-email-sender
python scripts\qq_mail.py --setup
```

`--setup` 会询问 QQ 邮箱账号和授权码，先测试 SMTP 登录，成功后才保存。授权码只写入系统钥匙串，不会写入 `config.json`。

配置文件位置：

```text
%APPDATA%\qq-email-sender\config.json
```

里面只有账号、显示名、联系人、错误食谱和反馈邮箱，不含授权码。

## 使用

预览（不发信）：

```powershell
python scripts\qq_mail.py --dry-run --to "someone@example.com" --subject "测试" --body "你好"
```

附件或长正文：

```powershell
python scripts\qq_mail.py --dry-run --to "someone@example.com" --subject "报告" --body-file report.txt --attach "report.pdf"
```

正式发送：

```powershell
python scripts\qq_mail.py --to "someone@example.com" --subject "报告" --body "..." --attach "report.pdf" --yes
```

联系人：

```powershell
python scripts\qq_mail.py --contacts
python scripts\qq_mail.py --set-contact "BYD工作邮箱" --email "name@company.com" --note "我的工作邮箱"
```

删除授权码：

```powershell
python scripts\qq_mail.py --forget-auth
```

## 错误食谱

遇到未分类错误时，脚本会显示：

- `原文`：脱敏或原始错误信息
- `善后`：内置处理方式
- `NEXT`：下一步
- 记录命令：修复验证成功后，把本次处理方式写回配置

```powershell
python scripts\qq_mail.py --record-fix E_UNKNOWN_xxxxxxxx --fix "有效的处理方式"
```

下次同一个错误代码会读取配置，并显示“已记录的处理”。脚本不会在失败时自动记录，避免把错误方案沉淀成永久配置。

## 可选错误反馈

默认反馈邮箱是：

```text
lanyorax@foxmail.com
```

记录错误后，脚本会提示可以用 `--feedback` 预览反馈邮件：

```powershell
python scripts\qq_mail.py --feedback E_UNKNOWN_xxxxxxxx --dry-run
python scripts\qq_mail.py --feedback E_UNKNOWN_xxxxxxxx --yes
```

反馈邮件必须经过用户选择、预览和再次确认。正文只包含错误代码、脱敏原文、处理方式、脚本/Python/系统版本和时间；不包含用户的收件人、邮件正文、附件或联系人。

反馈邮件是从用户的 QQ 邮箱发出的，维护者会看到发件地址。用户拒绝后不会继续追问。

## 安全说明

- 不要在聊天、仓库、`SKILL.md`、脚本或 `config.json` 中保存授权码。
- 不要把授权码放在命令行参数里；脚本故意不提供 `--auth-code`。
- 单封最多 20 个收件人，附件默认上限 20 MB。
- 邮件不能撤回；新收件人、敏感内容、实质变化仍应由技能向用户确认。
- `config.json` 可能包含联系人和错误记录，请勿提交到公开仓库。

## 测试

进入技能目录后运行：

```powershell
python -m unittest discover -s tests -v
```

测试使用临时配置和假 SMTP，不会发送真实邮件，也不会读取真实授权码。
