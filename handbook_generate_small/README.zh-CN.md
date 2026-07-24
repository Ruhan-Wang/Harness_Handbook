# handbook_generate_small —— 骨架驱动的 handbook 流水线

[English](README.md) | **中文** | [Русский](README.ru.md)

一个**项目无关**的三阶段流水线(静态图 → LLM 分类 → LLM 叙述),配统一的 `LanguageAdapter`
前端,可面向 **Python、Rust、TypeScript、Go**(以及轻量的 Starlark / Shell / PowerShell)。
适合规模足够小、可以用一份手写的 **stage 骨架**来描述、且你希望正文更贴合项目的代码库。

项目身份在运行时通过 `--project-name` / `--project-brief` / `--project-kind` 注入
(由 `project_context.py` 读取),因此没有任何硬编码 —— 你指向哪个代码库,就为哪个生成 handbook。

## 流水线

```
Phase 1   run_phase1.py   源码 → phase1/graph.json                     （无 LLM）
Phase 2   phase2/          LLM 分类（Critic-Actor 迭代）                 → stage 分配
Phase 3   phase3/          LLM 叙述（actor-critic-reflexion，stage 并行） → handbook
```

Phase 2/3 需要 LLM,**并且**需要一份用户手写的、描述 stage 生命周期的 `skeleton.yaml`。

## 目录结构

```
handbook_generate_small/
├── project_context.py        # 注入每个 LLM prompt 的项目身份
├── ir.py                     # 语言无关 IR（FunctionNode/BoundaryNode/CallEdge）
├── adapters/                 # LanguageAdapter 抽象基类 + 各语言前端
├── phase1/build_graph.py     # 语言无关的图组装 + 输出器
├── run_phase1.py             # Phase 1 CLI
├── phase2/                   # LLM 分类（Critic-Actor）;api_client 在此
├── phase3/                   # LLM 叙述（actor-critic-reflexion）,stage 并行
└── run.py                    # 端到端驱动（phase1 → phase2 → phase3）
```

## 环境准备

```bash
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments

# LLM:任意 OpenAI 兼容接口（Phase 2/3 需要;Phase 1 不需要）。
export OPENAI_API_KEY=sk-...                        # 必填（无 key 本地端点用 =EMPTY）
export OPENAI_MODEL=gpt-4o-mini                     # 可选（默认 gpt-4o-mini）
export OPENAI_BASE_URL=https://api.openai.com/v1    # 可选;或自托管 vLLM / 代理
```

`markdown` + `pygments` 仅在渲染 HTML 时需要。客户端在 `phase2/api_client.py`;
`HANDBOOK_LLM_MODEL` / `HANDBOOK_LLM_BASE_URL` / `HANDBOOK_LLM_API_KEY` 仍作为覆盖生效。

## 用法

端到端。用 `--project-*` 把项目描述一次,让 prompt 更贴合它:

```bash
python3 run.py \
    --lang rust \
    --source-root /path/to/repo \
    --skeleton skeletons/repo.yaml \
    --work-dir work/repo \
    --title "Repo Handbook" \
    --project-name "Repo" \
    --project-kind "coding agent" \
    --project-brief "A terminal coding agent that edits code and runs commands." \
    --out-lang en \
    --max-stage-workers 4
```

`--project-brief-file path.md` 可从文件读取简介。若省略 `--project-name`,则回退到 `--title`。

只要调用图(无 LLM,任意语言):

```bash
python3 run.py --lang rust --source-root /path/to/repo --work-dir work/repo --phase 1
# 或直接:
python3 run_phase1.py --lang go --source-root /path/to/repo --out out/repo
```

`--phase` 接受 `all | 1 | 2 | 3 | 1-2 | 2-3`。`--out-lang {zh,en}` 设定 handbook 语言
(默认 `zh`)。可用 `--files a.py,b.py` 把 Phase 1 限定到指定文件(否则自动发现 `--source-root`
下所选语言的全部文件)。

**输出** → `work/repo/phase3/output/`(markdown handbook + JSON)。

## 语言支持

