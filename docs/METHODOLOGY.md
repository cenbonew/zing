# zing Methodology / 方法论

**zing** is an LLM relay reality-check: a black-box auditor that asks whether an
OpenAI-compatible relay actually serves the model it *claims* to serve — or
whether it silently truncates context, downgrades/substitutes the model, fakes
capabilities, fakes streaming, inflates billing, or quietly degrades under load.

**zing** 是一个面向中转站 / 代理 (relay) 的"货不对板"体检工具：在纯黑盒
(black-box) 条件下，检验一个 OpenAI 兼容接口是否真的提供它所**声称**的模型——
还是偷偷截断上下文、降级/替换模型、伪造能力、伪造流式、虚报计费，或在高负载时
悄悄降质。

> **Reading note / 阅读说明.** Each detector dimension below is documented in
> English first, then 中文. The mapping table and the limitations section apply
> to all dimensions. 下面每个维度都先英文、后中文；末尾的对照表与"局限与
> 负责任使用"一节适用于所有维度。

---

## Core stance / 核心立场

zing reports **divergence and risk**, never proof of fraud. Every finding is
evidence-first and uses cautious language. A relay may legitimately drift,
update snapshots, pool capacity, or buffer a non-streaming upstream — so a single
signal is treated as a *risk indicator*, not a verdict. The headline verdict only
escalates to HIGH on hard, high-severity evidence (see `zing/scoring.py`), and the
strongest confirmation path is always **compare mode**: run the identical probe
set against a *trusted baseline of the claimed model* and diff.

zing 只报告**偏离与风险**，绝不下"欺诈"结论。所有发现都以证据为先、措辞谨慎。
中转站可能出于正当原因发生漂移、更新快照、共享算力池或对不支持流式的上游做缓冲——
因此任何单一信号都被视为**风险指标**，而非定论。总体判定只有在出现确凿的高
严重度证据时才会升级为 HIGH（见 `zing/scoring.py`）；而最强的确认手段始终是
**对比模式 (compare mode)**：用完全相同的探针集去打一个**可信的、所声称模型的
基线 (baseline)**，再做逐项对比。

### Dimension weights / 维度权重

The substitution- and truncation-revealing dimensions dominate the score
(`DIMENSION_WEIGHTS` in `zing/scoring.py`):

替换 / 截断类维度在评分中权重最高（见 `zing/scoring.py` 的 `DIMENSION_WEIGHTS`）：

| Dimension / 维度 | Weight / 权重 | Role / 作用 |
|---|---|---|
| model_identity | 22 | core 核心 |
| context_window | 20 | core 核心 |
| capability | 14 | core 核心 |
| connectivity | 8 | gate 前置 |
| protocol | 8 | conformance 合规 |
| billing | 8 | |
| reliability | 8 | |
| streaming | 6 | |
| security | 6 | |

The three **core dimensions** (`model_identity`, `context_window`, `capability`)
are the ones whose failure most directly indicates 货不对板; the verdict's
confidence rises to "high" only when all three ran *and* a baseline was used.

三个**核心维度**（`model_identity`、`context_window`、`capability`）的失败最直接
指向"货不对板"；只有这三者全部运行**且**使用了基线时，判定置信度才会达到 "high"。

---

## connectivity — Connectivity & basic completion / 连通性与基础补全

**Catches / 命中的伎俩.** None directly — this is the gate every other detector
depends on. It separates a dead/misconfigured key from a relay worth auditing.

**Black-box technique / 黑盒技术.** Hit `/v1/models` (note whether the claimed
model is listed) and issue one deterministic chat completion (`temperature=0`)
asking the model to echo an exact canary marker. Record latency and the echoed
`model_returned` field.

**What counts as a finding / 何为发现.** The chat completion fails (no content,
HTTP error) → FAIL/HIGH. `/v1/models` unreachable is only WARN/LOW (many relays
disable it). The canary not being echoed lowers the score but does not fail the
gate.

**False-positive caveats / 误报与确认.** `/v1/models` absence is benign — do not
treat it as deception. A transient network/5xx error can fail the gate; re-run.
Connectivity alone proves nothing about *which* model answered — that is the job
of model_identity.

---

**connectivity — 连通性与基础补全（中文）**

**命中的伎俩.** 不直接命中任何作弊手法——它是其他所有检测器的前置门槛，用来把
"密钥已失效 / 配置错误"和"值得审计的中转站"区分开。

**黑盒技术.** 访问 `/v1/models`（记录所声称模型是否在列表中），并发起一次
确定性补全（`temperature=0`），要求模型原样回显一个精确的 canary 标记，记录
时延以及回显的 `model_returned` 字段。

**何为发现.** 补全失败（无内容 / HTTP 错误）→ FAIL/HIGH。`/v1/models` 不可达
只是 WARN/LOW（许多中转站会关闭它）。canary 未被回显会降低分数，但不会让门槛
失败。

**误报与确认.** `/v1/models` 缺失属正常，切勿当作欺诈。瞬时网络 / 5xx 错误可能
导致门槛失败，应重跑。连通性本身无法证明"是哪个模型"在应答——那是 model_identity
的职责。

---

## protocol — OpenAI-spec conformance / 协议合规

**Catches / 命中的伎俩.** `capability.json-tool-fakery` (partially), and any
relay whose home-grown middleware mangles the OpenAI response envelope. Backed by
the prior-art gap that *no production OpenAI-compatibility conformance suite
exists* (only vLLM as a de-facto spec, NeMo curl recipes).

**Black-box technique / 黑盒技术.** Assert the OpenAI surface: response object
shape, valid `finish_reason` values, presence/shape of the `usage` object,
SSE framing (`data:` chunks + `[DONE]` terminator), and the error JSON schema.
Curl-recipe-style minimal probes for portability.

