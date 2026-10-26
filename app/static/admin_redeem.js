/* R213 · 兑换码后台前端 --------------------------------------------------- */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var toastTimer = 0, lastResult = null, META = { max_per_group: 500, max_codes: 500, max_total: 5000 };

  function toast(msg) {
    var t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.classList.remove("show"); }, 2000);
  }

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function showAlert(msg) {
    var a = $("alert");
    a.textContent = msg;
    a.hidden = false;
  }
  function clearAlert() { $("alert").hidden = true; }

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

  /* ---------- 计算 ---------- */
  function calc() {
    var mode = $("mode").value;
    var a = parseInt($("num-a").value || "0", 10);
    var b = parseInt($("num-b").value || "0", 10);
    var per = 0, total = 0;
    if (mode === "total") {
      per = (b > 0 && a > 0 && a % b === 0) ? a / b : 0;
      total = a;
    } else {
      per = a; total = a * b;
    }
    $("calc-codes").textContent = String(b > 0 ? b : 0);
    $("calc-per").textContent = String(per > 0 ? per : 0);
    $("calc-total").textContent = String(total > 0 ? total : 0);
    $("lab-a").textContent = mode === "total" ? "卡密链接总数" : "每组卡密链接数量";
    // 不合法时给个轻提示
    var bad = total <= 0 || total > META.max_total || (mode === "total" && (b <= 0 || a % b !== 0));
    $("calc").style.borderColor = bad ? "rgba(251,113,133,.4)" : "rgba(148,163,204,.2)";
    return { mode: mode, a: a, b: b, per: per, total: total, bad: bad };
  }

  ["mode", "num-a", "num-b"].forEach(function (id) {
    $(id).addEventListener("input", calc);
    $(id).addEventListener("change", calc);
  });

  /* ---------- 创建 ---------- */
  $("create-form").addEventListener("submit", function (ev) {
    ev.preventDefault();
    clearAlert();
    var c = calc();
    if (c.b < 1) { showAlert("兑换码数量至少为 1"); return; }
    if (c.bad) {
      showAlert(c.mode === "total" ? "总链接数必须能被兑换码数量整除，且不超过上限"
        : "请检查数量，总链接数不能超过 " + META.max_total);
      return;
    }
    if (c.per > 50 && !window.confirm("每组 " + c.per + " 条链接，确认要生成这么多吗？")) return;
    var btn = $("create-btn");
    btn.disabled = true;
    btn.querySelector("span").textContent = "生成中…";

    fetch("/admin/api/redeem/create", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        mode: c.mode,
        per_group: c.a,
        code_count: c.b,
        note: $("note").value || ""
      })
    }).then(function (r) {
      return r.json().catch(function () { return { ok: false, detail: "HTTP " + r.status }; });
    }).then(function (d) {
      if (!d || !d.ok) {
        showAlert((d && (d.detail || d.message)) || "生成失败");
        return;
      }
      lastResult = d;
      renderResult(d);
      toast("已生成 " + d.code_count + " 个兑换码 / " + d.link_count + " 条链接");
      loadList();
    }).catch(function () {
      showAlert("网络异常，请重试");
    }).finally(function () {
      btn.disabled = false;
      btn.querySelector("span").textContent = "生成兑换码";
    });
  });

  var RES_PAGE = 20, resPage = 1;

  function renderResult(d) {
    $("result-panel").hidden = false;
    $("result-sub").textContent = "批次 " + d.batch_no + " · " + d.code_count +
      " 个兑换码 × 每组 " + d.per_group + " 条 = " + d.link_count +
      " 条链接。链接不再一次性下发，请用「下载 TXT」或进批次详情查看。";
    resPage = 1;
    paintResultGroups();
    $("result-panel").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function paintResultGroups() {
    var box = $("result-list");
    if (!box) return;
    box.textContent = "";
    var groups = (lastResult && lastResult.groups) || [];
    groups.slice((resPage - 1) * RES_PAGE, resPage * RES_PAGE).forEach(function (g) {
      var wrap = el("div", "rgroup");
      var head = el("div", "rgroup__head");
      head.appendChild(el("span", "rgroup__no", "GROUP " + String(g.group_no).padStart(2, "0")));
      head.appendChild(el("span", "code", g.code));
      wrap.appendChild(head);
      box.appendChild(wrap);
    });
    paintPager(box, groups.length, RES_PAGE, resPage, function (p) {
      resPage = p;
      paintResultGroups();
    });
  }

  function paintPager(host, total, size, page, go) {
    if (!host) return;
    if (!host.id) host.id = "pgr-" + Math.random().toString(36).slice(2, 8);
    var pid = host.id + "-pager";
    var bar = document.getElementById(pid);
    if (!bar) {
      bar = el("div", "pager");
      bar.id = pid;
      host.parentNode.insertBefore(bar, host.nextSibling);
    }
    bar.textContent = "";
    var pages = Math.max(1, Math.ceil(total / size));
    if (pages <= 1) return;
    var mk = function (txt, p, dis) {
      var b = el("button", "ghost ghost--sm", txt);
      b.type = "button";
      b.disabled = !!dis;
      b.addEventListener("click", function () { go(p); });
      return b;
    };
    bar.appendChild(mk("上一页", page - 1, page <= 1));
    bar.appendChild(el("span", "pager__now", page + " / " + pages));
    bar.appendChild(mk("下一页", page + 1, page >= pages));
  }

  function allCodesText() {
    if (!lastResult) return "";
    return (lastResult.groups || []).map(function (g) { return g.code; }).join("\n");
  }

  $("copy-codes").addEventListener("click", function () {
    var t = allCodesText();
    if (!t) { toast("没有可复制的兑换码"); return; }
    Promise.resolve(copy(t)).then(function () { toast("已复制全部兑换码"); })
      .catch(function () { toast("复制失败，请手动选择"); });
  });

  $("download-result").addEventListener("click", function () {
    if (!lastResult) { toast("没有可下载的结果"); return; }
    window.open("/admin/api/redeem/export/" + lastResult.batch_id, "_blank");
    toast("TXT 已开始下载");
  });

  /* ---------- 列表 ---------- */
  var LIST_PAGE = 20, listPage = 1;

  function loadList(page) {
    if (page) listPage = page;
    fetch("/admin/api/redeem/list?limit=" + LIST_PAGE + "&offset=" + ((listPage - 1) * LIST_PAGE), { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        var body = $("batch-body");
        body.textContent = "";
        (d.items || []).forEach(function (b) {
          var tr = document.createElement("tr");
          tr.appendChild(el("td", "mono", b.batch_no));
          tr.appendChild(el("td", "", b.note || "—"));
          tr.appendChild(el("td", "mono", String(b.codes)));
          tr.appendChild(el("td", "mono", String(b.used)));
          tr.appendChild(el("td", "mono", b.created || "—"));

          var td = document.createElement("td");
          var wrap = el("div", "row-actions");

          var v = el("button", "ghost ghost--sm", "查看");
          v.type = "button";
          v.addEventListener("click", function () { openDrawer(b.id, b.batch_no); });
          wrap.appendChild(v);

          var ex = el("button", "ghost ghost--sm", "导出");
          ex.type = "button";
          ex.addEventListener("click", function () { window.open("/admin/api/redeem/export/" + b.id, "_blank"); });
          wrap.appendChild(ex);

          var off = el("button", "ghost ghost--sm ghost--danger", "停用");
          off.type = "button";
          off.addEventListener("click", function () {
            if (!window.confirm("停用批次 " + b.batch_no + " 中所有未使用的兑换码？")) return;
            fetch("/admin/api/redeem/disable", {
              method: "POST", headers: { "Content-Type": "application/json" }, credentials: "same-origin",
              body: JSON.stringify({ batch_id: b.id })
            }).then(function (r) { return r.json(); }).then(function (res) {
              toast((res && res.message) || "已处理");
              loadList();
            }).catch(function () { toast("操作失败"); });
          });
          wrap.appendChild(off);
          td.appendChild(wrap);
          tr.appendChild(td);
          body.appendChild(tr);
        });
        if (!(d.items || []).length) {
          var tr = document.createElement("tr");
          var td = el("td", "empty", "还没有批次，先在上方创建一批吧");
          td.colSpan = 6;
          tr.appendChild(td);
          body.appendChild(tr);
        }
        var st = d.stats || {};
        $("s-batches").textContent = String(st.batches || d.total || 0);
        $("s-codes").textContent = String(st.codes || 0);
        $("s-used").textContent = String(st.used || 0);
        $("s-left").textContent = String(st.left || 0);
        var tbl = document.querySelector(".tbl");
        if (tbl && !tbl.id) tbl.id = "batch-table";
        paintPager(tbl, d.total || 0, LIST_PAGE, listPage, function (p) { loadList(p); });
      })
      .catch(function () { toast("列表加载失败"); });
  }

  /* ---------- 详情抽屉 ---------- */
  var DRAW_PAGE = 20, drawPage = 1, drawId = 0;

  function openDrawer(id, batchNo) {
    drawId = id;
    drawPage = 1;
    $("drawer").hidden = false;
    $("drawer-title").textContent = "批次 " + batchNo;
    loadDrawer();
  }

  function loadDrawer() {
    $("drawer-body").textContent = "加载中…";
    fetch("/admin/api/redeem/batch/" + drawId + "?page=" + drawPage + "&size=" + DRAW_PAGE, { credentials: "same-origin" })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) { $("drawer-body").textContent = "加载失败"; return; }
        var box = $("drawer-body");
        box.textContent = "";
        (d.items || []).forEach(function (it) {
          var wrap = el("div", "rgroup");
          var head = el("div", "rgroup__head");
          head.appendChild(el("span", "rgroup__no", "GROUP " + String(it.group_no).padStart(2, "0")));
          var st = el("span", "tag " + (it.state === "used" ? "tag--ok" : it.state === "disabled" ? "tag--off" : ""),
            it.state === "used" ? "已兑换" : it.state === "disabled" ? "已停用" : "未兑换");
          head.appendChild(st);
          wrap.appendChild(head);
          var body = el("div", "rgroup__links");
          body.appendChild(el("span", "", "兑换码: " + it.code_plain));
          var arr = it.links || [];
          arr.slice(0, 10).forEach(function (u) { body.appendChild(el("span", "", u)); });
          if (arr.length > 10) body.appendChild(el("span", "", "… 共 " + arr.length + " 条，完整列表请用「导出」"));
          wrap.appendChild(body);
          box.appendChild(wrap);
        });
        paintPager(box, d.total || 0, DRAW_PAGE, d.page || 1, function (p) {
          drawPage = p;
          loadDrawer();
        });
      })
      .catch(function () { $("drawer-body").textContent = "加载失败"; });
  }

  $("drawer").addEventListener("click", function (ev) {
    if (ev.target && ev.target.getAttribute && ev.target.getAttribute("data-close")) {
      $("drawer").hidden = true;
    }
  });

  $("refresh").addEventListener("click", function () { loadList(); toast("已刷新"); });

  /* ---------- 启动 ---------- */
  fetch("/admin/api/redeem/meta", { credentials: "same-origin" })
    .then(function (r) { return r.json(); })
    .then(function (d) { if (d && d.ok) META = d; })
    .catch(function () {})
    .finally(function () { calc(); loadList(); });
})();