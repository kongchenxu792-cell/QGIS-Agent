#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scan_deps.py — QGIS-Agent 引擎瘦身依赖扫描器（只读，不修改任何项目文件）

用途：
  为 qgis-portable/（1.73GB）瘦身提供数据依据，产出"可删清单 / 必须保留清单"。

功能：
  1. 扫描项目源码的 import 闭包（AST 解析，含本地模块递归）
  2. 对照 qgis-portable 内置 Python 的 site-packages，标出 用到/未用到 的包
  3. 解析 Windows PE DLL 导入表（纯标准库实现），构建依赖图，找出孤立 DLL
  4. 输出 Markdown 报告（默认 reports/dep_scan_report.md）

用法：
  python scripts/scan_deps.py [--project <根目录>] [--output <报告路径>]
                              [--entries main.py 启动_静默.py]

注意：
  - 仅做静态分析；DLL 延迟加载 / 运行时 os.add_dll_directory 行为无法覆盖，
    删除前必须以"删一个、跑一遍测试"的方式人工验证。
"""

import argparse
import ast
import json
import os
import re
import struct
import sys
from collections import defaultdict
from datetime import datetime

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SKIP_DIRS = {
    "qgis-portable", "__pycache__", ".git", ".venv", "venv", "node_modules",
    "temp", "logs", "user_data", "wdac_policy", ".pytest_cache", "dist",
    "build", "交流中转站", ".idea", ".vscode", "reports",
}
PY_EXTS = {".py"}
DLL_EXTS = {".dll", ".pyd"}

# 核心 DLL：从这些出发做反向可达闭包 = 必须保留
CORE_DLLS = {
    "qgis_core.dll", "qgis_gui.dll", "qgis_app.dll", "qgis_analysis.dll",
    "python312.dll", "python311.dll",
    "Qt5Core.dll", "Qt5Gui.dll", "Qt5Widgets.dll",
    "gdal312.dll", "gdal311.dll", "gdal.dll",
}

# ---------------------------------------------------------------------------
# 1. 源码 import 扫描
# ---------------------------------------------------------------------------

def collect_py_files(root):
    """收集项目内所有 .py 文件（排除引擎与跳过目录）。"""
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() in PY_EXTS:
                files.append(os.path.join(dirpath, fn))
    return files


def get_imports(path):
    """AST 解析单文件，返回 [(模块名, 相对层级), ...] 列表。

    模块名不含相对层级的 '.' 前缀；level=0 表示绝对导入。
    """
    imports = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tree = ast.parse(f.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError, OSError):
        return imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imports.append((a.name.split(".")[0], 0))
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append((node.module.split(".")[0], node.level))
            elif node.level > 0:
                # from . import xxx —— 模块名为空，仅相对层级
                imports.append(("", node.level))
    return imports


def build_local_module_map(py_files):
    """本地模块名 -> 文件路径。

    注册规则：
      - 每个 .py 文件：basename 名 + 相对项目根的 dotted 路径
      - 每个 __init__.py：其所在目录的包名
      - 若存在 src/ 包根，同时注册去 src. 前缀的别名（core.xxx -> src/core/xxx.py）
    返回 (modmap, dirmap)：modmap 模块名->文件，dirmap 包目录->该目录下所有 py 文件
    """
    modmap = {}
    dirmap = {}
    has_src = os.path.isdir(os.path.join(project_root, "src"))
    for fp in py_files:
        rel = os.path.relpath(fp, project_root)
        dotted = os.path.splitext(rel.replace(os.sep, "."))[0]
        base = os.path.basename(fp)[:-3] if fp.endswith(".py") else ""
        names = [dotted]
        if has_src and dotted.startswith("src."):
            names.append(dotted[len("src."):])
        if base == "__init__":
            # 包：注册目录名（去掉最后的 .__init__）
            pkg = dotted[:-len(".__init__")] if dotted.endswith(".__init__") else dotted
            pkg_aliases = [pkg]
            if has_src and pkg.startswith("src."):
                pkg_aliases.append(pkg[len("src."):])
            for pa in pkg_aliases:
                modmap.setdefault(pa, fp)
                modmap.setdefault(pa.split(".")[-1], fp)
            pkg_dir = os.path.dirname(fp)
            dirmap[pkg] = pkg_dir
            if has_src and pkg.startswith("src."):
                dirmap[pkg[len("src."):]] = pkg_dir
        else:
            for n in names:
                modmap.setdefault(n, fp)
            modmap.setdefault(base, fp)
    # dirmap: 包名 -> 包内所有 py 文件（含子包，保守展开）
    full_dirmap = {}
    for pkg, d in dirmap.items():
        files = []
        for dp, _, fns in os.walk(d):
            for fn in fns:
                if fn.endswith(".py") and fn != "__init__.py":
                    files.append(os.path.join(dp, fn))
        full_dirmap[pkg] = files
    return modmap, full_dirmap


def resolve_module(modname, level, current_file, modmap, dirmap):
    """把 import 解析为本地文件路径列表（可能是包内多个文件）。"""
    cur_dir = os.path.dirname(current_file)
    if level > 0:
        # 相对导入：从当前文件的包往上跳 level-1 层
        # 当前文件的 dotted 包名（相对项目根）
        rel = os.path.relpath(cur_dir, project_root)
        cur_pkg = rel.replace(os.sep, ".")
        if cur_pkg == ".":
            cur_pkg = ""
        parts = cur_pkg.split(".") if cur_pkg else []
        # 向上跳 level-1 层（level=1 表示当前包）
        base_parts = parts[:max(0, len(parts) - (level - 1))]
        if modname:
            full = ".".join(base_parts + [modname])
        else:
            full = ".".join(base_parts)
    else:
        full = modname
    # 包匹配优先：展开包内所有文件（保守）—— 包可能同时注册在 modmap（作为 __init__）
    if full in dirmap:
        init = modmap.get(full)
        result = ([init] if init else []) + list(dirmap[full])
        return result
    # 精确文件匹配
    if full in modmap:
        return [modmap[full]]
    # 绝对导入但入口在子目录（如 src/）：尝试把 src/ 作为包根
    # 入口文件所在目录向上找最近的 __init__.py 链，作为候选包根
    for pkg_root in _candidate_pkg_roots(current_file):
        rel2 = os.path.relpath(cur_dir, pkg_root)
        cur_pkg2 = rel2.replace(os.sep, ".")
        if cur_pkg2 == ".":
            cur_pkg2 = ""
        parts2 = cur_pkg2.split(".") if cur_pkg2 else []
        full2 = ".".join(parts2 + [modname]) if parts2 else modname
        if full2 in modmap:
            return [modmap[full2]]
        if full2 in dirmap:
            return [modmap[full2]] + dirmap[full2]
    # 尝试包+模块：core.templates -> core/templates.py 或 core/templates/__init__.py
    if full and "." in full:
        pkg = full.rsplit(".", 1)[0]
        if pkg in dirmap:
            return [modmap.get(full, "")] + dirmap[pkg] if modmap.get(full) else dirmap[pkg]
    return []


_pkg_root_cache = {}


def _candidate_pkg_roots(start_file):
    """从入口文件出发，向上找所有含 __init__.py 的目录作为候选包根。

    例如 src/main.py -> src/ 有 __init__.py，则 src/ 是包根。
    """
    if start_file in _pkg_root_cache:
        return _pkg_root_cache[start_file]
    roots = []
    d = os.path.dirname(os.path.abspath(start_file))
    while True:
        if os.path.isfile(os.path.join(d, "__init__.py")):
            roots.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    _pkg_root_cache[start_file] = roots
    return roots


def compute_import_closure(entry_files, py_files, modmap, dirmap):
    """从入口文件 BFS 计算运行时 import 闭包。

    返回 (closure_files, used_top_imports)
    """
    closure = set()
    used_top = set()
    queue = list(entry_files)
    seen = set()
    while queue:
        fp = queue.pop(0)
        if fp in seen:
            continue
        seen.add(fp)
        closure.add(fp)
        for modname, level in get_imports(fp):
            if level == 0 and modname:
                used_top.add(modname)
            targets = resolve_module(modname, level, fp, modmap, dirmap)
            for t in targets:
                if t and t not in seen and t not in queue:
                    queue.append(t)
    return closure, used_top, seen


# ---------------------------------------------------------------------------
# 2. site-packages 对照
# ---------------------------------------------------------------------------

def scan_site_packages(sp_dir):
    """返回 {顶层包名: {size_bytes, path}}，含 .pyd / .py / 目录。"""
    pkgs = {}
    if not os.path.isdir(sp_dir):
        return pkgs
    for name in os.listdir(sp_dir):
        p = os.path.join(sp_dir, name)
        if name.startswith("_"):
            continue
        if os.path.isdir(p):
            size = sum(
                os.path.getsize(os.path.join(dp, fn))
                for dp, _, fns in os.walk(p)
                for fn in fns
                if not fn.endswith((".pyc", ".pyo"))
            )
            pkgs[name] = {"size": size, "path": p, "kind": "dir"}
        else:
            base, ext = os.path.splitext(name)
            if ext.lower() in {".pyd", ".py", ".dll"}:
                pkgs[base] = {"size": os.path.getsize(p), "path": p, "kind": "file"}
    return pkgs


# ---------------------------------------------------------------------------
# 3. PE DLL 导入表解析（纯标准库）
# ---------------------------------------------------------------------------

def _rva_to_offset(data, pe_offset, rva):
    """RVA -> 文件偏移（按节表转换）。"""
    num_sections = struct.unpack_from("<H", data, pe_offset + 6)[0]
    opt_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    opt_offset = pe_offset + 24
    sec_offset = opt_offset + opt_size
    for i in range(num_sections):
        s = sec_offset + i * 40
        vsize = struct.unpack_from("<I", data, s + 8)[0]
        vaddr = struct.unpack_from("<I", data, s + 12)[0]
        raw_size = struct.unpack_from("<I", data, s + 16)[0]
        raw_ptr = struct.unpack_from("<I", data, s + 20)[0]
        if vaddr <= rva < vaddr + max(vsize, raw_size):
            return raw_ptr + (rva - vaddr)
    return None


def parse_pe_imports(path):
    """解析 PE 导入表，返回导入的 DLL 名列表；非 PE 返回 None。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if len(data) < 0x40 or data[:2] != b"MZ":
        return None
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if pe_offset + 24 > len(data) or data[pe_offset:pe_offset + 4] != b"PE\0\0":
        return None
    opt_size = struct.unpack_from("<H", data, pe_offset + 20)[0]
    opt_offset = pe_offset + 24
    if opt_offset + opt_size > len(data):
        return None
    magic = struct.unpack_from("<H", data, opt_offset)[0]
    if magic == 0x10B:      # PE32
        dd_offset = opt_offset + 96
    elif magic == 0x20B:    # PE32+
        dd_offset = opt_offset + 112
    else:
        return None
    import_rva, _ = struct.unpack_from("<II", data, dd_offset + 1 * 8)
    if not import_rva:
        return []
    off = _rva_to_offset(data, pe_offset, import_rva)
    if off is None:
        return []
    names = []
    idx = 0
    while True:
        desc = off + idx * 20
        if desc + 20 > len(data):
            break
        name_rva = struct.unpack_from("<I", data, desc + 12)[0]
        if not name_rva:
            break
        noff = _rva_to_offset(data, pe_offset, name_rva)
        if noff is None:
            break
        end = data.find(b"\0", noff)
        if end == -1:
            break
        try:
            names.append(data[noff:end].decode("ascii"))
        except UnicodeDecodeError:
            pass
        idx += 1
    return names


