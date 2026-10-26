"""Timetable importer: turn text / CSV / JSON / XLSX timetables into courses.json entries.

Design rules (from skills/timetable-import/SKILL.md):
- Deterministic parsing only; never guess a classroom, teacher or time.
- Anything missing or ambiguous is reported in the course's "_issues" list
  for a human to confirm, instead of being silently filled in.
- Sections and clock times are both kept; times are derived from
  term["sectionTimes"] when the source only gives section numbers.
"""
import csv, hashlib, io, json, re, zipfile
import xml.etree.ElementTree as ET

WEEKDAY_MAP = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7}

COLUMN_ALIASES = {
    "课程": "name", "课程名": "name", "课程名称": "name", "name": "name",
    "星期": "weekday", "周几": "weekday", "weekday": "weekday",
    "节次": "sections", "节": "sections", "sections": "sections",
    "时间": "time", "上课时间": "time", "time": "time",
    "周次": "weeks", "周": "weeks", "weeks": "weeks",
    "地点": "location", "教室": "location", "上课地点": "location", "location": "location",
    "教师": "teacher", "老师": "teacher", "teacher": "teacher",
    "携带物品": "requiredItems", "带": "requiredItems", "requiredItems": "requiredItems",
    "校区": "campus", "campus": "campus",
}

# ---------------------------------------------------------------- weeks ----
def parse_weeks_string(s, term=None):
    """Parse Chinese week expressions. Returns a weeks dict or None.
    Supports: 1-17周 / 1-17周(单) / 1-17周双 / 第3周 / 1,3,5周 / 全学期 / 1-16周单周"""
    if s is None:
        return None
    if isinstance(s, dict):
        return s
    s = str(s).strip()
    total = int((term or {}).get("totalWeeks", 18))
    if s in ("全学期", "全部", "每周", "全周"):
        return {"mode": "all", "startWeek": 1, "endWeek": total}
    m = re.search(r"((?:\d+\s*[,、，]\s*)+\d+)\s*周", s)
    if m:
        weeks = [int(x) for x in re.split(r"[,、，]", m.group(1)) if x.strip()]
        return {"mode": "list", "list": weeks}
    m = re.search(r"第?\s*(\d+)\s*[-~—]\s*(\d+)\s*周\s*[\(（]?\s*(单|双)?\s*[\)）]?\s*(?:周)?", s)
    if m:
        start, end, parity = int(m.group(1)), int(m.group(2)), m.group(3)
        mode = {"单": "odd", "双": "even"}.get(parity, "range" if (start, end) != (1, total) else "all")
        if mode == "all":
            return {"mode": "all", "startWeek": start, "endWeek": end}
        return {"mode": mode, "startWeek": start, "endWeek": end}
    m = re.search(r"第?\s*(\d+)\s*周", s)
    if m:
        return {"mode": "list", "list": [int(m.group(1))]}
    return None

def parse_weekday(v):
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if s.isdigit():
        return int(s)
    m = re.search(r"[周星期]([一二三四五六日天])", s)
    return WEEKDAY_MAP.get(m.group(1)) if m else None

def parse_sections(v):
    if isinstance(v, dict):
        return {"start": int(v["start"]), "end": int(v["end"])}
    if isinstance(v, (list, tuple)) and len(v) == 2:
        return {"start": int(v[0]), "end": int(v[1])}
    m = re.search(r"(\d+)\s*[-~、,，]\s*(\d+)", str(v))
    if m:
        return {"start": int(m.group(1)), "end": int(m.group(2))}
    m = re.search(r"(\d+)", str(v))
    if m:
        n = int(m.group(1))
        return {"start": n, "end": n}
    return None

def split_location(raw):
    """'教三301' -> building 教三, room 301. '线上' -> room 线上. Never invents."""
    raw = str(raw or "").strip()
    if not raw:
        return {"campus": "", "building": "", "room": ""}, ["地点待确认"]
    if "线上" in raw or "在线" in raw:
        return {"campus": "", "building": "", "room": "线上"}, []
    m = re.match(r"^(.+?)(机房\s*\d+|\d{2,4}\s*[A-Za-z]?)$", raw)
    if m:
        return {"campus": "", "building": m.group(1).strip(), "room": m.group(2).replace(" ", "")}, []
    return {"campus": "", "building": raw, "room": ""}, ["教室号待确认"]

def _derive_times(sections, term, issues):
    st = (term or {}).get("sectionTimes", {})
    if not sections or str(sections["start"]) not in st or str(sections["end"]) not in st:
        issues.append("上课时间待确认（节次无法换算）")
        return "", ""
    return st[str(sections["start"])][0], st[str(sections["end"])][1]

