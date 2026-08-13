#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_engine.py — QGIS-Agent GIS 引擎分发下载脚本（首次运行引导）

逻辑（Solo 审批伪代码）：
  setup_engine(project_root):
    engine_dir = project_root/qgis-portable
    IF engine_dir 存在 且 qgis_core.dll + python312.dll 都在:
        RETURN "已就绪"
    version = 读取(project_root/.engine_version)
    url = releases/download/engine-{version}/qgis-engine-{version}.7z
    target = project_root/qgis-engine-{version}.7z
    PRINT "首次运行，正在下载 GIS 引擎 (~650MB) ..."
    下载(url, target, 断点续传, 重试3次, 进度条)
    IF sha256(下载文件) != .engine_version 记录值: 报错退出并提示手动下载链接
    解压(target, 到 project_root/)
    删除 target
    设置环境变量并验证
    RETURN "引擎就绪"

接入方式：
  将 setup_engine() 调用放在 启动_静默.py 的 main() 之前：
      from scripts.setup_engine import setup_engine  # 或直接 import setup_engine
      setup_engine(PROJECT_ROOT)
"""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

# 发布者在发布引擎包时，将实际 sha256 填到这里；
# 若留空则跳过校验（仅打印警告）。
ENGINE_SHA256 = "89a56d13dcf318af19bdffdefe9624dba963c39dcd4aaa3077c3003038744138"

# 默认下载源（GitHub Release，由 .engine_version 决定具体 tag）
GITHUB_REPO = "kongchenxu792-cell/QGIS-Agent"
RELEASE_BASE = f"https://github.com/{GITHUB_REPO}/releases/download/engine-{{version}}/qgis-engine-{{version}}.zip"


def _read_engine_version(project_root: Path) -> str:
    ver_file = project_root / ".engine_version"
    if not ver_file.exists():
        raise RuntimeError(
            f"缺少 {ver_file}，请先在项目根目录创建 .engine_version（内容为版本号，如 2.0.0）"
        )
    version = ver_file.read_text(encoding="utf-8").strip()
    if not version:
        raise RuntimeError(".engine_version 内容为空")
    return version


def _download(url: str, target: Path, retries: int = 3) -> None:
    """断点续传下载，带进度条，失败自动重试。"""
    # 断点续传：支持已下载部分续传（服务器需支持 Range）
    headers = {}
    if target.exists() and target.stat().st_size > 0:
        headers["Range"] = f"bytes={target.stat().st_size}-"

    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers=headers)
            resp = urllib.request.urlopen(req, timeout=30)
            total = int(resp.headers.get("Content-Length", 0))
            mode = "ab" if target.exists() and target.stat().st_size > 0 else "wb"
            done = target.stat().st_size if mode == "ab" else 0
            chunk = 1 << 20
            with open(target, mode) as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if total:
                        pct = min(done / (done + total), 1.0) if total > 0 else 0
                        bar = "#" * int(pct * 30) + "-" * (30 - int(pct * 30))
                        print(f"\r  [{bar}] {pct * 100:5.1f}%  {done / (1 << 20):.1f} MB", end="")
                    else:
                        print(f"\r  已下载 {done / (1 << 20):.1f} MB", end="")
            print()
            return
        except Exception as e:
            print(f"\n[重试 {attempt}/{retries}] 下载失败：{e}")
            if attempt == retries:
                raise
    raise RuntimeError("下载失败，已超出重试次数")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            buf = f.read(1 << 20)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _extract_7z(target: Path, dest: Path) -> None:
    """解压 .7z 到目标目录。

    优先使用引擎内 7z.exe；找不到时尝试系统 7z；均不可用时尝试
    Python zipfile（仅当文件实为 zip 容器时可用，7z 本体不支持）。
    """
    seven_zips = [
        dest / "qgis-portable" / "7z.exe",
        Path(r"C:\Program Files\7-Zip\7z.exe"),
        Path(r"C:\Program Files (x86)\7-Zip\7z.exe"),
    ]
    exe = next((p for p in seven_zips if p.exists()), None)
    if exe is not None:
        import subprocess
        proc = subprocess.run(
            [str(exe), "x", str(target), f"-o{dest}", "-y"],
            capture_output=True,
        )
        if proc.returncode == 0:
            return
        raise RuntimeError(f"7z 解压失败：{proc.stderr.decode(errors='ignore')[-500:]}")

    # 兜底：尝试按 zip 容器读取（部分场景 .7z 内部为 zip 兼容结构）
    try:
        with zipfile.ZipFile(target) as zf:
            zf.extractall(dest)
        return
    except Exception as e:
        raise RuntimeError(f"无可用 7z，且 zipfile 解压失败：{e}")


def setup_engine(project_root=None) -> str:
    """确保 GIS 引擎就绪；已就绪直接返回，否则下载并解压。"""
    root = Path(project_root) if project_root else Path(__file__).resolve().parent.parent
    engine_dir = root / "qgis-portable"
    core_dll = engine_dir / "apps" / "qgis-ltr" / "bin" / "qgis_core.dll"
    py_dll = engine_dir / "apps" / "Python312" / "python312.dll"

    if engine_dir.exists() and core_dll.exists() and py_dll.exists():
        print("[setup_engine] GIS 引擎已就绪")
        return "已就绪"

    version = _read_engine_version(root)
    url = RELEASE_BASE.format(version=version)
    target = root / f"qgis-engine-{version}.zip"

    print(f"[setup_engine] 首次运行，正在下载 GIS 引擎 (~650MB) ...")
    print(f"[setup_engine] 版本: {version}")
    print(f"[setup_engine] URL: {url}")

    if not target.exists() or target.stat().st_size == 0:
        _download(url, target)
    else:
        print("[setup_engine] 检测到已存在的安装包，继续使用")

    if ENGINE_SHA256:
        actual = _sha256(target)
        if actual.lower() != ENGINE_SHA256.lower():
            raise RuntimeError(
                f"sha256 校验失败！\n  期望: {ENGINE_SHA256}\n  实际: {actual}\n"
                f"请手动下载: {url}"
            )
        print("[setup_engine] sha256 校验通过")
    else:
        print("[setup_engine] [警告] 未配置 ENGINE_SHA256，跳过校验（发布时请填写）")

    _extract_7z(target, root)
    target.unlink(missing_ok=True)
    print("[setup_engine] 引擎解压完成，安装包已清理")

    # 环境变量验证
    if not (core_dll.exists() and py_dll.exists()):
        raise RuntimeError("解压后引擎结构不完整，缺少 qgis_core.dll / python312.dll")
    print("[setup_engine] 引擎就绪")
    return "引擎就绪"


if __name__ == "__main__":
    try:
        result = setup_engine()
        print(f"[setup_engine] 结果: {result}")
    except Exception as e:
        print(f"[setup_engine] 失败: {e}")
        sys.exit(1)
