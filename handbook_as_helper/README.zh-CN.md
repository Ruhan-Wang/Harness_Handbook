# handbook_as_helper

[English](README.md) | **中文**

用生成好的 **handbook** 来辅助 code agent。本模块只做两件事:

1. **handbook 作为 planner** —— 把 handbook 变成一个 agent SKILL,交给一个只读的
   planner,让它为一句自然语言的改动需求定位出**所有**需要改动的位置(尤其是那些分散、
   不显眼的点)。**只出规划(plan-only)**:它只产出计划,绝不改代码。
2. **handbook 重新同步(resync)** —— 当一次真实的代码改动落地后,把 handbook 的派生层
   (function 卡片、行号锚点、代码位点、索引)向前滚动到与改动一致,而无需重新生成整本
   handbook。

> planner 采用 **"recall" 扁平(flat)** 架构:一个**单独的只读 agent** 用 handbook
> (`SKILL.md` / `index.md` / `registers.md` / `stages/<id>.md`)做导航,并**亲自读真实源码**,
> 然后产出精确、逐字节的 EDIT 规划 —— 没有 `locator` 子代理,也没有 map-reduce。这是**唯一**的
> planner,就实现在 `code_agent.py` 里。(所有评测 / benchmark / 打分相关代码 —— golden 集、
> A/B 评审、`run_eval.py`、executor 阶段 —— 均已删除。)

---

## 环境准备

```bash
# 在仓库根目录下
python3 -m venv .venv && source .venv/bin/activate
pip install pyyaml requests tree-sitter tree-sitter-language-pack

# planner 构建在 NexAU 官方示例 agent 之上 —— 指向该 checkout
export NEXAU_CODE_AGENT_DIR=/path/to/NexAU/examples/code_agent   # 或放在兄弟目录 NexAU/ 下

# LLM:任意 OpenAI 兼容端点(planner 和 resync 共用同一个)
export OPENAI_API_KEY=sk-...                        # 必填
export OPENAI_MODEL=gpt-4o-mini                     # 可选(默认 gpt-4o-mini)
export OPENAI_BASE_URL=https://api.openai.com/v1    # 可选;也可指向自托管 vLLM / 代理
```

接**无需 key 的本地端点**时,请显式设置 `OPENAI_API_KEY=EMPTY`(planner 与 resync 都要求
必须有*某个* key,缺失就会明确报错)。更底层的 `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY`
仍然生效,并且优先级高于 `OPENAI_*`。

---

## 第一部分 —— handbook 作为 planner

### 1. 从 handbook 构建可供 planner 使用的 SKILL

```bash
# 通用(任意 target):从渲染好的 handbook 目录(如 work/repo/handbook 或 .../phase3/output)
# 组装出 handbook_skills/handbook_skill_<target>/
python handbook_skills/build_skill_from_handbook.py --target codex \
    --src /path/to/rendered/handbook

# 仅 Terminus-2:从完整渲染的 handbook markdown 中"雕刻"出 skill
python handbook_skills/build_handbook_skill.py
```

两者都会生成 `handbook_skills/handbook_skill_<target>/` = `SKILL.md` + `references/`
(`overview.md`、`index.md`、`registers.md`、`stages/<id>.md`)。

### 2. 针对一个改动需求运行 planner

```python
import sys; sys.path.insert(0, "pipeline")
from pathlib import Path
from code_agent import run_query               # 需要 NexAU + 上面的 OPENAI_*/LLM_* 环境

out = run_query(
    "<评审者的自然语言改动需求>",
    Path("/path/to/source"),        # 用于规划的原始代码库(必填)
    Path("runs/case1"),             # 临时沙箱:source 的 git 副本,用完即删(必填)
    # arm="handbook" 为默认(唯一的 arm)
)
print(out["plan"])                  # 定位规划;out["diff"] == ""(只出规划)
```

