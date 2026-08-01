(() => {
  "use strict";

  const bootstrapNode = document.getElementById("app-bootstrap");
  const bootstrap = JSON.parse(bootstrapNode.textContent || "{}");
  const app = document.getElementById("app");
  const nav = document.getElementById("app-nav");
  const main = document.getElementById("app-main");
  const toastRegion = document.getElementById("toast-region");
  const audioCache = new Map();
  const waveformRequests = new WeakMap();
  const deferredWaveforms = new WeakMap();
  const waveformObserver =
    "IntersectionObserver" in window
      ? new IntersectionObserver(
          (entries) => {
            entries.forEach((entry) => {
              if (!entry.isIntersecting) return;
              waveformObserver.unobserve(entry.target);
              const request = deferredWaveforms.get(entry.target);
              deferredWaveforms.delete(entry.target);
              if (request) drawWaveform(entry.target, request.url, request.options);
            });
          },
          { rootMargin: "160px" }
        )
      : null;

  const ICONS = {
    brand: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 15V9m4 9V6m4 14V4m4 13V7m4 8V9"/></svg>',
    projects:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/></svg>',
    overview:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="16" height="6" rx="1"/></svg>',
    blind:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 12h2l1.4-5 2.4 10 2.2-13 2.2 15 2.4-12 1.6 8 1.3-3H21"/></svg>',
    named:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 8h8M8 12h8M8 16h5"/></svg>',
    plan: '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="19" cy="18" r="2"/><path d="M7 12h4a4 4 0 0 0 4-4V6m0 12v-2a4 4 0 0 0-4-4"/></svg>',
    download:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 3v12m-4-4 4 4 4-4M5 20h14"/></svg>',
    arrow:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5"/></svg>',
    play: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8 5 11 7-11 7Z"/></svg>',
    pause:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 5v14M15 5v14"/></svg>',
    back: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14 6-6 6 6 6"/></svg>',
    forward:
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="m10 6 6 6-6 6"/></svg>',
  };

  const REASON_LABELS = {
    warmth_fullness: "温暖度与饱满度",
    hook_catchiness: "Hook 与抓耳度",
    vocal_timbre_identity: "主唱质感与身份",
    arrangement_harmony_development: "编曲与和声发展",
    lyric_delivery: "歌词表达",
    ending_completeness: "结尾完成度",
    overall_preference: "整体偏好",
  };

  const AXIS_LABELS = {
    compliance: ["需求匹配", "REQUIREMENT FIT"],
    craft: ["作品完成度", "COMPLETENESS"],
    release_readiness: ["直接发布可行性", "RELEASE READINESS"],
    distinctiveness: ["差异身份", "DISTINCT IDENTITY"],
  };

  const STATUS_LABELS = {
    pass: "通过",
    fail: "未通过",
    indeterminate: "需要人工确认",
    not_evaluated: "未采集",
    ready: "可直接发布",
    needs_suno_edit: "需要 Suno 内编辑",
    blocked: "暂不推荐",
    recommended: "建议发布",
    unique_survivor: "唯一通过候选",
    abstain: "暂不推荐",
    no_release_candidate: "没有可发布候选",
  };

  const CONFIDENCE_LABELS = {
    high: "高",
    medium: "中",
    low: "低",
    indeterminate: "待定",
  };

  const REQUIREMENT_LABELS = {
    "Captured generation lyrics and section order": "捕获的歌词与段落顺序",
    "Captured generation style intent": "捕获的生成风格意图",
    "Frozen lyrics must remain correct and intelligible":
      "冻结歌词必须正确且清晰可辨",
    "Declared style intent": "已声明的风格意图",
  };

  const ENDING_LABELS = {
    silence_tail: "静音尾段",
    natural_decay: "自然衰减",
    active_audio_at_boundary: "边界仍有活动音频",
    likely_abrupt_boundary: "疑似突然截断",
    indeterminate: "待确认",
  };

  const PRESERVATION_LABELS = {
    structural_gesture: "结构感觉",
    melody_rhythm: "旋律 / 节奏",
    exact_audio: "精确音频",
  };

  const PLAN_TEXT = new Map([
    [
      "Open the target song in Library or Create; choose More Actions, then Edit and Replace Section.",
      "在 Library 或 Create 打开目标歌曲，依次选择 More Actions、Edit、Replace Section。",
    ],
    [
      "Select from a clean boundary before the complete Bridge phrase through a clean boundary after the first complete Chorus phrase.",
      "从完整 Bridge 前的干净边界开始，选到第一段完整 Chorus 后的干净边界。",
    ],
    [
      "Keep the frozen lyrics unchanged and enter only the positive local transition direction.",
      "冻结歌词保持原文，只填写正向、局部的转场方向。",
    ],
    [
      "Confirm one batch of two versions; do not choose by title or order.",
      "确认生成一批两个版本；不要按标题或出现顺序选择。",
    ],
    [
      "Download or retain both Whole Song results and rerun this evaluator.",
      "下载或保留两个 Whole Song 结果，再回到本工具重新评估。",
    ],
    [
      "Stop after two batches; if neither passes, keep the target fallback.",
      "最多两批；若都未通过，停止生成并保留目标候选作为回退。",
    ],
    [
      "wrong, missing, reordered, or unintelligible frozen lyric",
      "冻结歌词错误、缺失、乱序或无法听清",
    ],
    ["mid-line hard cut or obvious edit seam", "句中硬切或明显编辑接缝"],
    [
      "unexpected vocalist or vocal-register identity change",
      "歌手或声区身份发生非预期变化",
    ],
    ["long instrumental detour before Chorus", "进入 Chorus 前出现过长器乐绕行"],
    [
      "new drums or percussion that violate the Brief",
      "新增鼓或打击乐违反 Brief",
    ],
    [
      "surrounding protected target material regresses",
      "替换区周围原本受保护的目标内容出现退化",
    ],
    [
      "User must explicitly initiate every generation; the tool never spends credits.",
      "每次生成都必须由你明确发起；本工具不会自动消耗 credits。",
    ],
    [
      "Replace Section requires a paid Pro or Premier subscription.",
      "Replace Section 需要 Pro 或 Premier 付费订阅。",
    ],
    [
      "The tool will not substitute a Sample workflow because it cannot enforce section-relative placement.",
      "工具不会改用 Sample 流程，因为它无法强制参考内容落在指定歌曲段落。",
    ],
    [
      "A 16-second Sample or audio upload cannot guarantee exact placement inside a new full song.",
      "16 秒 Sample 或音频上传无法保证它在新完整歌曲中的精确位置。",
    ],
    [
      "Without measured retention evidence, the tool will not relabel a structural resemblance as exact or melody-rhythm preservation.",
      "缺少测得的保留证据时，工具不会把结构相似改称为精确、旋律或节奏保留。",
    ],
    [
      "Studio availability does not by itself prove that a generative edit retained the requested audio identity.",
      "拥有 Studio 本身不能证明生成式编辑保留了要求的音频身份。",
    ],
    [
      "Studio-only multitrack operations are unavailable on Pro.",
      "Pro 订阅不能使用仅限 Studio 的多轨操作。",
    ],
  ]);

  const SOURCE_RULE_LABELS = {
    use_target_as_edit_parent: "使用目标候选作为编辑父级",
    do_not_attach_reference_as_sample: "不要把参考音频作为 Sample 附加",
  };

  const EVIDENCE_TRANSLATIONS = new Map([
    [
      "Analysis is available, but formal recommendation is withheld.",
      "已有分析结果，但正式发布建议仍被保留。",
    ],
    [
      "ProjectDecisionPolicy is missing",
      "尚未显式声明项目决策规则",
    ],
    [
      "blind-listening probes failed; subjective round invalid",
      "匿名盲听探针尚未通过，主观轮次无效",
    ],
    [
      "no valid blind-listening evidence",
      "尚无有效的匿名盲听证据",
    ],
  ]);

  function localizeEvidence(value, context = null) {
    const text = displayValue(value, "");
    const complianceNa = text.match(
      /^(.+): Compliance N\/A exceeds policy ceiling$/
    );
    if (complianceNa) {
      const candidate = context ? findCandidate(context, complianceNa[1]) : null;
      return `${candidate?.title || complianceNa[1]}：需求匹配的 N/A 比例超过已确认上限`;
    }
    const normalizedDivergence = text.match(
      /^time-normalized (pitch_harmony|rhythm_onset|energy_structure) divergence$/
    );
    if (normalizedDivergence) {
      const family = {
        pitch_harmony: "旋律 / 和声",
        rhythm_onset: "节奏 / 起音",
        energy_structure: "能量 / 结构",
      }[normalizedDivergence[1]];
      return `按时间归一后的${family}差异`;
    }
    return EVIDENCE_TRANSLATIONS.get(text) || text;
  }

  function requirementLabel(value) {
    return REQUIREMENT_LABELS[value] || displayValue(value);
  }

  function requirementSummary(requirement) {
    const value =
      typeof requirement.value === "string"
        ? requirement.value
        : JSON.stringify(requirement.value);
    const compact = displayValue(value).replace(/\s+/g, " ").trim();
    if (requirement.id.toLowerCase().includes("lyric")) {
      const sectionCount = (compact.match(/\[[^\]]+\]/g) || []).length;
      return `已冻结 ${compact.length} 字${
        sectionCount ? ` · ${sectionCount} 个段落标记` : ""
      }`;
    }
    return compact.length > 160 ? `${compact.slice(0, 157)}…` : compact;
  }

  function endingLabel(value) {
    return ENDING_LABELS[value] || displayValue(value);
  }

  function preservationLabel(value) {
    return PRESERVATION_LABELS[value] || displayValue(value);
  }

  function directiveTargetId(directive) {
    return directive.resolved_target_id || directive.target_artifact_id || null;
  }

  function localizePlanText(value) {
    return PLAN_TEXT.get(displayValue(value, "")) || displayValue(value);
  }

  function create(tag, options = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(options)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "className") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "html") node.innerHTML = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key === "on") {
        for (const [eventName, handler] of Object.entries(value)) {
          node.addEventListener(eventName, handler);
        }
      } else if (key in node && key !== "form") {
        try {
          node[key] = value;
        } catch {
          node.setAttribute(key, value);
        }
      } else {
        node.setAttribute(key, value);
      }
    }
    const values = Array.isArray(children) ? children : [children];
    for (const child of values) {
      if (child === undefined || child === null || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  function fragment(...children) {
    const result = document.createDocumentFragment();
    children.flat().forEach((child) => {
      if (child !== undefined && child !== null && child !== false) result.append(child);
    });
    return result;
  }

  function button(label, options = {}) {
    const node = create(
      options.href ? "a" : "button",
      {
        className: `button${options.variant ? ` ${options.variant}` : ""}`,
        href: options.href,
        type: options.href ? undefined : options.type || "button",
        disabled: options.disabled,
        title: options.title,
        on: options.onClick ? { click: options.onClick } : undefined,
      },
      []
    );
    if (options.icon) node.insertAdjacentHTML("beforeend", ICONS[options.icon]);
    node.append(document.createTextNode(label));
    if (options.trailingIcon) node.insertAdjacentHTML("beforeend", ICONS[options.trailingIcon]);
    return node;
  }

  function formatDuration(seconds) {
    if (seconds === null || seconds === undefined || seconds === "") return "未采集";
    if (!Number.isFinite(Number(seconds))) return "未采集";
    const total = Math.max(0, Math.round(Number(seconds)));
    const minutes = Math.floor(total / 60);
    const rest = total % 60;
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds)) return "00:00";
    const total = Math.max(0, Math.floor(seconds));
    return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(
      total % 60
    ).padStart(2, "0")}`;
  }

  function displayValue(value, fallback = "未采集") {
    if (value === undefined || value === null || value === "") return fallback;
    return String(value);
  }

  function statusChip(label, kind = "") {
    return create("span", {
      className: `status-chip${kind ? ` ${kind}` : ""}`,
      text: label,
    });
  }

  function pageHeader({ kicker, title, subtitle = [], actions = [] }) {
    const titleBlock = create("div", {}, [
      create("p", { className: "page-kicker", text: kicker }),
      create("h1", { className: "page-title", text: title }),
      create(
        "p",
        { className: "page-subtitle" },
        subtitle.map((item) => create("span", { text: item }))
      ),
    ]);
    return create("header", { className: "page-head" }, [
      titleBlock,
      create("div", { className: "head-actions" }, actions),
    ]);
  }

  function section(title, subtitle = "", body = null, meta = "") {
    const titleNode = create("h2", { className: "section-title" }, [
      create("span", { text: title }),
      subtitle ? create("small", { text: subtitle }) : null,
    ]);
    return create("section", { className: "section" }, [
      create("header", { className: "section-head" }, [
        titleNode,
        meta ? create("div", { className: "section-meta", text: meta }) : null,
      ]),
      body,
    ]);
  }

  function emptyState(title, text, action = null) {
    return create("div", { className: "empty-state" }, [
      create("div", { className: "empty-symbol", text: "○", "aria-hidden": "true" }),
      create("h3", { text: title }),
      create("p", { text }),
      action ? create("div", { style: "margin-top:16px" }, [action]) : null,
    ]);
  }

  async function api(path, options = {}) {
    const isFormData = options.body instanceof FormData;
    const response = await fetch(path, {
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body && !isFormData
          ? { "Content-Type": "application/json" }
          : {}),
        ...(options.headers || {}),
      },
    });
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json")
      ? await response.json()
      : await response.text();
    if (!response.ok) {
      let detail = payload;
      if (payload && typeof payload === "object") {
        detail = payload.detail || payload;
      }
      if (Array.isArray(detail)) {
        detail = detail
          .map((item) => {
            if (!item || typeof item !== "object") return String(item);
            const location = Array.isArray(item.loc) ? item.loc.join(" → ") : "";
            return `${location ? `${location}：` : ""}${item.msg || JSON.stringify(item)}`;
          })
          .join("；");
      } else if (detail && typeof detail === "object") {
        detail = JSON.stringify(detail);
      }
      const error = new Error(detail || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function toast(message, kind = "") {
    const node = create("div", {
      className: `toast${kind ? ` ${kind}` : ""}`,
      text: message,
      role: kind === "error" ? "alert" : "status",
    });
    toastRegion.append(node);
    window.setTimeout(() => node.remove(), 4200);
  }

  function setBusy(value) {
    app.setAttribute("aria-busy", value ? "true" : "false");
  }

  function renderError(error) {
    document.body.classList.remove("blind-page");
    main.replaceChildren(
      create("div", { className: "error-page", role: "alert" }, [
        create("h1", { text: "无法打开此复核界面" }),
        create("p", {
          text: error instanceof Error ? error.message : String(error),
        }),
        button("返回项目列表", { href: "/", variant: "primary" }),
      ])
    );
    setBusy(false);
  }

  function navItem({ label, icon, href, active, disabled, onClick }) {
    const item = create(href && !disabled ? "a" : "button", {
      className: "nav-item",
      type: href ? undefined : "button",
      href: disabled ? undefined : href,
      "aria-label": label,
      "aria-current": active ? "page" : undefined,
      "aria-disabled": disabled ? "true" : undefined,
      dataset: { label },
      html: ICONS[icon],
      on: onClick && !disabled ? { click: onClick } : undefined,
    });
    return item;
  }

  function renderNavigation({ active, project, blindUrl, onWorkspaceView }) {
    const projectId = project?.id;
    const list = create("div", { className: "nav-list" });
    list.append(
      navItem({
        label: "项目列表",
        icon: "projects",
        href: "/",
        active: active === "projects",
      })
    );
    if (projectId) {
      list.append(
        navItem({
          label: "项目总览",
          icon: "overview",
          href: `/projects/${encodeURIComponent(projectId)}?view=overview`,
          active: active === "overview",
          onClick: onWorkspaceView
            ? (event) => {
                event.preventDefault();
                onWorkspaceView("overview");
              }
            : undefined,
        }),
        navItem({
          label: blindUrl ? "盲听工作台" : "尚未创建盲听轮次",
          icon: "blind",
          href: blindUrl || undefined,
          active: active === "blind",
          disabled: !blindUrl && active !== "blind",
        }),
        navItem({
          label: "具名复核",
          icon: "named",
          href: `/projects/${encodeURIComponent(projectId)}?view=named`,
          active: active === "named",
          onClick: onWorkspaceView
            ? (event) => {
                event.preventDefault();
                onWorkspaceView("named");
              }
            : undefined,
        }),
        navItem({
          label: "参考段计划",
          icon: "plan",
          href: `/projects/${encodeURIComponent(projectId)}?view=plan`,
          active: active === "plan",
          onClick: onWorkspaceView
            ? (event) => {
                event.preventDefault();
                onWorkspaceView("plan");
              }
            : undefined,
        })
      );
    }
    nav.replaceChildren(
      create("a", {
        className: "brand",
        href: "/",
        "aria-label": "Song Evaluator 项目列表",
        html: ICONS.brand,
      }),
      list,
      create("div", { className: "nav-meta" }, [
        create("strong", { text: project?.title || "本地运行" }),
        create("span", {
          text: project ? "不会上传至 Suno" : "Private analysis",
        }),
      ])
    );
  }

  async function decodeAudio(url) {
    if (!audioCache.has(url)) {
      audioCache.set(
        url,
        (async () => {
          const response = await fetch(url);
          if (!response.ok) throw new Error(`音频读取失败（${response.status}）`);
          const buffer = await response.arrayBuffer();
          const AudioContextClass = window.AudioContext || window.webkitAudioContext;
          if (!AudioContextClass) throw new Error("浏览器不支持音频解码");
          const context = new AudioContextClass();
          try {
            return await context.decodeAudioData(buffer.slice(0));
          } finally {
            await context.close();
          }
        })()
      );
    }
    const pending = audioCache.get(url);
    try {
      return await pending;
    } finally {
      if (audioCache.get(url) === pending) audioCache.delete(url);
    }
  }

  async function drawWaveform(canvas, url, options = {}) {
    const requestToken = Symbol("waveform");
    waveformRequests.set(canvas, requestToken);
    const parentWidth = canvas.clientWidth || 320;
    const parentHeight = canvas.clientHeight || 60;
    const scale = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = Math.max(1, Math.floor(parentWidth * scale));
    canvas.height = Math.max(1, Math.floor(parentHeight * scale));
    const context = canvas.getContext("2d");
    context.clearRect(0, 0, canvas.width, canvas.height);
    try {
      const audio = await decodeAudio(url);
      if (waveformRequests.get(canvas) !== requestToken) return null;
      const samples = audio.getChannelData(0);
      const columns = Math.max(40, Math.floor(canvas.width / (options.dense ? 3 : 5)));
      const step = Math.max(1, Math.floor(samples.length / columns));
      const center = canvas.height / 2;
      const maxHeight = canvas.height * 0.42;
      context.strokeStyle = options.color || "rgba(23,22,19,.48)";
      context.lineWidth = Math.max(1, scale);
      context.beginPath();
      for (let index = 0; index < columns; index += 1) {
        const start = index * step;
        const end = Math.min(samples.length, start + step);
        let peak = 0;
        for (let sample = start; sample < end; sample += Math.max(1, Math.floor(step / 180))) {
          peak = Math.max(peak, Math.abs(samples[sample]));
        }
        const x = (index / Math.max(1, columns - 1)) * canvas.width;
        const height = Math.max(scale, peak * maxHeight);
        context.moveTo(x, center - height);
        context.lineTo(x, center + height);
      }
      context.stroke();
      canvas.setAttribute(
        "aria-label",
        `由当前音频解码生成的波形，时长 ${formatDuration(audio.duration)}`
      );
      return audio.duration;
    } catch (error) {
      if (waveformRequests.get(canvas) !== requestToken) return null;
      context.strokeStyle = options.errorColor || "rgba(23,22,19,.18)";
      context.lineWidth = scale;
      context.beginPath();
      context.moveTo(0, canvas.height / 2);
      context.lineTo(canvas.width, canvas.height / 2);
      context.stroke();
      canvas.setAttribute("aria-label", `波形不可用：${error.message}`);
      return null;
    }
  }

  function drawWaveformWhenVisible(canvas, url, options = {}) {
    if (!waveformObserver) {
      drawWaveform(canvas, url, options);
      return;
    }
    deferredWaveforms.set(canvas, { url, options });
    waveformObserver.observe(canvas);
  }

  function findCandidate(context, artifactId) {
    return context.candidates.find((item) => item.artifact_id === artifactId);
  }

  function candidateLabel(candidate) {
    if (!candidate) return "";
    return `${candidate.title} · ${formatDuration(
      candidate.measured_duration_s || candidate.platform_duration_s
    )}`;
  }

  function operationLabel(operation) {
    const labels = {
      raw: "原始候选",
      crop: "Crop",
      edit_crop: "Crop",
      cover: "Cover",
      replace_section: "Replace Section",
      extend: "Extend",
      remaster: "Remaster",
      unknown: "操作未知",
    };
    return labels[operation] || displayValue(operation, "操作未知");
  }

  function intakeField(label, control, helper = "", className = "") {
    return create("div", { className: `field${className ? ` ${className}` : ""}` }, [
      create("label", { text: label, htmlFor: control.id }),
      control,
      helper ? create("small", { className: "field-helper", text: helper }) : null,
    ]);
  }

  function humanFileSize(bytes) {
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function parseParentDeclarations(value) {
    return value
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .map((line) => {
        const separator = line.indexOf("=");
        if (separator < 1 || separator === line.length - 1) {
          throw new Error("父级关系必须使用 CHILD_CLIP_ID=PARENT_CLIP_ID_OR_SUNO_URL");
        }
        return {
          child_clip_id: line.slice(0, separator).trim(),
          parent: line.slice(separator + 1).trim(),
        };
      });
  }

  function buildIntakeComposer(onJobCreated) {
    let source = "suno";
    let previewClips = [];
    const root = create("div", { className: "intake-composer", id: "new-analysis" });
    const modeRoot = create("div", { className: "intake-mode", role: "tablist" });
    const panel = create("div", {
      className: "intake-panel",
      id: "intake-source-panel",
      role: "tabpanel",
      tabIndex: 0,
    });
    const modePanels = new Map();

    function metadataFields(prefix) {
      const projectId = create("input", {
        id: `${prefix}-project-id`,
        className: "input",
        required: true,
        maxLength: 80,
        pattern: "[\\w\\u4e00-\\u9fff\\-]{1,80}",
        placeholder: "spring-final",
        autocomplete: "off",
      });
      const title = create("input", {
        id: `${prefix}-title`,
        className: "input",
        required: true,
        maxLength: 160,
        placeholder: "春 · 发布候选",
        autocomplete: "off",
      });
      const lyrics = create("textarea", {
        id: `${prefix}-lyrics`,
        className: "textarea",
        maxLength: 100000,
        placeholder: "可留空；填写时会作为冻结歌词要求",
      });
      const style = create("textarea", {
        id: `${prefix}-style`,
        className: "textarea compact",
        maxLength: 10000,
        placeholder: "例如：饱满、温暖、女声克制",
      });
      const exclude = create("textarea", {
        id: `${prefix}-exclude`,
        className: "textarea compact",
        maxLength: 10000,
        placeholder: "例如：避免突然转冷、过长器乐绕行",
      });
      const analyze = create("input", {
        id: `${prefix}-analyze`,
        type: "checkbox",
        checked: true,
      });
      return { projectId, title, lyrics, style, exclude, analyze };
    }

    function advancedFields(fields, extra = null) {
      return create("details", { className: "intake-advanced" }, [
        create("summary", { text: "创作约束与高级选项" }),
        create("div", { className: "form-grid" }, [
          intakeField("冻结歌词（可选）", fields.lyrics, "只作为评估要求，不会发送给 Suno。", "span-2"),
          intakeField("Style（可选）", fields.style),
          intakeField("Exclude（可选）", fields.exclude),
          extra,
          create("label", { className: "check-row span-2" }, [
            fields.analyze,
            create("span", {}, [
              create("strong", { text: "导入完成后立即运行技术分析" }),
              create("small", { text: "分析在你的服务器上执行，可在任务区查看进度。" }),
            ]),
          ]),
        ]),
      ]);
    }

    function resetIdentityFields(fields) {
      fields.projectId.value = "";
      fields.title.value = "";
    }

    function renderSuno() {
      previewClips = [];
      let previewedUrl = "";
      let maxSelectable = 24;
      const fields = metadataFields("suno");
      const url = create("input", {
        id: "suno-url",
        className: "input",
        type: "url",
        required: true,
        placeholder: "https://suno.com/s/... 或 /playlist/...",
        autocomplete: "off",
      });
      const parents = create("textarea", {
        id: "suno-parents",
        className: "textarea compact mono",
        placeholder: "每行一条：CHILD_CLIP_ID=PARENT_CLIP_ID_OR_SUNO_URL",
      });
      const previewStatus = create("div", { className: "form-status", role: "status" });
      const clipRoot = create("div", { className: "intake-clips" });
      const submit = button("创建分析项目", { variant: "primary", type: "submit", disabled: true });

      function renderClips() {
        clipRoot.replaceChildren(
          ...previewClips.map((clip, index) => {
            const checkbox = create("input", {
              type: "checkbox",
              value: clip.id,
              checked: index < maxSelectable,
              on: {
                change: () => {
                  const selected = clipRoot.querySelectorAll("input:checked");
                  if (checkbox.checked && selected.length > maxSelectable) {
                    checkbox.checked = false;
                    previewStatus.className = "form-status error";
                    previewStatus.textContent = `一次最多选择 ${maxSelectable} 个候选。`;
                  }
                  submit.disabled = !clipRoot.querySelector("input:checked");
                },
              },
            });
            return create("label", { className: "intake-clip" }, [
              checkbox,
              create("span", { className: "intake-clip-copy" }, [
                create("strong", { text: clip.title || "未命名 Suno 候选" }),
                create("small", { text: `${formatDuration(clip.duration)} · ${clip.id}` }),
              ]),
              clip.audio_url
                ? create("audio", { controls: true, preload: "none", src: clip.audio_url })
                : statusChip("无公开音频", "unknown"),
            ]);
          })
        );
      }

      const preview = button("读取候选", {
        onClick: async () => {
          if (!url.reportValidity()) return;
          preview.disabled = true;
          previewStatus.className = "form-status";
          previewStatus.textContent = "正在读取 Suno 公开页面…";
          try {
            const result = await api("/intakes/suno/preview", {
              method: "POST",
              body: JSON.stringify({ url: url.value.trim() }),
            });
            previewClips = result.clips;
            maxSelectable = result.max_selectable || 24;
            previewedUrl = url.value.trim();
            renderClips();
            submit.disabled = previewClips.length === 0;
            previewStatus.className = "form-status success";
            previewStatus.textContent = result.count > maxSelectable
              ? `已读取 ${result.count} 个候选；一次最多选择 ${maxSelectable} 个。`
              : `已读取 ${result.count} 个候选；请选择要比较的版本。`;
          } catch (error) {
            previewStatus.className = "form-status error";
            previewStatus.textContent = error.message;
          } finally {
            preview.disabled = false;
          }
        },
      });
      url.addEventListener("input", () => {
        if (!previewedUrl || url.value.trim() === previewedUrl) return;
        previewedUrl = "";
        previewClips = [];
        clipRoot.replaceChildren();
        submit.disabled = true;
        previewStatus.className = "form-status";
        previewStatus.textContent = "链接已改变，请重新读取候选。";
      });
      const form = create("form", {
        className: "intake-form",
        on: {
          submit: async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            if (!previewedUrl || url.value.trim() !== previewedUrl) {
              previewStatus.className = "form-status error";
              previewStatus.textContent = "请先读取当前链接的候选。";
              return;
            }
            const selected = [...clipRoot.querySelectorAll("input:checked")].map(
              (item) => item.value
            );
            if (!selected.length) {
              previewStatus.className = "form-status error";
              previewStatus.textContent = "至少选择一个候选。";
              return;
            }
            submit.disabled = true;
            previewStatus.className = "form-status";
            previewStatus.textContent = "正在创建任务…";
            try {
              const job = await api("/intakes/suno", {
                method: "POST",
                body: JSON.stringify({
                  project_id: fields.projectId.value.trim(),
                  title: fields.title.value.trim(),
                  url: url.value.trim(),
                  selected_clip_ids: selected,
                  lyrics: fields.lyrics.value.trim() || null,
                  style: fields.style.value.trim() || null,
                  exclude: fields.exclude.value.trim() || null,
                  parents: parseParentDeclarations(parents.value),
                  analyze_now: fields.analyze.checked,
                }),
              });
              previewStatus.className = "form-status success";
              previewStatus.textContent = "任务已创建，可在下方继续查看进度。";
              onJobCreated(job);
              resetIdentityFields(fields);
              submit.disabled = false;
            } catch (error) {
              previewStatus.className = "form-status error";
              previewStatus.textContent = error.message;
              submit.disabled = false;
            }
          },
        },
      }, [
        create("div", { className: "intake-lead" }, [
          create("div", {}, [
            create("h3", { text: "从 Suno 公开链接开始" }),
            create("p", { text: "先读取并核对候选，再由你明确创建分析任务。工具不会生成歌曲、上传到 Suno 或消耗 credits。" }),
          ]),
          statusChip("只读 Suno", "recorded"),
        ]),
        create("div", { className: "form-grid intake-primary-fields" }, [
          intakeField("项目 ID", fields.projectId, "创建后不可修改；可使用中文、字母、数字和连字符。"),
          intakeField("项目名称", fields.title),
          intakeField("Suno 分享或 Playlist 链接", url, "只允许 suno.com。", "span-2"),
        ]),
        create("div", { className: "intake-preview-action" }, [preview, previewStatus]),
        clipRoot,
        advancedFields(
          fields,
          intakeField("已知父级关系（可选）", parents, "浏览器端只接受候选 Clip ID 或 Suno URL，不接受服务器路径。", "span-2")
        ),
        create("footer", { className: "form-footer" }, [
          create("span", { className: "intake-privacy", text: "音频会下载到你的私有服务器，保持原始字节。" }),
          submit,
        ]),
      ]);
      return form;
    }

    function renderUpload() {
      const fields = metadataFields("upload");
      const fileInput = create("input", {
        id: "upload-files",
        type: "file",
        accept: ".wav,.wave,.mp3,.m4a,.flac,.ogg,.aac,audio/*",
        multiple: true,
        required: true,
        className: "sr-file-input",
      });
      const fileList = create("div", { className: "upload-file-list" });
      const uploadStatus = create("div", { className: "form-status", role: "status" });
      const submit = button("上传并分析", { variant: "primary", type: "submit" });

      function renderFiles() {
        const files = [...fileInput.files];
        fileList.replaceChildren(
          ...(files.length
            ? files.map((file) => create("div", { className: "upload-file" }, [
                create("span", { text: file.name }),
                create("small", { text: humanFileSize(file.size) }),
              ]))
            : [create("p", { text: "支持 WAV、MP3、M4A、FLAC、OGG、AAC；最多 24 首。" })])
        );
      }
      fileInput.addEventListener("change", renderFiles);
      const drop = create("label", { className: "upload-drop", htmlFor: fileInput.id }, [
        create("span", { className: "upload-mark", text: "+", "aria-hidden": "true" }),
        create("strong", { text: "选择要比较的歌曲" }),
        create("small", { text: "可一次选择多个文件；服务器会验证实际音频，不会转码。" }),
        fileInput,
      ]);
      renderFiles();

      const form = create("form", {
        className: "intake-form",
        on: {
          submit: async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            const files = [...fileInput.files];
            if (!files.length) return;
            submit.disabled = true;
            uploadStatus.className = "form-status";
            uploadStatus.textContent = "正在上传并校验原始音频，请保持页面打开…";
            const body = new FormData();
            body.append("project_id", fields.projectId.value.trim());
            body.append("title", fields.title.value.trim());
            body.append("lyrics", fields.lyrics.value.trim());
            body.append("style", fields.style.value.trim());
            body.append("exclude", fields.exclude.value.trim());
            body.append("analyze_now", String(fields.analyze.checked));
            files.forEach((file) => body.append("files", file, file.name));
            try {
              const job = await api("/intakes/upload", { method: "POST", body });
              uploadStatus.className = "form-status success";
              uploadStatus.textContent = "上传完成，分析任务已进入队列。";
              onJobCreated(job);
              resetIdentityFields(fields);
              submit.disabled = false;
            } catch (error) {
              uploadStatus.className = "form-status error";
              uploadStatus.textContent = error.message;
              submit.disabled = false;
            }
          },
        },
      }, [
        create("div", { className: "intake-lead" }, [
          create("div", {}, [
            create("h3", { text: "上传已经下载的候选" }),
            create("p", { text: "适合 WAV 或从 Suno 下载的完整歌曲。上传只发生在浏览器与你的私有服务器之间。" }),
          ]),
          statusChip("Byte exact", "measured"),
        ]),
        create("div", { className: "form-grid intake-primary-fields" }, [
          intakeField("项目 ID", fields.projectId, "可使用中文、字母、数字和连字符。"),
          intakeField("项目名称", fields.title),
          create("div", { className: "span-2" }, [drop, fileList]),
        ]),
        advancedFields(fields),
        create("footer", { className: "form-footer" }, [uploadStatus, submit]),
      ]);
      return form;
    }

    function renderAdvanced() {
      const fields = metadataFields("advanced");
      fields.projectId.required = false;
      fields.title.required = false;
      const payload = create("textarea", {
        id: "advanced-json",
        className: "textarea intake-json mono",
        required: true,
        spellcheck: "false",
        placeholder: "粘贴 song-eval fetch-suno 输出的 JSON 数组，或完整 ProjectManifest JSON 对象",
      });
      const status = create("div", { className: "form-status", role: "status" });
      const submit = button("验证并导入", { variant: "primary", type: "submit" });
      const form = create("form", {
        className: "intake-form",
        on: {
          submit: async (event) => {
            event.preventDefault();
            if (!form.reportValidity()) return;
            submit.disabled = true;
            status.className = "form-status";
            status.textContent = "正在验证 JSON…";
            try {
              const parsed = JSON.parse(payload.value);
              if (Array.isArray(parsed)) {
                const job = await api("/intakes/suno/snapshot", {
                  method: "POST",
                  body: JSON.stringify({
                    project_id: fields.projectId.value.trim(),
                    title: fields.title.value.trim(),
                    snapshots: parsed,
                    lyrics: fields.lyrics.value.trim() || null,
                    style: fields.style.value.trim() || null,
                    exclude: fields.exclude.value.trim() || null,
                    analyze_now: fields.analyze.checked,
                  }),
                });
                status.className = "form-status success";
                status.textContent = "快照任务已创建。";
                onJobCreated(job);
                resetIdentityFields(fields);
                submit.disabled = false;
              } else if (parsed && typeof parsed === "object") {
                const imported = await api("/manifests/import", {
                  method: "POST",
                  body: JSON.stringify(parsed),
                });
                if (fields.analyze.checked) {
                  await api(`/projects/${encodeURIComponent(imported.project_id)}/analysis`, {
                    method: "POST",
                    body: JSON.stringify({ review: null }),
                  });
                }
                status.className = "form-status success";
                status.textContent = "Manifest 已导入；正在打开项目。";
                window.location.assign(`/projects/${encodeURIComponent(imported.project_id)}`);
              } else {
                throw new Error("JSON 必须是 Suno 快照数组或 ProjectManifest 对象");
              }
            } catch (error) {
              status.className = "form-status error";
              status.textContent = error.message;
              submit.disabled = false;
            }
          },
        },
      }, [
        create("div", { className: "intake-lead" }, [
          create("div", {}, [
            create("h3", { text: "离线快照或完整 Manifest" }),
            create("p", { text: "用于可复现导入。数组按 Suno 快照处理；对象按 ProjectManifest 处理，本地路径仍必须位于已配置的可信目录。" }),
          ]),
          statusChip("Advanced", "manual"),
        ]),
        create("div", { className: "form-grid" }, [
          intakeField("项目 ID（快照数组必填）", fields.projectId),
          intakeField("项目名称（快照数组必填）", fields.title),
          intakeField("JSON", payload, "请求体上限为 8 MB。", "span-2"),
        ]),
        advancedFields(fields),
        create("footer", { className: "form-footer" }, [status, submit]),
      ]);
      return form;
    }

    const modeDefinitions = [
      ["suno", "Suno 链接"],
      ["upload", "本地音频"],
      ["advanced", "高级 JSON"],
    ];
    const renderers = {
      suno: renderSuno,
      upload: renderUpload,
      advanced: renderAdvanced,
    };
    let modes = [];

    function selectMode(value, { focus = false } = {}) {
      source = value;
      const activeIndex = modeDefinitions.findIndex(([key]) => key === value);
      modes.forEach((item, index) => {
        const active = index === activeIndex;
        item.classList.toggle("active", active);
        item.setAttribute("aria-selected", active ? "true" : "false");
        item.tabIndex = active ? 0 : -1;
      });
      if (!modePanels.has(value)) modePanels.set(value, renderers[value]());
      panel.setAttribute("aria-labelledby", `intake-tab-${value}`);
      panel.replaceChildren(modePanels.get(value));
      if (focus) modes[activeIndex].focus();
    }

    modes = modeDefinitions.map(([value, label]) => button(label, {
      variant: value === source ? "active" : "",
      onClick: () => selectMode(value),
    }));
    modes.forEach((item, index) => {
      const value = modeDefinitions[index][0];
      item.id = `intake-tab-${value}`;
      item.setAttribute("role", "tab");
      item.setAttribute("aria-controls", panel.id);
      item.setAttribute("aria-selected", index === 0 ? "true" : "false");
      item.tabIndex = index === 0 ? 0 : -1;
      item.addEventListener("keydown", (event) => {
        const current = modes.indexOf(event.currentTarget);
        let next = current;
        if (event.key === "ArrowRight") next = (current + 1) % modes.length;
        else if (event.key === "ArrowLeft") next = (current - 1 + modes.length) % modes.length;
        else if (event.key === "Home") next = 0;
        else if (event.key === "End") next = modes.length - 1;
        else return;
        event.preventDefault();
        selectMode(modeDefinitions[next][0], { focus: true });
      });
    });
    modeRoot.append(...modes);
    root.append(modeRoot, panel);
    selectMode("suno");
    return root;
  }

  function intakeJobCard(job, onChanged) {
    const statusLabels = {
      queued: "等待中",
      running: "处理中",
      succeeded: "已完成",
      failed: "失败",
      canceled: "已取消",
    };
    const actions = [];
    if (job.status === "queued" || job.status === "running") {
      actions.push(button("取消", {
        onClick: async () => {
          try {
            await api(`/intake-jobs/${encodeURIComponent(job.id)}/cancel`, { method: "POST" });
            onChanged();
          } catch (error) {
            toast(error.message, "error");
          }
        },
      }));
    } else if (job.status === "failed" || job.status === "canceled") {
      let discardPartialProject = false;
      const cleanup = button("清理", {
        onClick: async () => {
          try {
            const query = discardPartialProject
              ? "?discard_partial_project=true"
              : "";
            await api(`/intake-jobs/${encodeURIComponent(job.id)}${query}`, {
              method: "DELETE",
            });
            onChanged();
          } catch (error) {
            if (
              !discardPartialProject &&
              error.status === 409 &&
              error.message.includes("discard_partial_project=true")
            ) {
              discardPartialProject = true;
              cleanup.textContent = "确认放弃项目";
              toast("项目已导入但分析未完成；再次点击会删除这个不完整项目。", "error");
              return;
            }
            toast(error.message, "error");
          }
        },
      });
      actions.push(
        button("重试", {
          variant: "primary",
          onClick: async () => {
            try {
              await api(`/intake-jobs/${encodeURIComponent(job.id)}/retry`, { method: "POST" });
              onChanged();
            } catch (error) {
              toast(error.message, "error");
            }
          },
        }),
        cleanup
      );
    } else if (job.result?.project_url) {
      actions.push(button("打开项目", { href: job.result.project_url, variant: "primary", trailingIcon: "arrow" }));
    }
    return create("article", { className: `intake-job ${job.status}` }, [
      create("div", { className: "intake-job-main" }, [
        create("div", { className: "intake-job-title" }, [
          create("strong", { text: job.title }),
          statusChip(statusLabels[job.status] || job.status, job.status === "succeeded" ? "recorded" : job.status === "failed" ? "unknown" : "manual"),
        ]),
        create("small", { text: `${job.source.kind === "suno" ? "Suno 链接" : `${job.source.filenames.length} 个上传文件`} · ${job.step}` }),
        create("div", { className: "job-progress", role: "progressbar", "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": job.progress }, [
          create("span", { style: `width:${job.progress}%` }),
        ]),
        job.error ? create("p", { className: "job-error", text: job.error }) : null,
      ]),
      create("div", { className: "intake-job-actions" }, actions),
    ]);
  }

  async function bootProjectList() {
    renderNavigation({ active: "projects" });
    const [projects, initialJobs] = await Promise.all([
      api("/projects"),
      api("/intake-jobs"),
    ]);
    const page = create("div", { className: "page" });
    const projectBody = create("div");
    const jobBody = create("div", { className: "intake-jobs" });
    const guardrails = create("div", { className: "intake-guardrails" }, [
      create("p", { text: "不登录或操作你的 Suno 账户，也不会点赞、删除或发布。" }),
      create("p", { text: "不生成或再生成歌曲，不会消耗任何 Suno credits。" }),
      create("p", { text: "“上传”只指浏览器到你的私有服务器；音频不会发往 Suno 或 LLM。" }),
      create("p", { text: "源文件逐字节保存；未采集的元数据保持空缺，不做推测填充。" }),
    ]);
    let jobs = initialJobs;
    let refreshTimer = null;
    const projectSection = section("已有项目", `${projects.length} 个项目`, projectBody);

    function scheduleRefresh(delay) {
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
      refreshTimer = window.setTimeout(() => {
        refreshTimer = null;
        refreshJobs();
      }, delay);
    }

    function renderProjects(items) {
      const count = projectSection.querySelector(".section-title small");
      if (count) count.textContent = `${items.length} 个项目`;
      if (!items.length) {
        projectBody.replaceChildren(
          emptyState(
            "还没有已完成项目",
            "可直接在上方从 Suno 链接或本地音频创建第一个分析项目。"
          )
        );
        return;
      }
      projectBody.replaceChildren(
        create(
          "div",
          { className: "project-list" },
          items.map((project) =>
            create("a", {
              className: "project-row",
              href: `/projects/${encodeURIComponent(project.id)}`,
            }, [
              create("div", {}, [
                create("strong", { text: project.title }),
                create("small", { text: project.id }),
              ]),
              create("span", { html: ICONS.arrow, "aria-hidden": "true" }),
            ])
          )
        )
      );
    }

    async function refreshJobs() {
      const previous = new Map(jobs.map((job) => [job.id, job.status]));
      try {
        jobs = await api("/intake-jobs");
        renderJobs();
        const newlySucceeded = jobs.some(
          (job) => job.status === "succeeded" && previous.get(job.id) !== "succeeded"
        );
        if (newlySucceeded) {
          renderProjects(await api("/projects"));
        }
      } catch (error) {
        toast(error.message, "error");
        if (jobs.some((job) => job.status === "queued" || job.status === "running")) {
          scheduleRefresh(3000);
        }
      }
    }

    function renderJobs() {
      jobBody.replaceChildren(
        ...(jobs.length
          ? jobs.map((job) => intakeJobCard(job, refreshJobs))
          : [
              create("p", {
                className: "intake-jobs-empty",
                text: "还没有导入任务。上传或读取 Suno 候选后，进度会显示在这里。",
              }),
            ])
      );
      if (jobs.some((job) => job.status === "queued" || job.status === "running")) {
        scheduleRefresh(900);
      } else if (refreshTimer !== null) {
        window.clearTimeout(refreshTimer);
        refreshTimer = null;
      }
    }

    const composer = buildIntakeComposer((job) => {
      jobs = [job, ...jobs.filter((item) => item.id !== job.id)];
      renderJobs();
      jobBody.scrollIntoView({ behavior: "smooth", block: "center" });
      scheduleRefresh(300);
    });
    window.addEventListener("pagehide", () => {
      if (refreshTimer !== null) window.clearTimeout(refreshTimer);
    }, { once: true });
    page.append(
      pageHeader({
        kicker: "NEW ANALYSIS",
        title: "听清候选之间的区别",
        subtitle: ["Suno 链接或本地音频", "私有服务器分析", "不生成歌曲"],
        actions: [
          button("查看已有项目", {
            onClick: () => projectBody.scrollIntoView({ behavior: "smooth" }),
          }),
        ],
      })
    );
    page.append(
      create("div", { className: "intake-dashboard" }, [
        create("div", { className: "intake-primary-column" }, [
          section("新建分析项目", "先确认来源，再开始处理", composer, "不会操作你的 Suno 账户"),
          section("导入与分析任务", "可恢复、取消或重试", jobBody),
        ]),
        create("aside", { className: "intake-side-column", "aria-label": "项目与安全边界" }, [
          projectSection,
          section("这个入口不会做什么", "明确边界", guardrails),
        ]),
      ])
    );
    renderProjects(projects);
    renderJobs();
    main.replaceChildren(page);
    setBusy(false);
  }

  function buildEvidenceState(context) {
    const report = context.latest_report;
    const candidates = context.candidates;
    const allDurations = candidates.length > 0 && candidates.every((item) => item.measured_duration_s);
    const validBlind = Boolean(context.latest_listening?.valid);
    const named = Boolean(context.latest_review);
    const policy = Boolean(context.policy);
    const finalChoice = Boolean(context.latest_release_decision);
    const lyrics = context.lyric_analyses.length > 0;
    const hotspots = report
      ? (report.comparisons || []).reduce(
          (count, item) => count + (item.hotspots?.length || 0),
          0
        )
      : null;
    const lineageKnown = context.lineage.known_edges > 0;
    const sourceKnown = context.source_assessments.some(
      (item) => item.relationship !== "unknown"
    );
    const references = context.references.length > 0;
    const entries = [
      ["候选时长", allDurations ? "已测量" : "未采集", allDurations ? "measured" : "unknown"],
      ["技术分析", report ? "已测量" : "未采集", report ? "measured" : "unknown"],
      ["盲听轮次", validBlind ? "已记录" : context.latest_listening ? "未通过" : "未采集", validBlind ? "recorded" : "manual"],
      ["具名复核", named ? "已记录" : "需要人工", named ? "recorded" : "manual"],
      ["决策规则", policy ? "已确认" : "需要人工", policy ? "recorded" : "manual"],
      ["最终人工选择", finalChoice ? "已记录" : "需要人工", finalChoice ? "recorded" : "manual"],
      ["歌词定位", lyrics ? "已记录" : "未采集", lyrics ? "recorded" : "unknown"],
      ["差异定位", hotspots === null ? "未采集" : `${hotspots} 处`, hotspots === null ? "unknown" : "measured"],
      ["谱系关系", lineageKnown ? "已记录" : "来源未知", lineageKnown ? "recorded" : "unknown"],
      ["参考段", references ? "已记录" : sourceKnown ? "不适用" : "未采集", references || sourceKnown ? "recorded" : "unknown"],
    ];
    return entries;
  }

  function recommendationSection(context) {
    const report = context.latest_report;
    if (!report) {
      return section(
        "建议",
        "等待分析证据",
        emptyState(
          "尚未运行分析",
          "先运行技术分析，工具才会在证据允许时给出词典序建议；缺失证据不会被补成结论。",
          button("运行初次分析", {
            variant: "primary",
            onClick: async () => {
              try {
                setBusy(true);
                await api(`/projects/${encodeURIComponent(context.project.id)}/analysis`, {
                  method: "POST",
                  body: JSON.stringify({ review: null }),
                });
                window.location.reload();
              } catch (error) {
                toast(error.message, "error");
                setBusy(false);
              }
            },
          })
        )
      );
    }
    const recommendation = report.recommendation;
    const recommended = findCandidate(context, recommendation.recommended_artifact_id);
    const alternate = findCandidate(context, recommendation.alternate_artifact_id);
    const rationale = recommendation.rationale || [];
    const gaps = recommendation.evidence_gaps || [];
    const recordedChoiceId =
      context.latest_release_decision?.recommendation?.user_final_choice || null;
    const recordedChoice = findCandidate(context, recordedChoiceId);
    const primary = create("div", { className: "recommendation-primary" }, [
      create("p", {
        className: "eyeline",
        text:
          recommendation.status === "recommended"
            ? "建议发布"
            : "当前结论",
      }),
      create("h2", { className: "recommendation-name" }, [
        create("span", {
          text: recommended ? recommended.title : STATUS_LABELS[recommendation.status] || "暂不推荐",
        }),
        recommended
          ? create("small", {
              text: `${recommended.artifact_id} · ${formatDuration(
                recommended.measured_duration_s || recommended.platform_duration_s
              )}`,
            })
          : null,
        statusChip(
          recommendation.confidence
            ? `置信度 ${CONFIDENCE_LABELS[recommendation.confidence] || recommendation.confidence}`
            : "证据不足",
          recommendation.status === "recommended" ? "recorded" : "manual"
        ),
      ]),
      create(
        "ul",
        { className: "evidence-list" },
        rationale.length || gaps.length
          ? [
              ...rationale
                .slice(0, 4)
                .map((item) =>
                  create("li", { text: localizeEvidence(item, context) })
                ),
              ...gaps.slice(0, 3).map((item) =>
                create("li", {
                  className: "warning",
                  text: `证据缺口：${localizeEvidence(item, context)}`,
                })
              ),
            ]
          : [
              create("li", {
                className: "warning",
                text: "没有足够证据形成具体建议。",
              }),
            ]
      ),
    ]);
    const alternatePanel = create("div", { className: "recommendation-alternate" }, [
      create("p", { className: "eyeline", text: "备选与不确定性" }),
      alternate
        ? create("div", { className: "alternate-row" }, [
            create("span", { className: "rank", text: "02" }),
            create("div", {}, [
              create("strong", {
                text: `${alternate.title} · ${formatDuration(
                  alternate.measured_duration_s || alternate.platform_duration_s
                )}`,
              }),
              create("p", {
                text:
                  recommendation.alternate_costs?.join("；") ||
                  "作为词典序备选保留；不代表音乐质量高低。",
              }),
            ]),
          ])
        : create("p", {
            className: "table-note",
            text: "当前证据没有产生可陈述的备选。工具不会用输入顺序补位。",
          }),
      create("p", {
        className: "table-note",
        text: "建议只引用已测量、已记录或人工确认的证据；各轴不合并为总分。",
      }),
    ]);
    if (recordedChoice) {
      alternatePanel.append(
        create("div", { className: "decision-record" }, [
          create("strong", { text: `最终人工选择：${recordedChoice.title}` }),
          create("p", {
            className: "table-note",
            text:
              context.latest_release_decision.recommendation.user_override_reason ||
              "未填写补充理由。该记录不改写上方的策略建议。",
          }),
        ])
      );
    } else {
      const choice = create(
        "select",
        { className: "select", "aria-label": "最终人工发布选择" },
        context.candidates.map((candidate) =>
          create("option", {
            value: candidate.artifact_id,
            text: `${candidate.title} · ${formatDuration(
              candidate.measured_duration_s || candidate.platform_duration_s
            )}`,
          })
        )
      );
      if (recommended) choice.value = recommended.artifact_id;
      const reason = create("input", {
        className: "input",
        type: "text",
        maxlength: "1000",
        placeholder: "可选：说明为什么最终采用这个版本",
      });
      let decisionPending = false;
      const recordDecision = async () => {
        if (decisionPending) return;
        decisionPending = true;
        try {
          setBusy(true);
          await api(
            `/projects/${encodeURIComponent(context.project.id)}/release-decisions`,
            {
              method: "POST",
              body: JSON.stringify({
                artifact_id: choice.value,
                reason: reason.value.trim() || null,
                run_id: report.run.id,
                confirm: true,
              }),
            }
          );
          toast("最终人工选择已独立记录；策略建议保持不变。");
          window.location.reload();
        } catch (error) {
          decisionPending = false;
          toast(error.message, "error");
          setBusy(false);
        }
      };
      alternatePanel.append(
        create("div", { className: "decision-record" }, [
          create("strong", { text: "记录最终人工选择" }),
          create("p", {
            className: "table-note",
            text: "人工选择单独保存，不会倒写或伪装成策略建议。",
          }),
          choice,
          reason,
          button("确认记录", {
            variant: "quiet",
            onClick: recordDecision,
          }),
        ])
      );
    }
    return create("section", { className: "section" }, [
      create("header", { className: "section-head" }, [
        create("h2", { className: "section-title" }, [
          create("span", { text: "建议" }),
          create("small", { text: "依据已记录的证据" }),
        ]),
        create("span", {
          className: "section-meta",
          text: recommendation.status === "recommended" ? "词典序建议" : "明确弃权",
        }),
      ]),
      create("div", { className: "recommendation-grid" }, [primary, alternatePanel]),
    ]);
  }

  function evidenceSection(context) {
    const entries = buildEvidenceState(context);
    const complete = entries.filter(([, , kind]) => ["measured", "recorded"].includes(kind)).length;
    const manual = entries.filter(([, , kind]) => kind === "manual").length;
    const unknown = entries.length - complete - manual;
    const strip = create(
      "div",
      {
        className: "status-strip",
        "aria-hidden": "true",
        style: `grid-template-columns:repeat(${entries.length},minmax(0,1fr))`,
      },
      entries.map(([, , kind]) =>
        create("span", {
          className: ["measured", "recorded"].includes(kind)
            ? "complete"
            : kind === "manual"
              ? "manual"
              : "",
        })
      )
    );
    const grid = create(
      "div",
      { className: "evidence-grid" },
      entries.map(([label, state, kind]) =>
        create("div", { className: "evidence-cell" }, [
          create("span", { text: label }),
          statusChip(state, kind),
        ])
      )
    );
    return create("section", { className: "section" }, [
      create("header", { className: "section-head" }, [
        create("h2", { className: "section-title" }, [
          create("span", { text: "证据完整度" }),
          create("small", { text: "Evidence completeness" }),
        ]),
        create("div", {
          className: "section-meta",
          text: `${complete} 项已获取 · ${manual} 项待人工 · ${unknown} 项未采集`,
        }),
      ]),
      strip,
      grid,
    ]);
  }

  function candidateSection(context) {
    if (!context.candidates.length) {
      return section("候选记录", "", emptyState("没有候选", "当前项目没有可分析的发布候选。"));
    }
    const recommendation = context.latest_report?.recommendation;
    const order = new Map(
      [
        recommendation?.recommended_artifact_id,
        recommendation?.alternate_artifact_id,
      ]
        .filter(Boolean)
        .map((id, index) => [id, index])
    );
    const candidates = [...context.candidates].sort((a, b) => {
      const aOrder = order.has(a.artifact_id) ? order.get(a.artifact_id) : 99;
      const bOrder = order.has(b.artifact_id) ? order.get(b.artifact_id) : 99;
      return aOrder - bOrder;
    });
    const table = create("table", { className: "candidate-table" });
    table.append(
      create("thead", {}, [
        create("tr", {}, [
          create("th", { text: "记录 ID", style: "width:31%" }),
          create("th", { text: "真实音频波形", style: "width:31%" }),
          create("th", { text: "时长", style: "width:11%" }),
          create("th", { text: "已记录的内容" }),
        ]),
      ])
    );
    const tbody = create("tbody");
    for (const candidate of candidates) {
      const recommended =
        candidate.artifact_id === recommendation?.recommended_artifact_id;
      const canvas = create("canvas", {
        className: "metric-lines",
        role: "img",
        "aria-label": "正在生成真实音频波形",
      });
      const note =
        candidate.artifact_id === recommendation?.recommended_artifact_id
          ? "当前词典序建议"
          : candidate.artifact_id === recommendation?.alternate_artifact_id
            ? "当前备选"
            : candidate.assessment
              ? "已有分析评估"
              : "尚未形成评估";
      const row = create("tr", { className: recommended ? "recommended" : "" }, [
        create("td", {}, [
          create("div", { className: "candidate-id" }, [
            create("span", { className: "candidate-marker", "aria-hidden": "true" }),
            create("span", {}, [
              create("strong", { text: candidate.title }),
              create("small", {
                text: `${operationLabel(candidate.operation)} · ${
                  candidate.parent_state === "unknown"
                    ? "父级来源未知"
                    : candidate.parent_state || "无已声明父级"
                }`,
              }),
            ]),
          ]),
        ]),
        create("td", {}, [canvas]),
        create("td", {
          className: "duration",
          text: formatDuration(
            candidate.measured_duration_s || candidate.platform_duration_s
          ),
        }),
        create("td", { className: "table-note", text: note }),
      ]);
      tbody.append(row);
      requestAnimationFrame(() => {
        drawWaveformWhenVisible(canvas, candidate.audio_url, {
          color: recommended ? "rgba(7,85,217,.70)" : "rgba(23,22,19,.34)",
        });
      });
    }
    table.append(tbody);
    return section(
      "候选记录",
      `${candidates.length} 条 · 波形由当前本地音频生成`,
      table,
      "不从波形外观推断音乐质量"
    );
  }

  function hotspotSection(context) {
    const comparisons = context.latest_report?.comparisons || [];
    const hotspots = comparisons.flatMap((comparison) =>
      (comparison.hotspots || []).map((hotspot) => ({ comparison, hotspot }))
    );
    if (!hotspots.length) {
      return section(
        "时间轴差异热点",
        "",
        emptyState(
          "尚未获得差异定位",
          "热点只来自实际的多特征分析。工具不会用“听起来不同”或波形外观推断差异位置与原因。"
        ),
        "未采集"
      );
    }
    const body = create(
      "div",
      { className: "requirements" },
      hotspots.slice(0, 6).map(({ comparison, hotspot }, index) => {
        const a = findCandidate(context, comparison.artifact_a_id);
        const b = findCandidate(context, comparison.artifact_b_id);
        return create("div", { className: "requirement-row" }, [
          create("span", {
            className: "row-index",
            text: String(index + 1).padStart(2, "0"),
          }),
          create("div", {}, [
            create("strong", {
              text: `${a?.title || "候选 A"} ${formatTime(
                hotspot.a_start_s
              )}–${formatTime(hotspot.a_end_s)} ↔ ${
                b?.title || "候选 B"
              } ${formatTime(hotspot.b_start_s)}–${formatTime(hotspot.b_end_s)}`,
            }),
            create("p", { text: localizeEvidence(hotspot.evidence, context) }),
          ]),
          statusChip(
            {
              pitch_harmony: "旋律 / 和声",
              rhythm_onset: "节奏 / 起音",
              energy_structure: "能量 / 结构",
            }[hotspot.feature_family] || hotspot.feature_family,
            "measured"
          ),
        ]);
      })
    );
    return section(
      "时间轴差异热点",
      "",
      body,
      `${hotspots.length} 处实际分析结果`
    );
  }

  function axisSection(context) {
    const assessments = context.candidates
      .map((candidate) => candidate.assessment)
      .filter(Boolean);
    const body = create(
      "div",
      { className: "axis-list" },
      Object.entries(AXIS_LABELS).map(([axis, [label, english]]) => {
        const evaluations =
          axis === "compliance"
            ? assessments
                .map(
                  (assessment) =>
                    assessment.compliance_vs_target ||
                    assessment.compliance_as_generated ||
                    (assessment.evaluations || []).find(
                      (evaluation) => evaluation.axis === axis
                    )
                )
                .filter(Boolean)
            : assessments
                .flatMap((assessment) => assessment.evaluations || [])
                .filter((evaluation) => evaluation.axis === axis);
        const passCount = evaluations.filter(
          (evaluation) => evaluation.status === "pass"
        ).length;
        const failCount = evaluations.filter(
          (evaluation) => evaluation.status === "fail"
        ).length;
        const evidenceCount = evaluations.reduce(
          (count, evaluation) => count + (evaluation.observations?.length || 0),
          0
        );
        const status = !evaluations.length
          ? "未采集"
          : failCount
            ? `${failCount} 条未通过`
            : passCount === evaluations.length
              ? `${passCount} 条已评估`
              : `${evaluations.length - passCount} 条待确认`;
        const ignored = evaluations.some(
          (evaluation) => evaluation.ignored_for_ordering
        );
        return create("div", { className: "axis" }, [
          create("span", { className: "axis-label", text: english }),
          create("strong", { text: `${label} · ${status}` }),
          create("p", {
            text: evaluations.length
              ? `${evidenceCount} 条独立观察；${
                  ignored ? "本轴只作描述，不参与排序。" : "不与其它轴相加。"
                }`
              : "当前没有可归属到此轴的评估证据。",
          }),
        ]);
      })
    );
    return section("四个独立轴", "", body, "各轴不合并、不加权");
  }

  function overviewView(context, navigate) {
    const report = context.latest_report;
    let blindRequestPending = false;
    const canCreateBlind = Boolean(
      report?.comparisons?.some((comparison) => comparison.hotspots?.length)
    );
    const subtitle = [
      `${context.candidates.length} 条候选记录`,
      report ? `分析运行 ${report.run.id}` : "尚未运行分析",
      context.lineage.unknown_parent_count
        ? `${context.lineage.unknown_parent_count} 个父级来源未知`
        : "谱系按已记录证据呈现",
    ];
    const actions = [];
    if (report) {
      actions.push(
        button("导出证据包", {
          href: `/projects/${encodeURIComponent(context.project.id)}/evidence.json`,
          icon: "download",
        })
      );
    }
    const createBlindRound = async () => {
      if (!report || !canCreateBlind || blindRequestPending) return;
      try {
        blindRequestPending = true;
        setBusy(true);
        const payload = await api(
          `/projects/${encodeURIComponent(context.project.id)}/blind-sessions`,
          {
            method: "POST",
            body: JSON.stringify({
              run_id: report.run.id,
              max_hotspots_per_pair: 1,
            }),
          }
        );
        window.location.href = payload.review_url;
      } catch (error) {
        blindRequestPending = false;
        toast(error.message, "error");
        setBusy(false);
      }
    };
    if (context.latest_listening?.valid && canCreateBlind) {
      actions.push(
        button("新建盲听轮次", {
          variant: "quiet",
          onClick: createBlindRound,
        })
      );
    }
    actions.push(
      button(
        context.latest_listening?.valid
          ? "进入具名复核"
          : context.latest_listening
            ? "继续盲听"
            : canCreateBlind
              ? "进入盲听"
              : "暂无可比盲听片段",
        {
          variant: "primary",
          trailingIcon: "arrow",
          disabled: !context.latest_listening && !canCreateBlind,
          title:
            !context.latest_listening && !canCreateBlind
              ? "至少需要两个带有可比差异热点的候选"
              : undefined,
          onClick: async () => {
            if (context.latest_listening?.valid) {
              await navigate("named");
              return;
            }
            if (context.latest_listening) {
              window.location.href = context.latest_listening.review_url;
              return;
            }
            if (!report) {
              toast("请先运行分析，才能建立盲听刺激。", "error");
              return;
            }
            await createBlindRound();
          },
        }
      )
    );
    const page = create("div", { className: "page" }, [
      pageHeader({
        kicker: "项目总览 / PROJECT OVERVIEW",
        title: context.project.title,
        subtitle,
        actions,
      }),
      recommendationSection(context),
      evidenceSection(context),
      candidateSection(context),
      hotspotSection(context),
      create("div", { className: "two-column" }, [
        axisSection(context),
        create("div", {}, [
          create("div", { className: "warning-panel", style: "margin-top:18px" }, [
            create("h2", { text: "不确定性 · 暂不推荐的部分" }),
            create(
              "ul",
              { className: "plain-list" },
              [
                context.lineage.unknown_parent_count
                  ? `${context.lineage.unknown_parent_count} 个 Crop / 编辑父级来源未知，谱系不做推断。`
                  : null,
                ...(report?.recommendation?.evidence_gaps || [])
                  .slice(0, 4)
                  .map((item) => localizeEvidence(item, context)),
                "温暖度、Hook、人声身份、编曲发展、歌词表达和结尾完成度由人听辨，不由测量替代。",
              ]
                .filter(Boolean)
                .map((item) => create("li", { text: item }))
            ),
            button("前往具名复核", {
              variant: "warning",
              onClick: () => navigate("named"),
            }),
          ]),
        ]),
      ]),
    ]);
    return page;
  }

  function latestReviewByArtifact(context) {
    const reviews = context.latest_review?.review_packet?.artifact_reviews || [];
    return new Map(reviews.map((item) => [item.artifact_id, item]));
  }

  function audioWasAuditioned(audio, select, helper) {
    const playedIntervals = [];
    let previousTime = null;
    let previousWallTime = null;
    const mergeInterval = (start, end) => {
      if (end <= start) return;
      playedIntervals.push([start, end]);
      playedIntervals.sort((a, b) => a[0] - b[0]);
      for (let index = playedIntervals.length - 1; index > 0; index -= 1) {
        const current = playedIntervals[index];
        const prior = playedIntervals[index - 1];
        if (current[0] <= prior[1] + 0.15) {
          prior[1] = Math.max(prior[1], current[1]);
          playedIntervals.splice(index, 1);
        }
      }
    };
    const update = (forcePlaying = false) => {
      if (!Number.isFinite(audio.duration)) return;
      const now = performance.now();
      const zoneStart = Math.max(0, audio.duration - 10);
      const currentTime = Math.min(audio.duration, audio.currentTime);
      const wallDelta =
        previousWallTime === null ? 0 : (now - previousWallTime) / 1000;
      if (
        previousTime !== null &&
        (forcePlaying || !audio.paused) &&
        currentTime >= previousTime &&
        currentTime - previousTime <= wallDelta + 0.75
      ) {
        mergeInterval(
          Math.max(zoneStart, previousTime),
          Math.min(audio.duration, currentTime)
        );
      }
      previousTime = currentTime;
      previousWallTime = now;
      const auditioned = playedIntervals.reduce(
        (total, [start, end]) => total + Math.max(0, end - start),
        0
      );
      const required = audio.duration - zoneStart;
      if (auditioned >= Math.max(0, required - 0.4)) {
        select.disabled = false;
        helper.textContent = "已真实播放完整结尾区域，可以记录人工判断。";
        helper.dataset.auditioned = "true";
      } else {
        helper.textContent = `结尾已真实试听 ${auditioned.toFixed(
          1
        )} / ${required.toFixed(1)} 秒；拖动进度条不会解锁。`;
      }
    };
    audio.addEventListener("play", () => {
      previousTime = audio.currentTime;
      previousWallTime = performance.now();
    });
    audio.addEventListener("seeking", () => {
      previousTime = null;
      previousWallTime = null;
    });
    audio.addEventListener("seeked", () => {
      previousTime = audio.currentTime;
      previousWallTime = performance.now();
      update();
    });
    audio.addEventListener("timeupdate", update);
    audio.addEventListener("ended", () => {
      update(true);
      previousTime = null;
      previousWallTime = null;
    });
  }

  function candidateReviewCard(candidate, serverReview, draft, formState) {
    const card = create("section", { className: "section", dataset: { artifactId: candidate.artifact_id } });
    const audio = create("audio", {
      controls: true,
      preload: "metadata",
      src: candidate.audio_url,
    });
    const endingSelect = create("select", {
      className: "select",
      "aria-label": `${candidate.title} 结尾人工确认`,
    }, [
      create("option", { value: "", text: "尚未判断" }),
      create("option", { value: "pass", text: "结尾自然、可发布" }),
      create("option", { value: "fail", text: "结尾不自然或疑似硬切" }),
    ]);
    const savedEnding =
      draft?.ending ||
      serverReview?.technical_confirmations?.ending_boundary ||
      "";
    endingSelect.value = savedEnding;
    endingSelect.disabled = !savedEnding;
    const endingHelper = create("small", {
      text: savedEnding
        ? "已载入上一轮人工确认；可重新播放后修改。"
        : "请先播放到最后 10 秒，工具才允许记录结尾判断。",
    });
    audioWasAuditioned(audio, endingSelect, endingHelper);
    const requirements = create("div", { className: "requirements" });
    const requirementSelects = new Map();
    candidate.requirements.forEach((requirement, index) => {
      const select = create("select", {
        className: "select",
        "aria-label": `${candidate.title}：${requirementLabel(requirement.label)}`,
      }, [
        create("option", { value: "", text: "N/A 尚未判断" }),
        create("option", { value: "3", text: "3 完全符合" }),
        create("option", { value: "2", text: "2 基本符合" }),
        create("option", { value: "1", text: "1 明显偏离" }),
        create("option", { value: "0", text: "0 不符合" }),
      ]);
      const saved =
        draft?.requirements?.[requirement.id] ??
        serverReview?.requirement_observations?.[requirement.id]?.value;
      select.value = saved === null || saved === undefined ? "" : String(saved);
      requirementSelects.set(requirement.id, select);
      requirements.append(
        create("div", { className: "requirement-row" }, [
          create("span", {
            className: "row-index",
            text: String(index + 1).padStart(2, "0"),
          }),
          create("div", {}, [
            create("strong", {
              text: `${requirementLabel(requirement.label)}${
                requirement.hard ? "（硬要求）" : ""
              }`,
            }),
            create("p", {
              text: `已声明值：${requirementSummary(requirement)}`,
            }),
          ]),
          select,
        ])
      );
    });
    if (!candidate.requirements.length) {
      requirements.append(
        emptyState(
          "该 Brief 没有可评估要求",
          "正式推荐会继续弃权；界面不会创建替代要求。"
        )
      );
    }
    const endingSummary = candidate.ending
      ? candidate.ending.classification === "active_audio_at_boundary" ||
        candidate.ending.classification === "likely_abrupt_boundary"
        ? "自动诊断发现边界处仍有活动音频，只能作为定位证据。"
        : `自动诊断：${endingLabel(
            candidate.ending.classification
          )}；仍由人确认。`
      : "尚未运行结尾诊断。";
    card.append(
      create("header", { className: "section-head" }, [
        create("h2", { className: "section-title" }, [
          create("span", { text: candidate.title }),
          create("small", {
            text: `${candidate.artifact_id} · ${formatDuration(
              candidate.measured_duration_s || candidate.platform_duration_s
            )}`,
          }),
        ]),
        statusChip(operationLabel(candidate.operation), "recorded"),
      ]),
      create("div", { className: "audio-card-body" }, [
        audio,
        create("div", { className: "audio-meta" }, [
          create("span", { text: "身份已解锁" }),
          create("span", { text: endingSummary }),
        ]),
      ]),
      create("div", { className: "form-grid" }, [
        create("div", { className: "field span-2" }, [
          create("label", { text: "人工确认结尾" }),
          endingSelect,
          endingHelper,
        ]),
      ]),
      requirements
    );
    formState.set(candidate.artifact_id, {
      candidate,
      endingSelect,
      requirementSelects,
    });
    return card;
  }

  function lyricsSection(context) {
    const briefs = new Map(context.briefs.map((item) => [item.id, item]));
    const targetBrief =
      context.briefs.find(
        (item) =>
          item.id === context.latest_review?.review_packet?.target_brief_id
      ) ||
      context.briefs[0];
    const lyrics = targetBrief?.lyrics || "";
    const sections = lyrics.includes("[")
      ? lyrics.split(/\s*(?=\[[^\]]+\])/)
      : lyrics.split(/\r?\n/);
    const lines = sections
      .map((line) => line.trim())
      .filter(Boolean);
    const analyses = context.lyric_analyses;
    if (!lines.length) {
      return section(
        "歌词行",
        "冻结内容",
        emptyState("没有已声明歌词", "当前 Brief 没有可显示的冻结歌词行。")
      );
    }
    const located = new Map();
    analyses.forEach((analysis) => {
      analysis.locations.forEach((location) => {
        if (!located.has(location.expected_text)) located.set(location.expected_text, location);
      });
    });
    const block = create(
      "div",
      { className: "lyric-block" },
      lines.map((line, index) => {
        const location = located.get(line);
        const text = location
          ? `${location.status} · ${
              location.start_s === null
                ? "没有可靠时间点"
                : `${formatTime(location.start_s)}–${formatTime(location.end_s)}`
            }`
          : "ASR 未运行；仅显示冻结原文";
        return create("div", { className: "lyric-line" }, [
          create("span", {
            className: "line-number",
            text: String(index + 1).padStart(2, "0"),
          }),
          create("div", {}, [
            create("p", { text: line }),
            create("small", { text }),
          ]),
          statusChip(location ? "标记疑问" : "待人工核对", "manual"),
        ]);
      })
    );
    return section(
      lyrics.includes("[") ? "歌词段落" : "歌词行",
      "已冻结",
      block,
      "ASR 仅作定位，任何判断都需人工确认"
    );
  }

  function namedView(context, navigate) {
    const draftKey = `song-eval:named:${context.project.id}`;
    let draft = {};
    try {
      draft = JSON.parse(localStorage.getItem(draftKey) || "{}");
    } catch {
      draft = {};
    }
    const formState = new Map();
    let submitPending = false;
    const serverReviews = latestReviewByArtifact(context);
    const page = create("div", { className: "page" });
    page.append(
      pageHeader({
        kicker: "具名复核 / NAMED REVIEW",
        title: "身份已解锁，判断由你完成",
        subtitle: [
          context.project.title,
          "自动分析只提供定位",
          "不提供版权或法律结论",
        ],
        actions: [
          button("返回总览", { variant: "quiet", onClick: () => navigate("overview") }),
        ],
      }),
      create("div", { className: "review-banner" }, [
        create("div", {}, [
          create("strong", {
            text: context.latest_review ? "已有复核记录" : "等待人工确认",
          }),
          create("p", {
            text:
              "需求符合、歌词语句、结尾边界和采集限制都必须由你在有声播放中确认；测量不能替代判断。",
          }),
        ]),
        statusChip(
          context.latest_review ? "已记录，可追加新版本" : "需要人工",
          context.latest_review ? "recorded" : "manual"
        ),
      ])
    );
    const candidateRoot = create("div");
    context.candidates.forEach((candidate) => {
      candidateRoot.append(
        candidateReviewCard(
          candidate,
          serverReviews.get(candidate.artifact_id),
          draft.artifacts?.[candidate.artifact_id],
          formState
        )
      );
    });
    page.append(
      section(
        "候选需求与技术复核",
        `${context.candidates.length} 条具名候选`,
        candidateRoot,
        "请先完成匿名盲听"
      ),
      lyricsSection(context)
    );
    const policyChecks = [
      [
        "priority",
        "我确认采用词典序优先级",
        "需求匹配 > 作品完成度 > 直接发布可行性 > 差异身份；各轴不相加。",
      ],
      [
        "abstain",
        "我确认关键未知时弃权",
        "关键证据未知、盲听无效或词典序仍然并列时，不强行推荐。",
      ],
      [
        "preservation",
        "我确认必须保留项需要通过",
        "若目标声明必须保留参考内容，缺少有效比对证据时继续弃权。",
      ],
    ];
    const policyBody = create("div", { style: "padding:6px 20px" });
    const checkNodes = new Map();
    policyChecks.forEach(([id, title, text]) => {
      const input = create("input", {
        type: "checkbox",
        checked: Boolean(draft.policy?.[id] ?? context.policy),
      });
      checkNodes.set(id, input);
      policyBody.append(
        create("label", { className: "check-row" }, [
          input,
          create("span", {}, [
            create("strong", { text: title }),
            create("small", { text }),
          ]),
        ])
      );
    });
    const maxNa = create("input", {
      className: "input",
      type: "number",
      min: "0",
      max: "1",
      step: "0.05",
      value:
        draft.policy?.maxNa ??
        context.policy?.max_na_ratio ??
        "0.25",
    });
    const status = create("div", { className: "form-status", role: "status" });
    const persistDraft = () => {
      const artifacts = {};
      for (const [artifactId, state] of formState) {
        artifacts[artifactId] = {
          ending: state.endingSelect.value,
          requirements: Object.fromEntries(
            [...state.requirementSelects].map(([id, select]) => [id, select.value])
          ),
        };
      }
      const policy = {
        ...Object.fromEntries([...checkNodes].map(([id, input]) => [id, input.checked])),
        maxNa: maxNa.value,
      };
      localStorage.setItem(draftKey, JSON.stringify({ artifacts, policy }));
    };
    page.addEventListener("change", persistDraft);
    const submit = async () => {
      if (submitPending) return;
      status.className = "form-status";
      if ([...checkNodes.values()].some((input) => !input.checked)) {
        status.classList.add("error");
        status.textContent = "请逐条确认三项发布规则；工具不会从页面行为推断你的同意。";
        return;
      }
      const maxNaValue = Number(maxNa.value);
      if (!Number.isFinite(maxNaValue) || maxNaValue < 0 || maxNaValue > 1) {
        status.classList.add("error");
        status.textContent = "N/A 上限必须在 0 到 1 之间。";
        return;
      }
      submitPending = true;
      const artifactReviews = [];
      for (const [artifactId, state] of formState) {
        const requirementObservations = {};
        for (const [requirementId, select] of state.requirementSelects) {
          requirementObservations[requirementId] = {
            criterion: requirementId,
            value: select.value === "" ? null : Number(select.value),
            evidence: "human confirmation in local named review",
          };
        }
        const technicalConfirmations = {};
        if (state.endingSelect.value) {
          technicalConfirmations.ending_boundary = state.endingSelect.value;
        }
        artifactReviews.push({
          artifact_id: artifactId,
          requirement_observations: requirementObservations,
          technical_confirmations: technicalConfirmations,
        });
      }
      status.textContent = "正在保存策略、人工复核并重新分析…";
      try {
        await api(`/projects/${encodeURIComponent(context.project.id)}/policy`, {
          method: "POST",
          body: JSON.stringify({
            confirm: true,
            max_na_ratio: maxNaValue,
            require_preservation: true,
          }),
        });
        await api(`/projects/${encodeURIComponent(context.project.id)}/reviews`, {
          method: "POST",
          body: JSON.stringify({
            project_id: context.project.id,
            artifact_reviews: artifactReviews,
          }),
        });
        const analysis = await api(
          `/projects/${encodeURIComponent(context.project.id)}/analysis`,
          {
            method: "POST",
            body: JSON.stringify({ review: null }),
          }
        );
        localStorage.removeItem(draftKey);
        status.classList.add("success");
        status.textContent = `已保存并重新分析：${
          STATUS_LABELS[analysis.recommendation.status] ||
          analysis.recommendation.status
        }。`;
        toast("复核证据已写入本地数据库。");
      } catch (error) {
        submitPending = false;
        status.classList.add("error");
        status.textContent = `保存失败：${error.message}`;
        return;
      }
      try {
        await navigate("overview", true, true);
      } catch {
        window.location.href = `/projects/${encodeURIComponent(
          context.project.id
        )}?view=overview`;
      }
    };
    page.append(
      create("section", { className: "section" }, [
        create("header", { className: "section-head" }, [
          create("h2", { className: "section-title" }, [
            create("span", { text: "发布决策规则" }),
            create("small", { text: "需要显式确认" }),
          ]),
          statusChip(context.policy ? "已有声明" : "尚未声明", context.policy ? "recorded" : "manual"),
        ]),
        policyBody,
        create("div", { className: "form-grid" }, [
          create("div", { className: "field" }, [
            create("label", { text: "N/A 证据上限" }),
            maxNa,
          ]),
          create("div", { className: "field" }, [
            create("label", { text: "固定轴顺序" }),
            create("input", {
              className: "input",
              readOnly: true,
              value: "需求匹配 > 完成度 > 发布可行性 > 差异身份",
            }),
          ]),
        ]),
        create("footer", { className: "form-footer" }, [
          status,
          button("保存复核并重新分析", {
            variant: "primary",
            onClick: submit,
          }),
        ]),
      ])
    );
    return page;
  }

  function referenceGraphic(duration) {
    const bars = Array.from({ length: 86 }, (_, index) => {
      const value =
        18 +
        Math.abs(
          Math.sin(index * 0.61) * 22 +
            Math.cos(index * 0.19) * 12
        );
      return create("span", {
        style: `height:${Math.min(56, value)}px`,
      });
    });
    return create("div", {
      className: "reference-timeline",
      role: "img",
      "aria-label": `参考段示意时间轴，记录时长 ${formatDuration(duration)}`,
    }, bars);
  }

  function renderPlanResult(result, target) {
    if (result.status !== "actionable" || !result.plan) {
      return create("div", { className: "warning-panel", style: "margin-top:18px" }, [
        create("h3", { text: "暂不推荐执行" }),
        create("p", {
          text:
            result.rationale?.map(localizePlanText).join("；") ||
            "当前证据不足。",
        }),
        create("p", {
          text: `建议回退：${
            candidateLabel(target) ||
            displayValue(result.suggested_fallback, "保留当前目标")
          }`,
        }),
      ]);
    }
    const plan = result.plan;
    const steps = create(
      "div",
      { className: "step-list" },
      plan.steps.map((step, index) =>
        create("label", { className: "step-row" }, [
          create("input", { type: "checkbox" }),
          create("span", {}, [
            create("strong", { text: localizePlanText(step) }),
            create("p", {
              text:
                index === 0
                  ? `编辑父级保持为 ${
                      candidateLabel(target) || plan.target_artifact_id
                    }。`
                  : "完成后由你决定是否继续；工具不操作 Suno。",
            }),
          ]),
          create("span", { className: "step-number", text: `步骤 ${index + 1}` }),
        ])
      )
    );
    return create("div", {}, [
      section(
        "替换段落（Replace Section）步骤",
        "真实计划输出",
        steps,
        `${plan.steps.length} 步`
      ),
      create("div", { className: "two-column" }, [
        section(
          "生成边界",
          "",
          create("div", { className: "plan-metrics" }, [
            create("div", { className: "plan-metric" }, [
              create("small", { text: "每批 TAKE 数" }),
              create("strong", { text: String(plan.takes_per_batch) }),
              create("p", { text: "不多不少" }),
            ]),
            create("div", { className: "plan-metric" }, [
              create("small", { text: "最大批次" }),
              create("strong", { text: String(plan.max_batches) }),
              create("p", {
                text: `共 ≤ ${plan.takes_per_batch * plan.max_batches} 个 take`,
              }),
            ]),
            create("div", { className: "plan-metric" }, [
              create("small", { text: "编辑父级 / 回退" }),
              create("strong", {
                text: candidateLabel(target) || plan.target_artifact_id,
              }),
              create("p", { text: "保持同一候选，不覆盖" }),
            ]),
            create("div", { className: "plan-metric" }, [
              create("small", { text: "工作面" }),
              create("strong", {
                text: plan.workflow_surface === "song_editor" ? "Song Editor" : plan.workflow_surface,
              }),
              create("p", {
                text: plan.studio_available ? "Studio 可用" : "无需 Studio",
              }),
            ]),
          ])
        ),
        create("div", { className: "warning-panel", style: "margin-top:18px" }, [
          create("h3", {
            text: plan.exact_retention_claimed
              ? "保留状态：有证据支持"
              : "保留状态：不声称精确复现",
          }),
          create("p", {
            text: `参考处理：${plan.source_rules
              .map((rule) => SOURCE_RULE_LABELS[rule] || rule)
              .join("；")}。`,
          }),
          create(
            "ul",
            { className: "plain-list" },
            plan.rejection_conditions.map((item) =>
              create("li", { text: localizePlanText(item) })
            )
          ),
          create("p", { text: localizePlanText(plan.credit_guardrail) }),
        ]),
      ]),
    ]);
  }

  function planView(context, navigate) {
    const page = create("div", { className: "page" });
    page.append(
      create("div", { className: "project-banner" }, [
        create("span", {
          text: `项目：${context.project.title}。参考段、目标候选和回退均从当前数据库读取。`,
        }),
        button("回到项目总览", {
          variant: "quiet",
          onClick: () => navigate("overview"),
        }),
      ]),
      pageHeader({
        kicker: "参考段 → SUNO 计划 / REFERENCE TO PLAN",
        title: context.references.length
          ? "参考段只作为证据"
          : "先登记参考段，再形成计划",
        subtitle: [
          `${context.references.length} 个参考段`,
          `${context.directives.length} 条保留指令`,
          "工具不上传、不生成、不消耗 credits",
        ],
      })
    );
    if (context.references.length) {
      context.references.forEach((reference) => {
        const directive = context.directives.find(
          (item) => item.reference_segment_id === reference.id
        );
        page.append(
          section(
            reference.title || "已登记参考段",
            `${formatDuration(reference.duration_s)} · ${
              directive
                ? preservationLabel(directive.preservation_intent)
                : "意图未提供"
            }`,
            create("div", { className: "reference-summary" }, [
              create("p", {
                className: "table-note",
                text: `来源记录：${
                  reference.source_artifact_title ||
                  reference.source_artifact_id
                }；区间 ${formatTime(reference.start_s)}–${formatTime(
                  reference.end_s
                )}。`,
              }),
              referenceGraphic(reference.duration_s),
            ]),
            "真实登记信息；波形区域仅表示时间范围"
          )
        );
      });
    }
    const targetOptions = context.candidates.map((candidate) =>
      create("option", {
        value: candidate.artifact_id,
        text: `${candidate.title} · ${formatDuration(
          candidate.measured_duration_s || candidate.platform_duration_s
        )}`,
      })
    );
    const referencePath = create("input", {
      className: "input",
      type: "text",
      placeholder: "/Users/…/reference-16s.wav",
      autocomplete: "off",
    });
    const referenceTarget = create("select", { className: "select" }, targetOptions);
    const referenceIntent = create("select", { className: "select" }, [
      create("option", { value: "structural_gesture", text: "只保留结构感觉" }),
      create("option", { value: "melody_rhythm", text: "旋律 / 节奏保留（需要证据）" }),
      create("option", { value: "exact_audio", text: "精确音频（需要证据）" }),
    ]);
    const start = create("input", {
      className: "input",
      type: "number",
      min: "0",
      step: "0.01",
      value: "0",
    });
    const end = create("input", {
      className: "input",
      type: "number",
      min: "0",
      step: "0.01",
      placeholder: "留空表示文件结尾",
    });
    const registrationStatus = create("div", {
      className: "form-status",
      role: "status",
    });
    let registrationPending = false;
    const register = async () => {
      if (registrationPending) return;
      registrationStatus.className = "form-status";
      if (!referencePath.value.trim()) {
        registrationStatus.classList.add("error");
        registrationStatus.textContent = "请输入本机参考音频的绝对路径。";
        return;
      }
      registrationPending = true;
      registrationStatus.textContent = "正在复制并登记参考证据…";
      try {
        await api(
          `/projects/${encodeURIComponent(context.project.id)}/references/register`,
          {
            method: "POST",
            body: JSON.stringify({
              target_artifact_id: referenceTarget.value,
              reference_path: referencePath.value.trim(),
              intent: referenceIntent.value,
              start_s: Number(start.value || 0),
              end_s: end.value === "" ? null : Number(end.value),
            }),
          }
        );
        registrationStatus.classList.add("success");
        registrationStatus.textContent = "参考段已登记；它没有被附加到任何生成事件。";
        toast("参考证据已登记。");
        window.setTimeout(() => window.location.reload(), 650);
      } catch (error) {
        registrationPending = false;
        registrationStatus.classList.add("error");
        registrationStatus.textContent = `登记失败：${error.message}`;
      }
    };
    page.append(
      create("section", { className: "section" }, [
        create("header", { className: "section-head" }, [
          create("h2", { className: "section-title" }, [
            create("span", { text: "登记本地参考证据" }),
            create("small", { text: "不会上传到 Suno" }),
          ]),
          statusChip("本地路径", "recorded"),
        ]),
        create("div", { className: "form-grid" }, [
          create("div", { className: "field span-2" }, [
            create("label", { text: "参考音频绝对路径" }),
            referencePath,
          ]),
          create("div", { className: "field" }, [
            create("label", { text: "目标候选 / 编辑父级" }),
            referenceTarget,
          ]),
          create("div", { className: "field" }, [
            create("label", { text: "保留意图" }),
            referenceIntent,
          ]),
          create("div", { className: "field" }, [
            create("label", { text: "起点（秒）" }),
            start,
          ]),
          create("div", { className: "field" }, [
            create("label", { text: "终点（秒，可留空）" }),
            end,
          ]),
        ]),
        create("footer", { className: "form-footer" }, [
          registrationStatus,
          button("登记参考段", { variant: "primary", onClick: register }),
        ]),
      ])
    );
    if (context.directives.length) {
      const directiveSelect = create(
        "select",
        { className: "select" },
        context.directives.map((directive) =>
          create("option", {
            value: directive.id,
            text: `${preservationLabel(directive.preservation_intent)} · ${
              directive.id
            }`,
          })
        )
      );
      const targetSelect = create(
        "select",
        { className: "select" },
        context.candidates.map((candidate) =>
          create("option", {
            value: candidate.artifact_id,
            text: `${candidate.title} · ${formatDuration(
              candidate.measured_duration_s || candidate.platform_duration_s
            )}`,
          })
        )
      );
      const firstDirective = context.directives[0];
      const syncDirectiveTarget = () => {
        const selectedDirective = context.directives.find(
          (item) => item.id === directiveSelect.value
        );
        const selectedTargetId = selectedDirective
          ? directiveTargetId(selectedDirective)
          : null;
        if (selectedTargetId) targetSelect.value = selectedTargetId;
        targetSelect.disabled = Boolean(selectedTargetId);
        targetSelect.title = targetSelect.disabled
          ? "目标候选在登记此保留指令时已固定"
          : "旧指令未记录目标，可在生成计划前明确选择";
      };
      syncDirectiveTarget();
      directiveSelect.addEventListener("change", syncDirectiveTarget);
      const prompt = create("textarea", {
        className: "textarea",
        placeholder:
          "描述要保留的结构动作，例如：收束桥段、留一拍呼吸、立即进入副歌。不要要求复制具体旋律。",
      });
      const lyrics = create("textarea", {
        className: "textarea",
        placeholder: "可选：冻结歌词原文；工具不会替你改写。",
      });
      const tier = create("select", { className: "select" }, [
        create("option", { value: "pro", text: "Pro（非 Studio）" }),
        create("option", { value: "premier", text: "Premier" }),
        create("option", { value: "unknown", text: "订阅未知" }),
      ]);
      const studio = create("input", { type: "checkbox" });
      const planStatus = create("div", { className: "form-status", role: "status" });
      const resultRoot = create("div");
      let planPending = false;
      const buildPlan = async () => {
        if (planPending) return;
        planStatus.className = "form-status";
        if (!prompt.value.trim()) {
          planStatus.classList.add("error");
          planStatus.textContent = "请用文字描述结构动作；参考音频本身不会成为 Sample。";
          return;
        }
        planPending = true;
        planStatus.textContent = "正在按当前能力边界生成确定性计划…";
        try {
          const result = await api(
            `/projects/${encodeURIComponent(context.project.id)}/suno-plan`,
            {
              method: "POST",
              body: JSON.stringify({
                directive_id: directiveSelect.value,
                target_artifact_id: targetSelect.value,
                prompt: prompt.value.trim(),
                lyrics_excerpt: lyrics.value.trim() || null,
                subscription_tier: tier.value,
                studio_available: studio.checked,
              }),
            }
          );
          planStatus.classList.add("success");
          planStatus.textContent =
            result.status === "actionable"
              ? "计划已生成；执行仍由你在 Suno 中逐步完成。"
              : "当前证据不足，工具明确弃权。";
          resultRoot.replaceChildren(
            renderPlanResult(result, findCandidate(context, targetSelect.value))
          );
          planPending = false;
        } catch (error) {
          planPending = false;
          planStatus.classList.add("error");
          planStatus.textContent = `计划失败：${error.message}`;
        }
      };
      page.append(
        create("section", { className: "section" }, [
          create("header", { className: "section-head" }, [
            create("h2", { className: "section-title" }, [
              create("span", { text: "生成 Suno 操作计划" }),
              create("small", { text: "确定性规则，不调用 LLM" }),
            ]),
            statusChip("不作为 Sample 上传", "manual"),
          ]),
          create("div", { className: "form-grid" }, [
            create("div", { className: "field" }, [
              create("label", { text: "保留指令" }),
              directiveSelect,
            ]),
            create("div", { className: "field" }, [
              create("label", { text: "编辑父级 / 回退候选" }),
              targetSelect,
            ]),
            create("div", { className: "field span-2" }, [
              create("label", { text: "结构动作描述" }),
              prompt,
            ]),
            create("div", { className: "field span-2" }, [
              create("label", { text: "冻结歌词（可选）" }),
              lyrics,
            ]),
            create("div", { className: "field" }, [
              create("label", { text: "订阅层级" }),
              tier,
            ]),
            create("label", { className: "check-row" }, [
              studio,
              create("span", {}, [
                create("strong", { text: "Studio 可用" }),
                create("small", {
                  text: "未勾选时按 Pro / 非 Studio 的 Song Editor 路径规划。",
                }),
              ]),
            ]),
          ]),
          create("footer", { className: "form-footer" }, [
            planStatus,
            button("生成计划", { variant: "primary", onClick: buildPlan }),
          ]),
        ]),
        resultRoot
      );
    } else {
      page.append(
        create("div", { className: "warning-panel", style: "margin-top:18px" }, [
          create("h3", { text: "尚无可用保留指令" }),
          create("p", {
            text:
              "登记参考段后才能生成计划。结构感觉可以规划；精确音频、旋律或节奏保留若缺少有效比对证据，将明确弃权。",
          }),
        ])
      );
    }
    return page;
  }

  async function bootWorkspace() {
    const projectId = bootstrap.project_id;
    let context = await api(
      `/projects/${encodeURIComponent(projectId)}/workspace-context`
    );
    const params = new URLSearchParams(window.location.search);
    let activeView =
      params.get("view") ||
      bootstrap.initial_view ||
      "overview";
    if (!["overview", "named", "plan"].includes(activeView)) activeView = "overview";
    const navigate = async (view, push = true, refresh = false) => {
      if (refresh) {
        setBusy(true);
        context = await api(
          `/projects/${encodeURIComponent(projectId)}/workspace-context`
        );
      }
      activeView = view;
      if (push) {
        const url = new URL(window.location.href);
        url.pathname = `/projects/${encodeURIComponent(projectId)}`;
        url.searchParams.set("view", view);
        history.pushState({ view }, "", url);
      }
      render();
      main.focus({ preventScroll: true });
      window.scrollTo({ top: 0, behavior: "auto" });
    };
    const render = () => {
      document.body.classList.remove("blind-page");
      const blindUrl = context.latest_listening?.review_url || null;
      renderNavigation({
        active: activeView,
        project: context.project,
        blindUrl,
        onWorkspaceView: (view) => navigate(view),
      });
      const view =
        activeView === "named"
          ? namedView(context, navigate)
          : activeView === "plan"
            ? planView(context, navigate)
            : overviewView(context, navigate);
      main.replaceChildren(view);
      document.title = `${context.project.title} · ${
        activeView === "named"
          ? "具名复核"
          : activeView === "plan"
            ? "参考段计划"
            : "项目总览"
      }`;
      setBusy(false);
    };
    window.addEventListener("popstate", (event) => {
      const view =
        event.state?.view ||
        new URLSearchParams(window.location.search).get("view") ||
        bootstrap.initial_view ||
        "overview";
      if (["overview", "named", "plan"].includes(view)) {
        activeView = view;
        render();
      }
    });
    render();
  }

  function blindPlayer(label) {
    const audio = create("audio", { preload: "metadata" });
    const canvas = create("canvas", {
      className: "waveform",
      role: "img",
      "aria-label": `${label} 刺激音频波形`,
    });
    const playhead = create("span", { className: "playhead", "aria-hidden": "true" });
    const wrap = create("div", { className: "waveform-wrap" }, [canvas, playhead]);
    const playButton = create("button", {
      className: "blind-button primary",
      type: "button",
      "aria-label": `播放 ${label}`,
    }, [
      create("span", { html: ICONS.play }),
      create("span", { text: "播放" }),
    ]);
    const rewind = create("button", {
      className: "blind-button",
      type: "button",
      text: "−5s",
      "aria-label": `${label} 后退 5 秒`,
    });
    const forward = create("button", {
      className: "blind-button",
      type: "button",
      text: "+5s",
      "aria-label": `${label} 前进 5 秒`,
    });
    const time = create("span", { className: "timecode", text: "00:00 / 00:00" });
    const card = create("section", { className: "blind-player" }, [
      create("header", { className: "blind-player-head" }, [
        create("div", { className: "blind-label" }, [
          create("span", { text: label }),
          create("span", { text: `刺激 ${label}` }),
        ]),
        create("small", { text: "身份隐藏 · 响度按规则处理" }),
      ]),
      wrap,
      create("div", { className: "player-controls" }, [
        playButton,
        rewind,
        forward,
        time,
      ]),
      audio,
    ]);
    audio.hidden = true;
    const update = () => {
      const duration = Number.isFinite(audio.duration) ? audio.duration : 0;
      const progress = duration ? Math.min(1, audio.currentTime / duration) : 0;
      playhead.style.left = `${progress * 100}%`;
      time.textContent = `${formatTime(audio.currentTime)} / ${formatTime(duration)}`;
      const playing = !audio.paused && !audio.ended;
      playButton.replaceChildren(
        create("span", { html: playing ? ICONS.pause : ICONS.play }),
        create("span", { text: playing ? "暂停" : "播放" })
      );
      playButton.setAttribute("aria-label", `${playing ? "暂停" : "播放"} ${label}`);
    };
    playButton.addEventListener("click", async () => {
      try {
        if (audio.paused) await audio.play();
        else audio.pause();
      } catch (error) {
        toast(`无法播放音频：${error.message}`, "error");
      }
      update();
    });
    rewind.addEventListener("click", () => {
      audio.currentTime = Math.max(0, audio.currentTime - 5);
      update();
    });
    forward.addEventListener("click", () => {
      audio.currentTime = Math.min(
        Number.isFinite(audio.duration) ? audio.duration : audio.currentTime + 5,
        audio.currentTime + 5
      );
      update();
    });
    wrap.addEventListener("click", (event) => {
      if (!Number.isFinite(audio.duration)) return;
      const bounds = wrap.getBoundingClientRect();
      const ratio = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
      audio.currentTime = ratio * audio.duration;
      update();
    });
    audio.addEventListener("timeupdate", update);
    audio.addEventListener("loadedmetadata", update);
    audio.addEventListener("play", update);
    audio.addEventListener("pause", update);
    audio.addEventListener("ended", update);
    return { card, audio, canvas, update, label };
  }

  async function bootBlind() {
    document.body.classList.add("blind-page");
    const sessionId = bootstrap.session_id;
    const data = await api(
      `/listening-sessions/${encodeURIComponent(sessionId)}`
    );
    const project = {
      id: data.project_id,
      title: "盲听进行中",
    };
    renderNavigation({
      active: "blind",
      project,
      blindUrl: data.review_url,
    });
    if (data.review_status?.valid) {
      main.replaceChildren(
        create("div", { className: "blind-workspace blind-complete" }, [
          create("p", { className: "page-kicker", text: "BLIND ROUND COMPLETE" }),
          create("h1", { text: "本轮匿名盲听已完成" }),
          create("p", {
            text:
              "位置交换与校准探针已经通过，听感证据已写入本地数据库。候选身份仍保持隐藏。",
          }),
          create("a", {
            className: "blind-button primary",
            href: data.project_url,
            text: "进入具名复核",
          }),
        ])
      );
      setBusy(false);
      return;
    }
    if (!data.trials.length) {
      main.replaceChildren(
        create("div", { className: "blind-workspace blind-complete" }, [
          create("p", { className: "page-kicker", text: "BLIND ROUND UNAVAILABLE" }),
          create("h1", { text: "本轮没有可比较的真实片段" }),
          create("p", {
            text:
              "至少需要两个带本地音频、且分析产生可比差异热点的候选。这个空轮次不会被视为有效证据。",
          }),
          create("a", {
            className: "blind-button primary",
            href: `/projects/${encodeURIComponent(data.project_id)}`,
            text: "返回项目总览",
          }),
        ])
      );
      setBusy(false);
      return;
    }
    const storageKey = `song-eval:blind:${sessionId}`;
    let stored = {};
    try {
      stored = JSON.parse(sessionStorage.getItem(storageKey) || "{}");
    } catch {
      stored = {};
    }
    const responses = data.trials.map((trial) => ({
      trial_id: trial.trial_id,
      outcome: stored[trial.trial_id]?.outcome || null,
      reason_tags: Array.isArray(stored[trial.trial_id]?.reason_tags)
        ? stored[trial.trial_id].reason_tags
        : [],
      comment: stored[trial.trial_id]?.comment || "",
    }));
    const storedCurrent = Number(stored.__current);
    let current = Number.isFinite(storedCurrent)
      ? Math.max(0, Math.min(data.trials.length - 1, Math.floor(storedCurrent)))
      : 0;
    let activeSide = "left";
    let submitted = false;
    const left = blindPlayer("A");
    const right = blindPlayer("B");
    const title = create("h1", { text: "匿名盲听工作台" });
    const subtitle = create("p", {
      text: "候选身份、原始顺序、探针类型、谱系和完整时长均已隐藏。",
    });
    const progressLabel = create("strong");
    const progressBar = create("span");
    const progress = create("div", { className: "blind-progress" }, [
      progressLabel,
      create("div", { className: "blind-progress-track" }, [progressBar]),
    ]);
    const outcomeRoot = create("div", { className: "outcome-grid" });
    const reasonRoot = create("div", { className: "reason-grid" });
    const notes = create("textarea", {
      className: "blind-textarea",
      maxlength: "2000",
      placeholder: "可选：记录具体听感和时间位置。不要猜测版本或生成参数。",
      "aria-label": "当前对比的听感备注",
    });
    const status = create("div", {
      className: "blind-status",
      role: "status",
    });
    const previous = create("button", {
      className: "blind-button",
      type: "button",
      html: `${ICONS.back}<span>上一组</span>`,
    });
    const next = create("button", {
      className: "blind-button",
      type: "button",
      html: `<span>下一组</span>${ICONS.forward}`,
    });
    const submit = create("button", {
      className: "blind-button primary",
      type: "button",
      text: "提交整轮结果",
    });
    const save = () => {
      const payload = Object.fromEntries(
        responses.map((item) => [
          item.trial_id,
          {
            outcome: item.outcome,
            reason_tags: item.reason_tags,
            comment: item.comment,
          },
        ])
      );
      payload.__current = current;
      sessionStorage.setItem(storageKey, JSON.stringify(payload));
    };
    const pauseAll = () => {
      left.audio.pause();
      right.audio.pause();
    };
    const setActive = (side) => {
      activeSide = side;
      left.card.classList.toggle("active", side === "left");
      right.card.classList.toggle("active", side === "right");
    };
    const renderTrial = (reloadAudio = true) => {
      const trial = data.trials[current];
      const response = responses[current];
      progressLabel.textContent = `对比 ${current + 1} / ${data.trials.length}`;
      const completed = responses.filter((item) => item.outcome).length;
      progressBar.style.width = `${(completed / data.trials.length) * 100}%`;
      if (reloadAudio) {
        pauseAll();
        left.audio.src = trial.left.media_url;
        right.audio.src = trial.right.media_url;
        left.audio.load();
        right.audio.load();
        left.canvas.setAttribute("aria-label", "正在读取刺激 A 的真实波形");
        right.canvas.setAttribute("aria-label", "正在读取刺激 B 的真实波形");
        requestAnimationFrame(() => {
          drawWaveform(left.canvas, trial.left.media_url, {
            color: "rgba(109,155,255,.78)",
            errorColor: "rgba(255,255,255,.15)",
            dense: true,
          });
          drawWaveform(right.canvas, trial.right.media_url, {
            color: "rgba(183,190,205,.68)",
            errorColor: "rgba(255,255,255,.15)",
            dense: true,
          });
        });
      }
      outcomeRoot.replaceChildren();
      [
        ["a", "A 更符合"],
        ["b", "B 更符合"],
        ["tie", "听不出差别"],
        ["n/a", "无法判断"],
      ].forEach(([value, label]) => {
        outcomeRoot.append(
          create("button", {
            className: `outcome-button${response.outcome === value ? " selected" : ""}`,
            type: "button",
            text: label,
            "aria-pressed": response.outcome === value ? "true" : "false",
            on: {
              click: () => {
                response.outcome = value;
                status.textContent = "";
                save();
                renderTrial(false);
              },
            },
          })
        );
      });
      reasonRoot.replaceChildren();
      data.reason_tags.forEach((tag) => {
        const input = create("input", {
          type: "checkbox",
          value: tag,
          checked: response.reason_tags.includes(tag),
        });
        input.addEventListener("change", () => {
          response.reason_tags = [...reasonRoot.querySelectorAll("input:checked")].map(
            (node) => node.value
          );
          save();
        });
        reasonRoot.append(
          create("label", { className: "reason-check" }, [
            input,
            create("span", { text: REASON_LABELS[tag] || tag }),
          ])
        );
      });
      notes.value = response.comment;
      previous.disabled = current === 0;
      next.disabled = current === data.trials.length - 1;
      setActive(activeSide);
    };
    notes.addEventListener("input", () => {
      responses[current].comment = notes.value;
      save();
    });
    previous.addEventListener("click", () => {
      responses[current].comment = notes.value;
      current = Math.max(0, current - 1);
      save();
      renderTrial();
    });
    next.addEventListener("click", () => {
      responses[current].comment = notes.value;
      current = Math.min(data.trials.length - 1, current + 1);
      save();
      renderTrial();
    });
    submit.addEventListener("click", async () => {
      if (submitted) {
        window.location.href = data.project_url;
        return;
      }
      responses[current].comment = notes.value;
      const missing = responses.findIndex((item) => !item.outcome);
      if (missing >= 0) {
        current = missing;
        status.className = "blind-status error";
        status.textContent = `还有未完成的对比，已跳转到第 ${missing + 1} 组。`;
        save();
        renderTrial();
        return;
      }
      status.className = "blind-status";
      status.textContent = "正在提交并校验位置交换与校准探针…";
      submit.disabled = true;
      try {
        const result = await api(data.response_url, {
          method: "POST",
          body: JSON.stringify({
            responses: responses.map((item) => ({
              trial_id: item.trial_id,
              outcome: item.outcome,
              reason_tags: item.reason_tags,
              comment: item.comment || null,
            })),
          }),
        });
        if (result.valid) {
          sessionStorage.removeItem(storageKey);
          status.className = "blind-status success";
          status.textContent = "整轮校验通过，听感证据已保存。";
          submit.textContent = "返回项目复核";
          submit.disabled = false;
          submitted = true;
        } else {
          status.className = "blind-status error";
          status.textContent = `本轮未通过：${(result.failures || []).join("；")}`;
          submit.disabled = false;
        }
      } catch (error) {
        status.className = "blind-status error";
        status.textContent = `提交失败：${error.message}`;
        submit.disabled = false;
      }
    });
    const workspace = create("div", { className: "blind-workspace" }, [
      create("header", { className: "blind-head" }, [
        create("div", {}, [title, subtitle]),
        progress,
      ]),
      create("div", { className: "blind-notice" }, [
        create("span", {}, [
          create("strong", { text: "身份隐藏" }),
          " · 响度已按规则处理 · 波形由当前刺激音频生成",
        ]),
        create("span", {
          text: "请使用同一设备和系统音量；若内容一致而仅响度不同，请选“听不出差别”",
        }),
      ]),
      create("div", { className: "blind-players" }, [left.card, right.card]),
      create("div", { className: "judgment-grid" }, [
        create("section", { className: "judgment-panel" }, [
          create("header", { className: "judgment-head" }, [
            create("strong", { text: "当前判断" }),
            create("small", { text: "必须选择一项" }),
          ]),
          outcomeRoot,
          create("header", { className: "judgment-head" }, [
            create("strong", { text: "听辨依据" }),
            create("small", { text: "可多选，也可以不选" }),
          ]),
          reasonRoot,
        ]),
        create("section", { className: "judgment-panel" }, [
          create("header", { className: "judgment-head" }, [
            create("strong", { text: "听感备注" }),
            create("small", { text: "最多 2000 字" }),
          ]),
          create("div", { className: "blind-notes" }, [notes]),
        ]),
      ]),
    ]);
    const footer = create("footer", { className: "blind-footer" }, [
      create("div", { className: "blind-footer-nav" }, [previous, next]),
      create("div", { className: "shortcut-help" }, [
        create("div", { text: "Space 播放 / 暂停 · L 选 A · N 选 B" }),
        create("div", { text: "A / B 选择结果 · ← / → 跳转 5 秒" }),
      ]),
      status,
      create("div", { className: "blind-footer-actions" }, [submit]),
    ]);
    main.replaceChildren(workspace, footer);
    left.card.addEventListener("click", () => setActive("left"));
    right.card.addEventListener("click", () => setActive("right"));
    document.addEventListener("keydown", (event) => {
      if (
        event.isComposing ||
        event.ctrlKey ||
        event.metaKey ||
        event.altKey ||
        event.shiftKey
      ) {
        return;
      }
      const target = event.target;
      if (
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement ||
        (event.key === " " &&
          target instanceof HTMLElement &&
          target.closest("button, a[href]"))
      ) {
        return;
      }
      const active = activeSide === "left" ? left : right;
      if (event.key === " ") {
        event.preventDefault();
        if (active.audio.paused) {
          active.audio.play().catch((error) => {
            toast(`无法播放音频：${error.message}`, "error");
          });
        }
        else active.audio.pause();
      } else if (event.key === "ArrowLeft") {
        event.preventDefault();
        active.audio.currentTime = Math.max(0, active.audio.currentTime - 5);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        active.audio.currentTime = Math.min(
          Number.isFinite(active.audio.duration) ? active.audio.duration : Infinity,
          active.audio.currentTime + 5
        );
      } else if (event.key.toLowerCase() === "l") {
        setActive("left");
      } else if (event.key.toLowerCase() === "n") {
        setActive("right");
      } else if (event.key.toLowerCase() === "a") {
        responses[current].outcome = "a";
        save();
        renderTrial(false);
      } else if (event.key.toLowerCase() === "b") {
        responses[current].outcome = "b";
        save();
        renderTrial(false);
      }
    });
    renderTrial();
    setBusy(false);
  }

  async function boot() {
    try {
      if (bootstrap.page === "projects") await bootProjectList();
      else if (bootstrap.page === "blind") await bootBlind();
      else await bootWorkspace();
    } catch (error) {
      renderError(error);
    }
  }

  boot();
})();
