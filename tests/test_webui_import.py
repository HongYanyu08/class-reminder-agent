"""Tests for the Web UI timetable-import endpoints.

POST /api/import/preview must parse only (never write); POST
/api/import/apply re-parses the same raw input and mirrors
scripts/import_timetable.py --apply: backup first, dedupe by
name+weekday+sections+weeks, "_issues" courses imported with the
flagged fields left blank (exactly the CLI's behaviour).
"""
import base64
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.webui import apply_import, get_state, handle_request, preview_import

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
SAMPLE_TEXT = (ROOT / "examples" / "sample_timetable.txt").read_text(
    encoding="utf-8"
)


def _md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def _b64(raw):
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _minimal_xlsx(rows):
    """Smallest .xlsx the stdlib reader accepts: one sheet, inline strings."""
    def cell(value):
        if value == "":
            return "<c/>"
        return (
            '<c t="inlineStr"><is><t>' + value + "</t></is></c>"
        )

    body = "".join(
        "<row>" + "".join(cell(v) for v in row) + "</row>" for row in rows
    )
    sheet = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/'
        'spreadsheetml/2006/main"><sheetData>' + body + "</sheetData>"
        "</worksheet>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


class ImportTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "data"
        shutil.copytree(DATA, self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def read_courses(self):
        return json.loads((self.data / "courses.json").read_text(encoding="utf-8"))

    def dir_snapshot(self):
        return sorted(p.name for p in self.data.iterdir())

    def post(self, path, payload):
        status, _, body = handle_request(
            self.data, "POST", path,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )
        return status, json.loads(body.decode("utf-8"))


class PreviewTest(ImportTestCase):
    def test_preview_text_sample_counts_and_fields(self):
        result = preview_import(self.data, {"text": SAMPLE_TEXT})
        summary = result["summary"]
        self.assertEqual(summary["total"], 5)
        self.assertEqual(summary["willAdd"], 4)
        self.assertEqual(summary["duplicates"], 1)
        self.assertEqual(summary["duplicateNames"], ["高等数学"])
        self.assertEqual(summary["withIssues"], 3)
        self.assertEqual(summary["issueCount"], 4)

        math = result["courses"][0]
        self.assertEqual(math["name"], "高等数学")
        self.assertEqual(math["weekday"], 1)
        self.assertEqual(math["sections"], {"start": 1, "end": 2})
        self.assertEqual(math["startTime"], "08:00")
        self.assertEqual(math["endTime"], "09:40")
        self.assertEqual(
            math["weeks"], {"mode": "odd", "startWeek": 1, "endWeek": 17}
        )
        self.assertEqual(math["location"]["building"], "教三")
        self.assertEqual(math["location"]["room"], "301")
        self.assertEqual(math["teacher"], "王老师")
        self.assertEqual(math["_issues"], [])

        pe = next(c for c in result["courses"] if c["name"] == "体育")
        self.assertIn("教师待确认", pe["_issues"])
        self.assertIn("教室号待确认", pe["_issues"])
        ai = next(c for c in result["courses"] if c["name"] == "人工智能导论")
        self.assertIn("节次待确认", ai["_issues"])
        self.assertEqual(ai["location"]["room"], "线上")

    def test_preview_never_writes(self):
        before_md5 = _md5(self.data / "courses.json")
        before_files = self.dir_snapshot()
        status, payload = self.post(
            "/api/import/preview", {"text": SAMPLE_TEXT}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(_md5(self.data / "courses.json"), before_md5)
        self.assertEqual(self.dir_snapshot(), before_files)

    def test_preview_csv_file(self):
        csv_text = (
            "课程名称,星期,节次,周次,上课地点,教师\n"
            "大学英语,周三,3-4,1-16周,外语楼205,李老师\n"
        )
        result = preview_import(
            self.data,
            {"filename": "tt.csv", "contentBase64": _b64(csv_text)},
        )
        self.assertEqual(result["summary"]["total"], 1)
        course = result["courses"][0]
        self.assertEqual(course["name"], "大学英语")
        self.assertEqual(course["weekday"], 3)
        self.assertEqual(course["sections"], {"start": 3, "end": 4})
        self.assertEqual(course["startTime"], "10:00")
        self.assertEqual(course["endTime"], "11:40")
        self.assertEqual(course["location"]["building"], "外语楼")
        self.assertEqual(course["location"]["room"], "205")
        self.assertEqual(course["_issues"], [])
        self.assertEqual(result["source"]["filename"], "tt.csv")

    def test_preview_json_file(self):
        rows = [{
            "name": "线性代数", "weekday": "周二", "sections": "3-4",
            "weeks": "2-18周(双)", "location": "教一102", "teacher": "赵老师",
        }]
        result = preview_import(
            self.data,
            {"filename": "tt.json", "contentBase64": _b64(json.dumps(rows))},
        )
        course = result["courses"][0]
        self.assertEqual(course["weekday"], 2)
        self.assertEqual(course["weeks"]["mode"], "even")
        self.assertEqual(course["_issues"], [])

    def test_preview_xlsx_file(self):
        raw = _minimal_xlsx([
            ["课程", "星期", "节次", "周次", "地点", "教师"],
            ["体育", "周五", "7", "第1,3,5,7,9周", "体育馆", ""],
        ])
        result = preview_import(
            self.data,
            {"filename": "tt.xlsx", "contentBase64": _b64(raw)},
        )
        self.assertEqual(result["summary"]["total"], 1)
        course = result["courses"][0]
        self.assertEqual(course["name"], "体育")
        self.assertEqual(course["sections"], {"start": 7, "end": 7})
        self.assertEqual(course["weeks"]["list"], [1, 3, 5, 7, 9])
        self.assertEqual(course["startTime"], "16:00")
        self.assertIn("教师待确认", course["_issues"])
        self.assertIn("教室号待确认", course["_issues"])


class ApplyTest(ImportTestCase):
    def test_apply_writes_backup_and_dedupes(self):
        before = len(self.read_courses())
        status, payload = self.post(
            "/api/import/apply", {"text": SAMPLE_TEXT}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["added"], 4)
        self.assertEqual(payload["skippedDuplicates"], 1)
        self.assertEqual(payload["total"], before + 4)
        self.assertTrue(payload["backup"])
        self.assertTrue((self.data / payload["backup"]).exists())

        stored = self.read_courses()
        self.assertEqual(len(stored), before + 4)
        for course in stored:
            self.assertNotIn("_issues", course)
        # A course with _issues is imported anyway (CLI semantics),
        # with the flagged fields left blank rather than guessed.
        pe = next(c for c in stored if c["name"] == "体育")
        self.assertEqual(pe["teacher"], "")
        self.assertEqual(pe["location"]["room"], "")

    def test_apply_twice_second_is_noop(self):
        apply_import(self.data, {"text": SAMPLE_TEXT})
        md5_after_first = _md5(self.data / "courses.json")
        backups_after_first = [
            n for n in self.dir_snapshot() if ".bak-" in n
        ]
        result = apply_import(self.data, {"text": SAMPLE_TEXT})
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["skippedDuplicates"], 5)
        self.assertIsNone(result["backup"])
        self.assertEqual(_md5(self.data / "courses.json"), md5_after_first)
        self.assertEqual(
            [n for n in self.dir_snapshot() if ".bak-" in n],
            backups_after_first,
        )

    def test_apply_reparse_matches_preview(self):
        preview = preview_import(self.data, {"text": SAMPLE_TEXT})
        applied = apply_import(self.data, {"text": SAMPLE_TEXT})
        self.assertEqual(applied["added"], preview["summary"]["willAdd"])
        self.assertEqual(
            applied["skippedDuplicates"], preview["summary"]["duplicates"]
        )
        self.assertEqual(applied["withIssues"], preview["summary"]["withIssues"])

    def test_state_reflects_imported_courses(self):
        apply_import(self.data, {"text": SAMPLE_TEXT})
        state = get_state(self.data, today="2026-09-07")
        self.assertEqual(state["summary"]["courseCount"], 7)
        names = {c["name"] for c in state["courses"]}
        self.assertIn("线性代数", names)


class RejectTest(ImportTestCase):
    def assert_rejected(self, path, payload, needle=None):
        before_md5 = _md5(self.data / "courses.json")
        before_files = self.dir_snapshot()
        status, body = self.post(path, payload)
        self.assertEqual(status, 400, body)
        self.assertFalse(body["ok"])
        if needle:
            self.assertIn(needle, body["error"])
        self.assertEqual(_md5(self.data / "courses.json"), before_md5)
        self.assertEqual(self.dir_snapshot(), before_files)

    def test_reject_bad_extension(self):
        payload = {"filename": "tt.xls", "contentBase64": _b64("x")}
        self.assert_rejected("/api/import/preview", payload, "不支持的文件类型")
        payload = {"filename": "tt.png", "contentBase64": _b64("x")}
        self.assert_rejected("/api/import/apply", payload, "不支持的文件类型")

    def test_reject_oversize_file(self):
        big = b"x" * (2 * 1024 * 1024 + 1)
        payload = {"filename": "big.txt", "contentBase64": _b64(big)}
        self.assert_rejected("/api/import/preview", payload, "超过 2 MB 上限")

    def test_reject_invalid_base64(self):
        payload = {"filename": "tt.txt", "contentBase64": "!!!not-base64!!!"}
        self.assert_rejected("/api/import/preview", payload, "base64")

    def test_reject_missing_conflicting_and_unknown_fields(self):
        self.assert_rejected("/api/import/preview", {}, "没有提供课表内容")
        both = {
            "text": "高等数学 周一 第1-2节 1-16周 教三301 王老师",
            "filename": "tt.txt",
            "contentBase64": _b64("x"),
        }
        self.assert_rejected("/api/import/preview", both, "二选一")
        self.assert_rejected(
            "/api/import/preview", {"text": "   "}, "课表文字为空"
        )
        unknown = {"text": "高等数学 周一 第1-2节", "bogus": 1}
        self.assert_rejected("/api/import/apply", unknown, "未知字段")
        self.assert_rejected(
            "/api/import/apply", {"filename": "tt.txt"}, "contentBase64"
        )

    def test_reject_unparseable_content(self):
        self.assert_rejected(
            "/api/import/preview", {"text": "# 只有注释，没有课程\n"},
            "没有解析出任何课程",
        )
        bad_json = {"filename": "tt.json", "contentBase64": _b64("{not json")}
        self.assert_rejected("/api/import/apply", bad_json, "解析失败")


class PageWiringTest(unittest.TestCase):
    def test_page_has_import_tab_and_endpoints(self):
        self.assertIn('data-tab="import"', HTML)
        self.assertIn('id="panel-import"', HTML)
        self.assertIn("/api/import/preview", HTML)
        self.assertIn("/api/import/apply", HTML)


if __name__ == "__main__":
    unittest.main()
