# 部署指南：在你的 OpenClaw 上跑起微信自动上课提醒

这份指南记录的是一次**真实走通**的部署（Windows 笔记本 + OpenClaw 2026.10.5 +
官方微信插件 + DeepSeek），不是理论步骤。照着做完，你会得到：

- 你的真实课表进入 `data/`（仓库里只有示例数据，必须换成你自己的）；
- 每 5 分钟一次的自动检查：前晚预告（21:00）、出门提醒、课前提醒会自动出现在
  微信的「微信 ClawBot」对话里，你什么都不用打开。

先理解一个关键设计，后面就不会奇怪：**个人微信没有官方机器人 API**。腾讯的
ClawBot 插件走 iLink 协议，每次投递必须带上"最近一次入站消息下发的
context token"，而实测下来只有**主会话里智能体亲口说出的话**能正确带上这个
token（`openclaw message send` 直发会被 iLink 拒绝：`sendMessage ret=-3`）。
所以本项目的投递是两阶段的：调度器把到期提醒排进队列（状态 `queued`），
定时任务唤醒主会话的智能体把它逐条原样说出来，再回写已发送。部署的核心
就是把这条链路搭起来。

## 前置要求

| 需要 | 版本/说明 |
|---|---|
| Node.js | ≥ 22.13.0（微信插件要求；OpenClaw 本身也依赖它） |
| OpenClaw | ≥ 2026.3.22（建议装最新版；本指南实测于 2026.10.5） |
| Python | 3.10+（调度器与脚本仅用标准库，不需要 pip 装任何东西） |
| 微信 | iOS 微信 ≥ 8.0.70；「微信 ClawBot」入口是灰度放量，没有入口的账号绑不了 |
| 模型 | 一个 DeepSeek API key（智能体的推理用；确定性调度脚本本身不调用模型） |
| 机器 | 一台常开的电脑（部署在笔记本上就要保持开机、不合盖睡眠） |

## 第 1 步：安装 OpenClaw 并完成 onboard

Windows 在 PowerShell 里操作（注意：PowerShell 的执行策略会挡 `.ps1`，
**所有命令都用 `.cmd` 后缀**，如 `npx.cmd`、`openclaw.cmd`；macOS/Linux
直接用 `npx`/`openclaw`）：

```powershell
npm install -g openclaw        # 或按 OpenClaw 官网的最新安装方式
openclaw.cmd onboard           # 向导里：manual 配置 → 选 DeepSeek → 填你自己的 API key
openclaw.cmd gateway install   # 把网关注册为登录自启（端口 18789）
openclaw.cmd gateway start
openclaw.cmd tui               # 能和智能体正常对话即算装好
```

规矩：**API key 只在向导/终端里由你本人输入，或放环境变量
`DEEPSEEK_API_KEY`，永远不要写进任何项目文件或仓库。**

> ⚠ 如果这台机器以前装过**桌面版 OpenClaw**（另一个版本），两套程序会抢
> 同一个状态库，版本不一致时 CLI 会因为 state DB schema 不兼容而罢工
> （`openclaw.cmd doctor` 能看出来）。处理办法是关掉旧版、备份并重命名
> `~/.openclaw` 后只用一套 CLI 重新 onboard。平时也**不要去点桌面上旧版
> 的 OpenClaw 图标**。

## 第 2 步：安装微信插件并扫码登录

```powershell
npx.cmd -y @tencent-weixin/openclaw-weixin-cli install
# 或：openclaw.cmd plugins install "@tencent-weixin/openclaw-weixin"
openclaw.cmd config set plugins.entries.openclaw-weixin.enabled true
openclaw.cmd channels login --channel openclaw-weixin   # 终端出二维码，用微信扫
openclaw.cmd gateway restart
openclaw.cmd channels status                            # 看到 openclaw-weixin 已登录
```

登录成功后，你的微信里会多出一个「微信 ClawBot」联系人，发条"你好"
能收到回复就通了。

> ⚠ 登录或收发失败先检查**代理/VPN**：iLink 的域名
> （`ilinkai.weixin.qq.com`）和 DeepSeek 都是国内服务，被代理绕到境外
> 节点时会直接断（典型报错 `fetch failed`）。部署联调期间先把代理关掉。

## 第 3 步：把项目放进 OpenClaw 的 workspace

把本项目目录整体放进 OpenClaw 的 workspace，例如 Windows 上：

```
C:\Users\<你>\.openclaw\workspace\class-reminder-agent
```

（解压 ZIP 时注意别多套一层同名文件夹。）项目里的
`config/openclaw.json` 是给新装的 agent 参考的配置模板；你第 1 步已经
onboard 过的机器不需要覆盖自己的配置。

## 第 4 步：导入你自己的课表

仓库里的 `data/term.json`、`data/courses.json`、`data/exceptions.json`
**只是示例数据**（三门示例课），提醒会按它们跑——务必换成你自己的。
两种方式任选：

**方式 A：用导入器（推荐）。** 课表截图先让智能体视觉转成"一行一门课"
的文字（或直接用教务导出的 .txt/.csv/.xlsx/.json），然后：

```bash
python3 scripts/import_timetable.py 我的课表.txt     # 先预览，核对每门课
python3 scripts/import_timetable.py 我的课表.txt --apply   # 确认无误才写入（自动备份）
```

也可以打开 Web UI 的导入页完成：启动 `python3 scripts/serve_ui.py` 后进入「导入课表」页，上传文件或粘贴文字，预览确认后点「确认导入」，效果与命令行 `--apply` 相同。

