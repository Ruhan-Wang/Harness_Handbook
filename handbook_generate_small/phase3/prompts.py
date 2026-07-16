# -*- coding: utf-8 -*-
"""All Phase 3 generation prompts (Tier 1 / Tier 2 / register appendix), bilingual.

These prompts are **codebase-agnostic**. The project being documented is injected
at run time via `{project_name}` / `{project_block}` placeholders (populated from
`project_context.get_project_context()` in `tier_actors.py`), so the same prompts
work for any repository — an agent harness, a web service, a compiler, etc.

Each variant's prompt is independent — translating one to the other would lose
the register cues (Chinese "短句直接 / 不要 throat-clear" maps to English "active
voice / avoid nominalization" — they need different examples). Tier 3's prompt
lives in translate_member.py (it owns the structured schema).

Bump _NARRATIVE_PROMPT_VERSION when a prompt changes so cached narratives
auto-invalidate.
"""
from __future__ import annotations

# v5-generic: prompts genericized from the Terminus-specific originals — project
#   identity is now injected via {project_name}/{project_block}, and all
#   domain-specific examples (tmux, trajectory, fixed 6-stage lifecycle) removed.
_NARRATIVE_PROMPT_VERSION = "v5-generic"


_TIER1_PROMPT_ZH = """{project_block}

你是 {project_name} 项目的资深工程师。

你的任务不是解释代码，而是在给一个**刚入职、第一次接触这个项目的同事做 3 分钟白板介绍**，帮助他快速建立心智模型（mental model）。

读完这一节后，读者应该能够回答：

1. 这个项目是什么？
2. 它在解决什么问题？
3. 它整体是怎么工作的？
4. 后面章节大概会展开哪些部分？

如果读者看完仍然需要先去读代码才能理解整体，那这一节就是失败的。

---

# 写作原则（重要）

## 像工程师讲给同事听

不要写成：

- 技术文档
- 学术论文
- 系统设计报告

而是像在会议室白板前，用大白话把系统的骨架画出来。

---

## 用短句、用动词

优先用具体动词（读、写、算、发、收、判断、循环……）。

避免空泛名词化（执行、实施、构造、合成、完成初始化流程、进入下一环节……）。

---

## 第一次出现术语必须解释

任何项目内部的专有名词、缩写、组件名，第一次出现时都要用括号给一句白话解释。

不要假设读者知道任何这个项目的内部概念。

---

## 可以类比

允许使用准确类比，前提是帮助理解，而不是增加花哨表达。

---

## 只讲整体，不讲细节

这一层只回答：是什么、做什么、整体什么形状。

不要展开单个函数实现、内部状态字段、边界条件等细节——这些属于后续章节。

---

# 输入

## stages（主流程顺序）

{stages_brief}

## side / crosscut / subsystem

{side_brief}

---

# 输出格式（Markdown 直出，不要 H1）

按顺序输出以下 3 个部分。

---

## 1. 一段系统总览（150-200 字）

第一句话必须直接回答「这个项目是什么」，不要任何铺垫。

例如应该类似：

> {project_name} 是一个 <一句话说清它是什么、给谁用、解决什么问题>。

而不是「{project_name} 是一个复杂的框架……为了实现……」这种空话。

重点建立一个简单心智模型，让读者理解：

- 核心组件有哪些
- 数据/控制大致怎么流动
- 系统整体是什么形状（一次性流水线？循环？请求-响应？）
- 什么时候开始、什么时候结束

---

## 2. 两张 ASCII 小图

必须使用两个独立的 ```text 代码块。不要画一张大图。

---

### 图 A · 顶层结构 / 生命周期

回答：整个系统从入口到结束，大致经历哪几个阶段？

要求：

- 以输入的顶层 stage 为骨架，不要臆造
- 突出最核心的那个阶段（主流程 / 主循环 / 主入口）
- 不出现内部实现细节
- 约 5 行

---

### 图 B · 主流程走一遍

回答：系统跑一次核心流程时，按顺序会做哪几步？

要求：

- 6~8 个步骤
- 1~2 个 yes/no 判断
- 只画 happy path，不画错误恢复
- 用动作描述，不出现内部对象名
- 如果系统是循环型，就画一次迭代；否则画一条端到端主路径
- 长度约 12~15 行

---

## 3. 顶层 Stage 定位

列出输入里**全部**顶层 Stage（数量以输入为准，不要凑数也不要漏）。

每个 Stage：

- 一句话，≤ 30 字
- 说明：干什么 + 在整体流程中的位置

不要讲实现细节。

---

# 自检（生成前检查）

1. 第一段第一句话是否直接回答「这个项目是什么」？
2. 有没有出现「首先」「随后」「最后」「至此完成」等废话？
3. 一个从未看过代码的人能否在 2 分钟内理解系统形状？
4. 第一次出现的术语是否带括号解释？
5. 是否避免了单函数实现、内部状态字段等细节？
6. ASCII 图是否只展示概念层流程，而非实现层结构？
7. 整体是否更像白板讲解，而不是设计文档？

如果有任何一项不满足，重写。
"""