| 语言 | 解析器 | 节点（函数/方法/签名/async/类） | 调用边 | self 属性类型 |
|---|---|---|---|---|
| Python | 标准库 `ast` | 精确 | 完整（所有 `call_type`） | 来自 `__init__` 赋值 + 注解 |
| Rust | tree-sitter | 完整 | self / self-field / param / `Type::` / free / macro | 来自 struct 字段类型 |
| TypeScript | tree-sitter | 完整（类方法、函数、箭头函数） | this / this-field / param / free / import | 来自类字段 + 构造器参数 |
| Go | tree-sitter | 完整（函数、带 receiver 的方法） | receiver / receiver-field / param / free / pkg | 来自 struct 字段类型 |
| Starlark | tree-sitter | 函数（无类） | 调用名 → internal/boundary | 不适用 |
| Shell (bash) | tree-sitter | 函数（无类） | 命令名 → internal/boundary | 不适用 |
| PowerShell | tree-sitter | 函数（无类） | 命令名 → internal/boundary | 不适用 |

它们都输出**同一套 `graph.json` schema**,因此 Phase 2/3 无需改动即可消费任意一种。
Starlark / Shell / PowerShell 用轻量的自由函数模型(调用图语义较弱 —— 多数命令是外部的),
所以混合仓库**不会漏掉文件**。

### 混合语言仓库:`--lang auto`

`--lang auto` 会发现源码根下所有受支持的语言并合并成一个 `graph.json`。各语言内部的调用图
是完整的;**跨语言的调用边会在边界处断开**(如 Rust 启动一个 Python 脚本),落入
`dropped_calls.json`,与其它无法解析的调用一样。任何函数都不会丢失。

```bash
python3 run.py --lang auto --source-root /path/to/repo \
    --skeleton skeletons/repo.yaml --work-dir work/repo --title "Repo Handbook"
```

### 已知简化（非 Python）

- 调用解析是尽力而为的静态分析(无完整类型推断);无法定位到名字的落入 `dropped_calls.json`
  标为 `unresolved`。
- `boundary` 的 qualname 拆分用 `.` 分段(为 Python 点分路径调优);Rust 的 `::` 边界节点仍能
  解析,但其模块/类元数据拆分是近似的。不影响 Phase 2/3 —— 它们按 qualname + 文件 + 行号区间为键。

## 项目上下文(让 prompt 通用)

运行时 `run.py` 注入三个环境变量(由 `project_context.py` 读取),被每个 Phase 2 / Phase 3
prompt 使用:

| 环境变量（由 `run.py` 设置） | CLI 参数 | 含义 |
|---|---|---|
| `HANDBOOK_PROJECT_NAME` | `--project-name`（回退到 `--title`） | 显示名,如 "Redis" |
| `HANDBOOK_PROJECT_BRIEF` | `--project-brief` / `--project-brief-file` | 1–3 句描述 |
| `HANDBOOK_PROJECT_KIND` | `--project-kind` | 名词,如 "web service"、"compiler" |

可选的子系统富化(默认为空,需要就直接在环境里设):`HANDBOOK_SUBSYS_FILE_MAP`
(JSON `{"file.py": "subsys-x"}`)与 `HANDBOOK_SUBSYS_BOUNDARY_MAP`(JSON `{"module.path": "subsys-x"}`)。

## 并发

- **Phase 2 · Pass A** 已经用线程池并行分类函数。
- **Phase 3** 并行生成各 stage(`--max-stage-workers`,默认 4)。stage 内部的每函数 Tier 3
  单元保持串行,以便各自能交叉引用已写好的同级内容。设 `--max-stage-workers 1` 可完全串行。

## 新增一门语言

1. 用 `pip` 安装,或依赖 `tree-sitter-language-pack` 提供文法。
2. 新增 `adapters/<lang>_adapter.py`,实现 `LanguageAdapter.analyze()`(返回 `ModuleAnalysis`),
   可选实现 `statement_spans()`。使用 `base.py` 里的 `TSNode` 包装器 + `parse_tree()`。
3. 在文件底部 `register("<lang>", <Adapter>, (".ext",))`;`base._autoregister` 会自动识别。