**What counts as a finding / 何为发现.** Structurally invalid envelopes, illegal
`finish_reason`, missing required fields, or a malformed SSE terminator → WARN/FAIL
depending on severity. These are conformance defects, not (yet) fraud.

**False-positive caveats / 误报与确认.** Legitimate gateways may emit
provider-specific extra fields (forbidding them would over-flag) — only flag
*missing required* or *structurally invalid* fields, not extras. Anthropic↔OpenAI
dialect translation legitimately reshapes some structures. Confirm a suspected
defect by repeating and, where possible, comparing the same probe against a known
conformant endpoint.

---

**protocol — 协议合规（中文）**

**命中的伎俩.** 部分命中 `capability.json-tool-fakery`，以及任何用自研中间件
破坏 OpenAI 响应封装的中转站。研究中的空白点表明：业界**没有**生产级的
OpenAI 兼容性合规套件（只有 vLLM 这一事实标准、以及 NeMo 的 curl 配方）。

**黑盒技术.** 校验 OpenAI 接口面：响应对象结构、合法的 `finish_reason` 取值、
`usage` 对象的存在与结构、SSE 帧格式（`data:` 分片 + `[DONE]` 终止符）、以及
错误 JSON schema。采用 curl 配方式的最小探针以保证可移植性。

**何为发现.** 结构非法的封装、非法的 `finish_reason`、缺少必填字段、或畸形的
SSE 终止符——视严重度判 WARN/FAIL。这些是合规缺陷，（暂）不等同于欺诈。

**误报与确认.** 合法网关可能附带厂商特定的额外字段（一律禁止会过度告警）——
只对**缺失必填**或**结构非法**的字段告警，不对额外字段告警。Anthropic↔OpenAI
方言转换会正当地重塑部分结构。确认可疑缺陷的方式是重复探测，并在可能时把同一
探针打到已知合规的接口做对比。

---

## context_window — Effective context window / 真实上下文窗口

**Catches / 命中的伎俩.** `context.window-truncation` (relay advertises 128K/200K/1M
but silently trims the prompt) and `context.lost-in-middle-rag` (a cheap
RAG/summarization shim forwards only top-k chunks or an LLM-compressed summary).

**Black-box technique / 黑盒技术.** Needle-in-a-haystack (NIAH/RULER-style) canary
recall. Generate a high-entropy canary (`NEEDLE-<uuid4>`), embed it at a known
depth inside *non-repetitive* filler of total length L, and ask the model to
return the exact string. Sweep L over {4K…1M} and depth over {0…1.0}; binary-search
the smallest length at which an **edge-placed** needle stops being recalled to
estimate the effective window. Run the needle at the START *and* the END to catch
both tail- and head-truncation, and use multi-needle + NoLiMa-style **non-literal**
needles (answer requires semantic, not keyword, match) to expose RAG/summarization
shims via chunk-boundary-aligned or all-or-nothing failures. Token counting uses
the claimed model's tokenizer hint; the detector is `pure_code_detectable` (no
judge needed).

**What counts as a finding / 何为发现.** Disappearance of an *easy, edge-placed*
needle at a length well below the advertised window → context-truncation risk;
effective/advertised ratio < ~0.5 is a strong finding. Distinctive all-or-nothing
or chunk-boundary failure patterns, or failure on non-literal needles that a
literal needle passes → RAG/summarization-shim risk.

**False-positive caveats / 误报与确认.** A genuine long-context model exhibits
*lost-in-the-middle* and can fail a too-hard mid-needle even with no truncation —
so **only edge-placement failures** prove truncation. Make the needle trivially
retrievable (verbatim, explicit instruction) and require exact match. Recall near
the cliff is probabilistic: repeat each cell and accept it only on majority recall.
Middle-only truncation (head+tail kept) is missed by an edge search alone — pair it
with the mid-placement multi-needle sweep. To confirm, baseline the same prompts
against the trusted model to separate intrinsic model behavior from shim behavior.

---

**context_window — 真实上下文窗口（中文）**

**命中的伎俩.** `context.window-truncation`（中转站宣称 128K/200K/1M，却悄悄
裁剪 prompt）以及 `context.lost-in-middle-rag`（廉价的 RAG / 摘要垫片只转发
top-k 分片或经 LLM 压缩的摘要）。

**黑盒技术.** 大海捞针 (NIAH / RULER 风格) canary 召回。生成高熵 canary
（`NEEDLE-<uuid4>`），将其埋入总长 L 的**非重复**填充文本的已知深度处，要求模型
原样返回该串。在 {4K…1M} 上扫 L、在 {0…1.0} 上扫深度；对**边缘放置**的 needle
做二分搜索，找出召回失败的最小长度，以估算真实窗口。把 needle 分别放在**开头**
**和结尾**，以同时捕捉尾部截断与头部截断；并用多 needle + NoLiMa 风格的**非字面**
needle（答案需语义匹配而非关键词匹配），通过分片边界对齐式或全有全无式的失败
来暴露 RAG / 摘要垫片。token 计数使用所声称模型的 tokenizer 提示；该检测器属
`pure_code_detectable`（无需裁判模型）。

**何为发现.** 在远低于宣称窗口的长度上，一个**简单的、边缘放置**的 needle 消失
→ 上下文截断风险；真实/宣称比 < ~0.5 是强信号。出现明显的全有全无或分片边界
式失败，或在非字面 needle 上失败而字面 needle 通过 → RAG / 摘要垫片风险。

**误报与确认.** 真正的长上下文模型也会"中段迷失"(lost-in-the-middle)，即使没有
截断也可能在过难的中段 needle 上失败——所以**只有边缘放置的失败**才能证明截断。
要把 needle 设计得极易召回（逐字、明确指令）并要求精确匹配。临界点附近的召回
是概率性的：每个格子要重复，且仅在多数召回时才记为通过。仅截断中段（保留头+尾）
的情形单靠边缘搜索会漏掉——需配合中段多 needle 扫描。确认手段：把相同 prompt 打到
可信基线，以区分模型固有行为与垫片行为。

