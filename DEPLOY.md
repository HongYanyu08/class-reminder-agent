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

## 根治版自动化（任务计划）：tick 脱离主会话（2026-10-28 起推荐）

上面第 5 步的 `cr-tick-5min` 方案能用，但 2026-10-28 实测暴露了结构
问题：每 5 分钟一个 agent 回合全都堆在主会话里，一天下来会话涨到
171k token，回合越来越慢；当天下午一个回合卡住，后面的 tick 根本
没执行，体育课的出门/课前提醒**没有排队就丢了**（不是迟到，是从未
生成发送记录）。根治版把链路拆开：

```
Windows 任务计划（每 5 分钟）→ scripts\tick_task.cmd
  ├─ run_scheduler.py --tick --send   确定性 tick：排队/重排/备用补发
  └─ speak_trigger.py                 只有 queued 非空才一次性唤醒主会话
主会话（小课）被 cr-speak-* 唤醒 → run_scheduler.py --speak-pending
  → 逐条原样念出 → --confirm-sent 回写
```

没有待念提醒时主会话整天不用醒，上下文只随真实聊天增长；tick 永远
不会被会话堵住；备用通道补发也只依赖脚本，最坏情况是晚
`fallbackAfterMin` 分钟从备用通道到，不再无声丢失。从旧方案切换的
顺序必须照下面来，保证任何一步失败都能退回去：