def _make_id(name, weekday, sections):
    raw = f"{name}|{weekday}|{sections}"
    return "c_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]

def _finish(course, issues):
    course["_issues"] = issues
    return course

# ----------------------------------------------------------------- text ----
def parse_text_line(line, term):
    """Parse one free-form line, e.g.
    高等数学 周一 第1-2节 1-17周(单) 教三301 王老师 带:课本、习题册"""
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    issues, rest = [], s

    def take(pattern, flags=0):
        nonlocal rest
        m = re.search(pattern, rest, flags)
        if m:
            rest = (rest[:m.start()] + " " + rest[m.end():]).strip()
        return m

    items = []
    m = take(r"(?:带|携带物品|物品)[:：]?\s*([^\s;；|]+(?:[、,，][^\s;；|]+)*)")
    if m:
        items = [x for x in re.split(r"[、,，]", m.group(1)) if x]

    teacher = ""
    m = take(r"教师[:：]?\s*([一-龥]{2,4})") or take(r"([一-龥]{1,3}老师)")
    if m:
        teacher = m.group(1)
        if not teacher.endswith("老师"):
            teacher += "老师"
    else:
        issues.append("教师待确认")

    start_time = end_time = ""
    m = take(r"(\d{1,2}:\d{2})\s*[-~—]\s*(\d{1,2}:\d{2})")
    if m:
        start_time, end_time = m.group(1).zfill(5), m.group(2).zfill(5)

    weeks = parse_weeks_string(rest, term)
    if weeks:
        rest = re.sub(r"第?\s*((?:\d+\s*[,、，]\s*)+\d+)\s*周", " ", rest)
        rest = re.sub(r"第?\s*(?:\d+\s*[-~—]\s*\d+|\d+)\s*周\s*[\(（]?\s*[单双]?\s*[\)）]?\s*(?:周)?", " ", rest)
        rest = rest.replace("全学期", " ")
    else:
        issues.append("周次待确认")

    weekday = None
    m = take(r"(?:周|星期)([一二三四五六日天])")
    if m:
        weekday = WEEKDAY_MAP[m.group(1)]
    else:
        issues.append("星期待确认")

    sections = None
    m = take(r"第?\s*(\d+)\s*[-~、,，]\s*(\d+)\s*节") or take(r"第?\s*(\d+)\s*节")
    if m:
        g = m.groups()
        sections = {"start": int(g[0]), "end": int(g[-1])}
    if not start_time and sections:
        start_time, end_time = _derive_times(sections, term, issues)
    if not start_time:
        issues.append("上课时间待确认")
    if not sections:
        issues.append("节次待确认")

    parts = [p for p in re.split(r"[\s;；|]+", rest) if p and p not in ("课程",)]
    name = parts[0] if parts else ""
    if not name:
        issues.append("课程名待确认")
    loc_raw = parts[1] if len(parts) > 1 else ""
    location, loc_issues = split_location(loc_raw)
    issues.extend(loc_issues)
    if len(parts) > 2:
        issues.append("未识别字段待确认：" + " ".join(parts[2:]))

    return _finish({
        "id": _make_id(name, weekday, sections), "name": name, "teacher": teacher,
        "location": location, "weekday": weekday, "sections": sections,
        "startTime": start_time, "endTime": end_time,
        "weeks": weeks or {"mode": "all", "startWeek": 1, "endWeek": int((term or {}).get("totalWeeks", 18))},
        "requiredItems": items, "reminderOverride": {},
    }, issues)

def parse_text(text, term):
    out = []
    for line in str(text).splitlines():
        c = parse_text_line(line, term)
        if c:
            out.append(c)
    return out

