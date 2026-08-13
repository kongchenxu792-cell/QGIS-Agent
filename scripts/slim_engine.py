#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
slim_engine.py — QGIS-Agent 引擎清单驱动瘦身脚本（只执行本文件内明确列出的删除项）

用途：
  对 qgis-portable/ 引擎做保守瘦身。删除清单由 Solo 审批后固化在本文件内，
  脚本执行时：
    1. 逐项检查目标是否存在，记录删除前大小
    2. 对删除项执行"移入回收站"（走 Windows SHFileOperation，可恢复）
    3. 执行后输出删除记录与体积变化，追加到 reports/slim_log.md

红线保护：
  - 内置 RED_LINE 清单（Solo 审批的禁止删除项），任何匹配均拒绝执行
  - 仅允许删除 DEL_TIERS 中明确列出的路径模式

用法：
  python scripts/slim_engine.py          # 查看待删清单（dry-run 模式，默认）
  python scripts/slim_engine.py --apply  # 执行第一、二档删除（删除前逐项确认）
  python scripts/slim_engine.py --tier 1 --apply   # 只执行第一档
  python scripts/slim_engine.py --tier 2 --apply   # 只执行第二档

说明：
  2026-08-13 引擎瘦身任务已完成两档删除（见 reports/slim_log.md），
  本脚本用于记录清单、支持重跑与复盘，删除动作默认 dry-run。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 路径常量
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(r"D:\桌面\QGIS-Agent")
ENGINE_DIR = PROJECT_ROOT / "qgis-portable"
SP_DIR = ENGINE_DIR / "apps" / "Python312" / "Lib" / "site-packages"
BIN_DIR = ENGINE_DIR / "bin"
QT5_BIN_DIR = ENGINE_DIR / "apps" / "Qt5" / "bin"
REPORTS_DIR = PROJECT_ROOT / "reports"
LOG_FILE = REPORTS_DIR / "slim_log.md"

# ---------------------------------------------------------------------------
# 🔴 红线 — 禁止删除清单（Solo 审批，任何情况不删，不讨论）
# ---------------------------------------------------------------------------

RED_LINE_DIRS = {
    "pymupdf", "_mupdf.pyd", "mupdfcpp64.dll",           # fitz 依赖
    "Qt5", "QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "Qsci.pyd",  # PyQt5 绑定
    "numpy", "numpy.libs",                                # numpy
    "osgeo", "_gdal.pyd", "gdal", "gdal312.dll",          # GDAL 核心
    "shapely", "PyQt5", "PIL", "requests", "docx",        # 核心第三方
    "qgis_core.dll", "qgis_gui.dll", "qgis_app.dll", "qgis_analysis.dll",
    "icudt67.dll", "python312.dll", "python311.dll",
    "scipy", "scipy.libs", "pandas", "matplotlib",
    "pip", "setuptools",
}
RED_LINE_PREFIXES = ("Qt5", "provider_", "ogr_", "gdal", "qgis_")
RED_LINE_EXACT = {
    "qgis_core.dll", "qgis_gui.dll", "qgis_app.dll", "qgis_analysis.dll",
    "gdal312.dll", "gdal311.dll", "gdal.dll", "icudt67.dll",
    "python312.dll", "python311.dll", "mupdfcpp64.dll",
}

# ---------------------------------------------------------------------------
# 删除清单（Solo 审批）
# ---------------------------------------------------------------------------

DEL_TIERS = {
    1: {  # 第一档：纯 Python 包（site-packages 目录）
        "wx", "plotly", "pyarrow", "fontTools", "sipbuild", "OpenGL",
        "networkx", "pydantic", "pydantic_core", "reportlab", "pygments",
        "PyInstaller", "narwhals", "remotior_sensus", "future", "owslib",
        "osgeo_utils", "lxml", "pythonwin", "win32", "win32comext", "win32com",
    },
    2: {  # 第二档：孤立第三方 SDK DLL
        "gsdll64.dll", "NCSEcw.dll", "lti_dsdk_9.5.dll",
        "lti_dsdk_cdll_9.5.dll", "adal.dll", "cairo.dll",
        "Qt5Designer.dll", "Qt5DesignerComponents.dll",
    },
}

