"""代理网关实现：OpenAI + Anthropic 双协议反向代理，响应文本嵌入水印。

架构::

    CLI 工具 (Claude Code / Codex / opencode / WorkBuddy / ...)
        │  base_url = http://localhost:8787   （工具侧唯一配置）
        ▼
    AAWM Proxy ── key_map: 客户端 key → UID（溯源身份）
        │  替换为真实上游 key，转发请求
        ▼
    真实 API（OpenAI / Anthropic / 兼容网关）
        │  响应（含 SSE 流式）
        ▼
    AAWM Proxy ── 文本嵌入水印（句子级流式 / 整段非流式）
        │  fail-open：任何异常透传原文
        ▼
    CLI 工具（用户无感）

协议支持：
    - POST /v1/chat/completions   OpenAI 协议（Codex、opencode、Qwen Code…）
    - POST /v1/messages           Anthropic 协议（Claude Code、WorkBuddy…）
    - 其余路径原样透传（/v1/models、count tokens 等）

身份识别：客户端请求头的 ``Authorization: Bearer <key>`` 或 ``x-api-key``
在 key_map 中查 UID。查不到 → 使用 default_uid（None 则跳过嵌入）。

salt 存档：嵌入成功时经 on_embed 回调把 (uid, session_salt) 追加写入
JSONL 归档文件，事后溯源必用。
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from ..plugins.context import Context
from ..plugins.facade import Watermarker
from ..plugins.middleware import WatermarkMiddleware
from ..plugins.streaming import StreamingWatermarker

logger = logging.getLogger("aawm.proxy")

# 转发时跳过的 hop-by-hop / 长度相关头
_HOP_HEADERS = {
    "content-length", "transfer-encoding", "content-encoding",
    "connection", "keep-alive", "host", "accept-encoding",
}


class ProxyConfig:
    """代理网关配置。"""

    def __init__(
        self,
        *,
        upstream_openai: str = "https://api.openai.com",
        upstream_anthropic: str = "https://api.anthropic.com",
        key_map: Optional[Dict[str, int]] = None,
        upstream_openai_key: Optional[str] = None,
        upstream_anthropic_key: Optional[str] = None,
        default_uid: Optional[int] = None,
        min_text_length: int = 50,
        salt_archive: Optional[Path] = None,
    ) -> None:
        self.upstream_openai = upstream_openai.rstrip("/")
        self.upstream_anthropic = upstream_anthropic.rstrip("/")
        # 客户端 key → UID（每台终端/每个用户一把 aawm key）
        self.key_map = dict(key_map or {})
        # 真实上游 key；None 时沿用客户端 key 原样转发（自建网关场景）
        self.upstream_openai_key = upstream_openai_key
        self.upstream_anthropic_key = upstream_anthropic_key
        self.default_uid = default_uid
        self.min_text_length = min_text_length
        self.salt_archive = Path(salt_archive) if salt_archive else None


def create_proxy_app(
    watermarker: Watermarker,
    config: Optional[ProxyConfig] = None,
    *,
    http_client: Any = None,
):
    """创建代理网关 FastAPI 应用。

    Args:
        watermarker: Watermarker 实例
        config: 代理配置
        http_client: 注入的 httpx.AsyncClient（测试用 MockTransport）

    Returns:
        FastAPI app
    """
    cfg = config or ProxyConfig()

    # ------------------------------------------------------------------
    # salt 归档（on_embed 回调 → JSONL）
    # ------------------------------------------------------------------

    def _archive_salt(result, ctx: Context) -> None:
        if cfg.salt_archive is None:
            return
        rec = {
            "ts": time.time(),
            "uid": result.user_id,
            "session_salt": result.session_salt.hex(),
            "n_bits": result.n_bits,
            "codec_mode": result.codec_mode,
        }
        with cfg.salt_archive.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    mw = WatermarkMiddleware(
        watermarker,
        min_text_length=cfg.min_text_length,
        skip_if_no_context=True,  # 查不到 UID 就不嵌
        on_embed=_archive_salt,
    )

    client: Any = http_client or httpx.AsyncClient(timeout=600.0)

    @asynccontextmanager
    async def lifespan(_app):
        # 启动：无额外初始化
        yield
        # 关闭：未注入外部 client 时释放自建连接池
        if http_client is None:
            try:
                await client.aclose()
            except Exception:
                pass

    app = FastAPI(title="AAWM Proxy", docs_url=None, redoc_url=None,
                  lifespan=lifespan)

    # ------------------------------------------------------------------
    # 身份解析：客户端 key → UID
    # ------------------------------------------------------------------

    def _resolve_uid(request: Request) -> Optional[int]:
        auth = request.headers.get("authorization", "")
        key = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not key:
            key = request.headers.get("x-api-key", "")
        if key in cfg.key_map:
            return cfg.key_map[key]
        return cfg.default_uid

    def _upstream_headers(request: Request,
                          is_anthropic: bool) -> Dict[str, str]:
        """重建转发头：换真实上游 key。"""
        headers = {
            k: v for k, v in request.headers.items()
            if k.lower() not in _HOP_HEADERS
        }
        real_key = (cfg.upstream_anthropic_key if is_anthropic
                    else cfg.upstream_openai_key)
        if real_key:
            if is_anthropic:
                headers["x-api-key"] = real_key
                headers.pop("authorization", None)
            else:
                headers["authorization"] = f"Bearer {real_key}"
                headers.pop("x-api-key", None)
        return headers

    async def _send_upstream(url: str, method: str, body: bytes,
                             headers: Dict[str, str], *, stream: bool):
        """转发上游请求（流式时保持连接不缓冲）。"""
        req = client.build_request(method, url, content=body, headers=headers)
        return await client.send(req, stream=stream)

    # ------------------------------------------------------------------
    # OpenAI 协议：/v1/chat/completions
    # ------------------------------------------------------------------

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        uid = _resolve_uid(request)
        body = await request.body()
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        stream = bool(payload and payload.get("stream"))
        headers = _upstream_headers(request, is_anthropic=False)

        upstream = await _send_upstream(
            f"{cfg.upstream_openai}/v1/chat/completions", "POST",
            body, headers, stream=stream)
        if stream:
            return _relay_sse_stream(
                upstream, uid, rewrite=_rewrite_openai_chunk,
                flush_before=b"data: [DONE]",
                tail_chunk=_openai_tail_chunk)
        return _embed_openai_json(upstream, uid)

    def _embed_openai_json(upstream, uid: Optional[int]):
        """非流式：整段嵌入 choices[0].message.content。"""
        data = _safe_json(upstream)
        if data is None or uid is None:
            return Response(content=upstream.content,
                            status_code=upstream.status_code,
                            media_type="application/json")
        try:
            for choice in data.get("choices", []):
                msg = choice.get("message") or {}
                text = msg.get("content")
                if isinstance(text, str) and text.strip():
                    marked, _ = mw.transform(text, Context(user_id=uid))
                    msg["content"] = marked
                # tool_calls 响应不改（中间件语义：工具调用参数不动）
        except Exception as e:
            logger.warning("openai embed failed, fail-open: %s", e)
            return Response(content=upstream.content,
                            status_code=upstream.status_code,
                            media_type="application/json")
        return Response(content=json.dumps(data, ensure_ascii=False),
                        status_code=upstream.status_code,
                        media_type="application/json")

    def _rewrite_openai_chunk(chunk: dict,
                              streamer: StreamingWatermarker) -> None:
        """原地改写 OpenAI 流式 chunk：delta.content 句子级嵌入后替换。"""
        for choice in chunk.get("choices", []):
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                delta["content"] = streamer.feed(text)
            # tool_calls / reasoning_content 原样透传

    def _openai_tail_chunk(text: str) -> bytes:
        chunk = {"id": "aawm-flush", "object": "chat.completion.chunk",
                 "choices": [{"index": 0, "delta": {"content": text},
                              "finish_reason": None}]}
        return b"data: " + json.dumps(chunk, ensure_ascii=False).encode(
            "utf-8") + b"\n\n"

    # ------------------------------------------------------------------
    # Anthropic 协议：/v1/messages
    # ------------------------------------------------------------------

    @app.post("/v1/messages")
    async def messages(request: Request):
        uid = _resolve_uid(request)
        body = await request.body()
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        stream = bool(payload and payload.get("stream"))
        headers = _upstream_headers(request, is_anthropic=True)

        upstream = await _send_upstream(
            f"{cfg.upstream_anthropic}/v1/messages", "POST",
            body, headers, stream=stream)
        if stream:
            return _relay_sse_stream(
                upstream, uid, rewrite=_rewrite_anthropic_chunk,
                flush_before=None, tail_chunk=_anthropic_tail_delta)
        return _embed_anthropic_json(upstream, uid)

    def _embed_anthropic_json(upstream, uid: Optional[int]):
        """非流式：嵌入 content[] 中 type==text 的块。"""
        data = _safe_json(upstream)
        if data is None or uid is None:
            return Response(content=upstream.content,
                            status_code=upstream.status_code,
                            media_type="application/json")
        try:
            for block in data.get("content", []):
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        marked, _ = mw.transform(text, Context(user_id=uid))
                        block["text"] = marked
                # tool_use / thinking 块不改
        except Exception as e:
            logger.warning("anthropic embed failed, fail-open: %s", e)
            return Response(content=upstream.content,
                            status_code=upstream.status_code,
                            media_type="application/json")
        return Response(content=json.dumps(data, ensure_ascii=False),
                        status_code=upstream.status_code,
                        media_type="application/json")

    def _rewrite_anthropic_chunk(chunk: dict,
                                 streamer: StreamingWatermarker) -> None:
        """原地改写 Anthropic 流式事件：text_delta 嵌入后替换。"""
        if (chunk.get("type") == "content_block_delta"
                and isinstance(chunk.get("delta"), dict)
                and chunk["delta"].get("type") == "text_delta"):
            text = chunk["delta"].get("text")
            if isinstance(text, str) and text:
                chunk["delta"]["text"] = streamer.feed(text)
        # thinking_delta / input_json_delta 原样透传

    def _anthropic_tail_delta(text: str) -> bytes:
        chunk = {"type": "content_block_delta", "index": 0,
                 "delta": {"type": "text_delta", "text": text}}
        return b"data: " + json.dumps(chunk, ensure_ascii=False).encode(
            "utf-8") + b"\n\n"

    # ------------------------------------------------------------------
    # OpenAI Responses 协议：/v1/responses（Codex / 新版 opencode）
    # ------------------------------------------------------------------

    @app.post("/v1/responses")
    async def responses(request: Request):
        uid = _resolve_uid(request)
        body = await request.body()
        try:
            payload = json.loads(body)
        except Exception:
            payload = None
        stream = bool(payload and payload.get("stream"))
        headers = _upstream_headers(request, is_anthropic=False)

        upstream = await _send_upstream(
            f"{cfg.upstream_openai}/v1/responses", "POST",
            body, headers, stream=stream)
        if stream:
            return _relay_sse_stream(
                upstream, uid, rewrite=_rewrite_responses_chunk,
                flush_before=b"response.completed",
                tail_chunk=_responses_tail_delta)
        return _embed_responses_json(upstream, uid)

    def _embed_responses_json(upstream, uid: Optional[int]):
        """非流式：嵌入 output[].content[].text 与顶层 output_text。

        Responses 响应里这两者是同一文本的不同视图（顶层是拼接），
        各自独立嵌入，salt 归档会各记一条。
        """
        data = _safe_json(upstream)
        if data is None or uid is None:
            return Response(content=upstream.content,
                            status_code=upstream.status_code,
                            media_type="application/json")
        try:
            for item in data.get("output", []):
                if isinstance(item, dict) and item.get("type") == "message":
                    for block in item.get("content", []):
                        if (isinstance(block, dict)
                                and block.get("type") == "output_text"):
                            text = block.get("text")
                            if isinstance(text, str) and text.strip():
                                marked, _ = mw.transform(text,
                                                         Context(user_id=uid))
                                block["text"] = marked
            # only=["output_text"] 时顶层快捷字段也要嵌
            top = data.get("output_text")
            if isinstance(top, str) and top.strip():
                marked, _ = mw.transform(top, Context(user_id=uid))
                data["output_text"] = marked
        except Exception as e:
            logger.warning("responses embed failed, fail-open: %s", e)
            return Response(content=upstream.content,
                            status_code=upstream.status_code,
                            media_type="application/json")
        return Response(content=json.dumps(data, ensure_ascii=False),
                        status_code=upstream.status_code,
                        media_type="application/json")

    def _rewrite_responses_chunk(chunk: dict,
                                 streamer: StreamingWatermarker) -> None:
        """原地改写 Responses 流式事件：output_text.delta 句子级嵌入。"""
        if chunk.get("type") == "response.output_text.delta":
            delta = chunk.get("delta")
            if isinstance(delta, str) and delta:
                chunk["delta"] = streamer.feed(delta)
        # output_text.done / function_call 等原样透传

    def _responses_tail_delta(text: str) -> bytes:
        chunk = {"type": "response.output_text.delta",
                 "item_id": "msg_aawm_flush", "output_index": 0,
                 "delta": text}
        return b"data: " + json.dumps(chunk, ensure_ascii=False).encode(
            "utf-8") + b"\n\n"

    # ------------------------------------------------------------------
    # 通用 SSE 流式中继
    # ------------------------------------------------------------------

    def _relay_sse_stream(upstream, uid: Optional[int], *,
                          rewrite, flush_before: Optional[bytes],
                          tail_chunk):
        """SSE 逐行中继：data 行解析改写，其余行原样转发。

        - flush_before：子串匹配某个 data 行（如 OpenAI 的 ``[DONE]``、
          Responses 的 ``response.completed``），在转发该行前先补发 flush 尾句
        - Anthropic 无终止哨兵行 → 流末尾补发尾句 delta
        """
        streamer = StreamingWatermarker(mw) if uid is not None else None
        if streamer is not None:
            # 整流共享同一 session_salt → 拼接后整段可溯源
            from ..keys import generate_session_salt
            streamer.feed("", Context(user_id=uid,
                                      session_salt=generate_session_salt()))
        def _is_flush(line: bytes) -> bool:
            return flush_before is not None and flush_before in line

        def _emit_tail() -> Optional[bytes]:
            if streamer is None:
                return None
            tail = streamer.flush()
            return tail_chunk(tail) if tail else None

        async def gen() -> AsyncIterator[bytes]:
            try:
                async for raw_line in upstream.aiter_lines():
                    line = (raw_line.encode("utf-8")
                            if isinstance(raw_line, str) else raw_line)
                    if (streamer is None
                            or not line.startswith(b"data: ")
                            or _is_flush(line)):
                        # 哨兵行（[DONE]/completed）：先把尾句发出去再转发
                        if _is_flush(line):
                            tail = _emit_tail()
                            if tail:
                                yield tail
                        yield line + b"\n"
                        if line.startswith(b"data: ") and streamer is not None:
                            yield b"\n"
                        continue
                    try:
                        chunk = json.loads(line[6:])
                    except Exception:
                        yield line + b"\n\n"
                        continue
                    try:
                        rewrite(chunk, streamer)
                    except Exception as e:
                        logger.warning("stream embed failed, fail-open: %s", e)
                    yield (b"data: "
                           + json.dumps(chunk, ensure_ascii=False).encode(
                               "utf-8") + b"\n\n")
                # 流自然结束（Anthropic）：补发尾句
                if streamer is not None and flush_before is None:
                    tail = _emit_tail()
                    if tail:
                        yield tail
            except Exception as e:
                logger.warning("stream relay aborted, fail-open: %s", e)

        return StreamingResponse(
            gen(), media_type="text/event-stream",
            background=BackgroundTask(_close_upstream, upstream))

    async def _close_upstream(upstream) -> None:
        try:
            await upstream.aclose()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 兜底：其余路径原样反向代理
    # ------------------------------------------------------------------

    @app.api_route("/{path:path}",
                   methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def passthrough(path: str, request: Request):
        url = f"{cfg.upstream_openai}/{path}"
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in _HOP_HEADERS}
        upstream = await _send_upstream(url, request.method,
                                        await request.body(), headers,
                                        stream=False)
        return Response(content=upstream.content,
                        status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type"))

    # ------------------------------------------------------------------
    # 兜底：其余路径原样反向代理
    # ------------------------------------------------------------------

    return app


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------

def _safe_json(upstream):
    try:
        return upstream.json()
    except Exception:
        return None
