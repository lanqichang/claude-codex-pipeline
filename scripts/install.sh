#!/usr/bin/env bash
# 流水线模式安装脚本：建 cxp 命令、装评委、体检。可重复运行，不改动你的其他配置。
set -euo pipefail

SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN_DIR="${HOME}/.local/bin"
AGENT_DIR="${HOME}/.claude/agents"

command -v python3 >/dev/null 2>&1 || { echo "❌ 需要 Python 3，请先安装"; exit 1; }

# 1. cxp 命令
mkdir -p "$BIN_DIR"
chmod +x "$SKILL_DIR/scripts/codex_dispatch.py"
ln -sf "$SKILL_DIR/scripts/codex_dispatch.py" "$BIN_DIR/cxp"
echo "✅ 命令已就绪：$BIN_DIR/cxp"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "⚠️ $BIN_DIR 不在 PATH 里。把下面这行加进 ~/.zshrc 或 ~/.bashrc 后重开终端："
     echo "   export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# 2. 评委（Claude Code 的 subagent），写入本 skill 的实际位置
mkdir -p "$AGENT_DIR"
sed "s|__SKILL_DIR__|$SKILL_DIR|g" "$SKILL_DIR/agents/pipeline-reviewer.md" > "$AGENT_DIR/pipeline-reviewer.md"
echo "✅ 评委已安装：$AGENT_DIR/pipeline-reviewer.md（新开一个 Claude Code 会话后生效）"

# 3. 体检：Codex 在不在、有没有登录、三档对应什么型号
echo "—— 体检 ——"
"$BIN_DIR/cxp" check || {
  echo "⚠️ Codex 还没准备好，按上面的提示处理后再运行一次：cxp check"
  exit 1
}
echo "🎉 安装完成。打开 Claude Code，说一句「流水线」加上你要做的事。"
