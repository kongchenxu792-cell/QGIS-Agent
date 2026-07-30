"""轻量桌面 GIS 应用程序的主窗口实现。

提供完整的桌面 GIS 界面，包含：
- 左侧：QGIS 原生图层树面板
- 中央：支持拖放加载的 QGIS 地图画布
- 底部：AI 自然语言处理控制台
- 顶部：地图导航工具栏
"""

from __future__ import annotations

import builtins
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Any, Dict, Iterable, List, Optional

from PyQt5.QtCore import QEventLoop, QPoint, QRect, Qt, pyqtSignal, QEvent, QThread, QTimer
from PyQt5.QtGui import QColor, QDragEnterEvent, QDropEvent, QKeySequence
from PyQt5.QtWidgets import (
    QAction,
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMenuBar,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QShortcut,
    QSplitter,
    QStatusBar,
    QTextEdit,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsLayerTreeModel,
    QgsMapLayer,
    QgsMapRendererCustomPainterJob,
    QgsMapSettings,
    QgsPointXY,
    QgsProject,
    QgsRasterFileWriter,
    QgsRasterLayer,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.gui import (
    QgsLayerTreeMapCanvasBridge,
    QgsLayerTreeView,
    QgsLayerTreeViewMenuProvider,
    QgsMapCanvas,
    QgsMapToolPan,
    QgsMapToolZoom,
)




from src import __version__ as _app_version

from core.ai_worker import (
    AIProcessingWorker,
    append_to_history,
    parse_agent_response,
    parse_pipeline_response,
    persist_conversation_turn,
    request_code_fix,
    request_spatial_code,
)
from core.config_manager import ConfigManager
from core.instruction_mapper import InstructionMapper
from core.layer_loader import create_layer_from_path, is_supported_path, is_table_path, load_layers_from_paths
from core.multimodal.canvas_capture import CanvasCapture
from core.output_persistence import generate_output_path, generate_geojson_output_path
from core.project_manager import ProjectManager
from core.qgis_env import QgisBootstrapResult
from core.sandbox_worker import SandboxExecutionWorker
from skills.style_manager import style_manager
from ui.api_config_dialog import ApiConfigDialog
from ui.ai_code_preview import AiCodePreviewDialog
from ui.attribute_table import AttributeTableDialog
from skills.spatial_analysis_skill import SpatialAnalysisSkill
from skills.skill_manager import get_skill_manager
from i18n import lang_manager

_log = logging.getLogger("main_window")


class DroppableMapCanvas(QgsMapCanvas):
    """支持本地文件拖放加载的 QGIS 地图画布。

    方案：在 QApplication 级别安装事件过滤器，在事件分发的最前端
    拦截 DragEnter / DragMove / Drop 事件，直接判断是否拖到画布上，
    绕过所有 QGIS / QGraphicsView / viewport 的内部拦截。
    """

    filesDropped = pyqtSignal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

        # 注册到 QApplication 级别 — 在事件分发链最前端拦截
        from PyQt5.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

    @staticmethod
    def _extract_file_paths(mime_data) -> List[str]:
        """从 MIME 数据中提取本地文件路径（兼容标准 URL 和 Windows FileNameW）。"""
        if mime_data is None:
            return []
        # 方式 1：标准 text/uri-list（跨平台）
        urls = mime_data.urls()
        paths = _extract_supported_paths(urls)
        if paths:
            return paths
        # 方式 2：Windows FileNameW / FileName（CF_HDROP 格式）
        for wm_format in (
            "application/x-qt-windows-mime;value=\"FileNameW\"",
            "application/x-qt-windows-mime;value=\"FileName\"",
            "text/uri-list",
        ):
            if mime_data.hasFormat(wm_format):
                data = mime_data.data(wm_format)
                if data:
                    # FileNameW 是 UTF-16LE 编码的 null 分隔路径列表
                    try:
                        raw = bytes(data).decode("utf-16-le").rstrip("\x00")
                        raw_paths = raw.split("\x00")
                    except (UnicodeDecodeError, LookupError):
                        try:
                            raw_paths = [bytes(data).decode("utf-8").rstrip("\x00")]
                        except (UnicodeDecodeError, LookupError):
                            raw_paths = []
                    for p in raw_paths:
                        p = p.strip().replace("/", "\\")
                        if p and is_supported_path(p):
                            paths.append(p)
                if paths:
                    return paths
        return paths

    def eventFilter(self, obj, event):
        """应用级事件过滤器：在事件到达任何 QGIS 内部组件之前拦截拖拽。"""
        if event.type() in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            drag_event = event
            from PyQt5.QtGui import QCursor
            global_pos = QCursor.pos()
            canvas_origin = self.mapToGlobal(QPoint(0, 0))
            canvas_size = self.size()
            canvas_rect_global = QRect(canvas_origin, canvas_size)
            _log.debug(
                "Drag type=%s | origin=%s size=%s pos=%s contains=%s | formats=%s",
                event.type(), canvas_origin, (canvas_size.width(), canvas_size.height()),
                global_pos, canvas_rect_global.contains(global_pos),
                drag_event.mimeData().formats() if drag_event.mimeData() else [],
            )
            if canvas_rect_global.contains(global_pos):
                if event.type() == QEvent.DragEnter:
                    paths = self._extract_file_paths(drag_event.mimeData())
                    if paths:
                        drag_event.acceptProposedAction()
                        return True
                elif event.type() == QEvent.DragMove:
                    paths = self._extract_file_paths(drag_event.mimeData())
                    if paths:
                        drag_event.acceptProposedAction()
                        return True
                elif event.type() == QEvent.Drop:
                    paths = self._extract_file_paths(drag_event.mimeData())
                    if paths:
                        drag_event.acceptProposedAction()
                        self.filesDropped.emit(paths)
                        return True

        return super().eventFilter(obj, event)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        """接受包含支持 GIS 文件的拖放事件。"""
        paths = self._extract_file_paths(event.mimeData())
        _log.debug("dragEnterEvent on canvas | formats=%s paths=%d",
                   event.mimeData().formats(), len(paths))
        if paths:
            event.acceptProposedAction()
            return
        event.ignore()

    def dropEvent(self, event: QDropEvent) -> None:
        """发射拖放文件路径信号。"""
        file_paths = self._extract_file_paths(event.mimeData())
        _log.debug("dropEvent on canvas | formats=%s paths=%d",
                   event.mimeData().formats(), len(file_paths))
        if file_paths:
            event.acceptProposedAction()
            self.filesDropped.emit(file_paths)
            return
        event.ignore()


def _extract_supported_paths(urls: Iterable) -> List[str]:
    """从拖放 URL 中提取支持的本地文件路径。

    参数
    ----
    urls : Iterable
        QMimeData 中的 URL 对象列表。

    返回
    ----
    List[str]
        去重后的支持文件路径列表。
    """

    file_paths = []
    for url in urls:
        if not url.isLocalFile():
            continue

        local_file = url.toLocalFile()
        if is_supported_path(local_file):
            file_paths.append(local_file)

    return file_paths


class LayerTreeMenuProvider(QgsLayerTreeViewMenuProvider):
    """QGIS 官方右键菜单接口，通过 setMenuProvider() 注册，不与 C++ 事件冲突。"""

    def __init__(
        self,
        view: QgsLayerTreeView,
        on_attribute_table=None,
        on_zoom=None,
        on_remove=None,
        on_rename=None,
        on_copy=None,
        on_toggle_edit=None,        # P0
        on_layer_style=None,        # P1
        on_labels=None,             # P2
        on_filter=None,             # P1
        on_field_manager=None,      # P2
        on_export_attribute=None,   # P1
        on_statistic=None,          # P2
    ) -> None:
        super().__init__()
        self._view = view
        self._on_attribute_table = on_attribute_table
        self._on_zoom = on_zoom
        self._on_remove = on_remove
        self._on_rename = on_rename
        self._on_copy = on_copy
        self._on_toggle_edit = on_toggle_edit
        self._on_layer_style = on_layer_style
        self._on_labels = on_labels
        self._on_filter = on_filter
        self._on_field_manager = on_field_manager
        self._on_export_attribute = on_export_attribute
        self._on_statistic = on_statistic

    def createContextMenu(self) -> QMenu | None:
        # QGIS 先设置 currentIndex 再调用此方法
        node = self._view.currentNode()
        if node is None:
            node = self._view.layerTreeModel().index2node(self._view.currentIndex())
        if not isinstance(node, QgsLayerTreeLayer):
            return None
        layer = node.layer()
        if layer is None:
            return None

        menu = QMenu()

        if layer.type() == QgsMapLayer.VectorLayer:
            attr_action = menu.addAction("查看属性表")
            attr_action.triggered.connect(lambda: self._on_attribute_table(layer))

        zoom_action = menu.addAction("缩放到图层")
        zoom_action.triggered.connect(lambda: self._on_zoom(layer))

        menu.addSeparator()

        # P0: 开启/关闭编辑（仅矢量图层）
        if layer.type() == QgsMapLayer.VectorLayer:
            from qgis.core import QgsVectorLayer
            if isinstance(layer, QgsVectorLayer):
                edit_action = menu.addAction("开启/关闭编辑")
                edit_action.setCheckable(True)
                edit_action.setChecked(layer.isEditable())
                edit_action.triggered.connect(lambda: self._on_toggle_edit(layer))

        # P1: 图层样式设置（矢量和栅格）
        if callable(self._on_layer_style):
            style_action = menu.addAction("图层样式设置")
            style_action.triggered.connect(lambda: self._on_layer_style(layer))

        # P2: 显示/隐藏标注（仅矢量图层）
        if layer.type() == QgsMapLayer.VectorLayer and callable(self._on_labels):
            labels_action = menu.addAction("显示/隐藏标注")
            labels_action.setCheckable(True)
            labels_action.setChecked(layer.labelsEnabled())
            labels_action.triggered.connect(lambda: self._on_labels(layer))

        # P1: 设置属性过滤（仅矢量图层）
        if layer.type() == QgsMapLayer.VectorLayer and callable(self._on_filter):
            filter_action = menu.addAction("设置属性过滤")
            filter_action.triggered.connect(lambda: self._on_filter(layer))

        menu.addSeparator()

        # P2: 字段管理（仅矢量图层）
        if layer.type() == QgsMapLayer.VectorLayer and callable(self._on_field_manager):
            fm_action = menu.addAction("字段管理")
            fm_action.triggered.connect(lambda: self._on_field_manager(layer))

        # P1: 导出属性表（仅矢量图层）
        if layer.type() == QgsMapLayer.VectorLayer and callable(self._on_export_attribute):
            export_action = menu.addAction("导出属性表")
            export_action.triggered.connect(lambda: self._on_export_attribute(layer))

        # P2: 要素统计（仅矢量图层）
        if layer.type() == QgsMapLayer.VectorLayer and callable(self._on_statistic):
            stat_action = menu.addAction("要素统计")
            stat_action.triggered.connect(lambda: self._on_statistic(layer))

        menu.addSeparator()

        rename_action = menu.addAction("重命名图层")
        rename_action.triggered.connect(lambda: self._on_rename_triggered(layer))

        copy_action = menu.addAction("复制图层")
        copy_action.triggered.connect(lambda: self._on_copy(layer))

        menu.addSeparator()

        remove_action = menu.addAction("移除图层")
        remove_action.triggered.connect(lambda: self._on_remove(layer))

        # QGIS 负责调用 menu.exec()
        return menu

    def _on_rename_triggered(self, layer) -> None:
        new_name, ok = QInputDialog.getText(
            self._view, "重命名图层", "新名称：", text=layer.name(),
        )
        if ok and new_name.strip() and self._on_rename:
            self._on_rename(layer, new_name.strip())


class MainWindow(QMainWindow):
    """桌面 GIS MVP 应用程序的主窗口类。

    布局
    ----
    - 左侧：QGIS 原生图层树面板
    - 中央：QGIS 地图画布（支持拖放加载）
    - 底部：AI 自然语言处理控制台
    - 顶部：地图导航工具栏
    """

    def __init__(self, bootstrap_result: QgisBootstrapResult) -> None:
        """初始化主窗口及其子控件。"""

        super().__init__()
        self.bootstrap_result = bootstrap_result
        self.layer_tree_view = None
        self.layer_tree_model = None
        self.map_canvas = None
        self.canvas_bridge = None
        self.pan_tool = None
        self.zoom_in_tool = None
        self.zoom_out_tool = None
        self.ai_worker = None
        self.last_ai_code = ""
        self.skip_preview = False  # 是否跳过代码预览

        # 沙箱 Worker 状态（Pain 2 自愈循环）
        self._sandbox_worker: Optional[SandboxExecutionWorker] = None
        self._sandbox_retry_count = 0
        self._sandbox_original_code = ""
        self._sandbox_user_text = ""
        self._sandbox_force_numpy_gdal = False  # 追踪是否已强制降级到 numpy/gdal

        # 任务编排器状态（复合任务串行传送带）
        self._task_pipeline: List[dict] = []
        self._task_pipeline_index: int = 0
        self._task_pipeline_user_text: str = ""
        self._task_pipeline_layer_metadata: list = []
        self._task_pipeline_outputs: Dict[int, str] = {}  # step编号 → 真实图层名

        # Phase 4：多模态开关（环境变量 AIQGIS_DISABLE_MULTIMODAL 可降级回滚）
        self.multimodal_enabled = not (os.environ.get("AIQGIS_DISABLE_MULTIMODAL", "") == "1")
        self._pending_multimodal_data = None  # 截图分析时暂存 viewport_snapshot

        # P1 改造：在线/离线全局模式
        self.config = ConfigManager()
        self._mapper = InstructionMapper()  # Phase 5 Block 1: 统一匹配执行入口
        # 启动时从 JSON 持久化配置回填 ai_config（文件路径匹配正则异常时的兜底）
        import core.ai_config as ai_config
        if not ai_config.API_KEY and self.config.api_key:
            ai_config.API_KEY = self.config.api_key
        if not ai_config.BASE_URL or ai_config.BASE_URL == "https://dashscope.aliyuncs.com/compatible-mode/v1":
            if self.config.base_url:
                ai_config.BASE_URL = self.config.base_url
        if not ai_config.MODEL_NAME or ai_config.MODEL_NAME == "qwen-plus":
            if self.config.model_name:
                ai_config.MODEL_NAME = self.config.model_name
        self.offline_mode = (self.config.last_mode == "offline")
        self.offline_mode_label: Optional[QLabel] = None
        self._offline_buttons: List[QPushButton] = []

        # i18n 语言管理器（必须在 UI 组件之前初始化）
        self._lm = lang_manager()

        self.ai_prompt_input = QTextEdit(self)
        self.run_button = QPushButton(self._lm.tr("btn_run_ai"), self)
        self.screenshot_button = QPushButton(self._lm.tr("btn_screenshot"), self)
        self.ai_response_display = QTextEdit(self)
        self.ai_response_display.setReadOnly(True)

        # 项目管理器
        self.project_manager = ProjectManager()

        self._lm.language_changed.connect(self._apply_language)
        self.setWindowTitle(f"{self._lm.tr('window_title')} v{_app_version}")
        self.resize(1440, 900)
        self._build_ui()
        self._apply_styles()

        # P2：进度条（离线流程时显示在状态栏）
        self._offline_progress_bar = QProgressBar()
        self._offline_progress_bar.setMaximumWidth(200)
        self._offline_progress_bar.setMaximumHeight(16)
        self._offline_progress_bar.setRange(0, 0)  # 不确定模式
        self._offline_progress_bar.setVisible(False)
        self._offline_progress_bar.setToolTip("离线快捷流程执行中...")
        self.statusBar().addPermanentWidget(self._offline_progress_bar)

        # 文件菜单新增项引用（供 _refresh i18n 使用）
        self._file_menu: QMenu | None = None
        self._save_action: QAction | None = None
        self._save_as_action: QAction | None = None
        self._export_map_action: QAction | None = None
        self._export_layer_action: QAction | None = None
        self._import_layer_action: QAction | None = None

        self.statusBar().showMessage(self._lm.tr("status_ready"), 5000)

    def _build_ui(self) -> None:
        """构建完整的窗口布局。"""

        self._build_menubar()
        self._build_toolbar()

        central_widget = QWidget(self)
        root_layout = QVBoxLayout(central_widget)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.setSpacing(12)

        # ── P1 改造：在线/离线模式切换栏 ──
        root_layout.addWidget(self._build_mode_toggle())

        splitter = QSplitter(Qt.Horizontal, central_widget)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_canvas_panel())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([300, 1100])

        root_layout.addWidget(splitter, 1)
        root_layout.addWidget(self._build_ai_console(), 0)
        self.setCentralWidget(central_widget)
        self.setStatusBar(QStatusBar(self))

        # 键盘快捷键
        QShortcut(QKeySequence(Qt.CTRL + Qt.Key_Equal), self, self._zoom_in)
        QShortcut(QKeySequence(Qt.CTRL + Qt.Key_Minus), self, self._zoom_out)
        QShortcut(QKeySequence(Qt.CTRL + Qt.Key_0), self, self._zoom_full)

    def _build_mode_toggle(self) -> QWidget:
        """P1 改造：构建在线/离线模式切换栏 + 语言切换。"""

        bar = QFrame(self)
        bar.setObjectName("modeToggleBar")
        bar.setStyleSheet("""
            #modeToggleBar {
                background: #ffffff;
                border: 1px solid #dbe3ec;
                border-radius: 10px;
                padding: 6px 12px;
            }
        """)
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(16)

        mode_label = QLabel(self._lm.tr("mode_label"), bar)
        mode_label.setStyleSheet("font-weight: 600; color: #16202a;")

        self.btn_online = QRadioButton(self._lm.tr("mode_online"), bar)
        self.btn_online.setToolTip(self._lm.tr("mode_online_tooltip"))
        self.btn_online.toggled.connect(self._on_mode_toggled)
        self.btn_online.setChecked(not self.offline_mode)

        self.btn_offline = QRadioButton(self._lm.tr("mode_offline"), bar)
        self.btn_offline.setToolTip(self._lm.tr("mode_offline_tooltip"))
        self.btn_offline.toggled.connect(self._on_mode_toggled)
        self.btn_offline.setChecked(self.offline_mode)

        mode_group = QButtonGroup(bar)
        mode_group.addButton(self.btn_online)
        mode_group.addButton(self.btn_offline)

        # 状态指示标签（根据上次保存的模式初始化）
        if self.offline_mode:
            self.offline_mode_label = QLabel(" " + self._lm.tr("status_offline"), bar)
            self.offline_mode_label.setStyleSheet("""
                color: #dc2626; font-weight: 600; font-size: 12px;
                border: 1px solid #fca5a5; border-radius: 14px;
                padding: 2px 12px; background: #fef2f2;
            """)
        else:
            self.offline_mode_label = QLabel(" " + self._lm.tr("status_online"), bar)
            self.offline_mode_label.setStyleSheet("""
                color: #16a34a; font-weight: 600; font-size: 12px;
                border: 1px solid #86efac; border-radius: 14px;
                padding: 2px 12px; background: #f0fdf4;
            """)

        layout.addWidget(mode_label)
        layout.addWidget(self.btn_online)
        layout.addWidget(self.btn_offline)
        layout.addWidget(self.offline_mode_label)
        layout.addStretch()

        # ── 语言切换下拉框（右侧） ──
        from PyQt5.QtWidgets import QComboBox
        lang_label = QLabel(self._lm.tr("lang_label"), bar)
        lang_label.setStyleSheet("font-weight: 600; color: #16202a;")
        layout.addWidget(lang_label)

        self.lang_combo = QComboBox(bar)
        self.lang_combo.setFixedWidth(100)
        for code, name in self._lm.supported_langs.items():
            self.lang_combo.addItem(name, code)
        # 选中当前语言
        current_idx = self.lang_combo.findData(self._lm.current_lang)
        if current_idx >= 0:
            self.lang_combo.setCurrentIndex(current_idx)
        self.lang_combo.currentIndexChanged.connect(self._on_language_changed)
        layout.addWidget(self.lang_combo)

        return bar

    def _on_mode_toggled(self) -> None:
        """P1 改造：在线/离线模式切换回调。"""

        if self.btn_offline.isChecked():
            self.offline_mode = True
            self._apply_offline_mode()
            self.config.last_mode = "offline"
        else:
            self.offline_mode = False
            self._apply_online_mode()
            self.config.last_mode = "online"

        # 同步到 ai_worker 全局标志（防御层）
        from core.ai_worker import set_offline_mode
        set_offline_mode(self.offline_mode)

    def _on_language_changed(self, _index: int) -> None:
        """语言下拉框切换回调。"""
        lang_code = self.lang_combo.currentData()
        if lang_code and lang_code != self._lm.current_lang:
            self._lm.set_language(lang_code)

    def _apply_language(self, lang: str) -> None:
        """响应语言切换信号，刷新所有 UI 文本。"""
        self.setWindowTitle(f"{self._lm.tr('window_title')} v{_app_version}")
        self._refresh_all_labels()
        self.statusBar().showMessage(
            self._lm.tr("status_online") if not self.offline_mode
            else self._lm.tr("status_offline"),
            3000,
        )

    def _refresh_all_labels(self) -> None:
        """统一刷新所有 UI 标签文本（从语言资源重新加载）。"""
        # 模式切换栏
        self.btn_online.setText(self._lm.tr("mode_online"))
        self.btn_online.setToolTip(self._lm.tr("mode_online_tooltip"))
        self.btn_offline.setText(self._lm.tr("mode_offline"))
        self.btn_offline.setToolTip(self._lm.tr("mode_offline_tooltip"))

        # 状态标签
        if self.offline_mode_label:
            txt = self._lm.tr("status_offline") if self.offline_mode else self._lm.tr("status_online")
            self.offline_mode_label.setText(" " + txt)

        # 菜单栏
        self._refresh_menu_labels()

        # 工具栏
        self._refresh_toolbar_labels()

        # 侧边栏
        self._refresh_sidebar_labels()

        # AI 控制台
        self._refresh_ai_console_labels()

        # 画布
        if self.map_canvas:
            self.map_canvas.setToolTip(self._lm.tr("canvas_tooltip"))

    def _refresh_menu_labels(self) -> None:
        """刷新所有菜单项文本。"""
        mb = self.menuBar()
        if not mb:
            return
        for action in mb.actions():
            action.setText(self._tr_menu(action.text()))

    @staticmethod
    def _tr_menu(text: str) -> str:
        """根据旧菜单文本匹配 i18n key（兼容切换时无需存储 key 映射）。"""
        mapping = {
            "文件(&F)": "menu_file",
            "新建项目(&N)": "menu_new_project",
            "关闭项目(&C)": "menu_close_project",
            "保存(&S)": "menu_save_project",
            "另存为(&A)...": "menu_save_as",
            "导入/导出": "menu_import_export",
            "导出地图为图片...": "menu_export_map_image",
            "导出图层...": "menu_export_layer",
            "导入图层...": "menu_import_layer",
            "API 设置(&S)...": "menu_api_settings",
            "退出(&X)": "menu_exit",
            "视图(&V)": "menu_view",
            "启用代码预览(&P)": "menu_code_preview",
            "启用画布截图分析(&M)": "menu_multimodal",
            "重置 AI 状态(&R)": "menu_reset_ai",
            "工具(&T)": "menu_tools",
            "提示词 Agent(&P)...": "menu_prompt_agent",
            "一键瘦身": "menu_cleanup",
            "帮助(&H)": "menu_help",
            "查看日志(&L)...": "menu_view_log",
            "关于 AIQGIS(&A)": "menu_about",
            "File(&F)": "menu_file",
            "New Project(&N)": "menu_new_project",
            "Close Project(&C)": "menu_close_project",
            "Save(&S)": "menu_save_project",
            "Save As(&A)...": "menu_save_as",
            "Import/Export": "menu_import_export",
            "Export Map as Image...": "menu_export_map_image",
            "Export Layer...": "menu_export_layer",
            "Import Layer...": "menu_import_layer",
            "API Settings(&S)...": "menu_api_settings",
            "Exit(&X)": "menu_exit",
            "View(&V)": "menu_view",
            "Enable Code Preview(&P)": "menu_code_preview",
            "Enable Canvas Screenshot Analysis(&M)": "menu_multimodal",
            "Reset AI State(&R)": "menu_reset_ai",
            "Tools(&T)": "menu_tools",
            "Prompt Agent(&P)...": "menu_prompt_agent",
            "Clean Up": "menu_cleanup",
            "Help(&H)": "menu_help",
            "View Log(&L)...": "menu_view_log",
            "About AIQGIS(&A)": "menu_about",
            "ファイル(&F)": "menu_file",
            "新規プロジェクト(&N)": "menu_new_project",
            "プロジェクトを閉じる(&C)": "menu_close_project",
            "保存(&S)": "menu_save_project",
            "名前を付けて保存(&A)...": "menu_save_as",
            "インポート/エクスポート": "menu_import_export",
            "地図を画像としてエクスポート...": "menu_export_map_image",
            "レイヤをエクスポート...": "menu_export_layer",
            "レイヤをインポート...": "menu_import_layer",
            "API設定(&S)...": "menu_api_settings",
            "終了(&X)": "menu_exit",
            "表示(&V)": "menu_view",
            "コードプレビューを有効化(&P)": "menu_code_preview",
            "キャンバススクリーンショット分析を有効化(&M)": "menu_multimodal",
            "AI状態をリセット(&R)": "menu_reset_ai",
            "ツール(&T)": "menu_tools",
            "プロンプトエージェント(&P)...": "menu_prompt_agent",
            "クリーンアップ": "menu_cleanup",
            "ヘルプ(&H)": "menu_help",
            "ログを表示(&L)...": "menu_view_log",
            "AIQGISについて(&A)": "menu_about",
            # 新增菜单项映射（三语）
            "导出属性表(&E)...": "menu_export_attribute",
            "加载样式文件(&L)...": "menu_load_style",
            "全图显示(&F)": "menu_full_extent",
            "标注开关(&L)": "menu_toggle_labels",
            "字段管理器(&F)": "menu_field_manager",
            "要素统计(&S)...": "menu_layer_statistic",
            "缓冲区分析(&B)...": "menu_create_buffer",
            "批量样式设置(&Y)...": "menu_batch_style",
            "Export Attribute Table(&E)...": "menu_export_attribute",
            "Load Style File(&L)...": "menu_load_style",
            "Full Extent(&F)": "menu_full_extent",
            "Toggle Labels(&L)": "menu_toggle_labels",
            "Field Manager(&F)": "menu_field_manager",
            "Layer Statistics(&S)...": "menu_layer_statistic",
            "Buffer Analysis(&B)...": "menu_create_buffer",
            "Batch Style Setting(&Y)...": "menu_batch_style",
            "属性テーブルをエクスポート(&E)...": "menu_export_attribute",
            "スタイルファイルを読み込む(&L)...": "menu_load_style",
            "全図表示(&F)": "menu_full_extent",
            "ラベル切替(&L)": "menu_toggle_labels",
            "フィールドマネージャ(&F)": "menu_field_manager",
            "地物統計(&S)...": "menu_layer_statistic",
            "バッファ分析(&B)...": "menu_create_buffer",
            "一括スタイル設定(&Y)...": "menu_batch_style",
        }
        key = mapping.get(text)
        if key:
            return lang_manager().tr(key)
        return text

    def _refresh_toolbar_labels(self) -> None:
        """刷新工具栏文本。"""
        for tb in self.findChildren(QToolBar):
            tb.setWindowTitle(lang_manager().tr("toolbar_title"))
            for action in tb.actions():
                action_name = action.text()
                bar_map = {
                    "平移": "tool_pan", "移动": "tool_pan", "Pan": "tool_pan",
                    "放大": "tool_zoom_in", "拡大": "tool_zoom_in", "Zoom In": "tool_zoom_in",
                    "缩小": "tool_zoom_out", "縮小": "tool_zoom_out", "Zoom Out": "tool_zoom_out",
                    "选择": "tool_select", "選択": "tool_select", "Select": "tool_select",
                    "编辑": "tool_edit", "編集": "tool_edit", "Edit": "tool_edit",
                }
                key = bar_map.get(action_name)
                if key:
                    action.setText(lang_manager().tr(key))
                # tooltip
                tip_map = {
                    "平移": "tool_pan_tip", "移动": "tool_pan_tip", "Pan": "tool_pan_tip",
                    "放大": "tool_zoom_in_tip", "拡大": "tool_zoom_in_tip", "Zoom In": "tool_zoom_in_tip",
                    "缩小": "tool_zoom_out_tip", "縮小": "tool_zoom_out_tip", "Zoom Out": "tool_zoom_out_tip",
                    "选择": "tool_select_tip", "選択": "tool_select_tip", "Select": "tool_select_tip",
                    "编辑": "tool_edit_tip", "編集": "tool_edit_tip", "Edit": "tool_edit_tip",
                }
                tip_key = tip_map.get(action_name)
                if tip_key:
                    action.setToolTip(lang_manager().tr(tip_key))

    def _refresh_sidebar_labels(self) -> None:
        """刷新侧边栏文本。"""
        # 通过遍历 QFrame 的子控件找到标签和按钮
        for child in self.findChildren(QLabel):
            if child.objectName() == "panelTitle":
                child.setText(lang_manager().tr("sidebar_title"))
        # 按钮
        btn_map = {"移除图层": "btn_remove_layer", "缩放到图层": "btn_zoom_layer",
                    "Remove Layer": "btn_remove_layer", "Zoom to Layer": "btn_zoom_layer",
                    "レイヤを削除": "btn_remove_layer", "レイヤにズーム": "btn_zoom_layer"}
        tip_map = {"移除图层": "btn_remove_tip", "缩放到图层": "btn_zoom_tip",
                   "Remove Layer": "btn_remove_tip", "Zoom to Layer": "btn_zoom_tip",
                   "レイヤを削除": "btn_remove_tip", "レイヤにズーム": "btn_zoom_tip"}
        for btn in self.findChildren(QPushButton):
            txt = btn.text()
            key = btn_map.get(txt)
            if key:
                btn.setText(lang_manager().tr(key))
                tip_key = tip_map.get(txt)
                if tip_key:
                    btn.setToolTip(lang_manager().tr(tip_key))

    def _refresh_ai_console_labels(self) -> None:
        """刷新 AI 控制台文本。"""
        lm = lang_manager()
        self.run_button.setText(lm.tr("btn_run_ai"))
        self.run_button.setToolTip(lm.tr("btn_run_tip"))
        self.screenshot_button.setText(lm.tr("btn_screenshot"))
        self.screenshot_button.setToolTip(lm.tr("btn_screenshot_tip"))

        if self.offline_mode:
            self.ai_prompt_input.setPlaceholderText(lm.tr("ai_placeholder_offline"))
        else:
            self.ai_prompt_input.setPlaceholderText(lm.tr("ai_placeholder_online"))
        self.ai_prompt_input.setToolTip(lm.tr("ai_tooltip"))

        self.ai_response_display.setPlaceholderText(lm.tr("ai_response_placeholder"))
        self.ai_response_display.setToolTip(lm.tr("ai_response_tooltip"))

        # 快捷流程标签
        if hasattr(self, '_offline_label'):
            self._offline_label.setText(lm.tr("label_shortcuts"))

        # 快捷流程按钮
        btn_keys = ["btn_cadastral", "btn_hydrology", "btn_batch_clip",
                     "btn_attribute_batch", "btn_thematic_map"]
        tip_keys = ["tip_cadastral", "tip_hydrology", "tip_batch_clip",
                     "tip_attribute_batch", "tip_thematic_map"]
        for i, btn in enumerate(self._offline_buttons):
            if i < len(btn_keys):
                btn.setText(lm.tr(btn_keys[i]))
                btn.setToolTip(lm.tr(tip_keys[i]))

        # 分组面板标题、箭头和 tooltip
        if hasattr(self, '_vector_toggle_btn'):
            vec_collapsed = (
                not hasattr(self, '_vector_button_row')
                or not self._vector_button_row.isVisible()
            )
            vec_arrow = "▶" if vec_collapsed else "▼"
            self._vector_toggle_btn.setText(
                f"{vec_arrow} {lm.tr('group_vector_title')}"
            )
            self._vector_toggle_btn.setToolTip(
                lm.tr("btn_expand") if vec_collapsed else lm.tr("btn_collapse")
            )
        if hasattr(self, '_raster_toggle_btn'):
            ras_collapsed = (
                not hasattr(self, '_raster_button_row')
                or not self._raster_button_row.isVisible()
            )
            ras_arrow = "▶" if ras_collapsed else "▼"
            self._raster_toggle_btn.setText(
                f"{ras_arrow} {lm.tr('group_raster_title')}"
            )
            self._raster_toggle_btn.setToolTip(
                lm.tr("btn_expand") if ras_collapsed else lm.tr("btn_collapse")
            )

        # 刷新文件菜单新增项
        if self._save_action:
            self._save_action.setText(lm.tr("menu_save_project"))
        if self._save_as_action:
            self._save_as_action.setText(lm.tr("menu_save_as"))
        if self._export_map_action:
            self._export_map_action.setText(lm.tr("menu_export_map_image"))
        if self._export_layer_action:
            self._export_layer_action.setText(lm.tr("menu_export_layer"))
        if self._import_layer_action:
            self._import_layer_action.setText(lm.tr("menu_import_layer"))
        # 子菜单标题刷新 — 通过 parent 找到 io_menu
        if self._import_layer_action:
            io_menu = self._import_layer_action.parent()
            if isinstance(io_menu, QMenu):
                io_menu.setTitle(lm.tr("menu_import_export"))

    def _apply_offline_mode(self) -> None:
        """应用离线模式 UI 状态。"""
        self.ai_prompt_input.setEnabled(True)
        self.ai_prompt_input.setPlaceholderText(self._lm.tr("ai_placeholder_offline_local"))
        self.run_button.setEnabled(True)
        self.run_button.setToolTip(self._lm.tr("btn_run_tip"))
        self.screenshot_button.setEnabled(False)

        for btn in self._offline_buttons:
            btn.setEnabled(False)

        if self.offline_mode_label:
            self.offline_mode_label.setText(" " + self._lm.tr("status_offline"))
            self.offline_mode_label.setStyleSheet("""
                color: #dc2626; font-weight: 600; font-size: 12px;
                border: 1px solid #fca5a5; border-radius: 14px;
                padding: 2px 12px; background: #fef2f2;
            """)

        self.statusBar().showMessage(self._lm.tr("status_offline_switched"), 5000)

    def _apply_online_mode(self) -> None:
        """应用在线模式 UI 状态。"""
        self.ai_prompt_input.setEnabled(True)
        self.ai_prompt_input.setPlaceholderText(self._lm.tr("ai_placeholder_online"))
        self.run_button.setEnabled(True)
        self.run_button.setToolTip(self._lm.tr("btn_run_tip"))
        self.screenshot_button.setEnabled(self.multimodal_enabled)

        for btn in self._offline_buttons:
            btn.setEnabled(True)

        if self.offline_mode_label:
            self.offline_mode_label.setText(" " + self._lm.tr("status_online"))
            self.offline_mode_label.setStyleSheet("""
                color: #16a34a; font-weight: 600; font-size: 12px;
                border: 1px solid #86efac; border-radius: 14px;
                padding: 2px 12px; background: #f0fdf4;
            """)

        self.statusBar().showMessage(self._lm.tr("msg_switched_online"), 5000)

    def _build_menubar(self) -> None:
        """构建菜单栏。"""
        menubar = self.menuBar()

        file_menu = menubar.addMenu(self._lm.tr("menu_file"))
        
        # 新建项目
        new_project_action = QAction(self._lm.tr("menu_new_project"), self)
        new_project_action.setShortcut(QKeySequence("Ctrl+N"))
        new_project_action.triggered.connect(self._on_new_project)
        file_menu.addAction(new_project_action)
        
        # 关闭项目  
        close_project_action = QAction(self._lm.tr("menu_close_project"), self)
        close_project_action.triggered.connect(self._on_close_project)
        file_menu.addAction(close_project_action)

        # 保存项目
        self._save_action = QAction(self._lm.tr("menu_save_project"), self)
        self._save_action.setShortcut(QKeySequence("Ctrl+S"))
        self._save_action.triggered.connect(self._on_save_project)
        file_menu.addAction(self._save_action)

        # 另存为
        self._save_as_action = QAction(self._lm.tr("menu_save_as"), self)
        self._save_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self._save_as_action.triggered.connect(self._on_save_as_project)
        file_menu.addAction(self._save_as_action)

        file_menu.addSeparator()

        # 导入/导出子菜单
        io_menu = file_menu.addMenu(self._lm.tr("menu_import_export"))

        self._export_map_action = QAction(self._lm.tr("menu_export_map_image"), self)
        self._export_map_action.triggered.connect(self._on_export_map_image)
        io_menu.addAction(self._export_map_action)

        self._export_layer_action = QAction(self._lm.tr("menu_export_layer"), self)
        self._export_layer_action.triggered.connect(self._on_export_layer)
        io_menu.addAction(self._export_layer_action)

        io_menu.addSeparator()

        self._import_layer_action = QAction(self._lm.tr("menu_import_layer"), self)
        self._import_layer_action.triggered.connect(self._on_import_layer)
        io_menu.addAction(self._import_layer_action)

        file_menu.addSeparator()

        # P1 新增：导出属性表
        self._export_attr_action = QAction(self._lm.tr("menu_export_attribute"), self)
        self._export_attr_action.triggered.connect(self._on_export_attribute_menu)
        file_menu.addAction(self._export_attr_action)

        # P1 新增：加载样式文件
        self._load_style_action = QAction(self._lm.tr("menu_load_style"), self)
        self._load_style_action.triggered.connect(self._on_load_style_menu)
        file_menu.addAction(self._load_style_action)

        file_menu.addSeparator()
        
        # 保存 file_menu 引用供语言切换刷新
        self._file_menu = file_menu

        settings_action = QAction(self._lm.tr("menu_api_settings"), self)
        settings_action.triggered.connect(self._show_api_config)
        file_menu.addAction(settings_action)
        file_menu.addSeparator()
        exit_action = QAction(self._lm.tr("menu_exit"), self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        view_menu = menubar.addMenu(self._lm.tr("menu_view"))
        preview_action = QAction(
            self._lm.tr("menu_code_preview"),
            self,
            checkable=True,
            checked=not self.skip_preview,
        )
        preview_action.triggered.connect(self._toggle_preview)
        view_menu.addAction(preview_action)

        # Phase 4：多模态开关菜单项
        multimodal_action = QAction(
            self._lm.tr("menu_multimodal"),
            self,
            checkable=True,
            checked=self.multimodal_enabled,
        )
        multimodal_action.triggered.connect(self._toggle_multimodal)
        view_menu.addAction(multimodal_action)
        self._multimodal_menu_action = multimodal_action

        reset_ai_action = QAction(self._lm.tr("menu_reset_ai"), self)
        reset_ai_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        reset_ai_action.triggered.connect(self._reset_ai_context)
        view_menu.addAction(reset_ai_action)

        # P0 新增：全图显示
        full_extent_action = QAction(self._lm.tr("menu_full_extent"), self)
        full_extent_action.triggered.connect(self._handle_reset_view)
        view_menu.addAction(full_extent_action)

        # P2 新增：标注开关
        toggle_labels_action = QAction(self._lm.tr("menu_toggle_labels"), self)
        toggle_labels_action.triggered.connect(self._on_toggle_labels_menu)
        view_menu.addAction(toggle_labels_action)

        tools_menu = menubar.addMenu(self._lm.tr("menu_tools"))
        prompt_agent_action = QAction(self._lm.tr("menu_prompt_agent"), self)
        prompt_agent_action.setToolTip(self._lm.tr("menu_prompt_agent_tip"))
        prompt_agent_action.triggered.connect(self._open_prompt_agent)
        tools_menu.addAction(prompt_agent_action)

        cleanup_action = QAction(self._lm.tr("menu_cleanup"), self)
        cleanup_action.setToolTip(self._lm.tr("menu_cleanup_tip"))
        cleanup_action.triggered.connect(self._on_cleanup_clicked)
        tools_menu.addAction(cleanup_action)

        # P2 新增：字段管理器
        field_mgr_action = QAction(self._lm.tr("menu_field_manager"), self)
        field_mgr_action.triggered.connect(self._on_open_field_manager_menu)
        tools_menu.addAction(field_mgr_action)

        # P2 新增：要素统计
        statistic_action = QAction(self._lm.tr("menu_layer_statistic"), self)
        statistic_action.triggered.connect(self._on_layer_statistic_menu)
        tools_menu.addAction(statistic_action)

        # P2 新增：缓冲区分析
        buffer_action = QAction(self._lm.tr("menu_create_buffer"), self)
        buffer_action.triggered.connect(self._on_create_buffer_menu)
        tools_menu.addAction(buffer_action)

        # P1 新增：批量样式设置
        batch_style_action = QAction(self._lm.tr("menu_batch_style"), self)
        batch_style_action.triggered.connect(self._on_batch_style_menu)
        tools_menu.addAction(batch_style_action)

        help_menu = menubar.addMenu(self._lm.tr("menu_help"))
        view_log_action = QAction(self._lm.tr("menu_view_log"), self)
        view_log_action.triggered.connect(self._show_log_viewer)
        help_menu.addAction(view_log_action)
        help_menu.addSeparator()
        about_action = QAction(self._lm.tr("menu_about"), self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _on_new_project(self) -> None:
        """新建空白项目，清空画布并重置状态。"""
        result = self.project_manager.create_new(canvas=self.map_canvas)
        if self.layer_tree_model is not None:
            self.layer_tree_model.rootGroup().removeAllChildren()
        self.statusBar().showMessage(result.get("message", "新建项目完成"), 5000)

    def _on_close_project(self) -> None:
        """关闭当前项目，清空画布和图层树。"""
        result = self.project_manager.close_project(canvas=self.map_canvas)
        if self.layer_tree_model is not None:
            self.layer_tree_model.rootGroup().removeAllChildren()
        self.statusBar().showMessage("项目已关闭", 5000)

    def _on_save_project(self) -> None:
        """保存当前 QGIS 项目（Ctrl+S）。

        使用 QgsProject.instance().write() 直接写入当前路径；
        若项目为新建未保存状态（fileName 为空），自动调用另存为。
        """
        project = QgsProject.instance()
        current_path = project.fileName()

        if not current_path:
            self._on_save_as_project()
            return

        if project.write():
            self.statusBar().showMessage(f"项目已保存：{current_path}", 5000)
        else:
            QMessageBox.warning(self, "保存失败", f"无法写入项目文件：{current_path}")

    def _on_save_as_project(self) -> None:
        """另存为 QGIS 项目（Ctrl+Shift+S）。

        弹出文件对话框选择 .qgz 路径，调用 QgsProject.instance().write() 写入。
        """
        from PyQt5.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self,
            self._lm.tr("menu_save_as"),
            QgsProject.instance().fileName() or "",
            "QGIS 项目文件 (*.qgz *.qgs);;所有文件 (*)",
        )
        if not path:
            return

        if QgsProject.instance().write(path):
            self.statusBar().showMessage(f"项目已另存为：{path}", 8000)
        else:
            QMessageBox.warning(self, "保存失败", f"无法写入项目文件：{path}")

    def _on_export_map_image(self) -> None:
        """导出当前地图画布为图片（PNG/JPG/BMP）。

        使用 QgsMapSettings + QgsMapRendererCustomPainterJob 渲染当前画布并保存。
        """
        from PyQt5.QtWidgets import QFileDialog
        from PyQt5.QtGui import QImage, QPainter

        if not self.map_canvas:
            QMessageBox.warning(self, "导出失败", "地图画布未初始化。")
            return

        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            self._lm.tr("menu_export_map_image"),
            "",
            "PNG 图片 (*.png);;JPEG 图片 (*.jpg *.jpeg);;BMP 图片 (*.bmp)",
        )
        if not path:
            return

        # 使用 QGIS 原生渲染管道
        settings = self.map_canvas.mapSettings()
        size = settings.outputSize()

        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)

        painter = QPainter(image)
        job = QgsMapRendererCustomPainterJob(settings, painter)
        job.start()
        job.waitForFinished()
        painter.end()

        if image.save(path):
            self.statusBar().showMessage(f"地图已导出：{path}", 8000)
        else:
            QMessageBox.warning(self, "导出失败", f"无法写入图片：{path}")

    def _on_export_layer(self) -> None:
        """导出当前选中图层为文件。

        矢量图层 → Shapefile/GeoJSON；栅格图层 → GeoTIFF。
        使用 QGIS 原生 QgsVectorFileWriter / QgsRasterFileWriter。
        """
        from PyQt5.QtWidgets import QFileDialog

        layer = self._get_active_layer()
        if not layer:
            QMessageBox.warning(self, "导出失败", "请先在图层列表中选择一个图层。")
            return

        if isinstance(layer, QgsVectorLayer):
            default_filter = "ESRI Shapefile (*.shp);;GeoJSON (*.geojson);;GPKG (*.gpkg)"
            path, selected_filter = QFileDialog.getSaveFileName(
                self, self._lm.tr("menu_export_layer"), "", default_filter
            )
            if not path:
                return

            driver_map = {".shp": "ESRI Shapefile", ".geojson": "GeoJSON", ".gpkg": "GPKG"}
            import os
            ext = os.path.splitext(path)[1].lower()
            driver = driver_map.get(ext, "ESRI Shapefile")

            error = QgsVectorFileWriter.writeAsVectorFormat(layer, path, "UTF-8", layer.crs(), driver)
            if error[0] == QgsVectorFileWriter.NoError:
                self.statusBar().showMessage(f"图层已导出：{path}", 8000)
            else:
                QMessageBox.warning(self, "导出失败", f"导出错误：{error}")

        elif isinstance(layer, QgsRasterLayer):
            path, _ = QFileDialog.getSaveFileName(
                self, self._lm.tr("menu_export_layer"), "", "GeoTIFF (*.tif *.tiff)"
            )
            if not path:
                return

            from qgis.core import QgsRasterPipe
            provider = layer.dataProvider()
            pipe = QgsRasterPipe()
            if pipe.set(provider.clone()):
                writer = QgsRasterFileWriter(path)
                writer.setOutputFormat("GTiff")
                writer.writeRaster(pipe, provider.xSize(), provider.ySize(), provider.extent(), layer.crs())
                self.statusBar().showMessage(f"图层已导出：{path}", 8000)
            else:
                QMessageBox.warning(self, "导出失败", "无法创建栅格数据管道。")
        else:
            QMessageBox.warning(self, "导出失败", "不支持的图层类型。")

    def _on_import_layer(self) -> None:
        """导入图层文件到当前项目。

        支持矢量（SHP/GeoJSON/GPKG）和栅格（TIF/PNG/JPG）。
        使用 QGIS 原生 QgsVectorLayer / QgsRasterLayer 加载。
        """
        from PyQt5.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self,
            self._lm.tr("menu_import_layer"),
            "",
            "所有支持格式 (*.shp *.geojson *.gpkg *.tif *.tiff *.png *.jpg *.jpeg);;矢量 (*.shp *.geojson *.gpkg);;栅格 (*.tif *.tiff *.png *.jpg *.jpeg);;所有文件 (*)",
        )
        if not path:
            return

        import os
        name = os.path.splitext(os.path.basename(path))[0]

        raster_exts = {'.tif', '.tiff', '.png', '.jpg', '.jpeg', '.bmp'}
        ext = os.path.splitext(path)[1].lower()

        if ext in raster_exts:
            layer = QgsRasterLayer(path, name)
        else:
            layer = QgsVectorLayer(path, name, "ogr")

        if not layer.isValid():
            QMessageBox.warning(self, "导入失败", f"无法加载图层：{path}")
            return

        QgsProject.instance().addMapLayer(layer)
        self.statusBar().showMessage(f"图层已导入：{path}", 8000)

    def _add_layer_from_file(self, path: str) -> None:
        """从文件静默加载图层到项目，用于管道输出的 GeoJSON 自动加载。"""
        import os
        if not os.path.exists(path):
            _log.warning("_add_layer_from_file: 文件不存在 %s", path)
            return
        name = os.path.splitext(os.path.basename(path))[0]
        layer = QgsVectorLayer(path, name, "ogr")
        if not layer.isValid():
            _log.warning("_add_layer_from_file: 无法加载图层 %s", path)
            return
        QgsProject.instance().addMapLayer(layer)
        _log.info("_add_layer_from_file: 已加载 %s", path)
        # 缩放到新图层
        if self.map_canvas:
            self.map_canvas.setExtent(layer.extent())
            self.map_canvas.refresh()

    def _show_api_config(self) -> None:
        """显示 API 配置对话框。"""
        import core.ai_config as ai_config
        current = {
            "api_key": ai_config.API_KEY,
            "base_url": ai_config.BASE_URL,
            "model_name": ai_config.MODEL_NAME,
        }
        result = ApiConfigDialog.get_config(self, current)
        if not result:
            return

        try:
            # 动态更新已加载的模块
            ai_config.API_KEY = result["api_key"]
            ai_config.BASE_URL = result["base_url"]
            ai_config.MODEL_NAME = result["model_name"]

            # 持久化到 JSON 配置文件（跨版本/重装不丢失）
            self.config.api_key = result["api_key"]
            self.config.base_url = result["base_url"]
            self.config.model_name = result["model_name"]

            # P2 改造：保存本地模型配置
            if result.get("local_model_url"):
                self.config.local_model_url = result["local_model_url"]
            if result.get("local_model_name"):
                self.config.local_model_name = result["local_model_name"]

            QMessageBox.information(self, "配置已保存", "API 配置已更新，下次分析将使用新设置。")
        except Exception as e:
            QMessageBox.critical(self, "保存失败", f"无法保存配置：{e}")

    def _toggle_preview(self, checked: bool) -> None:
        """切换代码预览开关。"""
        self.skip_preview = not checked
        self.statusBar().showMessage(
            "代码预览已启用" if checked else "代码预览已关闭，AI 代码将直接执行",
            3000,
        )

    def _toggle_multimodal(self, checked: bool) -> None:
        """Phase 4：切换多模态画布截图分析开关。"""
        self.multimodal_enabled = checked
        self.screenshot_button.setEnabled(checked)
        self.statusBar().showMessage(
            "画布截图分析已启用" if checked else "画布截图分析已关闭，回滚至纯文本管线",
            3000,
        )

    def _reset_ai_context(self) -> None:
        """清空 AI 上下文缓存，确保下次分析从最新 Prompt 重新请求。"""
        self.last_ai_code = ""
        self.skip_preview = False
        self.ai_prompt_input.clear()
        self.ai_response_display.clear()
        self._pending_multimodal_data = None
        # 终止 AI 流水线线程
        if self.ai_worker and self.ai_worker.isRunning():
            self.ai_worker.terminate()
            self.ai_worker.wait(2000)
            self.ai_worker = None
        # 终止代码生成线程
        if hasattr(self, '_code_worker') and self._code_worker is not None:
            if self._code_worker.isRunning():
                self._code_worker.terminate()
                self._code_worker.wait(2000)
            self._code_worker = None
        self.run_button.setEnabled(True)
        self.screenshot_button.setEnabled(self.multimodal_enabled)
        self.statusBar().showMessage("AI 上下文已重置，代码缓存已清空。", 3000)
        _log.info("AI 上下文已手动重置")

    def _show_about(self) -> None:
        """显示关于对话框。"""
        QMessageBox.about(
            self,
            "关于 AIQGIS",
            "<h2>AIQGIS</h2>"
            "<p>AI 驱动轻量桌面 GIS 应用</p>"
            "<p>版本 0.2.0</p>"
            "<p>基于 PyQGIS + DeepSeek</p>"
            "<p>开源项目 - GitHub</p>",
        )

    def _open_prompt_agent(self) -> None:
        """打开提示词 Agent 独立窗口，接收 .docx/.pdf 拖拽并提炼 AI 指令。"""
        from prompt_agent.widget import PromptAgentWidget

        dialog = PromptAgentWidget(self)
        dialog.instruction_applied.connect(self.ai_prompt_input.setPlainText)
        dialog.exec_()

    def _on_cleanup_clicked(self) -> None:
        """弹出确认框，确认后清空 user_data/ 下所有子目录的文件（保留目录结构）。"""
        lm = lang_manager()
        reply = QMessageBox.question(
            self,
            lm.tr("cleanup_confirm_title"),
            lm.tr("cleanup_confirm_msg"),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        user_data_dir = os.path.join(project_root, "user_data")

        if not os.path.isdir(user_data_dir):
            QMessageBox.information(self, lm.tr("cleanup_confirm_title"), lm.tr("cleanup_nothing"))
            return

        total_size = 0
        files_deleted = 0
        for root, dirs, files in os.walk(user_data_dir):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    total_size += os.path.getsize(fp)
                    os.remove(fp)
                    files_deleted += 1
                except OSError:
                    pass

        size_mb = total_size / (1024 * 1024)
        if files_deleted == 0:
            QMessageBox.information(self, lm.tr("cleanup_confirm_title"), lm.tr("cleanup_nothing"))
            return

        msg = lm.tr("cleanup_done").format(size=f"{size_mb:.1f}")
        QMessageBox.information(self, lm.tr("cleanup_confirm_title"), msg)

    def _show_log_viewer(self) -> None:
        """显示日志查看对话框，ERROR/CRITICAL 行前加红色感叹号标记。"""
        from PyQt5.QtWidgets import QDialog, QVBoxLayout
        from PyQt5.QtGui import QTextCursor

        # 定位日志文件
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        log_path = os.path.join(project_root, "user_data", "logs", "aiqgis.log")

        # 读取并标记错误行
        lines: List[str] = []
        error_count = 0
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                raw_lines = f.readlines()
        except FileNotFoundError:
            raw_lines = ["日志文件尚未生成。请先运行程序触发一次日志记录。"]
        except Exception as exc:
            raw_lines = [f"读取日志文件失败：{exc}"]

        for line in raw_lines:
            stripped = line.strip()
            if "| ERROR " in stripped or "| CRITICAL " in stripped or "| WARNING " in stripped:
                if "| ERROR " in stripped or "| CRITICAL " in stripped:
                    lines.append(f"  {stripped}")
                    error_count += 1
                else:
                    lines.append(f"  {stripped}")
            else:
                lines.append(f"    {stripped}")

        full_text = "\n".join(lines)

        # 构建对话框
        dialog = QDialog(self)
        dialog.setWindowTitle("运行日志")
        dialog.resize(1000, 650)
        dialog.setMinimumSize(700, 400)

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)

        text_edit = QTextEdit(dialog)
        text_edit.setReadOnly(True)
        text_edit.setPlainText(full_text)
        # 等宽字体
        text_edit.setStyleSheet("""
            QTextEdit {
                font-family: "Consolas", "Microsoft YaHei", monospace;
                font-size: 13px;
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: none;
            }
        """)

        # 高亮 ERROR/CRITICAL 行 — 红色前景 + 感叹号前缀
        doc = text_edit.document()
        cursor = QTextCursor(doc)
        fmt_error = text_edit.currentCharFormat()
        fmt_error.setForeground(QColor("#f44747"))
        fmt_error.setFontWeight(75)

        fmt_warn = text_edit.currentCharFormat()
        fmt_warn.setForeground(QColor("#e5c07b"))

        fmt_normal = text_edit.currentCharFormat()
        fmt_normal.setForeground(QColor("#d4d4d4"))
        fmt_normal.setFontWeight(50)

        cursor.movePosition(QTextCursor.Start)
        block = doc.begin()
        while block.isValid():
            text = block.text()
            if text.lstrip().startswith("!"):
                cursor.setPosition(block.position())
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                if "| ERROR " in text or "| CRITICAL " in text:
                    cursor.mergeCharFormat(fmt_error)
                else:
                    cursor.mergeCharFormat(fmt_warn)
            else:
                cursor.setPosition(block.position())
                cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
                cursor.mergeCharFormat(fmt_normal)
            block = block.next()

        # 滚动到底部（最新日志）
        cursor.movePosition(QTextCursor.End)
        text_edit.setTextCursor(cursor)
        text_edit.ensureCursorVisible()

        layout.addWidget(text_edit)

        status_label = QLabel(
            f"日志共 {len(raw_lines)} 行 | 错误 {error_count} 条"
            if error_count
            else f"日志共 {len(raw_lines)} 行 | 无错误"
        )
        status_label.setStyleSheet(
            "font-size: 12px; color: #888; padding: 4px 8px;"
        )
        layout.addWidget(status_label)

        dialog.exec_()

    def _build_toolbar(self) -> None:
        """构建顶部地图导航工具栏。"""

        toolbar = QToolBar(self._lm.tr("toolbar_title"), self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)

        pan_action = QAction(self._lm.tr("tool_pan"), self)
        pan_action.setToolTip(self._lm.tr("tool_pan_tip"))
        zoom_in_action = QAction(self._lm.tr("tool_zoom_in"), self)
        zoom_in_action.setToolTip(self._lm.tr("tool_zoom_in_tip"))
        zoom_out_action = QAction(self._lm.tr("tool_zoom_out"), self)
        zoom_out_action.setToolTip(self._lm.tr("tool_zoom_out_tip"))

        pan_action.triggered.connect(self._handle_pan)
        zoom_in_action.triggered.connect(self._handle_zoom_in)
        zoom_out_action.triggered.connect(self._handle_zoom_out)

        toolbar.addAction(pan_action)
        toolbar.addAction(zoom_in_action)
        toolbar.addAction(zoom_out_action)
        self.addToolBar(toolbar)

        # P0 新增：要素选择和编辑切换按钮
        select_action = QAction(self._lm.tr("tool_select"), self)
        select_action.setToolTip(self._lm.tr("tool_select_tip"))
        select_action.triggered.connect(self._handle_select_tool)
        toolbar.addAction(select_action)

        edit_action = QAction(self._lm.tr("tool_edit"), self)
        edit_action.setToolTip(self._lm.tr("tool_edit_tip"))
        edit_action.triggered.connect(self._handle_toggle_edit_tool)
        toolbar.addAction(edit_action)

    def _build_sidebar(self) -> QWidget:
        """构建左侧原生 QGIS 图层树面板。"""

        sidebar = QFrame(self)
        sidebar.setObjectName("sidebarPanel")
        sidebar.setMinimumWidth(280)

        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(self._lm.tr("sidebar_title"), sidebar)
        title.setObjectName("panelTitle")

        self.layer_tree_view = QgsLayerTreeView(sidebar)
        self.layer_tree_view.setObjectName("layerTreeView")
        self.layer_tree_view.setHeaderHidden(True)

        menu_provider = LayerTreeMenuProvider(
            self.layer_tree_view,
            on_attribute_table=self._open_attribute_table,
            on_zoom=self._zoom_to_layer,
            on_remove=self._remove_layer_from_menu,
            on_rename=self._rename_layer,
            on_copy=self._copy_layer,
            on_toggle_edit=self._on_toggle_edit_layer,
            on_layer_style=self._on_layer_style_menu,
            on_labels=self._on_toggle_labels_layer,
            on_filter=self._on_filter_layer,
            on_field_manager=self._on_field_manager_layer,
            on_export_attribute=self._on_export_attribute_layer,
            on_statistic=self._on_statistic_layer,
        )
        self.layer_tree_view.setMenuProvider(menu_provider)

        if self.bootstrap_result.available:
            root = QgsProject.instance().layerTreeRoot()
            self.layer_tree_model = QgsLayerTreeModel(root)
            self.layer_tree_model.setFlag(QgsLayerTreeModel.AllowNodeReorder, True)
            self.layer_tree_model.setFlag(QgsLayerTreeModel.AllowNodeChangeVisibility, True)
            self.layer_tree_view.setModel(self.layer_tree_model)

        layout.addWidget(title)
        layout.addWidget(self.layer_tree_view, 1)

        # 图层操作按钮
        btn_row = QHBoxLayout()
        btn_remove = QPushButton("移除图层")
        btn_remove.setToolTip("从工程中移除选中的图层（不删除文件）")
        btn_remove.clicked.connect(self._remove_selected_layer)
        btn_zoom = QPushButton("缩放到图层")
        btn_zoom.setToolTip("将地图缩放至选中图层的范围")
        btn_zoom.clicked.connect(self._zoom_to_selected_layer)
        btn_row.addWidget(btn_remove)
        btn_row.addWidget(btn_zoom)
        layout.addLayout(btn_row)

        return sidebar

    def _build_canvas_panel(self) -> QWidget:
        """构建中央地图画布面板。"""

        container = QFrame(self)
        container.setObjectName("canvasPanel")

        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)

        if not self.bootstrap_result.available:
            message_label = QLabel(self.bootstrap_result.message, container)
            message_label.setObjectName("canvasStatus")
            message_label.setWordWrap(True)
            message_label.setAlignment(Qt.AlignCenter)
            layout.addWidget(message_label)
            return container

        self.map_canvas = DroppableMapCanvas(container)
        self.map_canvas.setCanvasColor(QColor("#ffffff"))
        self.map_canvas.setToolTip("可将 SHP、GeoJSON、TIF 等图层文件拖拽到此处加载。")
        self.map_canvas.filesDropped.connect(self._load_dropped_files)

        root = QgsProject.instance().layerTreeRoot()
        self.canvas_bridge = QgsLayerTreeMapCanvasBridge(root, self.map_canvas)

        self.pan_tool = QgsMapToolPan(self.map_canvas)
        self.zoom_in_tool = QgsMapToolZoom(self.map_canvas, False)
        self.zoom_out_tool = QgsMapToolZoom(self.map_canvas, True)
        self.map_canvas.setMapTool(self.pan_tool)

        # 确保画布本身接受拖放（QgsMapCanvas 构造后可能重置）
        self.map_canvas.setAcceptDrops(True)
        self.map_canvas.viewport().setAcceptDrops(True)
        _log.debug("Canvas acceptDrops=%s viewport acceptDrops=%s", self.map_canvas.acceptDrops(), self.map_canvas.viewport().acceptDrops())

        layout.addWidget(self.map_canvas, 1)
        return container

    def _build_ai_console(self) -> QWidget:
        """构建底部 AI 提示控制台。"""

        container = QFrame(self)
        container.setObjectName("aiConsole")

        main_layout = QVBoxLayout(container)
        main_layout.setContentsMargins(16, 16, 16, 16)
        main_layout.setSpacing(12)

        # 输入行
        input_row = QHBoxLayout()
        input_row.setSpacing(12)

        self.ai_prompt_input.setObjectName("aiPromptInput")
        self.ai_prompt_input.setPlaceholderText(
            "请输入自然语言空间分析指令，例如：“为当前图层创建 30 米缓冲区”"
        )
        self.ai_prompt_input.setToolTip(
            "AI 会将自然语言指令转换为 PyQGIS 处理代码并自动执行。"
        )
        self.ai_prompt_input.setFixedHeight(96)

        button_column = QVBoxLayout()
        button_column.setContentsMargins(0, 0, 0, 0)
        button_column.setSpacing(8)
        button_column.addStretch(1)

        self.run_button.setToolTip("执行当前 AI 地理空间分析指令")
        self.run_button.clicked.connect(self._handle_run_clicked)
        button_column.addWidget(self.run_button)

        # Phase 4：画布截图分析按钮
        self.screenshot_button.setToolTip("截取当前画布并基于视觉分析执行指令")
        self.screenshot_button.clicked.connect(self._on_canvas_screenshot_clicked)
        self.screenshot_button.setEnabled(self.multimodal_enabled)
        button_column.addWidget(self.screenshot_button)

        input_row.addWidget(self.ai_prompt_input, 1)
        input_row.addLayout(button_column)

        main_layout.addLayout(input_row)

        # AI 响应显示区
        self.ai_response_display.setObjectName("aiResponseDisplay")
        self.ai_response_display.setPlaceholderText("AI 响应将显示在此处...")
        self.ai_response_display.setToolTip("AI 返回的原始响应内容")
        self.ai_response_display.setFixedHeight(120)
        main_layout.addWidget(self.ai_response_display)

        # ── P1 改造：离线快捷流程按钮组（分组折叠面板） ──
        offline_row = QHBoxLayout()
        offline_row.setSpacing(8)

        self._offline_label = QLabel("快捷流程：", container)
        self._offline_label.setStyleSheet("font-weight: 600; color: #16202a; font-size: 12px;")
        offline_row.addWidget(self._offline_label)

        # 读取折叠状态
        collapsed = self.config.offline_group_collapsed
        vector_collapsed = collapsed.get("vector", False)
        raster_collapsed = collapsed.get("raster", False)

        # 创建5个工作流按钮（clicked 信号保持原有连接不变）
        off_btn1 = QPushButton("1. 地籍标准化", container)
        off_btn1.setToolTip("图层批量转JGD2000 → 拓扑检查修复 → 属性规整 → 导出标准SHP")
        off_btn1.clicked.connect(lambda: self._on_offline_workflow("cadastral"))

        off_btn2 = QPushButton("2. DEM水文解析", container)
        off_btn2.setToolTip("洼地填充 → D8流向提取 → 汇流累积 → 河网提取 → 沟壑密度统计")
        off_btn2.clicked.connect(lambda: self._on_offline_workflow("hydrology"))

        off_btn3 = QPushButton("3. 一括切取+投影", container)
        off_btn3.setToolTip("多图层批量加载 → 边界裁剪 → 统一JGD2000 → 分类归档")
        off_btn3.clicked.connect(lambda: self._on_offline_workflow("batch_clip"))

        off_btn4 = QPushButton("4. 属性一括処理", container)
        off_btn4.setToolTip("条件筛选 → 字段批量赋值 → 导出SHP")
        off_btn4.clicked.connect(lambda: self._on_offline_workflow("attribute_batch"))

        off_btn5 = QPushButton("5. 主題図一括出力", container)
        off_btn5.setToolTip("图例/比例尺/指北针 → 统一样式渲染 → 批量PNG/PDF")
        off_btn5.clicked.connect(lambda: self._on_offline_workflow("thematic_map"))

        self._offline_buttons = [off_btn1, off_btn2, off_btn3, off_btn4, off_btn5]

        # 分组面板样式
        panel_style = """
            QFrame#offlineGroupPanel {
                background: #f8fafc;
                border: 1px solid #dbe3ec;
                border-radius: 8px;
            }
            QPushButton#groupToggleBtn {
                background: transparent;
                border: none;
                color: #16202a;
                font-weight: 600;
                font-size: 12px;
                text-align: left;
                padding: 4px 8px;
                min-width: auto;
                min-height: auto;
            }
            QPushButton#groupToggleBtn:hover {
                background: #e7edf6;
                border-radius: 6px;
            }
        """

        # ── 矢量批量处理分组 ──
        vector_panel = QFrame(container)
        vector_panel.setObjectName("offlineGroupPanel")
        vector_panel.setStyleSheet(panel_style)
        vector_layout = QVBoxLayout(vector_panel)
        vector_layout.setContentsMargins(6, 4, 6, 4)
        vector_layout.setSpacing(2)

        vec_arrow = "▶" if vector_collapsed else "▼"
        self._vector_toggle_btn = QPushButton(
            f"{vec_arrow} 矢量批量处理", container
        )
        self._vector_toggle_btn.setObjectName("groupToggleBtn")
        self._vector_toggle_btn.setToolTip(
            "展开分组" if vector_collapsed else "折叠分组"
        )
        self._vector_toggle_btn.clicked.connect(
            lambda: self._toggle_offline_group("vector")
        )
        vector_layout.addWidget(self._vector_toggle_btn)

        self._vector_button_row = QWidget(container)
        vec_row_layout = QHBoxLayout(self._vector_button_row)
        vec_row_layout.setContentsMargins(0, 0, 0, 0)
        vec_row_layout.setSpacing(4)
        vec_row_layout.addWidget(off_btn1)   # 1. 地籍标准化
        vec_row_layout.addWidget(off_btn3)   # 3. 一括切取+投影
        vec_row_layout.addWidget(off_btn4)   # 4. 属性一括処理
        vec_row_layout.addWidget(off_btn5)   # 5. 主題図一括出力
        vec_row_layout.addStretch()
        self._vector_button_row.setVisible(not vector_collapsed)
        vector_layout.addWidget(self._vector_button_row)

        offline_row.addWidget(vector_panel)

        # ── 栅格地形分析分组 ──
        raster_panel = QFrame(container)
        raster_panel.setObjectName("offlineGroupPanel")
        raster_panel.setStyleSheet(panel_style)
        raster_layout = QVBoxLayout(raster_panel)
        raster_layout.setContentsMargins(6, 4, 6, 4)
        raster_layout.setSpacing(2)

        ras_arrow = "▶" if raster_collapsed else "▼"
        self._raster_toggle_btn = QPushButton(
            f"{ras_arrow} 栅格地形分析", container
        )
        self._raster_toggle_btn.setObjectName("groupToggleBtn")
        self._raster_toggle_btn.setToolTip(
            "展开分组" if raster_collapsed else "折叠分组"
        )
        self._raster_toggle_btn.clicked.connect(
            lambda: self._toggle_offline_group("raster")
        )
        raster_layout.addWidget(self._raster_toggle_btn)

        self._raster_button_row = QWidget(container)
        ras_row_layout = QHBoxLayout(self._raster_button_row)
        ras_row_layout.setContentsMargins(0, 0, 0, 0)
        ras_row_layout.setSpacing(4)
        ras_row_layout.addWidget(off_btn2)   # 2. DEM水文解析
        ras_row_layout.addStretch()
        self._raster_button_row.setVisible(not raster_collapsed)
        raster_layout.addWidget(self._raster_button_row)

        offline_row.addWidget(raster_panel)
        offline_row.addStretch()
        main_layout.addLayout(offline_row)

        # P2 改造：隐藏快捷按钮区域
        self._offline_label.setVisible(False)
        vector_panel.setVisible(False)
        raster_panel.setVisible(False)

        # 断开所有离线按钮信号
        for btn in self._offline_buttons:
            try:
                btn.clicked.disconnect()
            except Exception:
                pass

        return container

    def _apply_styles(self) -> None:
        """应用现代化简约样式表。"""

        self.setStyleSheet(
            """
            QMainWindow {
                background: #f4f7fb;
            }
            QToolBar {
                background: #ffffff;
                border: none;
                spacing: 8px;
                padding: 8px 12px;
            }
            QToolButton {
                background: #e7edf6;
                border: 1px solid #d4dde8;
                border-radius: 8px;
                padding: 8px 14px;
            }
            #sidebarPanel, #aiConsole, #canvasPanel {
                background: #ffffff;
                border: 1px solid #dbe3ec;
                border-radius: 14px;
            }
            #panelTitle {
                font-size: 16px;
                font-weight: 600;
                color: #16202a;
            }
            #canvasStatus {
                color: #b54708;
                font-size: 14px;
                padding: 24px;
            }
            QTextEdit, #aiPromptInput, #aiResponseDisplay {
                background: #fbfdff;
                border: 1px solid #dbe3ec;
                border-radius: 10px;
                padding: 8px;
            }
            #layerTreeView {
                background: #fbfdff;
                border: 1px solid #dbe3ec;
                border-radius: 10px;
            }
            QPushButton {
                min-width: 100px;
                min-height: 40px;
                background: #10b981;
                color: #ffffff;
                border: none;
                border-radius: 10px;
                font-weight: 600;
                padding: 0 16px;
            }
            QPushButton:hover {
                background: #059669;
            }
            QStatusBar {
                background: #ffffff;
                color: #475467;
            }
            """
        )

    def _handle_pan(self) -> None:
        """激活平移模式。"""

        if self.map_canvas is None or self.pan_tool is None:
            self._show_qgis_error()
            return

        self.map_canvas.setMapTool(self.pan_tool)
        self.statusBar().showMessage("当前工具：平移（按住鼠标左键拖动地图）", 3000)

    def _handle_zoom_in(self) -> None:
        """激活放大模式。"""

        if self.map_canvas is None or self.zoom_in_tool is None:
            self._show_qgis_error()
            return

        self.map_canvas.setMapTool(self.zoom_in_tool)
        self.statusBar().showMessage("当前工具：放大（拖拽矩形区域）", 3000)

    def _handle_zoom_out(self) -> None:
        """激活缩小模式。"""

        if self.map_canvas is None or self.zoom_out_tool is None:
            self._show_qgis_error()
            return

        self.map_canvas.setMapTool(self.zoom_out_tool)
        self.statusBar().showMessage("当前工具：缩小（点击或拖拽矩形区域）", 3000)

    # ── P0/P1/P2 新增 handler 方法 ──────────────────────────

    def _handle_select_tool(self) -> None:
        """工具栏：激活框选要素工具。"""
        if self.map_canvas is None:
            self._show_qgis_error()
            return
        from qgis.utils import iface
        if iface is not None:
            iface.actionSelectRectangle().trigger()
            self.statusBar().showMessage("框选工具已激活 — 在地图上拖拽矩形选择要素", 3000)
        else:
            QMessageBox.information(self, "选择工具", "框选工具已激活，请在地图上拖拽矩形区域选择要素。")

    def _handle_toggle_edit_tool(self) -> None:
        """工具栏：切换当前活动矢量图层的编辑状态。"""
        from qgis.core import QgsVectorLayer
        from core.instruction_mapper import InstructionMapper

        layer = self._get_active_layer()
        if layer is None:
            QMessageBox.information(self, "编辑切换", "当前没有活动图层。")
            return
        if not isinstance(layer, QgsVectorLayer):
            QMessageBox.information(self, "编辑切换", "编辑操作仅支持矢量图层。")
            return

        mapper = InstructionMapper()
        result = mapper._handle_toggle_editing(layer_name=layer.name(), canvas=self.map_canvas)
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if result.get("success") and self.map_canvas:
            self.map_canvas.refresh()

    def _handle_reset_view(self) -> None:
        """视图 → 全图显示。"""
        if self.map_canvas is None:
            self._show_qgis_error()
            return
        self.map_canvas.zoomToFullExtent()
        self.map_canvas.refresh()
        self.statusBar().showMessage("已缩放至全图范围", 3000)

    # ── 文件菜单新增 ──

    def _on_export_attribute_menu(self) -> None:
        """文件菜单 → 导出属性表。"""
        from qgis.core import QgsVectorLayer
        from core.instruction_mapper import InstructionMapper

        layer = self._get_active_layer()
        if layer is None:
            QMessageBox.information(self, "导出属性表", "当前没有活动图层。")
            return
        if not isinstance(layer, QgsVectorLayer):
            QMessageBox.information(self, "导出属性表", "导出属性表仅支持矢量图层。")
            return

        from PyQt5.QtWidgets import QFileDialog
        default_name = f"{layer.name()}_属性表.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出属性表", default_name, "CSV 文件 (*.csv)",
        )
        if not path:
            return

        mapper = InstructionMapper()
        result = mapper._handle_export_attribute(
            layer_name=layer.name(), output_path=path, canvas=self.map_canvas,
        )
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if not result.get("success"):
            QMessageBox.warning(self, "导出失败", result.get("message", ""))

    def _on_load_style_menu(self) -> None:
        """文件菜单 → 加载 QML 样式文件。"""
        from core.instruction_mapper import InstructionMapper

        layer = self._get_active_layer()
        if layer is None:
            QMessageBox.information(self, "加载样式", "当前没有活动图层。")
            return

        from PyQt5.QtWidgets import QFileDialog
        path, _ = QFileDialog.getOpenFileName(
            self, "加载样式文件", "", "QML 样式文件 (*.qml)",
        )
        if not path:
            return

        mapper = InstructionMapper()
        result = mapper._handle_load_layer_style(
            layer_name=layer.name(), qml_path=path, canvas=self.map_canvas,
        )
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if not result.get("success"):
            QMessageBox.warning(self, "加载失败", result.get("message", ""))

    # ── 视图菜单新增 ──

    def _on_toggle_labels_menu(self) -> None:
        """视图菜单 → 全局标注开关。"""
        from qgis.core import QgsVectorLayer, QgsProject

        proj = QgsProject.instance()
        any_on = any(
            layer.labelsEnabled()
            for layer in proj.mapLayers().values()
            if isinstance(layer, QgsVectorLayer)
        )
        new_state = not any_on  # toggle: 如果任何标注开启→全部关闭，否则全部开启

        for layer in proj.mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                if new_state:
                    # 简化的标注开启：用第一个字符串字段
                    fields = layer.fields()
                    if fields.isEmpty():
                        continue
                    from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling
                    settings = QgsPalLayerSettings()
                    settings.fieldName = fields.at(0).name()
                    settings.isExpression = False
                    layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
                else:
                    layer.setLabeling(None)
                layer.triggerRepaint()

        if self.map_canvas:
            self.map_canvas.refresh()
        state_text = "开启" if new_state else "关闭"
        self.statusBar().showMessage(f"所有矢量图层标注已{state_text}", 3000)

    # ── 工具菜单新增 ──

    def _on_open_field_manager_menu(self) -> None:
        """工具菜单 → 字段管理器。"""
        from qgis.utils import iface
        layer = self._get_active_layer()
        if layer is None:
            QMessageBox.information(self, "字段管理", "当前没有活动图层。")
            return
        from qgis.core import QgsVectorLayer
        if not isinstance(layer, QgsVectorLayer):
            QMessageBox.information(self, "字段管理", "字段管理仅支持矢量图层。")
            return
        if iface is not None:
            iface.setActiveLayer(layer)
            iface.actionManageFields().trigger()
        else:
            QMessageBox.information(
                self, "字段管理",
                f"请在 QGIS 桌面中手动打开图层「{layer.name()}」的字段管理器。",
            )

    def _on_layer_statistic_menu(self) -> None:
        """工具菜单 → 要素统计。"""
        from core.instruction_mapper import InstructionMapper

        layer = self._get_active_layer()
        if layer is None:
            QMessageBox.information(self, "要素统计", "当前没有活动图层。")
            return
        from qgis.core import QgsVectorLayer
        if not isinstance(layer, QgsVectorLayer):
            QMessageBox.information(self, "要素统计", "要素统计仅支持矢量图层。")
            return

        mapper = InstructionMapper()
        result = mapper._handle_layer_statistic(
            layer_name=layer.name(), method="count",
        )
        if result.get("success"):
            QMessageBox.information(self, f"要素统计 — {layer.name()}", result.get("message", ""))
        else:
            QMessageBox.warning(self, "统计失败", result.get("message", ""))

    def _on_create_buffer_menu(self) -> None:
        """工具菜单 → 缓冲区分析。"""
        from core.instruction_mapper import InstructionMapper

        layer = self._get_active_layer()
        if layer is None:
            QMessageBox.information(self, "缓冲区分析", "当前没有活动图层。")
            return
        from qgis.core import QgsVectorLayer
        if not isinstance(layer, QgsVectorLayer):
            QMessageBox.information(self, "缓冲区分析", "缓冲区分析仅支持矢量图层。")
            return

        distance, ok = QInputDialog.getDouble(
            self, "缓冲区分析", "缓冲距离（单位与图层坐标系一致）：", 100.0, 0.001, 999999, 3,
        )
        if not ok:
            return

        mapper = InstructionMapper()
        result = mapper._handle_create_buffer(
            layer_name=layer.name(), distance=distance, canvas=self.map_canvas,
        )
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if self.map_canvas:
            self.map_canvas.refresh()

    def _on_batch_style_menu(self) -> None:
        """工具菜单 → 批量样式设置。"""
        from qgis.core import QgsVectorLayer, QgsProject
        from PyQt5.QtWidgets import QColorDialog

        color = QColorDialog.getColor(QColor("#3388ff"), self, "选择渲染颜色")
        if not color.isValid():
            return

        layer_count = 0
        proj = QgsProject.instance()
        for layer in proj.mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                from qgis.core import (
                    QgsSingleSymbolRenderer, QgsFillSymbol,
                    QgsLineSymbol, QgsMarkerSymbol,
                )
                geom_type = layer.geometryType()
                if geom_type == 0:  # Point
                    symbol = QgsMarkerSymbol.createSimple({})
                elif geom_type == 1:  # Line
                    symbol = QgsLineSymbol.createSimple({})
                else:  # Polygon
                    symbol = QgsFillSymbol.createSimple({})
                symbol.setColor(color)
                layer.setRenderer(QgsSingleSymbolRenderer(symbol))
                layer.triggerRepaint()
                layer_count += 1

        if self.map_canvas:
            self.map_canvas.refresh()
        self.statusBar().showMessage(f"已对 {layer_count} 个矢量图层应用统一颜色", 5000)

    # ── 图层右键菜单新增回调 ──

    def _on_toggle_edit_layer(self, layer) -> None:
        """右键菜单 → 开启/关闭编辑。"""
        from core.instruction_mapper import InstructionMapper
        mapper = InstructionMapper()
        result = mapper._handle_toggle_editing(layer_name=layer.name(), canvas=self.map_canvas)
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if self.map_canvas:
            self.map_canvas.refresh()

    def _on_layer_style_menu(self, layer) -> None:
        """右键菜单 → 图层样式设置。"""
        from PyQt5.QtWidgets import QColorDialog, QInputDialog
        from qgis.core import QgsVectorLayer

        if isinstance(layer, QgsVectorLayer):
            render_type, ok = QInputDialog.getItem(
                self, "图层样式设置 — " + layer.name(),
                "渲染类型：", ["single", "categorized", "graduated"], 0, False,
            )
            if not ok:
                return
            color = QColorDialog.getColor(QColor("#3388ff"), self, "选择颜色")
            color_hex = color.name() if color.isValid() else "#3388ff"

            from core.instruction_mapper import InstructionMapper
            mapper = InstructionMapper()
            # 对 categorized/graduated 尝试第一个字符串字段
            field_name = ""
            if render_type in ("categorized", "graduated"):
                fields = layer.fields()
                field_name = fields.at(0).name() if not fields.isEmpty() else ""
            result = mapper._handle_set_layer_style(
                layer_name=layer.name(), render_type=render_type,
                color=color_hex, field_name=field_name, canvas=self.map_canvas,
            )
            self.statusBar().showMessage(result.get("message", ""), 5000)
        else:
            # 栅格图层简单样式
            from core.instruction_mapper import InstructionMapper
            mapper = InstructionMapper()
            result = mapper._handle_set_layer_style(
                layer_name=layer.name(), render_type="single",
                color="#000000", canvas=self.map_canvas,
            )
            self.statusBar().showMessage(result.get("message", ""), 5000)
        if self.map_canvas:
            self.map_canvas.refresh()

    def _on_toggle_labels_layer(self, layer) -> None:
        """右键菜单 → 显示/隐藏标注。"""
        from core.instruction_mapper import InstructionMapper
        mapper = InstructionMapper()
        if layer.labelsEnabled():
            # 关闭标注
            result = mapper._handle_add_label(layer_name=layer.name(), canvas=self.map_canvas)
        else:
            # 开启标注：用第一个字符串字段
            fields = layer.fields()
            field = ""
            for f in fields:
                if f.typeName() in ("String", "QString", "string"):
                    field = f.name()
                    break
            if not field and not fields.isEmpty():
                field = fields.at(0).name()
            result = mapper._handle_add_label(
                layer_name=layer.name(), field=field, canvas=self.map_canvas,
            )
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if self.map_canvas:
            self.map_canvas.refresh()

    def _on_filter_layer(self, layer) -> None:
        """右键菜单 → 设置属性过滤。"""
        from PyQt5.QtWidgets import QInputDialog, QLineEdit
        lm = lang_manager()
        expr, ok = QInputDialog.getText(
            self, lm.tr("dialog_title_filter"),
            lm.tr("dialog_label_filter"), QLineEdit.Normal, layer.subsetString(),
        )
        if not ok:
            return
        from core.instruction_mapper import InstructionMapper
        mapper = InstructionMapper()
        result = mapper._handle_filter_layer(
            layer_name=layer.name(), expression=expr.strip(),
        )
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if self.map_canvas:
            self.map_canvas.refresh()

    def _on_field_manager_layer(self, layer) -> None:
        """右键菜单 → 字段管理。"""
        from qgis.utils import iface
        if iface is not None:
            iface.setActiveLayer(layer)
            iface.actionManageFields().trigger()
        else:
            QMessageBox.information(
                self, "字段管理",
                f"请在 QGIS 桌面中手动打开图层「{layer.name()}」的字段管理器。",
            )

    def _on_export_attribute_layer(self, layer) -> None:
        """右键菜单 → 导出属性表。"""
        from core.instruction_mapper import InstructionMapper
        from PyQt5.QtWidgets import QFileDialog

        default_name = f"{layer.name()}_属性表.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出属性表", default_name, "CSV 文件 (*.csv)",
        )
        if not path:
            return

        mapper = InstructionMapper()
        result = mapper._handle_export_attribute(
            layer_name=layer.name(), output_path=path, canvas=self.map_canvas,
        )
        self.statusBar().showMessage(result.get("message", ""), 5000)
        if not result.get("success"):
            QMessageBox.warning(self, "导出失败", result.get("message", ""))

    def _on_statistic_layer(self, layer) -> None:
        """右键菜单 → 要素统计。"""
        from core.instruction_mapper import InstructionMapper
        mapper = InstructionMapper()
        result = mapper._handle_layer_statistic(
            layer_name=layer.name(), method="all",
        )
        if result.get("success"):
            QMessageBox.information(
                self, f"要素统计 — {layer.name()}", result.get("message", ""),
            )
        else:
            QMessageBox.warning(self, "统计失败", result.get("message", ""))

    def _handle_run_clicked(self) -> None:
        """将当前 AI 指令发送至后台工作线程。"""

        # 每次运行前清空上一轮的代码缓存，确保使用最新 Prompt 重新请求 API
        self.last_ai_code = ""
        self.skip_preview = False

        user_text = self.ai_prompt_input.toPlainText().strip()
        if not user_text:
            QMessageBox.warning(
                self,
                "缺少分析指令",
                "请输入地理空间分析指令后再运行。",
            )
            return

        # 本地关键词预检：仅对简单"打开属性表"请求直接路由（无图层指定）
        table_keywords = ["属性表", "查看属性", "查看数据", "看属性", "打开表", "表格"]
        if any(kw in user_text for kw in table_keywords):
            # 如果用户指定了具体图层（如"刚刚生成的"、"质心"等），交给 AI 流水线处理
            layer_specifiers = ["刚刚", "上一步", "生成", "质心", "裁剪", "缓冲", "新", "那个"]
            if not any(ls in user_text for ls in layer_specifiers):
                mgr = get_skill_manager()
                result = mgr.execute_skill("open_table", active_layer=self._get_active_layer(),
                                           layer_tree=self.layer_tree_view, arguments=user_text)
                if result.get("success") and result.get("layer"):
                    self._open_attribute_table(result["layer"])
                else:
                    QMessageBox.information(self, "提示", result.get("message", "无法打开属性表"))
                return

        layer_metadata = self._collect_layer_metadata()
        if not layer_metadata:
            QMessageBox.warning(
                self,
                "缺少图层数据",
                "当前没有可分析的图层，请先拖拽加载至少一个图层。",
            )
            return

        self.run_button.setEnabled(False)
        self.screenshot_button.setEnabled(False)
        self.statusBar().showMessage("AI 正在思考并计算中，请稍候...")

        # Phase 5.1: 传递 ProjectManager 和活动图层名称给 Worker，
        # 使 Worker 能在 API 调用前执行实时 QGIS 状态同步（Perception-Action Loop）
        active_layer = self._get_active_layer()
        active_layer_name = active_layer.name() if active_layer else ""
        self.ai_worker = AIProcessingWorker(
            user_text, layer_metadata,
            project_manager=self.project_manager,
            active_layer_name=active_layer_name,
            viewport_snapshots=self._pending_multimodal_data,
            canvas=getattr(self, 'map_canvas', None),
            mapper=self._mapper,
        )
        self._pending_multimodal_data = None
        self.ai_worker.pipeline_ready.connect(self._execute_pipeline)
        self.ai_worker.failed.connect(self._handle_ai_error)
        self.ai_worker.finished.connect(self._reset_ai_worker_state)
        self.ai_worker.start()

    def _on_canvas_screenshot_clicked(self) -> None:
        """Phase 4：画布截图分析按钮回调。

        截取当前 QGIS 画布视口，将截图（含空间元数据）与用户 Prompt
        一并注入 AIWorker，走 Vision API 多模态管线。
        """
        user_text = self.ai_prompt_input.toPlainText().strip()
        if not user_text:
            QMessageBox.warning(
                self,
                "缺少分析指令",
                "请输入地理空间分析指令后再运行。",
            )
            return

        if self.map_canvas is None:
            QMessageBox.warning(self, "画布不可用", "QGIS 地图画布未初始化。")
            return

        # 每次运行前清空上一轮的代码缓存
        self.last_ai_code = ""
        self.skip_preview = False

        # 捕获画布视口（含 spatial_context 元数据）
        try:
            snapshot = CanvasCapture.capture_viewport(self.map_canvas)
            self._pending_multimodal_data = [snapshot]
            _log.info(
                "画布截图成功，CRS=%s，尺寸=%d",
                snapshot.get("spatial_context", {}).get("crs", "?"),
                len(snapshot.get("image_base64", "")),
            )
        except Exception as e:
            _log.error("画布截图失败：%s", e)
            QMessageBox.warning(self, "截图失败", f"无法截取画布：{e}")
            return

        # 复用已有的纯文本执行管线，注入多模态数据
        self._handle_run_clicked()

    def _show_qgis_error(self) -> None:
        """当 QGIS 不可用时显示中文错误对话框。"""

        QMessageBox.critical(
            self,
            "QGIS 环境不可用",
            self.bootstrap_result.message,
        )

    def _load_dropped_files(self, file_paths: List[str]) -> None:
        """加载拖放的图层文件并立即刷新地图画布。"""

        try:
            # 检查是否有表格文件（Excel/CSV）
            table_files = [fp for fp in file_paths if is_table_path(fp)]
            
            # 如果有表格文件，自动触发 AI 指令
            if table_files:
                for table_file in table_files:
                    # 自动向 AI 线程触发指令
                    self._auto_trigger_table_analysis(table_file)
                
                # 表格文件不加载为图层，直接返回
                self.statusBar().showMessage(
                    f"已识别 {len(table_files)} 个表格文件，正在启动 AI 分析...",
                    5000,
                )
                return
            
            # 正常加载 GIS 图层文件
            loaded_layers, errors = load_layers_from_paths(file_paths)

            if not loaded_layers and errors:
                QMessageBox.warning(
                    self,
                    "图层加载失败",
                    "\n".join(errors),
                )
                return

            if loaded_layers:
                self._zoom_to_layers(loaded_layers)
                self.statusBar().showMessage(
                    f"已成功加载 {len(loaded_layers)} 个图层。",
                    5000,
                )

            if errors:
                QMessageBox.warning(
                    self,
                    "部分文件未加载",
                    "\n".join(errors),
                )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "拖拽加载异常",
                f"处理拖拽文件时发生错误：{exc}",
            )

    def _auto_trigger_table_analysis(self, table_file: str) -> None:
        """拖入表格文件时自动向 AI 线程触发空间化分析指令。

        参数
        ----
        table_file : str
            表格文件（.xlsx / .xls / .csv）的绝对路径。
        """

        # P1 改造：离线模式拦截
        if self.offline_mode:
            self.statusBar().showMessage(
                "离线模式：表格文件拖入已忽略。请切换至在线模式使用 AI 表格解析。", 5000
            )
            return

        # 清空上一轮的代码缓存，确保从最新的 Prompt 重新生成代码
        self.last_ai_code = ""
        self.skip_preview = False

        prompt = f"我拖入了表格文件，路径是: {table_file}，请帮我把它解析并建立点数据。"

        # 填充 AI 输入框，让用户可见触发了什么指令
        self.ai_prompt_input.setPlainText(prompt)

        # 无底图画布托管：项目为空时设置全局默认坐标系
        project = QgsProject.instance()
        if project.count() == 0:
            project.setCrs(QgsCoordinateReferenceSystem('EPSG:4612'))
            _log.info("项目为空，已设置全局坐标系为 EPSG:4612 (JGD2000)")

        # 收集当前画布上的图层元数据（即使为空也仍然派发 AI 任务）
        layer_metadata = self._collect_layer_metadata()
        active_layer = self._get_active_layer()
        active_layer_name = active_layer.name() if active_layer else ""

        self.run_button.setEnabled(False)
        self.screenshot_button.setEnabled(False)
        self.statusBar().showMessage("AI 正在解析表格并建立点数据，请稍候...")

        self.ai_worker = AIProcessingWorker(
            prompt, layer_metadata,
            project_manager=self.project_manager,
            active_layer_name=active_layer_name,
            viewport_snapshots=None,
            canvas=getattr(self, 'map_canvas', None),
            mapper=self._mapper,
        )
        self.ai_worker.pipeline_ready.connect(self._execute_pipeline)
        self.ai_worker.failed.connect(self._handle_ai_error)
        self.ai_worker.finished.connect(self._reset_ai_worker_state)
        self.ai_worker.start()

    def _zoom_to_layers(self, layers: List[object]) -> None:
        """将地图画布缩放至新加载图层的合并范围。"""

        if self.map_canvas is None:
            return

        combined_extent = None
        for layer in layers:
            layer_extent = layer.extent()
            if layer_extent.isEmpty():
                continue

            if combined_extent is None:
                combined_extent = QgsRectangle(layer_extent)
            else:
                combined_extent.combineExtentWith(layer_extent)

        if combined_extent is not None:
            self.map_canvas.setExtent(combined_extent)

        self.map_canvas.refresh()

    def _open_attribute_table(self, layer) -> None:
        """打开矢量图层属性表查看器。"""
        dialog = AttributeTableDialog(layer, self)
        dialog.exec_()

    def _zoom_to_layer(self, layer) -> None:
        """缩放到指定图层范围（右键菜单回调）。"""
        if layer is None or not self.map_canvas:
            return
        extent = layer.extent()
        if not extent.isEmpty():
            self.map_canvas.setExtent(extent)
            self.map_canvas.refresh()

    def _remove_layer_node(self, node) -> None:
        """移除指定图层节点（右键菜单回调）。"""
        layer = node.layer()
        if layer is None:
            return
        reply = QMessageBox.question(
            self,
            "确认移除",
            f"确定要移除图层「{layer.name()}」吗？\n此操作不会删除原始文件。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        parent = node.parent()
        if parent:
            parent.removeChildNode(node)
        self.map_canvas.refresh()
        self.statusBar().showMessage(f"已移除图层：{layer.name()}", 3000)

    def _remove_layer_from_menu(self, layer) -> None:
        """移除图层（右键菜单回调，接收 QgsMapLayer 而非 QgsLayerTreeLayer）。"""
        node = QgsProject.instance().layerTreeRoot().findLayer(layer.id())
        if node is not None:
            self._remove_layer_node(node)

    def _rename_layer(self, layer, new_name: str) -> None:
        """重命名图层（右键菜单回调）。"""
        old_name = layer.name()
        layer.setName(new_name)
        self.statusBar().showMessage(f"已重命名：{old_name} → {new_name}", 3000)
        self.map_canvas.refresh()

    def _copy_layer(self, layer) -> None:
        """复制图层（右键菜单回调）。"""
        if isinstance(layer, QgsVectorLayer):
            geom_type_str = QgsWkbTypes.displayString(layer.wkbType())
            crs = layer.crs().authid()
            uri = f"{geom_type_str}?crs={crs}"
            for field in layer.fields():
                uri += f"&field={field.name()}:{field.typeName()}"
            new_layer = QgsVectorLayer(uri, f"{layer.name()} (副本)", "memory")
            features = list(layer.getFeatures())
            if features:
                new_layer.dataProvider().addFeatures(features)
                new_layer.updateExtents()
            QgsProject.instance().addMapLayer(new_layer)
            self.map_canvas.refresh()
            self.statusBar().showMessage(f"已复制图层：{new_layer.name()}", 3000)
        else:
            QMessageBox.information(self, "提示", "暂不支持复制栅格图层。")

    def _remove_selected_layer(self) -> None:
        """使用 QGIS 原生 LayerTree API 移除图层。"""
        current = self._get_selected_layer_node()
        if not current:
            QMessageBox.information(self, "提示", "请先在图层列表中选中一个图层。")
            return
        layer = current.layer()
        if layer is None:
            return
        reply = QMessageBox.question(
            self,
            "确认移除",
            f"确定要移除图层「{layer.name()}」吗？\n此操作不会删除原始文件。",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return
        # QGIS 原生方式：从父节点移除子节点（QGIS 内部就是这样做的）
        parent = current.parent()
        if parent:
            parent.removeChildNode(current)
        self.map_canvas.refresh()
        self.statusBar().showMessage(f"已移除图层：{layer.name()}", 3000)

    def _zoom_to_selected_layer(self) -> None:
        """缩放到当前选中图层的范围。"""
        current = self._get_selected_layer_node()
        if not current:
            return
        layer = current.layer()
        if layer is None or not self.map_canvas:
            return
        extent = layer.extent()
        if not extent.isEmpty():
            self.map_canvas.setExtent(extent)
            self.map_canvas.refresh()

    def _zoom_in(self) -> None:
        """放大一级。"""
        if self.map_canvas:
            self.map_canvas.zoomIn()

    def _zoom_out(self) -> None:
        """缩小一级。"""
        if self.map_canvas:
            self.map_canvas.zoomOut()

    def _zoom_full(self) -> None:
        """缩放到所有图层的完整范围。"""
        if self.map_canvas:
            self.map_canvas.zoomToFullExtent()

    def _get_selected_layer_node(self):
        """获取当前选中的图层树节点。"""
        if not self.layer_tree_view:
            return None
        try:
            node = self.layer_tree_view.currentNode()
            if node and hasattr(node, 'layer') and node.layer():
                return node
        except Exception:
            pass
        if self.layer_tree_model:
            try:
                sm = self.layer_tree_view.selectionModel()
                if sm and sm.selectedIndexes():
                    idx = sm.selectedIndexes()[0]
                    node = self.layer_tree_model.index2node(idx)
                    if node and hasattr(node, 'layer') and node.layer():
                        return node
            except Exception:
                pass
        return None

    def _collect_layer_metadata(self) -> List[Dict[str, Any]]:
        """收集当前已加载图层的元数据供 AI 工作线程使用。"""

        active_layer = self._get_active_layer()
        metadata = []
        for layer in QgsProject.instance().mapLayers().values():
            metadata.append(
                {
                    "name": layer.name(),
                    "type": self._layer_type_name(layer),
                    "path": layer.source(),
                    "provider": layer.providerType(),
                    "is_active": active_layer is not None and layer.id() == active_layer.id(),
                }
            )

        return metadata

    def _get_active_layer(self):
        """返回当前活动图层（若存在）。"""

        if self.layer_tree_view is not None and hasattr(self.layer_tree_view, "currentLayer"):
            layer = self.layer_tree_view.currentLayer()
            if layer is not None:
                return layer

        layers = list(QgsProject.instance().mapLayers().values())
        return layers[0] if layers else None

    @staticmethod
    def _layer_type_name(layer: QgsMapLayer) -> str:
        """将图层实例转换为可读的类型名称。"""

        if layer.type() == QgsMapLayer.VectorLayer:
            return "矢量图层"
        if layer.type() == QgsMapLayer.RasterLayer:
            return "栅格图层"

        return "未知类型"

    def _execute_pipeline(self, pipeline: list) -> None:
        """执行 AI 规划的技能流水线。

        遍历流水线任务列表，通过 SkillManager 顺序执行每个技能。
        前序步骤的输出图层自动传递给后续步骤（pipeline_context）。
        执行完毕后更新对话记忆。

        Parameters
        ----------
        pipeline : list of dict
            每个元素包含 skill / arguments / reasoning。
        """



        if not pipeline:
            QMessageBox.information(self, "AI 响应", "流水线为空，无任务执行。")
            return

        # 显示流水线概览
        steps_text = " → ".join(
            f"[{i+1}] {step.get('skill', '?')}" for i, step in enumerate(pipeline)
        )
        self.ai_response_display.setPlainText(
            json.dumps(pipeline, ensure_ascii=False, indent=2)
        )
        _log.info("流水线执行开始：%s", steps_text)
        self.statusBar().showMessage(f"流水线：{steps_text}", 5000)

        # 流水线上下文：在步骤间传递中间结果
        pipeline_context: Dict[str, Any] = {}
        mgr = get_skill_manager()
        user_text = self.ai_prompt_input.toPlainText().strip()
        all_success = True
        summary_parts: List[str] = []

        for i, step in enumerate(pipeline):
            skill_name = step.get("skill", "unknown")
            arguments = step.get("arguments", "")
            reasoning = step.get("reasoning", "")

            _log.info("流水线 [%d/%d]：%s — %s", i + 1, len(pipeline), skill_name, reasoning)

            # P2 改造：离线模式本地大模型响应
            if skill_name == "_offline_response":
                try:
                    offline_args = json.loads(arguments) if isinstance(arguments, str) else arguments
                    offline_msg = offline_args.get("message", "")
                    offline_success = offline_args.get("success", False)
                    output_file = offline_args.get("output_file", "")
                except Exception:
                    offline_msg = str(arguments)
                    offline_success = False
                    output_file = ""
                self.ai_response_display.setPlainText(offline_msg)
                self.statusBar().showMessage(
                    "离线模式 — 指令已执行" if offline_success else "离线模式 — 问答",
                    5000,
                )
                # 自动加载管道输出的图层文件
                if output_file and os.path.exists(output_file):
                    self._add_layer_from_file(output_file)
                append_to_history("user", user_text)
                append_to_history("assistant", offline_msg[:500])
                return

            if skill_name == "unknown":
                msg = reasoning or "AI 无法识别该指令。"
                QMessageBox.information(self, f"流水线 [{i+1}]", msg)
                all_success = False
                summary_parts.append(f"❌ [{i+1}] {skill_name}: {msg}")
                break

            # 注入流水线上下文到参数
            if pipeline_context and arguments:
                ctx_desc = "，".join(
                    f"{k}: {v}" for k, v in pipeline_context.items()
                )
                arguments = f"上下文（{ctx_desc}）。{arguments}"

            # 执行技能
            # 主画布物理图层注入：从 canvas 提取活图层传给技能
            active_layers = None
            if self.map_canvas and hasattr(self.map_canvas, 'layers'):
                active_layers = [lyr for lyr in self.map_canvas.layers() if lyr.isValid()]

            result = mgr.execute_skill(
                skill_name,
                canvas=self.map_canvas,
                layer_tree=self.layer_tree_view,
                arguments=arguments,
                active_layer=self._get_active_layer(),
                main_window=self,
                pipeline_context=pipeline_context,
                active_layers=active_layers,
            )

            if not result.get("success"):
                err_msg = result.get("message", "未知错误")
                QMessageBox.information(
                    self, f"流水线 [{i+1}] {skill_name}", err_msg
                )
                all_success = False
                summary_parts.append(f"❌ [{i+1}] {skill_name}: {err_msg}")
                break

            # 处理技能结果
            if skill_name == "open_table":
                layer = result.get("layer")
                if layer:
                    self._open_attribute_table(layer)

            elif skill_name == "open_project":
                # 项目加载后清空对话记忆，全量刷新画布
                from core.ai_worker import clear_conversation_history
                clear_conversation_history()
                loaded = result.get("loaded_layers", [])
                if loaded and self.map_canvas:
                    combined_extent = None
                    for lyr in loaded:
                        ext = lyr.extent()
                        if not ext.isEmpty():
                            if combined_extent is None:
                                combined_extent = lyr.extent()
                            else:
                                combined_extent.combineExtentWith(ext)
                    if combined_extent:
                        self.map_canvas.setExtent(combined_extent)
                    self.map_canvas.refresh()
                layer_names = result.get("layer_names", [])
                summary_parts.append(
                    f"✅ [{i+1}] {skill_name} → 加载了 {len(loaded)} 个图层"
                )
                if layer_names:
                    pipeline_context["last_output_layers"] = layer_names
                    pipeline_context["last_output_layer"] = layer_names[0] if layer_names else ""
                    pipeline_context["project_path"] = result.get("project_path", "")

            elif skill_name == "layer_styling":
                styled_layers = result.get("styled_layers", [])
                names = ", ".join(l.name() for l in styled_layers) if styled_layers else "?"
                summary_parts.append(f"✅ [{i+1}] {skill_name} → {names}")

            elif skill_name == "map_export":
                summary_parts.append(
                    f"✅ [{i+1}] {skill_name} → {result.get('message', '完成')}"
                )

            else:
                summary_parts.append(
                    f"✅ [{i+1}] {skill_name} → {result.get('message', '完成')}"
                )

            # 收集新增图层并传递到上下文（open_project 已在上面单独处理）
            if skill_name != "open_project":
                added = result.get("added_layers", [])
                if added:
                    self._zoom_to_layers(added)
                    layer_names = [lyr.name() for lyr in added]
                    pipeline_context["last_output_layers"] = layer_names
                    pipeline_context["last_output_layer"] = layer_names[0]
                    summary_parts[-1] += f"（新增: {', '.join(layer_names)}）"

            # 传递技能可能返回的其他上下文
            for ctx_key in ("output_path", "output_layer_name"):
                if ctx_key in result:
                    pipeline_context[ctx_key] = result[ctx_key]

        # 流水线完成，写入对话记忆
        final_summary = "\n".join(summary_parts) if summary_parts else "流水线已完成。"
        self.ai_response_display.append(f"\n--- 流水线结果 ---\n{final_summary}")

        if all_success:
            self.statusBar().showMessage(f"流水线完成（{len(pipeline)} 步）", 6000)
        else:
            self.statusBar().showMessage("流水线部分失败", 6000)

        # 更新对话记忆（写入确定性的系统状态通知）
        append_to_history("user", user_text)
        append_to_history("assistant", final_summary)
        append_to_history(
            "system",
            "[System Notification]: Skill executed successfully. Current active layer tree updated."
        )
        persist_conversation_turn()

        # 自动保存 .qgz 工程
        if all_success:
            project_path = style_manager.save_project()
            if project_path:
                self.statusBar().showMessage(
                    "AIQGIS 已自动完成高颜值制图！可直接双击打开工程文件排版打印。",
                    8000,
                )

        _log.info("流水线执行结束，记忆已持久化")

    def _handle_ai_response(self, response_text: str) -> None:
        """解析 AI JSON 路由指令（旧版兼容，单对象格式）。"""

        self.ai_response_display.setPlainText(response_text)

        try:
            route = parse_agent_response(response_text)
            skill_name = route.get("skill", "unknown")
            reasoning = route.get("reasoning", "")
            arguments = route.get("arguments", "")

            _log.info("AI 路由 → %s，理由：%s", skill_name, reasoning)
            self.statusBar().showMessage(f"路由：{skill_name} — {reasoning}", 5000)

            if skill_name == "unknown":
                QMessageBox.information(self, "AI 响应", reasoning or "无法识别该指令。")
                return

            # Phase 5 Block 1: spatial_analysis 已不再生成代码，统一走 skill_manager 或 instruction_mapper
            mgr = get_skill_manager()
            result = mgr.execute_skill(
                skill_name,
                canvas=self.map_canvas,
                layer_tree=self.layer_tree_view,
                arguments=arguments,
                active_layer=self._get_active_layer(),
                main_window=self,
            )

            if not result.get("success"):
                QMessageBox.information(self, "技能返回", result.get("message", "未知错误"))
                return

            if skill_name == "open_table":
                layer = result.get("layer")
                if layer:
                    self._open_attribute_table(layer)
            elif skill_name == "layer_styling":
                self.statusBar().showMessage(result.get("message", "样式已应用"), 5000)
            elif skill_name == "map_export":
                self.statusBar().showMessage(result.get("message", "导出完成"), 5000)

            added = result.get("added_layers", [])
            if added:
                self._zoom_to_layers(added)

        except Exception as exc:
            _log.exception("AI 响应处理异常，进入回退执行")
            self._fallback_legacy_execution(response_text, exc)


    # ══════════════════════════════════════════════════════════
    # SandboxExecutionWorker 信号槽网络（Pain 2 自愈循环）
    # ══════════════════════════════════════════════════════════

    def _launch_sandbox_worker(
        self,
        code: str,
        active_layer=None,
        layers_by_name=None,
        user_text: str = "",
    ) -> None:
        """创建并启动 SandboxExecutionWorker（异步，不阻塞 UI 线程）。

        Phase 5 Block 1: 此方法已停用于在线模式。在线 LLM 不再生成 PyQGIS
        代码，统一走 InstructionMapper.match_and_execute() 确定性执行。
        
        保留以备审计。误调用时抛出 NotImplementedError。
        """
        raise NotImplementedError(
            "Phase 5 Block 1 后 Sandbox 已停用于在线模式。"
            "在线/离线统一走 InstructionMapper.match_and_execute()。"
        )
        import builtins
        import processing

        # 防御：自动清洗 AI 误输出的 iface 引用（独立应用无 iface）
        if re.search(r'\biface\b', code):
            _log.warning("AI 代码含 iface 引用，自动清洗")
            code = re.sub(
                r'iface\.addMapLayer\((\w+)\)',
                r'QgsProject.instance().addMapLayer(\1)',
                code
            )
            code = re.sub(
                r'iface\.activeLayer\(\)',
                r'active_layer',
                code
            )
            code = re.sub(
                r'iface\.mapCanvas\(\).refresh(AllLayers)?\(\)',
                r'pass  # iface.mapCanvas() stripped',
                code
            )
            code = re.sub(
                r'^\s*iface\..+$',
                r'pass  # auto-stripped iface call',
                code,
                flags=re.MULTILINE
            )

        self._sandbox_retry_count = 0
        self._sandbox_force_numpy_gdal = False
        self._sandbox_original_code = code
        self._sandbox_user_text = user_text

        exec_globals = {
            "__builtins__": builtins.__dict__,
            "processing": processing,
            "QgsProject": QgsProject,
            "QgsVectorLayer": QgsVectorLayer,
            "QgsRasterLayer": QgsRasterLayer,
            "QgsMapLayer": QgsMapLayer,
            "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
            "QgsCoordinateTransform": QgsCoordinateTransform,
            "QgsFeature": QgsFeature,
            "QgsGeometry": QgsGeometry,
            "QgsPointXY": QgsPointXY,
            "QgsField": QgsField,
            "QgsFields": QgsFields,
            "active_layer": active_layer,
            "layers_by_name": layers_by_name or {},
            "generate_output_path": generate_output_path,
            "generate_geojson_output_path": generate_geojson_output_path,
            "style_manager": style_manager,
            "os": os,
            "tempfile": tempfile,
        }

        # ── PROJ_LIB 兜底：沙箱内 numpy/gdal 手写算法需要 proj.db ──
        _proj_lib = os.environ.get("PROJ_LIB", "")
        if not _proj_lib:
            # 运行时推导 — 不再硬编码绝对路径
            _project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            _proj_lib = os.path.join(_project_root, "qgis-portable", "share", "proj")
            if os.path.isdir(_proj_lib):
                os.environ["PROJ_LIB"] = _proj_lib
                exec_globals["_proj_lib_path"] = _proj_lib

        self._create_and_start_worker(code, exec_globals, active_layer, layers_by_name, 0)

    def _create_and_start_worker(
        self,
        code: str,
        exec_globals: Dict[str, Any],
        active_layer,
        layers_by_name,
        retry_count: int,
    ) -> None:
        """实例化 SandboxExecutionWorker、连接信号并启动。"""
        if self._sandbox_worker is not None:
            # 断开旧 Worker 所有信号，防止 stale callback
            for sig_name in ("progress", "stdout_line", "finished", "error", "fix_needed"):
                sig = getattr(self._sandbox_worker, sig_name, None)
                if sig is not None:
                    try:
                        sig.disconnect()
                    except TypeError:
                        pass

            if self._sandbox_worker.isRunning():
                self._sandbox_worker.quit()
                self._sandbox_worker.wait(500)

            self._sandbox_worker.deleteLater()
            self._sandbox_worker = None

        self._sandbox_worker = SandboxExecutionWorker(
            code=code,
            exec_globals=exec_globals,
            active_layer=active_layer,
            layers_by_name=layers_by_name or {},
            user_query=self._sandbox_user_text,
            retry_count=retry_count,
        )
        self._sandbox_worker.progress.connect(self._on_sandbox_progress)
        self._sandbox_worker.stdout_line.connect(self._on_sandbox_stdout)
        self._sandbox_worker.finished.connect(self._on_sandbox_finished)
        self._sandbox_worker.error.connect(self._on_sandbox_error)
        self._sandbox_worker.fix_needed.connect(self._on_sandbox_fix_needed)
        self._sandbox_worker.start()

    # ── 信号槽 ──

    def _on_sandbox_progress(self, msg: str) -> None:
        """Worker 进度通知 → 状态栏。"""
        self.statusBar().showMessage(msg, 3000)

    def _on_sandbox_stdout(self, line: str) -> None:
        """Worker 中 print() 输出 → AI 控制台实时回显。"""
        self.ai_response_display.append(f"[沙箱] {line}")

    # ══════════════════════════════════════════════════════════
    # 任务编排器（复合任务串行传送带）
    # ══════════════════════════════════════════════════════════

    def _start_task_pipeline(self, tasks: List[dict]) -> None:
        """初始化复合任务流水线并启动第一步。

        将 LLM 拆解的 JSON 任务队列写入内部状态，
        生成步骤 1 的单任务提示词并异步请求 PyQGIS 代码。
        """
        total = len(tasks)
        self._task_pipeline = tasks
        self._task_pipeline_index = 0
        self._task_pipeline_user_text = self.ai_prompt_input.toPlainText().strip()
        self._task_pipeline_layer_metadata = self._collect_layer_metadata()
        self._task_pipeline_outputs = {}

        descs = " → ".join(
            f"[{t['step']}] {t.get('description', t.get('action', '?'))}"
            for t in tasks
        )
        _log.info("启动复合任务流水线（%d 步）：%s", total, descs)
        self.statusBar().showMessage(f"[1/{total}] {tasks[0].get('description', '正在执行...')}")

        self._execute_next_pipeline_step()

    def _execute_next_pipeline_step(self) -> None:
        """Phase 5 Block 1: 此方法已停用。在线模式不再生成 PyQGIS 代码，
        统一走 InstructionMapper.match_and_execute() 确定性执行。
        
        保留以备审计。
        """
        raise NotImplementedError(
            "Phase 5 Block 1 后复合任务流水线已停用。"
            "在线/离线统一走 InstructionMapper.match_and_execute()。"
        )

    def _build_single_task_prompt(self, step: dict) -> str:
        """将复合任务拆解步骤转换为单任务 PyQGIS 代码生成提示词。

        根据 action 类型生成针对性提示词，包含 target_layer 和 overlay_layer
        的具体引用，确保 LLM 生成精确的单步代码。
        同时解析 {output_var} 占位符，将上游步骤的输出图层名动态注入。
        """
        target_raw = step.get("target_layer", "")
        overlay_raw = step.get("overlay_layer", "")
        desc = step.get("description", "")

        # ── 动态参数注入：解析 {output_var} 占位符 → 真实图层名 ──
        target = self._resolve_placeholder(target_raw)
        overlay = self._resolve_placeholder(overlay_raw) if overlay_raw else ""

        action_map = {
            "clip": "裁剪",
            "buffer": "缓冲",
            "dissolve": "融合",
            "intersect": "相交",
            "centroid": "计算质心",
            "convex_hull": "生成凸包",
            "calculate_area": "计算面积",
            "calculate_length": "计算长度",
        }
        action_cn = action_map.get(step.get("action", "clip"), step.get("action", "?"))

        depends_on = step.get("depends_on", [])
        if depends_on:
            dep_info = "（该步骤依赖前序步骤 [{}] 的输出图层）".format(
                ", ".join(str(d) for d in depends_on)
            )
        else:
            dep_info = ""

        if action_cn == "计算面积":
            return (
                f"请对图层「{target}」的每个要素计算面积（平方米），"
                f"使用 QgsDistanceArea 或字段计算器将结果写入新字段。{dep_info}\n"
                f"（复合任务步骤 {step.get('step', '?')}：{desc}）"
            )
        elif action_cn == "计算长度":
            return (
                f"请对图层「{target}」的每个要素计算长度（米），"
                f"使用 QgsDistanceArea 或字段计算器将结果写入新字段。{dep_info}\n"
                f"（复合任务步骤 {step.get('step', '?')}：{desc}）"
            )
        elif step.get("action") in ("clip", "intersect"):
            return (
                f"请对图层「{target}」执行{action_cn}操作，"
                f"使用图层「{overlay}」作为裁剪/相交边界。{dep_info}\n"
                f"（复合任务步骤 {step.get('step', '?')}：{desc}）"
            )
        elif step.get("action") == "buffer":
            return (
                f"请对图层「{target}」执行缓冲分析。{dep_info}\n"
                f"（复合任务步骤 {step.get('step', '?')}：{desc}）"
            )
        else:
            return (
                f"请对图层「{target}」执行{action_cn}操作。{dep_info}\n"
                f"（复合任务步骤 {step.get('step', '?')}：{desc}）"
            )

    def _resolve_placeholder(self, raw_value: str) -> str:
        """解析字符串中的 {output_var} 占位符为真实图层名。

        从 _task_pipeline_outputs 中查找匹配的 step 编号对应的真实图层名。
        若占位符无法解析，保持原值返回（由 LLM 根据上下文自行推断）。
        """
        import re

        if not raw_value or "{" not in raw_value:
            return raw_value

        def _replace(match: re.Match) -> str:
            var_name = match.group(1).strip()
            # 尝试在所有已完成的步骤中匹配 output_var
            for step_num, layer_name in self._task_pipeline_outputs.items():
                # 遍历流水线步骤，找到 output_var == var_name 的步骤
                for s in self._task_pipeline:
                    if s.get("step") == step_num and s.get("output_var") == var_name:
                        return layer_name
            return match.group(0)  # 无法解析，保持原占位符

        return re.sub(r"\{([^}]+)\}", _replace, raw_value)

    # ══════════════════════════════════════════════════════════

    def _on_sandbox_finished(self, result: dict) -> None:
        """执行成功：注册图层、刷新画布、保存工程、持久化历史。"""
        pending_layers = result.get("pending_layers", [])
        raw_result = result.get("result")
        gc_removed = result.get("gc_removed", [])

        # Monkey-patch 拦截的图层 → 主线程安全加载
        for lyr in pending_layers:
            try:
                if QgsProject.instance().mapLayer(lyr.id()) is None:
                    QgsProject.instance().addMapLayer(lyr)
            except Exception:
                pass

        if gc_removed:
            self.statusBar().showMessage(f"GC 清除 {len(gc_removed)} 个中间图层", 3000)

        # 注册结果图层
        try:
            added_layers = self._register_result_layers(raw_result or {})
        except RuntimeError:
            added_layers = []

        if added_layers:
            self._zoom_to_layers(added_layers)
            layer_names = ", ".join(lyr.name() for lyr in added_layers)

            # ── 任务编排器：捕获当前步骤的输出图层名，供下游步骤动态注入 ──
            if self._task_pipeline and self._task_pipeline_index < len(self._task_pipeline):
                current_step = self._task_pipeline[self._task_pipeline_index]
                step_num = current_step.get("step", self._task_pipeline_index + 1)
                primary_name = added_layers[0].name() if added_layers else ""
                self._task_pipeline_outputs[step_num] = primary_name
                _log.info(
                    "流水线输出捕获：step %d → 「%s」",
                    step_num, primary_name,
                )

            project_path = style_manager.save_project()
            self.statusBar().showMessage(
                "AIQGIS 已自动完成高颜值制图！可直接双击打开工程文件排版打印。",
                8000,
            )
            self.ai_response_display.append(
                f"\n📦 工程已保存: {project_path}" if project_path
                else "\n⚠ 工程保存失败，请检查 output/projects/ 目录权限"
            )
            append_to_history("user", self._sandbox_user_text)
            append_to_history("assistant", f"空间分析完成，新增图层：{layer_names}")
            append_to_history(
                "system",
                "[System Notification]: Spatial analysis skill executed successfully. "
                "New layers added to canvas.",
            )
            persist_conversation_turn()
        elif raw_result:
            project_path = style_manager.save_project()
            self.statusBar().showMessage(
                "AIQGIS 已自动完成高颜值制图！可直接双击打开工程文件排版打印。",
                8000,
            )
            self.ai_response_display.append(
                f"\n工程已保存: {project_path}" if project_path
                else "\n工程保存失败"
            )
            append_to_history("user", self._sandbox_user_text)
            append_to_history("assistant", "空间分析完成（无新增图层）")
            append_to_history(
                "system",
                "[System Notification]: Spatial analysis skill executed successfully. "
                "No new layers added.",
            )
            persist_conversation_turn()
        else:
            self.statusBar().showMessage("代码已执行（未生成新图层）。", 6000)

        # ── 任务编排器：流水线续行 ──
        self._check_and_continue_pipeline()

    def _check_and_continue_pipeline(self) -> None:
        """检查复合任务流水线是否还有剩余步骤，如有则自动触发下一步。

        由 _on_sandbox_finished 在每个步骤成功后调用。
        """
        if not self._task_pipeline:
            return

        self._task_pipeline_index += 1
        total = len(self._task_pipeline)

        if self._task_pipeline_index >= total:
            # 流水线完成
            _log.info("复合任务流水线全部完成（%d 步）", total)
            self.statusBar().showMessage(
                f"复合任务流水线全部完成（共 {total} 步）", 8000
            )
            self.ai_response_display.append(
                f"\n[流水线] 全部 {total} 步执行完毕。"
            )
            # 清理流水线状态
            self._task_pipeline = []
            self._task_pipeline_index = 0
            self._task_pipeline_user_text = ""
            self._task_pipeline_layer_metadata = []
            self._task_pipeline_outputs = {}
            return

        next_step = self._task_pipeline[self._task_pipeline_index]
        desc = next_step.get("description", next_step.get("action", "?"))
        self.statusBar().showMessage(
            f"[{self._task_pipeline_index + 1}/{total}] 正在自动执行下一步：{desc}..."
        )
        self.ai_response_display.append(
            f"\n--- [流水线 {self._task_pipeline_index + 1}/{total}] {desc} ---"
        )
        self._execute_next_pipeline_step()

    def _on_sandbox_fix_needed(self, fix_context: dict) -> None:
        """自愈循环入口：收到 fix_needed 信号后回炉 LLM 获取修正代码。

        Pain 2 核心：调用方编排重试循环，最大 3 次。
        """
        MAX_RETRY = 3
        retry_count = fix_context.get("retry_count", 0)

        # ── 环境缺失型异常检测（前缀感知） ──
        # 当报错消息包含 "Algorithm .* not found" 时，判定为外部算子缺失，
        # 在异常消息末尾追加环境降级指引，确保 LLM 回炉时得到明确降级方向。
        _original_msg = fix_context.get("exception_msg", "")
        _algo_match = re.search(r"Algorithm\s+(\S+)\s+not found", _original_msg)
        if _algo_match:
            _missing_algo = _algo_match.group(1)
            _missing_prefix = _missing_algo.split(":")[0] if ":" in _missing_algo else ""
            _log.warning(
                "环境缺失型异常检测命中: %s（当前环境无此算子，前缀=%s）",
                _missing_algo, _missing_prefix or "(无)",
            )
            # ── 前缀感知指引：native 缺算子 ≠ grass7 缺算子 ──
            if _missing_prefix == "native":
                _suffix = (
                    "\n\n【环境诊断】当前 QGIS 不存在原生算子「{algo}」。"
                    "可能原因：算法名拼写错误（QGIS 原生算子的 ID 不使用下划线分割单词），"
                    "或此版本的 QGIS 未内置该算法。"
                    "\n请尝试：1) 修正算法名拼写 2) 改用 SAGA 等效算子（如 saga:fillsinksxxlwangbrennan）"
                    " 3) 若均不可用，用 numpy/gdal 手写算法实现。"
                    "\n严禁再次尝试相同算法 ID。"
                ).format(algo=_missing_algo)
            else:
                _suffix = (
                    "\n\n【环境诊断】当前便携版 QGIS 环境缺少算子「{algo}」。"
                    "请立即放弃使用任何「{prefix}」前缀的算法，改用 native: 原生等效算子。"
                    "若原生算子也不可用，请用 numpy/gdal 手写算法实现。"
                    "严禁再次尝试相同算法 ID 或同前缀的其他算法。"
                ).format(algo=_missing_algo, prefix=_missing_prefix)
            fix_context["exception_msg"] = _original_msg + _suffix
            # ── 追踪强制降级状态：仅 native/saga 前缀算子不存在时置位，gdal/grass 等不做强制降级 ──
            if retry_count >= 1 and _missing_prefix in ("native", "saga"):
                self._sandbox_force_numpy_gdal = True
            # 若为 grass7: 丢失，同时在 user_query 中注入提示
            if "grass7" in _missing_algo.lower():
                fix_context["user_query"] = (
                    fix_context.get("user_query", "") +
                    " [环境提示：grass7 算子库不存在，请用 native: 替代]"
                )

        # ── 强制降级持久化：一旦标记过，后续所有 retry 必须阻止回归 processing ──
        if self._sandbox_force_numpy_gdal:
            _regression_block = (
                "\n\n【强制降级续】之前的重试已确认 native: 和 SAGA 等效算子均不存在。"
                "你当前使用的是 numpy/gdal 手写方案。"
                "请修正 numpy/gdal 代码中的错误（如 proj.db 缺失时设置 os.environ['PROJ_LIB']），"
                "严禁回到 processing.run() 调用任何算法。"
            )
            fix_context["exception_msg"] = fix_context.get("exception_msg", "") + _regression_block

        # HeatmapRenderSuccessException → 视为成功
        if "HeatmapRender" in fix_context.get("exception_type", ""):
            self.statusBar().showMessage("前端热力图渲染完成", 5000)
            return

        if retry_count >= MAX_RETRY:
            # ── 任务编排器：终端失败 → 清空流水线，防止污染后续步骤 ──
            if self._task_pipeline:
                current_idx = self._task_pipeline_index
                current_step = self._task_pipeline[current_idx] if current_idx < len(self._task_pipeline) else {}
                desc = current_step.get("description", current_step.get("action", "?"))
                _log.error(
                    "流水线步骤 [%d/%d]「%s」彻底失败，清空整个流水线",
                    current_idx + 1, len(self._task_pipeline), desc,
                )
                self.ai_response_display.append(
                    f"\n[流水线] 步骤「{desc}」经 {MAX_RETRY} 次自愈后仍然失败，"
                    f"流水线已中断，剩余 {len(self._task_pipeline) - current_idx - 1} 步已取消。"
                )
                self._task_pipeline = []
                self._task_pipeline_index = 0
                self._task_pipeline_user_text = ""
                self._task_pipeline_layer_metadata = []
                self._task_pipeline_outputs = {}

            self.statusBar().showMessage("空间分析失败（已达最大重试次数）", 5000)
            QMessageBox.warning(
                self,
                "分析失败",
                f"代码执行 {MAX_RETRY} 次后仍然失败。\n"
                f"错误: {fix_context.get('exception_msg', '未知')}\n\n"
                f"请简化需求或手动调整参数后重试。",
            )
            return

        self.statusBar().showMessage(
            f"正在自愈修正（第 {retry_count + 1}/{MAX_RETRY} 次）..."
        )

        try:
            response = request_code_fix(
                broken_code=fix_context["broken_code"],
                error_line=fix_context["error_line"],
                exception_type=fix_context["exception_type"],
                exception_msg=fix_context["exception_msg"],
                user_query=fix_context["user_query"],
                retry_count=retry_count,
            )
            fixed_code = self._extract_python_code(response)
            self.ai_response_display.append(
                f"\n--- 自愈修正（第 {retry_count + 1} 次）---\n{fixed_code[:300]}..."
            )

            # 重建 exec_globals
            import builtins
            import processing

            active_layer = self._get_active_layer()
            layers_by_name = {
                layer.name(): layer
                for layer in QgsProject.instance().mapLayers().values()
            }
            exec_globals = {
                "__builtins__": builtins.__dict__,
                "processing": processing,
                "QgsProject": QgsProject,
                "QgsVectorLayer": QgsVectorLayer,
                "QgsRasterLayer": QgsRasterLayer,
                "QgsMapLayer": QgsMapLayer,
                "QgsCoordinateReferenceSystem": QgsCoordinateReferenceSystem,
                "QgsCoordinateTransform": QgsCoordinateTransform,
                "QgsFeature": QgsFeature,
                "QgsGeometry": QgsGeometry,
                "QgsPointXY": QgsPointXY,
                "QgsField": QgsField,
                "QgsFields": QgsFields,
                "active_layer": active_layer,
                "layers_by_name": layers_by_name,
                "generate_output_path": generate_output_path,
                "generate_geojson_output_path": generate_geojson_output_path,
                "style_manager": style_manager,
                "os": os,
                "tempfile": tempfile,
            }

            self._create_and_start_worker(
                fixed_code, exec_globals, active_layer, layers_by_name, retry_count + 1
            )
        except Exception as exc:
            self.statusBar().showMessage("自愈修正失败", 3000)
            QMessageBox.warning(self, "自愈失败", f"代码修正请求失败: {exc}")

    def _on_sandbox_error(self, msg: str) -> None:
        """Worker 致命错误（非代码级，如 Worker 自身崩溃）。"""
        self.statusBar().showMessage("空间分析执行失败", 5000)
        QMessageBox.warning(self, "沙箱错误", msg)

    def _fallback_legacy_execution(self, response_text: str, original_error: Exception) -> None:
        """回退：尝试旧版 Python 代码块提取。"""
        try:
            code = self._extract_python_code(response_text)
            self.last_ai_code = code
            if not self.skip_preview:
                result = AiCodePreviewDialog.preview_and_execute(
                    self, code, self.ai_prompt_input.toPlainText().strip()
                )
                if result is None:
                    return
                code, _ = result

            # 异步启动沙箱 Worker，结果通过信号槽网络返回
            active_layer = self._get_active_layer()
            layers_by_name = {
                layer.name(): layer
                for layer in QgsProject.instance().mapLayers().values()
            }
            self._launch_sandbox_worker(
                code=code,
                active_layer=active_layer,
                layers_by_name=layers_by_name,
                user_text=self.ai_prompt_input.toPlainText().strip(),
            )
        except Exception as exc:
            QMessageBox.critical(self, "执行失败", f"{exc}\n\n原始错误：{original_error}")

    def _handle_ai_error(self, error_message: str) -> None:
        """显示 API 或工作线程错误信息（中文）。"""

        QMessageBox.critical(
            self,
            "AI 请求失败",
            error_message,
        )
        self.run_button.setEnabled(True)
        self.screenshot_button.setEnabled(self.multimodal_enabled)
        self.statusBar().showMessage("AI 请求失败。", 6000)

    def _reset_ai_worker_state(self) -> None:
        """AI 请求完成后恢复 UI 状态。"""

        self.run_button.setEnabled(True)
        self.screenshot_button.setEnabled(self.multimodal_enabled)
        self.ai_worker = None

    @staticmethod
    def _try_parse_task_pipeline(response_text: str) -> Optional[List[dict]]:
        """从 LLM 响应中提取复合任务拆解 JSON 队列。

        支持三种格式：
        A. 纯 JSON 数组（无围栏）
        B. ```json ... ``` 代码块包裹
        C. 混合文本中嵌入 JSON 数组（AI 输出描述性前言 + JSON 的边界情况）

        Returns
        -------
        list or None
            合法任务队列（≥1 步），否则 None。
        """
        text = response_text.strip()

        # 策略 A：剥离 Markdown 代码块
        m = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
        if m:
            text = m.group(1).strip()

        # 策略 B：直接 JSON 解析
        try:
            tasks = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            tasks = None

        # 策略 C：正则从混合文本中提取 JSON 数组
        if tasks is None:
            # 先尝试 step 字段定位（最鲁棒的锚点）
            m2 = re.search(
                r"\[\s*\{[\s\S]*?\"step\"[\s\S]*?\}[\s\S]*?\]",
                text,
            )
            if m2:
                try:
                    tasks = json.loads(m2.group(0))
                except (json.JSONDecodeError, ValueError):
                    tasks = None

            # 还失败：尝试用方括号配对的贪婪提取
            if tasks is None:
                bracket_start = text.find("[")
                if bracket_start >= 0:
                    depth = 0
                    bracket_end = -1
                    for i in range(bracket_start, len(text)):
                        if text[i] == "[":
                            depth += 1
                        elif text[i] == "]":
                            depth -= 1
                            if depth == 0:
                                bracket_end = i + 1
                                break
                    if bracket_end > 0:
                        json_candidate = text[bracket_start:bracket_end]
                        try:
                            tasks = json.loads(json_candidate)
                        except (json.JSONDecodeError, ValueError):
                            tasks = None

        if tasks is None:
            return None

        if not isinstance(tasks, list) or len(tasks) == 0:
            return None

        # 校验必要字段
        for t in tasks:
            if not isinstance(t, dict):
                return None
            if "step" not in t or "action" not in t:
                return None

        return tasks

    @staticmethod
    def _extract_python_code(response_text: str) -> str:
        """从 AI 响应中提取 Python 代码块。

        多策略提取，优先级依次降低：
        1. 标准 Markdown 代码围栏（```python ... ``` 或 ``` ... ```）
        2. 无语言标记围栏（~~~ ... ~~~）
        3. 用 `// --- Python code ---` 等标记包裹
        4. 兜底：整段响应若含 import processing 等 PyQGIS 特征，直接当代码返回
        """
        # 策略 1：标准 Markdown 围栏
        match = re.search(
            r"```(?:python|py)?\s*\n?([\s\S]*?)```",
            response_text,
        )
        if match:
            return match.group(1).strip()

        # 策略 2：波浪线围栏（GitHub Flavored Markdown 替代语法）
        match = re.search(
            r"~~~(?:python|py)?\s*\n?([\s\S]*?)~~~",
            response_text,
        )
        if match:
            return match.group(1).strip()

        # 策略 3：以 import processing 或 result = processing.run 起头的行开始的块
        lines = response_text.split("\n")
        code_start = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("import ") or stripped.startswith("from "):
                if "processing" in stripped or "qgis" in stripped.lower():
                    code_start = i
                    break
            if "processing.run(" in stripped or "result =" in stripped:
                code_start = i
                break

        if code_start is not None:
            # 取从 code_start 到末尾的所有行（去除开头的纯文本说明）
            code_lines = []
            in_code = False
            for line in lines[code_start:]:
                if not in_code and line.strip() == "":
                    continue
                in_code = True
                code_lines.append(line)
            extracted = "\n".join(code_lines).strip()
            if extracted and (
                "import " in extracted or "processing.run(" in extracted
            ):
                _log.warning(
                    "代码提取降级：AI 未使用 Markdown 围栏，已从纯文本中抽取 %d 行",
                    len(code_lines),
                )
                return extracted

        # 策略 4：整段响应仅含 PyQGIS 模式 → 直接作为代码返回
        if re.search(r"processing\.run\(|QgsProject\.instance\(\)", response_text):
            _log.warning("代码提取降级：AI 响应无围栏，但检测到 PyQGIS 代码特征，直接执行全文")
            return response_text.strip()

        # 所有策略均失败
        _log.error(
            "AI 响应未找到 Python 代码块。响应前 800 字符：\n%s",
            response_text[:800],
        )
        raise ValueError(
            "AI 返回内容中未找到合法的 Python 代码块。\n"
            "请简化问题后重试，或检查 API 是否正常工作。\n\n"
            f"模型返回前 300 字符：\n{response_text[:300]}..."
        )

    def _register_result_layers(self, result: Dict[str, Any]) -> List[QgsMapLayer]:
        """将 processing.run() 产生的输出图层注册到工程中。"""

        added_layers: List[QgsMapLayer] = []

        if isinstance(result, dict):
            for value in result.values():
                self._collect_result_layers(value, added_layers)
        elif isinstance(result, QgsMapLayer):
            self._add_layer_if_needed(result, added_layers)
        elif isinstance(result, str):
            # 可能是图层 ID 或文件路径
            layer = QgsProject.instance().mapLayer(result)
            if layer is not None:
                self._add_layer_if_needed(layer, added_layers)
            elif os.path.exists(result) and is_supported_path(result):
                layer = create_layer_from_path(result)
                self._add_layer_if_needed(layer, added_layers)
        elif isinstance(result, (list, tuple)):
            for item in result:
                self._collect_result_layers(item, added_layers)
        else:
            raise RuntimeError(
                f"result 类型为 {type(result).__name__}，不支持自动解析。"
                f"请使用更简单的指令重试。"
            )

        if not added_layers:
            raise RuntimeError("空间分析已执行，但未识别到可自动添加的新图层输出。")

        self.map_canvas.refresh()
        return added_layers

    def _collect_result_layers(self, value: Any, added_layers: List[QgsMapLayer]) -> None:
        """递归检查处理输出并注册新图层。"""

        if value is None:
            return

        if isinstance(value, QgsMapLayer):
            self._add_layer_if_needed(value, added_layers)
            return

        if isinstance(value, (list, tuple, set)):
            for item in value:
                self._collect_result_layers(item, added_layers)
            return

        if isinstance(value, dict):
            for item in value.values():
                self._collect_result_layers(item, added_layers)
            return

        if isinstance(value, str):
            existing_layer = QgsProject.instance().mapLayer(value)
            if existing_layer is not None:
                self._add_layer_if_needed(existing_layer, added_layers)
                return

            if os.path.exists(value) and is_supported_path(value):
                loaded_layer = create_layer_from_path(value)
                self._add_layer_if_needed(loaded_layer, added_layers)

    def _add_layer_if_needed(
        self,
        layer: QgsMapLayer,
        added_layers: List[QgsMapLayer],
    ) -> None:
        """仅当图层尚未加入工程时才将其添加。"""

        project = QgsProject.instance()
        if project.mapLayer(layer.id()) is None:
            project.addMapLayer(layer)

        if all(existing.id() != layer.id() for existing in added_layers):
            added_layers.append(layer)

    # ══════════════════════════════════════════════════════════
    # P1 改造：离线快捷流程
    # ══════════════════════════════════════════════════════════

    # 离线工作流映射
    _WORKFLOW_MAP = {
        "cadastral": ("地籍标准化", "run_cadastral_standardization", {}),
        "hydrology": ("DEM水文解析", "run_dem_hydrological_analysis", {}),
        "batch_clip": ("一括切取+投影", "run_batch_clip_project", {}),
        "attribute_batch": ("属性一括処理", "run_vector_attribute_batch", {}),
        "thematic_map": ("主題図一括出力", "run_thematic_map_export", {}),
    }

    def _on_offline_workflow(self, workflow_key: str) -> None:
        """P1 改造：离线快捷流程按钮回调。

        在 QThread 中执行硬编码 PyQGIS 管道，不经过 LLM 解析。
        """

        if workflow_key not in self._WORKFLOW_MAP:
            QMessageBox.warning(self, "未知工作流", f"未识别的流程键：{workflow_key}")
            return

        wf_name, fn_name, extra_kwargs = self._WORKFLOW_MAP[workflow_key]

        try:
            from core.offline_workflows import OfflineWorkflowWorker
            import core.offline_workflows as ow

            fn = getattr(ow, fn_name, None)
            if fn is None:
                raise ImportError(f"无法加载离线工作流函数: {fn_name}")
        except Exception as exc:
            QMessageBox.critical(self, "加载失败", f"无法加载离线工作流模块：{exc}")
            return

        # 禁用所有按钮，避免重复点击
        self.run_button.setEnabled(False)
        self.screenshot_button.setEnabled(False)
        for btn in self._offline_buttons:
            btn.setEnabled(False)

        self.statusBar().showMessage(f"正在执行离线快捷流程：{wf_name}...")
        self.ai_response_display.setPlainText(f"[离线模式] 正在执行：{wf_name}...\n")

        self._offline_worker = OfflineWorkflowWorker(
            workflow_fn=fn,
            **extra_kwargs,
        )
        self._offline_worker.progress.connect(self._on_offline_progress)
        self._offline_worker.finished.connect(self._on_offline_finished)
        self._offline_worker.error.connect(self._on_offline_error)
        self._offline_worker.start()

    def _on_offline_progress(self, msg: str) -> None:
        """离线工作流进度回调。"""
        current = self.ai_response_display.toPlainText()
        self.ai_response_display.setPlainText(current + msg + "\n")
        # 滚动到底部
        scrollbar = self.ai_response_display.verticalScrollBar()
        if scrollbar:
            scrollbar.setValue(scrollbar.maximum())
        self.statusBar().showMessage(msg, 0)

        # P2：确保进度条可见
        if hasattr(self, '_offline_progress_bar') and not self._offline_progress_bar.isVisible():
            self._offline_progress_bar.setVisible(True)

    def _on_offline_finished(self, result: dict) -> None:
        """离线工作流完成回调。"""
        self._restore_offline_ui()

        msg = "\n离线快捷流程执行完成！\n"
        if result.get("output_dir"):
            msg += f"输出目录：{result['output_dir']}\n"
        if result.get("results"):
            for r in result["results"]:
                if isinstance(r, dict):
                    msg += f"  - {r.get('layer_name', r.get('layer', ''))} → {r.get('output', r.get('path', ''))}\n"

        self.ai_response_display.append(msg)
        self.statusBar().showMessage("离线快捷流程执行完成。", 8000)

    def _on_offline_error(self, msg: str) -> None:
        """离线工作流错误回调。"""
        self._restore_offline_ui()
        self.ai_response_display.append(f"\n执行失败：\n{msg}")
        self.statusBar().showMessage("离线快捷流程执行失败。", 8000)
        QMessageBox.critical(self, "离线流程错误", msg)

    def _restore_offline_ui(self) -> None:
        """恢复离线工作流按钮状态。"""
        if self.offline_mode:
            self.run_button.setEnabled(False)
            self.screenshot_button.setEnabled(False)
        else:
            self.run_button.setEnabled(True)
            self.screenshot_button.setEnabled(self.multimodal_enabled)
        for btn in self._offline_buttons:
            btn.setEnabled(True)
        self._offline_worker = None

        # P2：隐藏进度条
        if hasattr(self, '_offline_progress_bar'):
            self._offline_progress_bar.setVisible(False)

    def _toggle_offline_group(self, group: str) -> None:
        """切换离线快捷流程分组折叠状态，并持久化到配置。"""
        if group == "vector":
            currently_visible = self._vector_button_row.isVisible()
            new_collapsed = currently_visible
            self._vector_button_row.setVisible(not currently_visible)
            arrow = "▶" if new_collapsed else "▼"
            lm = lang_manager()
            self._vector_toggle_btn.setText(
                f"{arrow} {lm.tr('group_vector_title')}"
            )
            self._vector_toggle_btn.setToolTip(
                lm.tr("btn_expand") if new_collapsed else lm.tr("btn_collapse")
            )
        else:
            currently_visible = self._raster_button_row.isVisible()
            new_collapsed = currently_visible
            self._raster_button_row.setVisible(not currently_visible)
            arrow = "▶" if new_collapsed else "▼"
            lm = lang_manager()
            self._raster_toggle_btn.setText(
                f"{arrow} {lm.tr('group_raster_title')}"
            )
            self._raster_toggle_btn.setToolTip(
                lm.tr("btn_expand") if new_collapsed else lm.tr("btn_collapse")
            )

        collapsed = self.config.offline_group_collapsed
        collapsed[group] = new_collapsed
        self.config.offline_group_collapsed = collapsed
