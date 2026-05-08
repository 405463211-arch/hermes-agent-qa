"""Dashboard plugin — list UI automation YAMLs under project-knowledge.

新增 endpoint（采纳 test-center E2eTaskDetail.vue 思路，仅后端版）:
    GET /runs                              列最近运行（按日期倒序）
    GET /runs/{rel_path:path}              单次 run 详情 JSON
    GET /runs/{rel_path:path}/view         详情页（vanilla HTML，左侧步骤右侧大图）
    GET /screenshot/{rel_path:path}/{filename}   返回某次 run 的截图
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse
from hermes_constants import get_hermes_home

router = APIRouter()


def _automation_root() -> Path:
    env = os.environ.get("HERMES_UI_AUTOMATION_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return get_hermes_home() / "project-knowledge" / "diancaibao-app" / "automation"


def _runs_root() -> Path:
    env = os.environ.get("HERMES_AUTOMATION_RUNS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return get_hermes_home() / "cache" / "ui-automation" / "runs"


def _yaml_entries(directory: Path, base: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(directory.glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        try:
            st = p.stat()
            try:
                rel = str(p.relative_to(base))
            except ValueError:
                rel = str(p)
            out.append(
                {
                    "name": p.name,
                    "rel_path": rel,
                    "bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
        except OSError:
            continue
    return out


def _png_entries(directory: Path, base: Path) -> list[dict]:
    if not directory.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(directory.glob("*_review.png")):
        try:
            st = p.stat()
            try:
                rel = str(p.relative_to(base))
            except ValueError:
                rel = str(p)
            out.append(
                {
                    "name": p.name,
                    "rel_path": rel,
                    "bytes": st.st_size,
                    "mtime": int(st.st_mtime),
                }
            )
        except OSError:
            continue
    return out


@router.get("/inventory")
async def inventory():
    """返回 cases / pages / auto 下的 YAML 与 auto review 图清单。"""
    root = _automation_root()
    return {
        "automation_root": str(root),
        "exists": root.is_dir(),
        "cases": _yaml_entries(root / "cases", root),
        "pages": _yaml_entries(root / "pages", root),
        "pages_auto": _yaml_entries(root / "pages" / "auto", root),
        "review_images": _png_entries(root / "pages" / "auto", root),
        "hint": (
            "设置 HERMES_UI_AUTOMATION_ROOT 可指向其它 automation 根目录；"
            "ADB 截图持久化见环境变量 HERMES_ADB_SCREENSHOT_DIR（写入 ~/.hermes/...）"
        ),
    }


def _safe_resolve_under(root: Path, rel_path: str) -> Path | None:
    """把用户传的 rel_path 解析为 root 下的绝对路径，防 path traversal。"""
    target = (root / unquote(rel_path)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def _read_run_payload(run_dir: Path) -> dict | None:
    """读 report.json 优先，没有则读 result.json；都没有返回 None。"""
    for name in ("report.json", "result.json"):
        f = run_dir / name
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
    return None


@router.get("/runs")
async def list_runs(date: str = "", limit: int = 50):
    """列最近运行（按日期倒序）。

    查询参数:
        date  YYYY-MM-DD 仅返回某天（空=全部）
        limit 最多返回多少条（默认 50）
    """
    root = _runs_root()
    if not root.is_dir():
        return {"runs": [], "runs_root": str(root), "exists": False}

    out: list[dict] = []
    for date_dir in sorted(root.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue
        if date and date_dir.name != date:
            continue
        for run_dir in sorted(date_dir.iterdir(), reverse=True):
            if not run_dir.is_dir():
                continue
            data = _read_run_payload(run_dir)
            if data is None:
                continue
            steps_count = (
                len(data["steps"])
                if isinstance(data.get("steps"), list)
                else int(data.get("steps_executed", 0))
            )
            rel = str(run_dir.relative_to(root))
            out.append(
                {
                    "date": date_dir.name,
                    "name": run_dir.name,
                    "case_id": data.get("case_id") or data.get("case_name") or "",
                    "task": data.get("task", ""),
                    "success": data.get("success"),
                    "duration_s": data.get("duration_s") or data.get("total_s"),
                    "steps_count": steps_count,
                    "steps_skipped": data.get("steps_skipped", 0),
                    "vlm_calls_total": data.get("vlm_calls_total", 0),
                    "rel_path": rel,
                    "view_url": f"/api/v1/plugins/ui_automation/view/{rel}",
                    "detail_url": f"/api/v1/plugins/ui_automation/runs/{rel}",
                }
            )
            if len(out) >= limit:
                return {"runs": out, "runs_root": str(root), "exists": True}
    return {"runs": out, "runs_root": str(root), "exists": True}


async def _build_run_detail(rel_path: str) -> dict:
    """共享逻辑：给 run_detail (JSON) 和 run_detail_html (HTML) 复用。"""
    root = _runs_root()
    run_dir = _safe_resolve_under(root, rel_path)
    if run_dir is None:
        return {"error": "path traversal denied"}
    if not run_dir.is_dir():
        return {"error": "run not found", "rel_path": rel_path}

    data = _read_run_payload(run_dir) or {}
    steps_dir = run_dir / "steps"
    screenshots = (
        sorted([p.name for p in steps_dir.glob("*.png")])
        if steps_dir.exists()
        else []
    )
    return {
        "rel_path": rel_path,
        "data": data,
        "screenshots": screenshots,
        "screenshot_url_prefix": (
            f"/api/v1/plugins/ui_automation/screenshot/{rel_path}/"
        ),
    }


@router.get("/runs/{rel_path:path}")
async def run_detail(rel_path: str):
    """单次 run 详情 JSON。

    路由顺序注意: 因为 rel_path:path 贪婪匹配会吃掉 /view 后缀，
    所以 detail JSON 端点拒绝以 /view 结尾的请求 — 让 detail HTML
    走专属处理器。
    """
    if rel_path.endswith("/view") or rel_path == "view":
        rel = rel_path.removesuffix("/view")
        return await _build_run_detail(rel)
    return await _build_run_detail(rel_path)


@router.get("/screenshot/{rel_path:path}/{filename}")
async def screenshot_file(rel_path: str, filename: str):
    """返回某次 run 下 steps/ 里的截图文件。"""
    root = _runs_root()
    run_dir = _safe_resolve_under(root, rel_path)
    if run_dir is None:
        return {"error": "path traversal denied"}
    if not filename.lower().endswith((".png", ".jpg", ".jpeg")):
        return {"error": "unsupported file type"}
    img_path = (run_dir / "steps" / filename).resolve()
    try:
        img_path.relative_to(run_dir.resolve())
    except ValueError:
        return {"error": "path traversal denied"}
    if not img_path.exists() or not img_path.is_file():
        return {"error": "not found"}
    return FileResponse(img_path, media_type="image/png")


_DETAIL_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>Run: {title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
         margin: 0; display: flex; height: 100vh; }}
  .left {{ width: 380px; padding: 12px; overflow-y: auto;
          border-right: 1px solid #ddd; background: #fafafa; }}
  .right {{ flex: 1; display: flex; align-items: center;
           justify-content: center; background: #1f1f1f; }}
  .right img {{ max-width: 95%; max-height: 95%;
                box-shadow: 0 4px 12px rgba(0,0,0,0.4); }}
  .right .placeholder {{ color: #888; }}
  ul {{ list-style: none; padding: 0; }}
  li {{ cursor: pointer; padding: 6px 8px; margin-bottom: 4px;
        border-left: 4px solid #ccc; background: #fff; }}
  li:hover {{ background: #eef; }}
  li.active {{ background: #e6f0ff; }}
  h3 {{ margin-top: 0; }}
  .meta {{ color: #555; font-size: 13px; margin-bottom: 12px;
          line-height: 1.6; }}
  .badge {{ display: inline-block; padding: 1px 8px; border-radius: 10px;
            font-size: 12px; font-weight: 600; color: #fff; }}
</style></head><body>
<div class="left">
  <h3>{title}</h3>
  <div class="meta">{meta_html}</div>
  <ul>{steps_html}</ul>
</div>
<div class="right">
  <img id="img" src="" alt="step screenshot" style="display:none">
  <span id="placeholder" class="placeholder">(no screenshots)</span>
</div>
<script>
  const screenshots = {screenshots_js};
  function show(i, el) {{
    if (!screenshots[i]) return;
    document.getElementById('img').src = screenshots[i];
    document.getElementById('img').style.display = '';
    document.getElementById('placeholder').style.display = 'none';
    document.querySelectorAll('li.active').forEach(n => n.classList.remove('active'));
    if (el) el.classList.add('active');
  }}
  if (screenshots.length > 0) show(0, document.querySelectorAll('li')[0]);
</script>
</body></html>"""


