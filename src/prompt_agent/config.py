"""提示词 Agent 独立 API 配置。

与主分析管线（core/ai_config.py）完全隔离，可接入不同模型。
"""

#: 独立 API 密钥
API_KEY = ""  # 部署时从环境变量或配置文件注入

#: 独立 API 端点
BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

#: 模型名称（推荐轻量模型，提炼任务不要求强推理）
MODEL_NAME = "qwen-turbo"

#: 请求超时（秒）
REQUEST_TIMEOUT = 30

#: 最大输入字符数（超过此长度自动截断尾部）
MAX_INPUT_CHARS = 6000