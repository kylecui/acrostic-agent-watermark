#!/usr/bin/env bash
# 安装 aawm 全局命令 shim（供任意 agent / 终端发现）
#
# 背景：aawm 以 editable 模式装进 WorkBuddy 内置 python（其 Scripts 目录不在
# PATH），agent 检查 `command -v aawm` 会失败，从而"找不到水印工具"。
# 本脚本在用户 bin 目录（已在 PATH）生成 aawm / aawm-embed / aawm-trace
# 三个 shim（bash + cmd 双版本），使 bash / PowerShell / cmd 环境都能发现。
#
# 用法: bash install_local_shim.sh
# 重跑时机: WorkBuddy 升级内置 python 版本导致 shim 失效时
set -eu

# 1. 定位 WorkBuddy 内置 python（取已安装的最新版本）
PYVERS_DIR="$HOME/.workbuddy/binaries/python/versions"
PYEXE=""
if [ -d "$PYVERS_DIR" ]; then
    # glob 版本目录，取版本号最大者（vX.Y.Z 排序）
    for d in "$PYVERS_DIR"/*/; do
        [ -x "$d/python.exe" ] || continue
        ver="$(basename "$d")"
        if [ -z "$PYEXE" ] || [ "$ver" \> "$(basename "$(dirname "$PYEXE")")" ]; then
            PYEXE="$d/python.exe"
        fi
    done
fi
if [ -z "$PYEXE" ]; then
    echo "错误: 未找到 WorkBuddy 内置 python（$PYVERS_DIR）" >&2
    exit 1
fi
PYEXE_WIN="$(cygpath -w "$PYEXE")"
echo "使用 python: $PYEXE"

# 2. 目标目录（须在 PATH 中；注册表用户 PATH 已含 C:\Users\<user>\bin）
BIN_DIR="${AAWM_BIN_DIR:-$HOME/bin}"
mkdir -p "$BIN_DIR"
BIN_DIR_WIN="$(cygpath -w "$BIN_DIR")"
echo "安装到: $BIN_DIR_WIN"

# 3. 生成 shim
cat > "$BIN_DIR/aawm" <<EOF
#!/usr/bin/env bash
# AAWM CLI 全局入口（install_local_shim.sh 生成）
if [ -x "$PYEXE" ]; then
    exec "$PYEXE" -m aawm.cli "\$@"
fi
exec python -m aawm.cli "\$@"
EOF

cat > "$BIN_DIR/aawm-embed" <<EOF
#!/usr/bin/env bash
# aawm embed 语义化别名（install_local_shim.sh 生成）
exec "$PYEXE" -m aawm.cli embed "\$@"
EOF

cat > "$BIN_DIR/aawm-trace" <<EOF
#!/usr/bin/env bash
# aawm trace 语义化别名（install_local_shim.sh 生成）
exec "$PYEXE" -m aawm.cli trace "\$@"
EOF

cat > "$BIN_DIR/aawm.cmd" <<EOF
@echo off
rem AAWM CLI 全局入口（install_local_shim.sh 生成）
"$PYEXE_WIN" -m aawm.cli %*
EOF

cat > "$BIN_DIR/aawm-embed.cmd" <<EOF
@echo off
rem aawm embed 语义化别名（install_local_shim.sh 生成）
"$PYEXE_WIN" -m aawm.cli embed %*
EOF

cat > "$BIN_DIR/aawm-trace.cmd" <<EOF
@echo off
rem aawm trace 语义化别名（install_local_shim.sh 生成）
"$PYEXE_WIN" -m aawm.cli trace %*
EOF

chmod +x "$BIN_DIR/aawm" "$BIN_DIR/aawm-embed" "$BIN_DIR/aawm-trace"
echo "完成。验证:"
echo "  bash  -> command -v aawm && aawm --help"
echo "  其它  -> $BIN_DIR_WIN 下的 aawm.cmd / aawm-embed.cmd / aawm-trace.cmd"
