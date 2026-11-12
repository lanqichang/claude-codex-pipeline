# 流水线模式 · Claude × Codex Pipeline

> 对 Claude 说一句「流水线，帮我把这件事做了」。Claude 当指挥，把活拆好派给本机的 Codex，脚本盯着干，评委把关，做完再叫你。

一个 Claude Code skill，加一个约 1000 行、零依赖的 Python 派单脚本。

## 它解决什么问题

Claude 判断力强，但 token 贵；Codex 干活便宜、耐心，但需要有人派活和验收。自己在两个窗口之间来回复制粘贴，既累又容易漏。

流水线模式把这件事固定成一条线：

```
你说一句话
   ↓
① 对齐   Claude 复述「交付什么、怎么算完成」
② 派单   Claude 写任务单，后台调用 Codex（按复杂度自动选档位）
③ 监工   脚本盯着：卡死、打转、超时自动叫停；改了哪些文件由脚本对账，不信自报
④ 验收   脚本独立跑验收命令，不过就自动返工一轮
⑤ 评审   至少 2 位独立评委，各管一个维度，给灯色、分数和必修项
⑥ 返工   有必修就发回 Codex，最多三轮
   ↓
Claude 用一两句话告诉你结果，需要你拍板的事合并成一条
```

你全程不用打开 Codex，中途可以聊别的。

## 几个设计

- **不把「我做完了」当完成。** Codex 自报成功不算数，以脚本独立跑的验收命令和评委结论为准。
- **写的和评的分开。** 每单至少 2 位评委，各看一个维度（比如脚本看「正确性与边界」和「运行可靠性」）。
- **防作弊。** 验收用的测试文件可以设为受保护，Codex 改了它就直接判验收失败。
- **监工不花 token。** 盯进度、对账、跑验收、生成评审包全是脚本做的，Claude 派完单就回到对话，等回调。
- **型号不写死。** 三个档位（顶配、主力、轻快）按 Codex 官方模型清单自动选最新的，出了新模型自动顶上。
- **只在四类事上打扰你。** 需求有歧义、不可逆或对外的操作、敏感资料要外发、卡住了。其余自己定，最后列出「我替你默认了什么」。
- **不只是写代码。** 抓网页、读长材料出摘要、批量改文件、清洗数据、巡检诊断，这些量大但不费脑的活都可以派。

## 需要什么

| 需要 | 说明 |
|---|---|
| Claude Code | 当指挥。其他能跑终端命令、能派 subagent 的 Claude 宿主也可以 |
| Codex | Codex CLI（`npm i -g @openai/codex`）或 ChatGPT 桌面版，并已登录。作者实测版本为 codex-cli 0.167；版本太旧可能缺少脚本用到的参数，先升级 |
| Python 3 | 只用标准库 |
| macOS 或 Linux | 作者在 macOS 上日常使用；Linux 按设计支持但未充分测试；Windows 请用 WSL |

## 安装

**方式一：让 Claude 帮你装（推荐）**

把这句话发给 Claude Code：

```
帮我安装这个 skill：https://github.com/lanqichang/claude-codex-pipeline
放到 ~/.claude/skills/claude-codex-pipeline，然后运行里面的 scripts/install.sh。
```

**方式二：自己装**

```bash
git clone https://github.com/lanqichang/claude-codex-pipeline ~/.claude/skills/claude-codex-pipeline
bash ~/.claude/skills/claude-codex-pipeline/scripts/install.sh
```

安装脚本做三件事：建一个 `cxp` 命令（放在 `~/.local/bin`）、把评委装进 `~/.claude/agents/`、检查 Codex 有没有登录。可以重复运行。

装完**新开一个 Claude Code 会话**（评委要新会话才生效）。

想少点几次确认，可以在 Claude Code 的权限设置里放行 `Bash(cxp *)`。

## 怎么用

| 你说 | 发生什么 |
|---|---|
| **流水线**，把 docs 里的 30 篇文章统一成新的 front-matter 格式 | 启动完整流程 |
| 流水线，**上顶配**，重构登录模块 | 全用最强档位 |
| 流水线，**省着点**，…… | 档位封顶在主力档 |
| **查进度** | 每单一行：进行到哪、跑了多久、验收过没过 |
| **收工** | 停掉所有在跑的单，说明各自停在哪 |
| **准** ／ **驳：理由** ／ **准 1 3，驳 2** | 一次性答复收尾时列出的待拍板事项 |

流水线只在你说出「流水线」并明确要启用时才启动。Claude 不会因为活多或想省 token 就自己进入这个模式。

## 安全边界

- Codex 默认在沙箱里跑，只能写任务指定的工作目录。
- Codex 不做 git 提交、部署、发布、删除、上云、发消息、付费。需要这些时它会报告，由 Claude 在收尾时合并成一条问你。
- 联网单和浏览器单只读不提交。`--browser` 会放开沙箱，只在确实要开浏览器时用。
- **派给 Codex 的内容会发到 OpenAI。** 不想外发的资料，把路径片段写进 `~/.config/codex-pipeline/deny.txt`（每行一个，如 `私密资料`）。工作目录、附加目录或任务单路径命中时，脚本直接拒绝派单。它拦的是派单位置：如果把敏感文件夹的上级目录当工作目录，脚本拦不住，只剩给 Codex 的文字约束，所以别这么派。

## 也可以不通过 Claude 直接用脚本

```bash
cxp check                      # 体检：Codex 在不在、登没登录、三档对应什么型号
cxp go --title 补测试 --cwd ~/code/myapp --tier normal --verify "pytest -q" <<'EOF'
## 目标
给 utils/date.py 补齐单元测试，覆盖闰年和跨时区。
EOF
cxp status                     # 查进度
cxp close <任务id> --verdict "🟢 通过"
```

完整命令和选项见 [`references/操作手册.md`](references/操作手册.md)。

## 仓库结构

```
claude-codex-pipeline/
├── SKILL.md                     Claude 读的流程与规则
├── references/操作手册.md        命令全表、任务单模板、默认验收、红线
├── references/评审维度.md        评委规范：维度、判定标准、输出格式
├── agents/pipeline-reviewer.md  评委 subagent 定义
├── scripts/codex_dispatch.py    派单脚本（cxp）
├── scripts/tiers.json           三档模型配置
├── scripts/install.sh           安装脚本
└── LICENSE                      MIT
```

## 常见问题

**费用怎么算？** Claude 的部分只花在对齐、拆单、裁决、汇报和评委上；执行的大头走你的 Codex 额度。Codex 额度用完时脚本会给出明确提示。

**评委用什么模型？** 默认跟随你当前会话的模型。想固定用某个模型，改 `~/.claude/agents/pipeline-reviewer.md` 里的 `model`。

**为什么叫 `cxp`？** Codex Pipeline 的缩写。

**提示找不到 Codex？** 用环境变量指定：`export CODEX_BIN=/完整/路径/codex`。

**过程文件在哪？** `~/.local/share/codex-pipeline/`，每单一个文件夹，里面有任务单、事件流、评审包和评委报告，方便事后复盘。

## 许可证

MIT。

## 作者

兰启昌。微信公众号：兰启昌，长期研究财富、AI、修心并持续分享。

想给 AI 搭一个长期用的知识库，可以再看配套的 [第二大脑](https://github.com/lanqichang/second-brain-skill)。

哪里卡住、哪句话看不懂，欢迎提 Issue。
