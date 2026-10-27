# 上课提醒智能体（OpenClaw + DeepSeek）

基于 OpenClaw 的上课提醒智能体：把课表（文字/CSV/XLSX/JSON，或截图转文字）导入成结构化数据，
处理单双周、节假日、调休和班群调课/停课通知，按三级策略发提醒——
**前晚预告（night_before）→ 出门提醒（departure，按路程+缓冲算）→ 课前提醒（pre_class）**，
解决“课表记不住、调课通知看漏、临时换教室不知道”的问题。

## 核心原则

- **确定性代码管时间**：周次、单双周、调休、展开、提醒时刻、状态流转全部由 `src/` 的
  纯标准库代码计算，有单元测试覆盖，绝不让模型算时间。
- **LLM（DeepSeek）只做两件事**：解析（把课表图片/通知原文转成文字交给确定性解析器）
  和文案（提醒措辞，且有模板兜底，模型不可用时照样发）。
- **不猜测**：课程、日期、教室锚不准时标“待确认”并拒绝入库，宁可问一句，不编数据。
- **写入先预览**：所有改数据的操作默认只预览，用户确认后加 `--apply` / `--send` 才落盘，
  落盘前自动备份。

## 架构

```
用户输入（文字 / 图片 / 转发的班群通知）
        ↓  Skills（timetable-import / notice-parser / reminder-composer）
        ↓  LLM 只负责转文字与措辞，锚定与解析在确定性代码里完成
data/*.json ──→ src/ 确定性核心 ──→ Occurrence（未来 14 天）──→ ReminderTask
 term/courses     week_calc 周次/调休      expand 展开+应用例外      scheduler 生成/合并状态/分发
 exceptions/user  timetable_import 导入    notice_parser 通知解析    sender 可插拔发送通道
        ↑
HEARTBEAT 每 5 分钟 → scripts/run_scheduler.py --tick [--send] → 到期任务经 sender 发出并回写状态
```

## 功能与完成状态

| 功能 | 状态 | 说明 |
|---|---|---|
| 课表导入 | ✅ 已实现 | .txt/.csv/.xlsx/.json 确定性解析；周次（单/双/列表/范围）、节次转时间、地点拆分；识别不了标待确认；预览→`--apply`（备份+去重）。图片先经智能体视觉转文字 |
| 调课/停课通知解析 | ✅ 已实现 | 停课→cancel、调课→reschedule、换教室→location_change、改线上→online；只影响单次课，不动周期模板；锚不准拒绝写入 |
| 周次与展开 | ✅ 已实现 | 教学周、单双周、节假日、调休（makeupDays）；滚动展开未来 14 天 Occurrence 并应用例外 |
| 提醒调度 | ✅ 已实现 | 三级提醒时刻全部确定性计算；每 tick 重新生成任务并与已存运行状态合并，已发送的永不被重置 |
| 发送层 | ✅ 已实现 | 可插拔通道 console / outbox / webhook / gateway（见下）；成功回写 sent+sentAt+channel，失败回写 failed+error；已 sent 不重发，failed 不自动重试 |
| 天气提前量 | ⬜ 待做 | `user.json` 已预留 `rainExtraMin`，尚未接天气 API 给出门提醒加提前量 |
| 回执确认 | ⬜ 待做 | 用户回复“收到/请假了/snooze”后回写 acknowledged / snoozed（数据流已在 AGENTS.md 定义，未实现） |
| OpenClaw 网关联调 | ⬜ 待联调 | gateway 通道是命令适配接口（见下），需在真实 OpenClaw 环境配置命令后联调，本仓库无法自证 |

## 快速开始

前置：Python 3（仅标准库，无需 pip 安装任何东西）。接 OpenClaw 时另需安装 OpenClaw 本体。

1. （接 OpenClaw 时）把 `config/openclaw.json` 复制到 `~/.openclaw/openclaw.json`，
   把本目录作为 agent 的 workspace。
2. 设置环境变量（**密钥只走环境变量，绝不写进任何文件**，清单见 `.env.example`）：
   ```bash
   export DEEPSEEK_API_KEY=你的key      # 只在需要 LLM 解析/文案时用到；确定性脚本不依赖它
   export REMINDER_CHANNEL=outbox       # 发送通道：console|outbox|webhook|gateway（默认 console）
   ```
3. 改 `data/term.json`（开学第一周周一、总周数、假期、调休、节次时间）和
   `data/user.json`（提醒偏好：前晚时间、课前提前量、默认路程/缓冲分钟）。
