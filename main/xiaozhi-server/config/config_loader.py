import os
import asyncio
import yaml
from collections.abc import Mapping
from config.manage_api_client import (
    init_service,
    get_server_config,
    get_agent_models,
    get_correct_words,
    DeviceNotFoundException,
    DeviceBindException,
)


def get_project_dir():
    """获取项目根目录"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/"


def read_config(config_path):
    """读取yaml配置；文件不存在（如云端部署没有data/.config.yaml）时返回空dict"""
    if not os.path.exists(config_path):
        return {}
    with open(config_path, "r", encoding="utf-8") as file:
        config = yaml.safe_load(file)
    return config or {}


def _env(name, default=None):
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def apply_env_overrides(config):
    """
    用环境变量覆盖配置（适配 Render 等云平台）。
    - PORT: 云平台下发的公网端口。WebSocket 监听 0.0.0.0:$PORT，/health 也在该端口响应
    - HTTP_PORT: 内部HTTP端口（OTA/视觉接口），默认 8003
    - PUBLIC_WS_URL / PUBLIC_VISION_URL: 对外公布给设备的地址
    - ASR_PROVIDER / LLM_PROVIDER / TTS_PROVIDER: 选择 selected_module 中的模块名
    - GROQ_API_KEY / OPENAI_API_KEY / GEMINI_API_KEY: API 密钥
    - TTS_VOICE: EdgeTTS 音色，默认 vi-VN-HoaiMyNeural
    """
    server = config.setdefault("server", {})
    server["ip"] = "0.0.0.0"
    port = _env("PORT")
    http_port = _env("HTTP_PORT")
    single_port = _env("SINGLE_PORT_MODE", "true").lower() not in ("0", "false", "no")
    if port and single_port:
        # Render chỉ công khai 1 cổng: aiohttp (admin + /health + OTA) lắng nghe 0.0.0.0:$PORT
        # và chuyển tiếp WebSocket /xiaozhi/v1/ sang WebSocket server nội bộ.
        server["http_port"] = int(port)
        server["port"] = int(_env("INTERNAL_WS_PORT", 8000))
        server["single_port_mode"] = True
    else:
        # WebSocket server lắng nghe trực tiếp 0.0.0.0:$PORT, /health trả lời cùng cổng
        if port:
            server["port"] = int(port)
        if http_port:
            server["http_port"] = int(http_port)
        elif port and int(server.get("http_port", 8003)) == int(port):
            server["http_port"] = 8003
        server["single_port_mode"] = False
    if _env("PUBLIC_WS_URL"):
        server["websocket"] = _env("PUBLIC_WS_URL")
    if _env("PUBLIC_VISION_URL"):
        server["vision_explain"] = _env("PUBLIC_VISION_URL")
    if _env("AUTH_KEY"):
        server["auth_key"] = _env("AUTH_KEY")

    selected = config.setdefault("selected_module", {})
    for env_name, module in (
        ("ASR_PROVIDER", "ASR"),
        ("LLM_PROVIDER", "LLM"),
        ("TTS_PROVIDER", "TTS"),
        ("VAD_PROVIDER", "VAD"),
    ):
        if _env(env_name):
            selected[module] = _env(env_name)

    def set_field(module, name, field, value):
        if value and name in config.get(module, {}):
            config[module][name][field] = value

    groq_key = _env("GROQ_API_KEY")
    openai_key = _env("OPENAI_API_KEY")
    gemini_key = _env("GEMINI_API_KEY")
    set_field("ASR", "GroqASR", "api_key", groq_key)
    set_field("ASR", "OpenaiASR", "api_key", openai_key)
    set_field("LLM", "GeminiLLM", "api_key", gemini_key)
    set_field("LLM", "GeminiOpenAILLM", "api_key", gemini_key)
    set_field("LLM", "OpenAILLM", "api_key", openai_key)
    set_field("LLM", "GroqLLM", "api_key", groq_key)
    set_field("ASR", "GroqASR", "language", _env("ASR_LANGUAGE"))
    set_field("ASR", "OpenaiASR", "language", _env("ASR_LANGUAGE"))
    set_field("LLM", "GeminiOpenAILLM", "model_name", _env("GEMINI_MODEL"))
    set_field("LLM", "OpenAILLM", "model_name", _env("OPENAI_MODEL"))
    set_field("TTS", "EdgeTTS", "voice", _env("TTS_VOICE"))
    return config


async def load_config():
    """加载配置文件"""
    from core.utils.cache.manager import cache_manager, CacheType

    # 检查缓存
    cached_config = cache_manager.get(CacheType.CONFIG, "main_config")
    if cached_config is not None:
        return cached_config

    default_config_path = get_project_dir() + "config.yaml"
    custom_config_path = get_project_dir() + "data/.config.yaml"

    # 加载默认配置
    default_config = read_config(default_config_path)
    custom_config = read_config(custom_config_path)

    if custom_config.get("manager-api", {}).get("url"):
        config = await get_config_from_api_async(custom_config)
    else:
        # 合并配置
        config = merge_configs(default_config, custom_config)
    # 环境变量覆盖（PORT、API Key等）
    apply_env_overrides(config)
    # 初始化目录
    ensure_directories(config)

    # 缓存配置
    cache_manager.set(CacheType.CONFIG, "main_config", config)
    return config


async def get_config_from_api_async(config):
    """从Java API获取配置（异步版本）"""
    # 初始化API客户端
    init_service(config)

    # 获取服务器配置
    config_data = await get_server_config()
    if config_data is None:
        raise Exception("Failed to fetch server config from API")

    config_data["read_config_from_api"] = True
    config_data["manager-api"] = {
        "url": config["manager-api"].get("url", ""),
        "secret": config["manager-api"].get("secret", ""),
    }
    auth_enabled = config_data.get("server", {}).get("auth", {}).get("enabled", False)
    # server的配置以本地为准
    if config.get("server"):
        config_data["server"] = {
            "ip": config["server"].get("ip", ""),
            "port": config["server"].get("port", ""),
            "http_port": config["server"].get("http_port", ""),
            "vision_explain": config["server"].get("vision_explain", ""),
            "auth_key": config["server"].get("auth_key", ""),
        }
    config_data["server"]["auth"] = {"enabled": auth_enabled}
    # 如果服务器没有prompt_template，则从本地配置读取
    if not config_data.get("prompt_template"):
        config_data["prompt_template"] = config.get("prompt_template")
    return config_data


async def get_private_config_from_api(config, device_id, client_id):
    """从Java API获取私有配置"""
    results = await asyncio.gather(
        get_agent_models(device_id, client_id, config["selected_module"]),
        get_correct_words(device_id),
        return_exceptions=True,
    )
    agent_result = results[0]
    correct_words = results[1] if not isinstance(results[1], Exception) else None

    # 抛出业务异常
    if isinstance(agent_result, DeviceNotFoundException):
        raise agent_result
    if isinstance(agent_result, DeviceBindException):
        raise agent_result

    private_config = agent_result if not isinstance(agent_result, Exception) else {}
    if correct_words:
        private_config["correct_words"] = correct_words
    return private_config


def ensure_directories(config):
    """确保所有配置路径存在"""
    dirs_to_create = set()
    project_dir = get_project_dir()  # 获取项目根目录
    # 日志文件目录
    log_dir = config.get("log", {}).get("log_dir", "tmp")
    dirs_to_create.add(os.path.join(project_dir, log_dir))

    # ASR/TTS模块输出目录
    for module in ["ASR", "TTS"]:
        if config.get(module) is None:
            continue
        for provider in config.get(module, {}).values():
            output_dir = provider.get("output_dir", "")
            if output_dir:
                dirs_to_create.add(output_dir)

    # 根据selected_module创建模型目录
    selected_modules = config.get("selected_module", {})
    for module_type in ["ASR", "LLM", "TTS"]:
        selected_provider = selected_modules.get(module_type)
        if not selected_provider:
            continue
        if config.get(module_type) is None:
            continue
        if config.get(selected_provider) is None:
            continue
        provider_config = config.get(module_type, {}).get(selected_provider, {})
        output_dir = provider_config.get("output_dir")
        if output_dir:
            full_model_dir = os.path.join(project_dir, output_dir)
            dirs_to_create.add(full_model_dir)

    # 统一创建目录（保留原data目录创建）
    for dir_path in dirs_to_create:
        try:
            os.makedirs(dir_path, exist_ok=True)
        except PermissionError:
            print(f"警告：无法创建目录 {dir_path}，请检查写入权限")


def merge_configs(default_config, custom_config):
    """
    递归合并配置，custom_config优先级更高

    Args:
        default_config: 默认配置
        custom_config: 用户自定义配置

    Returns:
        合并后的配置
    """
    if not isinstance(default_config, Mapping) or not isinstance(
        custom_config, Mapping
    ):
        return custom_config

    merged = dict(default_config)

    for key, value in custom_config.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = merge_configs(merged[key], value)
        else:
            merged[key] = value

    return merged
