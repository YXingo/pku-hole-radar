# macOS 本地定时运行

模板文件 `com.pku-hole-radar.run-once.plist.template` 使用绝对整点日历调度，不包含 token、
Cookie 或其他环境变量：01:00–07:30 每 30 分钟，08:00 至次日 00:50 每 10 分钟。默认不会自动安装
或启用 LaunchAgent；本机在用户明确授权后才加载。调度使用 macOS 的本地系统时区；本机当前
为中国标准时间，与项目配置的 `Asia/Shanghai` 一致。

## 安装前检查

1. 将模板复制为 `~/Library/LaunchAgents/com.pku-hole-radar.run-once.plist`。模板已包含上述
   10/30 分钟分时日历，不需要再设置 `StartInterval`；`RunAtLoad=false` 可避免加载任务时产生
   非整点执行。
2. 把其中所有 `/ABSOLUTE/PATH/PKUHoleRadar` 替换为项目绝对路径，并确认解释器与
   `config.toml` 实际存在。
3. 先手动执行 `doctor`、`probe-source`，再由用户明确允许时执行一次
   `notify-test`。`run-once` 需要 `source.contract_confirmed = true`、树洞会话和
   PushPlus 配置。
4. 用 `plutil -lint ~/Library/LaunchAgents/com.pku-hole-radar.run-once.plist` 检查
   plist 语法；语法通过不代表树洞或微信联调通过。

## 启用与停用

确认配置和测试消息无误后，由用户在本机执行：

```text
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pku-hole-radar.run-once.plist
launchctl print gui/$(id -u)/com.pku-hole-radar.run-once
```

停用但保留数据库和日志：

```text
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.pku-hole-radar.run-once.plist
```

停用不会删除 `var/pku-hole-radar.sqlite3`。电脑睡眠期间不会持续补采历史，唤醒后最多
由一次 `run-once` 处理当前状态；如长时间离线导致无法判断覆盖范围，再显式使用
`baseline-reset --ack-skip-unseen`，不要把它作为自动恢复动作。