---

## model_identity — Is this the claimed model? / 是不是所声称的模型

**Catches / 命中的伎俩.** The headline cheats — `downgrade.silent-substitution`
(premium name, cheaper/open backend), `downgrade.reasoning-collapse` (claim a
reasoner, serve the chat sibling), `downgrade.quantized-distilled` (lossy 4-bit/
distilled weights under the full name), and `downgrade.partial-probabilistic-routing`
(serve the real model only to a fraction of requests / known auditors).

**Black-box technique / 黑盒技术.** Two independent verification methods, because
each catches a different cheat:
1. **Active fingerprinting (LLMmap-style)** — a small curated battery of
   discriminative probes (style, refusal phrasing, format quirks, knowledge cutoff,
   rare-token recall, self-identification), embed outputs and compute cosine distance
   to a trusted baseline; flag when distance > ~1.2× baseline self-distance, or when
   the nearest model in a reference DB ≠ the claimed model. Catches outright
   *identity* substitution.
2. **Distributional / behavioral testing (Model Equality Testing / rank-based RUT)** —
   two-sample test (rank-uniformity / Cramér-von Mises-style) at α=0.05 over many
   samples; catches *behavioral drift* (quantization, weight edits) that keeps the
   same identity. Works even when only top-k logprobs (or none) are exposed.

For reasoning-collapse, run problems with a sharp reasoning-vs-chat accuracy cliff
and check the latency/reasoning-token profile against a same-family reasoner. For
probabilistic routing, use **randomized high-volume, paraphrased** probes
(`>>50` requests) and test for a *bimodal/mixture* distribution of quality, latency,
and fingerprint distance — reporting the estimated *fraction* of downgraded requests.
This dimension `needs_llm_judge` for grading open-ended outputs and largely
`requires_baseline` for its strongest signals.

**What counts as a finding / 何为发现.** Fingerprint distance above the baseline-
self-distance threshold; a capability gap of >15–20pp on ≥3 independent benchmarks;
a two-sample test rejecting equality at α=0.05; near-constant reasoning-token counts
regardless of difficulty; or a clear multimodal split across requests. Self-ID alone
(`"what model are you?"`) only corroborates — it never decides.

**False-positive caveats / 误报与确认.** Official APIs silently update / fine-tune
snapshots, so divergence can be *benign drift*, not substitution — report
"divergent / inconclusive", never "proven substitution". Temperature noise inflates
apparent difference: use `temperature=0`, large N (≥500 for distribution tests),
three independent benchmark runs (accuracy SD < 5pp), and latency CV < 0.15. Identity
fingerprint can PASS while behavior collapses (quantization) and vice versa — use
both signals. A single self-ID answer is unreliable (models hallucinate their own
name). Relays may memorize a fixed probe battery — rotate/paraphrase. **Compare mode
against a genuinely trusted baseline of the exact claimed snapshot is mandatory for
a high-confidence verdict.**

---

**model_identity — 是不是所声称的模型（中文）**

**命中的伎俩.** 最核心的几类作弊——`downgrade.silent-substitution`（挂高端名、
跑廉价/开源后端）、`downgrade.reasoning-collapse`（声称推理模型，却给出非推理
兄弟版）、`downgrade.quantized-distilled`（用全量模型名跑 4-bit / 蒸馏的有损权重）、
以及 `downgrade.partial-probabilistic-routing`（只对一部分请求 / 已知审计者才给
真模型）。

**黑盒技术.** 两种相互独立的验证方法，因为它们各自命中不同作弊：
1. **主动指纹 (LLMmap 风格)** —— 一小组精选的判别性探针（风格、拒答措辞、格式
   怪癖、知识截止、稀有 token 召回、自我标识），将输出做嵌入并计算与可信基线的
   余弦距离；当距离 > ~1.2× 基线自距离、或参考库中最近模型 ≠ 所声称模型时告警。
   命中赤裸裸的**身份**替换。
2. **分布 / 行为检验 (Model Equality Testing / 基于秩的 RUT)** —— 在大量样本上做
   双样本检验（秩均匀性 / Cramér-von Mises 风格），α=0.05；命中保持同一身份的
   **行为漂移**（量化、权重编辑）。即使只暴露 top-k logprobs（甚至不暴露）也有效。

对"推理坍缩"，运行推理 vs 非推理存在陡峭准确率断崖的题目，并把时延 / 推理 token
画像与同族推理模型对比。对概率路由，使用**随机化、大批量、改写过**的探针
（`>>50` 次请求），检验质量 / 时延 / 指纹距离是否呈**双峰 / 混合**分布——并报告
被降级请求的估计**占比**。本维度在为开放式输出打分时 `needs_llm_judge`，其最强
信号大体上 `requires_baseline`。

**何为发现.** 指纹距离超过基线自距离阈值；在 ≥3 个独立基准上能力差 >15–20pp；
α=0.05 下双样本检验拒绝"相等"；推理 token 数几乎与难度无关而保持恒定；或跨请求
出现清晰的多峰分裂。仅靠自我标识（"你是哪个模型？"）只能佐证，绝不单独定论。

**误报与确认.** 官方 API 会悄悄更新 / 微调快照，因此偏离可能是**良性漂移**而非
替换——只报"偏离 / 不确定"，绝不报"已证实替换"。温度噪声会放大表观差异：用
`temperature=0`、大样本 N（分布检验 ≥500）、三次独立基准运行（准确率 SD < 5pp）、
时延 CV < 0.15。身份指纹可能通过而行为坍缩（量化），反之亦然——两个信号都要用。
单次自我标识不可靠（模型会幻觉出自己的名字）。中转站可能记住固定探针集——要
轮换 / 改写。**要得到高置信度判定，必须用对比模式，对照一个真正可信、与所声称
快照完全一致的基线。**

---