4. 导入你的课表（见下），本地验证：
   ```bash
   python3 -m unittest discover -s tests -v                                  # 全部测试
   python3 scripts/run_scheduler.py --tick --now 2026-09-13T21:00:00+08:00   # 预览到期提醒，不发送
   ```

## 使用指南

### 导入课表
```bash
python3 scripts/import_timetable.py examples/sample_timetable.txt            # 先预览（不写入）
python3 scripts/import_timetable.py examples/sample_timetable.txt --apply    # 确认后写入（自动备份+去重）
```
支持 .txt（一行一课）/ .csv / .xlsx / .json。图片课表先由智能体视觉读成文字再走 txt 导入，
规则见 `skills/timetable-import/SKILL.md`。

### 解析调课/停课通知
```bash
python3 scripts/parse_notice.py "大学英语 2026年9月16日（周三）的课改到周五线上上课"   # 先预览（不写入）
python3 scripts/parse_notice.py "高等数学 2026年9月21日停课一次" --apply              # 确认后写入（自动备份+去重）
python3 scripts/parse_notice.py "本周三大学英语停课一次" --date 2026-09-14             # 相对日期需给通知日期才能锚定
```
解析出一条 CourseException，只影响单次课；课程或日期锚不准时标“待确认”并拒绝写入。
规则见 `skills/notice-parser/SKILL.md`。

### 跑调度器
```bash
python3 scripts/run_scheduler.py --expand --from 2026-09-07 --days 14   # 只展开课表+生成任务（写 occurrences/reminders）
python3 scripts/run_scheduler.py --tick                                 # tick 预览：打印到期提醒，不发送
python3 scripts/run_scheduler.py --tick --send                          # 真发送：到期任务经通道发出并回写状态
python3 scripts/run_scheduler.py --tick --send --dry-run                # 强制预览：不发送、不写任何文件（--dry-run 优先于 --send）
python3 scripts/run_scheduler.py --tick --send --channel webhook        # 临时指定通道（覆盖 REMINDER_CHANNEL）
python3 scripts/run_scheduler.py --tick --send --data-dir /tmp/xx       # 用另一份 data 目录（测试/试运行）
python3 scripts/run_scheduler.py --tick --now 2026-09-13T21:00:00+08:00  # 指定“现在”（复现/调试）
```
退出码：0 正常 · 1 至少一条发送失败 · 2 通道配置错误（此时不发送、不改任何任务状态）。
部署到 HEARTBEAT 时把命令换成 `--tick --send` 才会真正发出提醒；不加 `--send` 永远只是预览。

## 发送通道

通道由 `--channel` 或环境变量 `REMINDER_CHANNEL` 选择（默认 console）。敏感配置只读环境变量。

| 通道 | 配置 | 行为 |
|---|---|---|
| `console` | 无 | 把提醒打印到标准输出（原 MVP 行为），默认/回退通道 |
| `outbox` | `REMINDER_OUTBOX_DIR`（默认 `<data-dir>/outbox`） | 每条提醒追加一行 JSON 到 `outbox.jsonl`（含 taskId、课程、文案、发送时间），便于本地验证与 OpenClaw 侧消费 |
| `webhook` | `REMINDER_WEBHOOK_URL`（必填）、`REMINDER_WEBHOOK_TIMEOUT`（秒，默认 10） | POST JSON 到该 URL，载荷含 taskId/type/scheduledAt/message；非 2xx、超时、连不上都算发送失败并记录原因。**URL 只放环境变量，不要写进仓库任何文件** |
| `gateway` | `REMINDER_GATEWAY_CMD`（必填） | 命令模板适配：占位符 `{text}` `{task_id}` `{type}` `{user_id}` 替换后经 shlex 拆分、无 shell 执行，退出码非 0 算失败。本项目的 `config/openclaw.json` 里 `channels` 为空、没有可依据的消息端点约定，所以具体命令要按部署环境的 OpenClaw 版本的通道命令来配，**需要真实 OpenClaw 环境联调**，本仓库不编造其协议 |

**状态与失败策略**（`scheduler.dispatch_due` + `merge_task_states`，均有测试）：
- 发送成功 → 任务回写 `status=sent`、`sentAt`、`channel`；之后每 tick 重新生成任务都会把
  已存状态合并回来，**已 sent 的任务永不重发**。