**① 更新代码。** 按老规矩：先备份笔记本上的 `data\`，再用新版代码
覆盖项目目录（`data\` 不动）。确认新文件都在：
`scripts\tick_task.cmd`、`scripts\install_tick_task.cmd`、
`scripts\speak_trigger.py`、`scripts\uninstall_tick_task.cmd`。

**② 先 disable 旧 cron（不要删，留着回滚）。**

```powershell
openclaw.cmd cron list
openclaw.cmd cron disable <cr-tick-5min 的 id>
```

**③ 核对一次性唤醒命令的语法。** 说话触发器默认执行的命令模板是

```
openclaw.cmd cron add --name cr-speak-{stamp} --at +20s --session main --system-event {text} --delete-after-run
```

这条命令的确切语法**没有在目标机上验证过**。先在笔记本上看
`openclaw.cmd cron add --help`，逐项核对 `--at` 的写法、`--session`、
`--system-event`、`--delete-after-run` 是否都受支持；如果需要把
`{text}` 用引号包起来或参数名不同，不要改代码，设置环境变量覆盖
模板（唤醒文案本身不含任何引号字符，就是为这个留的）：

```powershell
setx REMINDER_SPEAK_CMD "openclaw.cmd cron add --name cr-speak-{stamp} --at +20s --session main --system-event ""{text}"" --delete-after-run"
```

（`setx` 写的是用户环境变量，新开的任务计划进程能读到；改完先跑一次
下面的 `--dry-run` 确认渲染出的命令无误。）

**④ 注册任务计划。** 在项目目录里运行：

```powershell
scripts\install_tick_task.cmd
schtasks /Query /TN "ClassReminderTick"
```

**⑤ 冒烟测试（全链路走一遍）。** 用模拟时间把今晚的前晚预告排队，
再手动跑一遍任务计划脚本，看小课是否自己醒来念出并回写：

```powershell
py scripts\run_scheduler.py --tick --send --from 2026-10-28 --now 2026-10-28T21:00:00+08:00
py scripts\speak_trigger.py --dry-run
scripts\tick_task.cmd
```

（日期按你测试当天改。）`--dry-run` 那行应打印出完整的唤醒命令且
不真正执行；跑完 `tick_task.cmd` 后等约半分钟，小课应在微信里念出
预告；然后检查 `data\reminders.json` 里对应任务已变成 `sent`。
注意触发器有 10 分钟节流（状态在 `data\speak_trigger_state.json`）：
重复冒烟时若发现没再发唤醒，先看是不是被节流了，删掉这个状态
文件即可立即再试。

**⑥ 收尾与回滚。** 冒烟通过后删除旧 cron：
`openclaw.cmd cron delete <cr-tick-5min 的 id>`（子命令名以
`openclaw.cmd cron --help` 为准）。任何一步失败则回滚：
`openclaw.cmd cron enable <cr-tick-5min 的 id>` 恢复旧链路，再运行
`scripts\uninstall_tick_task.cmd` 撤掉任务计划，系统回到切换前状态。

两条 Windows 实机经验（2026-10-28 晚冒烟实测，后来者别再踩）：
一是 Windows 的 Python 没有系统时区库、通常也没装 tzdata 包，
`zoneinfo.ZoneInfo("Asia/Shanghai")` 会直接抛错——核心代码已用
`src/tzutil.py` 做分层兜底（ZoneInfo → 上海固定 UTC+8 → 本地
时区），tick 不带 `--from` 也不会再崩，不要为此装包或改系统。
二是 `scripts\*.cmd` 必须保持纯 ASCII：GBK 的 cmd.exe 会把
UTF-8 中文注释拆成乱码当命令执行，所以那三个脚本的注释只有
英文，以后改它们时不要写回中文。

## 省钱与防烧干设置（2026-10-29 实测）

这三条都是网关侧设置，不改项目文件，但直接决定运行成本和会不会
"没钱全天静默"。背景数据：旧链路（每 5 分钟一个主会话回合 + 默认
pro 模型 + 会话无限增长）曾在一天内烧掉 61,053,456 token /
¥16.52（375 次请求，平均每次重发约 16 万 token 的上下文），当晚
余额耗尽——第二天早上全部提醒缺席，就是余额归零的连锁反应（智能
体每个回合都在计费错误上失败，定时任务连续报错后被网关自动停
用）。根治版架构已经把 tick 挪出主会话，剩下的花销来自模型档位
和会话上下文，按下面三条收住：

**① 默认模型选便宜档。** onboard 向导可能默认选到
`deepseek-v4-pro`（当前 OpenClaw 版本的本地模型目录只预注册了
它）。实测 `deepseek-v4-flash` 可用且便宜得多，提醒场景完全够
用；而 `deepseek-chat` 这个 ID 在现在的 DeepSeek API 上不存在，
设了会报 "model not found"。切换：

```powershell
openclaw.cmd models set deepseek/deepseek-v4-flash
openclaw.cmd gateway restart
openclaw.cmd models status   # 确认 Default 已是 v4-flash，再发条消息实测能回
```

（目录外 ID 保存时会提示 not in catalog 的警告，以 `models status`
和实际对话能通为准。）

**② 会话每天自动重置。** 主会话上下文随聊天和唤醒无限增长（实测
3 天涨到 234k token），每个回合都要全量重发，这是日常花销的大
头。设成每天凌晨自动开新会话：

```powershell
openclaw.cmd config set session.reset.mode daily
openclaw.cmd config set session.reset.atHour 4
openclaw.cmd config get session   # 应回显 {"reset": {"mode": "daily", "atHour": 4}}
```

智能体的规矩和长期记忆都在 workspace 文件里，重置聊天记录不影
响干活。想立刻瘦身可以在微信里发 `/new`（`/compact` 也行，但它
本身要调一次模型，余额不足时压不动）。

**③ 开 DeepSeek 余额预警。** platform.deepseek.com 的用量信息页
有「余额预警」设置（默认未开启），设一个阈值（如 ¥5）。余额归零
前没有任何先兆，唯一的提前量就是这个预警；归零后的表现见本节
开头——不是提醒迟到，而是全天静默。

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
| 智能体在线但自动提醒没到微信 | 大概率 token 失效：先在微信里给 ClawBot 发一条消息，等下一个 5 分钟 tick；再检查笔记本是否睡着了、网关是否在跑（`openclaw.cmd gateway status`）。**根治办法是开备用通道兜底**（2026-10-28 实测：过夜后 08:05 的出门提醒因 token 失效迟到 50 分钟，用户发消息刷新 token 后才积压送达；重排机制保证不丢，但治不了迟到）：① 去 [Server酱](https://sct.ftqq.com/)（微信扫码登录 →「SendKey」）或 [PushPlus](https://www.pushplus.plus/)（微信扫码登录 →「个人中心」里的 token）注册，拿到 key；② 打开 Web 配置 UI 的「发送渠道」页，在「备用通道（主会话兜底）」里选对应服务、粘贴 key、补发阈值默认 15 分钟即可，点「发送备用通道测试」确认能收到，再保存。开启后，提醒排队（queued）满阈值还没被确认送达，tick 会自动改走备用通道补发（推送到你的微信服务号消息），任务记为 `sent`、channel 为 `fallback:serverchan` / `fallback:pushplus`；tick 输出里会有 `[备用通道] X 条经 … 补发成功` 一行可查。key 只存在本机 `data/sender_settings.json`（已 gitignore），页面上只显示打码版 |
| 同一条提醒被说了两次 | queued 后 15 分钟没等到 `--confirm-sent` 回写会自动回退重排（至少一次投递的恢复机制）。偶发一次属正常；反复出现说明智能体回写那步在失败，去 TUI 里看它的执行记录 |
| 某条提醒状态是 failed | failed 不自动重试（防止坏渠道每 5 分钟刷屏）。修好原因后，把 `data/reminders.json` 里对应任务的 `status` 改回 `pending` 再等下一轮 tick |
| PowerShell 里 `npx`/`openclaw` 提示脚本被禁止运行 | 用 `.cmd` 后缀版本（`npx.cmd`、`openclaw.cmd`），不要去改系统执行策略 |
| `openclaw.cmd doctor` 报 state DB schema 不兼容 | 机器上有两个 OpenClaw 版本在抢状态库。关掉旧版，备份并重命名 `~/.openclaw`，只用一套 CLI 重新 onboard（见第 1 步的警告） |
| 手动跑 tick 想指定时间 | `--now` 请带时区偏移，如 `--now 2026-10-27T21:05:00+08:00`；不带偏移也不会崩，会按 `term.json` 的 timezone 解释 |
