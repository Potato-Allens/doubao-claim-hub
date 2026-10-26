/* R213 · 总后台内嵌「卡密链接分组 / 兑换站」面板 --------------------------- */
(function () {
  "use strict";
  var root = document.getElementById("rdPanel");
  if (!root) return;

  var $ = function (id) { return document.getElementById(id); };
  var META = { max_per_group: 500, max_codes: 500, max_total: 5000 };
  var last = null;
  var openDetail = {};

  function say(msg) {
    if (typeof window.toast === "function") { window.toast(msg); return; }
    var t = document.getElementById("toast");
    if (!t) return;
    t.textContent = msg;
    t.classList.add("show");
    setTimeout(function () { t.classList.remove("show"); }, 1800);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function calc() {
    var mode = $("rdMode").value;
    var a = parseInt($("rdA").value || "0", 10);
    var b = parseInt($("rdB").value || "0", 10);
    var per = 0, total = 0;
    if (mode === "total") {
      per = (b > 0 && a > 0 && a % b === 0) ? a / b : 0;
      total = a;
    } else {
      per = a; total = a * b;
    }
    $("rdA").previousElementSibling; /* noop */
    $("rdCalc").textContent = "将生成 " + (b > 0 ? b : 0) + " 个兑换码 × 每组 " +
      (per > 0 ? per : 0) + " 条 = " + (total > 0 ? total : 0) + " 条卡密链接";
    $("rdCalc").style.color = (total <= 0 || total > META.max_total) ? "#ffa3b6" : "";
    return { mode: mode, a: a, b: b, per: per, total: total,
             bad: total <= 0 || total > META.max_total || (mode === "total" && (b <= 0 || a % b !== 0)) };
  }

  $("rdMode").addEventListener("change", function () {
    $("rdALabel").textContent = $("rdMode").value === "total" ? "链接总数" : "每组链接数";
    calc();
  });
  $("rdA").addEventListener("input", calc);
  $("rdB").addEventListener("input", calc);

  /* ---------- 创建 ---------- */
  $("rdCreate").addEventListener("click", function () {
    var c = calc();
    if (c.b < 1) { say("兑换码数量至少为 1"); return; }
    if (c.bad) { say(c.mode === "total" ? "总链接数必须能被兑换码数整除，且不超过上限" : "总链接数超过上限"); return; }
    var btn = $("rdCreate");
    btn.disabled = true;
    btn.textContent = "生成中…";
    fetch("/admin/api/redeem/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ mode: c.mode, per_group: c.a, code_count: c.b, note: $("rdNote").value || "" })
    }).then(function (r) { return r.json().catch(function () { return { ok: false, detail: "HTTP " + r.status }; }); })
      .then(function (d) {
        if (!d || !d.ok) { say((d && (d.detail || d.message)) || "生成失败"); return; }
        last = d;
        renderResult(d);
        load();
        say("已生成 " + d.code_count + " 个兑换码 / " + d.link_count + " 条链接");
      })
      .catch(function () { say("网络异常，请重试"); })
      .finally(function () { btn.disabled = false; btn.textContent = "生成兑换码"; });
  });

  function renderResult(d) {
    $("rdResult").classList.remove("hide");
    $("rdResultMeta").textContent = "批次 " + d.batch_no + " · " + d.code_count + " 个码 × 每组 " +
      d.per_group + " 条 = " + d.link_count + " 条链接 · 请立即下载或复制";
    var box = $("rdGroups");
    box.textContent = "";
    (d.groups || []).forEach(function (g) {
      var wrap = el("div", "rd-group");
      var head = el("div", "rd-group__head");
      head.appendChild(el("span", "rd-group__no", "组 " + String(g.group_no).padStart(2, "0")));
      head.appendChild(el("span", "rd-code", g.code));
      wrap.appendChild(head);
      var links = el("div", "rd-group__links");
      (g.links || []).forEach(function (u) { links.appendChild(el("span", "link", u)); });
      wrap.appendChild(links);
      box.appendChild(wrap);
    });
    $("rdResult").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function copy(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text).catch(function () { return legacy(text); });
    }
    return Promise.resolve(legacy(text));
  }
  function legacy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.style.cssText = "position:fixed;top:-9999px;opacity:0";
    document.body.appendChild(ta); ta.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) {}
    document.body.removeChild(ta);
    if (!ok) throw new Error("copy failed");
    return true;
  }

  $("rdCopyCodes").addEventListener("click", function () {
    if (!last) { say("还没有生成结果"); return; }
    var t = (last.groups || []).map(function (g) { return g.code; }).join("\n");
    Promise.resolve(copy(t)).then(function () { say("已复制全部兑换码"); })
      .catch(function () { say("复制失败"); });
  });

  $("rdDownload").addEventListener("click", function () {
    if (!last) { say("还没有生成结果"); return; }
    var lines = ["# 兑换批次 " + last.batch_no,
      "# 每组 " + last.per_group + " 条 · " + last.code_count + " 个兑换码 · 共 " + last.link_count + " 条链接", ""];
    (last.groups || []).forEach(function (g) {
      lines.push("[组 " + String(g.group_no).padStart(2, "0") + "] 兑换码: " + g.code);
      (g.links || []).forEach(function (u) { lines.push("    " + u); });
      lines.push("");
    });
    var blob = new Blob(["\ufeff" + lines.join("\r\n")], { type: "text/plain;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "兑换批次_" + last.batch_no + ".txt";
    document.body.appendChild(a); a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 800);
    say("TXT 已开始下载");
  });

  /* ---------- 历史批次 ---------- */
  function load() {
    fetch("/admin/api/redeem/list?limit=50", { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        var body = $("rdBatchBody");
        body.textContent = "";
        if (!(d.items || []).length) {
          var tr = document.createElement("tr");
          var td = el("td", "muted", "还没有批次，先在上面生成一批吧");
          td.colSpan = 6; tr.appendChild(td); body.appendChild(tr);
          return;
        }
        (d.items || []).forEach(function (b) {
          var tr = document.createElement("tr");
          tr.appendChild(el("td", "code", b.batch_no));
          tr.appendChild(el("td", "", b.note || "—"));
          tr.appendChild(el("td", "", String(b.codes)));
          tr.appendChild(el("td", "", String(b.used)));
          tr.appendChild(el("td", "muted", b.created || "—"));

          var td = document.createElement("td");
          var row = el("div", "row");
          row.style.gap = "6px";

          var v = el("button", "btn ghost mini", "查看");
          v.type = "button";
          v.addEventListener("click", function () { toggleDetail(b.id, tr); });
          row.appendChild(v);

          var ex = el("button", "btn ghost mini", "导出");
          ex.type = "button";
          ex.addEventListener("click", function () { window.open("/admin/api/redeem/export/" + b.id, "_blank"); });
          row.appendChild(ex);

          var off = el("button", "btn ghost mini", "停用");
          off.type = "button";
          off.addEventListener("click", function () {
            if (!window.confirm("停用批次 " + b.batch_no + " 中所有未使用的兑换码？")) return;
            fetch("/admin/api/redeem/disable", {
              method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
              body: JSON.stringify({ batch_id: b.id })
            }).then(function (r) { return r.json(); }).then(function (res) {
              say((res && res.message) || "已处理"); load();
            }).catch(function () { say("操作失败"); });
          });
          row.appendChild(off);

          td.appendChild(row);
          tr.appendChild(td);
          body.appendChild(tr);
        });
      })
      .catch(function () { say("批次列表加载失败"); });
  }

  function toggleDetail(id, tr) {
    var next = tr.nextElementSibling;
    if (next && next.classList.contains("rd-detail")) { next.remove(); return; }
    var dtr = document.createElement("tr");
    dtr.className = "rd-detail";
    var td = document.createElement("td");
    td.colSpan = 6;
    td.textContent = "加载中…";
    dtr.appendChild(td);
    tr.parentNode.insertBefore(dtr, tr.nextSibling);

    fetch("/admin/api/redeem/batch/" + id, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) { td.textContent = "加载失败"; return; }
        td.textContent = "";
        var wrap = el("div", "rd-groups");
        (d.items || []).forEach(function (it) {
          var g = el("div", "rd-group");
          var head = el("div", "rd-group__head");
          head.appendChild(el("span", "rd-group__no", "组 " + String(it.group_no).padStart(2, "0")));
          head.appendChild(el("span", "tag " + (it.state === "used" ? "done" : it.state === "disabled" ? "disabled" : "unused"),
            it.state === "used" ? "已兑换" : it.state === "disabled" ? "已停用" : "未兑换"));
          head.appendChild(el("span", "rd-code", it.code_plain));
          g.appendChild(head);
          var links = el("div", "rd-group__links");
          (it.links || []).forEach(function (u) { links.appendChild(el("span", "link", u)); });
          g.appendChild(links);
          wrap.appendChild(g);
        });
        td.appendChild(wrap);
      })
      .catch(function () { td.textContent = "加载失败"; });
  }

  $("rdRefresh").addEventListener("click", function () { load(); say("已刷新"); });

  /* ---------- 启动 ---------- */
  fetch("/admin/api/redeem/meta", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (d) { if (d && d.ok) META = d; })
    .catch(function () {})
    .finally(function () { calc(); load(); });
})();