@router.get("/view/{rel_path:path}", response_class=HTMLResponse)
async def run_detail_html(rel_path: str):
    """简易详情页 — 左侧步骤列表，右侧大图（vanilla JS，无前端构建）。

    URL 格式: /api/v1/plugins/ui_automation/view/{date}/{run_name}
    （刻意把 view 放前缀避免与 /runs/{rel_path} 冲突）
    """
    detail = await _build_run_detail(rel_path)
    if isinstance(detail, dict) and "error" in detail:
        return HTMLResponse(
            f"<h2>Error: {detail['error']}</h2>", status_code=404
        )

    data = detail["data"]
    screenshots = detail["screenshots"]
    prefix = detail["screenshot_url_prefix"]

    title = data.get("case_id") or data.get("case_name") or "Run"
    duration = data.get("duration_s") or data.get("total_s") or 0
    success = data.get("success")
    success_color = "#1aaa55" if success else "#db3b21"
    success_label = "PASS" if success else "FAIL"
    if success is None:
        success_color = "#999"
        success_label = "UNKNOWN"

    meta_html = (
        f'状态: <span class="badge" style="background:{success_color}">'
        f'{success_label}</span><br>'
        f"耗时: {float(duration):.2f}s<br>"
        f"VLM 调用: {data.get('vlm_calls_total', 0)} | "
        f"步数: {data.get('steps_executed') or len(data.get('steps', []))} | "
        f"跳过: {data.get('steps_skipped', 0)}"
    )

    steps = data.get("steps", []) or []
    steps_html = ""
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict):
            continue
        status = s.get("status") or ("PASS" if s.get("success") else "FAIL")
        action = s.get("action", "")
        elapsed = float(s.get("elapsed_s", 0) or 0)
        failure = s.get("failure_code") or s.get("detail", "") or ""
        color = {
            "PASS": "#1aaa55",
            "FAIL": "#db3b21",
            "SKIPPED": "#999",
        }.get(status, "#666")
        steps_html += (
            f'<li onclick="show({i - 1}, this)" style="border-left-color:{color}">'
            f"<b>#{i}</b> {action} "
            f'<span style="color:{color};font-weight:600">[{status}]</span> '
            f'<span style="color:#888">{elapsed:.2f}s</span>'
            + (f"<br><small style='color:#888'>{failure}</small>" if failure else "")
            + "</li>"
        )

    if not steps_html and screenshots:
        # case_engine 没有 steps 字段时（比如 runner 的 result.json）按 screenshots 降级
        for i, name in enumerate(screenshots, 1):
            steps_html += (
                f'<li onclick="show({i - 1}, this)">'
                f"<b>#{i}</b> {name}</li>"
            )

    return HTMLResponse(
        _DETAIL_HTML_TEMPLATE.format(
            title=title,
            meta_html=meta_html,
            steps_html=steps_html or "<li>(no steps)</li>",
            screenshots_js=json.dumps([f"{prefix}{name}" for name in screenshots]),
        )
    )