## capability — Advertised feature fidelity / 能力真实性

**Catches / 命中的伎俩.** `capability.json-tool-fakery` — the relay claims function/
tool calling, strict JSON mode / structured outputs, vision, or logprobs, but the
backend doesn't truly support it and the relay fakes it with regex post-processing,
ignores the schema, or doesn't honor `tool_choice`.

**Black-box technique / 黑盒技术.** Capability-claim probing with adversarial
structure. (1) **JSON mode** — request `response_format` `json_object`/`json_schema`
with a strict, deeply nested schema and *adversarial* content (strings full of
braces/quotes, unicode, nested arrays) that tempts a regex faker to break; measure
parse-success and schema-conformance *rates* over many trials. (2) **Tool calling** —
define a tool, set `tool_choice="required"` or force a specific function, assert a
well-formed `tool_calls` array with JSON-valid, *value-faithful* arguments (use
fully-determined expected args / canaries), and test parallel calls. (3) Probe other
claims the same way (logprobs present? seed honored? vision actually read a detail
only in the image?). `pure_code_detectable` for parsing/validation; baselining
sharpens it.

**What counts as a finding / 何为发现.** A high malformed-rate or schema-violation
rate under stress, the forced tool not being chosen, or argument values not matching
the determined expectation → faked/unreliable-capability risk. Single failures are
not findings — *rates* are.

**False-positive caveats / 误报与确认.** Even genuine top models occasionally emit
invalid JSON or skip a forced tool, so measure failure **rates** over many trials,
not single failures. Legitimate Anthropic↔OpenAI dialect translation reshapes
tool-call JSON — distinguish benign *structural reshaping* from genuine non-support
by baselining the same probes against the claimed model. Prefer `json_schema` strict
mode where the spec guarantees validity for the cleanest signal. "Support" may be
partial — probe the specific sub-feature you actually rely on.

---

**capability — 能力真实性（中文）**

**命中的伎俩.** `capability.json-tool-fakery` —— 中转站声称支持函数 / 工具调用、
严格 JSON 模式 / 结构化输出、视觉或 logprobs，但后端并不真正支持，于是用正则
后处理伪造、忽略 schema、或不遵守 `tool_choice`。

**黑盒技术.** 用对抗性结构做能力声明探测。(1) **JSON 模式** —— 用严格的深层嵌套
schema 加**对抗性**内容（充满花括号 / 引号的字符串、unicode、嵌套数组，专门
诱使正则伪造者出错）请求 `response_format` 的 `json_object`/`json_schema`；在大量
试验上测量解析成功率与 schema 合规**率**。(2) **工具调用** —— 定义工具，设
`tool_choice="required"` 或强制某函数，校验返回是结构良好的 `tool_calls` 数组、
参数 JSON 合法且**取值忠实**（用完全确定的期望参数 / canary），并测试并行调用。
(3) 同法探测其他声明（是否返回 logprobs？是否遵守 seed？视觉是否真读到了只在
图中可见的细节？）。解析 / 校验部分属 `pure_code_detectable`，基线对比可增强。

**何为发现.** 压力下高畸形率或 schema 违例率、被强制的工具未被选中、或参数取值
不符合确定期望 → 能力造假 / 不可靠风险。单次失败不算发现——要看**比率**。

**误报与确认.** 即便顶级真模型偶尔也会输出非法 JSON 或跳过被强制的工具，所以要在
大量试验上测**失败率**，而非单次失败。合法的 Anthropic↔OpenAI 方言转换会重塑
tool-call JSON——通过把相同探针打到所声称模型做基线对比，区分良性的**结构重塑**
与真正的不支持。在规范保证有效的地方优先用 `json_schema` 严格模式，信号最干净。
"支持"可能是部分的——要探测你实际依赖的那个子功能。

---

## streaming — Real vs fake streaming / 真假流式

**Catches / 命中的伎俩.** `stream.fake-streaming` — the relay claims SSE token
streaming but actually buffers the full upstream completion and slices it into fake
chunks, defeating the latency benefit and revealing that it can log/inspect/modify
the whole response.

**Black-box technique / 黑盒技术.** Inter-chunk timing analysis. Request a long
generation (`max_tokens ≥ 256`, `stream=true`); record each chunk's arrival
timestamp. Compute TTFT, total duration, the inter-token-latency (ITL) distribution,
the ratio R = TTFT/total, the coefficient of variation CV(ITL), and the linearity of
cumulative-bytes-vs-time. Classify: **REAL** = small R (<~0.3), positive ITL with
natural jitter (CV ~0.2–1.0), bytes grow roughly linearly from early on. **FAKE-
buffered** = R ≈ 1 (whole answer in a late burst). **FAKE-timer** = CV(ITL) near 0
(<0.05), unnaturally uniform gaps. `pure_code_detectable`.

**What counts as a finding / 何为发现.** R ≈ 1 / late-burst delivery, or uniform-timer
ITL (CV near zero) → fake-streaming risk (the timer pattern is the strongest tell).

**False-positive caveats / 误报与确认.** Network jitter, short outputs, server-side
speculative decoding, and a fast small model can all mimic burst delivery; a real
upstream that *doesn't* support streaming forces the relay to buffer **legitimately**
— frame that as "relay does not stream", not malice. Mitigate with long outputs
(≥256 tokens), repeat across runs, and compare the TTFT/total ratio against a
**known-streaming baseline**. Treat uniform-timer ITL (CV≈0) as the only near-
conclusive tell.

---

**streaming — 真假流式（中文）**

**命中的伎俩.** `stream.fake-streaming` —— 中转站声称 SSE token 级流式，实际上把
上游完整结果缓冲下来再切成假的分片，既抹掉了延迟优势，也暴露出它能够
记录 / 检查 / 修改整段响应。

