"""AAWM 本地代理网关：为黑盒 CLI/IDE agent（Claude Code、Codex、opencode、
WorkBuddy、Antigravity、Qwen Code 等）提供零改造水印接入。

这些工具是独立进程，无法 import SDK，唯一稳定接入点是网络层——它们全部
支持自定义 OpenAI / Anthropic 兼容 endpoint。把工具的 base_url 指向本代理，
代理在响应文本流上嵌入水印，对工具完全透明。

用法::

    from aawm.proxy import ProxyConfig, create_proxy_app
    from aawm.plugins import Watermarker

    app = create_proxy_app(watermarker, ProxyConfig(
        key_map={"sk-aawm-alice": 0xA11C, "sk-aawm-bob": 0xB0B},
    ))
    # uvicorn.run(app, port=8787)
"""
from .app import ProxyConfig, create_proxy_app

__all__ = ["ProxyConfig", "create_proxy_app"]
