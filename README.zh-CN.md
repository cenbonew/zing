# zing — 大模型中转站「货不对板」检测工具

> [English](README.md) · **中文**

[![CI](https://github.com/cenbonew/zing/actions/workflows/ci.yml/badge.svg)](https://github.com/cenbonew/zing/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)

**zing** 是一个本地优先（local-first）的命令行工具，用于检测一个 OpenAI 兼容的
API 中转站 / 代理 / 转售商提供的，**是否真的是它声称的那个模型**——还是悄悄换成了
更便宜的模型、截断了上下文窗口、伪造了流式输出，或者虚报了 token 计费（即「**货不对板**」）。

你只需告诉 zing 中转站的接口地址和它声称的模型，zing 就会运行一整套黑盒探测，
把观测到的行为与内置的**覆盖 7 大平台、85 个原生模型画像**的知识库逐项比对，
最后给出一个清晰、有证据支撑的结论——既适合人看，也能输出 JSON 给其它程序或大模型直接消费。

> zing 给出的是**「行为差异与风险」的黑盒证据，而非欺诈的密码学证明**。
> 请阅读 [负责任使用](#负责任使用)。

---

## 为什么需要它

中转 key 市场充斥着「GPT-4o 一折价」的报价。很多是诚实的，但有些不是——而且作弊手法肉眼很难识别：

- 你点名要 `gpt-4o`，实际被悄悄换成 `gpt-4o-mini` 或某个开源模型；
- 宣传 1M token 上下文，实际悄悄截断到 32K；
- 所谓「流式」其实是把整段回复缓存后再切片下发，毫无首字延迟优势；
- 上报的 `usage` token 数被夸大，你的余额烧得比应有的更快；
- 本该支持工具调用 / JSON 模式的模型，实际并不支持。

zing 把「感觉不太对」变成一份可复现的报告。

## 安装

需要 Python 3.10+。

```bash
# 从源码安装（PyPI 发布前）
git clone https://github.com/cenbonew/zing
cd zing
pip install -e .

# 或使用 uv
uv pip install -e .
```

可选：安装 `tokenizers` 附加项，让账单审计对 OpenAI 系模型做精确 token 计数：

```bash
pip install -e '.[tokenizers]'
```

## 快速开始

```bash
# 1) 按中转站声称的模型审计它（模型 id + 平台提示）
export ZING_API_KEY=sk-你的中转key
zing check \
  --base-url https://relay.example.com/v1 \
  --api-key env:ZING_API_KEY \
  --model gpt-4o \
  --suite standard

# 2) 最强检测：与同款模型的可信基线对比
export OPENAI_API_KEY=sk-你的官方key
zing compare \
  --target-base-url https://relay.example.com/v1 --target-api-key env:ZING_API_KEY --target-model gpt-4o \
  --baseline-base-url https://api.openai.com/v1 --baseline-api-key env:OPENAI_API_KEY --baseline-model gpt-4o \
  --suite deep

# 3) 查看内置知识库
zing kb            # 全部 85 个模型
zing kb deepseek   # 单个平台

# 4) 生成可提交的配置文件
zing init          # 生成 zing.yaml
zing check -c zing.yaml
```

### 作为给大模型 / Agent 使用的工具

加 `--json` 会把结构化报告打到标准输出（而非写文件），可直接喂给其它程序或模型：

```bash
zing check --base-url ... --model gpt-4o --json | jq .verdict
```

## 检测了什么

zing 对九个维度评分。其中最直接揭示「货不对板」的三项（模型身份、真实上下文窗口、能力声明）权重最高。

| 维度 | 能抓到什么 |
|---|---|
| **model_identity 模型身份** | 静默降级 / 替换——自我身份识别、知识截止、分词器指纹、回包里的 `model` 字段 |
| **context_window 上下文窗口** | 静默截断（宣称 1M，32K 就召回失败）、廉价 RAG/摘要垫片导致的「中段遗忘」；用大海捞针 + 二分搜索实测 |
| **capability 能力声明** | 工具调用 / JSON 模式 / json-schema / 最大输出等声称的能力是否真的兑现（或「过度兑现」，反向暗示是替身模型）|
| **billing 计费** | 通过独立分词估算，发现 token/usage 虚高，以及缺失/无法核验的用量统计 |
| **streaming 流式** | 通过分片数量与分片间隔时序，识别伪流式（缓存后切片）|
| **protocol 协议兼容** | OpenAI 兼容性一致性：多轮、停止序列、响应结构、错误结构；并含一项「确定性」子检查，识别忽略 temperature/seed 的响应缓存 |
| **reliability 可靠性** | 并发成功率与延迟（HTTP 429 限流单独计列，不计入失败）|
| **connectivity 连通性** | 端点可达性与声称的 `/v1/models` 列表 |
| **security 安全** | 传输（HTTPS）、响应头卫生、密钥回显 |

每项检测背后的技术、对应的中转作弊手法、以及误报注意事项，详见
[docs/METHODOLOGY.md](docs/METHODOLOGY.md)。

## 两种检测模式

- **纯代码（默认）**：所有可确定性判定的探测——指纹、上下文扫描、账单计算、流式时序。
  无需第二个模型，完全可复现。
- **代码 + LLM 融合（`--judge`）**：在此基础上，额外调用一个**可信的**裁判模型
  （单独配置，绝不用被测中转站本身）来评估纯代码无法判定的模糊信号，
  如回答质量、推理深度。`quality_judge` 检测器即由此驱动。

```bash
zing check --base-url ... --model gpt-4o --suite deep --judge \
  --judge-base-url https://api.openai.com/v1 --judge-api-key env:OPENAI_API_KEY --judge-model gpt-4o-mini
```

## 套件（suite）

| 套件 | 包含检测器 | 成本 |
|---|---|---|
| `smoke` | connectivity, security | 极低 |
| `standard` | + protocol, model_identity, capability, streaming, billing, reliability | 低–中 |
| `deep` | + context_window, determinism, quality_judge（加 `--judge` 时）| 较高（长上下文探测消耗 token）|
| `full` | 全部 | 最高 |

上下文窗口探测受 `--max-context-tokens`（默认 200K）约束，因此审计 1M 上下文的模型也能控制花费。

## 结论示例

```text
╭─ ✗ HIGH RISK — 有力证据表明该中转未按宣传提供所声称的模型 ───────────────────╮
│ Target : my-relay · model gpt-4o · provider openai                            │
│ Mode   : check · suite deep                                                   │
│ Score  : 53.5/100 (rating F) · confidence medium                             │
╰───────────────────────────────────────────────────────────────────────────────╯
  • 在 gpt-4o 名义下自我标识为竞品品牌（anthropic）
  • 真实上下文窗口约 8000 << 声称的 128000（疑似静默截断）
  • 上报的 prompt token 数远超独立估算
```

报告以 JSON、Markdown、HTML 三种格式写入 `reports/`。

## 知识库

平台画像位于 [`zing/knowledge/data/`](zing/knowledge/data)，是可编辑的 YAML——
每个平台一个文件（OpenAI、Anthropic、Google Gemini、DeepSeek、通义千问 Qwen、智谱 GLM、月之暗面 Moonshot）。
每个模型都带有原生上下文窗口、最大输出、分词器、能力标志、身份关键词与行为指纹。
无需 fork 即可新增或覆盖画像：

```bash
zing check --kb-dir ./my-profiles ...     # 或设置 ZING_KB_DIR
```

## 负责任使用

zing 是黑盒审计的辅助工具。它**无法证明**：

- 服务方是否存储或用于训练你的 prompt；
- 它是否始终路由到某个确切的模型（中转可能做概率性路由）；
- 超出独立 token 估算所能暗示范围之外的计费欺诈。

请将报告用于你自己的尽职调查。**不要仅凭单次运行就公开指控某个服务商**，
应先评估样本量、采样设置与当地法律。在得出强结论前，请先用 `zing compare`
与可信基线对比。

## 许可证

[Apache-2.0](LICENSE)
