"""AAWM CLI：命令行工具。

命令：
    aawm keygen --output key.json        生成 master_key
    aawm registry add <alias> [--uid N]  注册用户
    aawm registry list                   列出所有用户
    aawm registry find <uid>             查 UID 对应别名
    aawm embed --key <file> --user <id> [--registry <file>] <input.txt> [-o marked.txt]
    aawm trace --key <file> [--registry <file>] [--salt <hex>] <suspect.txt>
    aawm find-meta --key <file> <suspect.txt> <meta目录或glob>  查找匹配的 meta
    aawm serve --key <file> [--registry <file>] --port 8765
    aawm proxy --key <file> --key-map keys.json [--port 8787]
            [--upstream-openai URL] [--upstream-anthropic URL]
            [--salt-archive salts.jsonl]
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

# 支持 ``python -m aawm.cli`` 和直接 ``aawm`` 两种调用
try:
    from .plugins import UIDRegistry, Watermarker
    from .plugins.keystore import KeyStore
except ImportError:
    # 直接运行时
    import os
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from aawm.plugins import UIDRegistry, Watermarker
    from aawm.plugins.keystore import KeyStore


def main(argv: Optional[List[str]] = None) -> int:
    parser = _make_parser()
    args = parser.parse_args(argv)

    if args.command == "keygen":
        return _cmd_keygen(args)
    elif args.command == "registry":
        return _cmd_registry(args)
    elif args.command == "embed":
        return _cmd_embed(args)
    elif args.command == "trace":
        return _cmd_trace(args)
    elif args.command == "find-meta":
        return _cmd_find_meta(args)
    elif args.command == "serve":
        return _cmd_serve(args)
    elif args.command == "proxy":
        return _cmd_proxy(args)
    else:
        parser.print_help()
        return 1


# ----------------------------------------------------------------------
# keygen
# ----------------------------------------------------------------------

def _cmd_keygen(args: argparse.Namespace) -> int:
    ks = KeyStore()
    if args.output:
        ks.save(args.output)
        print(f"master_key 已保存到 {args.output}")
    elif args.env:
        print(ks.export_env(args.env))
    else:
        # 默认输出 hex 到 stdout
        print(ks.get().hex())
    return 0


# ----------------------------------------------------------------------
# registry
# ----------------------------------------------------------------------

def _cmd_registry(args: argparse.Namespace) -> int:
    if args.registry_action == "add":
        reg = UIDRegistry(backend="file", path=args.registry) if args.registry else UIDRegistry()
        uid = reg.register(args.alias, uid=args.uid)
        print(f"注册成功: {args.alias} -> UID 0x{uid:04X} ({uid})")
        return 0
    elif args.registry_action == "list":
        reg = UIDRegistry(backend="file", path=args.registry) if args.registry else UIDRegistry()
        entries = reg.list_all()
        if not entries:
            print("（注册库为空）")
        else:
            print(f"{'UID':>8}  {'Alias':<30}")
            print("-" * 40)
            for uid, alias in sorted(entries.items()):
                print(f"0x{uid:04X}    {alias}")
        return 0
    elif args.registry_action == "find":
        reg = UIDRegistry(backend="file", path=args.registry) if args.registry else UIDRegistry()
        # 支持 0x 前缀
        uid_str = args.uid_str
        uid = int(uid_str, 16) if uid_str.startswith("0x") else int(uid_str)
        alias = reg.lookup(uid)
        if alias:
            print(f"UID 0x{uid:04X} -> {alias}")
            return 0
        else:
            print(f"UID 0x{uid:04X} 未注册")
            return 1
    return 1


# ----------------------------------------------------------------------
# embed
# ----------------------------------------------------------------------

def _cmd_embed(args: argparse.Namespace) -> int:
    # 加载密钥
    ks = KeyStore.from_any(key_file=args.key, master_key=args.key_hex)
    # 加载注册库
    reg = UIDRegistry(backend="file", path=args.registry) if args.registry else None
    # 创建 Watermarker
    wm = _make_watermarker(args, ks, reg)

    # 读取输入文本
    text = _read_input(args.input)

    # 解析 user_id
    user_id = _parse_user_id(args.user)

    # 嵌入
    result = wm.embed(text, user_id=user_id, sign=not args.no_sign,
                      n_bits=args.n_bits)

    # 输出
    if args.output:
        Path(args.output).write_text(result.watermarked_text, encoding="utf-8")
        # 同时保存 salt 和 seal
        meta_file = Path(args.output).with_suffix(".meta.json")
        meta = {
            "session_salt": result.session_salt.hex(),
            "user_id": result.user_id,
            "user_alias": result.user_alias,
            "has_seal": result.seal is not None,
            "existence_score": result.existence_score,
            "codec_mode": result.codec_mode,
            "bands": result.bands,
            "capacity": result.capacity,
            "n_bits": result.n_bits,
        }
        if result.seal:
            meta["seal"] = {
                "merkle_root": result.seal.merkle_root.hex(),
                "para_hashes": [h.hex() for h in result.seal.para_hashes],
                "aad": result.seal.aad.hex(),
                "version": result.seal.version,
            }
        meta_file.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"水印文本已保存到 {args.output}")
        print(f"元数据（salt+seal+bands）已保存到 {meta_file}")
    else:
        print(result.watermarked_text)

    print(f"\n[统计] UID=0x{result.user_id:04X}, 词典命中={result.n_dict_words}, "
          f"存在性={result.existence_score:.1f}", file=sys.stderr)
    if result.codec_mode != "default":
        print(f"[自适应] 模式={result.codec_mode}, 容量={result.capacity} bit, "
              f"编码={result.n_bits} bit, bands={result.bands}", file=sys.stderr)
    return 0


# ----------------------------------------------------------------------
# trace
# ----------------------------------------------------------------------

def _load_meta(path: str) -> dict:
    """加载 meta JSON，返回 {salt, seal, bands, n_bits, raw}。缺项为 None。"""
    from aawm.binding import BindingSeal
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    salt = bytes.fromhex(raw["session_salt"]) if "session_salt" in raw else None
    seal = None
    if "seal" in raw:
        seal = BindingSeal(
            merkle_root=bytes.fromhex(raw["seal"]["merkle_root"]),
            para_hashes=[bytes.fromhex(h) for h in raw["seal"]["para_hashes"]],
            aad=bytes.fromhex(raw["seal"]["aad"]),
            version=raw["seal"]["version"],
        )
    bands = list(raw["bands"]) if raw.get("bands") else None
    n_bits = raw.get("n_bits") if bands else None
    return {"salt": salt, "seal": seal, "bands": bands,
            "n_bits": n_bits, "raw": raw}


def _cmd_trace(args: argparse.Namespace) -> int:
    ks = KeyStore.from_any(key_file=args.key, master_key=args.key_hex)
    reg = UIDRegistry(backend="file", path=args.registry) if args.registry else None
    wm = _make_watermarker(args, ks, reg)

    text = _read_input(args.input)

    # 加载 salt 和 seal
    session_salt = None
    seal = None
    bands = None
    n_bits = None
    if args.salt:
        session_salt = bytes.fromhex(args.salt)
    if args.meta:
        m = _load_meta(args.meta)
        session_salt = m["salt"] or session_salt
        seal = m["seal"]
        bands = m["bands"]
        n_bits = m["n_bits"]

    trace = wm.trace(text, session_salt=session_salt, seal=seal,
                     bands=bands, n_bits=n_bits)

    # 输出
    print(f"检出水印: {'是' if trace.watermarked else '否'}")
    if trace.uid is not None:
        print(f"解码 UID: 0x{trace.uid:04X}")
    if trace.user:
        print(f"匹配用户: {trace.user} (汉明距={trace.hamming_dist})")
    elif reg is not None and trace.uid is not None:
        print(f"匹配用户: 未匹配 (最近邻汉明距={trace.hamming_dist})")
    print(f"置信度: {trace.confidence:.2f}")
    print(f"存在性得分: {trace.existence_score:.1f}")
    print(f"词典命中: {trace.n_dict_words}")
    if bands:
        print(f"自适应: 容量={trace.capacity} bit, 存活带={trace.active_bands}/{len(bands)}")
    if trace.tampered is not None:
        print(f"篡改判定: {'是' if trace.tampered else '否'}")
        if trace.tampered_paragraphs:
            print(f"被改段落: {trace.tampered_paragraphs}")
    return 0 if trace.watermarked else 2


# ----------------------------------------------------------------------
# find-meta
# ----------------------------------------------------------------------

def _resolve_meta_candidates(patterns: List[str]) -> List[Path]:
    """展开候选 meta：目录→递归 *.meta.json；通配符→glob；否则当文件。"""
    out: List[Path] = []
    seen = set()
    for pat in patterns:
        p = Path(pat)
        if p.is_dir():
            files = sorted(p.rglob("*.meta.json"))
        elif any(ch in pat for ch in "*?["):
            files = [Path(x) for x in sorted(glob.glob(pat, recursive=True))]
        else:
            files = [p]
        for f in files:
            key = str(f.resolve()) if f.exists() else str(f)
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out


def _cmd_find_meta(args: argparse.Namespace) -> int:
    """为嫌疑文本在存档 meta 中找出匹配的一份。

    问题场景：拿到一篇待验证文本，但不知道嵌入时的 meta（salt+bands）
    存档在哪。没有正确 meta，信道 B 解码会用错误码本而漏检。

    两级策略：
    1. 段落哈希匹配（免密钥）：seal.para_hashes 存的是段落规范化文本
       的纯 SHA256（非密钥化），嫌疑文本逐段哈希求交集即可定位——
       即使文本被部分改写，未改段落仍匹配。
    2. 信道 B 回退（需 key）：对候选逐个用其 salt+bands 跑 trace，
       以检出与否裁决（无 seal 的 meta 只能走这条路）。
    """
    text = _read_input(args.input)
    from aawm.binding import split_paragraphs
    paras = split_paragraphs(text)
    text_hashes = {hashlib.sha256(p.encode("utf-8")).digest() for p in paras}

    candidates = _resolve_meta_candidates(args.metas)
    if not candidates:
        print("未找到候选 meta 文件", file=sys.stderr)
        return 1

    ranked = []  # (overlap, n_archived, path, meta)
    for mp in candidates:
        try:
            m = _load_meta(str(mp))
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            print(f"跳过无法解析的 meta: {mp}", file=sys.stderr)
            continue
        phs = set(m["seal"].para_hashes) if m["seal"] else set()
        ranked.append((len(text_hashes & phs), len(phs), mp, m))

    ranked.sort(key=lambda r: -r[0])

    print(f"候选 meta: {len(ranked)} 份，嫌疑文本段落数: {len(paras)}")
    print("\n[段落哈希匹配]（免密钥，命中文本中未改写的段落）")
    any_overlap = False
    for overlap, n_arch, mp, m in ranked[:args.top]:
        if overlap == 0 and any_overlap:
            break
        raw = m["raw"]
        mark = " ← 命中" if overlap else ""
        print(f"  {overlap}/{len(paras)} 段匹配（存档 {n_arch} 段）  "
              f"UID={raw.get('user_id', '?')} ({raw.get('user_alias', '?')})  "
              f"{mp}{mark}")
        if overlap:
            any_overlap = True
    if not any_overlap:
        print("  （无命中：meta 无 seal（--no-sign 嵌入）或文本被整段改写）")

    if not (args.key or args.key_hex):
        return 0 if any_overlap else 2

    # 信道 B 确认/回退
    ks = KeyStore.from_any(key_file=args.key, master_key=args.key_hex)
    reg = UIDRegistry(backend="file", path=args.registry) if args.registry else None
    default_mode = getattr(args, "codec_mode", None) or "zero_cost"
    wm_cache = {default_mode: _make_watermarker(args, ks, reg)}

    def wm_for(meta: dict):
        """按 meta 自身 codec_mode 构建 Watermarker（不同文件可能异模式）。"""
        mode = meta["raw"].get("codec_mode") or default_mode
        if mode not in wm_cache:
            import copy as _copy
            ns = _copy.copy(args)
            ns.codec_mode = mode
            wm_cache[mode] = _make_watermarker(ns, ks, reg)
        return wm_cache[mode]

    to_try = [r for r in ranked if r[0] > 0] or ranked
    print(f"\n[信道 B 验证]（用各 meta 的 salt+bands 解码，最多试 {args.max_trace} 份）")
    found = None
    for i, (overlap, _, mp, m) in enumerate(to_try):
        if i >= args.max_trace:
            break
        t = wm_for(m).trace(text, session_salt=m["salt"], seal=m["seal"],
                            bands=m["bands"], n_bits=m["n_bits"])
        status = "检出" if t.watermarked else "未检出"
        detail = ""
        if t.watermarked:
            detail = f" UID=0x{t.uid:04X}"
            if t.user:
                detail += f" 匹配 {t.user} (汉明距={t.hamming_dist})"
            found = found or (mp, m, t)
        print(f"  {mp}: {status}（存在性={t.existence_score:.1f}"
              f" 置信度={t.confidence:.2f}）{detail}")

    if found:
        mp, m, t = found
        print(f"\n结论: 匹配 meta = {mp}")
        print(f"  UID=0x{t.uid:04X}, 用户={t.user or '未匹配'}, "
              f"汉明距={t.hamming_dist}, 置信度={t.confidence:.2f}")
        if t.tampered is not None:
            print(f"  篡改判定: {'是' if t.tampered else '否'}")
            if t.tampered_paragraphs:
                print(f"  被改段落: {t.tampered_paragraphs}")
        print(f"  溯源命令: aawm trace <text> --meta \"{mp}\" ...")
        return 0
    print("\n结论: 信道 B 未检出（文本被重度改写，或候选中无正确 meta）")
    return 2


# ----------------------------------------------------------------------
# serve
# ----------------------------------------------------------------------

def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn 未安装。请运行: pip install 'aawm[server]'", file=sys.stderr)
        return 1

    ks = KeyStore.from_any(key_file=args.key, master_key=args.key_hex)
    reg = UIDRegistry(backend="file", path=args.registry) if args.registry else None
    wm = _make_watermarker(args, ks, reg)

    # 把 watermarker 注入 server 模块
    from .server.api import create_app, set_watermarker
    set_watermarker(wm)
    app = create_app()

    print(f"AAWM 检测服务启动于 http://0.0.0.0:{args.port}")
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level=args.log_level)
    return 0


# ----------------------------------------------------------------------
# proxy（CLI/IDE agent 本地网关）
# ----------------------------------------------------------------------

def _cmd_proxy(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("uvicorn 未安装。请运行: pip install 'aawm[server]'", file=sys.stderr)
        return 1

    ks = KeyStore.from_any(key_file=args.key, master_key=args.key_hex)
    reg = UIDRegistry(backend="file", path=args.registry) if args.registry else None
    wm = _make_watermarker(args, ks, reg)

    # key→UID 映射：JSON 文件 {"sk-aawm-alice": 41244, ...}（值支持 0x 前缀/别名）
    from pathlib import Path as _P
    key_map: dict = {}
    for k, v in json.loads(_P(args.key_map).read_text(encoding="utf-8")).items():
        if isinstance(v, str):
            if v.lower().startswith("0x"):
                key_map[k] = int(v, 16)
            elif v.isdigit():
                key_map[k] = int(v)
            elif reg is not None:
                uid = reg.resolve_alias(v)
                if uid is None:
                    print(f"key-map 别名未注册: {v}", file=sys.stderr)
                    return 1
                key_map[k] = uid
            else:
                print(f"key-map 值必须是 UID 数字或已注册别名: {v}", file=sys.stderr)
                return 1
        else:
            key_map[k] = int(v)

    import os
    from .proxy import ProxyConfig, create_proxy_app
    cfg = ProxyConfig(
        upstream_openai=args.upstream_openai or os.environ.get("AAWM_UPSTREAM_OPENAI", "https://api.openai.com"),
        upstream_anthropic=args.upstream_anthropic or os.environ.get("AAWM_UPSTREAM_ANTHROPIC", "https://api.anthropic.com"),
        key_map=key_map,
        upstream_openai_key=os.environ.get("OPENAI_API_KEY"),
        upstream_anthropic_key=os.environ.get("ANTHROPIC_API_KEY"),
        salt_archive=_P(args.salt_archive) if args.salt_archive else None,
    )
    app = create_proxy_app(wm, cfg)

    print(f"AAWM 代理网关启动于 http://{args.host}:{args.port}")
    print(f"  OpenAI 协议上游:    {cfg.upstream_openai}")
    print(f"  Anthropic 协议上游: {cfg.upstream_anthropic}")
    print(f"  已映射客户端 key:   {len(key_map)} 个")
    if cfg.salt_archive:
        print(f"  salt 归档: {cfg.salt_archive}")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


# ----------------------------------------------------------------------
# 内部工具
# ----------------------------------------------------------------------

def _make_watermarker(args: argparse.Namespace, ks, reg) -> Watermarker:
    """按 CLI 参数构建 Watermarker（codec_mode / 标定语料 / 补充词典）。"""
    kwargs = {}
    codec_mode = getattr(args, "codec_mode", None) or "default"
    if codec_mode != "default":
        kwargs["codec_mode"] = codec_mode
    calib_path = getattr(args, "calibrate_corpus", None)
    if calib_path:
        # 标定语料：目录（全部 .txt/.md）或单文件（UTF-8 文本）
        p = Path(calib_path)
        if p.is_dir():
            corpus = []
            for f in sorted(p.glob("*.txt")) + sorted(p.glob("*.md")):
                corpus.append(f.read_text(encoding="utf-8"))
        else:
            corpus = [p.read_text(encoding="utf-8")]
        kwargs["calibrate_corpus"] = corpus
    supp_path = getattr(args, "supplementary_dict", None)
    if supp_path:
        # 补充词典：JSON 文件 {词: [同义词, ...]}
        import json as _json
        kwargs["supplementary_dict"] = _json.loads(
            Path(supp_path).read_text(encoding="utf-8"))
    return Watermarker(keystore=ks, registry=reg,
                       language=getattr(args, "language", None) or "auto",
                       **kwargs)


def _add_codec_options(parser: argparse.ArgumentParser) -> None:
    """embed/trace/serve 共用的 codec 选项。"""
    parser.add_argument("--codec-mode", choices=["default", "zero_cost", "hybrid"],
                        default="zero_cost",
                        help="中文 codec 模式（zero_cost=零感词典（默认，高自然）；"
                             "hybrid=零感+补充词典（配 --supplementary-dict）；"
                             "default=全词林旧行为，不推荐——实测病句率高）")
    parser.add_argument("--calibrate-corpus", dest="calibrate_corpus",
                        help="p0/null 标定语料（目录或文件路径）")
    parser.add_argument("--supplementary-dict", dest="supplementary_dict",
                        help="补充词典 JSON（hybrid 模式用）：{词: [同义词, ...]}")


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="aawm",
        description="Acrostic Agent Watermark CLI",
    )
    sub = parser.add_subparsers(dest="command")

    # keygen
    p_keygen = sub.add_parser("keygen", help="生成 master_key")
    p_keygen.add_argument("--output", "-o", help="输出文件路径")
    p_keygen.add_argument("--env", help="输出 export 环境变量格式")

    # registry
    p_reg = sub.add_parser("registry", help="UID 注册库管理")
    reg_sub = p_reg.add_subparsers(dest="registry_action", required=True)
    reg_add = reg_sub.add_parser("add", help="注册用户")
    reg_add.add_argument("alias", help="用户别名")
    reg_add.add_argument("--uid", type=int, help="指定 UID（默认自动分配）")
    reg_add.add_argument("--registry", help="注册库文件路径")
    reg_list = reg_sub.add_parser("list", help="列出所有用户")
    reg_list.add_argument("--registry", help="注册库文件路径")
    reg_find = reg_sub.add_parser("find", help="查 UID 对应别名")
    reg_find.add_argument("uid_str", help="UID（支持 0x 前缀）")
    reg_find.add_argument("--registry", help="注册库文件路径")

    # embed
    p_embed = sub.add_parser("embed", help="嵌入水印")
    p_embed.add_argument("input", help="输入文件路径（- 表示 stdin）")
    p_embed.add_argument("--key", help="密钥文件路径")
    p_embed.add_argument("--key-hex", dest="key_hex", help="密钥 hex（直接传入）")
    p_embed.add_argument("--user", required=True, help="用户 ID 或别名")
    p_embed.add_argument("--registry", help="注册库文件路径")
    p_embed.add_argument("--language", choices=["en", "zh", "auto"], help="语言")
    p_embed.add_argument("--no-sign", action="store_true", help="不签信道 A")
    p_embed.add_argument("--output", "-o", help="输出文件路径")
    p_embed.add_argument("--n-bits", dest="n_bits", type=int, default=None,
                         help="自适应模式编码位数（默认满容量；"
                              "小于容量时留冗余带抗替换攻击）")
    _add_codec_options(p_embed)

    # trace
    p_trace = sub.add_parser("trace", help="溯源检测")
    p_trace.add_argument("input", help="输入文件路径（- 表示 stdin）")
    p_trace.add_argument("--key", help="密钥文件路径")
    p_trace.add_argument("--key-hex", dest="key_hex", help="密钥 hex")
    p_trace.add_argument("--registry", help="注册库文件路径")
    p_trace.add_argument("--language", choices=["en", "zh", "auto"], help="语言")
    p_trace.add_argument("--salt", help="会话盐（hex）")
    p_trace.add_argument("--meta", help="元数据文件（含 salt+seal+bands 的 JSON）")
    _add_codec_options(p_trace)

    # find-meta
    p_find = sub.add_parser(
        "find-meta", help="为嫌疑文本在存档 meta 中查找匹配的一份")
    p_find.add_argument("input", help="输入文件路径（- 表示 stdin）")
    p_find.add_argument("metas", nargs="+",
                        help="候选 meta：文件路径 / 目录（递归 *.meta.json）/ glob 模式")
    p_find.add_argument("--key", help="密钥文件路径（用于信道 B 验证）")
    p_find.add_argument("--key-hex", dest="key_hex", help="密钥 hex")
    p_find.add_argument("--registry", help="注册库文件路径")
    p_find.add_argument("--language", choices=["en", "zh", "auto"], help="语言")
    p_find.add_argument("--top", type=int, default=5,
                        help="段落哈希排名显示条数")
    p_find.add_argument("--max-trace", dest="max_trace", type=int, default=10,
                        help="信道 B 验证最多尝试的 meta 份数")
    _add_codec_options(p_find)

    # serve
    p_serve = sub.add_parser("serve", help="启动检测服务")
    p_serve.add_argument("--key", help="密钥文件路径")
    p_serve.add_argument("--key-hex", dest="key_hex", help="密钥 hex")
    p_serve.add_argument("--registry", help="注册库文件路径")
    p_serve.add_argument("--port", type=int, default=8765, help="监听端口")
    p_serve.add_argument("--log-level", default="info", help="日志级别")
    _add_codec_options(p_serve)

    # proxy
    p_proxy = sub.add_parser("proxy", help="启动 CLI/IDE agent 代理网关")
    p_proxy.add_argument("--key", help="密钥文件路径")
    p_proxy.add_argument("--key-hex", dest="key_hex", help="密钥 hex")
    p_proxy.add_argument("--registry", help="注册库文件路径")
    p_proxy.add_argument("--key-map", dest="key_map", required=True,
                         help="客户端 key→UID 映射 JSON：{\"sk-aawm-alice\": 41244}")
    p_proxy.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_proxy.add_argument("--port", type=int, default=8787, help="监听端口")
    p_proxy.add_argument("--upstream-openai", dest="upstream_openai",
                         help="OpenAI 协议上游 base URL（默认环境变量 AAWM_UPSTREAM_OPENAI）")
    p_proxy.add_argument("--upstream-anthropic", dest="upstream_anthropic",
                         help="Anthropic 协议上游 base URL（默认环境变量 AAWM_UPSTREAM_ANTHROPIC）")
    p_proxy.add_argument("--salt-archive", dest="salt_archive",
                         help="salt 归档 JSONL 文件（溯源必用）")
    p_proxy.add_argument("--log-level", default="info", help="日志级别")
    _add_codec_options(p_proxy)

    return parser


def _read_input(path: str) -> str:
    if path == "-":
        return sys.stdin.read()
    return Path(path).read_text(encoding="utf-8")


def _parse_user_id(s: str):
    """解析 user_id：数字→int，否则保留字符串。"""
    s = s.strip()
    if s.lower().startswith("0x"):
        try:
            return int(s, 16)
        except ValueError:
            return s
    try:
        return int(s)
    except ValueError:
        return s


if __name__ == "__main__":
    sys.exit(main())
