# PKUHoleRadar

`pku-hole-radar` 是一个单进程、SQLite、本地定时运行的北大树洞新帖提醒工具。它按帖子
ID 去重，支持可选关键词筛选，并将一轮匹配结果合并为一条 PushPlus 微信渠道消息。首版
不提供 Web 服务、不抓取历史全量、不读取评论、不下载媒体、不调用 AI。

当前状态：离线采集、去重、筛选、简报、outbox 与失败恢复已实现；自动测试、真实列表读取、
PushPlus 服务端受理及微信实际收件均已完成一次本地验证。真实接口不是稳定的公开 API，当前
验证不构成平台兼容性保证。接口证据与已知限制见 [接口研究记录](docs/INTEGRATION.md)。

## 使用边界

本项目是非官方个人项目，与北京大学及北大树洞没有隶属、合作或背书关系。北大树洞当前服务
协议对非官方客户端、自动脚本以及内容传播设有限制。使用者必须自行阅读并遵守适用的平台协议、
学校规定和法律，并在获得所需授权后使用；本项目不提供账号、不绕过认证，也不附带任何真实
树洞内容。

## 安装

项目要求 Python 3.11+。在项目根目录执行：

```text
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pip install --no-deps -e .
cp config.example.toml config.toml
cp .env.example .env
chmod 600 .env
```

`config.toml` 和 `.env` 都是本地文件，不要提交到版本库，也不要把 token、Cookie 或原始
响应粘贴到对话中。状态目录默认为 `var/`，数据库、锁和日志均留在本机。

## 先做离线验证

示例配置没有真实接口地址和凭据，因此可以安全执行：

```text
.venv/bin/python -m pku_hole_radar --config config.example.toml doctor
.venv/bin/python -m pku_hole_radar --config config.example.toml preview --fixture tests/fixtures/basic.json
.venv/bin/python -m pytest
.venv/bin/python -m ruff check .
.venv/bin/python -m ruff format --check .
```

fixture 预览使用临时数据库，stdout 的 `accepted` 只表示本地输出已接收，不代表微信收到。

## 配置真实接入

先阅读 `docs/INTEGRATION.md`。在本地 `config.toml` 填写你通过合法会话确认过的
`source.endpoint` 和 `timestamp_unit = "seconds"`；首次 `probe-source` 可以暂时留空
`source.post_url_template`，正式运行前必须补上经确认的固定链接模板。然后在 `.env` 填写：

```text
PKUHOLE_TOKEN=...
PKUHOLE_UUID=...
PKUHOLE_XSRF_TOKEN=...
PKUHOLE_COOKIE_PKU_TOKEN=...
PKUHOLE_SESSION_COOKIE=...
PUSHPLUS_TOKEN=...
```

后四个树洞字段按本地会话实际情况填写，不需要把密码交给程序。先保持
`source.contract_confirmed = false`，执行：

```text
.venv/bin/python -m pku_hole_radar --config config.toml doctor
.venv/bin/python -m pku_hole_radar --config config.toml probe-source
```

`probe-source` 只请求一轮最新列表，不建立业务基线、不生成 outbox；它仍遵守请求间隔和
整轮运行看门狗。
只有你确认返回字段、排序和分页语义后，才将 `contract_confirmed` 改为 `true`。之后可用
`preview --live` 查看候选；它不改生产基线或 outbox，但会持久化采集限频和冷却状态。

建议分两条链路、按以下顺序联调：

1. 先把 `[notify] provider` 从示例的 `stdout` 改成 `pushplus`，仅在本地 `.env` 填写
   `PUSHPLUS_TOKEN`，执行 `notify-test`。它只发送合成文本、不访问树洞；服务端返回受理流水号
   只能证明 PushPlus 已受理，不等于微信实际收到。
2. 再填写树洞会话字段，保持 `source.contract_confirmed = false`，执行 `probe-source`。
   根据返回的字段、分页、排序和手机链接逐项确认后，才在本地改为 `true`；不要把测试 fixture
   的路径当作官网链接。
3. 先执行 `preview --live` 检查候选，再执行一次 `run-once --source live`。首次正式成功采集
   只建立基线，不通知启动前旧帖。

通知渠道冷却会同时约束正常批次、`notify-test` 和人工重新排队后的发送，并在重启后保留。遇到
`unknown` 或 `failed`，先执行 `status` 找到批次 ID，再用 `outbox list --status unknown`
或 `outbox list --status failed` 查看结构化错误，最后按确认结果执行 `outbox retry ID`
（unknown 需追加 `--ack-possible-duplicate`）或 `outbox discard ID`。

真实运行入口是：

```text
.venv/bin/python -m pku_hole_radar --config config.toml run-once --source live
```

首次成功列表页只建立基线，不通知启动前旧帖。没有匹配新帖不会创建消息；采集不完整会保留
旧水位并在状态和简报中标明 `incomplete`。`status`、`posts --batch ID`、`outbox list`
以及 `outbox retry ID [--ack-possible-duplicate]` / `outbox discard ID` 用于本地恢复。

## 定时运行

项目只提供 macOS LaunchAgent 模板，不会自行安装或启用后台任务。模板按系统本地时区分时
触发：01:00–07:30 每 30 分钟，08:00 至次日 00:50 每 10 分钟。确认真实接入和合成测试后，
按 [部署说明](deploy/launchd/README.md) 替换绝对路径并安装。卸载任务不会删除数据库。

## 证据边界

“离线验证通过”“树洞接入验证通过”和“微信渠道验证通过”是三个不同结论。仓库测试只保证
离线行为；每位使用者仍需使用自己的合法会话独立验证真实树洞连接和微信收件。服务端返回
`accepted` 仅表示请求已受理，不等于最终送达。

## 许可证

项目代码以 [MIT License](LICENSE) 发布。该许可证只授权使用本仓库代码，不授予北大树洞
接口、数据、名称、标识或第三方内容的任何权利。
