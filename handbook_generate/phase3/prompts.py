# -*- coding: utf-8 -*-
"""All Phase 3 generation prompts (Tier 1 / Tier 2 / register appendix), bilingual.

Each variant's prompt is independent — translating one to the other would lose
the register cues (Chinese "短句直接 / 不要 throat-clear" maps to English "active
voice / avoid nominalization" — they need different examples). Tier 3's prompt
lives in translate_member.py (it owns the structured schema).

Bump _NARRATIVE_PROMPT_VERSION when a prompt changes so cached narratives
auto-invalidate.
"""
from __future__ import annotations

# v3-tier1-split: Tier 1 diagram split from 1 big to 3 small.
# v4-tier1-novice: Tier 1 scoped to purpose/what/shape for a complete novice —
#   dropped Diagram C (cross-iteration state machines) and the registers section.
_NARRATIVE_PROMPT_VERSION = "v4-tier1-novice"


_TIER1_PROMPT_ZH = """你是 Terminus 2 项目的资深工程师。

你的任务不是解释代码，而是在给一个**刚入职、第一次接触 Terminus 2 的同事做 3 分钟白板介绍**，帮助他快速建立心智模型（mental model）。

读完这一节后，读者应该能够回答：

1. Terminus 2 是什么？
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

而是像：

> 「你可以把它理解成一个会操作终端的 AI。」
>
> 「它会先看看终端发生了什么，然后决定下一步敲什么命令。」
>
> 「整个系统其实就是不断重复这个过程。」

---

## 用短句

优先：

- 看
- 想
- 敲
- 读
- 写
- 等
- 记

避免：

- 执行
- 实施
- 构造
- 合成
- 完成初始化流程
- 开启后续阶段
- 进入下一环节

---

## 第一次出现术语必须解释

例如：

- tmux session（可以远程发命令的终端）
- Chat（调用 LLM 的薄包装）
- trajectory（Agent 的行动记录）

不要假设读者知道任何 Terminus 2 内部概念。

---

## 可以类比

允许使用准确类比，例如：

- 像远程操控一台电脑
- 像电话会议里的记录员
- 像快递的回执单

前提是帮助理解，而不是增加花哨表达。

---

## 只讲整体，不讲细节

这一层只回答：

- 是什么
- 做什么
- 整体什么形状

不要展开：

- register
- state machine
- 跨迭代状态
- checkpoint
- scheduler
- token budgeting
- prompt 构造细节

这些内容属于后续章节。

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

第一句话必须直接回答：

> Terminus 2 是什么？

不要任何铺垫。

例如应该类似：

> Terminus 2 是一个让大语言模型直接操作终端完成任务的 Agent。

而不是：

> Terminus 2 是一个复杂的智能体框架……
>
> 为了实现自动化……

重点建立一个简单心智模型：

> 看终端 → 想下一步 → 敲命令 → 再看结果

让读者理解：

- 谁在思考（LLM）
- 谁在执行（终端）
- 整体如何循环
- 什么时候结束

---

## 2. 两张 ASCII 小图

必须使用两个独立的 ```text 代码块。

不要画一张大图。

---

### 图 A · 生命周期

回答：

> 整个 Agent 从启动到结束经历什么？

形式：

init → setup → run → teardown

要求：

- 主循环 run 作为中心方块
- 不出现任何内部实现
- 不出现 register
- 约 5 行

---

### 图 B · 一次迭代做什么

回答：

> Agent 在 run 里面每转一圈会干什么？

要求：

- 6~8 个步骤
- 1~2 个 yes/no 判断
- happy path 为主
- 不画错误恢复
- 不出现 register 名
- 不出现内部对象名

使用动作描述：

- 看终端
- 收集信息
- 问 LLM
- 判断完成了吗
- 敲命令
- 等结果
- 记一步

长度约 12~15 行。

---

## 3. Top-Level Stages 定位

列出 6 个主 Stage。

每个 Stage：

- 一句话
- ≤ 30 字
- 说明：
  - 干什么
  - 在整体流程中的位置

例如：

- Setup：准备终端和运行环境。
- Observe：收集当前状态给 LLM。
- Act：执行 LLM 决定的动作。

不要讲实现细节。

---

# 自检（生成前检查）

1. 第一段第一句话是否直接回答「Terminus 2 是什么」？
2. 有没有出现「首先」「随后」「最后」「至此完成」等废话？
3. 一个从未看过代码的人能否在 2 分钟内理解系统形状？
4. 第一次出现的术语是否带括号解释？
5. 是否避免了 register、状态机、跨迭代细节？
6. ASCII 图是否只展示概念层流程，而非实现层结构？
7. 整体是否更像白板讲解，而不是设计文档？

如果有任何一项不满足，重写.
"""


