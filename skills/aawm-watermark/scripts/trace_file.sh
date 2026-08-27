#!/usr/bin/env bash
# AAWM 溯源脚本 —— 对可疑文本文件执行水印检出。
#
# 用法:
#   trace_file.sh <可疑文件> [更多文件...]
#   trace_file.sh --comma "a.txt,b.txt"
#
# 环境变量:
#   AAWM_KEY / AAWM_KEY_HEX  密钥（必填）
#   AAWM_REGISTRY            注册库 JSON 路径（推荐，输出匹配用户别名）
#   AAWM_LANGUAGE            默认 auto
#   AAWM_CODEC               默认 zero_cost（须与嵌入时一致）
#   AAWM_CALIB               标定语料（须与嵌入时一致，提升存在性判定）
#
# 行为:
#   - 自动读取 <文件>.meta.json 作为 salt/seal/bands 输入；meta 缺失时盲检
#   - 直接透传 aawm trace 的可读输出
#   - 任一文件检出 → 退出码 0；全部未检出 → 退出码 2
set -u

# ----------------------------------------------------------------------
# 路径规范化：把 bash 可访问的路径转成原生 Python 可访问的 Windows
# 绝对路径（正斜杠形式）。cygpath -w 对 /tmp（usertemp 挂载）的解析
# 与实际目录错位，pwd -W 返回真实位置，故以 pwd -W 为准。
# ----------------------------------------------------------------------
norm_path() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*)
            local p="$1"
            case "$p" in
                [A-Za-z]:[\\/]*|\\\\*) echo "$p"; return ;;
            esac
            local dir fname
            dir="$(dirname "$p")"
            fname="$(basename "$p")"
            if [ -d "$dir" ]; then
                local base
                base="$(cd "$dir" && pwd -W 2>/dev/null)"
                if [ -n "$base" ]; then
                    echo "$base/$fname"
                    return
                fi
            fi
            cygpath -w "$p" 2>/dev/null || echo "$p"
            ;;
        *) echo "$1" ;;
    esac
}

files=()
if [ "${1:-}" = "--comma" ]; then
    shift
    if [ $# -eq 1 ]; then
        local_ifs="$IFS"; IFS=','
        for f in $1; do [ -n "$f" ] && files+=("$f"); done
        IFS="$local_ifs"
    else
        for a in "$@"; do
            local_ifs="$IFS"; IFS=','
            for f in $a; do [ -n "$f" ] && files+=("$f"); done
            IFS="$local_ifs"
        done
    fi
else
    files=("$@")
fi

if [ "${#files[@]}" -eq 0 ]; then
    echo "用法: trace_file.sh <可疑文件...> | --comma <csv>" >&2
    exit 1
fi
if [ -z "${AAWM_KEY:-}" ] && [ -z "${AAWM_KEY_HEX:-}" ]; then
    echo "错误: AAWM_KEY 或 AAWM_KEY_HEX 必须设置一个。" >&2
    exit 1
fi

AAWM_BIN=()
if command -v aawm >/dev/null 2>&1; then
    AAWM_BIN=(aawm)
elif python -c "import aawm" >/dev/null 2>&1; then
    AAWM_BIN=(python -m aawm.cli)
else
    echo "错误: 找不到 aawm。请先安装: pip install aawm" >&2
    exit 1
fi

COMMON_ARGS=()
[ -n "${AAWM_KEY:-}" ] && COMMON_ARGS+=(--key "$(norm_path "$AAWM_KEY")")
[ -n "${AAWM_KEY_HEX:-}" ] && COMMON_ARGS+=(--key-hex "$AAWM_KEY_HEX")
[ -n "${AAWM_REGISTRY:-}" ] && COMMON_ARGS+=(--registry "$(norm_path "$AAWM_REGISTRY")")
[ -n "${AAWM_LANGUAGE:-}" ] && COMMON_ARGS+=(--language "$AAWM_LANGUAGE")
[ -n "${AAWM_CALIB:-}" ] && COMMON_ARGS+=(--calibrate-corpus "$(norm_path "$AAWM_CALIB")")
CODEC="${AAWM_CODEC:-zero_cost}"
COMMON_ARGS+=(--codec-mode "$CODEC")

# 跳过非文本扩展名
skip_ext() {
    case "$1" in
        *.meta.json|*.png|*.jpg|*.jpeg|*.gif|*.webp|*.bmp|*.ico|*.pdf|*.doc|*.docx|*.xls|*.xlsx|*.ppt|*.pptx|*.zip|*.gz|*.tar|*.7z|*.rar|*.whl|*.exe|*.dll|*.so|*.dylib|*.pyc|*.pyo|*.class|*.o|*.a)
            return 0 ;;
        *) return 1 ;;
    esac
}

any_hit=0
for f in "${files[@]}"; do
    if [ ! -f "$f" ]; then
        echo "跳过: $f（文件不存在）" >&2
        continue
    fi
    if skip_ext "$f"; then
        echo "跳过: $f（非文本/二进制）" >&2
        continue
    fi
    echo "===== 溯源: $f ====="
    f_norm="$(norm_path "$f")"
    local_meta="${f}.meta.json"
    local_args=()
    [ -f "$local_meta" ] && local_args+=(--meta "$(norm_path "$local_meta")")

    # shellcheck disable=SC2086
    "${AAWM_BIN[@]}" trace "$f_norm" "${local_args[@]}" "${COMMON_ARGS[@]}"
    rc=$?
    if [ $rc -eq 0 ]; then
        any_hit=1
    fi
    echo ""
done

[ "$any_hit" -eq 1 ]
rc=$?
if [ $rc -ne 0 ] && [ -z "${AAWM_CALIB:-}" ]; then
    echo "" >&2
    echo "提示: 未检出，且未设置 AAWM_CALIB（p0/null 标定语料）。" >&2
    echo "      未标定时存在性阈值偏严，短文本可能漏检；" >&2
    echo "      配置标定语料后重试更可靠。见技能包 README §快速开始。" >&2
fi
exit $rc