**黑盒技术.** 分片间时序分析。请求一个长生成（`max_tokens ≥ 256`，`stream=true`），
记录每个分片的到达时间戳。计算 TTFT、总时长、token 间延迟 (ITL) 分布、比值
R = TTFT/总时长、ITL 的变异系数 CV、以及"累计字节-时间"的线性度。分类：**真实** =
R 小（<~0.3）、ITL 为正且有自然抖动（CV ~0.2–1.0）、字节从早期起近似线性增长；
**假-缓冲** = R ≈ 1（整段在末尾一次性涌出）；**假-定时器** = CV(ITL) 近 0（<0.05），
间隔异常均匀。属 `pure_code_detectable`。

**何为发现.** R ≈ 1 / 末尾爆发式投递，或定时器式均匀 ITL（CV 近零）→ 伪造流式
风险（定时器模式是最强信号）。

**误报与确认.** 网络抖动、短输出、服务端投机解码、以及快速的小模型都可能模仿
爆发式投递；而上游**本就不支持**流式时，中转站只能**正当地**缓冲——这应表述为
"该中转站不提供流式"，而非恶意。缓解办法：用长输出（≥256 token）、多次重复、并
把 TTFT/总时长比与**已知确实流式的基线**对比。只有定时器式均匀 ITL（CV≈0）才
近乎定论。

---

## billing — Usage / billing audit / 计费稽核

**Catches / 命中的伎俩.** `billing.usage-inflation` (over-reports prompt/completion
tokens or injects invisible "reasoning" tokens), `billing.missing-usage` (omits/zeroes/
contradicts the `usage` object so billing is unauditable), and corroborates
`prompt.injected-system-prompt` (hidden prepended content inflates `prompt_tokens`).

**Black-box technique / 黑盒技术.** Tokenizer-based billing audit. For deterministic
probes (`temperature=0`, fixed prompt+output) independently tokenize the exact prompt
(with chat-template framing) and the exact returned content using the *correct
tokenizer for the claimed model* (e.g. tiktoken `o200k_base`/`cl100k_base` for GPT).
Compare computed vs reported `prompt_tokens`/`completion_tokens`; across many sizes
fit `reported = a·computed + b`. Slope a > 1 → a per-token multiplier; large
intercept b → injected hidden context. Separately assert presence and arithmetic
consistency (`total == prompt + completion`, positive counts) and that a final usage
chunk arrives when `stream_options.include_usage=true`. `pure_code_detectable`.

**What counts as a finding / 何为发现.** Reported systematically exceeds computed
beyond a small tolerance and *scales with size* (slope > 1 or large intercept) →
inflation risk. Absent/zero/internally inconsistent usage → unauditable-billing risk.

**False-positive caveats / 误报与确认.** Different models use different tokenizers,
and chat framing/special tokens add a small **fixed** offset, so an exact match is
not expected — allow a small offset, test multiple tokenizers, and focus on the
**slope/multiplier**, not absolute counts. Only flag *systematic, size-scaling*
overcounting. If the served model differs from the claimed one, the "right" tokenizer
is unknown — caveat accordingly. Reasoning/hidden tokens are intentionally invisible:
**bound** them (PALACE-style estimation), don't claim exactness. Some legitimate
gateways omit usage on streaming unless `include_usage` is set — distinguish "feature
unsupported" (WARN) from "usage present but internally inconsistent" (FAIL).

---

**billing — 计费稽核（中文）**

**命中的伎俩.** `billing.usage-inflation`（虚报 prompt/completion token，或注入不可见
的"推理"token）、`billing.missing-usage`（省略 / 置零 / 自相矛盾的 `usage` 对象，
使计费无法稽核），并佐证 `prompt.injected-system-prompt`（隐藏的前置内容会抬高
`prompt_tokens`）。

**黑盒技术.** 基于分词器的计费稽核。对确定性探针（`temperature=0`，固定 prompt+
输出），用**所声称模型的正确分词器**（如 GPT 用 tiktoken `o200k_base`/`cl100k_base`）
独立地对精确 prompt（含 chat 模板封装）与精确返回内容分词。比较计算值与上报的
`prompt_tokens`/`completion_tokens`；在多种长度上拟合 `reported = a·computed + b`：
斜率 a > 1 → 每 token 乘数；截距 b 偏大 → 注入了隐藏内容。另外校验 `usage` 的存在
与算术一致性（`total == prompt + completion`、计数为正），以及当
`stream_options.include_usage=true` 时是否到达最终 usage 分片。属 `pure_code_detectable`。

**何为发现.** 上报值系统性地超出计算值（超过小容差）且**随长度增长**（斜率 > 1
或截距偏大）→ 虚报风险。`usage` 缺失 / 置零 / 内部不一致 → 计费不可稽核风险。

**误报与确认.** 不同模型用不同分词器，chat 封装 / 特殊 token 会带来一个小的
**固定**偏移，因此不应期望精确相等——允许小偏移、尝试多种分词器、关注**斜率 /
乘数**而非绝对计数。只对**系统性、随长度增长**的多计告警。若实际服务的模型与
所声称的不同，则"正确"分词器未知，需相应加注。推理 / 隐藏 token 本就不可见：
要**给出上界**（PALACE 风格估计），而非声称精确。部分合法网关在未设
`include_usage` 时不返回流式 usage——要区分"功能不支持"(WARN) 与"usage 存在但
内部不一致"(FAIL)。

---

## reliability — Stability under load & over time / 负载与时间下的稳定性

**Catches / 命中的伎俩.** `throttle.rate-limit-quality` (degrades by load/time:
silently lowers `max_tokens`, swaps to a cheaper model at peak hours, truncates
outputs, raises latency, or injects spurious 429/5xx) and `infra.shared-upstream-key`
(all customers funneled through one upstream key → correlated rate limits, shared
quota, no tenant isolation). It also feeds the meta-signals used by model_identity
(latency CV, token-count volatility).

