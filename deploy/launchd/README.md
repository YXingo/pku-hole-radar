# macOS 本地定时运行

模板文件 `com.pku-hole-radar.run-once.plist.template` 使用绝对整点日历调度，不包含 token、
Cookie 或其他环境变量：01:00–07:30 每 30 分钟，08:00 至次日 00:50 每 10 分钟。默认不会自动安装
或启用 LaunchAgent；本机在用户明确授权后才加载。调度使用 macOS 的本地系统时区；本机当前
为中国标准时间，与项目配置的 `Asia/Shanghai` 一致。

## 安装前检查

1. 确认项目路径、`.venv/bin/python`、`config.toml` 和 `.env` 都存在；`.env` 权限应为 600。
   先完成 `doctor`、`probe-source`、合成 `notify-test` 和首次基线/增量验收。
2. 生成当前用户的实际 plist。下面命令不会改动项目模板，也不会读取或写入 token：

   ```bash
   cd "/绝对路径/PKUHoleRadar"
   mkdir -p "$HOME/Library/LaunchAgents"
   sed 's|/ABSOLUTE/PATH/PKUHoleRadar|/绝对路径/PKUHoleRadar|g' \
     deploy/launchd/com.pku-hole-radar.run-once.plist.template \
     > "$HOME/Library/LaunchAgents/com.pku-hole-radar.run-once.plist"
   plutil -lint "$HOME/Library/LaunchAgents/com.pku-hole-radar.run-once.plist"
   ```

   请把命令中的两处 `/绝对路径/PKUHoleRadar` 换成实际路径；如果路径含空格，保持引号。
   模板已包含上述 10/30 分钟分时日历，不需要再设置 `StartInterval`；`RunAtLoad=false` 可避免
   加载任务时产生非整点执行。
3. `plutil` 显示 `OK` 后再启用。语法通过不代表树洞或微信联调通过。

## 启用与停用

确认配置和测试消息无误后，由用户在本机执行：

```text
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.pku-hole-radar.run-once.plist
launchctl print gui/$(id -u)/com.pku-hole-radar.run-once
```

任务是“触发一次、进程退出”的模式；`launchctl print` 中看到当前没有运行进程不等于任务
失效。检查最近执行结果和下一次时间时，可查看：

```bash
tail -n 40 var/launchd.stdout.log
tail -n 40 var/launchd.stderr.log
.venv/bin/python -m pku_hole_radar --config config.toml status
```

停用但保留数据库和日志：

```text
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.pku-hole-radar.run-once.plist
```

停用不会删除 `var/pku-hole-radar.sqlite3`。电脑睡眠期间不会持续补采历史，唤醒后最多
由一次 `run-once` 处理当前状态；如长时间离线导致无法判断覆盖范围，再显式使用
`baseline-reset --ack-skip-unseen`，不要把它作为自动恢复动作。

若任务已加载但要更新模板，先 `bootout`，重新生成 plist，再执行 `bootstrap`；不要同时加载
两个 Label 相同的副本。查看当前任务是否仍加载：

```bash
launchctl print "gui/$(id -u)/com.pku-hole-radar.run-once"
```
