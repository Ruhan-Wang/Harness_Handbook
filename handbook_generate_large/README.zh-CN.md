# handbook_generate_large —— 以文件为叶子的 handbook 流水线

[English](README.md) | **中文**

把**大型**代码库自底向上转成一本可导航的 **handbook**(markdown + 可选 HTML),**以文件
为叶子节点**:读遍并描述每个文件,把文件归入一个有序的 stage 骨架,再从叶子一路向上叙述到
系统总览。覆盖率由构造保证 —— 不会静默漏掉任何文件,也无需手写骨架。

## 核心思路:自底向上,文件即叶子

1. **读遍每个文件** → 每文件一张卡片(purpose;deep 模式下还有详细描述 + 从调用图导出的
   函数清单及调用关系)。
2. **合成 stage 骨架**:从这些卡片归纳出一条有序的生命周期主线,并把每个文件分配到某个 stage。
3. **组织每个 stage 内部**(排序 + 把其文件再分子组)。
4. **自底向上叙述**:在叶子处渲染文件/函数细节,再让 LLM 从子节点摘要逐层归纳 子stage → stage
   → 系统;并抽取跨 stage 的状态寄存器。

stage 的*顺序*来自调用图(入口 → 调用者先于被调用者),所以骨架是一条叙事主线,而非盲目聚类。

## 流水线

```
Phase 1   run_phase1.py            源码 → phase1/graph.json                （无 LLM）
Phase 2a  phase2/read_files        读遍每个文件 → phase2/cards/            （每文件一卡）
Phase 2b  phase2/synth_stages      卡片 → phase2/skeleton.yaml + file_stage.json
Phase 2c  phase2/organize_stages   排序 + 分组每个 stage → stage_organization.yaml
Phase 3   phase3/build_handbook    自底向上叙述 → handbook/（md + 可选 html）
```

## 环境准备

```bash
pip install tree-sitter tree-sitter-language-pack pyyaml requests markdown pygments

# LLM:任意 OpenAI 兼容接口（Phase 2/3 需要;Phase 1 不需要）。
export OPENAI_API_KEY=sk-...                        # 必填（无 key 本地端点用 =EMPTY）
export OPENAI_MODEL=gpt-4o-mini                     # 可选（默认 gpt-4o-mini）
export OPENAI_BASE_URL=https://api.openai.com/v1    # 可选;或自托管 vLLM / 代理
```

`markdown` + `pygments`仅在渲染 HTML 时需要。`HANDBOOK_LLM_MODEL` / `HANDBOOK_LLM_BASE_URL` /
`HANDBOOK_LLM_API_KEY` 仍作为覆盖生效。

## 目录结构

```
run.py            端到端驱动（--phase all|1|2a|2b|2c|2|3|逗号列表）
run_phase1.py     Phase 1 独立运行（静态调用图）
run_phase3.py     Phase 3 独立运行（叙述;复用 Phase 2 产物）
ir.py  adapters/  语言适配器 → 语言无关 IR（rust/python/go/ts/…）
shared/           api_client（OpenAI 兼容 LLM）、skeleton_yaml、critic、progress
phase1/           build_graph.py
phase2/           read_files、synth_stages、synth_agent、skeleton_doctor_files、
                  file_assign、nav_pack、organize_stages、agent_tools/
phase3/           load_inputs、render_file、rollup、registers、render_html、build_handbook
```

## 各阶段详解

### 2a —— 读遍每个文件（`phase2/read_files.py`）
一趟 O(文件数) 的分批并行处理。`--read-detail deep` 会完整读取每个文件并写出 handbook 叶子
内容:详细 `description` + 每函数清单(qualname / 行号区间 / 签名 / 来自调用图的调用关系;LLM
负责写 `purpose` / `data_flow` / `relations`)。卡片增量写入(抗崩溃),`--resume` 跳过已完成的。

### 2b —— 合成 stage(`phase2/synth_stages.py`)
把每文件的 purpose 归纳到目录级,连同调用图入口一起交给 LLM,得到一份**有序**的 stage 骨架,
再把每个文件分配到 stage。`--synth-mode`:
- **`oneshot`**(默认):一次 LLM 调用起草骨架,然后分配一次。
- **`doctor`**:一次性起草 + 一个**actor-critic 收敛循环**(`skeleton_doctor_files`,复用
  `shared/critic.py`),不断拆分/合并/新增 stage 并重新分配,直到每个文件都落位。**无需
  NexAU / `LLM_*`。**
- **`agent`**:由一个 NexAU agent 起草骨架(需要 `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY`),
  之后走同样的收敛循环。若该端点不可用则回退到 oneshot。

### 2c —— 组织每个 stage(`phase2/organize_stages.py`)
对每个 stage:按调用图依赖排序其文件(调用者先于被调用者,Kahn 拓扑),并拆成 2–8 个有序子组。
约 O(stage 数) 次 LLM 调用。

### 3 —— 叙述(`phase3/build_handbook.py`)
后序遍历 stage 树:叶子处渲染文件/函数细节(无 LLM),非叶子节点由其子节点摘要经 LLM 归纳,
最后生成系统总览。同时抽取**状态寄存器**(跨 stage 的全局状态)与索引。输出 `handbook/`:
`overview.md`、`index.md`(每 stage 带总览)、`register.md`、`stages/<id>.md`,以及可选的
多页(`run.py` 用 `--phase3-html`;`run_phase3.py` 用 `--html`)或单页(`--html-single`)HTML 站点。

## 用法

```bash
# 全流程（英文）:deep 读取 → 合成 → 组织 → 叙述 + HTML
python3 run.py --source-root /path/to/repo --work-dir work/repo \
    --read-detail deep --read-batch-size 1 --read-workers 100 \
    --synth-mode doctor --doctor-workers 32 --doctor-llm-workers 100 \
    --organize-workers 100 --phase3-html

# 中文 handbook（用一个全新的 work-dir;zh 卡片需要重跑 2a）
python3 run.py --source-root /path/to/repo --work-dir work/repo_zh \
    --read-detail deep --read-batch-size 1 --narrate-lang zh --phase3-html

# 逐阶段运行
python3 run.py --source-root … --work-dir work/repo --phase 1
python3 run.py --source-root … --work-dir work/repo --phase 2a --read-detail deep
python3 run.py --source-root … --work-dir work/repo --phase 2b --synth-mode doctor
python3 run.py --source-root … --work-dir work/repo --phase 2c --organize-workers 100
python3 run_phase3.py --phase2-dir work/repo/phase2 --out work/repo/handbook \
    --lang zh --workers 100 --html
```

`--narrate-lang {en,zh}` 控制所有进入 handbook 的正文语言(文件/函数细节、stage/系统总览、
寄存器语义),贯穿 2a/2b/2c/3。`--lang` 与之无关 —— 它是 Phase 1 的源码语言提示(`auto`
会自动探测并合并源码根下所有受支持的语言)。

## 备注

- **不做函数级分类。** 本流水线只以文件为叶子;函数级路径(iterate / pass_a..d)在
  `handbook_generate_small` 里。
- LLM 接入是 `shared/api_client.py` 里的 OpenAI 兼容 `Api`,由 `OPENAI_API_KEY` /
  `OPENAI_MODEL` / `OPENAI_BASE_URL` 配置(只有 `agent` 起草骨架那步改用 NexAU,走 `LLM_*` 环境)。
- `work/` 存放每个项目的产物(graph、卡片、骨架、handbook),按需创建,不纳入提交。
