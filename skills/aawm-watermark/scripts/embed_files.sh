#!/usr/bin/env bash
# AAWM 交付物水印脚本（fail-open）—— 对落盘文本文件原位嵌入可溯源水印。
#
# 用法:
#   embed_files.sh <文件路径> [更多文件...]      # 空格分隔（SKILL 调用）
#   embed_files.sh --comma "a.txt,b.txt"         # 逗号分隔（Claude Code hooks 的 CLAUDE_FILE_PATHS）
#
# 环境变量:
#   AAWM_USER       用户身份：UID 数字或注册库别名。必填。
#   AAWM_KEY        密钥文件路径（与 AAWM_KEY_HEX 二选一）。必填。
#   AAWM_KEY_HEX    密钥 hex。
#   AAWM_REGISTRY   注册库 JSON 路径（推荐，别名解析与溯源需要）。
#   AAWM_LANGUAGE   auto|zh|en，默认 auto。
#   AAWM_CODEC               zero_cost|default|hybrid，默认 zero_cost（零感词典，高自然）。
#                            设置了 AAWM_SUPPLEMENTARY_DICT 而 AAWM_CODEC 未设时自动用 hybrid。
#   AAWM_SUPPLEMENTARY_DICT  补充词典 JSON 路径（hybrid 模式）。词条质量铁律见
#                            SKILL.md §4——坏词条 = 交付物病句。溯源须配同一份。
#   AAWM_CALIB      p0/null 标定语料路径（可选，目录或文件）。
#   AAWM_DRY_RUN    非空时只打印将执行的命令，不真正嵌入。
#
# 行为（fail-open 铁律）:
#   - 任一文件嵌入失败 → 保留原文件，警告到 stderr，不中断其余文件
#   - 成功 → 原文件被替换为带水印版本，元数据写入 <文件>.meta.json
#   - 二进制/非文本扩展名自动跳过
set -u

# ----------------------------------------------------------------------
# 路径规范化：把 bash 可访问的路径转成原生 Python 可访问的 Windows
# 绝对路径（正斜杠形式）。关键坑：cygpath -w 对 /tmp（usertemp 挂载）
# 的解析与实际目录错位，而 pwd -W 返回真实位置，故以 pwd -W 为准。
# ----------------------------------------------------------------------
norm_path() {
    case "$(uname -s)" in
        MINGW*|MSYS*|CYGWIN*)
            local p="$1"
            # 已是 Windows 盘符/UNC 路径 → 原样返回
            case "$p" in
                [A-Za-z]:[\\/]*|\\\\*) echo "$p"; return ;;
            esac
            # POSIX 路径：dirname 存在 → 用 pwd -W 求真实 Windows 目录
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
            # 兜底：cygpath 尽力转换，失败原样返回
            cygpath -w "$p" 2>/dev/null || echo "$p"
            ;;
        *) echo "$1" ;;
    esac
}