_TIER1_PROMPT_EN = """You are a senior Terminus 2 engineer.

Your job is NOT to explain the code.

Your job is to give a 2–3 minute whiteboard tour to a new teammate who has never seen Terminus 2 before and knows nothing about it.

The goal is to help them build a mental model.

After reading this page, they should be able to answer:

1. What is Terminus 2?
2. What problem does it solve?
3. What is its overall shape?
4. What parts will later chapters dive into?

If they still need to read the code to understand the big picture, this overview has failed.

---

# Writing Principles (Most Important)

## Talk Like An Engineer Explaining It To A Colleague

Do NOT write like:

- a design document
- a research paper
- an architecture specification
- generated documentation

Write like:

> "You can think of it as an AI that operates a terminal."
>
> "It looks at what's on the screen, decides what to do next, and types commands."
>
> "Most of the system is just repeating that loop."

The reader should feel like someone is sketching boxes on a whiteboard.

---

## Use Short Sentences

Prefer:

- look
- think
- type
- read
- write
- wait
- record
- ask

Avoid:

- perform
- execute
- construct
- facilitate
- orchestrate
- initialize the workflow
- proceed to the next phase
- enter the subsequent stage

---

## Define Every Term The First Time It Appears

Examples:

- tmux session (a terminal you can drive remotely)
- Chat (a thin wrapper around an LLM call)
- trajectory (the agent's action history)

Assume the reader has zero context.

Never assume they know Terminus 2 concepts.

---

## Analogies Are Welcome

Use them when they genuinely improve understanding.

Examples:

- like remotely operating a computer
- like a note taker in a meeting
- like a delivery receipt

Accuracy matters more than cleverness.

---

## Explain The Shape, Not The Details

This overview answers only:

- What is it?
- What does it do?
- What is the overall shape?

Do NOT explain:

- registers
- state machines
- cross-iteration state
- checkpoints
- schedulers
- token budgeting
- prompt construction details

Those belong in later chapters.

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

The FIRST sentence must directly answer:

> What does Terminus 2 do?

No setup.

Good:

> Terminus 2 is an agent that lets an LLM operate a terminal to complete tasks.

Bad:

> Terminus 2 is a sophisticated agent framework...
>
> Before discussing the architecture...

Build a simple mental model:

> look at terminal → think → type command → observe result

Make sure the reader understands:

- who thinks (the LLM)
- who executes (the terminal)
- how the loop works
- when the loop ends

---

## 2. Two Small ASCII Diagrams

Use two separate ```text fenced blocks.

Do NOT draw one giant diagram.

---

### Diagram A · Lifecycle

Answer:

> What stages does the agent go through from start to finish?

Form:

__init__ → setup → run → teardown

Requirements:

- highlight run as the centerpiece
- no internals
- no register names
- roughly 5 lines

---

### Diagram B · One Iteration

Answer:

> What happens during a single loop iteration?

Requirements:

- 6–8 numbered steps
- 1–2 yes/no decisions
- happy path only
- no recovery flows
- no register names
- no internal object names

Use action-oriented wording:

- read terminal
- gather context
- ask the LLM
- check if done
- run command
- wait for output
- record a step

Length: roughly 12–15 lines.

---

## 3. Top-Level Stages

List the six top-level stages.

For each stage:

- one bullet
- ≤ 22 words
- explain:
  - what it does
  - where it sits in the overall loop

Example:

- Setup: prepares the terminal and runtime before the loop starts.
- Observe: gathers information before asking the LLM.
- Act: executes the action chosen by the LLM.

Do not include implementation details.

---

# Self-Check Before Emitting

1. Does the first sentence immediately answer what Terminus 2 does?
2. Did I remove all throat-clearing?
3. Could someone with zero code knowledge understand the system in under 2 minutes?
4. Did I define every piece of jargon on first use?
5. Did I avoid registers, state machines, and cross-iteration details?
6. Do the diagrams show conceptual flow rather than implementation structure?
7. Does this read like a whiteboard explanation rather than generated documentation?

If any answer is "no", rewrite before emitting.
"""