**工作原理**:`run_query` 先把 `pristine_dir` 用 git 快照到 `workdir`,再从 NexAU 官方
`code_agent.yaml` 构建出一个只读 planner(把导航 handbook 按路径挂上),让它只读地在沙箱上
运行,最后删除沙箱并返回规划。planner 亲自用 handbook(`SKILL.md` / `index.md` /
`registers.md` / `stages/<id>.md`)做导航,并**亲自读真实源码**,然后产出逐字节的 EDIT 规划。

---

## 第二部分 —— 代码改动后重新同步 handbook

resync **与上面的 planner 相互独立**(planner 从不改代码,因此没有可供 resync 的 diff)。你需要
提供一个描述真实改动的 *case 目录*:

```
<case_dir>/
├── edited/       改动后的源码树(如已应用某个 PR 的 checkout)                    【必需】
├── plan.md       改动说明;其中的声明块驱动对账(reconcile)                       【必需】
└── agent.diff    (可选)edited/ 相对 pristine 的 diff —— 空 diff 会被跳过
```

```bash
# 成员级(默认):把 handbook 的派生层向前滚动到该改动
python pipeline/update_handbook.py <case_dir>
python pipeline/update_handbook.py <case_dir> --no-translate   # 跳过卡片翻译这一 LLM 步骤
python pipeline/update_handbook.py <case_dir> --target codex   # 选择目标项目

# 文件级引擎(用于 large 流程生成的 skill)
HANDBOOK_GEN_SCALE=large python pipeline/update_handbook.py <case_dir>
```

resync 是**多语言**的:它从 `lang_layer` 获取 spans / 语法闸门 / 重命名指纹 / 调用图 ——
Python 走 `ast`,Rust / TypeScript / Go / … 走 `handbook_generate_small` 的 tree-sitter
适配器。只有当某语言没有已注册的适配器时才会拒绝。目标项目必须存在一份 function 级的 phase-2
映射(见下方 `PHASE2_FINAL`)。

---

## 文件说明

| 文件 | 作用 |
|------|------|
| `pipeline/targets.py` | **目标项目配置层。** 每个项目(`terminus2`、`codex`……)是一个 `Target`:原始源码路径、语言、快照忽略项、prompt 用词。新增项目只改这里,别处不动。 |
| `pipeline/code_agent.py` | **handbook planner**("recall" 扁平 arm)及其胶水层:加载并对 NexAU 官方 `code_agent.yaml` 做环境变量插值,构建仅含导航的 handbook 副本(`_ensure_nosrc_handbook`)、只读 planner(`_build` / `build_planner`)、一次性 git 沙箱(`_snapshot_git` / `_git_diff`,resync 也在用)以及容错的 agent 运行器(`_run_agent`)。导入时把 `OPENAI_*` 桥接到 `LLM_*`。入口:`run_query(query, pristine_dir, workdir, arm="handbook")`。 |
| `pipeline/update_handbook.py` | **resync 入口。** 传入 case 目录(`edited/` + `plan.md`),把 handbook 向前滚动到该改动(不重跑 agent)。按 `HANDBOOK_GEN_SCALE` 选择成员级/文件级。 |
| `pipeline/resync_handbook.py` | **成员级 resync 引擎**(A→D:语义滚动 → sha 判定 → 重新分类 → handbook 回写)。 |
| `pipeline/resync_large.py` | **文件级 resync 引擎**,用于 large 流程的 skill(以整文件为叶子;`HANDBOOK_GEN_SCALE=large`)。 |
| `pipeline/resync_llm.py` | resync 引擎共用的 LLM 后端(`EnvLLM`:在 agent 所用的同一个 OpenAI 兼容端点上,每次调用发一个裸 `/chat/completions` POST)。 |
| `pipeline/resync_decl.py` | 不依赖成员引擎的声明解析器(供文件级路径使用,从而永不导入成员引擎)。 |
| `pipeline/lang_layer.py` | 多语言基座:spans、语法闸门、重命名指纹、调用图(Python 走 `ast`,其余走 tree-sitter 适配器)。 |
| `pipeline/_recon_terminus_base.py` | Terminus-2 辅助脚本:从渲染好的 handbook 重建规范的 `PHASE2_FINAL` mapping/skeleton YAML。 |
| `handbook_skills/build_skill_from_handbook.py` | 通用 skill 构建器:从任意渲染好的 handbook 目录组装 `handbook_skill_<target>/`,让同一个 planner prompt 跨项目通用。 |
| `handbook_skills/build_handbook_skill.py` | Terminus-2 专用 skill 构建器:从渲染的 handbook markdown 雕刻出 function 卡片式 skill,并做寄存器富化。 |
| `prompts/planner_handbook.md` | planner 的 prompt(用 handbook 做路由、亲自读真实源码、产出自包含的逐字节 EDIT 块)。 |
| `rerun_resync.py` | 小众辅助:通过回放账本(ledger)在已完成的 case 上重跑 resync(翻译 vs 不翻译的消融实验)。不属于常规流程。 |

