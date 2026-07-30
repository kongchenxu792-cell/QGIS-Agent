"""AI 驱动轻量桌面 GIS 应用 - 程序入口。

负责初始化 PyQGIS 环境、创建 PyQt5 应用程序和主窗口，并启动事件循环。
"""

from __future__ import annotations

import logging
import os
import sys
import traceback

# ── 将项目根目录注入 sys.path，确保 'from src.xxx' 绝对导入可行 ──
# 约束：realpath 归一化、幂等只插一次、不覆盖 QGIS 自带路径
_project_root = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ── PROJ 环境硬编码激活：基于 QGIS portable 标准布局，不再动态巡检 ──
# 必须在任何 GIS/GDAL 导入前设置 PROJ_LIB/PROJ_DATA，否则 GDAL 初始化会缓存空路径。
_app_path = os.path.join(_project_root, "qgis-portable")
_proj_path = os.path.join(_app_path, "share", "proj")
os.environ.setdefault("PROJ_LIB", _proj_path)      # 旧版 PROJ (<9.x)
os.environ.setdefault("PROJ_DATA", _proj_path)     # 新版 PROJ (9.x+)

from core.logger import init_logging
from core.qgis_env import bootstrap_qgis, initialize_processing, shutdown_qgis, _to_short_path

init_logging()
_log = logging.getLogger("main")

# ── API Key 明文存储检查 ──
from core.config_manager import config_manager
_api_key = config_manager.api_key
_placeholder_keys = {"your-api-key-here", "sk-placeholder", "YOUR_API_KEY_HERE", ""}
if _api_key and _api_key not in _placeholder_keys:
    _log.warning("⚠️ API Key 以明文存储于 aiqgis_config.json，请注意安全")

def run() -> int:
    """启动 GUI 应用程序并返回进程退出码。"""

    _log.info("AIQGIS 正在启动...")

    qgis_prefix_path = os.environ.get("QGIS_PREFIX_PATH")
    bootstrap_result = bootstrap_qgis(qgis_prefix_path)
    qgs_app = None

    try:
        if not bootstrap_result.available:
            raise RuntimeError(bootstrap_result.message)

        _log.info("PyQGIS 环境初始化成功：%s", bootstrap_result.prefix_path)

        from PyQt5.QtCore import Qt
        from PyQt5.QtWidgets import QMessageBox
        from qgis.core import QgsApplication
        from ui.main_window import MainWindow

        QgsApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
        QgsApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
        qgs_app = QgsApplication([], True)
        qgs_app.setApplicationName("AI 驱动轻量桌面 GIS")
        qgs_app.setOrganizationName("AI_QGIS_APP")

        # ── 强制修复便携版环境：QGIS 官方接口替代动态巡检 ──
        def _force_fix_portable_env():
            app_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "qgis-portable",
            )
            QgsApplication.setPrefixPath(app_path, True)
            os.environ["PROJ_LIB"] = os.path.join(app_path, "share", "proj")
            os.environ["PROJ_DATA"] = os.path.join(app_path, "share", "proj")

        _force_fix_portable_env()
        qgs_app.initQgis()
        # initQgis() 内部会基于 prefix_path 重新推算并覆盖 PROJ 路径，立即修复
        _force_fix_portable_env()

        initialize_processing(qgs_app)

        main_window = MainWindow(bootstrap_result)
        main_window.show()
        return qgs_app.exec()
    except Exception as exc:
        _log.critical("应用启动失败", exc_info=True)
        error_details = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )
        try:
            from PyQt5.QtWidgets import QApplication, QMessageBox

            app = QApplication.instance() or QApplication([])
            QMessageBox.critical(
                None,
                "应用启动失败",
                f"{exc}\n\n{error_details}",
            )
            if QApplication.instance() is not None and app is QApplication.instance():
                app.quit()
        except Exception:
            print("应用启动失败：")
            print(error_details)
        return 1
    finally:
        if qgs_app is not None:
            # 抑制 exitQgis() 阶段的 PROJ: Cannot find proj.db 无害告警
            _stderr_fd = sys.stderr.fileno()
            _saved_stderr = os.dup(_stderr_fd)
            try:
                os.dup2(os.open(os.devnull, os.O_WRONLY), _stderr_fd)
                qgs_app.exitQgis()
            finally:
                os.dup2(_saved_stderr, _stderr_fd)
                os.close(_saved_stderr)
        shutdown_qgis()


if __name__ == "__main__":
    sys.exit(run())