_TIER1_PROMPT_EN = """{project_block}

You are a senior engineer on the {project_name} project.

Your job is NOT to explain the code.

Your job is to give a 2–3 minute whiteboard tour to a new teammate who has never seen {project_name} before and knows nothing about it. The goal is to help them build a mental model.

After reading this page, they should be able to answer:

1. What is {project_name}?
2. What problem does it solve?
3. What is its overall shape?
4. What parts will later chapters dive into?

If they still need to read the code to understand the big picture, this overview has failed.

---

# Writing Principles (Most Important)

## Talk Like An Engineer Explaining It To A Colleague

Do NOT write like a design document, a research paper, an architecture spec, or generated documentation. Write like someone sketching boxes on a whiteboard.

---

## Use Short Sentences And Concrete Verbs

Prefer concrete verbs (read, write, compute, send, receive, check, loop, ...).

Avoid vague nominalizations (perform, execute, construct, facilitate, orchestrate, initialize the workflow, proceed to the next phase, ...).

---

## Define Every Term The First Time It Appears

Any project-specific name, abbreviation, or component gets a one-line plain-English gloss on first use. Assume the reader has zero context.

---

## Analogies Are Welcome

Use them when they genuinely improve understanding. Accuracy matters more than cleverness.

---

## Explain The Shape, Not The Details

This overview answers only: What is it? What does it do? What is the overall shape?

Do NOT explain individual function internals, internal state fields, or edge cases — those belong in later chapters.

---

# Input

## stages (in skeleton order)

{stages_brief}

## side / crosscut / subsystems

{side_brief}

---

# Output (Markdown directly, no H1)

Produce the following three sections in this exact order.

---

## 1. System Overview Paragraph (~120–180 words)

The FIRST sentence must directly answer: what is {project_name}? No setup.

Good:

> {project_name} is a <one line: what it is, who it's for, what problem it solves>.

Bad:

> {project_name} is a sophisticated framework... Before discussing the architecture...

Build a simple mental model so the reader understands:

- the core components
- roughly how data / control flows
- the overall shape (one-shot pipeline? loop? request-response?)
- when it starts and when it ends

---

## 2. Two Small ASCII Diagrams

Use two separate ```text fenced blocks. Do NOT draw one giant diagram.

---

### Diagram A · Top-Level Structure / Lifecycle

Answer: what phases does the system move through from entry to finish?

Requirements:

- use the top-level stages from the input as the skeleton; do not invent
- highlight the single most central phase (the main flow / main loop / entry point)
- no internals
- roughly 5 lines

---

### Diagram B · The Main Flow, Once Through

Answer: when the system runs its core flow once, what happens step by step?

Requirements:

- 6–8 steps
- 1–2 yes/no decisions
- happy path only, no recovery flows
- action-oriented wording, no internal object names
- if the system is a loop, draw one iteration; otherwise draw one end-to-end path
- roughly 12–15 lines

---

## 3. Top-Level Stages

List ALL top-level stages from the input (however many there are — don't pad, don't drop any).

For each stage:

- one bullet, ≤ 22 words
- explain: what it does + where it sits in the overall flow

Do not include implementation details.

---

# Self-Check Before Emitting

1. Does the first sentence immediately answer what {project_name} is?
2. Did I remove all throat-clearing?
3. Could someone with zero code knowledge understand the system in under 2 minutes?
4. Did I define every piece of jargon on first use?
5. Did I avoid function-internals and internal state fields?
6. Do the diagrams show conceptual flow rather than implementation structure?
7. Does this read like a whiteboard explanation rather than generated documentation?

If any answer is "no", rewrite before emitting.
"""