_TIER2_PROMPT_ZH = """你是 Terminus 2 项目的资深工程师。

读者已经看过 Tier 1。

他们已经知道：

- Terminus 2 是什么
- Agent 整体怎么循环
- 各个 Stage 的大致位置

现在他们想回答一个新的问题：

> 这个 Stage 为什么存在？

你的任务不是解释代码。

你的任务是帮助读者理解：

- 这个 Stage 负责什么
- 为什么需要它
- 它和前后 Stage 怎么配合
- 它对系统整体有什么贡献

如果读者读完后只能复述函数名，却不知道这个 Stage 的职责，那这一节就是失败的。

---

# 写作原则

## 第一段先回答「为什么有这个 Stage」

不要先讲代码。

不要：

> Run 开始之后会调用 XXX。

不要：

> 该 Stage 包含以下几个成员。

先回答：

> 这个 Stage 在解决什么问题？

例如：

> Reset Stage 的工作很简单：把上一次运行留下来的痕迹擦干净。

或者：

> Observe Stage 的任务是把终端里的信息整理成 LLM 能看懂的上下文。

读者应该先理解目的。

然后再理解实现。

---

## 像白板讲解

假设你正在会议室画框图。

不要像：

- API Reference
- Design Doc
- Code Walkthrough

而像：

> 这里其实是在做双重确认。
>
> 这里相当于给 LLM 准备材料。
>
> 这里是在收尾。

---

## 多讲 Why，少讲 How

优先解释：

- 为什么存在
- 为什么这么设计
- 如果删掉会怎样
- 为什么放在这里

其次再解释：

- 怎么实现

例如：

> 之所以先 reset，是因为同一个实例会被重复使用。
>
> 如果不清，上一次运行留下的数据会污染这一次。

这种解释比：

> reset() 会调用 A、B、C

更重要。

---

## 用短句

优先：

- 清
- 读
- 写
- 拼
- 等
- 看
- 抓
- 记

避免：

- 执行
- 实施
- 构造
- 消歧
- 归零
- 初始化流程

---

## 第一次出现术语要解释

例如：

- tmux（可以远程控制的终端）
- Chat（LLM 调用包装器）
- subagent（被主 Agent 调用的小 Agent）

假设读者不知道这些概念。

---

## 允许简化

不重要的旁支可以写：

> （这里还会处理 X 和 Y，略）

不要为了完整性破坏可读性。

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

第一句话直接回答：

> 这个 Stage 在解决什么问题？

不要 throat-clear。

这一段回答：

- 为什么存在
- 整体职责
- 在流水线中的位置

---

## (b) 主流程

用短 prose 或编号列表。

推荐格式：

1. 做什么
2. 做什么
3. 做什么

关键函数首次出现时：

`function_name()`（一句白话解释）

重点：

- 为什么调用
- 产出什么

不要变成代码 walkthrough。

---

## (c) 📊 状态流动

必须使用以下固定格式：

**📊 状态流动**

- 写: `<register-id>` — 什么时候写，为什么写
- 写: `<register-id>` — ...
- 读: `<register-id>` — 怎么使用
- 清: `<register-id>` — 为什么清
- 触发下游: `<stage-name>` — 在什么条件下进入

要求：

- register-id 必须来自输入
- 不允许编造
- 不允许遗漏核心 register

---

## (d) 与前后 Stage 的衔接

1-2 句即可。

回答：

- 上游给了什么
- 本 Stage 产出了什么
- 下游怎么消费

帮助读者建立流水线视角。

---

# 自检

1. 第一段第一句话是否直接回答「为什么有这个 Stage」？
2. 是否先讲职责，再讲实现？
3. 是否解释了为什么这么设计？
4. 是否避免变成函数逐行讲解？
5. 📊 状态流动中的 register 是否全部真实存在？
6. 新术语第一次出现时是否带括号解释？
7. 读者是否能回答：
   「这个 Stage 为什么存在？」
   
如果不能，重写。
"""