# ------------------------------------------------------- structured rows ----
def normalize_row(row, term):
    r = {}
    for k, v in row.items():
        key = COLUMN_ALIASES.get(str(k).strip())
        if key and v is not None and str(v).strip() != "":
            r[key] = v
    issues = []
    name = str(r.get("name", "")).strip()
    if not name:
        issues.append("课程名待确认")
    weekday = parse_weekday(r.get("weekday")) if r.get("weekday") is not None else None
    if weekday is None:
        issues.append("星期待确认")
    sections = parse_sections(r["sections"]) if r.get("sections") else None
    start_time = end_time = ""
    if r.get("time"):
        m = re.search(r"(\d{1,2}:\d{2})\s*[-~—]\s*(\d{1,2}:\d{2})", str(r["time"]))
        if m:
            start_time, end_time = m.group(1).zfill(5), m.group(2).zfill(5)
    if not start_time and sections:
        start_time, end_time = _derive_times(sections, term, issues)
    if not start_time:
        issues.append("上课时间待确认")
    if not sections:
        issues.append("节次待确认")
    weeks = parse_weeks_string(r.get("weeks"), term)
    if not weeks:
        weeks = {"mode": "all", "startWeek": 1, "endWeek": int((term or {}).get("totalWeeks", 18))}
        issues.append("周次待确认")
    if isinstance(r.get("location"), dict):
        location = {"campus": r.get("campus", r["location"].get("campus", "")),
                    "building": r["location"].get("building", ""), "room": r["location"].get("room", "")}
        if not location["room"]:
            issues.append("教室号待确认")
    else:
        location, li = split_location(r.get("location", ""))
        issues.extend(li)
        if r.get("campus"):
            location["campus"] = str(r["campus"])
    teacher = str(r.get("teacher", "")).strip()
    if not teacher:
        issues.append("教师待确认")
    items = r.get("requiredItems", [])
    if isinstance(items, str):
        items = [x for x in re.split(r"[、,，]", items) if x]
    return _finish({
        "id": _make_id(name, weekday, sections), "name": name, "teacher": teacher,
        "location": location, "weekday": weekday, "sections": sections,
        "startTime": start_time, "endTime": end_time, "weeks": weeks,
        "requiredItems": items, "reminderOverride": {},
    }, issues)

def parse_csv_text(text, term):
    rows = list(csv.DictReader(io.StringIO(text)))
    return [normalize_row(r, term) for r in rows if any(str(v or "").strip() for v in r.values())]

def parse_json_text(text, term):
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("courses", [data])
    return [normalize_row(r, term) for r in data]

# ----------------------------------------------------------------- xlsx ----
def _xlsx_rows(path):
    """Minimal .xlsx reader (stdlib only): first sheet -> list of row lists."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            root = ET.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join(t.text or "" for t in si.iter("{%s}t" % ns["m"])))
        sheet_name = next(n for n in z.namelist()
                          if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        root = ET.fromstring(z.read(sheet_name))
    rows = []
    for row in root.iter("{%s}row" % ns["m"]):
        cells = []
        for c in row.findall("m:c", ns):
            v = c.find("m:v", ns)
            inline = c.find("m:is", ns)
            if c.get("t") == "s" and v is not None:
                cells.append(shared[int(v.text)])
            elif inline is not None:
                cells.append("".join(t.text or "" for t in inline.iter("{%s}t" % ns["m"])))
            else:
                cells.append(v.text if v is not None else "")
        rows.append(cells)
    return rows

def parse_xlsx(path, term):
    rows = _xlsx_rows(path)
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    return [normalize_row(dict(zip(header, r)), term) for r in body if any(str(x).strip() for x in r)]

# -------------------------------------------------------------- dispatch ----
def load_input(path, term):
    p = str(path)
    if p.endswith(".json"):
        return parse_json_text(open(p, encoding="utf-8").read(), term)
    if p.endswith(".csv"):
        return parse_csv_text(open(p, encoding="utf-8-sig").read(), term)
    if p.endswith(".xlsx"):
        return parse_xlsx(p, term)
    if p.endswith(".xls"):
        raise ValueError(".xls 老格式不支持，请在 Excel/WPS 里另存为 .xlsx 或 .csv")
    return parse_text(open(p, encoding="utf-8").read(), term)  # .txt / OCR 文本

def parse_image(path):
    raise NotImplementedError(
        "图片 OCR 不在确定性解析器内：请先由智能体（视觉模型）把课表图片转成文字，"
        "再把文字按 text 格式喂给本解析器。见 skills/timetable-import/SKILL.md")

# ---------------------------------------------------------------- merge ----
def dedupe_key(c):
    s = c.get("sections") or {}
    return (c.get("name"), c.get("weekday"), s.get("start"), s.get("end"),
            json.dumps(c.get("weeks", {}), sort_keys=True, ensure_ascii=False))

def merge_courses(existing, new_courses):
    """Returns (merged, added, duplicates). Strips _issues from stored entries."""
    merged = list(existing)
    seen = {dedupe_key(c) for c in existing}
    added, duplicates = [], []
    for c in new_courses:
        if dedupe_key(c) in seen:
            duplicates.append(c)
            continue
        clean = {k: v for k, v in c.items() if not k.startswith("_")}
        merged.append(clean); added.append(clean); seen.add(dedupe_key(c))
    return merged, added, duplicates