**Black-box technique / 黑盒技术.** Longitudinal + load-conditioned probing. Run a
fixed probe battery (including a capability probe and a "generate exactly N words"
length probe) repeatedly across hours/days and at low vs high concurrency; track
per-run capability pass/fail, actual output length vs requested, latency p50/p95/p99,
and error-by-type. For shared-pool inference: read rate-limit headers
(`x-ratelimit-remaining-*`, `retry-after`, `x-request-id`, processing-ms/org headers),
send single probes **while otherwise idle** and watch whether remaining-quota
decreases (someone else is consuming the bucket), run a concurrency test for premature
429s, and look for leaked upstream-provider request-ids. `pure_code_detectable`;
benefits from a judge for the capability sub-probe.

**What counts as a finding / 何为发现.** Time-of-day- or load-correlated swings —
capability passes at 3am but fails at 8pm, completion length silently shrinks under
concurrency → throttling/peak-downgrade risk. Quota decrementing while you are idle,
premature 429s vs your declared limits, or leaked upstream request-ids → shared-pool
risk.

**False-positive caveats / 误报与确认.** Genuine providers also slow down and 429
under real load, and output length varies naturally; a single-snapshot audit misses
time-based behavior. Require **repeated** measurements across time windows; correlate
degradation specifically with load/time rather than randomness; separate "slower"
(acceptable) from "different model / shorter output" (cheating). Legitimate
multi-tenant gateways pool capacity and emit shared-looking headers — the strongest
signal is **quota decrementing while your client is idle**, and leaked upstream
request-ids; this can flag a shared-pool *risk* but **cannot prove key theft**.

---

**reliability — 负载与时间下的稳定性（中文）**

**命中的伎俩.** `throttle.rate-limit-quality`（按负载 / 时段降质：悄悄调低
`max_tokens`、高峰时段切换到廉价模型、截断输出、抬高时延、或注入假的 429/5xx）
以及 `infra.shared-upstream-key`（所有客户共用一个上游密钥 → 速率限制相关、配额
共享、无租户隔离）。它还为 model_identity 提供元信号（时延 CV、token 计数波动）。

**黑盒技术.** 纵向 + 负载条件下的探测。把固定探针集（含一个能力探针和一个
"生成正好 N 个词"的长度探针）跨小时 / 天、在低 vs 高并发下反复运行；逐次记录能力
通过 / 失败、实际输出长度 vs 请求值、时延 p50/p95/p99、按类型分类的错误。推断共享池：
读取速率限制头（`x-ratelimit-remaining-*`、`retry-after`、`x-request-id`、
processing-ms / 组织头），在**自身空闲**时发单个探针，观察 remaining 配额是否下降
（说明别人在消耗同一桶），做并发测试看是否过早 429，并查找泄漏的上游 request-id。
属 `pure_code_detectable`，能力子探针可借助裁判模型。

**何为发现.** 与时段 / 负载相关的波动——凌晨 3 点能力通过、晚 8 点失败，或并发下
补全长度悄悄缩水 → 限流 / 高峰降级风险。自身空闲时配额仍在递减、相对你声称限额
的过早 429、或泄漏的上游 request-id → 共享池风险。

**误报与确认.** 真正的厂商在真实负载下也会变慢、也会 429，输出长度本就有波动；
单次快照审计会漏掉与时间相关的行为。要在多个时间窗口**反复**测量；要把降质明确
关联到负载 / 时段而非随机性；要区分"更慢"（可接受）与"换了模型 / 输出更短"
（作弊）。合法的多租户网关也会共享算力并发出看似共享的头——最强信号是
**自身空闲时配额仍在递减**以及泄漏的上游 request-id；这只能标记共享池**风险**，
**无法证明密钥被盗**。

---

## security — Privacy, cache & integrity / 隐私、缓存与完整性

> **Implementation status.** This section describes the full methodology zing aims
> for; not all of it ships in v0.1.0. **Implemented today:** the `security` detector
> checks transport (HTTPS), revealing upstream/proxy headers, and verbatim API-key
> echo; the **cache-correctness** probe (high-temperature byte-identical output) ships
> as part of the `determinism` detector under the **protocol** dimension (it is
> suppressed for reasoning models that legitimately ignore sampling). **Roadmap /
> not yet implemented:** cross-user prefix-cache leak (with honeytoken),
> injected-system-prompt leak, and response/tool-call tampering diffs. Treat the
> roadmap probes below as design intent, not current behavior.

**Catches / 命中的伎俩.** `privacy.prompt-logging-leakage` (prompts stored or leaked
via cross-user cache sharing), `cache.ignore-temperature` (caches by prompt only and
serves a stored completion even at `temperature>0`), `prompt.injected-system-prompt`
(silently prepends/appends its own instructions), and `integrity.response-tampering`
(MITM rewrites response content or tool-call arguments — e.g. typosquatting an install
URL/package name).

**Black-box technique / 黑盒技术.**
- **Cache-correctness** — send the same open-ended high-entropy prompt K times at
  `temperature=1.0` (no seed); byte-identical completions across all K combined with
  collapsed/bimodal TTFT → a sampling-ignoring cache. Inverse check at
  `temperature=0`/fixed seed expects near-identical output.
- **Cross-user prefix-cache leak** — from identity A send a long unique secret prefix;
  from a *distinct* identity B send the same prefix for the first time and compare B's
  TTFT to a random-prefix control across many reps. A bimodal TTFT drop → shared prefix
  cache → prompts retained/shared. Optionally embed a honeytoken.