_TIER2_PROMPT_EN = """You are a senior Terminus 2 engineer.

The reader has already finished Tier 1.

They already know:

- what Terminus 2 is
- how the agent loops at a high level
- roughly where each stage sits

Now they want to answer a new question:

> Why does this stage exist?

Your job is NOT to explain the code.

Your job is to help the reader understand:

- what responsibility this stage owns
- why the system needs it
- how it fits into the surrounding pipeline
- what it contributes to the overall agent

If the reader finishes this page knowing function names but still cannot explain why the stage exists, this chapter has failed.

---

# Writing Principles

## Start With The Problem This Stage Solves

Do NOT start with code.

Avoid:

> This stage is called after setup.

Avoid:

> This stage contains the following members.

Instead start with:

> What problem is this stage solving?

Examples:

> Reset exists to wipe away leftovers from the previous run.

> Observe turns terminal state into context the LLM can understand.

The reader should understand the purpose before the implementation.

---

## Explain Like You're At A Whiteboard

Imagine you're sketching boxes for a teammate.

Do NOT write like:

- an API reference
- generated documentation
- a design specification
- a code walkthrough

Write like:

> This is basically a safety check.

> Think of this as preparing material for the LLM.

> This stage is mostly cleanup.

---

## Explain Why Before How

Prioritize:

- why it exists
- why it is placed here
- why it is designed this way
- what would happen if it were removed

Only then explain:

- how it works

For example:

> We reset here because the same agent instance gets reused across runs.
>
> Without it, state from the previous task would leak into the next one.

That explanation is more valuable than:

> The stage calls A(), B(), and C().

---

## Use Short Sentences

Prefer:

- clear
- read
- write
- build
- gather
- wait
- record
- check

Avoid:

- perform
- execute
- construct
- facilitate
- orchestrate
- initialize the workflow
- enter the next phase

---

## Define Terms On First Use

Examples:

- tmux session (a terminal you can drive remotely)
- Chat (a thin wrapper around an LLM call)
- subagent (a smaller agent invoked by the main agent)

Assume zero prior knowledge.

---

## Simplify Aggressively

Minor branches can be summarized as:

> (there are also a few edge cases handled here, omitted)

Completeness is less important than clarity.

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

The FIRST sentence must directly answer:

> What problem does this stage solve?

No warm-up.

This section should explain:

- why the stage exists
- its responsibility
- where it sits in the pipeline

---

## (b) Main Flow

Use short prose or a numbered list.

Recommended format:

1. What happens
2. What happens
3. What happens

When mentioning a function for the first time:

`function_name()` (plain-English explanation)

Focus on:

- why it is called
- what it produces
- what role it serves

Do not turn this into a line-by-line code walkthrough.

---

## (c) 📊 State Flow

Use this exact format:

**📊 State Flow**

- writes: `<register-id>` — when it is written and why
- writes: `<register-id>` — ...
- reads: `<register-id>` — how it is used
- clears: `<register-id>` — why it is cleared
- triggers downstream: `<stage-name>` — condition for transition

Requirements:

- register IDs must come from the provided input
- do not invent registers
- do not omit important registers

---

## (d) Pipeline Hand-Off

1–2 sentences.

Answer:

- what comes from upstream
- what this stage produces
- how downstream stages consume it

Help the reader see the pipeline rather than isolated code.

---

# Self-Check Before Emitting

1. Does the first sentence directly answer why this stage exists?
2. Did I explain responsibility before implementation?
3. Did I explain why the design exists?
4. Did I avoid turning this into a code walkthrough?
5. Are all register IDs in 📊 State Flow real?
6. Did I define jargon on first use?
7. Could the reader answer:

   "Why does this stage exist?"

If not, rewrite.
"""