识别不准的字段会被标成"待确认"，导入器**不会猜**——预览里逐条核对
周次/节次/教室再 `--apply`。同时检查 `data/term.json`：开学第一周的
周一、总周数、节假日、调休、每节课的起止时间都要和你的学校一致；
`data/user.json` 里可以改提醒偏好（前晚预告时间、课前提前量等）。

**方式 B：直接替换数据文件。** 把你准备好的 `term.json` /
`courses.json` / `exceptions.json` 覆盖进 `data/`，再删掉 `data/` 里
的旧运行状态 `occurrences.json`、`reminders.json`、
`session_outbox.jsonl`（如果有），让调度器从干净状态重新生成。

## 第 5 步：一键配置（deploy_setup.py）

```bash
python3 scripts/deploy_setup.py --dry-run   # 先预览它将做的每一步和命令（不写文件、不建任务）
python3 scripts/deploy_setup.py             # 正式执行
```

它会做三件事，每件都打印 ✓/✗：

1. 检查 Python 版本、openclaw CLI、微信插件是否就绪；
2. `data/sender_settings.json` 不存在时写入 `{"channel": "session"}`
   （已存在绝不覆盖，除非 `--force`）；
3. 创建定时任务 `cr-tick-5min`：每 5 分钟以 `--session main
   --system-event` 唤醒主会话，让智能体跑一次
   `run_scheduler.py --tick --send`，把 `[SESSION-SPEAK]` 的提醒逐条
   说到微信对话里，再执行输出里给出的 `--confirm-sent` 回写确认。
   同名任务已存在则跳过，脚本可重复运行。

## 第 6 步：首次验证

1. **先给「微信 ClawBot」随便发一条消息**（比如"测试"）。这一步是
   刷新 context token——长时间没互动时 token 会失效，第一次自动
   推送前必须刷一次。代理记得关着。
2. 等待下一次整点节奏的自动检查（最多 5 分钟）。如果当晚已过 21:00，
   明天课程的前晚预告应当被排队并出现在微信里；没到点的提醒不会
   提前发，这是正常的。
3. 想立刻验证链路：在 TUI 里对智能体说"运行一次定时上课提醒检查"，
   或等明早第一门课的出门提醒（上课前 路程+缓冲 分钟）自动到达。

三条提醒的节奏：**前晚预告**（默认前一天 21:00）→ **出门提醒**
（默认上课前 25 分钟）→ **课前提醒**（默认上课前 10 分钟）。

## 日常使用

- **不用打开任何东西**：网关随登录自启，定时任务自己跑。唯一要求是
  部署机器保持开机且不深度睡眠（合盖睡死期间的提醒会迟到或被作废，
  见下条）。
- 想找智能体聊天/看状态：打开 PowerShell（Win 键搜 `powershell`），
  运行 `openclaw.cmd tui`；连不上先 `openclaw.cmd gateway start`。
- 收到提醒后直接在微信里回复"收到""请假了""10 分钟后再提醒"，
  智能体会走回执流程回写状态；问"明天有什么课"走的是确定性查课表，
  不会编造。
- **过期提醒自动作废**：关机/睡眠错过的提醒不会在开机后一股脑补发。
  偏好项 `staleExpireMin`（默认 120 分钟）之外的 pending 任务会被
  标成 `expired` 不再发送；在 `data/user.json` 的 `preferences` 里
  可调，设 `0` 表示关闭这个机制。

## 故障排查

| 现象 | 原因与处理 |
|---|---|
| `openclaw message send` 报 `sendMessage ret=-3 invalid arguments` | 预期行为：直发路径不带 context token。本项目不走直发，确认 `data/sender_settings.json` 是 `"channel": "session"` 且 `cr-tick-5min` 任务存在（重跑 `deploy_setup.py`） |
| 登录插件/调 DeepSeek 报 `fetch failed`、超时 | 代理把国内域名绕去了境外节点。关掉 VPN/代理再试 |
| 智能体在线但自动提醒没到微信 | 大概率 token 失效：先在微信里给 ClawBot 发一条消息，等下一个 5 分钟 tick；再检查笔记本是否睡着了、网关是否在跑（`openclaw.cmd gateway status`） |
| 同一条提醒被说了两次 | queued 后 15 分钟没等到 `--confirm-sent` 回写会自动回退重排（至少一次投递的恢复机制）。偶发一次属正常；反复出现说明智能体回写那步在失败，去 TUI 里看它的执行记录 |
| 某条提醒状态是 failed | failed 不自动重试（防止坏渠道每 5 分钟刷屏）。修好原因后，把 `data/reminders.json` 里对应任务的 `status` 改回 `pending` 再等下一轮 tick |
| PowerShell 里 `npx`/`openclaw` 提示脚本被禁止运行 | 用 `.cmd` 后缀版本（`npx.cmd`、`openclaw.cmd`），不要去改系统执行策略 |
| `openclaw.cmd doctor` 报 state DB schema 不兼容 | 机器上有两个 OpenClaw 版本在抢状态库。关掉旧版，备份并重命名 `~/.openclaw`，只用一套 CLI 重新 onboard（见第 1 步的警告） |
| 手动跑 tick 想指定时间 | `--now` 请带时区偏移，如 `--now 2026-10-27T21:05:00+08:00`；不带偏移也不会崩，会按 `term.json` 的 timezone 解释 |
