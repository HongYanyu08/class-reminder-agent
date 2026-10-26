# 上课提醒智能体（OpenClaw + DeepSeek）

基于 OpenClaw 的上课提醒智能体：解析课表、处理单双周/调休/调课、按三级策略（前晚预告 → 出门提醒 → 课前提醒）发送提醒。
设计原则：**时间与周次计算全部由确定性代码完成，LLM（DeepSeek）只做解析与文案。**

## 目录
```
SOUL.md / AGENTS.md / HEARTBEAT.md / USER.md / TOOLS.md   OpenClaw 智能体定义
config/openclaw.json     OpenClaw 基础配置（DeepSeek 模型、网关与工具权限收紧）
data/                    term / courses / exceptions / user（输入）+ occurrences / reminders（生成）
skills/                  timetable-import / notice-parser / reminder-composer
src/                     week_calc.py / expand.py / scheduler.py（纯标准库）
scripts/run_scheduler.py HEARTBEAT 每 5 分钟调用的调度入口
tests/test_core.py       核心逻辑单元测试
```

## 快速开始
1. 安装 OpenClaw，把 `config/openclaw.json` 复制到 `~/.openclaw/openclaw.json`，
   把本目录作为 agent 的 workspace。
2. 设置 DeepSeek 密钥（**只用环境变量，不要写进任何文件**）：
   ```bash
   export DEEPSEEK_API_KEY=你的key   # 或按 .env.example 建本地 .env（已被 .gitignore 忽略）
   openclaw models status            # 应能看到 deepseek 模型可用
   ```
3. 改 `data/term.json`（开学第一周周一、假期、调休）和 `data/courses.json`（你的课表）。
4. 本地验证（不依赖 OpenClaw 和网络）：
   ```bash
   python3 -m unittest discover -s tests -v
   python3 scripts/run_scheduler.py --tick --now 2026-09-13T21:00:00+08:00
   ```
5. 启动 OpenClaw Gateway 后，HEARTBEAT.md 会每 5 分钟触发一次调度 tick。

## 导入课表
```bash
python3 scripts/import_timetable.py examples/sample_timetable.txt   # 先预览（不写入）
python3 scripts/import_timetable.py examples/sample_timetable.txt --apply   # 确认后写入（自动备份+去重）
```
支持 .txt（一行一课）/ .csv / .xlsx / .json；周次（单/双/列表）、节次转时间、地点拆分全是确定性解析，
识别不了的字段会标“待确认”而不是猜。图片课表先由智能体视觉读成文字再走 txt 导入，见 `skills/timetable-import/SKILL.md`。

## 下一步（TODO）
- [ ] run_scheduler 的发送从 print 改为真实渠道发送（Telegram/WhatsApp 等）
- [ ] 接入天气 API，出门提醒按天气加提前量
- [x] timetable-import：文字/CSV/XLSX/JSON 导入（图片经智能体转文字后导入）
- [ ] notice-parser 接 DeepSeek 解析后人工确认入库
- [ ] 提醒状态机：收到回复后 acknowledged / snoozed 的回写
