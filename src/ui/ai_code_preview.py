"""
AI 代码预览对话框，在执行前显示 AI 生成的 PyQGIS 代码供用户确认。
"""

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QTextEdit,
    QPushButton,
    QCheckBox,
    QGroupBox,
    QMessageBox,
)


class AiCodePreviewDialog(QDialog):
    """AI 代码预览对话框。"""

    # 当用户确认执行时发射
    execute_confirmed = pyqtSignal(str, bool)  # (code, skip_confirm)

    def __init__(self, parent=None, ai_code="", user_prompt=""):
        """
        初始化预览对话框。

        Parameters
        ----------
        parent : QWidget, optional
            父窗口。
        ai_code : str, optional
            AI 生成的 PyQGIS 代码。
        user_prompt : str, optional
            用户原始指令，用于上下文。
        """
        super().__init__(parent)
        self.setWindowTitle("AI 代码预览")
        self.setMinimumSize(700, 500)
        self.setModal(True)

        self.ai_code = ai_code
        self.user_prompt = user_prompt
        self._setup_ui()

    def _setup_ui(self) -> None:
        """构建对话框 UI。"""

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # 用户指令组
        if self.user_prompt:
            prompt_group = QGroupBox("用户指令")
            prompt_layout = QVBoxLayout(prompt_group)
            prompt_label = QLabel(self.user_prompt)
            prompt_label.setWordWrap(True)
            prompt_label.setStyleSheet("background: #f8f9fa; padding: 8px; border-radius: 4px;")
            prompt_layout.addWidget(prompt_label)
            layout.addWidget(prompt_group)

        # 代码预览组
        code_group = QGroupBox("AI 生成的 PyQGIS 代码")
        code_layout = QVBoxLayout(code_group)

        self.code_editor = QTextEdit()
        self.code_editor.setPlainText(self.ai_code)
        self.code_editor.setReadOnly(True)
        self.code_editor.setStyleSheet(
            "font-family: 'Courier New', monospace; font-size: 12px;"
        )
        code_layout.addWidget(self.code_editor)

        # 代码信息
        info_label = QLabel(
            f"代码长度：{len(self.ai_code)} 字符 | "
            f"行数：{self.ai_code.count(chr(10)) + 1}"
        )
        info_label.setStyleSheet("color: #666; font-size: 12px;")
        code_layout.addWidget(info_label)

        layout.addWidget(code_group, stretch=1)

        # 选项组
        options_group = QGroupBox("执行选项")
        options_layout = QVBoxLayout(options_group)

        self.skip_confirm_check = QCheckBox("下次不再询问，直接执行（可在设置中恢复）")
        self.skip_confirm_check.setToolTip("启用后，后续 AI 分析将跳过预览直接执行。")
        options_layout.addWidget(self.skip_confirm_check)

        warning_label = QLabel(
            "⚠️ 注意：请仔细检查代码，确保其不包含危险操作（如删除文件、系统调用等）。"
        )
        warning_label.setStyleSheet("color: #d32f2f; font-weight: bold;")
        warning_label.setWordWrap(True)
        options_layout.addWidget(warning_label)

        layout.addWidget(options_group)

        # 按钮行
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.copy_btn = QPushButton("复制代码")
        self.copy_btn.clicked.connect(self._copy_code)
        button_layout.addWidget(self.copy_btn)

        self.cancel_btn = QPushButton("取消执行")
        self.cancel_btn.clicked.connect(self.reject)
        button_layout.addWidget(self.cancel_btn)

        self.execute_btn = QPushButton("确认执行")
        self.execute_btn.setDefault(True)
        self.execute_btn.setStyleSheet("background: #10b981; color: white;")
        self.execute_btn.clicked.connect(self._confirm_execute)
        button_layout.addWidget(self.execute_btn)

        layout.addLayout(button_layout)

    def _copy_code(self) -> None:
        """复制代码到剪贴板。"""
        clipboard = self.code_editor.textCursor().selectedText()
        if not clipboard:
            clipboard = self.code_editor.toPlainText()

        from PyQt5.QtWidgets import QApplication
        QApplication.clipboard().setText(clipboard)
        QMessageBox.information(self, "复制成功", "代码已复制到剪贴板。")

    def _confirm_execute(self) -> None:
        """确认执行代码。"""
        skip_confirm = self.skip_confirm_check.isChecked()
        self.execute_confirmed.emit(self.ai_code, skip_confirm)
        self.accept()

    @classmethod
    def preview_and_execute(cls, parent, ai_code, user_prompt=""):
        """
        静态方法：显示预览对话框并处理用户选择。

        Parameters
        ----------
        parent : QWidget
            父窗口。
        ai_code : str
            AI 生成的 PyQGIS 代码。
        user_prompt : str, optional
            用户原始指令。

        Returns
        -------
        tuple or None
            如果用户确认执行，返回 (code, skip_confirm)；
            如果取消，返回 None。
        """
        dialog = cls(parent, ai_code, user_prompt)
        result = []

        def on_confirmed(code, skip_confirm):
            result.append((code, skip_confirm))

        dialog.execute_confirmed.connect(on_confirmed)

        if dialog.exec_() == QDialog.Accepted:
            return result[0] if result else None
        return None