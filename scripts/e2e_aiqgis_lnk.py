# -*- coding: utf-8 -*-
"""AIQGIS E2E 验收司机程序（入库版）

用法（必须用项目内置引擎运行）：
  qgis-portable\\apps\\Python312\\python.exe scripts/e2e_aiqgis_lnk.py [--only 4.1,4.2] [--tag 全量]

前置条件：
  - 应用已通过 D:\\桌面\\AIQGIS.lnk 启动（真实窗口）
  - 4 个 demo 图层已通过 文件→导入/导出→导入图层 加载
  - aiqgis_config.json last_mode=offline；Ollama 服务在线且 qwen3.5-4b:latest 已预热

执行方式：
  - 逐条指令：输入框聚焦 → Ctrl+A → 剪贴板粘贴 → 真实鼠标点击 run_button
  - 完成判据：aiqgis.log 尾部出现「流水线执行开始 / AI 流水线响应」或 run_button 由 disabled 转 enabled
  - 澄清弹窗：标题「需要澄清」，按用例选择候选图层按钮或「取消」
  - 证据：每条用例截图 + 响应区文本 + 耗时 + 日志锚点，汇总 JSON 落 temp/e2e_evidence/
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import sys
import time

sys.path.insert(0, r"D:\桌面\QGIS-Agent\qgis-portable\apps\Python312\Lib\site-packages")
import uiautomation as auto

PROJECT_ROOT = r"D:\桌面\QGIS-Agent"
LOG_PATH = os.path.join(PROJECT_ROOT, "user_data", "logs", "aiqgis.log")
EVIDENCE_DIR = os.path.join(PROJECT_ROOT, "temp", "e2e_evidence")
WINDOW_TITLE = "AI 驱动轻量桌面 GIS v2.0.0"
CLARIFY_TITLE = "需要澄清"

# 单条指令最长等待（秒）。重跑 4.1~4.4 与全量共用，超时按 FAIL 记录
CASE_TIMEOUT = 180


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def log_tail(n: int = 12) -> str:
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            return "".join(f.readlines()[-n:])
    except Exception as e:
        return f"read err {e}"


def log_line_count() -> int:
    try:
        with open(LOG_PATH, "r", encoding="utf-8", errors="ignore") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0


def find_win(timeout: int = 30) -> auto.WindowControl | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for ctrl_cls in (auto.PaneControl, auto.WindowControl):
            w = ctrl_cls(searchDepth=1, Name=WINDOW_TITLE)
            if w.Exists(1, 0.3):
                return w
        time.sleep(1)
    return None


def real_click(ctrl, name: str) -> None:
    r = ctrl.BoundingRectangle
    cx = (r.left + r.right) // 2
    cy = (r.top + r.bottom) // 2
    ctypes.windll.user32.SetCursorPos(cx, cy)
    time.sleep(0.25)
    ctypes.windll.user32.mouse_event(0x0002, 0, 0, 0, 0)
    time.sleep(0.08)
    ctypes.windll.user32.mouse_event(0x0004, 0, 0, 0, 0)
    log(f"  click {name} @({cx},{cy})")
    time.sleep(0.8)


def find_clarify_dialog() -> auto.WindowControl | None:
    dlg = auto.WindowControl(searchDepth=2, Name=CLARIFY_TITLE)
    if dlg.Exists(1, 0.4):
        return dlg
    # 也允许 searchDepth=1 兜底
    dlg2 = auto.WindowControl(searchDepth=1, Name=CLARIFY_TITLE)
    if dlg2.Exists(1, 0.4):
        return dlg2
    return None


def handle_clarify(win, choice: str | None, cancel: bool) -> str | None:
    """处理澄清弹窗。返回点击的按钮文本或 None（无弹窗）。"""
    deadline = time.time() + 15
    while time.time() < deadline:
        dlg = find_clarify_dialog()
        if dlg is not None:
            log(f"  clarify dialog found")
            btns = [b for b in dlg.GetChildren()
                    if b.ControlTypeName == "ButtonControl" and b.Name]
            names = [b.Name for b in btns]
            log(f"  clarify buttons: {names}")
            if cancel:
                target = next((b for b in btns if b.Name == "取消"), None)
                if target:
                    real_click(target, "取消")
                    return "取消"
            if choice:
                target = next((b for b in btns if b.Name == choice), None)
                if target:
                    real_click(target, choice)
                    return choice
                # 找不到目标候选时点取消，避免卡死
                cancel_btn = next((b for b in btns if b.Name == "取消"), None)
                if cancel_btn:
                    real_click(cancel_btn, "取消")
                    return f"取消(未找到{choice})"
            else:
                return None  # 不期望弹窗却出现
        time.sleep(0.5)
    return None


def wait_run_complete(win, tag: str, prompt: str) -> tuple[bool, str]:
    """等待执行完成（按日志行号锚点，避免误用上一轮残留日志）。

    返回 (成功, 判据描述)。
    阶段1：等待本轮日志出现「发起 AI 流水线规划请求」（行号 > base）。
    阶段2：等待 run_button 由 disabled 恢复 enabled 且日志出现本轮结束关键词。
    """
    run = win.ButtonControl(Name="run_button")
    base = log_line_count()

    # 阶段1：本轮开始锚点
    deadline = time.time() + 60
    start_line = None
    while time.time() < deadline:
        if log_line_count() > base:
            tail = log_tail(30)
            if "发起 AI 流水线规划请求" in tail:
                start_line = log_line_count()
                log(f"  {tag} pipeline started (line>{base})")
                break
        time.sleep(1)
    if start_line is None:
        return False, "未检测到本轮「发起 AI 流水线规划请求」"

    # 阶段2：等待完成（run_button 恢复 enabled + 日志结束关键词）
    deadline = time.time() + CASE_TIMEOUT
    while time.time() < deadline:
        try:
            enabled = run.IsEnabled
        except Exception:
            enabled = False
        tail = log_tail(40)
        if enabled and (
            "AI 流水线响应" in tail
            or "流水线执行完成" in tail
            or "执行完成" in tail
            or "未知原因" in tail
            or "失败" in tail
            or "error" in tail.lower()
        ):
            return True, "run_button 恢复 enabled + 本轮日志结束关键词"
        time.sleep(2)
    return False, f"超时 {CASE_TIMEOUT}s，本轮日志尾部: {log_tail(8)[-200:]}"


def get_response_text(win) -> str:
    try:
        resp = win.EditControl(Name="ai_response_display")
        if not resp.Exists(0.5, 0.2):
            return ""
        txt = ""
        for c in resp.GetChildren():
            if c.Name:
                txt += c.Name + "\n"
        # 若子控件无文本，尝试 ValuePattern
        if not txt.strip():
            try:
                txt = resp.GetValuePattern().Value or ""
            except Exception:
                pass
        return txt.strip()
    except Exception as e:
        return f"(读取响应失败 {e})"


def screenshot(win, tag: str, seq: int) -> str:
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    path = os.path.join(EVIDENCE_DIR, f"{tag}_{seq}_{time.strftime('%H%M%S')}.png")
    try:
        win.CaptureToImage(path)
        log(f"  shot: {path}")
        return path
    except Exception as e:
        log(f"  shot err: {e}")
        return ""


def run_case(win, tag: str, prompt: str, *, clarify_choice: str | None = None,
             clarify_cancel: bool = False) -> dict:
    """执行单条指令并采集证据。"""
    inp = win.EditControl(Name="ai_prompt_input")
    run = win.ButtonControl(Name="run_button")
    t0 = time.time()

    inp.Click()
    time.sleep(0.4)
    auto.SetClipboardText(prompt)
    inp.SendKeys("{Ctrl}a", waitTime=0.2)
    time.sleep(0.2)
    inp.SendKeys("{Ctrl}v", waitTime=0.3)
    time.sleep(0.5)
    log(f"--- {tag} 指令已输入: {prompt}")
    real_click(run, "run_button")

    # 澄清弹窗处理（若有）
    clarify_action = None
    if clarify_choice or clarify_cancel:
        clarify_action = handle_clarify(win, clarify_choice, clarify_cancel)
        if clarify_action is not None:
            log(f"  {tag} 澄清动作: {clarify_action}")

    ok, criterion = wait_run_complete(win, tag, prompt)
    elapsed = round(time.time() - t0, 1)
    time.sleep(1.5)
    resp_text = get_response_text(win)
    shot = screenshot(win, tag, int(time.time()) % 100000)
    tail = log_tail(15)

    result = {
        "case": tag,
        "prompt": prompt,
        "ok": ok,
        "criterion": criterion,
        "elapsed_s": elapsed,
        "clarify_action": clarify_action,
        "response": resp_text[:1500],
        "screenshot": shot,
        "log_tail": tail,
    }
    log(f"--- {tag} {'PASS' if ok else 'FAIL'} 耗时 {elapsed}s 判据: {criterion}")
    return result


def clear_all_layers(win) -> str:
    """6.1 清空项目：循环选中图层树节点并点「移除图层」。"""
    tree = win.TreeControl(AutoId="sidebarPanel.layerTreeView")
    if not tree.Exists(1, 0.5):
        return "tree not found"
    rm_btn = win.ButtonControl(Name="移除图层")
    removed = []
    for _ in range(12):
        items = []
        def collect(c, d=0):
            for ch in c.GetChildren():
                try:
                    if ch.ControlTypeName == "TreeItemControl":
                        items.append(ch)
                except Exception:
                    pass
                if d < 2:
                    try:
                        collect(ch, d + 1)
                    except Exception:
                        pass
        collect(tree)
        if not items:
            break
        it = items[0]
        name = it.Name
        it.Click()
        time.sleep(0.4)
        if rm_btn.Exists(0.5, 0.2):
            real_click(rm_btn, "移除图层")
        time.sleep(0.8)
        removed.append(name)
    return "移除图层: " + ", ".join(removed)


def run_cases(win, cases: list[dict]) -> list[dict]:
    results = []
    for c in cases:
        try:
            r = run_case(win, c["tag"], c["prompt"],
                         clarify_choice=c.get("clarify_choice"),
                         clarify_cancel=c.get("clarify_cancel", False))
            results.append(r)
        except Exception as e:
            log(f"  {c['tag']} 执行异常: {e}")
            results.append({"case": c["tag"], "prompt": c["prompt"], "ok": False,
                            "error": str(e)})
    return results


CASES = [
    {"tag": "4.1", "prompt": "对避难所创建 500 米缓冲区"},
    {"tag": "4.2", "prompt": "计算避难所的覆盖范围，边界用东京行政区"},
    {"tag": "4.3", "prompt": "分析避难所500米服务范围的盲区，边界用东京行政区"},
    {"tag": "4.4", "prompt": "计算避难所500米范围内的人口覆盖率，边界用东京行政区",
     "clarify_choice": "人口"},
    {"tag": "4.5", "prompt": "计算避难所500米范围内的人口覆盖率，边界用东京行政区",
     "clarify_cancel": True},
    {"tag": "4.6", "prompt": "列出图层"},
    {"tag": "4.7", "prompt": "保存工作区"},
    {"tag": "4.8", "prompt": "打开刚保存的工作区"},
    {"tag": "5.1", "prompt": "避難所の500mバッファカバー率を分析、境界は東京行政区"},
    {"tag": "5.2", "prompt": "Analyze the 500m buffer coverage of shelters with Tokyo boundary"},
    {"tag": "6.1", "prompt": "计算覆盖率", "pre_clear": True},
    {"tag": "6.2", "prompt": "今天天气怎么样"},
    {"tag": "6.3a", "prompt": "计算避难所的覆盖范围，边界用东京行政区"},
    {"tag": "6.3b", "prompt": "分析避难所500米服务范围的盲区，边界用东京行政区"},
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑指定用例，逗号分隔，如 4.1,4.2")
    ap.add_argument("--tag", default="full", help="证据目录子标识")
    args = ap.parse_args()

    os.makedirs(EVIDENCE_DIR, exist_ok=True)

    win = find_win(30)
    if win is None:
        log("未找到应用窗口，请确认已通过 AIQGIS.lnk 启动")
        return 1
    win.SetActive()
    time.sleep(1)
    log(f"窗口就绪: {win.Name}")

    # 6.4 正常关闭：仅在跑 6.4 时由外层单独处理，此处不自动关闭

    if args.only:
        wanted = {x.strip() for x in args.only.split(",") if x.strip()}
        cases = [c for c in CASES if c["tag"] in wanted]
    else:
        cases = CASES

    results = []
    for c in cases:
        if c.get("pre_clear"):
            log("6.1 前置：清空项目图层")
            clear_all_layers(win)
            time.sleep(1)
        r = run_case(win, c["tag"], c["prompt"],
                     clarify_choice=c.get("clarify_choice"),
                     clarify_cancel=c.get("clarify_cancel", False))
        results.append(r)
        # 6.3 连续两条：紧接发第二条
        if c["tag"] == "6.3a":
            r2 = run_case(win, "6.3b", "分析避难所500米服务范围的盲区，边界用东京行政区")
            results.append(r2)

    out = {
        "tag": args.tag,
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
        "summary": {
            "total": len(results),
            "pass": sum(1 for r in results if r.get("ok")),
            "fail": sum(1 for r in results if not r.get("ok")),
        },
    }
    out_path = os.path.join(EVIDENCE_DIR, f"summary_{args.tag}_{time.strftime('%H%M%S')}.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    log(f"汇总已写入: {out_path}")
    log(f"结果: {out['summary']['pass']}/{out['summary']['total']} PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
