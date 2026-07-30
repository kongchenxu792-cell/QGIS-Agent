"""
语言管理器 — 单例模式，加载和管理中日英三语 UI 资源。
通过 tr(key, **kwargs) 获取翻译文本，set_language(lang) 切换语言。
语言偏好持久化到 config_manager。
"""

import json
import logging
import os
from typing import Dict

_log = logging.getLogger(__name__)

from PyQt5.QtCore import QObject, pyqtSignal


class LangManager(QObject):
    """多语言管理器（单例）。

    加载 src/i18n/ 下的 zh.json / ja.json / en.json，
    通过 tr(key, **kwargs) 获取当前语言的翻译文本。
    切换语言时发射 language_changed 信号，通知 UI 刷新。

    属性
    ----
    current_lang : str
        当前语言代码 ("zh" / "ja" / "en")。
    _instance : LangManager or None
        单例引用。
    """

    _instance: "LangManager | None" = None

    # 语言切换信号
    language_changed = pyqtSignal(str)  # 参数: 新语言代码

    SUPPORTED_LANGS = {
        "zh": "中文",
        "ja": "日本語",
        "en": "English",
    }

    def __new__(cls) -> "LangManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        super().__init__()
        self._i18n_dir = os.path.dirname(__file__)
        self._data: Dict[str, Dict[str, str]] = {}
        self.current_lang: str = "zh"

        # 预加载所有语言资源
        for lang in self.SUPPORTED_LANGS:
            path = os.path.join(self._i18n_dir, f"{lang}.json")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self._data[lang] = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError) as exc:
                _log.info(f"[LangManager] 加载 {lang}.json 失败: {exc}")
                self._data[lang] = {}

        # 从 config 恢复上次语言设置
        try:
            from core.config_manager import config_manager
            saved_lang = config_manager.language
            if saved_lang in self.SUPPORTED_LANGS:
                self.current_lang = saved_lang
        except Exception:
            pass

        self._initialized = True

    # ── 公共 API ────────────────────────────────────────────

    def tr(self, key: str, **kwargs: str) -> str:
        """获取当前语言的翻译文本。

        Parameters
        ----------
        key : str
            翻译键。
        **kwargs : str
            格式化参数，如 count=5 会替换文本中的 {count}。

        Returns
        -------
        str
            翻译后的文本。若 key 在当前语言资源中不存在，回退至中文；
            中文也不存在则返回 key 本身。
        """
        table = self._data.get(self.current_lang, {})
        text = table.get(key)
        if text is None:
            # 回退中文
            text = self._data.get("zh", {}).get(key, key)
        if kwargs:
            try:
                return text.format(**kwargs)
            except (KeyError, ValueError):
                pass
        return text

    def set_language(self, lang: str) -> bool:
        """切换语言并持久化。

        Parameters
        ----------
        lang : str
            目标语言代码 ("zh" / "ja" / "en")。

        Returns
        -------
        bool
            是否成功切换。
        """
        if lang not in self.SUPPORTED_LANGS:
            _log.info(f"[LangManager] 不支持的语言代码: {lang}")
            return False

        if lang == self.current_lang:
            return True

        self.current_lang = lang

        # 持久化到 config
        try:
            from core.config_manager import config_manager
            config_manager.language = lang
        except Exception:
            pass

        self.language_changed.emit(lang)
        return True

    # ── 便捷方法 ────────────────────────────────────────────

    @classmethod
    def instance(cls) -> "LangManager":
        """获取单例实例（已在 __new__ 中保证唯一性）。"""
        return cls()

    @property
    def supported_langs(self) -> Dict[str, str]:
        """受支持的语言代码 → 显示名称映射。"""
        return dict(self.SUPPORTED_LANGS)

    @property
    def current_display(self) -> str:
        """当前语言的显示名称。"""
        return self.SUPPORTED_LANGS.get(self.current_lang, self.current_lang)


# ── 模块级便捷函数 ────────────────────────────────────────

_lm: LangManager | None = None


def tr(key: str, **kwargs: str) -> str:
    """模块级便捷翻译函数。"""
    global _lm
    if _lm is None:
        _lm = LangManager.instance()
    return _lm.tr(key, **kwargs)


def lang_manager() -> LangManager:
    """获取 LangManager 单例。"""
    global _lm
    if _lm is None:
        _lm = LangManager.instance()
    return _lm
