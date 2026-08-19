"""AAWM 检测服务：FastAPI 端点。

提供 HTTP API 做水印溯源检测。

端点：
    POST /v1/trace    溯源检测
    POST /v1/embed    嵌入（内部用，需鉴权）
    GET  /v1/health   健康检查
"""
