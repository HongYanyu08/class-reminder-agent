# ⏰ 上课提醒智能体

把课表导入一次，它就算好每节课的日期，在前一晚、出门前、上课前通过微信「ClawBot」提醒你。支持单双周、节假日、调休、临时停课/调课/换教室，雨天自动提前出门提醒。

> SRTP 课题《基于 OpenClaw 的上课提示智能体开发》。仓库里是**示例数据**，部署时换成自己的课表。

---

## 部署全流程

完整版（含故障排查）见 [DEPLOY.md](DEPLOY.md)，下面是实测走通的顺序。前置：Node.js ≥ 22.13、OpenClaw ≥ 2026.3.22、Python ≥ 3.10、iOS 微信 ≥ 8.0.70、一个 DeepSeek API key；部署机器需保持开机不合盖。Windows 下命令都用 `.cmd` 后缀（`openclaw.cmd`），关掉代理/VPN。

**第 1 步：安装 OpenClaw**
```powershell
npm install -g openclaw
openclaw.cmd onboard            # manual 配置 → DeepSeek → 填自己的 API key
openclaw.cmd gateway install    # 网关注册为登录自启
openclaw.cmd gateway start
```
API key 只在向导/终端里输，或放环境变量 `DEEPSEEK_API_KEY`，**永远不要写进项目文件**。

**第 2 步：装微信插件并扫码登录**
```powershell
npx.cmd -y @tencent-weixin/openclaw-weixin-cli install
openclaw.cmd config set plugins.entries.openclaw-weixin.enabled true
openclaw.cmd channels login --channel openclaw-weixin   # 微信扫码
openclaw.cmd gateway restart
```
微信里出现「微信 ClawBot」联系人、发消息能回复即通。

**第 3 步：把项目放进 workspace**
```
C:\Users\<你>\.openclaw\workspace\class-reminder-agent
```
（解压别多套一层同名文件夹。）

**第 4 步：导入自己的课表**（仓库数据只是示例，必须换）
```bash
python3 scripts/import_timetable.py 我的课表.txt          # 先预览，逐条核对
python3 scripts/import_timetable.py 我的课表.txt --apply  # 确认后写入（自动备份）
```
也可用可视化方式：`python3 scripts/serve_ui.py` 打开本地页面，在「导入课表」页上传或粘贴。识别不准的字段会标「待确认」，导入器**不会猜**。同时核对 `data/term.json` 的开学周一、总周数、节假日、节次时间。

**第 5 步：一键配置**
```bash
python3 scripts/deploy_setup.py --dry-run   # 先预览
python3 scripts/deploy_setup.py
```
检查环境、写好微信主会话投递渠道，并自动应用省钱设置（默认模型切到便宜的 `deepseek-v4-flash`、会话每天凌晨 4 点自动重置）。

**第 6 步：装任务计划（正式自动化）**
```powershell
scripts\install_tick_task.cmd
schtasks /Query /TN "ClassReminderTick"
```
之后每 5 分钟由 Windows 任务计划跑一次确定性检查：有提醒才唤醒智能体念出来，没提醒零打扰。不要用旧的 `cr-tick-5min` 网关定时任务（若一键脚本建过，按 DEPLOY.md「根治版自动化」一节停用并删除）。

**第 7 步：验证**
先在微信给 ClawBot 随便发一条消息（刷新投递 token），再等当晚 21:00——明天课程的前晚预告自动出现在微信里，就是部署成功。想立刻冒烟一遍，按 DEPLOY.md 第 6 步/根治版第 ⑤ 步用模拟时间走全链路。

**别忘了**：去 platform.deepseek.com 打开「余额预警」（如 ¥5）。余额耗尽没有任何先兆，会表现为提醒全天静默；在「发送渠道」页再开一个 Server酱/PushPlus 备用通道，排队超时未送达会自动补发，双保险。

---

## 微信里是什么样

```text
🌙 明天预告｜大学英语 10:00 在外语楼205｜带：英语书
🎒 该出门了｜大学英语 10:00 在外语楼205｜带：英语书
⏰ 快上课了｜大学英语 10:00 在外语楼205｜带：英语书
```
直接回复即可：`收到`（确认）、`今天请假`（取消这节课后续提醒）、`10分钟后再提醒我`（延后）、`明天有什么课？`（查询）、`课前提前20分钟提醒`（改设置）。

## 功能一览

- **课表导入**：txt / csv / xlsx / json，或本地页面上传粘贴；截图先转文字再导入
- **调课通知解析**：「停课、改到周五、换到 301、改线上」变成只影响那一次课的例外
- **三级提醒**：前晚预告 → 出门提醒 → 课前提醒；天气恶劣自动增加出门提前量
- **对话操作**：查课、改提醒时间/提前量/静默时段、关某类提醒，都是一句话
- **本地配置页**：总览、提醒设置、单课覆盖、未来课程、发送渠道、导入课表
- **可靠性**：过期提醒自动作废不补发；主会话送不出去时备用通道（Server酱/PushPlus）超时补发

## 工作原理

一条原则：**时间不能让大模型猜**。第几周、单双周、假期、调休、提醒时刻、状态流转全部由确定性 Python 代码计算并有测试覆盖；LLM 只负责把截图/通知转成文字和辅助措辞。另外两条：识别不准标「待确认」不猜；导入、解析、改配置都是先预览、确认后才写入。

微信投递走两阶段：调度器把到期提醒排进队列（`queued`），任务计划唤醒主会话，智能体逐条原样念进微信对话再回写 `sent`。这是因为个人微信没有官方机器人 API，直发接口不带会话 token 会被 iLink 拒绝（`sendMessage ret=-3`），只有主会话亲口说出的消息能送达。原理细节见 DEPLOY.md。

## 常用命令

```bash
python3 scripts/parse_notice.py "高等数学 2026年9月21日停课一次" --apply   # 解析通知
python3 scripts/ask.py "明天有什么课"                                     # 查课
python3 scripts/configure.py "课前提前20分钟提醒"                          # 改设置（预览）
python3 scripts/serve_ui.py                                               # 本地配置页 http://127.0.0.1:8765/
python3 scripts/run_scheduler.py --tick                                   # 手动跑一次检查
```

## 数据文件

`data/term.json`（学期与节次）、`data/courses.json`（周期课程）、`data/exceptions.json`（单次例外）、`data/user.json`（提醒偏好）；`occurrences.json`、`reminders.json` 是运行生成的，不用手工维护。发送渠道等本机设置在 `data/sender_settings.json`（含密钥，已 gitignore）。

## 测试

```bash
python3 -m unittest discover -s tests   # 338 个测试
```
覆盖周次/假期/调休计算、导入与通知解析、提醒生成与状态流转、天气提前量、对话查课与改设置、Web 页面、主会话两阶段投递、备用通道补发、过期作废、一键部署脚本。只用临时数据和本地假服务，不向真实地址发测试消息。

## 进度

已完成：确定性排程与三级提醒、课表导入、通知解析、回复确认/请假/延后、天气提前、对话查课与改设置、本地配置页、微信真实部署与全自动化、真实课表投入使用、一键部署与省钱设置、过期作废与备用兜底。

后续：DeepSeek 自然语言理解链路、语音交互、教务系统直取课表/内置 OCR、准确率评测、正式需求/设计/测试文档、演示视频与结题材料。
