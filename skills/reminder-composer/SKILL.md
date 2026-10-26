# Skill: reminder-composer
输入：一条 Occurrence + ReminderTask 类型 +（可选）天气。
输出：一条提醒文案。模板兜底（LLM 不可用时直接用模板）：
「{emoji} {courseName}｜{startTime} {building}{room}｜带：{requiredItems}」
规则：第一行只放课程/时间/地点；出门类提醒额外给出建议出门时间；不添加数据里没有的信息。
