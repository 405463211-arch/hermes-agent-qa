"""白盒 + 集成测试: plugins/ui-automation/dashboard/plugin_api.py

代码分支映射:
  /inventory                                    ✓ T_inventory
  /runs (无 root)                               ✓ T_runs_empty
  /runs (有数据)                                ✓ T_runs_list
  /runs (date 过滤)                             ✓ T_runs_date_filter
  /runs (limit)                                 ✓ T_runs_limit
  /runs/{rel} 详情                              ✓ T_run_detail
  /runs/{rel} 不存在                            ✓ T_run_not_found
  /runs/{rel} path traversal (../)             ✓ T_traversal_runs
  /view/{rel} HTML                              ✓ T_view_html
  /view/{rel} 不存在 → 404                       ✓ T_view_not_found
  /screenshot 正常返回                          ✓ T_screenshot_ok
  /screenshot path traversal                   ✓ T_screenshot_traversal
  /screenshot 非图片后缀                        ✓ T_screenshot_bad_ext
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


# ── 把 plugin_api 模块以独立名字 import 进来（路径含连字符不能用普通 import）─
def _import_plugin_api():
    plugin_dir = Path(__file__).resolve().parents[2] / "plugins" / "ui-automation" / "dashboard"
    if str(plugin_dir) not in sys.path:
        sys.path.insert(0, str(plugin_dir))
    if "plugin_api" in sys.modules:
        del sys.modules["plugin_api"]
    return importlib.import_module("plugin_api")


@pytest.fixture
def plugin_api(monkeypatch, tmp_path):
    """每个测试都拿一份新 plugin_api 实例 + 全 tmp_path 隔离"""
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    automation_root = tmp_path / "automation"
    (automation_root / "cases").mkdir(parents=True)
    (automation_root / "pages").mkdir()
    monkeypatch.setenv("HERMES_AUTOMATION_RUNS_DIR", str(runs_root))
    monkeypatch.setenv("HERMES_UI_AUTOMATION_ROOT", str(automation_root))
    return _import_plugin_api()


@pytest.fixture
def client(plugin_api):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(plugin_api.router)
    return TestClient(app)


def _make_run(runs_root: Path, date: str, name: str,
              report_data: dict | None = None,
              result_data: dict | None = None,
              screenshots: list[str] | None = None) -> Path:
    """工具：在 runs_root/<date>/<name>/ 下造一份 run 数据"""
    rd = runs_root / date / name
    (rd / "steps").mkdir(parents=True)
    if report_data is not None:
        (rd / "report.json").write_text(json.dumps(report_data), encoding="utf-8")
    if result_data is not None:
        (rd / "result.json").write_text(json.dumps(result_data), encoding="utf-8")
    for sc in screenshots or []:
        # 写一个最小有效 PNG
        (rd / "steps" / sc).write_bytes(_tiny_png())
    return rd


def _tiny_png() -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
        b"\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rI"
        b"DAT\x08\xd7c\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3?\xeb\x00"
        b"\x00\x00\x00IEND\xaeB`\x82"
    )


# ─── /inventory ─────────────────────────────────────────────────────
class TestInventory:
    def test_T_inventory_returns_keys(self, client):
        r = client.get("/inventory")
        assert r.status_code == 200
        body = r.json()
        for k in ("automation_root", "exists", "cases", "pages", "pages_auto",
                  "review_images", "hint"):
            assert k in body


# ─── /runs ──────────────────────────────────────────────────────────
class TestRunsList:
    def test_T_runs_empty_when_root_missing(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv(
            "HERMES_AUTOMATION_RUNS_DIR", str(tmp_path / "no_such_dir")
        )
        r = client.get("/runs")
        assert r.status_code == 200
        body = r.json()
        assert body["runs"] == []
        assert body["exists"] is False

    def test_T_runs_list_returns_recent(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-08", "case_001",
                  report_data={
                      "case_id": "case_001",
                      "success": True,
                      "steps": [
                          {"step_idx": 1, "action": "tap", "status": "PASS"},
                      ],
                      "steps_total": 1,
                      "steps_skipped": 0,
                      "vlm_calls_total": 3,
                  })
        r = client.get("/runs")
        body = r.json()
        assert len(body["runs"]) == 1
        run = body["runs"][0]
        assert run["case_id"] == "case_001"
        assert run["success"] is True
        assert run["steps_count"] == 1
        assert run["vlm_calls_total"] == 3
        # view_url 和 detail_url 字段必须有
        assert run["view_url"].endswith("/view/2026-05-08/case_001")
        assert run["detail_url"].endswith("/runs/2026-05-08/case_001")

    def test_T_runs_date_filter(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-07", "old", report_data={"success": True})
        _make_run(runs_root, "2026-05-08", "new", report_data={"success": False})

        r = client.get("/runs", params={"date": "2026-05-08"})
        runs = r.json()["runs"]
        assert len(runs) == 1
        assert runs[0]["name"] == "new"

    def test_T_runs_limit_param(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        for i in range(5):
            _make_run(runs_root, "2026-05-08", f"r_{i:03d}",
                      report_data={"success": True})
        r = client.get("/runs", params={"limit": 2})
        assert len(r.json()["runs"]) == 2

    def test_T_runs_skips_dirs_without_payload(self, client, plugin_api):
        """没有 report.json 也没有 result.json 的目录 → 跳过"""
        runs_root = Path(plugin_api._runs_root())
        empty = runs_root / "2026-05-08" / "no_payload"
        (empty / "steps").mkdir(parents=True)
        # 不写 report.json
        r = client.get("/runs")
        assert r.json()["runs"] == []

    def test_T_runs_uses_result_json_fallback(self, client, plugin_api):
        """没 report.json 但有 result.json → 用 result.json"""
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-08", "from_runner",
                  result_data={
                      "case_name": "interactive",
                      "success": True,
                      "steps_executed": 5,
                      "duration_s": 12.3,
                      "vlm_calls_total": 8,
                  })
        body = client.get("/runs").json()
        assert len(body["runs"]) == 1
        run = body["runs"][0]
        assert run["case_id"] == "interactive"  # result.json 用 case_name
        assert run["steps_count"] == 5
        assert run["duration_s"] == 12.3
        assert run["vlm_calls_total"] == 8


# ─── /runs/{rel_path} ───────────────────────────────────────────────
class TestRunDetail:
    def test_T_run_detail_returns_data_screenshots(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-08", "x",
                  report_data={"case_id": "x", "success": True, "steps": []},
                  screenshots=["step_001.png", "step_002.png"])
        r = client.get("/runs/2026-05-08/x")
        body = r.json()
        assert body["data"]["case_id"] == "x"
        assert body["screenshots"] == ["step_001.png", "step_002.png"]
        assert body["screenshot_url_prefix"].endswith("/screenshot/2026-05-08/x/")

    def test_T_run_not_found_returns_error(self, client):
        r = client.get("/runs/2026-99-99/no_such")
        assert r.status_code == 200
        assert r.json()["error"] == "run not found"

    def test_T_run_detail_path_traversal_denied(self, client, plugin_api):
        """rel_path 含 ../ 试图逃出 runs_root → traversal denied"""
        # FastAPI 会把 ../../etc/passwd normalize；我们的 _safe_resolve_under
        # 检查 resolve 后是否仍在 runs_root 内
        runs_root = Path(plugin_api._runs_root())
        # 构造一个会逃出去的请求
        r = client.get("/runs/../../etc/passwd")
        # 可能被 FastAPI 的 path 路由层 404，或被我们的检查拒
        assert r.status_code in (200, 404)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code == 200:
            assert "error" in body
        # 关键: 即使能进来，绝不能返回 /etc/passwd 内容
        assert "root:" not in r.text


# ─── /view/{rel_path} ───────────────────────────────────────────────
class TestViewHtml:
    def test_T_view_html_renders_steps(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-08", "view_test",
                  report_data={
                      "case_id": "view_test",
                      "success": False,
                      "duration_s": 1.5,
                      "vlm_calls_total": 2,
                      "steps_skipped": 1,
                      "steps": [
                          {"step_idx": 1, "action": "tap", "status": "PASS",
                           "elapsed_s": 0.5, "failure_code": ""},
                          {"step_idx": 2, "action": "wait", "status": "FAIL",
                           "elapsed_s": 0.1,
                           "failure_code": "OCR_NO_MATCH"},
                          {"step_idx": 3, "action": "tap", "status": "SKIPPED",
                           "elapsed_s": 0.0,
                           "failure_code": "CRITICAL_FAIL_AT_STEP_2"},
                      ],
                  },
                  screenshots=["step_001.png"])
        r = client.get("/view/2026-05-08/view_test")
        assert r.status_code == 200
        body = r.text
        assert "view_test" in body
        # 三种状态颜色都应出现
        assert "PASS" in body
        assert "FAIL" in body
        assert "SKIPPED" in body
        assert "OCR_NO_MATCH" in body
        assert "CRITICAL_FAIL_AT_STEP_2" in body
        # JS 注入的截图 URL
        assert "/screenshot/2026-05-08/view_test/step_001.png" in body

    def test_T_view_not_found_returns_404(self, client):
        r = client.get("/view/2026-99-99/no_such")
        assert r.status_code == 404
        assert "Error:" in r.text

    def test_T_view_no_screenshots_falls_back(self, client, plugin_api):
        """report 里有 steps 但 steps/ 目录空 → 仍能渲染列表"""
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-08", "no_pics",
                  report_data={
                      "case_id": "no_pics",
                      "success": True,
                      "steps": [{"step_idx": 1, "action": "wait",
                                 "status": "PASS", "elapsed_s": 0.1}],
                  })
        r = client.get("/view/2026-05-08/no_pics")
        assert r.status_code == 200
        # placeholder 应在
        assert "(no screenshots)" in r.text


# ─── /screenshot ────────────────────────────────────────────────────
class TestScreenshot:
    def test_T_screenshot_returns_png(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-08", "sc",
                  report_data={"case_id": "sc"},
                  screenshots=["step_001.png"])
        r = client.get("/screenshot/2026-05-08/sc/step_001.png")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content.startswith(b"\x89PNG")

    def test_T_screenshot_traversal_denied(self, client, plugin_api):
        """文件名含 ../ 想读跳出 steps/ 的文件 → 拒"""
        runs_root = Path(plugin_api._runs_root())
        # 在 run 同级造个秘密文件
        _make_run(runs_root, "2026-05-08", "sc",
                  report_data={"case_id": "sc"})
        secret = runs_root / "2026-05-08" / "sc" / "secret.png"
        secret.write_bytes(b"secret content")
        # 试图通过 ../../ 跳到非 steps/ 下读 secret
        r = client.get("/screenshot/2026-05-08/sc/..%2Fsecret.png")
        body_text = r.text
        # 不能读到 secret
        assert b"secret content" not in r.content
        # 状态码: 404 或 200+error
        assert r.status_code in (200, 404)

    def test_T_screenshot_bad_extension_rejected(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        rd = _make_run(runs_root, "2026-05-08", "sc",
                       report_data={"case_id": "sc"})
        # 在 steps/ 下放一个 .txt 文件
        (rd / "steps" / "secret.txt").write_text("password=hunter2", encoding="utf-8")
        r = client.get("/screenshot/2026-05-08/sc/secret.txt")
        # 必须被拒
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        assert body.get("error") == "unsupported file type"
        assert "hunter2" not in r.text

    def test_T_screenshot_missing_file_returns_error(self, client, plugin_api):
        runs_root = Path(plugin_api._runs_root())
        _make_run(runs_root, "2026-05-08", "sc",
                  report_data={"case_id": "sc"})
        r = client.get("/screenshot/2026-05-08/sc/nonexistent.png")
        body = r.json()
        assert body.get("error") == "not found"
