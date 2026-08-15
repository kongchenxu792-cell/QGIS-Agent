"""配置持久化管理模块。

使用 JSON 文件持久化用户设置，重启不丢失。
持久化项：API密钥、默认输出路径、界面语言偏好、上次使用的模式。
"""

from __future__ import annotations

import json
import os
from typing import Any


class ConfigManager:
    """应用配置管理器（JSON 持久化，单例模式）。"""

    _instance: ConfigManager | None = None

    def __new__(cls) -> ConfigManager:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        self._config_path = os.path.join(project_root, "aiqgis_config.json")
        self._data: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                pass
        return {}

    def _save(self) -> None:
        try:
            with open(self._config_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except IOError:
            pass

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._save()

    # ── 便捷属性 ──

    @property
    def api_key(self) -> str:
        return self._data.get("api_key", "")

    @api_key.setter
    def api_key(self, value: str) -> None:
        self.set("api_key", value)

    @property
    def base_url(self) -> str:
        return self._data.get("base_url", "")

    @base_url.setter
    def base_url(self, value: str) -> None:
        self.set("base_url", value)

    @property
    def model_name(self) -> str:
        return self._data.get("model_name", "")

    @model_name.setter
    def model_name(self, value: str) -> None:
        self.set("model_name", value)

    @property
    def default_output_path(self) -> str:
        return self._data.get("default_output_path", "")

    @default_output_path.setter
    def default_output_path(self, value: str) -> None:
        self.set("default_output_path", value)

    @property
    def language(self) -> str:
        return self._data.get("language", "zh")

    @language.setter
    def language(self, value: str) -> None:
        self.set("language", value)

    @property
    def last_mode(self) -> str:
        """上次使用的模式：'online'、'offline' 或 'hybrid'（默认 offline）。"""
        mode = self._data.get("last_mode", "offline")
        if mode not in ("online", "offline", "hybrid"):
            mode = "offline"
        return mode

    @last_mode.setter
    def last_mode(self, value: str) -> None:
        self.set("last_mode", value)

    @property
    def offline_group_collapsed(self) -> dict:
        """离线快捷流程分组折叠状态。{'vector': False, 'raster': False}"""
        return self._data.get("offline_group_collapsed", {"vector": False, "raster": False})

    @offline_group_collapsed.setter
    def offline_group_collapsed(self, value: dict) -> None:
        self.set("offline_group_collapsed", value)

    @property
    def local_model_url(self) -> str:
        """本地大模型 API 地址。默认 Ollama 端口。"""
        return self._data.get("local_model_url", "http://127.0.0.1:11434/v1")

    @local_model_url.setter
    def local_model_url(self, value: str) -> None:
        self.set("local_model_url", value)

    @property
    def local_model_name(self) -> str:
        """本地大模型名称。默认 qwen2.5:7b。"""
        return self._data.get("local_model_name", "qwen2.5:7b")

    @local_model_name.setter
    def local_model_name(self, value: str) -> None:
        self.set("local_model_name", value)

# 模块级单例实例
config_manager = ConfigManager()