_REGISTER_APPENDIX_PROMPT_ZH = """你是 Terminus 2 项目的资深工程师。

读者已经读完 Tier 1 和所有 Stage 章节。

他们现在想换一个视角看系统：

> 不再按 Stage 看，而是按 Register 看。

你的任务不是列字段。

你的任务是把每个 register 讲成一条“状态传话线”：

- 谁写它
- 谁读它
- 什么时候清它
- 它把哪一轮的信息传给哪一轮
- 为什么系统需要这条状态线

如果读者读完后只知道 register 的名字，却不知道它在系统里帮谁传话，这个 appendix 就失败了。

---

# 写作原则

## 每个 Register 是一张独立卡片

每张卡只讲一个 register。

但不能孤立讲。

必须把它联到具体：

- Stage
- 函数
- 读写场景
- 上下游用途

---

## 先讲用途，再讲生命周期

不要先抄字段。

不要直接复述 semantics。

先回答：

> 这个 register 解决什么问题？

例如：

> 它保存上一轮的完成判断，让系统不要只相信 LLM 的一次回答。

比：

> 该 register 表示 done confirmation state。

更适合 handbook。

---

## 讲清楚“状态怎么流”

每个 register 都要讲：

- 默认值是什么
- 哪里写
- 哪里读
- 哪里清
- 是否跨迭代
- 下游如何使用

读者应该能画出这条线。

---

## 短句、直接、少名词化

优先：

- 写
- 读
- 清
- 传
- 留
- 查
- 挡
- 接

避免：

- 执行
- 实施
- 构造
- 消歧
- 进行持久化
- 完成状态同步

---

## 允许承认不跨迭代

不是所有 register 都要跨轮传话。

如果它只在单轮内生效，就明确写：

> 单轮内。

不要强行解释成跨迭代状态。

---

# 输入

## 全部 State Registers

{registers_full}

## 全部 Stage / Side / Crosscut / Subsystem 一句话定位

{all_stages_brief}

---

# 输出格式（Markdown 直出）

按输入顺序，为每个 register 输出一张卡。

只用 H3 标题。

不要 H1 / H2。

卡之间空一行即可，不要 `---`。

---

每张卡严格使用下面模板：

### 🔄 `<register-id>`

**用途**: <1 句话。回答“这个 register 解决什么问题”。不要直接抄 semantics，要重新解释。>

**生命周期**:
- **默认值**: <默认值；如果输入没有写清楚，写“输入未说明”>
- **重置**: <在哪个 stage / 函数被重置；若无显式 reset，写“无显式 reset”>
- **写**:
  - `<stage-id>` / `<function-name>` — <什么时候写，为什么写>
- **读**:
  - `<stage-id>` / `<function-name>` — <什么时候读，怎么用>
- **清 / 回填**: <如有则写，否则“无”>

**跨迭代传话**: <如果跨迭代生效，写成“第 N 轮 → 第 N+1 轮：传什么、谁读”。如果只在单轮内生效，写“单轮内”。>

**为什么这么设计**: <1-2 句。解释设计动机，不要只描述现象。>

---

# 强约束

- register 数量必须等于输入数量。
- register-id 必须来自输入。
- Stage / 函数名必须来自输入；不确定就写“输入未说明”，不要编。
- 每张卡 80-200 字。
- 不要输出总览段。
- 不要输出 H1 / H2。
- 不要使用 `---` 分隔卡片。
- 不要把所有 register 混成一段。
- 不要只解释字段含义，要解释它在系统里怎么传话。

---

# 自检

1. 是否每个 register 都有一张卡？
2. register-id 是否都来自输入？
3. 是否讲清楚谁写、谁读、谁清？
4. 是否说明它是跨迭代还是单轮内？
5. 是否解释了为什么需要这个 register？
6. 是否避免直接复述 semantics？
7. 读者是否能回答：

   “这个 register 在帮哪些 stage 传什么话？”

如果不能，重写。
"""

