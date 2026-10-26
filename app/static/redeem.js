/* R213 · 卡密兑换中心前端 ------------------------------------------------- */
(function () {
  "use strict";

  var LS_PREFIX = "db_redeem_result_v2:", LS_TTL = 86400, LS_OLD = "db_redeem_result_v1";
  var $ = function (id) { return document.getElementById(id); };
  var codeEl = $("code"), form = $("form"), field = $("field"), alertEl = $("alert");
  var submitBtn = $("submit"), stageInput = $("stage-input"), stageResult = $("stage-result");
  var linksEl = $("links"), toastEl = $("toast"), toastTimer = 0, busy = false;

  $("year").textContent = String(new Date().getFullYear());

  /* ---------- 工具 ---------- */
  function toast(msg) {
    toastEl.textContent = msg;
    toastEl.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { toastEl.classList.remove("show"); }, 1900);
  }

  function showAlert(msg) {
    alertEl.textContent = msg;
    alertEl.hidden = false;
    field.classList.add("is-error");
    setTimeout(function () { field.classList.remove("is-error"); }, 600);
  }

  function clearAlert() { alertEl.hidden = true; field.classList.remove("is-error"); }

  var RE_CODE = /^[A-HJ-NP-Z2-9]{20}$/;
  function alnum(v) { return String(v == null ? "" : v).toUpperCase().replace(/[^A-Z0-9]/g, ""); }
  function fmtCode(v) {
    var s = alnum(v).slice(0, 20);
    var out = [];
    for (var i = 0; i < s.length; i += 5) out.push(s.slice(i, i + 5));
    return out.join("-");
  }
  // ★R705：买家经常把整条兑换链接（或带说明文字）直接粘进来，
  //   旧逻辑会把 "http://203.0.113.10:8080/..." 当成兑换码。线上真实案例：
  //   粘贴后被输入框截断成 "HTTP4-52072-14318-0"，然后报「请输入完整的 20 位兑换码」。
  //   这里从文本里把真正的 20 位码抠出来（先找 /redeem/ 段，再全文匹配）。
  function extractCode(text) {
    var raw = String(text == null ? "" : text);
    // 1) 链接里直接带码：.../redeem/XXXXX-XXXXX-XXXXX-XXXXX
    var m = raw.match(/\/redeem\/([A-Za-z0-9][A-Za-z0-9-]{15,40})/i);
    if (m && RE_CODE.test(alnum(m[1]))) return alnum(m[1]);
    // 2) 整段就是码（允许空格/横线）
    var n = alnum(raw);
    if (RE_CODE.test(n)) return n;
    // 3) ★R705e：「链接 + 换行 + 兑换码」一起粘进来 —— 先把 URL 剥掉再找
    var noUrl = raw.replace(/https?:\/\/[^\s]+/gi, " ");
    if (noUrl !== raw) {
      var n2 = alnum(noUrl);
      if (RE_CODE.test(n2)) return n2;
      var m3 = n2.match(/[A-HJ-NP-Z2-9]{20}/);
      if (m3) return m3[0];
    }
    // 4) 纯文本里夹着码
    if (raw.length <= 200) {
      var m4 = n.match(/[A-HJ-NP-Z2-9]{20}/);
      if (m4) return m4[0];
    }
    return null;
  }

  // ★R705b：支持 /redeem?code=XXXXX-... / ?c=... / #code=... 直接带码进来（代理商发链接就不用买家手抄）
  function prefillFromUrl() {
    try {
      var q = new URLSearchParams(location.search || "");
      var raw = q.get("code") || q.get("c") || q.get("k") || "";
      if (!raw && location.hash) raw = decodeURIComponent(location.hash.replace(/^#/, "").replace(/^code=/, ""));
      var c = extractCode(raw || "");
      if (c) { codeEl.value = fmtCode(c); toast("已自动填入兑换码"); }
    } catch (e) {}
  }

  function fmtTime(ts) {
    try {
      var d = new Date((Number(ts) || Date.now() / 1000) * 1000);
      var p = function (n) { return String(n).padStart(2, "0"); };
      // R677_TZ_CST：兑换时间固定按北京时间（UTC+8）显示，不跟随访客浏览器/代理出口时区
      try {
        var o = {};
        new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric",
          month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
          .formatToParts(d).forEach(function (x) { o[x.type] = x.value; });
        if (o.year) return o.year + "-" + o.month + "-" + o.day + " " + o.hour + ":" + o.minute;
      } catch (e2) {}
      var t = new Date(d.getTime() + 8 * 3600 * 1000);
      return t.getUTCFullYear() + "-" + p(t.getUTCMonth() + 1) + "-" + p(t.getUTCDate()) +
        " " + p(t.getUTCHours()) + ":" + p(t.getUTCMinutes());
    } catch (e) { return "—"; }
  }

  /* ---------- 复制（含 http 非安全上下文兜底） ---------- */
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return legacyCopy(text); });
    }
    return Promise.resolve(legacyCopy(text));
  }

  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-9999px;left:-9999px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, ta.value.length);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (!ok) throw new Error("copy failed");
    return true;
  }

  /* ---------- 渲染结果（R216：20 条/页，避免几百条一次性进 DOM） ---------- */
  var PAGE_SIZE = 20, curPage = 1, current = null;

  function render(data) {
    clearAlert();
    current = data || { links: [] };
    curPage = 1;
    $("r-batch").textContent = current.batch_no || "—";
    $("r-group").textContent = current.group_no ? String(current.group_no).padStart(2, "0") : "—";
    $("r-count").textContent = (current.links || []).length + " 条";
    $("r-time").textContent = fmtTime(current.redeemed_at);
    paintLinks();
    stageInput.hidden = true;
    stageResult.hidden = false;
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function paintLinks() {
    var arr = (current && current.links) || [];
    linksEl.textContent = "";
    arr.slice((curPage - 1) * PAGE_SIZE, curPage * PAGE_SIZE).forEach(function (url) {
      var li = document.createElement("li");
      var span = document.createElement("span");
      span.className = "url";
      span.textContent = url;
      span.title = url;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "copy";
      btn.textContent = "复制";
      btn.addEventListener("click", function () {
        promiseToVoid(copyText(url).then(function () {
          btn.textContent = "已复制";
          btn.classList.add("done");
          setTimeout(function () { btn.textContent = "复制"; btn.classList.remove("done"); }, 1400);
        }).catch(function () { toast("复制失败，请长按链接手动复制"); }));
      });
      li.appendChild(span);
      li.appendChild(btn);
      linksEl.appendChild(li);
    });
    paintPager(arr.length);
  }

  function paintPager(total) {
    var pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
    var bar = document.getElementById("pager");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "pager";
      bar.className = "pager";
      linksEl.parentNode.insertBefore(bar, linksEl.nextSibling);
    }
    bar.textContent = "";
    if (pages <= 1) return;
    var mk = function (txt, p, dis) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "ghost ghost--sm";
      b.textContent = txt;
      b.disabled = !!dis;
      b.addEventListener("click", function () {
        curPage = p;
        paintLinks();
        linksEl.scrollIntoView({ behavior: "smooth", block: "start" });
      });
      return b;
    };
    bar.appendChild(mk("上一页", curPage - 1, curPage <= 1));
    var s = document.createElement("span");
    s.className = "pager__now";
    s.textContent = curPage + " / " + pages;
    bar.appendChild(s);
    bar.appendChild(mk("下一页", curPage + 1, curPage >= pages));
  }

  function promiseToVoid(p) { if (p && p.catch) p.catch(function () {}); }

  function keyOf(tail) { return LS_PREFIX + String(tail || "").toUpperCase(); }

  function saveLocal(data) {
    try {
      var tail = String(data.code_tail || "").toUpperCase();
      if (!tail) return;
      localStorage.setItem(keyOf(tail), JSON.stringify({
        tail: tail,
        links: data.links || [],
        batch_no: data.batch_no || "",
        group_no: data.group_no || 0,
        redeemed_at: data.redeemed_at || Math.floor(Date.now() / 1000)
      }));
      localStorage.removeItem(LS_OLD);
    } catch (e) { /* 隐私模式忽略 */ }
  }

  function newestLocal() {
    var best = null, bestKey = "";
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var k = localStorage.key(i);
        if (!k || k.indexOf(LS_PREFIX) !== 0) continue;
        var d = null;
        try { d = JSON.parse(localStorage.getItem(k) || "null"); } catch (e) { d = null; }
        if (!d || !d.links || !d.links.length) { localStorage.removeItem(k); continue; }
        if (Math.floor(Date.now() / 1000) - (d.redeemed_at || 0) > LS_TTL) { localStorage.removeItem(k); continue; }
        if (!best || (d.redeemed_at || 0) > (best.redeemed_at || 0)) { best = d; bestKey = k; }
      }
      localStorage.removeItem(LS_OLD);
    } catch (e) { return null; }
    return best ? { data: best, key: bestKey } : null;
  }

  // R216: 不再一打开页面就把上次结果（可能是别人的）渲染出来；
  //       只提示本机有记录，用户点「查看」才展开。
  function restore() {
    var hit = newestLocal();
    if (!hit) return false;
    var box = document.createElement("div");
    box.className = "restore-hint";
    var txt = document.createElement("span");
    txt.textContent = "本机保存过一次兑换结果（批次 " + (hit.data.batch_no || "-") +
      " · 组 " + String(hit.data.group_no || 0).padStart(2, "0") + "）";
    var look = document.createElement("button");
    look.type = "button";
    look.className = "ghost ghost--sm";
    look.textContent = "查看";
    look.addEventListener("click", function () { box.remove(); render(hit.data); });
    var skip = document.createElement("button");
    skip.type = "button";
    skip.className = "ghost ghost--sm";
    skip.textContent = "忽略";
    skip.addEventListener("click", function () { box.remove(); });
    box.appendChild(txt);
    box.appendChild(look);
    box.appendChild(skip);
    stageInput.parentNode.insertBefore(box, stageInput);
    return true;
  }

  /* ---------- 兑换 ---------- */
  function setBusy(v) {
    busy = v;
    submitBtn.disabled = v;
    submitBtn.classList.toggle("is-busy", v);
    submitBtn.querySelector(".cta__label").textContent = v ? "正在核验" : "立即兑换";
  }

  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    if (busy) return;
    clearAlert();
    // ★R705：先从输入/粘贴的内容里抽取兑换码，抽不到再给具体原因
    var code = extractCode(codeEl.value);
    if (!code) {
      var n = alnum(codeEl.value);
      if (/:\/\//.test(codeEl.value) || /\//.test(codeEl.value)) {
        showAlert("你粘进来的是链接：请只填链接末尾 /redeem/ 后面的兑换码（20 位，形如 XXXXX-XXXXX-XXXXX-XXXXX）");
      } else if (!n) {
        showAlert("请输入兑换码（20 位，形如 XXXXX-XXXXX-XXXXX-XXXXX）");
      } else if (n.length !== 20) {
        showAlert("这串只有 " + n.length + " 位（" + fmtCode(n) + "）——本站兑换码是 20 位（5-5-5-5 四组），请确认复制完整");
      } else {
        showAlert("本站兑换码不含 I/O/0/1，这串里出现了，请核对：" + fmtCode(n));
      }
      codeEl.focus();
      return;
    }
    codeEl.value = fmtCode(code);

    setBusy(true);
    fetch("/api/redeem/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ code: code })
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: "服务返回异常（" + r.status + "）" }; });
    }).then(function (d) {
      if (!d || !d.ok) {
        showAlert((d && d.message) || "兑换失败，请稍后再试");
        if (d && d.restored) { /* noop */ }
        return;
      }
      saveLocal(d);
      render(d);
      toast("兑换成功");
    }).catch(function () {
      showAlert("网络异常，请检查网络后重试");
    }).finally(function () {
      setBusy(false);
    });
  });

  codeEl.addEventListener("input", function () {
    // ★R705：只有「纯字母数字/横线/空格」才自动分组；
    //   粘贴链接或带说明的文字时原样保留，提交时再抽取（避免被拐成假码）
    if (!/[^A-Za-z0-9\-\s]/.test(codeEl.value)) {
      var pos = codeEl.selectionStart;
      var before = codeEl.value.length;
      codeEl.value = fmtCode(codeEl.value);
      var after = codeEl.value.length;
      try { codeEl.setSelectionRange(pos + (after - before), pos + (after - before)); } catch (e) {}
    }
    clearAlert();
  });

  codeEl.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter") { ev.preventDefault(); form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit")); }
  });

  /* ---------- 粘贴 ---------- */
  $("paste").addEventListener("click", function () {
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(function (t) {
        var c = extractCode(t);
        codeEl.value = c ? fmtCode(c) : String(t || "").slice(0, 120);
        clearAlert();
        toast(c ? "已识别兑换码" : "没识别到 20 位兑换码，请检查剪贴板内容");
      }).catch(function () {
        codeEl.focus();
        toast("请长按输入框粘贴");
      });
    } else {
      codeEl.focus();
      toast("请长按输入框粘贴");
    }
  });

  /* ---------- 复制全部 / 下载 ---------- */
  $("copy-all").addEventListener("click", function () {
    var urls = (current && current.links) || [];
    if (!urls.length) return;
    promiseToVoid(copyText(urls.join("\n")).then(function () { toast("已复制 " + urls.length + " 条链接"); })
      .catch(function () { toast("复制失败，请手动选择"); }));
  });

  function currentData() {
    if (current) return current;
    var hit = newestLocal();
    return hit ? hit.data : null;
  }

  $("download").addEventListener("click", function () {
    var d = currentData();
    if (!d || !d.links || !d.links.length) { toast("没有可下载的数据"); return; }
    var lines = [
      "卡密兑换结果",
      "========================",
      "兑换码尾号: " + (d.tail || "—"),
      "批次: " + (d.batch_no || "-"),
      "组号: " + (d.group_no ? String(d.group_no).padStart(2, "0") : "-"),
      "数量: " + d.links.length + " 条",
      "兑换时间（北京时间）: " + fmtTime(d.redeemed_at),
      "",
      "卡密链接:"
    ];
    d.links.forEach(function (u, i) { lines.push((i + 1) + ". " + u); });
    lines.push("", "请妥善保管，每张卡密仅可使用一次。");
    var blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/plain;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "卡密链接_" + (d.batch_no || "batch") + "_组" + (d.group_no || "") + ".txt";
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 800);
    toast("TXT 已开始下载");
  });

  /* ---------- 清除 ---------- */
  $("clear").addEventListener("click", function () {
    if (!window.confirm("清除后本页将不再保留兑换结果，且无法恢复。确定清除吗？")) return;
    try {
      for (var i = localStorage.length - 1; i >= 0; i--) {
        var k = localStorage.key(i);
        if (k && k.indexOf(LS_PREFIX) === 0) localStorage.removeItem(k);
      }
      localStorage.removeItem(LS_OLD);
    } catch (e) {}
    fetch("/api/redeem/reset", { method: "POST", credentials: "same-origin" }).catch(function () {});
    current = null;
    stageResult.hidden = true;
    stageInput.hidden = false;
    codeEl.value = "";
    clearAlert();
    window.scrollTo({ top: 0, behavior: "smooth" });
    setTimeout(function () { codeEl.focus(); }, 220);
    toast("已清除本页数据");
  });

  /* ---------- 启动 ---------- */
  prefillFromUrl();   // ★R705b：必须等 RE_CODE/extractCode 都初始化完（它们在上面的工具段）
  if (!restore() && !codeEl.value) {
    setTimeout(function () { codeEl.focus(); }, 260);
  }
})();