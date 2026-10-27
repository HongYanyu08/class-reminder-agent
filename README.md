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
        ↓  Skills（timetable-import / notice-parser / reminder-composer / course-query / reminder-config）
        ↓  LLM 只负责转文字与措辞，锚定与解析在确定性代码里完成
data/*.json ──→ src/ 确定性核心 ──→ Occurrence（未来 14 天）──→ ReminderTask
 term/courses     week_calc 周次/调休      expand 展开+应用例外      scheduler 生成/合并状态/分发
 exceptions/user  timetable_import 导入    notice_parser 通知解析    sender 发送 / ack 回执 / query 查课表 / config_edit 改配置 / webui 配置界面
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
| 发送层 | ✅ 已实现 | 可插拔通道 console / outbox / webhook / gateway（见下）；渠道与目标可在 Web UI 的「发送渠道」页配置（存 `data/sender_settings.json`，已 gitignore；环境变量仍优先，见「发送通道」）；成功回写 sent+sentAt+channel，失败回写 failed+error；已 sent 不重发，failed 不自动重试 |
| 天气提前量 | ✅ 已实现 | `src/weather.py` 接 Open-Meteo（免费、无需 key）：坐标配了才启用，降雨给 departure 加 `rainExtraMin`（默认 10 分钟）、恶劣天气加 `severeExtraMin`（默认 20 分钟）；预报缓存 3 小时，失败回退过期缓存或不加量，绝不阻塞 tick（见“天气提前量”） |
| 回执确认 | ✅ 已实现 | `src/ack.py` + `scripts/handle_reply.py`：回复分确认/请假/稍后三类确定性回写（见“处理回执”）；已 sent 超过宽限期（默认 10 分钟）仍未确认的课自动生成一条 follow_up 追问，一次课只追问一次 |
| 对话查课表 | ✅ 已实现 | `src/query.py` + `scripts/ask.py`：确定性问答——按日期查、按课程查（时间/地点/携带/老师）、下节课、本周概览；答案全部经 `expand()` 计算，例外如实呈现（停课注明、调课按新时间与新地点）；日期锚不准/课程多义返回澄清，绝不猜；只读，不写任何数据（见“对话查课表”） |
| 对话改提醒配置 | ✅ 已实现 | `src/config_edit.py` + `scripts/configure.py`：一句话改全局偏好（前晚时间/课前提前量/路程/缓冲/天气加量/追问宽限/静默时段）、开关四类提醒、改单课覆盖（出门提前量、单课开关）；变更计划先预览，用户确认 `--apply` 才写入并备份；数值含糊/越界/课程多义一律待确认不猜（见“对话改提醒配置”） |
| Web 配置 UI | ✅ 已实现 | `src/webui.py` + `scripts/serve_ui.py` + `ui/index.html`：本地网页含总览、提醒设置、单课覆盖、未来 14 天课程、发送渠道五个区块；服务端逐项校验、非法整单拒绝、写前备份；发送渠道页可配通道、保存设置并真发一条测试消息，webhook URL 只回打码版；无服务打开同一 HTML 时自动进入演示模式（示例数据，不会保存）（见“Web 配置 UI”） |
| OpenClaw 网关联调 | ⬜ 待联调 | gateway 通道是命令适配接口（见下），需在真实 OpenClaw 环境配置命令后联调，本仓库无法自证 |

## 快速开始

前置：Python 3（仅标准库，无需 pip 安装任何东西）。接 OpenClaw 时另需安装 OpenClaw 本体。

1. （接 OpenClaw 时）把 `config/openclaw.json` 复制到 `~/.openclaw/openclaw.json`，
   把本目录作为 agent 的 workspace。
2. 设置环境变量（**密钥只走环境变量，或 Web UI 维护的已 gitignore 的
   `data/sender_settings.json`，绝不写进仓库任何文件**，清单见 `.env.example`）：
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

### 处理回执（收到 / 请假 / 稍后提醒）
```bash
python3 scripts/handle_reply.py "收到"                                  # 先预览：将回写哪条任务、改成什么
python3 scripts/handle_reply.py "高等数学请假了，今天不去上课" --apply      # 确认后回写（自动备份）
python3 scripts/handle_reply.py "过10分钟再提醒我" --apply                # snooze，可带时长；没写时长默认 10 分钟（--snooze-min 可调）
```
回执规则（`src/ack.py`，与 AGENTS.md 数据流 3 一致）：
- **定位**：回复点名课程 → 定位该课；没点名 → 定位最近一条已发送且未确认的提醒所属的那次课；
  多条候选无法唯一确定（如发送时间相同）→ 标“待确认”列出候选，**拒绝回写**。
- **确认类**（收到/确认/知道了/OK…）→ 该次课所有已发送提醒回写 `status=acknowledged`、
  `acknowledgedAt`，并留存回复原文（`reply` 字段）。
- **请假类**（请假了/今天不去上课…）→ 已发送提醒回写 acknowledged 并标 `note=leave`；
  该次课还没发的提醒全部 `status=cancelled`（这节课不去了，后续提醒不再打扰），理由留痕。
- **稍后类**（晚点提醒/snooze/过X分钟再提醒…）→ 回写 `status=snoozed` + `snoozeUntil`；
  到期后的下一 tick 自动恢复为 pending 并重发（`resumedAt` 留痕）。
- **追问 follow_up**：departure / pre_class 已发送、课程开始 + 宽限期（`user.json` 的
  `followUpGraceMin`，默认 10 分钟）已过、仍未确认也未 snooze → 生成一条 follow_up 任务
  （排在“开始+宽限期”时刻，一次课最多一条，确认/请假后不再生成；课堂结束之后不再补发追问）。

### 对话查课表
```bash
python3 scripts/ask.py "明天有什么课"                                   # 直接输出可读答案
python3 scripts/ask.py "高等数学什么时候上" --date 2026-09-14            # --date 指定参照日（默认今天）
python3 scripts/ask.py "下节课是什么" --date 2026-09-07T07:30:00 --json  # 结构化输出，给智能体消费
```
查询引擎（`src/query.py`，口径见 `skills/course-query/SKILL.md`）：
- **支持的问法**：按日期（今天/明天/本周三/下周一/9月16日/第3周周五有什么课）、按课程
  （什么时候上 / 在哪上 / 要带什么 / 老师是谁）、下节课、本周概览、以及“明天高等数学有课吗”
  这类日期+课程的组合问法。
- **答案只来自数据**：全部经 `expand()` 展开计算（含单双周、假期、调休），智能体只转发
  问题与答案，不许自己编课。例外如实呈现：停课的那次会注明“已停课”而不是消失，
  调课按新日期/新地点出现且原日期注明已调走，换教室/线上按新地点显示。
- **不猜**：只说“周三”没说哪一周、简称同时匹配多门课 → 返回澄清（列出候选/示例问法），
  没听懂的问句会明确说没听懂并给出支持的问法。查询是只读的，永远不写 data/。

### 对话改提醒配置
```bash
python3 scripts/configure.py "课前提前20分钟提醒"              # 先预览变更计划（不写入）
python3 scripts/configure.py "课前提前20分钟提醒" --apply      # 用户确认后写入（自动备份，只改涉及的文件）
python3 scripts/configure.py "关掉前晚提醒" --apply            # 全局关掉一类提醒
python3 scripts/configure.py "高数出门提前50分钟" --apply      # 单课覆盖：该课的出门提前量
python3 scripts/configure.py "关掉高数的前晚提醒" --apply      # 单课覆盖：只关该课的前晚提醒
python3 scripts/configure.py "静默时间改成23:30到7:30" --apply
python3 scripts/configure.py "我现在的提醒设置是怎样的"         # 查看当前生效设置（只读）
```
配置引擎（`src/config_edit.py`，口径见 `skills/reminder-config/SKILL.md`）：
- **能改的**：全局偏好——前晚提醒时间、课前提前量、默认路程、缓冲、雨天/恶劣天气加量、
  追问宽限、静默时段起止；四类提醒（night_before / departure / pre_class / follow_up）
  的全局开关；单课覆盖——某门课的出门提前量与该课的类型开关。开关的数据模型是
  `preferences.disabledTypes` 与 `reminderOverride.disabledTypes` 两个列表（空 = 全开），
  scheduler 生成任务时尊重它们：**被关的类型不生成任务**（follow_up 追问同样受开关控制）。
- **生效方式**：改完下一 tick 起，新生成/待发的 pending 任务按新设置计算时刻；
  已 sent 的任务状态经 `merge_task_states` 合并保留，不会被改配置重置、更不会重发
  （注：合并口径下已发送任务记录里的 `scheduledAt` 会随重新生成刷新为新时刻，
  但状态仍是 sent，不影响发送行为）。
- **不猜**：分钟数只认 0–180、时间只认合法 HH:MM；“提前一点”没给数、点名课程却要改
  只有全局版的字段、简称匹配多门课 → 一律标“待确认”说明缺什么，拒绝写入（退出码 1）；
  “多提前5分钟”这类增减说法按现值 ± 计算，预览里会展示旧值 → 新值让用户核对。
- 全局没有单一的“出门提前量”字段（出门时刻 = 路程 + 缓冲，或单课覆盖），
  没点名课程就说改出门时间会被要求说清改哪一项。

### Web 配置 UI
```bash
python3 scripts/serve_ui.py                         # 默认 http://127.0.0.1:8765/
python3 scripts/serve_ui.py --port 8766             # 换端口
python3 scripts/serve_ui.py --data-dir /tmp/my-data # 使用另一份数据目录
```
启动后在本机浏览器打开页面，五个区块：
- **总览**：学期与当前教学周、课程数、未来 7 / 14 天课程数、提醒任务各状态计数。
- **提醒设置**：改前晚时间、课前/路程/缓冲/天气加量/追问宽限、静默时段，并开关四类提醒。
  保存走 `POST /api/config`，服务端逐项校验（分钟 0–180、时间 HH:MM、类型名合法），
  **任一项非法整单拒绝并显示具体字段原因，不部分写入**；成功时先给 `user.json` 做时间戳备份。
- **单课覆盖**：每门课单独保存出门提前量（留空 = 清除覆盖、回到全局）和四类提醒开关，
  走 `POST /api/course-override`，写 `courses.json` 前同样备份；课程不存在会明确报错。
- **未来课程**：未来 14 天按日展开（含单双周、假期、调休），停课/调课等例外有明确标注。
- **发送渠道**：选通道（console / outbox / webhook / gateway），选中才显示对应输入框；
  保存走 `POST /api/sender` 写入 `data/sender_settings.json`（写前备份），「发送测试消息」
  走 `POST /api/sender/test` 用页面草稿（不落盘）真发一条固定测试文案，成败当场显示原因。
  `GET /api/sender` 只回**打码版**的 webhook URL（协议 + 域名 + 末 4 位）与网关命令，
  且保存时提交打码版/空值表示沿用已存的真值，不会把真地址冲掉。页面顶部写明来源
  （环境变量 / 设置文件 / 默认），环境变量覆盖的项会明确提示改本页不生效。

**演示模式**：直接双击打开 `ui/index.html`、或把它当静态页托管（拿不到 `/api/state`）时，
页面自动用内置的 3 门示例课数据渲染，顶部显示“演示模式（示例数据，未连接服务）”横幅；
演示模式的保存只改本页内存并明确提示不会写入文件。由 `serve_ui.py` 提供服务时则走真实 API。

> ⚠️ **安全**：这个 UI **仅本机使用、无鉴权**，默认只绑 `127.0.0.1`。
> 不要改绑 `0.0.0.0`，更不要把端口暴露到局域网或公网。

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

### 天气提前量（出门提醒按天气提前）
```bash
export WEATHER_LAT=你的纬度    # 两个坐标都配齐才启用，只从环境变量读，绝不写进数据文件
export WEATHER_LON=你的经度
```
规则（`src/weather.py`，Open-Meteo 逐小时预报，免费、无需 API key）：
- **判定**：按 WMO 天气代码分级——重降水/雷暴为 `severe`，其他降水为 `rain`；
  无降水代码时，降水概率 ≥ 50% 也判 `rain`。判定时点为基础出门时刻所在的那一小时。
- **加量**：`rain` → departure 在基础提前量（单课覆盖或路程+缓冲）之上再加
  `user.json` 的 `rainExtraMin`（默认 10 分钟）；`severe` → 加 `severeExtraMin`（默认 20 分钟）；
  晴/无数据 → 不加。只影响 departure 一类提醒，night_before / pre_class 时刻不变。
- **幂等**：每 tick 都从课程开始时间重新计算，同一预报下连跑多次 departure 时刻完全一致，
  不会越叠越多；被加量的任务带 `weatherExtraMin` / `weatherNote` 留痕，提醒文案会带上
  「天气：降雨，已提前 10 分钟」一句（没加量时不加这句）。
- **缓存与失败回退**：预报缓存到 `data/weather_cache.json`（已 gitignore），默认 TTL 3 小时
  （`WEATHER_CACHE_TTL_H` 可调），tick 每 5 分钟跑也不会每次请求；请求失败时用过期缓存
  （状态标 `stale`），连缓存也没有就按无天气处理（`unavailable`）——天气失败**绝不**让 tick 崩。
  没配坐标时功能关闭（`disabled`），一切照常。预报约覆盖未来 16 天，展开窗口 14 天在覆盖内；
  覆盖不到的小时按无数据处理。

## 发送通道

通道配置的解析顺序（`src/sender_settings.py`，调度器与 Web UI 共用同一套）：

```
--channel 参数  >  REMINDER_* 环境变量  >  data/sender_settings.json（Web UI 维护）  >  默认值（console / 超时 10 秒）
```

`data/sender_settings.json` 已被 `.gitignore` 忽略（模板见 `data/sender_settings.example.json`），
因为里面的 webhook URL 等同于密码。设置文件损坏或字段非法时，发送在启动阶段明确报错
（退出码 2），**绝不静默回退到别的通道乱发**。

| 通道 | 配置 | 行为 |
|---|---|---|
| `console` | 无 | 把提醒打印到标准输出（原 MVP 行为），默认通道 |
| `outbox` | `outboxDir` / `REMINDER_OUTBOX_DIR`（默认 `<data-dir>/outbox`） | 每条提醒追加一行 JSON 到 `outbox.jsonl`（含 taskId、课程、文案、发送时间），便于本地验证与 OpenClaw 侧消费 |
| `webhook` | `webhookUrl`（必填）/ `REMINDER_WEBHOOK_URL`；`webhookTimeoutSec` / `REMINDER_WEBHOOK_TIMEOUT`（秒，默认 10） | POST JSON 到该 https URL，载荷含 taskId/type/scheduledAt/message；非 2xx、超时、连不上都算发送失败并记录原因。**URL 只放环境变量或 gitignore 的设置文件，不要写进仓库任何文件** |
| `gateway` | `gatewayCmd` / `REMINDER_GATEWAY_CMD`（必填，必须含 `{text}`） | 命令模板适配：占位符 `{text}` `{task_id}` `{type}` `{user_id}` 替换后经 shlex 拆分、无 shell 执行，退出码非 0 算失败。本项目的 `config/openclaw.json` 里 `channels` 为空、没有可依据的消息端点约定，所以具体命令要按部署环境的 OpenClaw 版本的通道命令来配，**需要真实 OpenClaw 环境联调**，本仓库不编造其协议 |

### 接入微信（诚实版）

**个人微信没有官方机器人接口，本项目不能、也不会假装能直连个人微信。** 能真正把提醒
推到微信的现成路径只有下面三条，都在本页/上表能配：

1. **企业微信群机器人**：在企业微信群里「群设置 → 群机器人 → 添加机器人」，拿到
   `https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...` 地址，在 Web UI 的
   「发送渠道」页选 webhook 通道填入即可。提醒会发到那个群里。
2. **Server酱 / PushPlus / WxPusher 等中转服务**：这类服务把 webhook 推送转成微信
   服务号/模板消息推到你个人微信。去对应服务拿到你的 webhook（推送）地址，同样填到
   webhook 通道即可。各家的载荷格式以其文档为准（本项目发的是通用 JSON，
   含 `message` / `text` 字段）。
3. **你自己的 OpenClaw 网关**：OpenClaw 那边接好渠道后，把它的发送命令按
   `{text}` 等占位符填成 gateway 命令模板，走 gateway 通道发出。

三条路都建议先在「发送渠道」页点一次「发送测试消息」确认能收到，再把 HEARTBEAT
切到 `--tick --send`。

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
  （all/range/odd/even/list）、地点、教师、携带物品、可选的单课提醒覆盖
  （`reminderOverride`：出门提前量 `departureLeadMin`、该课关闭的提醒类型 `disabledTypes`）。
- **CourseException**（`data/exceptions.json`）：单次变更——锚定 `courseId + originalDate`，
  类型 cancel / reschedule / location_change / online，可带新日期/新地点；
  `confirmed=false` 的预览永不入库。
- **Occurrence**（`data/occurrences.json`，生成物）：模板在滚动 14 天内展开的具体一次课，
  已应用假期、调休与例外。
- **ReminderTask**（`data/reminders.json`，生成物+运行状态）：一条提醒——类型
  night_before / departure / pre_class / follow_up（未确认追问，运行时生成）、`scheduledAt`、状态机
  `pending → sent / failed`，回执后 `sent → acknowledged / snoozed`（请假时同次课的 pending
  任务 → `cancelled`），snoozed 到期自动回到 pending；以及 sentAt / channel / error /
  retryCount / acknowledgedAt / snoozeUntil / reply / note 等运行字段。
- **User**（`data/user.json`）：时区与提醒偏好（前晚时间、课前提前量、路程/缓冲、下雨/恶劣天气提前量 `rainExtraMin` / `severeExtraMin`、回执宽限期、安静时段、全局关闭的提醒类型 `disabledTypes`）。
  另有运行时缓存 `data/weather_cache.json`（Open-Meteo 预报，已 gitignore，不手改）。

## 目录结构

```
SOUL.md / AGENTS.md / HEARTBEAT.md / USER.md / TOOLS.md   OpenClaw 智能体定义（HEARTBEAT 每 5 分钟触发 tick）
config/openclaw.json     OpenClaw 基础配置（DeepSeek 模型、网关与工具权限收紧；channels 待部署时配置）
data/                    term / courses / exceptions / user（输入）+ occurrences / reminders（生成）+ outbox/（发送日志，运行时产生）+ sender_settings.json（渠道设置，UI 维护，已 gitignore；模板 sender_settings.example.json）
skills/                  timetable-import / notice-parser / reminder-composer / course-query / reminder-config（给智能体的操作规程）
src/                     week_calc / expand / scheduler / sender / sender_settings（渠道设置解析）/ timetable_import / notice_parser / ack / weather / query / config_edit / webui（纯标准库）
scripts/                 run_scheduler（调度入口）/ import_timetable / parse_notice / handle_reply / configure（默认预览，--apply/--send 才生效）/ ask（对话查课表，只读）/ serve_ui（本地 Web 配置 UI）
ui/                      index.html（配置中心单文件前端，含演示模式，无外部资源）
tests/                   核心逻辑 + 导入 + 通知解析 + 发送层 + 渠道设置 + 回执 + 天气 + 对话查询 + 改配置 + Web UI 单元测试（216 个）
examples/                示例课表（.txt / .csv）
.env.example             环境变量清单（只有变量名与说明，无真实值）
```

## 测试

```bash
python3 -m unittest discover -s tests        # 216 个，全部本地运行
```
发送层测试全部走注入的假 transport / 假 runner 与临时目录，不打真实外网、不动真实 `data/`；
渠道设置与 UI 测试发送走本机 localhost 假 webhook；CLI 端到端测试用临时数据副本
验证发送回写、不重发、dry-run 不落盘，以及设置文件驱动发送与损坏设置报错退出。

## 安全约定

- 密钥（DeepSeek API key、webhook URL、网关命令里可能含的令牌）**只走环境变量，
  或 Web UI 维护的 `data/sender_settings.json`**——该文件已 gitignore、只存本机，
  绝不进仓库；**webhook URL 等同于密钥，别外传、别截图发群、别贴到聊天里**。
  UI 与 API 返回的 URL/命令一律打码，日志与报错也不会出现完整值。`.env` 同样已被
  `.gitignore` 忽略。
- 所有改数据的操作默认预览：导入/通知解析要 `--apply`，发送要 `--send`，
  `--dry-run` 永远不写文件；正式写入前自动生成带时间戳的备份。
- 锚不准不猜：解析器把不确定的字段标“待确认”留空，而不是编造课程、日期或教学楼。
- Web 配置 UI 只绑本机回环地址且无鉴权：仅限本机使用，不要暴露到局域网或公网。

## 下一步（TODO）

- [x] 接入天气 API（Open-Meteo），出门提醒按天气加提前量（rain 加 `rainExtraMin`、severe 加 `severeExtraMin`，缓存+失败回退，src/weather.py）
- [x] 提醒状态机：收到回复后 acknowledged / snoozed 的回写（src/ack.py + handle_reply.py，含 follow_up 追问）
- [ ] 在真实 OpenClaw 环境联调 gateway 通道，并把 HEARTBEAT 命令切到 `--tick --send`
- [x] run_scheduler 的发送从 print 改为真实渠道发送（console/outbox/webhook/gateway 可插拔）
- [x] timetable-import：文字/CSV/XLSX/JSON 导入（图片经智能体转文字后导入）
- [x] notice-parser：确定性解析调课/停课通知，预览确认后写入 exceptions.json（DeepSeek 只负责送原文/复述，不参与锚定）
- [x] 对话改提醒配置：确定性解析改配置的话（src/config_edit.py + configure.py），预览确认后写入 user.json/courses.json，scheduler 尊重全局/单课类型开关