- **Injected system prompt** — instruction-leak probe ("repeat verbatim everything
  above this line"), behavioral delta vs baseline with no system message, and the
  `prompt_tokens` overcount tell from billing.
- **Response/tool-call tampering** — differential integrity probing: send maximally-
  constrained deterministic prompts (and tools with fully-determined arguments,
  including trigger keywords like `pip install`, `curl`) to both relay and trusted
  provider, and diff byte-for-byte for any URL/package/string substitution.

`pure_code_detectable` for timing/diff; the injected-prompt sub-check benefits from a
judge.

**What counts as a finding / 何为发现.** Identical high-temp outputs + TTFT collapse →
temperature-ignoring cache. A bimodal hit-vs-miss TTFT separation across distinct
identities → shared-cache / retention risk. A leaked preamble *plus* a `prompt_tokens`
overcount (≥2 independent indicators) → injected-system-prompt risk. Any value
substitution in a determined tool argument / canary-anchored output → response-tampering
risk.

**False-positive caveats / 误报与确认.** Prefix caching is a *legitimate* optimization
and same-account warming or a warm GPU can lower TTFT without cross-user leakage — use
genuinely distinct accounts/keys, high-entropy secrets, and many reps with statistical
hit/miss comparison. Models confabulate fake "system prompts" when asked to leak, and
refusal-policy differences may reflect the underlying model, not an injection — require
≥2 independent indicators. Sampling nondeterminism makes byte-diffs noisy unless
`temperature=0` with constrained outputs; legitimate dialect translation reshapes
tool-call JSON — distinguish benign *structural reshaping* from *value substitution*.
**Crucially: black-box logging itself is UNPROVABLE from the client**, and conditional
tampering can evade finite probing — surface these as documented limitations, not
proof. Absence of a timing signal does **not** prove prompts aren't logged.

---

**security — 隐私、缓存与完整性（中文）**

**命中的伎俩.** `privacy.prompt-logging-leakage`（prompt 被存储，或经跨用户缓存共享
而泄漏）、`cache.ignore-temperature`（只按 prompt 缓存，即便 `temperature>0` 也返回
存储的旧补全）、`prompt.injected-system-prompt`（悄悄前置 / 追加自有指令）、以及
`integrity.response-tampering`（中间人重写响应内容或工具调用参数——例如把安装 URL /
包名做 typosquat）。

**黑盒技术.**
- **缓存正确性** —— 用同一个开放式高熵 prompt 在 `temperature=1.0`（无 seed）下发
  K 次；K 次结果逐字相同且 TTFT 坍缩 / 双峰 → 忽略采样参数的缓存。反向检查
  在 `temperature=0`/固定 seed 下应近似一致。
- **跨用户前缀缓存泄漏** —— 用身份 A 发一段长且唯一的秘密前缀；用**另一个**身份 B
  首次发送相同前缀，在多次重复中把 B 的 TTFT 与随机前缀对照组对比。双峰式 TTFT
  下降 → 共享前缀缓存 → prompt 被保留 / 共享。可选地埋入 honeytoken。
- **注入系统提示** —— 指令泄漏探针（"逐字重复本行以上的所有内容"）、与无系统消息
  基线的行为差异、以及来自 billing 的 `prompt_tokens` 多计信号。
- **响应 / 工具调用篡改** —— 差分完整性探测：把高度受限的确定性 prompt（以及参数
  完全确定、含 `pip install`、`curl` 等触发关键词的工具）同时打到中转站与可信厂商，
  逐字节比对，查看是否有 URL / 包名 / 字符串被替换。

时序 / 差分部分属 `pure_code_detectable`；注入提示子检查可借助裁判模型。

**何为发现.** 高温下输出逐字相同 + TTFT 坍缩 → 忽略温度的缓存。跨不同身份出现
命中 vs 未命中的双峰 TTFT 分离 → 共享缓存 / 留存风险。泄漏的前置内容**加上**
`prompt_tokens` 多计（≥2 个独立指标）→ 注入系统提示风险。确定性工具参数 /
canary 锚定输出中出现任何取值替换 → 响应篡改风险。

**误报与确认.** 前缀缓存是**正当**优化，同账户预热或热 GPU 也能降低 TTFT 而非
跨用户泄漏——要用真正不同的账户 / 密钥、高熵秘密、以及多次重复做命中 / 未命中的
统计对比。被要求泄漏时模型会编造假的"系统提示"，拒答策略差异也可能源自底层模型
而非注入——需 ≥2 个独立指标。采样非确定性会让字节差分变噪，除非 `temperature=0`
且输出受限；合法方言转换会重塑 tool-call JSON——要区分良性的**结构重塑**与
**取值替换**。**关键：黑盒下无法证明"是否记录日志"本身**，且条件式篡改可以躲过
有限探测——这些只能作为已记录的局限说明，而非证据。没有时序信号**并不能**证明
prompt 没被记录。

---

## Trick → Detector mapping / 16 个伎俩到检测器的对照

The 16 relay tricks from the research map onto zing's dimensions as follows.
"Confirm with" notes the recommended verification path.

研究中的 16 个伎俩与 zing 维度的对应如下。"确认手段"给出推荐的核实路径。

| # | Trick / 伎俩 (id) | Severity | Dimension(s) | Confirm with / 确认手段 |
|---|---|---|---|---|
| 1 | `downgrade.silent-substitution` | critical | model_identity | compare mode + fingerprint + capability gap, large N |
| 2 | `downgrade.reasoning-collapse` | critical | model_identity | difficulty-graded set + reasoning-token/latency vs same-family reasoner baseline |
| 3 | `downgrade.quantized-distilled` | high | model_identity (+ capability) | accuracy-under-load on hard tasks + distribution test; treat as "fidelity risk" |
| 4 | `downgrade.partial-probabilistic-routing` | high | model_identity | randomized high-volume paraphrased probes; test for bimodal mixture |
| 5 | `context.window-truncation` | high | context_window | edge-needle binary search; effective/advertised ratio |
| 6 | `context.lost-in-middle-rag` | high | context_window | multi-needle + NoLiMa non-literal needles; baseline same prompts |
| 7 | `stream.fake-streaming` | medium | streaming | inter-chunk timing; compare TTFT/total vs known-streaming baseline |
| 8 | `billing.usage-inflation` | high | billing | tokenizer audit; fit reported=a·computed+b; flag slope/intercept |
| 9 | `billing.missing-usage` | medium | billing (+ protocol) | usage presence + arithmetic consistency; `include_usage` |
| 10 | `privacy.prompt-logging-leakage` _(roadmap)_ | high | security | cross-user prefix-cache timing (distinct keys); logging itself unprovable |
| 11 | `infra.shared-upstream-key` _(roadmap)_ | high | reliability | quota-decrement-while-idle, premature 429, leaked upstream request-ids |
| 12 | `cache.ignore-temperature` | medium | protocol (determinism) | identical high-temp outputs + TTFT collapse; suppressed for reasoning models |
| 13 | `prompt.injected-system-prompt` _(roadmap)_ | medium | security (+ billing) | leak probe + prompt_tokens overcount + baseline delta (≥2 indicators) |
| 14 | `throttle.rate-limit-quality` | medium | reliability | 429s bucketed out of the success rate; longitudinal/load-conditioned probing is roadmap |
| 15 | `capability.json-tool-fakery` | medium | capability (+ protocol) | adversarial schema / forced tool over many trials; measure rates |
| 16 | `integrity.response-tampering` _(roadmap)_ | critical | security | differential byte-diff vs trusted provider on constrained/forced outputs |

**Two-method principle / 双方法原则.** For the headline 货不对板 question, zing
deliberately combines **identity fingerprinting** (catches outright substitution)
with **distributional/behavioral testing** (catches quantization and weight edits
that keep the same identity). Each catches what the other misses.

对核心的"货不对板"问题，zing 刻意把**身份指纹**（命中赤裸裸的替换）与**分布 /
行为检验**（命中保持同一身份的量化与权重编辑）结合起来，各自补足对方的盲区。

---

## Limitations & responsible use / 局限与负责任使用

**zing reports divergence and risk, not proof of fraud.** This boundary is built
into the tool: the verdict uses cautious language (CLEAN / LOW / MEDIUM / HIGH /
INCONCLUSIVE), keeps ambiguous results explicitly **inconclusive** rather than
forcing pass/fail, and escalates to HIGH only on hard, high-severity evidence.

**zing 报告偏离与风险，而非欺诈的证据。** 这条边界内建于工具之中：判定使用谨慎
措辞（CLEAN / LOW / MEDIUM / HIGH / INCONCLUSIVE），将含糊结果明确保留为
**不确定 (inconclusive)** 而非强行 pass/fail，且只有在确凿的高严重度证据下才升级
为 HIGH。

**What black-box auditing fundamentally cannot prove / 黑盒审计本质上无法证明的事:**

- **Prompt logging / data retention** is unprovable from the client. A cross-user
  cache-leak timing signal is *evidence of shared caching*; its **absence does not
  prove** prompts aren't logged. 客户端无法证明是否记录日志；没有缓存泄漏信号
  **不代表** prompt 未被记录。
- **Response/tool-call integrity** cannot be guaranteed absent provider-signed
  response envelopes; **conditional** tampering (only on certain keywords, after a
  warm-up, or only for some clients) can evade finite probing. 缺少厂商签名的响应
  封装时无法保证完整性；条件式篡改可躲过有限探测。
- **Shared-key / key theft** cannot be proven — zing flags shared-pool *risk*, not a
  stolen key. 无法证明密钥被盗，只能标记共享池风险。
- **Benign drift vs substitution.** Official APIs silently update/fine-tune snapshots,
  so output divergence can be legitimate. 官方 API 会悄悄更新快照，偏离可能是良性的。

**Methodological discipline / 方法纪律:**

- Use `temperature=0` and deterministic, constrained probes wherever a finding hinges
  on exact comparison; use long outputs and large N for distribution/timing tests.
  在依赖精确比对处用 `temperature=0` 与受限确定性探针；分布 / 时序检验用长输出与
  大样本。
- Prefer **compare mode** against a genuinely trusted baseline of the **exact claimed
  snapshot** — it is the single strongest way to separate intrinsic model behavior
  from relay behavior, and is required for a high-confidence verdict. 优先使用
  **对比模式**，对照一个真正可信、与**所声称快照完全一致**的基线——这是区分模型
  固有行为与中转站行为最有力的手段，也是高置信度判定的前提。
- Assume the relay may be **test-aware / adaptive**: rotate and paraphrase probes,
  avoid fixed canary strings the relay can pattern-match, vary timing, headers, and
  keys, and run beyond any plausible warm-up window. 假设中转站可能**感知测试 /
  自适应**：轮换与改写探针、避免固定 canary、变化时序 / 头 / 密钥、并跑过任何合理
  的预热窗口。
- Record reproducible evidence — exact inputs, decoding params, seed, sample size,
  timestamps, raw per-probe outputs — so any finding is independently checkable. 记录
  可复现证据（精确输入、解码参数、seed、样本量、时间戳、每个探针的原始输出），
  使任何发现都可被独立复核。

**Responsible disclosure / 负责任披露.** Do **not** publicly accuse a vendor of
fraud on the basis of a zing report. zing surfaces black-box evidence of divergence;
treating it as a definitive accusation is both methodologically and ethically wrong.
Before acting: re-run with larger N and across time windows, confirm with compare
mode, rule out benign drift/network/load explanations, and — if a serious concern
remains — raise it privately with the vendor first, framed as questions about
observed behavior, not allegations.

**负责任披露.** **切勿**仅凭一份 zing 报告就公开指控厂商欺诈。zing 呈现的是黑盒
偏离证据；将其当作定论式指控，在方法论与伦理上都是错误的。行动前请：加大样本量、
跨时间窗口重跑、用对比模式确认、排除良性漂移 / 网络 / 负载等解释；若严重疑虑仍在，
先以"对观察到的行为提出疑问"而非"指控"的方式私下向厂商反馈。
