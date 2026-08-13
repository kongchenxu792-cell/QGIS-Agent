"""
QGIS-Agent 桌面快捷方式安装脚本
创建桌面"QGIS-Agent"快捷方式，指向 pythonw.exe + 启动.bat，使用 aiqgis.ico 图标。
win32com 优先，PowerShell fallback。
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAUNCH_BAT = os.path.join(PROJECT_ROOT, "启动_静默.py")
PYTHONW = os.path.join(PROJECT_ROOT, r"qgis-portable\apps\Python312\pythonw.exe")
ICON = os.path.join(PROJECT_ROOT, "resources", "aiqgis.ico")
DESKTOP = os.path.join(os.environ["USERPROFILE"], "Desktop")
LNK_NAME = "QGIS-Agent.lnk"
LNK_PATH = os.path.join(DESKTOP, LNK_NAME)


def check_prerequisites():
    """检查所有必需文件是否存在"""
    missing = []
    for label, path in [("启动.bat", LAUNCH_BAT),
                        ("pythonw.exe", PYTHONW),
                        ("aiqgis.ico", ICON)]:
        if not os.path.exists(path):
            missing.append(f"{label}: {path}")
    if missing:
        print("[错误] 以下文件不存在：")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)
    print("[检查] 所有必需文件就绪")


def create_via_win32com():
    """使用 win32com 创建快捷方式（优先方案）"""
    try:
        from win32com.client import Dispatch
    except ImportError:
        print("[跳过] win32com 不可用")
        return False

    shell = Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(LNK_PATH)

    # 目标：pythonw.exe，参数：启动.bat 绝对路径，起始位置：项目根目录
    shortcut.Targetpath = PYTHONW
    shortcut.Arguments = f'"{LAUNCH_BAT}"'
    shortcut.WorkingDirectory = PROJECT_ROOT
    shortcut.IconLocation = ICON
    shortcut.Description = "QGIS-Agent — GIS 智能助手"
    shortcut.Save()

    print(f"[win32com] 快捷方式已创建: {LNK_PATH}")
    return True


def create_via_powershell():
    """使用 PowerShell 创建快捷方式（fallback 方案）"""
    import subprocess, tempfile

    ps_script = f'''
$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut("{LNK_PATH}")
$Shortcut.TargetPath = "{PYTHONW}"
$Shortcut.Arguments = '"{LAUNCH_BAT}"'
$Shortcut.WorkingDirectory = "{PROJECT_ROOT}"
$Shortcut.IconLocation = "{ICON}"
$Shortcut.Description = "QGIS-Agent — GIS 智能助手"
$Shortcut.Save()
"OK"
'''

    # 写临时 .ps1 文件（避免 subprocess 内联参数在嵌套线程中截断）
    tmp_path = os.path.join(tempfile.gettempdir(), "aiqgis_shortcut.ps1")
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(ps_script)

    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", tmp_path],
            capture_output=True, text=True, timeout=15
        )
        if "OK" in (result.stdout or ""):
            print(f"[PowerShell] 快捷方式已创建: {LNK_PATH}")
            return True
        else:
            print(f"[PowerShell 错误] stdout={result.stdout} stderr={result.stderr}")
            return False
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def main():
    print("=" * 50)
    print("  QGIS-Agent 桌面快捷方式安装")
    print("=" * 50)

    check_prerequisites()

    # 如果快捷方式已存在，先询问
    if os.path.exists(LNK_PATH):
        print(f"[注意] 桌面快捷方式已存在，将覆盖: {LNK_PATH}")

    success = create_via_win32com() or create_via_powershell()

    if success:
        print()
        print("安装完成。桌面上已生成「QGIS-Agent」快捷方式。")
        print(f"  图标文件: {ICON}")
    else:
        print()
        print("[失败] 无法创建快捷方式，请手动操作。")
        sys.exit(1)


if __name__ == "__main__":
    main()