_TIER2_PROMPT_ZH = """{project_block}

你是 {project_name} 项目的资深工程师。

读者已经看过 Tier 1，已经知道：这个项目是什么、整体怎么流动、各个 Stage 的大致位置。

现在他们想回答一个新的问题：

> 这个 Stage 为什么存在？

你的任务不是解释代码，而是帮助读者理解：

- 这个 Stage 负责什么
- 为什么需要它
- 它和前后 Stage 怎么配合
- 它对系统整体有什么贡献

如果读者读完后只能复述函数名，却不知道这个 Stage 的职责，那这一节就是失败的。

---

# 写作原则

## 第一段先回答「为什么有这个 Stage」

不要先讲代码，不要先罗列成员。先回答：这个 Stage 在解决什么问题？

读者应该先理解目的，再理解实现。

---

## 像白板讲解

假设你正在会议室画框图。不要像 API Reference / Design Doc / 逐行代码讲解。

---

## 多讲 Why，少讲 How

优先解释：为什么存在、为什么这么设计、如果删掉会怎样、为什么放在这里。其次再解释怎么实现。

---

## 用短句、用动词

优先具体动词；避免空泛名词化。

---

## 第一次出现术语要解释

项目内部专有名词第一次出现时用括号给一句白话解释。假设读者不知道这些概念。

---

## 允许简化

不重要的旁支可以写「（这里还会处理 X 和 Y，略）」。不要为了完整性破坏可读性。

---

# 输入

## Stage

id: {stage_id}

title: {stage_title}

description: {stage_description}

## Stage Members

{members_brief}

## State Registers

{stage_registers}

## Adjacent Stages

{adjacent_brief}

---

# 输出格式（Markdown 直出，不要 H2）

严格按顺序输出。

---

## (a) 开场解释（70-150 字）

第一句话直接回答：这个 Stage 在解决什么问题？不要 throat-clear。

这一段回答：为什么存在、整体职责、在流水线中的位置。

---

## (b) 主流程

用短 prose 或编号列表。关键函数首次出现时写 `function_name()`（一句白话解释），重点是为什么调用、产出什么。不要变成代码 walkthrough。

---

## (c) 📊 状态流动

必须使用以下固定格式：

**📊 状态流动**

- 写: `<register-id>` — 什么时候写，为什么写
- 读: `<register-id>` — 怎么使用
- 清: `<register-id>` — 为什么清
- 触发下游: `<stage-name>` — 在什么条件下进入

要求：register-id 必须来自输入；不允许编造；不允许遗漏核心 register。如果本 stage 与任何 register 都无交互，写一行「（本 stage 无显式 register 交互）」。

---

## (d) 与前后 Stage 的衔接

1-2 句即可。回答：上游给了什么、本 Stage 产出了什么、下游怎么消费。帮助读者建立流水线视角。

---

# 自检

1. 第一段第一句话是否直接回答「为什么有这个 Stage」？
2. 是否先讲职责，再讲实现？
3. 是否解释了为什么这么设计？
4. 是否避免变成函数逐行讲解？
5. 📊 状态流动中的 register 是否全部真实存在（来自输入）？
6. 新术语第一次出现时是否带括号解释？
7. 读者是否能回答：「这个 Stage 为什么存在？」

如果不能，重写。
"""