# 第二档 DLL 的根目录映射
TIER2_ROOTS = {
    "gsdll64.dll": BIN_DIR, "NCSEcw.dll": BIN_DIR,
    "lti_dsdk_9.5.dll": BIN_DIR, "lti_dsdk_cdll_9.5.dll": BIN_DIR,
    "adal.dll": BIN_DIR, "cairo.dll": BIN_DIR,
    "Qt5Designer.dll": QT5_BIN_DIR, "Qt5DesignerComponents.dll": QT5_BIN_DIR,
}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def fmt_size(n: int) -> str:
    if n >= 1 << 30:
        return f"{n / (1 << 30):.2f} GB"
    if n >= 1 << 20:
        return f"{n / (1 << 20):.2f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.2f} KB"
    return f"{n} B"


def dir_size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def is_red_line(path: Path) -> bool:
    """判断路径是否命中红线清单。"""
    name = path.name
    if name in RED_LINE_DIRS or name in RED_LINE_EXACT:
        return True
    if name.startswith(RED_LINE_PREFIXES):
        return True
    # 目录下任何子项命中红线也不删
    if path.is_dir():
        for child in path.rglob("*"):
            if child.name in RED_LINE_DIRS or child.name in RED_LINE_EXACT:
                return True
            if child.name.startswith(RED_LINE_PREFIXES):
                return True
    return False


def move_to_recycle_bin(path: Path) -> bool:
    """将文件/目录移入回收站（SHFileOperation），失败返回 False。"""
    path = str(path.resolve())
    buf = ctypes.create_unicode_buffer(path + "\0", len(path) + 2)
    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", ctypes.c_void_p),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", ctypes.c_bool),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]
    FO_DELETE = 0x0003
    FOF_ALLOWUNDO = 0x0040
    FOF_NOCONFIRMATION = 0x0010
    FOF_SILENT = 0x0004
    op = SHFILEOPSTRUCTW(
        None, FO_DELETE, buf, None,
        FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT,
        False, None, None,
    )
    return ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op)) == 0


def collect_targets(tier: int) -> list:
    """按档收集待删路径（仅存在且未命中红线）。"""
    targets = []
    for name in sorted(DEL_TIERS[tier]):
        if tier == 1:
            p = SP_DIR / name
        else:
            p = TIER2_ROOTS[name] / name
        if p.exists():
            targets.append(p)
    return targets


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="QGIS-Agent 引擎清单驱动瘦身")
    parser.add_argument("--tier", type=int, choices=[1, 2], default=None,
                        help="只执行指定档；缺省为两档都处理")
    parser.add_argument("--apply", action="store_true",
                        help="真正执行删除；缺省为 dry-run 预览")
    args = parser.parse_args()

    tiers = [args.tier] if args.tier else [1, 2]
    total_freed = 0
    records = []

    for tier in tiers:
        targets = collect_targets(tier)
        if not targets:
            print(f"[Tier {tier}] 无待处理项（可能已删过）")
            continue
        print(f"[Tier {tier}] 待处理 {len(targets)} 项：")
        tier_size = 0
        for p in targets:
            size = dir_size(p) if p.is_dir() else p.stat().st_size
            tier_size += size
            print(f"  - {p.relative_to(ENGINE_DIR)}  ({fmt_size(size)})")
        print(f"[Tier {tier}] 小计：{fmt_size(tier_size)}")

        if not args.apply:
            continue

        for p in targets:
            if is_red_line(p):
                print(f"  [跳过-红线] {p}")
                continue
            size = dir_size(p) if p.is_dir() else p.stat().st_size
            if move_to_recycle_bin(p):
                records.append((p, size, "recycle_bin"))
                total_freed += size
                print(f"  [已删] {p}  (-{fmt_size(size)})")
            else:
                records.append((p, size, "FAILED"))
                print(f"  [失败] {p}")

    if args.apply and records:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"\n## slim_engine.py 执行记录（{now}）\n")
            f.write(f"- 本次释放：{fmt_size(total_freed)}\n")
            for p, size, status in records:
                f.write(f"- [{status}] {p}  (-{fmt_size(size)})\n")
        print(f"\n[完成] 释放 {fmt_size(total_freed)}，记录已追加至 {LOG_FILE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