_REGISTER_APPENDIX_PROMPT_EN = """You are a senior Terminus 2 engineer.

The reader has already finished:

- Tier 1 (system overview)
- all stage chapters

They now want to look at the system from a different angle:

> not stage-by-stage, but register-by-register.

Your job is NOT to list fields.

Your job is to explain each register as a state handoff mechanism.

For every register, the reader should understand:

- who writes it
- who reads it
- when it gets reset
- whether it survives across iterations
- what information it carries
- why the system needs that information channel

If the reader finishes this appendix knowing register names but still cannot explain how information flows through the system, this appendix has failed.

---

# Writing Principles

## Treat Every Register As A Communication Line

Each register is a standalone card.

But never describe it in isolation.

Always connect it to:

- stages
- functions
- producers
- consumers
- state transitions

The reader should see a flow of information, not a data structure.

---

## Start With The Problem It Solves

Do NOT start by paraphrasing semantics.

Do NOT write:

> This register stores completion confirmation state.

Instead write:

> This register prevents the agent from trusting a single completion signal.

Focus on the problem first.

Then explain the mechanism.

---

## Explain State Movement

Every register should answer:

- where does its value come from?
- where does it go?
- who depends on it?
- when is it cleared?
- does it survive into the next iteration?

The reader should be able to draw the state flow after reading the card.

---

## Use Short Sentences

Prefer:

- write
- read
- clear
- carry
- pass
- check
- keep
- block

Avoid:

- perform
- execute
- facilitate
- orchestrate
- synchronize state
- maintain consistency
- perform persistence

---

## Cross-Iteration Matters

If the register survives across iterations:

show the handoff explicitly.

Example:

> Iteration N writes the completion candidate.
>
> Iteration N+1 checks it before finalizing completion.

If it does not survive:

simply write:

> single-iteration

Do not force an artificial cross-iteration story.

---

## Be Honest About Missing Information

If the input does not clearly identify:

- reset locations
- write sites
- read sites

write:

> input does not specify

Do not invent behavior.

---

# Input

## All State Registers

{registers_full}

## All Stage / Side / Crosscut / Subsystem Positioning

{all_stages_brief}

---

# Output (Markdown directly)

For every register, emit one card.

Use H3 headings only.

Do NOT emit H1 or H2.

Separate cards with a single blank line.

Do NOT use horizontal rules.

---

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

**Cross-Iteration Behavior**:
<either:
Iteration N → Iteration N+1 handoff
or:
single-iteration>

**Why This Design**:
<1–2 sentences explaining the design motivation rather than the mechanics.>

---

# Hard Constraints

- Number of cards must equal number of registers.
- Every register-id must come from the input.
- Stage names and function names must come from the input.
- If uncertain, write "input does not specify".
- Do not invent behavior.
- 80–180 words per card.
- No overview section.
- No H1.
- No H2.
- No horizontal rules.
- One register per card.

---

# Self-Check

1. Does every register have exactly one card?
2. Does every card explain who writes and who reads it?
3. Does every card explain whether it crosses iterations?
4. Did I explain the problem it solves rather than restating semantics?
5. Are all stage names and function names grounded in the input?
6. Could a reader answer:

   "Which stages use this register to pass information?"

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