_TIER2_PROMPT_EN = """{project_block}

You are a senior engineer on the {project_name} project.

The reader has already finished Tier 1. They know what {project_name} is, how it flows at a high level, and roughly where each stage sits.

Now they want to answer a new question:

> Why does this stage exist?

Your job is NOT to explain the code. Your job is to help the reader understand:

- what responsibility this stage owns
- why the system needs it
- how it fits into the surrounding pipeline
- what it contributes to the overall system

If the reader finishes this page knowing function names but still cannot explain why the stage exists, this chapter has failed.

---

# Writing Principles

## Start With The Problem This Stage Solves

Do NOT start with code or a member list. Start with: what problem is this stage solving? The reader should understand the purpose before the implementation.

---

## Explain Like You're At A Whiteboard

Imagine you're sketching boxes for a teammate. Do NOT write like an API reference, a design spec, or a line-by-line code walkthrough.

---

## Explain Why Before How

Prioritize: why it exists, why it's placed here, why it's designed this way, what would happen if it were removed. Only then explain how it works.

---

## Use Short Sentences And Concrete Verbs

Prefer concrete verbs; avoid nominalizations (perform, execute, construct, facilitate, orchestrate, ...).

---

## Define Terms On First Use

Project-specific names get a one-line gloss on first use. Assume zero prior knowledge.

---

## Simplify Aggressively

Minor branches can be summarized as "(a few edge cases handled here, omitted)". Completeness is less important than clarity.

---

# Input

## Stage

id: {stage_id}

title: {stage_title}

description: {stage_description}

## Stage Members

{members_brief}

## State Registers

{stage_registers}

## Adjacent Stages

{adjacent_brief}

---

# Output (Markdown directly, no H2)

Produce the following sections in this exact order.

---

## (a) Opening Explanation (70–150 words)

The FIRST sentence must directly answer: what problem does this stage solve? No warm-up.

This section should explain: why the stage exists, its responsibility, where it sits in the pipeline.

---

## (b) Main Flow

Use short prose or a numbered list. When mentioning a function for the first time, write `function_name()` (plain-English gloss). Focus on why it is called and what it produces. Do not turn this into a line-by-line walkthrough.

---

## (c) 📊 State Flow

Use this exact format:

**📊 State Flow**

- writes: `<register-id>` — when it is written and why
- reads: `<register-id>` — how it is used
- clears: `<register-id>` — why it is cleared
- triggers downstream: `<stage-name>` — condition for transition

Requirements: register IDs must come from the provided input; do not invent registers; do not omit important ones. If this stage interacts with no register, write a single line "(this stage has no explicit register interactions)".

---

## (d) Pipeline Hand-Off

1–2 sentences. Answer: what comes from upstream, what this stage produces, how downstream stages consume it. Help the reader see the pipeline rather than isolated code.

---

# Self-Check Before Emitting

1. Does the first sentence directly answer why this stage exists?
2. Did I explain responsibility before implementation?
3. Did I explain why the design exists?
4. Did I avoid turning this into a code walkthrough?
5. Are all register IDs in State Flow real (from the input)?
6. Did I define jargon on first use?
7. Could the reader answer: "Why does this stage exist?"

If not, rewrite.
"""

_REGISTER_APPENDIX_PROMPT_ZH = """{project_block}

你是 {project_name} 项目的资深工程师。

读者已经读完 Tier 1 和所有 Stage 章节。他们现在想换一个视角看系统：不再按 Stage 看，而是按 Register（状态寄存器）看。

你的任务不是列字段，而是把每个 register 讲成一条「状态传话线」：

- 谁写它
- 谁读它
- 什么时候清它
- 它把哪一段流程的信息传给哪一段
- 为什么系统需要这条状态线

如果读者读完后只知道 register 的名字，却不知道它在系统里帮谁传话，这个 appendix 就失败了。

---

# 写作原则

## 每个 Register 是一张独立卡片

每张卡只讲一个 register，但不能孤立讲——必须把它联到具体的 Stage、函数、读写场景、上下游用途。

## 先讲用途，再讲生命周期

不要先抄字段、不要直接复述 semantics。先回答：这个 register 解决什么问题？

## 讲清楚「状态怎么流」

每个 register 都要讲：默认值、哪里写、哪里读、哪里清、是否跨迭代/跨阶段、下游如何使用。读者应该能画出这条线。

## 短句、直接、少名词化

## 允许承认不跨迭代/不跨阶段

如果它只在单次流程内生效，就明确写「单次流程内」，不要强行解释成跨迭代状态。

---

# 输入

## 全部 State Registers

{registers_full}

## 全部 Stage / Side / Crosscut / Subsystem 一句话定位

{all_stages_brief}

---

# 输出格式（Markdown 直出）

按输入顺序，为每个 register 输出一张卡。只用 H3 标题，不要 H1 / H2。卡之间空一行即可，不要 `---`。

每张卡严格使用下面模板：

### 🔄 `<register-id>`

**用途**: <1 句话。回答「这个 register 解决什么问题」。不要直接抄 semantics，要重新解释。>

**生命周期**:
- **默认值**: <默认值；如果输入没写清楚，写「输入未说明」>
- **重置**: <在哪个 stage / 函数被重置；若无显式 reset，写「无显式 reset」>
- **写**:
  - `<stage-id>` / `<function-name>` — <什么时候写，为什么写>
- **读**:
  - `<stage-id>` / `<function-name>` — <什么时候读，怎么用>
- **清 / 回填**: <如有则写，否则「无」>

**跨迭代/跨阶段传话**: <如果跨迭代/跨阶段生效，写成「第 N 段 → 第 N+1 段：传什么、谁读」。否则写「单次流程内」。>

**为什么这么设计**: <1-2 句。解释设计动机，不要只描述现象。>

---

# 强约束

- register 数量必须等于输入数量。
- register-id 必须来自输入。
- Stage / 函数名必须来自输入；不确定就写「输入未说明」，不要编。
- 每张卡 80-200 字。
- 不要输出总览段，不要 H1 / H2，不要用 `---` 分隔卡片。
- 不要只解释字段含义，要解释它在系统里怎么传话。

---

# 自检

1. 是否每个 register 都有一张卡？
2. register-id 是否都来自输入？
3. 是否讲清楚谁写、谁读、谁清？
4. 是否说明它是跨迭代/跨阶段还是单次流程内？
5. 是否解释了为什么需要这个 register？
6. 是否避免直接复述 semantics？
7. 读者是否能回答：「这个 register 在帮哪些 stage 传什么话？」

如果不能，重写。
"""

