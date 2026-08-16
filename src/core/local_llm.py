"""
local_llm.py — 本地大模型调用模块

支持 Ollama / LM Studio 等兼容 OpenAI Chat Completions API 的本地服务。
离线模式下纯本地运行，不发起任何外网请求。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

_log = logging.getLogger("local_llm")


class LocalLLMClient:
    """本地大模型客户端。

    调用本地运行的 Ollama / LM Studio 等服务的 Chat Completions API。
    默认地址 http://localhost:11434/v1/chat/completions (Ollama)。

    Attributes
    ----------
    base_url : str
        API 基础地址。
    model : str
        模型名称。
    timeout : int
        请求超时秒数。
    """

    def __init__(self, base_url: str = "", model: str = "", timeout: int = 120) -> None:
        from core.config_manager import config_manager as cm
        self.base_url = base_url or cm.local_model_url or "http://127.0.0.1:11434/v1"
        self.model = model or cm.local_model_name or "qwen2.5:7b"
        self.timeout = timeout

    def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 4096,
        expect_json: bool = False,
        max_retries: int = 2,
    ) -> str:
        """发送对话请求到本地大模型。

        Parameters
        ----------
        messages : list
            OpenAI 格式消息列表 [{"role": "user"/"assistant"/"system", "content": "..."}]
        temperature : float
            生成温度。
        max_tokens : int
            最大输出 token 数。
        expect_json : bool
            期望返回 JSON 时置 True；无效响应（空内容或 JSON 解析失败）会触发重发。
        max_retries : int
            无效响应时的最大重发次数（每次间隔 0.3s）。

        Returns
        -------
        str
            模型回复文本。连续无效时返回最后一次 content，不抛异常。

        Raises
        ------
        ConnectionError
            连接本地模型服务失败。
        RuntimeError
            模型返回错误。
        """
        url = f"{self.base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }

        last_content = ""
        for attempt in range(max_retries + 1):
            content = self._post_once(url, payload)
            last_content = content
            if self._is_valid_response(content, expect_json):
                return content
            if attempt < max_retries:
                _log.warning(
                    "本地模型无效响应（第 %d/%d 次，expect_json=%s），0.3s 后重发",
                    attempt + 1, max_retries + 1, expect_json,
                )
                time.sleep(0.3)

        _log.warning(
            "本地模型连续 %d 次无效响应，返回最后一次内容（交给上层 unknown 兜底）",
            max_retries + 1,
        )
        return last_content

    def _post_once(self, url: str, payload: Dict[str, Any]) -> str:
        """发送单次请求并返回 content；网络/HTTP 异常语义保持现状。"""
        try:
            resp = requests.post(
                url,
                json=payload,
                timeout=self.timeout,
                headers={"Content-Type": "application/json"},
                proxies={"http": None, "https": None},
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except requests.exceptions.ConnectionError:
            raise ConnectionError(
                f"无法连接到本地模型服务 ({self.base_url})。请确认 Ollama / LM Studio 是否正在运行。"
            )
        except requests.exceptions.Timeout:
            raise TimeoutError(f"本地模型响应超时 ({self.timeout}s)。请尝试减小输入或更换更快的模型。")
        except Exception as e:
            raise RuntimeError(f"本地模型调用失败：{e}")

    @staticmethod
    def _is_valid_response(content: Optional[str], expect_json: bool) -> bool:
        """判断响应是否有效：空内容无效；expect_json 时 JSON 解析失败无效。"""
        if content is None or not str(content).strip():
            return False
        if expect_json:
            try:
                json.loads(str(content))
            except (json.JSONDecodeError, TypeError):
                return False
        return True

    def test_connection(self) -> bool:
        """测试本地模型服务连通性。"""
        try:
            resp = requests.get(
                f"{self.base_url.rstrip('/')}/models",
                timeout=5,
                proxies={"http": None, "https": None},
            )
            return resp.status_code == 200
        except Exception:
            return False
