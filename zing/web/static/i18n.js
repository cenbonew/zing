/* zing web UI — Simplified-Chinese localization of audit findings.
 *
 * Plain browser global, no modules. Exposes window.ZING_I18N with:
 *   - FINDINGS:   catalog id -> { title, tpl }
 *   - localizeFinding(id, fallbackSummary, evidence) -> { title, summary }
 *   - RISK_LEVEL, STATUS, SEVERITY, CONFIDENCE: enum -> zh label
 *   - DIMENSIONS: dimension id -> [zhName, zhDesc]
 *
 * Template rule: `tpl` may contain {placeholders} naming keys from THAT finding's
 * evidence dict. Placeholders are only used for keys confirmed to exist in the
 * detector source. If any referenced key is missing/undefined at fill time, the
 * English fallbackSummary is kept instead of emitting a broken template.
 *
 * Technical terms (token, JSON, HTTPS, finish_reason, TTFT, p95, CV, usage) are
 * intentionally left as-is.
 */
(function () {
  "use strict";

  // ---- Enum label maps ------------------------------------------------- //
  var RISK_LEVEL = {
    clean: "一致（疑似真货）",
    low: "基本可信",
    medium: "存在偏离",
    high: "货不对板",
    inconclusive: "信号不足",
  };

  var STATUS = {
    pass: "通过",
    warn: "警告",
    fail: "失败",
    info: "信息",
    inconclusive: "不确定",
    not_run: "未运行",
    error: "错误",
  };

  var SEVERITY = {
    info: "提示",
    low: "低",
    medium: "中",
    high: "高",
    critical: "严重",
  };

  var CONFIDENCE = { low: "低", medium: "中", high: "高" };

  // dimension id -> [zh 名称, zh 一句话说明]
  var DIMENSIONS = {
    connectivity: ["连通性", "端点是否可达、基础对话是否正常"],
    protocol: ["协议兼容", "是否符合 OpenAI 接口规范"],
    context_window: ["上下文窗口", "长文是否真能记住、有无静默截断"],
    model_identity: ["模型身份", "它是不是它自称的那个模型"],
    capability: ["能力声明", "工具调用 / JSON 模式是否真支持"],
    streaming: ["流式真实", "是真流式还是缓冲后伪装"],
    billing: ["计费用量", "token 用量有没有虚报"],
    reliability: ["并发可靠", "压力下的成功率与延迟"],
    security: ["传输安全", "传输加密与密钥处理"],
  };

  // ---- Finding catalog -------------------------------------------------- //
  // For each id: zh title + zh summary template. Placeholders reference keys
  // verified in the finding's evidence dict in zing/detectors/*.py.
  var FINDINGS = {
    // -- connectivity ----------------------------------------------------- //
    "connectivity.models": {
      title: "/v1/models 列表",
      tpl: "已列出 {model_count} 个模型；所声称的模型是否在列表中：{claimed_listed}。",
    },
    "connectivity.chat": {
      title: "基础对话补全",
      tpl: "返回了内容，耗时 {duration_ms} ms；返回模型为 {model_returned}。",
    },

    // -- protocol --------------------------------------------------------- //
    "protocol.multi_turn": {
      title: "多轮对话记忆",
      tpl: "多轮对话上下文检查；是否回忆起此前提到的颜色：{recalled_blue}。",
    },
    "protocol.stop": {
      title: "stop 截断序列处理",
      tpl: "stop 序列处理检查；finish_reason={finish_reason}。",
    },
    "protocol.shape": {
      title: "响应结构（信封）规范性",
      tpl: "finish_reason={finish_reason}；usage 是否完整：{has_usage}。",
    },
    "protocol.error_schema": {
      title: "错误响应结构",
      tpl: "非法请求返回 HTTP {status_code}；错误体是否为 OpenAI 风格：{conforming_error_body}。",
    },

    // -- context_window --------------------------------------------------- //
    "context_window.no_ladder": {
      title: "上下文窗口探测无法运行",
      tpl: "在下限与上限之间没有可用的探测尺寸（声称 {declared}，上限 {upper}，下限 {floor}）。",
    },
    "context_window.lost_in_middle": {
      title: "中段的针未被回忆（lost-in-the-middle）",
      tpl: "在 ~{mid_size} token 的提示中，开头与结尾被回忆但中段缺失（疑似廉价 RAG/摘要替身）。",
    },
    "context_window.rejected_below_claim": {
      title: "在声称窗口之内即拒绝长上下文",
      tpl: "一个 ~{rejected_at} token 的提示因上下文超长被拒，远低于声称的 {declared} token 窗口。",
    },
    "context_window.no_recall": {
      title: "没有任何尺寸能回忆出针",
      tpl: "连 ~{smallest_probed} token 的提示都无法回忆；可用窗口远小于声称的 {declared}。",
    },
    "context_window.truncation": {
      title: "真实上下文窗口远小于声称值",
      tpl: "真实上下文窗口 ~{effective_window} << 声称的 {declared}（占比 {ratio}，疑似静默截断）。",
    },
    "context_window.short": {
      title: "真实上下文窗口低于声称值",
      tpl: "真实上下文窗口 ~{effective_window} 低于声称的 {declared}（占比 {ratio}，在到达上限前回忆已退化）。",
    },
    "context_window.consistent": {
      title: "实测上下文窗口与声称一致",
      tpl: "在 ~{effective_window} token 处仍能回忆出针，接近声称的 {declared}（占比 {ratio}）。",
    },
    "context_window.measured": {
      title: "实测有效上下文窗口",
      tpl: "无声称窗口可对比；实测有效窗口 ~{effective_window} token。",
    },

    // -- model_identity --------------------------------------------------- //
    "model_identity.no_profile": {
      title: "所声称的模型不在知识库中",
      tpl: "知识库中未找到所声称的模型（claimed={claimed_model}，requested={requested_model}）；请传入 --declared-provider 或补充 KB 档案。",
    },
    "model_identity.self_id": {
      // status-dependent (consistent / rival / both / evasive / unavailable);
      // a single safe template keyed on the requested/returned identity context.
      title: "自我身份识别",
      tpl: "自我身份识别检查（claimed={claimed}）。",
    },
    "model_identity.model_field": {
      title: "回显的 model 字段",
      tpl: "响应中的 model 字段为 {returned}，请求的是 {requested}。",
    },
    "model_identity.fp_aggregate": {
      title: "多项行为指纹同时偏离",
      tpl: "{diverged} 等多项纯代码指纹偏离了原生行为；请排查是否存在替换。",
    },
    // model_identity.fp.<id> handled dynamically in localizeFinding.

    // -- capability ------------------------------------------------------- //
    "capability.tools": {
      // status-dependent (delivered / claimed-not-delivered / none / failed)
      title: "工具调用（function calling）",
      tpl: "工具调用探测；finish_reason={finish_reason}。",
    },
    "capability.tools.encoding": {
      title: "非 OpenAI 风格的工具参数编码",
      tpl: "function.arguments 返回为结构化对象（{arguments_type}）而非 JSON 字符串；真正的 OpenAI 兼容接口应返回字符串（疑似替身引擎）。",
    },
    "capability.json_mode": {
      title: "JSON 模式（json_object）",
      tpl: "JSON 模式探测；是否解析出对象：{parsed}，请求值是否匹配：{value_matched}。",
    },
    "capability.json_schema": {
      // status-dependent (honored / not enforced / over-delivered / failed)
      title: "严格 JSON Schema",
      tpl: "严格 JSON schema 探测；是否符合 schema：{conforms}。",
    },
    "capability.max_output": {
      // status-dependent (sustained / stopped early / plausible / failed)
      title: "最大输出长度",
      tpl: "最大输出探测：声称 max_output={declared_max_output}，请求 {requested_max_tokens} token；产出约 {line_count} 行，finish_reason={finish_reason}。",
    },

    // -- streaming -------------------------------------------------------- //
    "streaming.failed": {
      title: "流式请求失败",
      tpl: "流式请求失败（HTTP {status_code}，类型 {error_type}）；请确认该模型支持 stream=true。",
    },
    "streaming.few_chunks": {
      title: "响应仅用 ≤2 个分片送达",
      tpl: "{content_chars} 字符仅用 {chunk_count} 个分片送达（先缓冲再切片，并非真正的逐 token 流式）。",
    },
    "streaming.late_ttft": {
      title: "首 token 在流的末尾才到达",
      tpl: "首 token 直到约总时长的 {ttft_ratio} 才到达（ttft={ttft_ms} ms / duration={duration_ms} ms，疑似缓冲）。",
    },
    "streaming.uniform_gaps": {
      title: "分片间隔均匀且接近零",
      tpl: "{chunk_count} 个分片，平均间隔 ~{mean_delta_ms} ms、CV {delta_cv} —— 分片像是被一次性倾倒。",
    },
    "streaming.no_usage": {
      title: "流中没有 usage 分片",
      tpl: "stream_options.include_usage 未在流中返回任何 usage 数据。",
    },
    "streaming.healthy": {
      title: "流式表现真实",
      tpl: "共 {chunk_count} 个分片，耗时 {duration_ms} ms，首 token 于 {ttft_ms} ms 到达。",
    },

    // -- billing ---------------------------------------------------------- //
    "billing.request-failed": {
      title: "计费探测请求失败",
      tpl: "计费探测请求失败（HTTP {status_code}，类型 {error_type}）。",
    },
    "billing.missing-usage": {
      title: "未返回任何用量统计",
      tpl: "响应未包含 token usage；计费无法独立核验（usage 是否存在：{usage_present}）。",
    },
    "billing.usage-inflation": {
      title: "上报的 prompt token 远超估算",
      tpl: "上报 prompt token（{reported_prompt}）远超独立估算（~{estimated_prompt}，比值 {ratio}）；疑似按 token 多计费。",
    },
    "billing.reasoning-tokens": {
      title: "completion token 超过可见文本（推理模型）",
      tpl: "上报 completion token（{reported_completion}）超过可见文本估算（~{visible_estimate}，比值 {ratio}）；推理模型含隐藏思考 token，属预期，不视为虚报。",
    },
    "billing.usage-inflation-completion": {
      // FAIL (exact tokenizer) or WARN (heuristic tokenizer)
      title: "上报的 completion token 远超估算",
      tpl: "上报 completion token（{reported_completion}）远超独立估算（~{estimated_completion}，比值 {ratio}）；疑似按 token 多计费。",
    },
    // billing.usage-undercount-prompt / -completion handled dynamically below
    "billing.usage-undercount-prompt": {
      title: "上报的 prompt token 明显低于估算",
      tpl: "上报 prompt token（{reported_prompt}）远低于估算（~{estimated_prompt}）；对买方无害但属异常。",
    },
    "billing.usage-undercount-completion": {
      title: "上报的 completion token 明显低于估算",
      tpl: "上报 completion token（{reported_completion}）远低于估算（~{estimated_completion}）；对买方无害但属异常。",
    },
    "billing.total-mismatch": {
      title: "usage 总数 ≠ prompt + completion",
      tpl: "上报 total（{total}）不等于 prompt（{prompt}）+ completion（{completion}）。",
    },
    "billing.partial-usage": {
      title: "用量明细不完整",
      tpl: "usage 仅给出 total={total}（prompt={prompt}，completion={completion}）；按 token 计费所依赖的分项缺失、无法核验。",
    },
    "billing.usage-consistent": {
      title: "用量与独立估算一致",
      tpl: "上报用量在估算容差范围内（prompt 比值 {ratio_prompt}，completion 比值 {ratio_completion}）。",
    },

    // -- reliability ------------------------------------------------------ //
    "reliability.skipped": {
      title: "可靠性探测已禁用",
      tpl: "reliability_requests <= 0（{reliability_requests}）；未发起任何并发压力。",
    },
    "reliability.success_rate": {
      // status-dependent (all 429 / all ok / some failed)
      title: "并发请求成功率",
      tpl: "并发 {concurrency} 下，{successes} 个请求成功（共 {requests} 个，{rate_limited} 个被限流）。",
    },
    "reliability.rate_limited": {
      title: "部分并发请求被限流",
      tpl: "并发 {concurrency} 下有 {rate_limited}/{requests} 个请求返回 HTTP 429；计为限流而非不稳定。",
    },
    "reliability.latency": {
      title: "压力下尾延迟偏高",
      tpl: "并发 {concurrency} 下尾延迟偏高，超过阈值。",
    },

    // -- security --------------------------------------------------------- //
    "security.tls": {
      // PASS (https) or FAIL (not https)
      title: "传输是否使用 HTTPS",
      tpl: "传输协议：{scheme}。",
    },
    "security.headers": {
      // status-dependent (leaks / none / no headers)
      title: "上游/代理响应头",
      tpl: "响应头检查（HTTP {status_code}）。",
    },
    "security.key_echo": {
      // PASS or FAIL
      title: "响应是否回显 API key",
      tpl: "API key 回显检查。",
    },
    "security.note": {
      title: "黑盒检测的局限",
      tpl: "是否记录提示词、是否复用共享上游 key 无法从客户端侧黑盒证实；可将可靠性与流式时序维度视为间接信号。",
    },

    // -- injected_prompt -------------------------------------------------- //
    "injected_prompt.suspected": {
      title: "疑似被注入隐藏系统提示词",
      tpl: "上报 prompt token 带有较大的固定额外开销且随消息长度保持恒定，同时模型在被问及时泄漏了指令式内容；疑似被预置了隐藏系统提示词。",
    },
    "injected_prompt.overhead": {
      title: "异常的固定输入 token 开销",
      tpl: "prompt token 高于估算且随消息增长保持恒定 —— 与隐藏的预置提示词一致，但仅为单一信号，需佐证。",
    },
    "injected_prompt.leak": {
      title: "模型回出了指令式前言（弱信号）",
      tpl: "被要求复述此前指令时，模型返回了指令式文本而非 NONE。模型可能臆造，单独看属弱信号。",
    },
    "injected_prompt.inconclusive": {
      title: "无法测量输入 token 开销",
      tpl: "响应中没有可用的 prompt_tokens；已跳过开销检查。",
    },
    "injected_prompt.clean": {
      title: "未见注入系统提示词的迹象",
      tpl: "输入 token 开销很小（属模板级别），也未泄漏任何前言。",
    },

    // -- integrity -------------------------------------------------------- //
    "integrity.tampering": {
      title: "检测到由中继控制的值替换",
      tpl: "已知答案的探针返回时结构保留但值被替换（是否经基线佐证：{baseline_corroborated}）；代理改写 URL/包名属供应链风险，应停止使用。",
    },
    "integrity.intact": {
      title: "已知答案探针原样返回",
      tpl: "{verbatim} 个探针值被逐字回显；未观察到替换。",
    },
    "integrity.inconclusive": {
      title: "完整性探针结果不确定",
      tpl: "模型未逐字回显任何探针，无法评估是否被篡改。",
    },

    // -- determinism (dimension: protocol) -------------------------------- //
    "determinism.temp1_variability": {
      // status-dependent (caching WARN / reasoning INFO / varies PASS / inconclusive)
      title: "temperature=1.0 下的输出变异性",
      tpl: "在 temperature=1.0 下采样了 {samples} 次；是否字节完全相同：{identical}。",
    },
    "determinism.temp0_stability": {
      title: "temperature=0 下的答案稳定性",
      tpl: "在 temperature=0 下重复同一事实性提示；两次是否相同：{identical}。",
    },

    // -- prompt_cache ----------------------------------------------------- //
    "prompt_cache.inconclusive": {
      title: "前缀缓存时序数据不可用",
      tpl: "一个或多个流式探针未返回可用时序（cold={cold_ms} ms，warm={warm_ms} ms，control={control_ms} ms）。",
    },
    "prompt_cache.detected": {
      title: "中继按提示前缀做缓存",
      tpl: "重复前缀返回明显更快（warm ~{warm_ms} ms vs cold ~{cold_ms} ms，control ~{control_ms} ms）—— 存在提示前缀缓存。这是合法优化；zing 无法用单一 key 证明是否跨用户共享缓存。",
    },
    "prompt_cache.none": {
      title: "未见明显的提示前缀缓存",
      tpl: "重复前缀的 TTFT（~{warm_ms} ms）并未明显快于 cold（~{cold_ms} ms）/ control（~{control_ms} ms）。",
    },

    // -- vision (dimension: capability) ----------------------------------- //
    "vision.not_claimed": {
      title: "未声称视觉能力 —— 已跳过",
      tpl: "解析出的档案未声明视觉/图像模态；未发送多模态探针（modalities={modalities}）。",
    },
    "vision.color": {
      // status-dependent (PASS delivered / WARN not delivered / INCONCLUSIVE);
      // a single safe template keyed only on the protocol, which is always
      // present in every branch's evidence.
      title: "多模态（视觉）能力核验",
      tpl: "通过已知答案的纯色图片探针核验视觉能力（协议 {protocol}）。",
    },

    // -- quality_judge (dimension: model_identity) ------------------------ //
    "quality_judge.unavailable": {
      title: "没有可供评判的目标答案",
      tpl: "目标端对任何区分性探针都未返回可用内容（探针 {probes} 个，失败 {failed} 个）。",
    },
    "quality_judge.suspicious": {
      title: "LLM 评委：行为与所声称的模型不符（疑似降级/替换/量化）",
      tpl: "评委（置信度 {judge_confidence}）认为答案不像所声称的模型；可能更接近：{likely_actual_tier}。是否经基线佐证：{baseline_corroborated}。",
    },
    "quality_judge.consistent": {
      title: "LLM 评委：行为与所声称的模型一致",
      tpl: "评委（置信度 {judge_confidence}）认为答案与所声称的模型一致。",
    },
    "quality_judge.inconclusive": {
      title: "LLM 评委：评估结果不确定",
      tpl: "评委无法给出确定结论（置信度 {judge_confidence}）。",
    },

    // -- embeddings (non-chat surface: zing/embed_audit.py) ----------------- //
    "embed.connectivity": {
      // status-dependent (PASS reachable / ERROR unreachable or malformed);
      // a single safe template keyed on the always-present dimension/vector count.
      title: "嵌入端点连通性",
      tpl: "返回 {vectors} 个向量，维度 {dimension}。",
    },
    "embed.dimension": {
      // status-dependent (PASS match / FAIL mismatch / INFO unknown claim)
      title: "向量维度是否与所声称模型一致",
      tpl: "返回 {returned} 维向量，所声称模型应为 {claimed} 维。",
    },
    "embed.determinism": {
      // PASS (cosine ~1) or WARN (drifts below threshold)
      title: "嵌入是否确定性可复现",
      tpl: "同一输入两次的余弦相似度为 {cosine}（阈值 {threshold}）。",
    },
    "embed.distinctness": {
      // PASS (distinct) or WARN (near-identical = degenerate backend)
      title: "不同输入是否产生可区分的向量",
      tpl: "两段无关文本的余弦相似度为 {cosine}（阈值 {threshold}）。",
    },
    "embed.model_field": {
      title: "回显的 model 字段",
      tpl: "中继上报的模型为 {model_returned}，请求的是 {model_requested}，所声称为 {claimed_model}。",
    },

    // -- rerank (non-chat surface: known-answer probe) --------------------- //
    "rerank.connectivity": {
      // status-dependent (PASS reachable / ERROR unreachable or empty);
      // keyed on the result count present in the reachable branch.
      title: "重排端点连通性",
      tpl: "返回 {results} 条排序结果。",
    },
    "rerank.known_answer": {
      // PASS (obvious answer ranked first) or WARN (it was not)
      title: "是否把显而易见的答案排在首位",
      tpl: "排在首位的是文档 {top_index}，预期应为文档 {expected}（排名顺序 {ranking}）。",
    },
  };

  // Generic fallback title for unknown finding ids.
  var GENERIC_TITLE = "检测项";

  // ---- Template filling ------------------------------------------------- //
  // Replace {key} from evidence. If ANY referenced key is missing/undefined,
  // signal failure so the caller keeps the English fallback summary.
  function fillTemplate(tpl, evidence) {
    if (!tpl) return null;
    var ev = evidence || {};
    var ok = true;
    var out = tpl.replace(/\{([a-zA-Z0-9_]+)\}/g, function (_m, key) {
      if (!Object.prototype.hasOwnProperty.call(ev, key)) {
        ok = false;
        return "";
      }
      var v = ev[key];
      if (v === undefined || v === null) {
        ok = false;
        return "";
      }
      if (typeof v === "boolean") return v ? "是" : "否";
      if (typeof v === "number") {
        // Trim noisy floats; keep ints intact.
        return Number.isInteger(v) ? String(v) : String(Math.round(v * 1000) / 1000);
      }
      if (typeof v === "object") {
        try {
          return JSON.stringify(v);
        } catch (e) {
          ok = false;
          return "";
        }
      }
      return String(v);
    });
    return ok ? out : null;
  }

  // Resolve a catalog entry, including dynamic id families.
  function lookup(id) {
    if (!id) return null;
    if (Object.prototype.hasOwnProperty.call(FINDINGS, id)) return FINDINGS[id];
    // Per-fingerprint identity findings: model_identity.fp.<probe-id>
    if (id.indexOf("model_identity.fp.") === 0) {
      return {
        title: "行为指纹检查",
        tpl: "针对 {probe} 的纯代码行为指纹检查。",
      };
    }
    return null;
  }

  /**
   * localizeFinding(id, fallbackSummary, evidence) -> { title, summary }
   *  - title:   zh title from the catalog, or a generic zh title if unknown.
   *  - summary: filled zh template; if the id is unknown OR a template
   *             placeholder is missing, the original English fallbackSummary
   *             is returned instead of a broken string.
   */
  function localizeFinding(id, fallbackSummary, evidence) {
    var entry = lookup(id);
    if (!entry) {
      return { title: GENERIC_TITLE, summary: fallbackSummary || "" };
    }
    var summary = fillTemplate(entry.tpl, evidence);
    return {
      title: entry.title || GENERIC_TITLE,
      summary: summary != null ? summary : fallbackSummary || "",
    };
  }

  window.ZING_I18N = {
    FINDINGS: FINDINGS,
    localizeFinding: localizeFinding,
    RISK_LEVEL: RISK_LEVEL,
    STATUS: STATUS,
    SEVERITY: SEVERITY,
    CONFIDENCE: CONFIDENCE,
    DIMENSIONS: DIMENSIONS,
  };
})();