def scan_dll_deps(root):
    """扫描目录树内所有 DLL/pyd，返回 {name: imports} + 文件大小。"""
    deps = {}
    sizes = {}
    total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in DLL_EXTS:
                continue
            fp = os.path.join(dirpath, fn)
            try:
                sizes[fn] = os.path.getsize(fp)
            except OSError:
                continue
            total += 1
            imports = parse_pe_imports(fp)
            if imports is not None:
                deps[fn] = [d.lower() for d in imports]
    return deps, sizes, total


# ---------------------------------------------------------------------------
# 4. 报告生成
# ---------------------------------------------------------------------------

def fmt_mb(n):
    return f"{n / 1024 / 1024:,.1f} MB"


def build_report(py_files, closure, used_top, pkgs, dll_deps, dll_sizes,
                 entry_names, engine_root, sp_dir, out_path):
    lines = []
    A = lines.append
    A(f"# QGIS-Agent 依赖扫描报告")
    A(f"")
    A(f"- 生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    A(f"- 项目根: `{project_root}`")
    A(f"- 引擎目录: `{engine_root}`")
    A(f"- 入口: {', '.join(entry_names)}")
    A(f"- 说明: 静态分析，仅作瘦身参考；删除前务必人工验证。")
    A(f"")

    # ---- 1. 源码 import 总览 ----
    A(f"## 1. 项目源码 import 总览")
    A(f"")
    A(f"- 扫描 .py 文件: {len(py_files)} 个")
    A(f"- 入口闭包内文件: {len(closure)} 个（运行时实际加载的本地模块）")
    A(f"- 顶层 import 名（闭包内）: {len(used_top)} 个")
    A(f"")
    A(f"```")
    A(f"实际使用的顶层导入: {', '.join(sorted(used_top))}")
    A(f"```")
    A(f"")

    # ---- 2. site-packages 对照 ----
    A(f"## 2. site-packages 对照（qgis-portable 内置 Python）")
    A(f"")
    A(f"路径: `{sp_dir}`")
    A(f"")
    # 判断：闭包内所有文件实际 import 的第三方包（含相对导入解析后的）
    closure_imports = set()
    for fp in closure:
        for modname, level in get_imports(fp):
            if level == 0 and modname:
                closure_imports.add(modname)
    third_party_used = sorted(t for t in closure_imports if t in pkgs)
    # .libs 是所属包的运行时 DLL 目录，不能单独删，随主包走
    libs_dirs = {k for k in pkgs if k.endswith(".libs")}
    unused_candidates = sorted(
        (k for k, v in pkgs.items()
         if k not in closure_imports and k not in libs_dirs and v["size"] > 1024 * 1024),
        key=lambda k: -pkgs[k]["size"],
    )
    used_size = sum(pkgs[t]["size"] for t in third_party_used if t in pkgs)
    A(f"**用到的第三方包（{len(third_party_used)} 个，约 {fmt_mb(used_size)}）:**")
    A(f"")
    A(f"| 包 | 大小 | 类型 |")
    A(f"|----|------|------|")
    for t in third_party_used:
        v = pkgs[t]
        A(f"| {t} | {fmt_mb(v['size'])} | {v['kind']} |")
    A(f"")
    A(f"**未用到、且 >1MB 的候选可删包（{len(unused_candidates)} 个，合计约 "
      f"{fmt_mb(sum(pkgs[k]['size'] for k in unused_candidates))}）:**")
    A(f"")
    A(f"| 包 | 大小 | 类型 | 备注 |")
    A(f"|----|------|------|------|")
    for k in unused_candidates:
        v = pkgs[k]
        note = ""
        if k in {"scipy", "numpy", "pandas", "matplotlib", "PyQt5", "shapely"}:
            note = "⚠ 可能被间接依赖，删除前需验证"
        A(f"| {k} | {fmt_mb(v['size'])} | {v['kind']} | {note} |")
    A(f"")

    # ---- 3. DLL 依赖分析 ----
    A(f"## 3. DLL 依赖分析")
    A(f"")
    A(f"- 扫描 DLL/pyd: {total_dlls} 个")
    A(f"- 成功解析导入表: {len(dll_deps)} 个")
    A(f"")
    if dll_deps:
        # 反向闭包：从核心 DLL 出发，找出必须保留的集合
        must_keep = set()
        queue = list(CORE_DLLS)
        while queue:
            name = queue.pop(0)
            if name in must_keep:
                continue
            must_keep.add(name)
            for imp in dll_deps.get(name, []):
                if imp not in must_keep:
                    queue.append(imp)
        # 孤立 DLL：存在但不在闭包内，也从未被任何 DLL 导入
        imported_by_anyone = set()
        for imps in dll_deps.values():
            imported_by_anyone.update(imps)
        orphan = sorted(
            (n for n in dll_deps if n not in must_keep and n not in imported_by_anyone
             and dll_sizes.get(n, 0) > 1024 * 1024),
            key=lambda n: -dll_sizes.get(n, 0),
        )
        # .pyd 会被 Python import 加载（不走 PE 导入表），不能当普通孤立 DLL 删
        orphan_dll = [n for n in orphan if not n.lower().endswith(".pyd")]
        orphan_pyd = [n for n in orphan if n.lower().endswith(".pyd")]
        orphan_size = sum(dll_sizes.get(n, 0) for n in orphan_dll)
        pyd_size = sum(dll_sizes.get(n, 0) for n in orphan_pyd)
        A(f"**必须保留的 DLL 闭包（从核心出发反向可达）: {len(must_keep)} 个**")
        A(f"")
        A(f"**孤立候选可删 DLL（>1MB，无人导入也不在闭包内，不含 .pyd）: "
          f"{len(orphan_dll)} 个，合计约 {fmt_mb(orphan_size)}**")
        A(f"")
        A(f"| DLL | 大小 |")
        A(f"|-----|------|")
        for n in orphan_dll[:60]:
            A(f"| {n} | {fmt_mb(dll_sizes.get(n, 0))} |")
        if len(orphan_dll) > 60:
            A(f"| ... 其余 {len(orphan_dll) - 60} 个略 | |")
        A(f"")
        A(f"**孤立 .pyd（{len(orphan_pyd)} 个，约 {fmt_mb(pyd_size)}）——会被 Python import 加载，"
          f"PE 分析无法覆盖，删除前必须逐包确认其 import 名**")
        A(f"")
        A(f"| .pyd | 大小 |")
        A(f"|------|------|")
        for n in orphan_pyd[:40]:
            A(f"| {n} | {fmt_mb(dll_sizes.get(n, 0))} |")
        if len(orphan_pyd) > 40:
            A(f"| ... 其余 {len(orphan_pyd) - 40} 个略 | |")
        A(f"")
        # 大文件 TOP（无论是否孤立，供人工判断）
        big = sorted(dll_sizes.items(), key=lambda kv: -kv[1])[:15]
        A(f"**全引擎最大 DLL TOP15（人工复核）:**")
        A(f"")
        A(f"| DLL | 大小 | 在保留闭包内? |")
        A(f"|-----|------|-------------|")
        for n, s in big:
            if n in must_keep:
                in_keep = "✅ 保留闭包内"
            elif n.lower().endswith(".pyd"):
                in_keep = "⚠ Python 绑定，需确认 import 名"
            else:
                in_keep = "❌ 候选可删"
            A(f"| {n} | {fmt_mb(s)} | {in_keep} |")
        A(f"")

    # ---- 4. 汇总建议 ----
    A(f"## 4. 瘦身汇总建议")
    A(f"")
    A(f"### 可优先删除（依据充分）")
    A(f"")
    A(f"1. **Python 包层**: 上表\"未用到的候选可删包\"（合计约 "
      f"{fmt_mb(sum(pkgs[k]['size'] for k in unused_candidates))}）")
    A(f"2. **孤立 DLL（不含 .pyd）**: 上表\"孤立候选\"（合计约 {fmt_mb(orphan_size)}）")
    A(f"")
    A(f"### 需人工验证后删除（可能有间接依赖）")
    A(f"")
    A(f"- scipy / pandas / matplotlib（闭包未 import，但 QGIS 插件或第三方可能间接用）")
    A(f"- Qt5WebEngineCore / Qt5WebKit（QGIS 面板可能延迟加载）")
    A(f"- 孤立 .pyd 清单（会被 Python import 加载，先确认对应包的 import 名再删）")
    A(f"- numpy / shapely / osgeo / PyQt5 等已用到包内的子模块（保守保留）")
    A(f"")
    A(f"### 删除流程建议")
    A(f"")
    A(f"1. 先备份: 复制整个 qgis-portable 或做 7z 压缩")
    A(f"2. 删一批 -> 跑一次 `pytest` + 启动冒烟测试")
    A(f"3. 启动测试要点: 能出主窗口、能加载图层、能跑一次分析")
    A(f"4. 用 `os.add_dll_directory` / `PYTHONPATH` 的运行时加载路径也要检查")
    A(f"")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path, len(py_files), len(closure), len(unused_candidates), orphan_size


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    global project_root
    ap = argparse.ArgumentParser(description="QGIS-Agent 引擎瘦身依赖扫描（只读）")
    ap.add_argument("--project", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="项目根目录（默认: 脚本上一级）")
    ap.add_argument("--output", default=None, help="报告输出路径（默认: <项目>/reports/dep_scan_report.md）")
    ap.add_argument("--entries", nargs="*", default=["src/main.py", "启动_静默.py"],
                    help="入口文件（默认: src/main.py 启动_静默.py）")
    args = ap.parse_args()

    project_root = os.path.abspath(args.project)
    engine_root = os.path.join(project_root, "qgis-portable")
    sp_dir = os.path.join(engine_root, "apps", "Python312", "Lib", "site-packages")
    out_path = args.output or os.path.join(project_root, "reports", "dep_scan_report.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    print(f"[1/4] 收集项目源码 .py 文件 ...")
    py_files = collect_py_files(project_root)
    print(f"      找到 {len(py_files)} 个 .py 文件")

    print(f"[2/4] 计算入口 import 闭包 ...")
    entry_files = []
    modmap, dirmap = build_local_module_map(py_files)
    for e in args.entries:
        p = os.path.join(project_root, e)
        if os.path.isfile(p):
            entry_files.append(p)
        else:
            print(f"      [warn] 入口不存在: {p}")
    if not entry_files:
        print("      [warn] 无有效入口，退化为扫描全部文件 import")
        entry_files = py_files
    closure, used_top, _ = compute_import_closure(entry_files, py_files, modmap, dirmap)

    print(f"[3/4] 扫描 site-packages ...")
    pkgs = scan_site_packages(sp_dir)
    print(f"      识别 {len(pkgs)} 个包")

    print(f"[4/4] 解析 DLL 导入表（引擎目录，只读头部）...")
    global total_dlls
    dll_deps, dll_sizes, total_dlls = scan_dll_deps(engine_root)
    print(f"      扫描 {total_dlls} 个 DLL/pyd，成功解析 {len(dll_deps)} 个")

    out, n_py, n_closure, n_unused, orphan_size = build_report(
        py_files, closure, used_top, pkgs, dll_deps, dll_sizes,
        args.entries, engine_root, sp_dir, out_path,
    )
    print(f"\n✅ 报告已生成: {out}")
    print(f"   源码文件 {n_py} 个 | 闭包内 {n_closure} 个 | 未用候选包 {n_unused} 个 | "
          f"孤立 DLL 约 {fmt_mb(orphan_size)}")
    return 0


total_dlls = 0
project_root = ""

if __name__ == "__main__":
    sys.exit(main())
