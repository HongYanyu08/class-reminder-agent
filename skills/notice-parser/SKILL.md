# Skill: notice-parser
输入：班群调课/停课通知原文（文字或截图 OCR 结果）。
输出：一条 CourseException（cancel / reschedule / location_change / online）。
规则：
- 必须定位到 courseId + originalDate 两个锚点，缺一则向用户追问
- 只影响单次课，不改周期模板；解析结果先复述、等确认再写入 exceptions.json
