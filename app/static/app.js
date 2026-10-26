/* 豆包 30 天会员 · 扫码领取 —— 前端逻辑（R56：实时状态 + 防抖动） */
(() => {
  const $ = (id) => document.getElementById(id);
  const KEY = new URLSearchParams(location.search).get("k") || "";
  // 注意：末尾必须带 ? 或 &，否则 "/api/state" + q + "t=..." 会拼成非法 URL
  const q = KEY ? ("?k=" + encodeURIComponent(KEY) + "&") : "?";

  // ★R302_FETCH_T：所有轮询/提交都带兜底超时。任何一次请求挂死都不能把页面循环拖停（「卡在进度条不动」的另一半原因）。
  function fetchT(url, opts, ms) {
    const o = Object.assign({}, opts || {});
    let ctl = null, tid = null;
    try { if (typeof AbortController !== "undefined") ctl = new AbortController(); } catch (e) { ctl = null; }
    if (ctl) {
      o.signal = ctl.signal;
      tid = setTimeout(() => { try { ctl.abort(); } catch (e) {} }, ms || 15000);
    }
    const clear = () => { if (tid) { clearTimeout(tid); tid = null; } };
    try { return fetch(url, o).then((r) => { clear(); return r; }, (e) => { clear(); throw e; }); }
    catch (e) { clear(); throw e; }
  }

  // ★R283_BUILD_WATCH：本页加载的 app.js 版本（?v=mtime+size），用于发现服务端已热更新
  const MY_BUILD = (() => {
    try {
      const s = Array.from(document.scripts).find((x) => /\/app\.js(\?|$)/.test(x.src));
      const mm = s && /[?&]v=([^&]+)/.exec(s.src);
      return mm ? mm[1] : "";
    } catch (e) { return ""; }
  })();
  let buildReloaded = false;

  const STATUS = {
    idle:      { text: "准备中",         cls: "pill--wait" },
    waiting:   { text: "等待扫码",       cls: "pill--wait" },
    scanned:   { text: "已扫描，请确认",  cls: "pill--scan" },
    verifying: { text: "权益核验中",     cls: "pill--run"  },
    success:   { text: "领取成功",       cls: "pill--ok"   },
    fail:      { text: "本次未到账",     cls: "pill--bad"  },
    error:     { text: "出错了",         cls: "pill--bad"  },
  };
  const DONE = ["success", "fail", "error"];

  let busy = false, toastTimer = null, timer = null, fails = 0, stopPoll = false;
  // 上一次渲染的"指纹"：只有变化才动 DOM，避免每次轮询都重排 → 页面抖动
  const m = { qr: null, qrSeq: 0, mask: null, scan: null, stamp: null, pill: "", sub: "", acct: "", cd: "", bar: -1, banner: "", phNum: null, phCC: "CN", qrCC: "", subwrap: null };
  // ---- R57 手机号领取：独立状态（与扫码轮询互不干扰）----
  const CN_PHONE = /^1[3-9]\d{9}$/;
  let phBusy = false, phLeft = 0, phTick = null, phOkNum = "", phNeedQrHint = false;   // ★R288_LOGIN_ONCE 同号成功一次后不再重复提交
  let phLockUntil = 0;   // ★R276：1203 限错后的前端冷却截止时间
  // ★R322_PERSIST_LOCK：1203 冷却写 localStorage（按 国家+号码），刷新/重开页面不再归零。
  // 依据：DEMO0017 19:21~19:41 连续 6 次 1203 —— 每次刷新页面 phLockUntil 就丢了，用户又点，官方罚时越滚越长。
  const PH_LOCK_MS = 180000;
  function phLockKey() {
    try {
      const d = (phE164() || "").replace(/\D/g, "");
      return "wzPlk_" + (m.phCC || "CN") + "_" + d;
    } catch (e) { return "wzPlk_"; }
  }
  function phLockSave(ms) {
    phLockUntil = Date.now() + Math.max(1000, ms || PH_LOCK_MS);
    try { localStorage.setItem(phLockKey(), String(phLockUntil)); } catch (e) {}
  }
  function phLockSync() {
    try {
      const k = phLockKey();
      const v = parseFloat(localStorage.getItem(k) || "0");
      if (!v || v < Date.now()) { if (v) localStorage.removeItem(k); return phLockUntil; }
      if (v > phLockUntil) phLockUntil = v;
    } catch (e) {}
    return phLockUntil;
  }
  function phLockArm() {
    try { phPaint(); } catch (e) {}
    const left = phLockUntil - Date.now();
    if (left > 0) setTimeout(() => { try { phPaint(); } catch (e) {} }, left + 300);
  }
  // ★R424_CAP_PERSIST：风控失败后的 60s 冷却写 localStorage（按国家+号码），刷新/重开不再立刻连点
  let phCapUntil = 0;
  function phCapKey() {
    try {
      const d = (phE164() || "").replace(/\D/g, "");
      return "wzCap_" + (m.phCC || "CN") + "_" + d;
    } catch (e) { return "wzCap_"; }
  }
  function phCapSave(ms) {
    phCapUntil = Date.now() + Math.max(1000, ms || 60000);
    try { localStorage.setItem(phCapKey(), String(phCapUntil)); } catch (e) {}
  }
  function phCapClear() {
    phCapUntil = 0;
    try { localStorage.removeItem(phCapKey()); } catch (e) {}
  }
  function phCapSync() {
    try {
      const k = phCapKey();
      const v = parseFloat(localStorage.getItem(k) || "0");
      if (!v || v < Date.now()) { if (v) localStorage.removeItem(k); return 0; }
      phCapUntil = v;
      const left = Math.ceil((v - Date.now()) / 1000);
      if (left > 0 && !phBusy) phStartCd(left);
    } catch (e) {}
    return phCapUntil;
  }
  let phMsgTs = 0;      // ★R299_MSG_TTL：提示文案的时间戳（旧红字超时自动降级）
  let phMsgNum = "";     // ★R282_MSG_BIND：当前提示文案对应的号码（改号/自动填充后自动清除旧文案）

  function toast(msg) {
    const t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove("show"), 2600);
  }
  function setTxt(id, v) { const e = $(id); if (e && e.textContent !== v) e.textContent = v; }
  function setCls(id, v) { const e = $(id); if (e && e.className !== v) e.className = v; }

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, c =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  // 地区一致性：账号地区 + 出口地区（不一致时标黄提醒）
  function regionChip(s) {
    // ★R84-Q：出口优先显示**真实探测到**的国家（exit_cc），没探到才退回「指定地区」（region）
    const acct = (s.acct_iso || "").toUpperCase(), out = (s.exit_cc || s.region || "").toUpperCase();
    if (!acct && !out) return "";
    const mis = (acct && out && acct !== out);
    const unknown = (!acct && !!out);
    return '<div class="chip' + ((mis || unknown) ? " warn" : " ok") + '"><span>地区</span><b>账号 ' +
      esc(acct || "未识别") + " ｜ 出口 " + esc(out || "?") +
      (mis ? "（不一致）" : (unknown ? "（未能识别账号地区，可在扫码前手动选择）" : "")) + "</b></div>";
  }

  // ===== R75：治「页面显示失败、实际已到账」=====
  let lastSnap = null, rcState = "", sawPending = false, fastUntil = 0, progTxt = "";
  let coolUntil = 0;   // ★R86c：被限速（429）后的退避截止时间，别继续捶服务器

  function pick() {
    for (let i = 0; i < arguments.length; i++) {
      const v = arguments[i];
      if (v !== undefined && v !== null && v !== "") return v;
    }
    return "";
  }

  // 时间戳容错：秒 / 毫秒 / ISO 字符串；认不出来就原样显示（绝不编造）
  // ★R336_TZ_BJ：页面写的是「北京」，就必须按 UTC+8 算 —— 旧代码用浏览器本地时区，
  //   境外代理/买家用自己时区打开时，会把当地时分当成北京时间显示（爷爷 10-19 报的时区错）。
  //   已经是「2026-11-18 17:03」这种北京时间字符串则原样返回，不再做二次换算。
  function fmtTime(v) {
    if (v === undefined || v === null || v === "") return "";
    if (typeof v === "string") {
      const s0 = v.trim();
      if (/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?$/.test(s0)) return s0.substring(0, 16).replace("T", " ");
    }
    let ms = null;
    if (typeof v === "number" && isFinite(v)) ms = (v < 1e11) ? v * 1000 : v;
    else {
      const s0 = String(v).trim();
      if (/^\d+(\.\d+)?$/.test(s0)) { const n = parseFloat(s0); ms = (n < 1e11) ? n * 1000 : n; }
      else { const t = Date.parse(s0); if (isNaN(t)) return s0; ms = t; }
    }
    const dt = new Date(ms + 8 * 3600 * 1000);
    if (isNaN(dt.getTime())) return String(v);
    const p = (n) => (n < 10 ? "0" : "") + n;
    return dt.getUTCFullYear() + "-" + p(dt.getUTCMonth() + 1) + "-" + p(dt.getUTCDate()) + " " +
      p(dt.getUTCHours()) + ":" + p(dt.getUTCMinutes());
  }

  // ★R704：到期时间不能只显示「月-日」——线上截图出现过
  //   「到期(北京) 标准套餐 10-20 → 10-20」：既看不出有没有变化，也看不出几点到期。
  //   现在统一「月-日 时:分」（跨年补年份）；领取前后一样就不再画「→」，
  //   并标注「已过期」或「剩 N 天」。
  function bjNowStr() {
    const d = new Date(Date.now() + 8 * 3600 * 1000);
    return d.toISOString().slice(0, 16).replace("T", " ");
  }
  function expInfo(v, nowStr) {
    const t = String(v == null ? "" : v).trim();
    const m = t.match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2}))?/);
    if (!m) return { text: t, head: t, raw: t };   // 认不出的格式就原样显示，绝不丢信息
    const hm = m[4] ? (m[4] + ":" + m[5]) : "";
    const norm = m[1] + "-" + m[2] + "-" + m[3] + " " + (hm || "00:00");
    const head = (nowStr.slice(0, 4) === m[1] ? (m[2] + "-" + m[3])
                                               : (m[1] + "-" + m[2] + "-" + m[3])) + (hm ? " " + hm : "");
    let tail = "";
    const ms = Date.parse(norm.replace(" ", "T") + "Z");
    const nowMs = Date.parse(nowStr.replace(" ", "T") + "Z");
    if (!isNaN(ms) && !isNaN(nowMs)) {
      const days = Math.ceil((ms - nowMs) / 86400000);
      if (days <= 0) tail = "（已过期）";
      else if (days <= 60) tail = "（剩 " + days + " 天）";
    }
    return { text: head + tail, head: head, raw: norm };
  }
  // ★R82-L：uid / 到期时间变化
  // ★R702d：skipExp=true 时不再画「到期(北京)」—— 成功横幅已经有「到期时间(北京)」
  function idChips(s, d, skipExp) {
    const uid = pick(s.uid, d.uid, "");
    const bm = pick(s.before_member, d.before_member, "");
    const be = pick(s.before_expiry, d.before_expiry, "");
    const am = pick(s.after_member, d.after_member, "");
    const ae = pick(s.after_expiry, d.after_expiry, "");
    const out = [];
    if (uid) out.push('<div class="chip"><span>UID</span><b>' + esc(uid) + '</b></div>');
    if (!skipExp && (am || ae || bm || be)) {
      const nowStr = bjNowStr();
      const bi = be ? expInfo(be, nowStr) : null;
      const ai = ae ? expInfo(ae, nowStr) : null;
      const nm = (am || bm || "").trim();
      let val;
      if (bi && ai && bi.raw !== ai.raw) {
        val = (nm ? nm + " " : "") + bi.head + " → " + ai.text;   // 真的有变化才画箭头
      } else {
        const one = ai || bi;
        val = (nm ? nm + " " : "") + (one ? one.text : "—");
      }
      // 已过期就不要再用绿色（绿色 = 到账/正常），改黄色提醒
      const _exp = !!((ai || bi) && (ai || bi).text.indexOf("已过期") >= 0);
      out.push('<div class="chip ' + (_exp ? "warn" : "ok") + '"><span>到期(北京)</span><b>' + esc(val) + '</b></div>');
    }
    return out.join("");
  }

  // 复查进度：已复查 N/M 次，约 X 秒后再查（字段缺失就退回一句白）
  function progText(s, d) {
    const tries = Number(pick(s.late_tries, d.late_tries, 0)) || 0;
    const total = Number(pick(s.late_total, d.late_total, 0)) || 0;
    const nextIn = Number(pick(s.late_next_in, d.late_next_in, 0)) || 0;
    if (!tries) return "本页会自动复查";
    return "已复查 " + tries + (total ? "/" + total : "") + " 次" +
      (nextIn > 0 ? ("，约 " + nextIn + " 秒后再查") : "，正在复查…");
  }

  function rcHtml() {
    if (rcState === "loading") {
      return '<button type="button" class="recheck loading" id="bRecheck" disabled>' +
        '<span class="rc-ico">⟳</span><span>正在复查…</span></button>';
    }
    const label = (rcState === "done") ? "已提交复查，继续自动复查…" : "重新核验";
    return '<button type="button" class="recheck" id="bRecheck">' +
      '<span class="rc-ico">⟳</span><span>' + label + '</span></button>';
  }

  async function doRecheck() {
    if (rcState === "loading") return;
    rcState = "loading";
    fastUntil = Date.now() + 120000;            // 接下来 2 分钟把轮询加快，让结果尽快弹出来
    if (lastSnap) render(lastSnap);
    let opOk = false, msg = "";
    try {
      const r = await fetchT("/api/recheck" + q, { method: "POST", cache: "no-store" }, 30000);
      let jj = null;
      try { jj = await r.json(); } catch (e) { jj = null; }
      const op = (jj && (jj.op || (jj.detail ? { message: String(jj.detail) } : null))) || null;
      if (r.ok) {
        opOk = true;
        // 端点直接返回新快照：立刻渲染（万一已经到账，横幅当场翻成「领取成功」）
        if (jj && (jj.status || jj.detail)) { render(jj); lastSnap = jj; }
        if (op && op.message) msg = op.message;
      }
      else if (r.status === 429) { msg = (op && op.message) || "刚查过啦，稍等下（自动复查继续中）"; }
      else if (r.status === 404) { msg = "复查接口还没上线，稍后会继续自动复查"; }
      else { msg = (op && op.message) || "提交复查没成功，稍后会继续自动复查"; }
    } catch (e) { msg = "网络异常，稍后会继续自动复查"; }
    await tick();
    rcState = "done";
    if (lastSnap) render(lastSnap);
    toast(msg || (opOk ? "已提交复查，请稍候…" : "稍后会自动复查"));
    setTimeout(() => { if (rcState === "done") { rcState = ""; if (lastSnap) render(lastSnap); } }, 6000);
  }

  function entryText(e) {
    const mm = { free: "免费用户（无活动资格）", free_vip: "体验套餐用户", vip: "会员" };
    return mm[e.subs_status] || e.subs_status || "未知";
  }

  // 登录账号：一拿到就显示；没拿到时给出"当前在等什么"，而不是空白
  function acctText(s) {
    if (s.account) return s.account;
    if (s.status === "idle") return "准备中…";
    if (s.status === "waiting") return "尚未登录（请用豆包 App 扫码）";
    if (s.status === "scanned") return "已扫码，请在手机上点「确认登录」";
    if (s.status === "verifying") return "登录成功，正在读取账号…";
    return "—";
  }

  function render(s) {
    const prevStatus = window.__wzSt || "";
    window.__wzSt = s.status || "idle";
    // ---- 二维码（只在真的换了码时改 src）----
    const sig = (s.seq || 0) + "|" + (s.qr ? s.qr.length + ":" + s.qr.slice(-28) : "");
    if (s.qr && sig !== m.qr) { m.qr = sig; m.qrSeq = s.seq || 0; $("qr").src = s.qr; }
    const hasQr = !!s.qr || !!m.qrSeq;
    const showMask = !hasQr;
    if (showMask !== m.mask) { m.mask = showMask; $("qrmask").classList.toggle("show", showMask); }
    setTxt("maskText", s.message || "二维码生成中…");
    // ★R505_STALE_TRUTH：码已过期且「不会再自动换」→ 把二维码蒙灰 + 写明要用户点按钮，
    // 否则买家会一直对着死码扫 / 一直等「正在换新」（线上最典型的「扫不上」）。
    const _endState = (s.status === "success" || s.status === "fail" || s.status === "error");
    const expiredStale = !!(hasQr && s.qr_paused && (Number(s.remain) || 0) <= 0 && !_endState && s.status === "waiting");
    if (expiredStale !== m.stale) { m.stale = expiredStale; $("qrbox").classList.toggle("stale", expiredStale); }

    const scanning = (s.status === "waiting");
    if (scanning !== m.scan) { m.scan = scanning; $("qrbox").classList.toggle("scanning", scanning); }
    const stampShow = (s.status === "success");
    if (stampShow !== m.stamp) { m.stamp = stampShow; $("stamp").classList.toggle("show", stampShow); }

    // ---- R75：判定「已定论失败」还是「仍在复查中（可能延迟）」----
    // 约定：后端给 s.final_fail === true 才算定论失败；字段不存在 => 一律按「仍在复查中」显示（向后兼容）
    const d = s.detail || {};
    const isFailish = (s.status === "fail" || s.status === "error");
    // 优先级：s.final_fail === true 是**唯一**能显示「本次未领取到」的条件；
    // 其余（s.retrying === true / 字段缺失）一律按「仍在复查中（可能延迟）」显示。
    const finalFail = isFailish && ((s.final_fail === true) || (d.final_fail === true) || (s.final === true));
    const retryingNow = (s.retrying === true) || (s.retrying === undefined);
    const pendingState = isFailish && !finalFail;
    const canRecheck = (s.can_recheck !== false);
    if (DONE.indexOf(s.status) < 0) sawPending = false;      // 新一轮领取开始，清掉上一轮的延迟标记
    else if (pendingState) sawPending = true;                 // 记下「曾经可能是延迟」，成功翻牌时提示补记
    lastSnap = s;
    if (prevStatus === "scanned" && s.status === "waiting" && !rcState) {
      toast("上一次扫码未完成确认，二维码已更新，请重新扫描");
    }

    // ---- 状态灯 ----
    let st = STATUS[s.status] || STATUS.idle;
    if (pendingState) st = { text: retryingNow ? "待确认（可能延迟）" : "待确认", cls: "pill--wait" };
    const pkey = st.text + "|" + st.cls;
    if (pkey !== m.pill) {
      m.pill = pkey;
      const p = $("statusText");
      p.textContent = st.text; p.className = "pill " + st.cls;
    }

    // ---- 副标题：永远有字（网络/官方错误都看得见）----
    let sub = s.message || "正在准备…";
    let dot = "run";
    if (fails >= 2 && DONE.indexOf(s.status) < 0) { sub = "网络不太稳定，正在自动重试…"; dot = "bad"; }
    else if (s.status === "success") dot = "ok";
    else if (pendingState) { sub = "等待官方发放入账（礼包延迟发放），本页自动复查中…"; dot = "run"; }
    else if (s.status === "fail" || s.status === "error") dot = "bad";
    else if (s.status === "waiting") dot = "live";
    else if (s.status === "scanned") dot = "live";
    if (sub !== m.sub) { m.sub = sub; setTxt("subText", sub); }
    // ★R702：成功时也换行 —— 「到期 …」被截断成省略号很容易被当成信息不完整
    const subWrap = (s.status === "fail" || s.status === "error" || s.status === "success");
    if (subWrap !== m.subwrap) { m.subwrap = subWrap; $("subline").classList.toggle("wrap", subWrap); }
    setCls("sdot", "sdot " + dot);

    // ---- 登录账号（实时）----
    const at = acctText(s);
    if (at !== m.acct) {
      m.acct = at; setTxt("acctV", at);
      $("acctRow").classList.toggle("on", !!s.account);
    }

    // ---- 手机号面板：脱敏号提示 + 冷却接续（绝不写进输入框 value）----
    syncPhone(s.phone);

    // ---- 倒计时 / 进度条 ----
    const remain = Math.max(0, Math.round(s.remain || 0));
    const verifying = (s.status === "verifying");
    // ★R211：核验中显示「第 N/8 次」，进度条按时间平滑推进（原来直接 100% 看着像卡死）
    const vElapsed = Number(s.verify_elapsed || 0) || 0;
    const vTry = Number(s.verify_attempt || 0) || 0;
    const vTot = Number(s.verify_total || 0) || 8;
    setTxt("countdown", verifying
      ? ("已核验 " + vElapsed + "s" + (vTry ? (" · 第 " + vTry + "/" + vTot + " 次") : ""))
      : ((hasQr && s.status !== "success") ? (remain + "s") : "--s"));
    // ★R213：核验进度 = 「第 N 次」为主 + 时间做渐近平滑。
    // 旧公式 56s 就顶到 97%，核验轮次一慢（代理抖动/官方接口慢）又会「卡住不动」。
    const pByTry = 6 + Math.min(1, Math.max(0, (vTry ? (vTry - 0.1) : 0) / Math.max(1, vTot - 0.1))) * 84;
    const pByTime = 6 + 84 * (1 - Math.exp(-vElapsed / 90));
    const pct = verifying ? Math.min(96, Math.max(pByTry, pByTime))
                          : ((s.status === "success" || s.status === "fail" || s.status === "error") ? 100
                             : (hasQr ? Math.round(Math.min(100, (remain / 60) * 100)) : 0));
    if (pct !== m.bar) {
      m.bar = pct;
      const bar = $("bar");
      bar.style.width = pct + "%";
      // ★R702c：成功页不能是红条（买家会当成报错）；失败才用红/橙
      bar.style.background = (s.status === "success")
        ? "linear-gradient(90deg,#2fd58a,#38d39f)"
        : ((s.status === "fail" || s.status === "error")
           ? "linear-gradient(90deg,#ff8b6b,#ff6b8a)"
           : ((verifying || remain > 20)
              ? "linear-gradient(90deg,#5b8cff,#8b5bff)"
              : "linear-gradient(90deg,#ff8b6b,#ff6b8a)"));
    }

    // ---- 结果横幅：内容不变就不重建（重建会闪、会抖）----
    const items = (d.items || []);
    let cls = "banner hide", ic = "", title = "", desc = "", extra = "";
    if (s.status === "success") {
      // ★R208：到期只展示「赠送项」，避免把历史个人订阅的到期混进来
      const _giftItems = items.filter(i => i.source === "pc_membership_gift" && i.end);
      const endAll = (_giftItems.length ? _giftItems : items).map(i => fmtTime(i.end)).filter(Boolean).join(" / ");
      const prod = items.map(i => i.product).filter(Boolean).join(" + ");
      const at = fmtTime(pick(d.done_at, s.done_at, d.claimed_at, d.success_at));
      // 「延迟到账已补记」：后端有标记就用标记，否则用本页自身的记忆（先看到「可能延迟」再翻成成功）
      const late = (d.late === true || d.late_recorded === true || s.late_recorded === true || s.late === true || sawPending);
      cls = "banner ok"; ic = "🎉"; title = "领取成功";
      desc = "30 天体验套餐已到账。这是豆包电脑版新人礼，请在豆包电脑版或网页版「会员」页查看（手机 App 可能不显示）。";
      extra = [
        d.account ? `<div class="chip"><span>账号</span><b>${esc(d.account)}</b></div>` : "",
        prod ? `<div class="chip ok"><span>套餐</span><b>${esc(prod)}</b></div>` : "",
        at ? `<div class="chip ok"><span>到账时间</span><b>${esc(at)}</b></div>` : "",
        endAll ? `<div class="chip ok"><span>到期时间(北京)</span><b>${esc(endAll)}</b></div>` : "",
        idChips(s, d, !!endAll),
        `<div class="chip"><span>本码已领</span><b>${s.claims || 1} 次${(s.max_claims === 0) ? "（不限次）" : ""}</b></div>`,
        regionChip(s),
        late ? `<div class="note">延迟到账，已自动补记</div>` : "",
      ].join("");
    } else if (s.status === "fail" || s.status === "error") {
      const chips = [
        d.account ? `<div class="chip"><span>账号</span><b>${esc(d.account)}</b></div>` : "",
        idChips(s, d),
        (d.entry && d.entry.subs_status) ? `<div class="chip"><span>账号状态</span><b>${esc(entryText(d.entry))}</b></div>` : "",
        regionChip(s),
      ].join("");
      if (pendingState) {
        // ① 还没定论 -> 不吓人：说清楚「礼包是延迟发放的」，并给一个复查按钮
        const why = String(d.hint || s.message || "").trim();
        progTxt = progText(s, d);
        cls = "banner pending"; ic = "⏳"; title = "暂未查到到账（可能延迟）";
        desc = "官方礼包是延迟发放的，通常 1~10 分钟内到账。本页会自动复查，也可以稍后用同一个链接再打开看结果。";
        extra = [
          chips,
          `<div class="softnote">先去豆包电脑版/网页版 → 会员 看一眼（这是电脑版新人礼，手机 App 可能不显示）；若已到账，本页会自动变成「领取成功」。</div>`,
          `<div class="chip"><span>复查</span><b id="bProgV">复查中…</b></div>`,
          why ? `<div class="hint">💡 若不是延迟到账，常见原因是：${esc(why)}</div>` : "",
          canRecheck ? rcHtml() : "",
        ].join("");
      } else {
        // ② 已定论失败 -> 才给出原因
        cls = "banner bad"; ic = "😥"; title = "本次未领取到";
        desc = s.message || "请换一个账号重试";
        // ★R419_RETRY_HINT：失败不等于这张码废了（实测 1/3 的失败码后来换号领到了）
        let retryNote = "";
        if (!/网络|失效|超时|异常/.test(String(d.hint || "") + String(desc || ""))) {
          // ★R700：“资格类”失败很可能是本账号的礼包还在路上（实测 66 例/3天 后来补记成功）——
          // 先确认再换号，避免一张卡两个账号各领一份。
          const _maybeLate = /此前已领取过|近期已领取过|没有收到官方福利|未到账|没拿到/.test(String(d.hint || "") + " " + String(desc || ""));
          retryNote = _maybeLate
            ? '<div class="softnote">先别急着换号：如果这是这个号<b>第一次</b>领这张卡，官方礼包最长要 <b>3~5 分钟</b>才显示到账——等 3 分钟后点「重新核验」，确认真没到账再换号。只有「这个号已经领过、且到期日还在 30 天内」才是真的重复，那种情况直接换新号即可。</div>'
            : '<div class="softnote">这张码<b>没有失效</b>：换一个<b>没领过的豆包账号</b>，用同一个链接再扫一次即可（实测约 1/3 的失败码都是这样领到的）。</div>';
        }
        extra = [
          chips,
          d.hint ? `<div class="hint">💡 ${esc(d.hint)}</div>` : "",
          retryNote,
          canRecheck ? rcHtml() : "",
        ].join("");
      }
    }
    const bkey = cls + "|" + ic + "|" + title + "|" + desc + "|" + extra + "|" + rcState;
    if (bkey !== m.banner) {
      m.banner = bkey;
      const bn = $("banner");
      bn.className = cls;
      setTxt("bIc", ic); setTxt("bTitle", title); setTxt("bDesc", desc);
      if ($("bExtra").innerHTML !== extra) $("bExtra").innerHTML = extra;
      bn.classList.remove("fresh");                      // 文案切换只做一次淡入，不重排
      requestAnimationFrame(() => bn.classList.add("fresh"));
    }
    if (pendingState) setTxt("bProgV", progTxt);          // 进度只改文本，不重建 DOM（防抖）

    // ---- 按钮 ----
    const done = (DONE.indexOf(s.status) >= 0);
    // ★R685：成功/已完成后把二维码挡住，防止买家对着作废或自动换出的新码再扫一次
    const _clm = (s.claimed === true);
    const _succ = (s.status === "success");
    const _qb = $("qrbox");
    if (_qb) {
      if (_succ) _qb.classList.remove("stale");
      _qb.classList.toggle("isdone", _succ);
    }
    try { window.__wzClaimed = (_clm || _succ); } catch (e) {}   // ★R695：成功后降频
    // ★R702：成功/已领取 → 收起「扫码领取 / 手机号（备用）」与「保存二维码」操作区，
    // 只留成功信息（对着一张死码再扫一次是最常见的误操作）
    try { document.body.classList.toggle("claimed", !!(_clm || _succ)); } catch (e) {}
    if (_clm) {
      setTxt("nextText", "该码已完成领取");
      $("next").disabled = true;
      try { phLock(); } catch (e) {}          // ★R692：成功后手机号面板一并锁死
    } else {
      setTxt("nextText", done ? "换一个二维码，再领一份" : "换一个二维码");
      $("next").disabled = busy || s.status === "verifying";
    }
  }

  // ===== R57 手机号领取：面板逻辑（不改动扫码轮询）=====
  // ★R692：已领取成功 → 锁死手机号面板（后端也会拒绝，双保险）
  function phLock(msg) {
    ["phNum", "phCC", "phSend", "phCode", "phLogin"].forEach((id) => {
      const el = $(id); if (el) el.disabled = true;
    });
    if (msg) phSay(msg, "ok");
  }

  function phSay(msg, kind) {
    const e = $("phMsg");
    if (!e) return;
    const v = msg || "";
    if (e.textContent !== v) e.textContent = v;
    const cls = "phmsg" + (kind === "ok" ? " ok" : kind === "bad" ? " bad" : "");
    if (e.className !== cls) e.className = cls;
    // ★R282_MSG_BIND：记住这条文案是给哪个号码的；号码一变就清掉，避免旧红字赖在新号上
    try { phMsgNum = v ? phE164() : ""; } catch (e2) { phMsgNum = ""; }
    phMsgTs = v ? Date.now() : 0;
  }

  // ★R257_UI_ALERT：人机验证常要 60~150 秒，用户切走页面后不知道短信到了。
  // 成功时震动 + 标题闪烁（只在页面不可见时闪），把用户叫回来输码。
  function phAlert(msg) {
    try { if (navigator.vibrate) navigator.vibrate([180, 80, 180]); } catch (e) {}
    try {
      if (!document.hidden) return;
      if (window.__phTitleTimer) { clearInterval(window.__phTitleTimer); window.__phTitleTimer = null; }
      const old = document.title;
      let n = 0;
      window.__phTitleTimer = setInterval(() => {
        document.title = (n++ % 2) ? ("✅ " + msg) : old;
        if (n > 12) { clearInterval(window.__phTitleTimer); window.__phTitleTimer = null; document.title = old; }
      }, 800);
    } catch (e) {}
  }

  // ★R263_WAIT_TICK：发码最长要等 150s，静止文案像卡死；从第 5 秒起显示已等待秒数。
  let phWaitTick = null;
  function phWaitStart() {
    let n = 0;
    if (phWaitTick) { clearInterval(phWaitTick); phWaitTick = null; }
    phWaitTick = setInterval(() => {
      n += 1;
      if (!phBusy) { clearInterval(phWaitTick); phWaitTick = null; return; }
      if (n >= 5) {
        // ★R286_WAIT_UX：超过 150s 直接推荐扫码（官方 3D 越等越不划算，尤其是 R277 生效前）
        // ★R336_WAIT_KEEP：实测成功发码 p50=174s、p90=324s（143 单），150s 就劝用户去扫码等于
        // 把一半马上要成功的人赶走（今日 CAP 发码 79 单里 41 单用户再没输码）。
        phSay(n >= 150
          ? ("正在发送验证码…已等待 " + n + "s：官方 3D 验证偏慢（实测平均 3 分钟、最长约 7 分钟），仍在进行中，请勿关闭页面；短信一到会自动提示。")
          : ("正在发送验证码…已等待 " + n + "s（官方验证通常 1~3 分钟，最长约 7 分钟，请勿关闭页面）"));
      }
    }, 1000);
  }
  function phWaitStop() {
    if (phWaitTick) { clearInterval(phWaitTick); phWaitTick = null; }
  }

  function phPaint() {
    const b = $("phSend");
    if (b) {
      if (phLeft > 0) { b.disabled = true; b.textContent = "重新获取(" + phLeft + "s)"; }
      else { b.disabled = phBusy; b.textContent = "获取验证码"; }
    }
    const l = $("phLogin");
    if (l) l.disabled = phBusy || Date.now() < phLockUntil;
    // ★R409_ESCAPE：发码等待中显示出口按钮（请求仍在跑，切过去不会白等：码到了会提示回来）
    const qb = $("phToQr");
    if (qb) qb.style.display = (phBusy || phNeedQrHint) ? "" : "none";
  }

  function phStartCd(sec) {
    phLeft = Math.max(0, Math.round(sec || 60));
    if (phTick) { clearInterval(phTick); phTick = null; }
    phPaint();
    if (phLeft <= 0) return;
    phTick = setInterval(() => {
      phLeft -= 1;
      if (phLeft <= 0) { phLeft = 0; clearInterval(phTick); phTick = null; }
      phPaint();
    }, 1000);
  }

  // 脱敏号只做 placeholder 提示（如 1**********），不塞进 value
  function syncPhone(p) {
    if (!p) return;
    const num = p.number || "";
    if (num !== m.phNum && $("phNum")) {
      m.phNum = num;
      $("phNum").placeholder = num ? ("请输入手机号（上次：" + num + "）") : "请输入手机号";
    }
    const cd = Math.max(0, Math.round(p.cooldown || 0));
    if (cd > phLeft + 1 && !phBusy) phStartCd(cd);   // 刷新后接续冷却，不弹提示
  }

  // ★R301_POST_TIMEOUT：请求兜底超时。服务端最慢的是 3D 验证（worker budget 360s），
  // 客户端给 400s；一旦超时就 abort，按钮恢复可点，避免「卡在进度条不动」。
  async function phPost(path, body, timeoutMs) {
    const ms = timeoutMs || 240000;
    let ctl = null, tid = null;
    try { if (typeof AbortController !== "undefined") ctl = new AbortController(); } catch (e) { ctl = null; }
    if (ctl) tid = setTimeout(() => { try { ctl.abort(); } catch (e) {} }, ms);
    try {
      const r = await fetch(path + q, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body || {}),
        cache: "no-store",
        signal: ctl ? ctl.signal : undefined,
      });
      let j = null;
      try { j = await r.json(); } catch (e) { j = null; }
      return { r: r, j: j };
    } finally {
      if (tid) clearTimeout(tid);
    }
  }

  function phNumVal() { return ($("phNum") ? ($("phNum").value || "") : "").replace(/\D/g, ""); }
  function phCodeVal() { return ($("phCode") ? ($("phCode").value || "") : "").replace(/\D/g, ""); }

  async function phSendCode() {
    if (phBusy) return;
    phCapSync();
    if (Date.now() < phCapUntil) {
      const _left = Math.max(1, Math.ceil((phCapUntil - Date.now()) / 1000));
      phSay("官方人机验证刚刚没通过，请等 " + _left + " 秒再点「获取验证码」；也可以直接点上方「扫码领取」。", "bad");
      return;
    }
    const v = phCheck();
    if (!v.ok) { phSay(v.msg, "bad"); toast(v.msg); if ($("phNum")) $("phNum").focus(); return; }
    const num = phE164();
    // ★R275_STALE_GUARD：等待期间用户改了号码 → 旧号码的迟到结果不再画到新号码上
    const _sentNum = num;
    const _stale = () => phE164() !== _sentNum;
    phBusy = true; phNeedQrHint = false; phPaint();
    // ★R286_WAIT_UX：实测高峰期 3D 最长到 5 分钟（R277 重启后会压到约 2 台/轮），先把预期说清，并提供更快的扫码入口
    phSay("正在发送验证码…（若触发官方人机验证，通常 1~3 分钟，最长约 5 分钟；请保持页面打开、别重复点）");
    phWaitStart();                            // ★R263：5 秒后开始显示已等待秒数
    const t0 = Date.now();                    // ★R257：用真实等待时长判断是否走了人机验证
    try {
      // ★R336_TIMEOUT_450：worker 实测最长 437s，客户端 400s 会把最后 5% 的成功单 abort 掉
      const res = await phPost("/api/phone/send", { phone: num, cc: v.cc }, 450000);
      const j = res.j, r = res.r;
      if (j && j.phone && !_stale()) syncPhone(j.phone);
      const op = (j && j.op) || null;
      const waited = Math.round((Date.now() - t0) / 1000);
      if (op && op.ok && !_stale()) {
        let okMsg = (op && op.message) || "验证码已发送，请查看短信";   // R249_OK_MSG
        if (waited >= 45) {
          okMsg = "人机验证已通过（" + waited + "s），验证码刚刚发出，请在 5 分钟内输入";
          phAlert("验证码已发送");
        }
        if (m.phSent > 0) okMsg += "（旧验证码已失效，请用最新一条短信）";   // ★R258b
        m.phSent = (m.phSent || 0) + 1;
        phNeedQrHint = false;
        phCapClear();
        phSay(okMsg, "ok");
        toast(okMsg);
        phStartCd(60);
        if ($("phCode")) $("phCode").focus();   // R249_AUTOSUBMIT：发完码直接聚焦输入框
        // ★R336_BACK_TO_PHONE：等待期间用户可能已切到扫码页；短信到了要把他拉回来并聚焦输码框
        try {
          const _pp = $("panePhone");
          if (_pp && !_pp.classList.contains("is-on")) {
            switchTab("phone");
            toast("验证码已发送，已切回手机号领取，请输入 6 位短信码");
          }
        } catch (e2) {}
        setTimeout(() => { try { if ($("phCode") && !document.hidden) $("phCode").focus(); } catch (e3) {} }, 300);
      } else if (!_stale()) {
        let msg = (op && op.message) || (j && j.message)
          || (r && r.status === 429 ? "请求过于频繁，请稍后再试" : "发送失败，请稍后重试");
        // ★R692：该码已完成领取 —— 中性提示 + 锁面板，不要显示成红色失败
        if (op && op.done) { phLock(msg); return; }
        // ★R319_DUP_COOLDOWN：同一会话上一轮人机验证没结束 → 按钮冷却，别让用户连点
        // 依据：线上 DEMO0018 在 60 秒内连点 4 次「获取验证码」，随后被官方 1203 锁号。
        const _dupBusy = /上一次验证还在进行中/.test(String(msg)) || (r && r.status === 429);
        // ★R254_UI_FALLBACK：op 缺失/旧缓存时，也把风控长文压成短句
        let autoQr = false;
        // ★R422_CAPTCHA_COPY：官方人机验证不是「号废了」，失败多半是点太频繁。
        //   保留在短信页、给出 60 秒冷却和明确的扫码出口，不再 1.2 秒强行切走。
        const _cap = /滑块验证|3D|人机验证|captcha/i.test(String(msg)) || !!(op && op.need_qr);
        if (_cap) {
          const _sub = String((op && op.verify_subtype) || "").toLowerCase();
          const _subCn = _sub === "slide" ? "滑块验证" : (_sub === "3d" ? "3D 旋转验证" : _sub);
          const st = _subCn ? ("（" + _subCn + "）") : "";
          msg = "官方要求人机验证" + st + "：本次自动验证没通过，多数是点得太频繁。"
              + "请等 60 秒再点一次「获取验证码」；扫码通道成功率更高，也可直接点上方「扫码领取」。";
          phNeedQrHint = true;
          phCapSave(60000);
          phStartCd(60);
          // ★R547_FOREIGN_AUTOQR：境外号(+1/+852)滑块自动通过率极低（实测 slide 基本过不了），
          // 失败后不再让买家在短信页干等 60s，直接切「扫码领取」；+86 维持 R422 的原行为。
          autoQr = (String(v.cc || "").toUpperCase() !== "CN");   // ★R547b：v.cc 是 ISO（phCheck 返回 c.iso），不是区号
          if (autoQr) {   // ★R547c：境外号直接把话说清楚，别再让人「等 60 秒」和「已切走」自相矛盾
            msg = "官方要求人机验证" + st + "：本次自动验证没通过。已为你切到「扫码领取」（境外号更稳）；"
                + "想继续用手机号，请等 60 秒再点一次「获取验证码」。";
          }
        }
        if (_dupBusy) {
          msg = "上一轮验证还在跑（官方人机验证较慢）：请等 15 秒再点，重复点会被官方锁号；等不及可直接点上方「扫码领取」。";
          phStartCd(15);
        }
        phSay(msg, "bad");
        // ★R422_TOAST：风控长文只留在内联提示，toast 用短句，避免遮住页面
        toast(_cap ? (autoQr ? "境外号人机验证未通过，已切到扫码领取" : "人机验证没通过：等 60 秒再试，或改走扫码领取") : msg);
        // ★R310_AUTO_QR：境外/高风险号被风控，直接把买家送到扫码页，别让他卡在短信页重试
        if (autoQr) {
          setTimeout(() => { try { switchTab("qr"); } catch (e2) {} }, 1200);
        }
      }
    } catch (e) {
      if (!_stale()) {
        // ★R301：超时/中断不能让用户以为页面卡死——明确提示并把按钮放开
        const _ab = !!(e && (e.name === "AbortError" || String(e).indexOf("abort") >= 0));
        const _m = _ab
          ? "等待超时：官方人机验证太久，正在切到「扫码领取」…"
          : "网络异常，请稍后重试";
        phSay(_m, "bad"); toast(_m);
        // ★R310_AUTO_QR：4 分钟人机验证还没结果 → 直接切扫码，不让页面像卡死
        if (_ab) {
          setTimeout(() => { try { switchTab("qr"); } catch (e2) {} }, 1200);
        }
      }
    }
    phWaitStop();
    phBusy = false; phPaint();
    // ★R282_STALE_SETTLE：等待期间改了号 → 结束后不再卡在「正在发送验证码…」
    if (_stale()) phSay("号码已修改，上次发送的结果已忽略；请用新号码重新点「获取验证码」。", "");
  }

  async function phLogin() {
    if (phBusy) return;
    // ★R288_LOGIN_ONCE：同号已登录成功后不再重复提交（DEMO0019 19:46 成功 330s 后又提交 → 1203 把成功盖红）
    if (phOkNum && phE164() === phOkNum) {
      phSay("该号码已登录成功，正在核验权益，无需重复提交 ♡", "ok");
      return;
    }
    // ★R276_LOCK1203：官方 1203 后 60s 内不再发登录请求，避免用户连点把号码推进更久的冷却
    if (Date.now() < phLockUntil) {
      const _left = Math.ceil((phLockUntil - Date.now()) / 1000);
      const _t = _left > 90
        ? ("请等约 " + Math.ceil(_left / 60) + " 分钟再试（官方已临时限制该号码，刷新页面不会解锁）")
        : ("请 " + _left + " 秒后再试（连续提交会被官方临时限错）");
      phSay(_t, "bad"); toast(_t); return;
    }
    const v = phCheck();
    if (!v.ok) { phSay(v.msg, "bad"); toast(v.msg); if ($("phNum")) $("phNum").focus(); return; }
    const num = phE164(), code = phCodeVal();
    // ★R282_STALE_LOGIN：记录本次登录用的号码，等待期间用户改号则丢弃旧结果
    const _loginNum = num;
    const _staleL = () => phE164() !== _loginNum;
    if (!/^\d{6}$/.test(code)) {
      const t = "请输入 6 位短信验证码";
      phSay(t, "bad"); toast(t);
      if ($("phCode")) $("phCode").focus();
      return;
    }
    phBusy = true; phPaint();
    phSay("正在登录并核验权益…");
    try {
      const res = await phPost("/api/phone/login", { phone: num, code: code, cc: v.cc }, 240000);
      const j = res.j, r = res.r;
      // ★R282_STALE_LOGIN：登录等待期间改了号 → 旧号的结果不再画到新号上
      if (j && j.phone && !_staleL()) syncPhone(j.phone);
      if (j && (j.status || j.detail || j.phone) && !_staleL()) render(j);   // 复用现有成功/失败横幅
      const op = (j && j.op) || null;
      if (_staleL()) {
        /* 旧号的结果直接丢弃（下面统一提示） */
      } else if (op && op.ok) {
        toast("登录成功，正在核验权益…");
        phOkNum = num;   // ★R288_LOGIN_ONCE
        phSay("登录成功，正在核验权益…", "ok");
      } else {
        let msg = (op && op.message) || (j && j.message)
          || (r && r.status === 429 ? "请求过于频繁，请稍后再试" : "登录失败，请稍后重试");
        // ★R276_LOCK1203：官方 1203（错误次数过多/验证码过期）→ 清空输入框 + 锁 60s
        const _ec = (op && op.ec != null) ? Number(op.ec) : null;
        if (_ec === 1203 || (_ec === null && /错误次数过多|验证码过期/.test(msg))) {
          // ★R322：3 分钟持久冷却（写 localStorage），刷新页面也不清零；避免反复 1203 把号码越锁越久
          phLockSave(PH_LOCK_MS);
          if ($("phCode")) $("phCode").value = "";
          msg = "官方已临时限制该号码（验证码错误次数过多）：请等约 3 分钟再试；刷新页面不会解锁。也可以直接点上方「扫码领取」（更快、更稳）。";
          phLockArm();
        } else if (_ec === 1202 || /验证码错误/.test(msg)) {
          // ★R323_WRONG_CODE：1202=输错/旧码。清空输入框并聚焦，别让用户在 6 位旧数字上一位位删。
          if ($("phCode")) { $("phCode").value = ""; $("phCode").focus(); }
          msg = "验证码不对（可能是旧码或输错一位）：请用最新一条短信里的 6 位数字重输；连续错多次会被官方临时锁号。";
        } else if (!_ec && /操作失败/.test(msg)) {
          // ★R313_TRANSIENT：官方偶发「操作失败」（无 ec）多是风控抖动，
          // 不是号码错也不是验证码错；线上实测这类账号 78% 后续重试会成功。
          if ($("phCode")) $("phCode").value = "";
          msg = "官方服务抖了一下（不是你号码的问题）：请重新点「获取验证码」拿一条新码再登一次，多数第二次就成功；也可以直接点上方「扫码领取」（更稳）。";
        }
        phSay(msg, "bad"); toast(msg);
      }
    } catch (e) {
      if (!_staleL()) {
        const _abL = !!(e && (e.name === "AbortError" || String(e).indexOf("abort") >= 0));
        const _mL = _abL ? "登录核验超时：请稍后重试，或直接点上方「扫码领取」" : "网络异常，请稍后重试";
        phSay(_mL, "bad"); toast(_mL);
      }
    }
    phBusy = false; phPaint();
    if (_staleL()) phSay("号码已修改，本次登录结果已忽略；请用新号码重新获取验证码。", "");
  }

  // ===== R68 国家/地区：出口 IP 跟着手机号 / 账号国家走 =====
  let CC = [], ccLoaded = false;

  async function loadCC() {
    if (ccLoaded) return CC;
    try {
      const r = await fetchT("/api/countries" + q, { cache: "no-store" }, 15000);
      const jj = await r.json();
      CC = (jj && jj.countries) || [];
    } catch (e) { CC = []; }
    ccLoaded = true;
    fillCC();
    return CC;
  }

  function ccByIso(iso) { for (const c of CC) if (c.iso === iso) return c; return null; }

  function fillCC() {
    const ph = $("phCC"), qr = $("qrCC");
    if (ph) {
      ph.innerHTML = CC.map((c) => '<option value="' + c.iso + '">' + c.name + " +" + c.dial + "</option>").join("");
      ph.value = ccByIso(m.phCC) ? m.phCC : (CC[0] ? CC[0].iso : "CN");
      m.phCC = ph.value;
    }
    if (qr) {
      qr.innerHTML = '<option value="">自动（跟随后台设置）</option>' +
        CC.map((c) => '<option value="' + c.iso + '">' + c.name + " +" + c.dial + "</option>").join("");
      qr.value = m.qrCC || "";
    }
  }

  function phSetCC(iso) {
    if (!iso) return;
    m.phCC = iso;
    const sel = $("phCC");
    if (sel && sel.value !== iso) sel.value = iso;
  }

  // 从输入里猜国家：+852… / 00852… / 852… / 11 位 1 开头=大陆
  function detectCC(raw) {
    let s = String(raw || "").replace(/[^\d+]/g, "");
    if (!s) return "";
    let intl = false;
    if (s[0] === "+") { s = s.slice(1); intl = true; }
    else if (s.slice(0, 2) === "00") { s = s.slice(2); intl = true; }
    const byDial = CC.slice().sort((a, b) => b.dial.length - a.dial.length);
    if (intl) {
      for (const c of byDial) if (s.startsWith(c.dial)) return c.iso;
      return "";
    }
    if (/^1[3-9]\d{9}$/.test(s)) return "CN";
    for (const c of byDial) {
      if (c.dial === "1") continue;
      if (s.startsWith(c.dial)) {
        const nat = s.slice(c.dial.length);
        if (nat.length >= (c.min || 4) && nat.length <= (c.max || 15)) return c.iso;
      }
    }
    return "";
  }

  // 校验：返回 {ok, msg, cc}
  function phCheck() {
    const raw = $("phNum") ? ($("phNum").value || "").trim() : "";
    const s = raw.replace(/[^\d+]/g, "");
    if (!s) return { ok: false, msg: "请输入手机号" };
    const c = ccByIso(m.phCC) || ccByIso("CN");
    if (!c) return { ok: false, msg: "请选择手机号国家 / 地区" };
    let nat = s;
    if (s[0] === "+") {
      let t = s.slice(1);
      if (t.startsWith(c.dial)) nat = t.slice(c.dial.length);
    }
    nat = nat.replace(/\D/g, "");
    if ((c.iso === "US" || c.iso === "CA") && nat.length === 11 && nat.charAt(0) === "1") nat = nat.slice(1);
    if (c.iso === "CN") {
      if (!/^1[3-9]\d{9}$/.test(nat)) return { ok: false, msg: "请输入正确的中国大陆手机号（11 位，1 开头）" };
    } else if (c.iso === "US" || c.iso === "CA") {
      // ★R279_NANP：+1 必须是合法 NANP 号码（10 位，区号/局号不能以 0/1 开头）。
      // 实例：+1 168xxxxxxx 的区号以 1 开头，不是真实美国号，官方仍会回 success 但短信永远到不了，用户只会拿到旧码一直 1203。
      if (!/^[2-9]\d{2}[2-9]\d{6}$/.test(nat)) {
        return { ok: false, msg: "美国/加拿大号码格式不对（应为 10 位，区号/局号不能以 0 或 1 开头）；如果是大陆号，请把国家选成 +86" };
      }
    } else if (nat.length < (c.min || 5) || nat.length > (c.max || 15)) {
      return { ok: false, msg: c.name + "号码应为 " + (c.min || 5) + "~" + (c.max || 15) + " 位，请检查" };
    }
    return { ok: true, msg: "", cc: c.iso, nat: nat };
  }

  // 提交给后端的号码：带前缀就原样，不带就按所选国家补 +区号
  function phE164() {
    const raw = $("phNum") ? ($("phNum").value || "").trim() : "";
    const s = raw.replace(/[^\d+]/g, "");
    const c = ccByIso(m.phCC) || ccByIso("CN");
    if (!c) return s;
    let digits = s.replace(/\D/g, "");
    if (s.charAt(0) === "+" && digits.indexOf(c.dial) === 0) digits = digits.slice(c.dial.length);
    // ★R279_NANP：+1 允许多输一个前导 1；其余按所选国家拼接
    if ((c.iso === "US" || c.iso === "CA") && digits.length === 11 && digits.charAt(0) === "1") digits = digits.slice(1);
    return "+" + c.dial + " " + digits;
  }

  function phPhoneInput(el, max) {
    if (!el) return;
    // ★R284_CC_KEEP：已显式选 +1 时，「1 + 10 位」按带国家码的美加号处理，不要抢成 CN
    // （input 和 blur 都要用同一套判断：点「获取验证码」时输入框先 blur，旧行为会在这里被抢成 CN）
    const autoCC = (raw) => {
      const iso = detectCC(raw);
      if (!iso) return "";
      const digits = String(raw || "").replace(/\D/g, "");
      // 只有「1 + 合法 NANP（区号/局号首位 2-9）」才保持 +1；否则仍自动切回 CN
      if (iso === "CN" && (m.phCC === "US" || m.phCC === "CA") && /^1[2-9]\d{2}[2-9]\d{6}$/.test(digits)) return "";
      return iso;
    };
    el.addEventListener("input", () => {
      let v = (el.value || "").replace(/[^\d+\s-]/g, "").slice(0, max || 22);
      if (v !== el.value) el.value = v;
      const iso = autoCC(v);
      if (iso) phSetCC(iso);
      try { phSay(""); } catch (e) {}   // R248_CLEAR_STALE: 改号即清掉上一次的红色风控文案
      phLockUntil = 0; phLockSync(); phLockArm();   // ★R322：换号后恢复该号自己的 1203 冷却（到期自动解灰）
      // ★R424：换号时把发码倒计时也切到该号自己的风控冷却
      phLeft = 0; if (phTick) { clearInterval(phTick); phTick = null; } phCapSync(); phPaint();
      if (phLockUntil > Date.now()) {             // 刷新/重开后重新输入同号 → 明确告诉他为什么按钮是灰的
        const _lm = Math.max(1, Math.ceil((phLockUntil - Date.now()) / 60000));
        phSay("该号码已被官方临时限制（验证码错误次数过多）：请等约 " + _lm + " 分钟再试；刷新页面不会解锁，也可以直接点上方「扫码领取」。", "bad");
      }
    });
    el.addEventListener("blur", () => { const iso = autoCC(el.value); if (iso) phSetCC(iso); });
  }

  async function qrSetRegion(cc) {
    try {
      const res = await phPost("/api/region", { cc: cc }, 30000);
      const jj = res.j || {};
      const op = jj.op || {};
      m.qrCC = cc;
      if (op.ok) toast(op.message || "出口地区已切换");
      else if (cc) toast(op.message || "切换失败");
      if (jj.status || jj.detail) render(jj);
    } catch (e) { /* 静默：不影响扫码 */ }
  }

  function switchTab(name) {
    const phone = (name === "phone");
    const tq = $("tabQr"), tp = $("tabPhone"), pq = $("paneQr"), pp = $("panePhone");
    if (tq) { tq.classList.toggle("is-on", !phone); tq.setAttribute("aria-selected", phone ? "false" : "true"); }
    if (tp) { tp.classList.toggle("is-on", phone); tp.setAttribute("aria-selected", phone ? "true" : "false"); }
    if (pq) pq.classList.toggle("is-on", !phone);
    if (pp) pp.classList.toggle("is-on", phone);
  }

  // 输入框只留数字（防中文输入法/字母混入）
  function phDigitsOnly(el, max) {
    if (!el) return;
    el.addEventListener("input", () => {
      const v = (el.value || "").replace(/\D/g, "").slice(0, max);
      if (v !== el.value) el.value = v;
      // ★R498_NOAUTOSUBMIT：以前输满 6 位 400ms 后自动提交。
      // 线上 07:45:58 发码 → 07:46:07 自动提交 → 官方 ec=7「访问太频繁」
      // → 用户 4 秒后再点一次 → 1203 锁号。改为只提示，由用户点按钮提交。
      if (el.id === "phCode" && v.length === 6 && !phBusy) {
        clearTimeout(el._autoT);
        if ($("phMsg")) phSay("已填好 6 位验证码，请点下方「立即领取 30 天会员」提交", "ok");
      }
    });
  }

  // ★R283_BUILD_WATCH：服务端 app.js 已更新 → 开着的旧页面自动刷新一次（防死循环：同版本只刷一次）
  function checkBuild(j) {
    try {
      const b = j && j.build;
      if (!b || !MY_BUILD || b === MY_BUILD || buildReloaded) return;
      if (phBusy || phCodeVal()) return;                  // 正在发码/输码：等下一次轮询
      if (document.visibilityState !== "visible") return;  // 后台页先不动
      const key = "phBuildReload";
      if (sessionStorage.getItem(key) === b) return;       // 已为这个版本刷过 → 不再刷
      sessionStorage.setItem(key, b);
      buildReloaded = true;
      location.reload();
    } catch (e) {}
  }

  async function tick() {
    try {
      const r = await fetchT("/api/state" + q + "qs=" + (m.qrSeq || 0) + "&t=" + Date.now(), { cache: "no-store" }, 12000);
      if (r.status === 429) {
        // ★R86c：服务端说「太快了」——退避 5 秒再问，避免被限速时还在疯狂重试（越重试越慢）
        fails++;
        coolUntil = Date.now() + 5000;
        if (fails >= 2) { setTxt("subText", "当前人有点多，正在排队重试…"); setCls("sdot", "sdot bad"); }
        return;
      }
      if (r.status === 403) {
        // ★R442：卡密被代理商停用/删除 -> 停止轮询并明确告知，不再无脑重试
        let d = {};
        try { d = await r.json(); } catch (e) {}
        setTxt("subText", (d && d.detail) || "该兑换码已被停用");
        setCls("sdot", "sdot bad");
        stopPoll = true;
        return;
      }
      if (!r.ok) { fails++; return; }
      fails = 0;
      const _j = await r.json();
      checkBuild(_j);
      render(_j);
    } catch (e) {
      fails++;                                  // 网络断了也要让用户看到状态
      if (fails >= 2) { setTxt("subText", "网络不太稳定，正在自动重试…"); setCls("sdot", "sdot bad"); }
    }
  }

  // ★R315_HIDDEN_SLOW：很多人把页面挂在后台不管（实测有会话挂了 15 小时）。
  // 后台连续 30 分钟后降频到 10 分钟一跳，避免每 4 秒一次请求 + 每 50 秒一张新码。
  let hiddenSince = document.hidden ? Date.now() : 0;
  const HIDDEN_SLOW_AFTER = 30 * 60 * 1000;
  const HIDDEN_SLOW_POLL = 10 * 60 * 1000;

  function delay() {
    if (Date.now() < coolUntil) return Math.max(800, coolUntil - Date.now());   // ★R86c：429 退避
    // ★R695：已领取 → 状态不会再变，3s 改 60s（成功页常被挂着不关，省请求/流量）
    if (window.__wzClaimed) return 60000;
    if (document.hidden) {
      if (hiddenSince && Date.now() - hiddenSince >= HIDDEN_SLOW_AFTER) {
        window.__wzHiddenSlow = 1;
        return HIDDEN_SLOW_POLL;
      }
      window.__wzHiddenSlow = 0;
      return 4000;
    }
    const st = (window.__wzSt || "idle");
    if (Date.now() < fastUntil && (st === "fail" || st === "error")) return 1500;
    if (st === "scanned" || st === "verifying") return 800;   // 关键瞬间：有人在扫/在确认，盯紧点
    if (st === "waiting") return 1500;                        // ★R86：空闲态 800→1500ms，自建服务器请求量减半
    if (DONE.indexOf(st) >= 0) return 3000;
    return 1200;
  }
  function loop() {
    if (stopPoll) return;
    clearTimeout(timer);
    timer = setTimeout(async () => { await tick(); loop(); }, delay());
  }
  // 失败横幅里的「重新核验」（事件委托：横幅内容是 innerHTML 重建的）
  {
    const bnr = $("banner");
    if (bnr) bnr.addEventListener("click", (e) => {
      const el = (e.target && e.target.closest) ? e.target.closest("#bRecheck") : null;
      if (!el) return;
      e.preventDefault();
      doRecheck();
    });
  }

  $("next").addEventListener("click", async () => {
    if (busy) return;
    if (lastSnap && lastSnap.claimed === true) { toast("该码已完成领取，无需再换码"); return; }
    busy = true;
    $("next").disabled = true;
    setTxt("nextText", "正在生成新二维码…");
    try {
      const r = await fetchT("/api/next" + q, { method: "POST" }, 45000);
      const j = await r.json();
      if (j.ok) { m.qr = null; m.qrSeq = 0; m.banner = ""; rcState = ""; sawPending = false; fastUntil = 0; render(j); toast("正在生成新二维码…"); }
      else { toast(j.message || "生成失败，请重试"); }
    } catch (e) { toast("网络异常，请重试"); }
    busy = false;
    $("next").disabled = false;
    tick();
  });

  // ---- R57 手机号领取：事件绑定 ----
  if ($("tabQr")) $("tabQr").addEventListener("click", () => switchTab("qr"));
  if ($("tabPhone")) $("tabPhone").addEventListener("click", () => switchTab("phone"));
  if ($("phSend")) $("phSend").addEventListener("click", phSendCode);
  if ($("phLogin")) $("phLogin").addEventListener("click", phLogin);
  if ($("phToQr")) $("phToQr").addEventListener("click", () => {
    try { phWaitStop(); } catch (e) {}
    try { switchTab("qr"); toast("已切到扫码领取（短信到了也会提示）"); } catch (e) {}
  });
  if ($("phCode")) $("phCode").addEventListener("keydown", (e) => { if (e.key === "Enter") phLogin(); });
  phPhoneInput($("phNum"), 22);
  // ★R282_MSG_BIND：自动填充/脚本改号不会触发 input 事件，800ms 轮询兜底清旧文案
  setInterval(() => {
    try {
      if (phBusy) return;
      // ★R482_MSG_TTL：红字只挂 120 秒就自动消失——用户不会以为「号废了/页面坏了」而一直看着同一句话
      // （R299 原为 10 分钟，线上反馈太久；手机号通道本来就受官方人机验证限制，无需长期告警）
      if (phMsgTs && Date.now() - phMsgTs > 120000) {
        const _e = $("phMsg");
        if (_e && _e.className.indexOf("bad") >= 0) {
          phSay("");
          return;
        }
      }
      if (!phMsgNum) return;
      if (phE164() !== phMsgNum) phSay("");
    } catch (e) {}
  }, 800);
  phDigitsOnly($("phCode"), 6);
  if ($("phCC")) $("phCC").addEventListener("change", () => { m.phCC = $("phCC").value; try { phSay(""); } catch (e) {} });
  if ($("qrCC")) $("qrCC").addEventListener("change", () => qrSetRegion($("qrCC").value || ""));
  // ★R423_MOBILE_QR：手机端显示「保存/相册」引导（同一台手机没法直接扫自己屏幕）
  try {
    const _coarse = !!(window.matchMedia && window.matchMedia("(pointer: coarse)").matches);
    const _isMobile = /iphone|ipad|ipod|android|mobile|micromessenger/i.test(navigator.userAgent || "")
      || (navigator.maxTouchPoints > 0 && _coarse);
    if (_isMobile && $("qrMobile")) $("qrMobile").style.display = "block";
    if ($("qrSave")) $("qrSave").addEventListener("click", () => {
      try {
        const img = $("qr");
        if (!img || !img.src) { toast("二维码还没生成，请稍等"); return; }
        const a = document.createElement("a");
        a.href = img.src; a.download = "doubao-qr.png"; a.target = "_blank"; a.rel = "noopener";
        document.body.appendChild(a); a.click(); a.remove();
        toast("已打开二维码图片：可保存到相册，再用豆包「扫一扫」");
      } catch (e) { toast("请长按上方二维码保存到相册"); }
    });
  } catch (e) {}
  loadCC();
  phPaint();
  phCapSync();

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) { hiddenSince = Date.now(); return; }
    const _away = hiddenSince ? (Date.now() - hiddenSince) : 0;
    hiddenSince = 0;
    window.__wzHiddenSlow = 0;
    if (_away >= HIDDEN_SLOW_AFTER) clearTimeout(timer);   // ★R315：回到前台立刻恢复，不等 10 分钟
    tick();
    if (_away >= HIDDEN_SLOW_AFTER) loop();
  });
  tick();
  loop();
})();