---

## 关键环境变量

| 变量 | 使用方 | 含义 |
|------|--------|------|
| `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` | planner、resync | OpenAI(或兼容)端点。默认:`gpt-4o-mini` / `https://api.openai.com/v1`。无 key 的本地端点用 `OPENAI_API_KEY=EMPTY`。 |
| `LLM_MODEL` / `LLM_BASE_URL` / `LLM_API_KEY` | planner、resync | 同一端点的更底层覆盖(优先级高于 `OPENAI_*`)。 |
| `NEXAU_CODE_AGENT_DIR` | planner | NexAU `examples/code_agent` 的路径(默认为兄弟目录 `NexAU/`)。 |
| `EVAL_TARGET` | 全部 | 当前目标项目(默认 `terminus2`,还有 `codex`……)。`--target` 可覆盖。 |
| `PRISTINE_ROOT` | 全部 | 覆盖目标的原始源码路径。 |
| `HANDBOOK_SKILL_DIR` / `HANDBOOK_RENDERED_DIR` | skill 构建 / planner | 覆盖目标的已构建 skill 目录 / 渲染 handbook 源。 |
| `NEXAU_TOOL_CALL_MODE` | planner | 工具调用格式(`xml` / `structured`;默认取 yaml 的 `structured`)。 |
| `LLM_TEMPERATURE` / `LLM_MAX_TOKENS` / `LLM_MAX_CONTEXT` / `LLM_MAX_ITERATIONS` / `TOOL_OUTPUT_LIMIT` | planner | NexAU 调参旋钮(默认:`0.0` / yaml / `200000` / `300` / `300000`)。 |
| `LLM_EXTRA_BODY` | planner、resync | 合并进请求体的原始 JSON(经 SDK 的 `extra_body`)。 |
| `HANDBOOK_GEN_SCALE` | resync | `large` → 文件级引擎;`small` → 成员级引擎(默认)。 |
| `HANDBOOK_GEN_ROOT` | resync | resync 所驱动的 phase-2/3 生成器模块的显式路径。 |
| `PHASE2_FINAL` | resync | 成员级 resync 读取的 function 级 phase-2 mapping/skeleton 目录。 |
| `HANDBOOK_REFS` | resync | 覆盖成员级 resync 所编辑的 handbook `references/` 目录。 |
| `HANDBOOK_LARGE_SKILL` | resync | 覆盖文件级 resync 所编辑的 large 流程 skill 目录。 |
| `RESYNC_TRANSLATE` | resync | `0` / `off` 跳过卡片翻译这一 LLM 步骤(等同 `--no-translate`)。 |
| `RESYNC_NARRATE_LANG` | resync | 文件级(large)resync 的正文语言(`en` / `zh`;默认 `zh`)。 |

> **布局说明**:成员级 resync 默认从兄弟目录 `Harness_Translation/` 读取其原始 phase-2/3
> 产物(`PHASE2_FINAL`、`PRISTINE_HANDBOOK_JSON`、`UPSTREAM_CACHE`)。请设置 `PHASE2_FINAL`
> (必要时再设 `HANDBOOK_REFS`)指向你自己的布局;phase-3 的 JSON / 缓存路径只是可选富化,
> 缺失时会自动跳过。