_REGISTER_APPENDIX_PROMPT_EN = """{project_block}

You are a senior engineer on the {project_name} project.

The reader has already finished Tier 1 (system overview) and all stage chapters. They now want to look at the system from a different angle: not stage-by-stage, but register-by-register (state registers).

Your job is NOT to list fields. Your job is to explain each register as a state handoff mechanism. For every register, the reader should understand:

- who writes it
- who reads it
- when it gets reset
- whether it survives across iterations / stages
- what information it carries
- why the system needs that information channel

If the reader finishes this appendix knowing register names but still cannot explain how information flows through the system, this appendix has failed.

---

# Writing Principles

## Treat Every Register As A Communication Line

Each register is a standalone card, but never describe it in isolation. Always connect it to stages, functions, producers, consumers, and transitions.

## Start With The Problem It Solves

Do NOT start by paraphrasing semantics. First answer: what problem does this register solve? Then explain the mechanism.

## Explain State Movement

Every register should answer: where does its value come from, where does it go, who depends on it, when is it cleared, does it survive into the next iteration/stage. The reader should be able to draw the state flow.

## Use Short Sentences And Active Voice

Avoid nominalizations (perform, execute, facilitate, synchronize state, ...).

## Be Honest About Scope

If a register only lives within a single run of the flow, write "single-pass". Do not force an artificial cross-iteration story. If the input doesn't specify reset/write/read sites, write "input does not specify" rather than inventing behavior.

---

# Input

## All State Registers

{registers_full}

## All Stage / Side / Crosscut / Subsystem Positioning

{all_stages_brief}

---

# Output (Markdown directly)

For every register, emit one card. Use H3 headings only — no H1 or H2. Separate cards with a single blank line. No horizontal rules.

Each card must follow this template exactly:

### 🔄 `<register-id>`

**Purpose**: <one sentence answering "what problem does this register solve?" Do not paraphrase the semantics field. Re-express it in terms of system behavior.>

**Lifecycle**:
- **Default Value**: <default value if known; otherwise "input does not specify">
- **Reset**: <stage/function that resets it; otherwise "no explicit reset">
- **Write**:
  - `<stage-id>` / `<function-name>` — when and why it is written
- **Read**:
  - `<stage-id>` / `<function-name>` — how the value is used
- **Clear / Refill**: <if applicable; otherwise "none">

**Cross-Iteration / Cross-Stage Behavior**:
<either "segment N → segment N+1 handoff: what is passed, who reads it" or "single-pass">

**Why This Design**:
<1–2 sentences explaining the design motivation rather than the mechanics.>

---

# Hard Constraints

- Number of cards must equal number of registers.
- Every register-id must come from the input.
- Stage names and function names must come from the input; if uncertain, write "input does not specify".
- 80–180 words per card.
- No overview section. No H1. No H2. No horizontal rules. One register per card.

---

# Self-Check

1. Does every register have exactly one card?
2. Does every card explain who writes and who reads it?
3. Does every card explain whether it crosses iterations/stages?
4. Did I explain the problem it solves rather than restating semantics?
5. Are all stage names and function names grounded in the input?
6. Could a reader answer: "Which stages use this register to pass information?"

If not, rewrite.
"""


_PROMPTS_BY_LANG = {
    "zh": {
        "tier1": _TIER1_PROMPT_ZH,
        "tier2": _TIER2_PROMPT_ZH,
        "register_appendix": _REGISTER_APPENDIX_PROMPT_ZH,
    },
    "en": {
        "tier1": _TIER1_PROMPT_EN,
        "tier2": _TIER2_PROMPT_EN,
        "register_appendix": _REGISTER_APPENDIX_PROMPT_EN,
    },
}