# ----------------------------------------------------------------------
# 文件存在性/真实路径解析：Windows Git Bash 对含中文的路径做字面量
# test（[ -f ]）时因 NFD/NFC 归一化差异会误判"不存在"，但"目录字面量 +
# ASCII 后缀 glob"（如 某目录/*.md）展开稳定，故用 glob 兜底取真实路径。
# ----------------------------------------------------------------------
resolve_file() {
    local p="$1"
    [ -e "$p" ] && { echo "$p"; return 0; }
    local d sfx m
    d="$(dirname "$p")"
    sfx=".${p##*.}"
    for m in "$d"/*"$sfx"; do
        [ -e "$m" ] && { echo "$m"; return 0; }
    done
    return 1
}

# ----------------------------------------------------------------------
# 解析参数
# ----------------------------------------------------------------------
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
    echo "用法: embed_files.sh <文件...> | --comma <csv>" >&2
    exit 1
fi

if [ -z "${AAWM_USER:-}" ]; then
    echo "错误: 环境变量 AAWM_USER 未设置（UID 数字或注册库别名）。" >&2
    echo "      例如: export AAWM_USER=alice" >&2
    exit 1
fi
if [ -z "${AAWM_KEY:-}" ] && [ -z "${AAWM_KEY_HEX:-}" ]; then
    echo "错误: AAWM_KEY 或 AAWM_KEY_HEX 必须设置一个。" >&2
    exit 1
fi

# ----------------------------------------------------------------------
# 定位 aawm 可执行（已安装命令 或 python -m aawm.cli）
# ----------------------------------------------------------------------
AAWM_BIN=()
if command -v aawm >/dev/null 2>&1; then
    AAWM_BIN=(aawm)
elif python -c "import aawm" >/dev/null 2>&1; then
    AAWM_BIN=(python -m aawm.cli)
else
    echo "错误: 找不到 aawm。请先安装: pip install aawm （或设置 PYTHONPATH 指向 src/）" >&2
    exit 1
fi

# 通用参数（传给 Python 的路径统一 norm 为 Windows 绝对路径）
COMMON_ARGS=()
[ -n "${AAWM_KEY:-}" ] && COMMON_ARGS+=(--key "$(norm_path "$AAWM_KEY")")
[ -n "${AAWM_KEY_HEX:-}" ] && COMMON_ARGS+=(--key-hex "$AAWM_KEY_HEX")
[ -n "${AAWM_REGISTRY:-}" ] && COMMON_ARGS+=(--registry "$(norm_path "$AAWM_REGISTRY")")
[ -n "${AAWM_LANGUAGE:-}" ] && COMMON_ARGS+=(--language "$AAWM_LANGUAGE")
[ -n "${AAWM_CALIB:-}" ] && COMMON_ARGS+=(--calibrate-corpus "$(norm_path "$AAWM_CALIB")")

# 补充词典（hybrid）：给出且 AAWM_CODEC 未设时自动用 hybrid
SUPP="${AAWM_SUPPLEMENTARY_DICT:-}"
if [ -n "$SUPP" ] && ! resolve_file "$SUPP" >/dev/null; then
    echo "错误: AAWM_SUPPLEMENTARY_DICT 指向的文件不存在: $SUPP" >&2
    exit 1
fi
if [ -n "$SUPP" ]; then
    SUPP="$(resolve_file "$SUPP")"
    [ -z "${AAWM_CODEC:-}" ] && CODEC="hybrid" || CODEC="$AAWM_CODEC"
else
    CODEC="${AAWM_CODEC:-zero_cost}"
fi
COMMON_ARGS+=(--codec-mode "$CODEC")
[ -n "$SUPP" ] && COMMON_ARGS+=(--supplementary-dict "$(norm_path "$SUPP")")

# 未配置标定语料时提示（不阻塞，仅提醒：短文本存在性判定可能不可靠）
if [ -z "${AAWM_CALIB:-}" ]; then
    echo "提示: 未设置 AAWM_CALIB（标定语料）。未标定时短文本存在性阈值偏严，" >&2
    echo "      溯源可能漏检；生产建议配置后 embed 并复核 trace。" >&2
fi

# 跳过非文本扩展名（二进制 / meta 文件自身）
skip_ext() {
    case "$1" in
        *.meta.json|*.png|*.jpg|*.jpeg|*.gif|*.webp|*.bmp|*.ico|*.pdf|*.doc|*.docx|*.xls|*.xlsx|*.ppt|*.pptx|*.zip|*.gz|*.tar|*.7z|*.rar|*.whl|*.exe|*.dll|*.so|*.dylib|*.pyc|*.pyo|*.class|*.o|*.a)
            return 0 ;;
        *) return 1 ;;
    esac
}

embed_one() {
    local f
    f="$(resolve_file "$1")" || { echo "跳过: $1（文件不存在）" >&2; return 0; }
    if skip_ext "$f"; then
        echo "跳过: $f（非文本/二进制）" >&2
        return 0
    fi

    # 先写临时文件，成功后再原子替换 → 失败绝不破坏原文件
    local tmp tmp_norm f_norm
    tmp="$(mktemp "${TMPDIR:-/tmp}/aawm_embed.XXXXXX")"
    tmp_norm="$(norm_path "$tmp")"
    f_norm="$(norm_path "$f")"
    local tmp_meta="${tmp%.*}.meta.json"

    local -a cmd_args=("${AAWM_BIN[@]}" embed "$f_norm" --user "$AAWM_USER" \
                       -o "$tmp_norm" "${COMMON_ARGS[@]}")
    if [ -n "${AAWM_DRY_RUN:-}" ]; then
        printf '%q ' "${cmd_args[@]}"; echo
        rm -f "$tmp"
        return 0
    fi

    if ! "${cmd_args[@]}" >/dev/null 2>&1; then
        echo "WARN: 嵌入失败，保留原文件: $f" >&2
        rm -f "$tmp" "$tmp_meta"
        return 0
    fi

    mv -f "$tmp" "$f"
    if [ -f "$tmp_meta" ]; then
        mv -f "$tmp_meta" "$f.meta.json"
    fi
    echo "已嵌入水印: $f（meta: $f.meta.json）"
}

for f in "${files[@]}"; do
    embed_one "$f"
done

exit 0
