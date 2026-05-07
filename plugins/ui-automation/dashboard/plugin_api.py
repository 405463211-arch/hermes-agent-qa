"""Dashboard plugin — list UI automation YAMLs under project-knowledge."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from hermes_constants import get_hermes_home

router = APIRouter()


def _automation_root() -> Path:
    env = os.environ.get("HERMES_UI_AUTOMATION_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return get_hermes_home() / "project-knowledge" / "diancaibao-app" / "automation"


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
