"""
AIQGIS 静默启动脚本（无控制台窗口）
用于 pythonw.exe 启动，等价于 启动.bat 的环境初始化逻辑。
"""
import os
import sys
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OSGEO4W_ROOT = os.path.join(PROJECT_ROOT, "qgis-portable")

# 首次运行引导：引擎缺失时自动下载解压
try:
    from scripts.setup_engine import setup_engine
    setup_engine(PROJECT_ROOT)
except Exception as e:
    print(f"[setup_engine] 引擎准备失败: {e}")
    sys.exit(1)

# 环境变量
os.environ["OSGEO4W_ROOT"] = OSGEO4W_ROOT
os.environ["QGIS_PREFIX_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr")
os.environ["GDAL_FILENAME_IS_UTF8"] = "YES"
os.environ["PROJ_LIB"] = os.path.join(OSGEO4W_ROOT, "share", "proj")
os.environ["GDAL_DATA"] = os.path.join(OSGEO4W_ROOT, "apps", "gdal", "share", "gdal")
os.environ["PROJ_DATA"] = os.path.join(OSGEO4W_ROOT, "share", "proj")
os.environ["VSI_CACHE"] = "TRUE"
os.environ["VSI_CACHE_SIZE"] = "1000000"
os.environ["QT_PLUGIN_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "qtplugins") + ";" + os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "plugins")
os.environ["GDAL_DRIVER_PATH"] = os.path.join(OSGEO4W_ROOT, "apps", "gdal", "lib", "gdalplugins")
os.environ["PYTHONHOME"] = os.path.join(OSGEO4W_ROOT, "apps", "Python312")
os.environ["PYTHONUTF8"] = "1"
os.environ["SSL_CERT_FILE"] = os.path.join(OSGEO4W_ROOT, "bin", "curl-ca-bundle.crt")
os.environ["SSL_CERT_DIR"] = os.path.join(OSGEO4W_ROOT, "apps", "openssl", "certs")

# PATH
path_parts = [
    os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "bin"),
    os.path.join(OSGEO4W_ROOT, "bin"),
    os.path.join(OSGEO4W_ROOT, "apps", "Python312"),
    os.path.join(OSGEO4W_ROOT, "apps", "Python312", "Scripts"),
    os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "bin"),
    r"C:\WINDOWS\system32",
    r"C:\WINDOWS",
    r"C:\WINDOWS\system32\WBem",
]
os.environ["PATH"] = ";".join(path_parts)

# PYTHONPATH
os.environ["PYTHONPATH"] = os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "python") + ";" + os.path.join(OSGEO4W_ROOT, "apps", "Python312", "Lib", "site-packages")

# === SAC 绕过：锁定 DLL 搜索路径到便携版，避免 Windows Smart App Control 拦截未签名 DLL ===
_dll_dirs = [
    os.path.join(OSGEO4W_ROOT, "apps", "qgis-ltr", "bin"),
    os.path.join(OSGEO4W_ROOT, "bin"),
    os.path.join(OSGEO4W_ROOT, "apps", "Qt5", "bin"),
]
for _d in _dll_dirs:
    if hasattr(os, "add_dll_directory"):
        os.add_dll_directory(_d)

# 切换到项目根目录
os.chdir(PROJECT_ROOT)

# 启动主程序
main_script = os.path.join(PROJECT_ROOT, "src", "main.py")
sys.path.insert(0, os.path.dirname(main_script))
sys.argv = [main_script]
with open(main_script, encoding="utf-8") as f:
    code = compile(f.read(), main_script, "exec")
    exec(code, {"__name__": "__main__", "__file__": main_script})