- 发送失败 → 回写 `status=failed`、`error`（失败原因）、`retryCount+1`、`lastAttemptAt`。
  **failed 不自动重试**（避免通道故障时每 5 分钟重复打扰）；处理完把 `reminders.json` 里
  对应任务的 `status` 改回 `pending`，下一 tick 就会重新尝试。
- 通道未配置/配错（如选了 webhook 却没给 URL）→ 启动时明确报错并以退出码 2 中止，
  不静默吞、不动任何任务状态。
- 到期任务找不到对应 Occurrence（数据不一致）→ 标 failed 并写明原因，不静默丢弃。

## 数据模型

- **Term**（`data/term.json`）：学期锚点——`firstWeekMonday`、总周数、假期、调休日
  （`makeupDays`：某天按周几的课表上课）、节次时间表。
- **Course**（`data/courses.json`）：周期模板——周几、第几节与起止时间、周次模式
  （all/range/odd/even/list）、地点、教师、携带物品、可选的单课提醒覆盖。
- **CourseException**（`data/exceptions.json`）：单次变更——锚定 `courseId + originalDate`，
  类型 cancel / reschedule / location_change / online，可带新日期/新地点；
  `confirmed=false` 的预览永不入库。
- **Occurrence**（`data/occurrences.json`，生成物）：模板在滚动 14 天内展开的具体一次课，
  已应用假期、调休与例外。
- **ReminderTask**（`data/reminders.json`，生成物+运行状态）：一条提醒——类型
  night_before / departure / pre_class（follow_up 预留）、`scheduledAt`、状态机
  `pending → sent / failed`（acknowledged / snoozed 待回执功能实现后启用），
  以及 sentAt / channel / error / retryCount 等运行字段。
- **User**（`data/user.json`）：时区与提醒偏好（前晚时间、课前提前量、路程/缓冲、预留的下雨提前量、安静时段）。

## 目录结构

```
SOUL.md / AGENTS.md / HEARTBEAT.md / USER.md / TOOLS.md   OpenClaw 智能体定义（HEARTBEAT 每 5 分钟触发 tick）
config/openclaw.json     OpenClaw 基础配置（DeepSeek 模型、网关与工具权限收紧；channels 待部署时配置）
data/                    term / courses / exceptions / user（输入）+ occurrences / reminders（生成）+ outbox/（发送日志，运行时产生）
skills/                  timetable-import / notice-parser / reminder-composer（给智能体的操作规程）
src/                     week_calc / expand / scheduler / sender / timetable_import / notice_parser（纯标准库）
scripts/                 run_scheduler（调度入口）/ import_timetable / parse_notice（默认预览，--apply/--send 才生效）
tests/                   核心逻辑 + 导入 + 通知解析 + 发送层单元测试（54 个）
examples/                示例课表（.txt / .csv）
.env.example             环境变量清单（只有变量名与说明，无真实值）
```

## 测试

```bash
python3 -m unittest discover -s tests        # 54 个，全部本地运行
```
发送层测试全部走注入的假 transport / 假 runner 与临时目录，不打真实外网、不动真实 `data/`；
CLI 端到端测试用临时数据副本验证发送回写、不重发与 dry-run 不落盘。

## 安全约定

- 密钥（DeepSeek API key、webhook URL、网关命令里可能含的令牌）**只走环境变量**，
  不写进代码、配置、数据文件或仓库；`.env` 已被 `.gitignore` 忽略。
- 所有改数据的操作默认预览：导入/通知解析要 `--apply`，发送要 `--send`，
  `--dry-run` 永远不写文件；正式写入前自动生成带时间戳的备份。
- 锚不准不猜：解析器把不确定的字段标“待确认”留空，而不是编造课程、日期或教学楼。

## 下一步（TODO）

- [ ] 接入天气 API，出门提醒按天气加提前量（`rainExtraMin` 已预留）
- [ ] 提醒状态机：收到回复后 acknowledged / snoozed 的回写
- [ ] 在真实 OpenClaw 环境联调 gateway 通道，并把 HEARTBEAT 命令切到 `--tick --send`
- [x] run_scheduler 的发送从 print 改为真实渠道发送（console/outbox/webhook/gateway 可插拔）
- [x] timetable-import：文字/CSV/XLSX/JSON 导入（图片经智能体转文字后导入）
- [x] notice-parser：确定性解析调课/停课通知，预览确认后写入 exceptions.json（DeepSeek 只负责送原文/复述，不参与锚定）
