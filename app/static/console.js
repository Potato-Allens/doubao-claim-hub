/* 豆包卡密 · 运营控制台（总台 L0 / 代理商 L1 / 子代理 L2 共用一套前端） */
const $ = (id) => document.getElementById(id);

let ME = null, BASE = "/a/api", LOGIN_URL = "/a/login", IS_TOTAL = false;
const lvNum = () => Number((ME && ME.level) || 0) || 0;
const isRO = () => String((ME && ME.status) || "") === "disabled";
const state = { view: "dash", mounted: "" };
const PAGES = {
  tenants: { page: 0, size: 20, total: 0, pages: 1 },
  codes: { page: 0, size: 20, total: 0, pages: 1 },
  logs: { page: 0, size: 50, total: 0, pages: 1 },
  ledger: { page: 0, size: 50, total: 0, pages: 1 },
  rcLedger: { page: 0, size: 50, total: 0, pages: 1 },
  audit: { page: 0, size: 50, total: 0, pages: 1 },
  rcAdm: { page: 0, size: 50, total: 0, pages: 1 }
};
const LOAD = {};

/* ============================ 基础工具 ============================ */
function esc(v) {
  return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
    return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
  });
}
function num(v) { const n = parseInt(v == null ? 0 : v, 10); return isNaN(n) ? 0 : n; }
function pick(o, keys, d) {
  if (o) for (let i = 0; i < keys.length; i++) {
    const v = o[keys[i]];
    if (v !== undefined && v !== null && v !== "") return v;
  }
  return (d === undefined ? "—" : d);
}
function fmtTime(v) {
  if (v === undefined || v === null || v === "") return "—";
  if (typeof v === "string" && v.indexOf("-") > 0) return v;
  const n = Number(v); if (!n) return "—";
  const d = new Date(n * 1000);
  // ★R413_TZ_CST：代理/子代理在境外也统一显示北京时间（以前用 getHours() 跟着浏览器时区走）
  try {
    const o = {};
    new Intl.DateTimeFormat("en-CA", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit",
      day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
      .formatToParts(d).forEach((p) => { o[p.type] = p.value; });
    if (o.year) return o.year + "-" + o.month + "-" + o.day + " " + o.hour + ":" + o.minute;
  } catch (e) {}
  const p = (x) => (x < 10 ? "0" : "") + x;
  const t = new Date(d.getTime() + 8 * 3600 * 1000);
  return t.getUTCFullYear() + "-" + p(t.getUTCMonth() + 1) + "-" + p(t.getUTCDate()) + " " + p(t.getUTCHours()) + ":" + p(t.getUTCMinutes());
}
function lvlText(l) { const n = num(l); return n === 0 ? "总台" : (n === 1 ? "代理商" : "子代理"); }
function lvlCls(l) { const n = num(l); return n === 0 ? "lv0" : (n === 1 ? "lv1" : "lv2"); }
function stText(s) { return { active: "正常", disabled: "已停用", banned: "已封禁" }[s] || (s || "—"); }
function stCls(s) { return { active: "done", disabled: "visiting", banned: "disabled" }[s] || "unused"; }
function codeSt(s) { return { unused: "未使用", visiting: "已进入领取", done: "已完成领取", disabled: "已停用" }[s] || (s || "—"); }
function codeCls(s) { return { unused: "unused", visiting: "visiting", done: "done", disabled: "disabled" }[s] || "unused"; }
function kindText(k) { return { enter: "进入领取", scan: "已扫码", login: "登录成功", success: "领取成功", fail: "未到账" }[k] || (k || "—"); }
function kindCls(k) { return { enter: "unused", scan: "visiting", login: "done", success: "done", fail: "disabled" }[k] || "unused"; }
function ledgerKind(k) { return { grant: "发放", recycle: "回收", allocate: "划拨", spend: "建码消耗", refund: "回滚", recharge: "充值码充值" }[k] || (k || "—"); }
function claimsText(v) { const n = num(v); return n > 0 ? n + " 次" : "不限次"; }
function dlt(v) { const n = num(v); return (n > 0 ? "+" : "") + n; }
function dltCls(v) { return num(v) >= 0 ? "up" : "down"; }
function avail(t) {
  if (t && t.available !== undefined && t.available !== null) return num(t.available);
  return num(t && t.quota_total) - num(t && t.quota_given) - num(t && t.quota_used);
}
function toast(msg) {
  const t = $("toast"); if (!t) return;
  t.textContent = String(msg == null ? "" : msg); t.classList.add("show");
  clearTimeout(window._tt); window._tt = setTimeout(() => t.classList.remove("show"), 2600);
}
function setHtml(el, html) { if (el && el.innerHTML !== html) el.innerHTML = html; }

/* 复制：navigator.clipboard 优先，execCommand 兜底（生产是 http 非安全上下文） */
function copyText(txt, el) {
  const val = String(txt == null ? "" : txt);
  if (!val) { toast("没有可复制的内容"); return; }
  const done = () => { if (el) flashCopied(el); toast("已复制 ✓"); };
  const fallback = () => {
    try {
      const ta = document.createElement("textarea");
      ta.value = val; ta.setAttribute("readonly", "readonly");
      ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.left = "-1000px"; ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select(); ta.setSelectionRange(0, val.length);
      const ok = document.execCommand("copy");
      document.body.removeChild(ta);
      if (ok) done(); else toast("复制失败，请长按选中后手动复制");
    } catch (e) { toast("复制失败，请长按选中后手动复制"); }
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(val).then(done).catch(fallback);
  } else fallback();
}
function flashCopied(el) {
  if (!el || !el.dataset) return;
  if (!el.dataset.old) el.dataset.old = el.textContent;
  el.textContent = "已复制 ✓";
  el.classList.add("copied");
  clearTimeout(el._ct);
  el._ct = setTimeout(() => {
    el.textContent = el.dataset.old;
    el.classList.remove("copied");
  }, 1100);
}
document.addEventListener("click", (e) => {
  const t = e.target.closest("[data-copy]");
  if (t) copyText(t.getAttribute("data-copy"), t);
});

/* 导出 TXT */
function download(name, text) {
  try {
    const blob = new Blob(["\ufeff" + text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click();
    setTimeout(() => { try { URL.revokeObjectURL(a.href); a.remove(); } catch (e) {} }, 900);
    toast("已开始下载 " + name);
  } catch (e) { toast("导出失败，请重试"); }
}
function stampName(tag) {
  const d = new Date(), p = (x) => (x < 10 ? "0" : "") + x;
  return tag + "-" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes()) + ".txt";
}

/* ============================ 请求 ============================ */
async function req(path, opt) {
  const o = Object.assign({ cache: "no-store", credentials: "same-origin" }, opt || {});
  if (o.body && typeof o.body !== "string") {
    o.headers = Object.assign({ "Content-Type": "application/json" }, o.headers || {});
    o.body = JSON.stringify(o.body);
  }
  let r;
  try { r = await fetch(BASE + path, o); }
  catch (e) { throw new Error("连不上服务器，请检查网络后重试"); }
  let j = null;
  try { j = await r.json(); } catch (e) { j = null; }
  if (r.status === 401) { location.href = LOGIN_URL; throw new Error("登录已过期，请重新登录"); }
  if (!j) {
    if (r.status === 403 && IS_TOTAL) { location.href = "/admin"; }
    throw new Error(r.ok ? "服务器返回的内容看不懂，请刷新重试" : "请求没有成功，请稍后再试");
  }
  if (j.ok === false) throw new Error(j.message || "操作没有成功，请稍后再试");
  return j;
}
async function rawGet(path) {
  try {
    const r = await fetch(path, { cache: "no-store", credentials: "same-origin" });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) { return null; }
}

/* ============================ 菜单 / 视图路由 ============================ */
const MENU = [
  { k: "dash", t: "仪表盘", ic: "▤", lv: [0, 1, 2] },
  { k: "site", t: "入口网址", ic: "⌘", lv: [0, 1, 2] },
  { k: "tenants", t: "代理商", ic: "☰", lv: [0, 1] },
  { k: "ledger", t: "额度流水", ic: "⇄", lv: [0, 1, 2] },
  { k: "recharge", t: "充值码", ic: "＋", lv: [0, 1, 2] },
  { k: "audit", t: "审计日志", ic: "⛨", lv: [0, 1, 2] },
  { k: "codes", t: "卡密列表", ic: "▣", lv: [0, 1, 2] },
  { k: "logs", t: "领取记录", ic: "◷", lv: [0, 1, 2] },
  { k: "proxy", t: "代理池", ic: "⇌", lv: [0] }
];
function buildNav() {
  const lv = lvNum();
  const host = $("nav");
  host.innerHTML = MENU.filter((m) => m.lv.indexOf(lv) >= 0).map((m) =>
    '<button type="button" data-view="' + m.k + '"><span class="ni">' + m.ic + "</span><span>" + m.t + "</span></button>"
  ).join("");
  host.querySelectorAll("[data-view]").forEach((b) => {
    b.onclick = () => { state.view = b.getAttribute("data-view"); render(); };
  });
}
function render() {
  const lv = lvNum();
  if (!MENU.some((m) => m.k === state.view && m.lv.indexOf(lv) >= 0)) state.view = "dash";
  $("nav").querySelectorAll("[data-view]").forEach((b) => {
    b.classList.toggle("on", b.getAttribute("data-view") === state.view);
  });
  const v = VIEWS[state.view];
  $("vt").textContent = v.title;
  $("vsub").textContent = v.sub || "";
  const host = $("views");
  host.innerHTML = "";
  state.mounted = state.view;
  v.mount(host);
  enhanceSelects();
  if (state.view !== "tenants" && state.tnSel) exitSel();
  syncTabbar();
  ensureFab();
}

/* ============================ 分页组件 ============================ */
function pagerHtml(name, sizes) {
  const sz = sizes || [20, 50, 100];
  const st = PAGES[name];
  const opts = sz.map((n) => '<option value="' + n + '"' + (n === st.size ? " selected" : "") + ">每页 " + n + " 条</option>").join("");
  return '<div class="pager">' +
    '<span class="muted" id="pgt-' + name + '">—</span>' +
    '<select id="pgs-' + name + '" data-pgs="' + name + '">' + opts + "</select>" +
    '<button class="btn ghost mini" type="button" data-pg="' + name + '" data-act="first">首页</button>' +
    '<button class="btn ghost mini" type="button" data-pg="' + name + '" data-act="prev">上一页</button>' +
    '<button class="btn ghost mini" type="button" data-pg="' + name + '" data-act="next">下一页</button>' +
    '<button class="btn ghost mini" type="button" data-pg="' + name + '" data-act="last">尾页</button>' +
    "</div>";
}
function updPager(name) {
  const st = PAGES[name];
  const el = $("pgt-" + name);
  if (el) el.textContent = "第 " + (st.page + 1) + " / " + st.pages + " 页 · 共 " + st.total + " 条";
  ["prev", "first"].forEach((a) => {
    const b = document.querySelector('[data-pg="' + name + '"][data-act="' + a + '"]');
    if (b) b.disabled = st.page <= 0;
  });
  ["next", "last"].forEach((a) => {
    const b = document.querySelector('[data-pg="' + name + '"][data-act="' + a + '"]');
    if (b) b.disabled = st.page >= st.pages - 1;
  });
  const s = $("pgs-" + name);
  if (s && String(st.size) !== s.value) s.value = String(st.size);
}
function applyPage(j, name) {
  const st = PAGES[name];
  st.total = num(j.total !== undefined ? j.total : ((j.rows || []).length));
  st.pages = Math.max(1, num(j.pages) || Math.ceil(st.total / st.size) || 1);
  if (st.page > st.pages - 1) st.page = st.pages - 1;
  if (st.page < 0) st.page = 0;
  updPager(name);
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-pg]");
  if (!b) return;
  const n = b.getAttribute("data-pg"), st = PAGES[n], act = b.getAttribute("data-act");
  if (act === "first") st.page = 0;
  else if (act === "prev") st.page = Math.max(0, st.page - 1);
  else if (act === "next") st.page = Math.min(st.pages - 1, st.page + 1);
  else if (act === "last") st.page = Math.max(0, st.pages - 1);
  if (LOAD[n]) LOAD[n]();
});
document.addEventListener("change", (e) => {
  const s = e.target.closest("[data-pgs]");
  if (!s) return;
  const n = s.getAttribute("data-pgs");
  PAGES[n].size = num(s.value) || 20;
  PAGES[n].page = 0;
  if (LOAD[n]) LOAD[n]();
});

/* ============================ 自制深色下拉 ============================ */
let _selBound = false;
function closeAllSel() {
  document.querySelectorAll(".wzsel.open").forEach((w) => w.classList.remove("open"));
  document.querySelectorAll(".panel.z-top").forEach((p) => p.classList.remove("z-top"));
}
function syncSelectLabels() {
  document.querySelectorAll(".wzsel").forEach((w) => {
    const sel = w.querySelector("select"), lb = w.querySelector(".lb");
    if (sel && lb) { const op = sel.options[sel.selectedIndex]; lb.textContent = op ? op.textContent : ""; }
  });
}
function enhanceSelects() {
  document.querySelectorAll("select").forEach((sel) => {
    if (sel.dataset.wzsel === "1" || sel.multiple || sel.getAttribute("data-plain") === "1") return;
    sel.dataset.wzsel = "1";
    const wrap = document.createElement("span");
    wrap.className = "wzsel";
    sel.parentNode.insertBefore(wrap, sel);
    wrap.appendChild(sel);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "wzsel-btn";
    btn.innerHTML = '<span class="lb"></span><span class="arw">\u25be</span>';
    const list = document.createElement("div");
    list.className = "wzsel-list";
    wrap.appendChild(btn); wrap.appendChild(list);
    if (/^[0-9]/.test(sel.style.width || "")) wrap.style.minWidth = sel.style.width;
    wrap.addEventListener("click", (ev) => ev.stopPropagation());
    btn.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (wrap.classList.contains("open")) { closeAllSel(); return; }
      closeAllSel();
      list.innerHTML = "";
      Array.from(sel.options).forEach((op) => {
        const it = document.createElement("div");
        it.className = "wzsel-item" + (op.value === sel.value ? " on" : "");
        it.innerHTML = "<span>" + esc(op.textContent) + '</span><span class="ck">\u2713</span>';
        it.addEventListener("click", (ev2) => {
          ev2.stopPropagation();
          sel.value = op.value;
          sel.dispatchEvent(new Event("change", { bubbles: true }));
          closeAllSel(); syncSelectLabels();
        });
        list.appendChild(it);
      });
      wrap.classList.add("open");
      const pn = wrap.closest(".panel") || wrap.closest(".dlg");
      if (pn) pn.classList.add("z-top");
      const r = wrap.getBoundingClientRect();
      const below = window.innerHeight - r.bottom;
      wrap.classList.toggle("up", below < 300 && r.top > 320);
    });
    sel.addEventListener("change", syncSelectLabels);
  });
  if (!_selBound) {
    _selBound = true;
    document.addEventListener("click", closeAllSel);
    document.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { closeAllSel(); closeModal(); } });
  }
  syncSelectLabels();
}

/* ============================ 弹窗 ============================ */
function closeModal() {
  const root = $("modalRoot");
  if (!root) return;
  root.classList.remove("show");
  root.innerHTML = "";
}
function openModal(title, bodyHtml, onOk, okText) {
  const root = $("modalRoot");
  root.innerHTML =
    '<div class="mask" id="mMask"><div class="dlg"><div class="dlg-h"><b>' + esc(title) + "</b>" +
    '<button class="x" id="mX" type="button">\u2715</button></div>' +
    '<div class="dlg-b">' + bodyHtml + "</div>" +
    '<div class="dlg-f"><button class="btn ghost wide" id="mCancel" type="button">取消</button>' +
    '<button class="btn btn--primary wide" id="mOk" type="button">' + esc(okText || "确定") + "</button></div></div></div>";
  root.classList.add("show");
  root.querySelectorAll(".dlg-err").forEach((n) => n.remove());   // R66d：新一轮清掉旧报错
  $("mX").onclick = closeModal;
  $("mCancel").onclick = closeModal;
  $("mMask").addEventListener("click", (e) => { if (e.target.id === "mMask") closeModal(); });
  $("mOk").onclick = async () => {
    const btn = $("mOk");
    btn.disabled = true;
    try {
      const r = await onOk();
      if (r !== false) closeModal();
    } catch (e) {
      const msg = (e && e.message) ? e.message : "操作失败，请稍后再试";
      // R66d：原来只 toast，而 toast 在弹窗后面看不见 -> 弹窗里也写一行，绝不"点了没反应"
      try {
        const box = root.querySelector(".dlg-b");
        if (box) {
          let err = root.querySelector(".dlg-err");
          if (!err) {
            err = document.createElement("div");
            err.className = "dlg-err";
            box.appendChild(err);
          }
          err.textContent = "⚠ " + msg;
          if (err.scrollIntoView) err.scrollIntoView({ block: "nearest" });
        }
      } catch (_) { /* 忽略 */ }
      toast(msg);
    } finally { btn.disabled = false; }
  };
  enhanceSelects();
}
const mV = (id) => { const r = $("modalRoot"); return r ? r.querySelector("#" + id) : null; };

/* ============================ 仪表盘 ============================ */
const VIEWS = {};
function ledgerRowHtml(x) {
  const after = pick(x, ["balance_after", "after", "left"], "");
  return "<tr>" +
    '<td class="muted mono">' + esc(fmtTime(pick(x, ["time_str", "ts_str", "at_str", "created_at_str", "ts"], ""))) + "</td>" +
    '<td><span class="dlt ' + dltCls(pick(x, ["delta", "amount", "change"], 0)) + '">' + esc(dlt(pick(x, ["delta", "amount", "change"], 0))) + "</span></td>" +
    "<td>" + esc(pick(x, ["kind_text"], ledgerKind(pick(x, ["kind", "type"], "")))) + "</td>" +
    "<td>" + esc(pick(x, ["operator_name", "operator", "by"], "—")) + "</td>" +
    "<td>" + esc(pick(x, ["reason", "detail", "note", "memo"], "—")) + "</td>" +
    '<td class="mnum">' + esc(after === "" ? "—" : after) + "</td></tr>";
}
function tenantStateBtns(t, canManage) {
  if (!canManage) return "";
  const st = String(t.status || "active");
  const id = num(t.id);
  let b = "";
  if (st === "banned") {
    b += '<button class="btn ghost mini" type="button" data-tact="status" data-id="' + id + '" data-st="active">解封</button>';
  } else {
    b += '<button class="btn ghost mini" type="button" data-tact="status" data-id="' + id + '" data-st="' + (st === "disabled" ? "active" : "disabled") + '">' + (st === "disabled" ? "启用" : "停用") + "</button>";
    b += '<button class="btn ghost mini" type="button" data-tact="status" data-id="' + id + '" data-st="banned">封禁</button>';
  }
  return b;
}

VIEWS.dash = {
  title: "仪表盘",
  sub: "额度、卡密与领取情况总览",
  mount(host) {
    host.innerHTML =
      '<div class="cards" id="dashCards"></div>' +
      '<div class="panel"><div class="ph"><h2>最近额度流水</h2><span class="muted" id="dashRecentN">—</span></div>' +
      '<div class="tablewrap"><table><thead><tr><th>时间</th><th>变动</th><th>类型</th><th>操作人</th><th>说明</th><th>变动后余额</th></tr></thead>' +
      '<tbody id="dashRecent"></tbody></table></div></div>' +
      '<div class="panel" id="dashTopPanel"><div class="ph"><h2>额度 Top5</h2><span class="muted">按总额度排序</span></div>' +
      '<div class="tablewrap"><table><thead><tr><th>账号</th><th>层级</th><th>总额度</th><th>已划下级</th><th>自己已用</th><th>剩余可用</th></tr></thead>' +
      '<tbody id="dashTops"></tbody></table></div></div>';
    loadDash();
  },
  reload: () => loadDash()
};
LOAD.dash = () => loadDash();
async function loadDash(quiet) {
  try {
    const j = await req("/dashboard");
    const t = j.tenants || {}, c = j.codes || {}, e = j.events || {}, q = j.quota || {};
    const succ = num(e.success), fail = num(e.fail), tot = num(e.total);
    const den = (succ + fail) > 0 ? (succ + fail) : tot;
    const rate = den > 0 ? Math.round(succ * 100 / den) : 0;
    const cards = [
      { k: "代理商总数", v: num(t.total), s: "代理商 " + num(t.l1) + " · 子代理 " + num(t.l2), c: "acc" },
      { k: "账号状态", v: num(t.active), s: "停用 " + num(t.disabled) + " · 封禁 " + num(t.banned), c: "ok" },
      { k: "卡密总数", v: num(c.total), s: "累计领取 " + num(c.claims) + " 次", c: "acc" },
      { k: "剩余可用额度", v: num(q.left), s: "已发放 " + num(q.given) + " · 已消耗 " + num(q.used), c: "warn" },
      { k: "累计充值", v: num((j.recharge || {}).in), s: "用充值码充进来的张数", c: "ok" },
      { k: "未使用兑换码", v: num((c.by_status || {}).unused), s: "已完成领取 " + num((c.by_status || {}).done), c: "acc" },
      { k: "领取成功 / 未到账", v: succ, s: "未到账 " + fail + " 次", c: "ok" },
      { k: "成功率", v: rate + "%", s: "扫码 " + num(e.scan) + " · 登录 " + num(e.login), c: rate >= 60 ? "ok" : "warn" }
    ];
    setHtml($("dashCards"), cards.map((x) =>
      '<div class="stat ' + x.c + '"><div class="k">' + x.k + '</div><div class="v">' + x.v + '</div><div class="kd">' + x.s + "</div></div>"
    ).join(""));

    const recent = (j.recent || []).slice(0, 10);
    $("dashRecentN").textContent = recent.length ? "共 " + recent.length + " 条" : "";
    setHtml($("dashRecent"), recent.length
      ? recent.map(ledgerRowHtml).join("")
      : '<tr><td colspan="6" class="empty">还没有额度流水</td></tr>');

    const tops = j.tops || [];
    setHtml($("dashTops"), tops.length ? tops.map((x) =>
      "<tr><td><b>" + esc(pick(x, ["username", "name"], "—")) + "</b></td>" +
      '<td><span class="tag ' + lvlCls(pick(x, ["level"], 1)) + '">' + esc(lvlText(pick(x, ["level"], 1))) + "</span></td>" +
      '<td class="mnum">' + esc(num(x.quota_total)) + "</td>" +
      '<td class="mnum">' + esc(num(x.quota_given)) + "</td>" +
      '<td class="mnum">' + esc(num(x.quota_used)) + "</td>" +
      '<td class="mnum">' + esc(avail(x)) + "</td></tr>"
    ).join("") : '<tr><td colspan="6" class="empty">暂无代理商</td></tr>');
  } catch (e) { if (!quiet) toast(e.message || "加载失败"); }
}

/* ============================ 代理商 ============================ */
VIEWS.tenants = {
  title: "代理商",
  sub: "创建账号、发放与回收额度、停用或封禁",
  mount(host) {
    const lv = lvNum();
    const canNew = lv === 0 || lv === 1;
    host.innerHTML =
      '<div class="panel" id="tnPanel"><div class="ph"><h2>' + (lv === 0 ? "代理商列表" : "名下子代理") + '</h2>' +
      '<span class="muted" id="tnStats">—</span></div>' +
      '<div class="tools">' +
      '<input id="tnQ" class="grow" placeholder="搜索账号 / 备注" style="min-width:170px">' +
      (lv === 0 ? '<select id="tnLv"><option value="">全部层级</option><option value="1">代理商</option><option value="2">子代理</option></select>' : "") +
      '<button class="btn ghost mini" type="button" id="tnSearch">搜索</button>' +
      '<span class="sp"></span>' +
      (canNew ? '<button class="btn btn--primary mini" type="button" id="tnNew">新建' + (lv === 0 ? "代理商" : "子代理") + "</button>" : "") +
      (lv === 0 ? '<button class="btn ghost mini" type="button" id="tnBatch">批量新建</button>' : "") +
      (lv === 0 ? '<button class="btn ghost mini danger" type="button" id="tnDel">批量删除</button>' : "") +
      "</div>" +
      '<div class="tablewrap"><table><thead><tr>' +
      '<th class="selcol">选</th>' + "<th>账号</th><th>层级</th><th>状态</th><th>总额度</th><th>已划下级</th><th>自己已用</th><th>剩余可用</th>" +
      "<th>子代理名额</th><th>备注</th><th>创建时间</th><th>最后登录</th><th>专属网址</th><th>操作</th>" +
      "</tr></thead><tbody id=\"tnBody\"></tbody></table></div>" +
      pagerHtml("tenants") + "</div>";
    const go = () => { PAGES.tenants.page = 0; loadTenants(); };
    $("tnSearch").onclick = go;
    $("tnQ").onkeydown = (e) => { if (e.key === "Enter") go(); };
    const lvSel = $("tnLv");
    if (lvSel) lvSel.onchange = go;
    const nb = $("tnNew");
    if (nb) nb.onclick = () => newTenantModal();
    const bb = $("tnBatch");
    if (bb) bb.onclick = () => batchTenantModal();
    const db = $("tnDel");
    if (db) db.onclick = () => enterSel();
    loadTenants();
  },
  reload: () => loadTenants()
};
LOAD.tenants = () => loadTenants();
async function loadTenants() {
  const st = PAGES.tenants;
  const lv = lvNum();
  const q = ($("tnQ") && $("tnQ").value.trim()) || "";
  const lvF = ($("tnLv") && $("tnLv").value) || "";
  const qs = "?limit=" + st.size + "&offset=" + st.page * st.size +
    "&q=" + encodeURIComponent(q) + (lvF ? "&level=" + lvF : "");
  try {
    const j = await req("/tenants" + qs);
    applyPage(j, "tenants");
    const rows = j.rows || [];
    let urlById = {};
    try {
      if (IS_TOTAL) {
        const sj = await rawGet("/admin/api/sites");
        ((sj && sj.rows) || []).forEach((r) => { urlById[num(r.id)] = r.url || ""; });
      } else if (lv === 1) {
        const mj = await req("/my_site");
        ((mj && mj.subs) || []).forEach((x) => { urlById[num(x.id)] = x.url || ""; });
      }
    } catch (e) { /* 忽略 */ }
    const s = j.stats || {};
    const statsTxt = Object.keys(s).length
      ? "共 " + num((j.total !== undefined ? j.total : rows.length)) + " 条 · 正常 " + num(s.active) + " / 停用 " + num(s.disabled) + " / 封禁 " + num(s.banned)
      : "共 " + num(j.total !== undefined ? j.total : rows.length) + " 条";
    setHtml($("tnStats"), statsTxt);

    const myId = num(ME && ME.id);
    const html = rows.length ? rows.map((t) => {
      const id = num(t.id);
      const url = urlById[id] || "";
      const canManage = (lv === 0 && id !== 0) || (lv === 1 && num(t.parent_id) === myId);
      const sub = num(t.sub_agents_used) + " / " + num(t.max_sub_agents);
      const ops = [];
      if (canManage && !isRO()) {
        ops.push('<button class="btn ghost mini" type="button" data-tact="quota" data-id="' + id + '" data-sign="1">加额度</button>');
        ops.push('<button class="btn ghost mini" type="button" data-tact="quota" data-id="' + id + '" data-sign="-1">回收</button>');
        if (lv === 1 && num(t.level) === 2) ops.push('<button class="btn ghost mini" type="button" data-tact="recycle" data-id="' + id + '" data-name="' + esc(t.username || "") + '">一键回收</button>');
        ops.push(tenantStateBtns(t, true));
        if (num(t.level) >= 1) ops.push('<button class="btn ghost mini" type="button" data-tact="pwd" data-id="' + id + '" data-name="' + esc(t.username || "") + '">\u6539\u8d26\u53f7\u5bc6\u7801</button>');
      }
      ops.push('<button class="btn ghost mini" type="button" data-tact="copycred" data-id="' + id + '">复制账号密码</button>');
      ops.push('<button class="btn ghost mini" type="button" data-tact="secret" data-id="' + id + '">账号密码</button>');
      ops.push('<button class="btn ghost mini" type="button" data-tact="codes" data-id="' + id + '" data-name="' + esc(t.username || "") + '">卡密</button>');
      ops.push('<button class="btn ghost mini" type="button" data-tact="logs" data-id="' + id + '" data-name="' + esc(t.username || "") + '">记录</button>');
      ops.push('<button class="btn ghost mini" type="button" data-tact="more" data-id="' + id + '" data-name="' + esc(t.username || "") + '" data-st="' + esc(t.status || "active") + '">更多</button>');
      return "<tr>" +
        '<td class="selcol"><input type="checkbox" class="tnChk" value="' + id + '" data-name="' + esc(t.username || "") + '"></td>' +
        '<td class="c-name"><b>' + esc(pick(t, ["username"], "—")) + "</b></td>" +
        '<td class="c-lv"><span class="tag ' + lvlCls(t.level) + '">' + esc(lvlText(t.level)) + "</span></td>" +
        '<td class="c-st"><span class="tag ' + stCls(t.status) + '">' + esc(stText(t.status)) + "</span></td>" +
        '<td class="mnum c-qtotal">' + esc(num(t.quota_total)) + "</td>" +
        '<td class="mnum c-qgiven">' + esc(num(t.quota_given)) + "</td>" +
        '<td class="mnum c-qu">' + esc(num(t.quota_used)) + "</td>" +
        '<td class="mnum c-qavail"><b>' + esc(avail(t)) + "</b></td>" +
        '<td class="mnum c-sub">' + esc(sub) + "</td>" +
        '<td class="muted c-remark">' + esc(pick(t, ["remark"], "—")) + "</td>" +
        '<td class="muted mono c-created">' + esc(fmtTime(pick(t, ["created_at_str", "created_at"], ""))) + "</td>" +
        '<td class="muted mono c-last">' + esc(fmtTime(pick(t, ["last_login_str", "last_login_at"], "—"))) + "</td>" +
        '<td class="c-url"><span class="code" data-copy="' + esc(url) + '" title="点击复制">' + esc(url || "—") + "</span></td>" +
        '<td class="c-ops"><div class="ops">' + ops.join("") + "</div></td></tr>";
    }).join("") : '<tr><td colspan="14" class="empty">' + (lv === 0 ? "还没有代理商，点右上角「新建代理商」开一个" : "你还没有创建子代理") + "</td></tr>";
    setHtml($("tnBody"), html);
    document.querySelectorAll("#tnBody .tnChk").forEach((cb) => { cb.onchange = updateSelBar; });
    if (state.tnSel) updateSelBar();
  } catch (e) { toast(e.message || "加载失败"); }
}
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-tact]");
  if (!b) return;
  const act = b.getAttribute("data-tact"), id = num(b.getAttribute("data-id"));
  if (act === "quota") quotaModal(id, b.getAttribute("data-sign"));
  else if (act === "recycle") recycleModal(id, b.getAttribute("data-name") || "");
  else if (act === "status") setTenantStatus(id, b.getAttribute("data-st"));
  else if (act === "pwd") pwdModal(id, b.getAttribute("data-name") || "");
  else if (act === "secret") secretModal(id);
  else if (act === "codes") gotoCodes(id, b.getAttribute("data-name"));
  else if (act === "logs") gotoLogs(id, b.getAttribute("data-name"));
  else if (act === "copycred") copyCred(id);
  else if (act === "more") tenantMoreSheet(id, b.getAttribute("data-name") || "", b.getAttribute("data-st") || "active");
  else if (act === "del") batchDeleteModal([id], [b.getAttribute("data-name") || ("#" + id)]);
});
async function setTenantStatus(id, st) {
  const label = st === "active" ? "启用" : (st === "disabled" ? "停用" : "封禁");
  if (!confirm("确定要" + label + "这个账号吗？")) return;
  try {
    await req("/tenants/" + id + "/status", { method: "POST", body: { status: st } });
    toast("已" + label);
    if (state.view === "tenants") loadTenants();
  } catch (e) { toast(e.message || "操作失败"); }
}
function newTenantModal() {
  const lv = lvNum();
  const isL1 = lv === 1;
  const body =
    '<div class="form-grid">' +
    '<div class="field"><label>登录账号</label><input id="ntUser" placeholder="例如 agent001"></div>' +
    '<div class="field"><label>登录密码</label><input id="ntPwd" type="text" placeholder="至少 8 位"></div>' +
    '<div class="field"><label>分配额度（张卡密）</label><input id="ntQuota" type="number" min="0" value="0" data-plain="1"></div>' +
    (isL1 ? "" : '<div class="field"><label>可开下级名额</label><input id="ntMax" type="number" min="0" value="0" data-plain="1"></div>') +
    '<div class="field full"><label>备注</label><input id="ntRemark" placeholder="例如：张三 / 闲鱼渠道"></div>' +
    "</div>" +
    '<p class="hintline">' + (isL1
      ? "额度会从你自己的剩余额度里划出去（划出后你自己就不能再用了）。一共只有三级：总台 → 代理商 → 子代理商，子代理不能再开下级。"
      : "额度由总台发放；「可开下级名额」决定这个代理能再开多少个下级账号（最多三级：总台 → 代理商 → 子代理商）。") + "</p>";
  openModal(isL1 ? "新建子代理" : "新建代理商", body, async () => {
    const username = (mV("ntUser").value || "").trim();
    const password = (mV("ntPwd").value || "").trim();
    if (username.length < 3) throw new Error("登录账号至少 3 个字符");
    if (password.length < 8) throw new Error("登录密码至少 8 位");
    const payload = {
      username: username, password: password,
      quota: num(mV("ntQuota").value),
      remark: (mV("ntRemark").value || "").trim()
    };
    // 一共三级：子代理没有下级名额；总台建代理商时才带名额
    if (!isL1) { payload.level = 1; payload.max_sub_agents = num(mV("ntMax").value); }
    await req("/tenants", { method: "POST", body: payload });
    toast("创建成功，额度已发放");
    PAGES.tenants.page = 0;
    loadTenants();
  }, "创建");
}
function batchTenantModal() {
  const body =
    '<div class="form-grid">' +
    '<div class="field"><label>创建数量</label><input id="btCount" type="number" min="1" max="100" value="10" data-plain="1"></div>' +
    '<div class="field"><label>账号前缀</label><input id="btPrefix" value="agent" placeholder="例如 agent"></div>' +
    '<div class="field"><label>统一密码（可留空）</label><input id="btPwd" type="text" placeholder="留空 = 每个账号随机 12 位"></div>' +
    '<div class="field"><label>每个账号额度（张卡密）</label><input id="btQuota" type="number" min="0" value="0" data-plain="1"></div>' +
    '<div class="field"><label>可开下级名额</label><input id="btMax" type="number" min="0" value="0" data-plain="1"></div>' +
    '<div class="field full"><label>备注</label><input id="btRemark" placeholder="例如：10-20 批次 / 闲鱼渠道"></div>' +
    "</div>" +
    '<p class="hintline">只有总台能批量建代理商。账号自动编号（如 agent001、agent002…），重名会往后顺延。密码留空则每个账号随机生成；建完可一键复制「账号 / 密码 / 专属网址」，每行一条。</p>' +
    '<div id="btResult" class="hide" style="margin-top:12px"></div>';
  openModal("批量新建代理商", body, async () => {
    const count = num(mV("btCount").value);
    const prefix = (mV("btPrefix").value || "").trim();
    const pwd = (mV("btPwd").value || "").trim();
    if (count < 1 || count > 100) throw new Error("数量请填 1~100");
    if (pwd && pwd.length < 8) throw new Error("统一密码至少 8 位");
    const j = await req("/tenants/batch", { method: "POST", body: {
      count: count,
      prefix: prefix,
      password: pwd,
      quota: num(mV("btQuota").value),
      max_sub_agents: num(mV("btMax").value),
      remark: (mV("btRemark").value || "").trim()
    }});
    const rows = j.rows || [];
    const blocks = rows.map((r) => "账号：" + r.username + "\n密码：" + r.password + "\n网址：" + (r.url || "—"));
    const credLines = rows.map((r) => r.username + "  密码：" + r.password);
    const box = $("btResult");
    if (box) {
      box.classList.remove("hide");
      setHtml(box,
        '<div class="tools credtools"><b>\u5df2\u521b\u5efa ' + rows.length + ' \u4e2a</b><span class="sp"></span>' +
        '<button class="btn ghost mini" type="button" id="btCred">\u590d\u5236\u8d26\u53f7\u5bc6\u7801</button>' +
        '<button class="btn ghost mini" type="button" id="btCopy">\u590d\u5236\u5168\u90e8</button>' +
        '<button class="btn ghost mini" type="button" id="btTxt">\u5bfc\u51fa TXT</button></div>' +
        '<div class="crlist">' + (blocks.map((t) =>
          '<pre class="cred">' + esc(t) + "</pre>"
        ).join("") || "\u6ca1\u6709\u65b0\u5efa\u4efb\u4f55\u8d26\u53f7") + "</div>" +
        ((j.failed && j.failed.length)
          ? '<p class="hintline">\u5931\u8d25 ' + j.failed.length + ' \u4e2a\uff1a' +
            esc(j.failed.map((f) => f.username + "\uff08" + f.error + "\uff09").join("\u3001")) + "</p>"
          : ""));
      try { box.scrollIntoView({ block: "end" }); } catch (e) { /* 忽略 */ }
      const cp = $("btCopy"), tx = $("btTxt"), cr = $("btCred");
      if (cr) cr.onclick = () => copyText(credLines.join("\r\n"), cr);
      if (cp) cp.onclick = () => copyText(blocks.join("\r\n\r\n"), cp);
      if (tx) tx.onclick = () => download(stampName("\u4ee3\u7406\u5546"), blocks.join("\r\n\r\n"));
    }
    toast(j.message || ("\u5df2\u521b\u5efa " + rows.length + " \u4e2a\u4ee3\u7406\u5546"));
    PAGES.tenants.page = 0;
    loadTenants();
    return false;   // 弹窗不关，结果留在里面给爷爷复制
  }, "\u5f00\u59cb\u521b\u5efa");
}

function quotaModal(id, sign) {
  const plus = String(sign) !== "-1";
  const body =
    '<div class="form-grid">' +
    '<div class="field"><label>' + (plus ? "增加" : "回收") + '数量（张卡密）</label><input id="qtN" type="number" min="1" value="100" data-plain="1"></div>' +
    "</div>" +
    '<p class="hintline">' + (plus
      ? "从你的额度里再发给这个账号。"
      : "把这个账号还没用掉的额度收回来（已建码消耗掉的部分不会被回收）。") + "</p>";
  openModal(plus ? "增加额度" : "回收额度", body, async () => {
    const n = Math.abs(num(mV("qtN").value));
    if (!n) throw new Error("请输入大于 0 的数量");
    await req("/tenants/" + id + "/quota", {
      method: "POST",
      body: { delta: plus ? n : -n, reason: plus ? "发放额度" : "额度回收", memo: "" }
    });
    toast("额度已" + (plus ? "增加" : "回收") + " " + n + " 张");
    loadTenants();
  }, plus ? "确认增加" : "确认回收");
}
async function recycleModal(id, name) {
  let p = null;
  try { p = await req("/tenants/" + id + "/recycle", { method: "POST", body: { dry: true } }); }
  catch (e) { return toast(e.message || "读取回收信息失败"); }
  const nm = name || (p && p.username) || "该子代理";
  const uv = num(p && p.visiting);
  const base = num(p && p.quota) + num(p && p.unused);
  const body =
    '<div class="form-grid">' +
    '<div class="field full"><label>子代理</label><input readonly value="' + esc(nm) + '" data-plain="1"></div>' +
    '<div class="field"><label>剩余可用额度</label><input readonly value="' + num(p && p.quota) + ' 张" data-plain="1"></div>' +
    '<div class="field"><label>未使用卡密</label><input readonly value="' + num(p && p.unused) + ' 张" data-plain="1"></div>' +
    '<div class="field"><label>已进入未完成</label><input readonly value="' + uv + ' 张" data-plain="1"></div>' +
    '<div class="field"><label>已完成 / 已停用</label><input readonly value="' + num(p && p.done) + ' / ' + num(p && p.disabled) + ' 张" data-plain="1"></div>' +
    '<div class="field"><label>本次回收合计</label><input id="rcTotal" readonly value="' + base + ' 张" data-plain="1"></div>' +
    '</div>' +
    (uv ? '<label class="hintline" style="display:flex;gap:8px;align-items:flex-start"><input type="checkbox" id="rcV" data-plain="1" style="width:auto;margin-top:3px"><span>连带回收「已进入领取但未完成」的 ' + uv + ' 张（默认不动，避免打断正在领取的客户）</span></label>' : '') +
    '<label class="hintline" style="display:flex;gap:8px;align-items:flex-start"><input type="checkbox" id="rcLock" data-plain="1" style="width:auto;margin-top:3px"><span>同时停用该子代理账号（他立刻掉线，不能再登录操作）</span></label>' +
    '<p class="hintline">回收后：这些卡密立刻失效（客户再打开会显示「已停用」），额度回到你账上，该子代理剩余额度归 0。已完成领取的卡密不动。</p>';
  openModal("一键回收「" + nm + "」", body, async () => {
    const visiting = !!(mV("rcV") && mV("rcV").checked);
    const lock = !!(mV("rcLock") && mV("rcLock").checked);
    const r = await req("/tenants/" + id + "/recycle", { method: "POST", body: { visiting: visiting, lock: lock } });
    toast((r && r.message) || "已回收");
    if (state.view === "tenants") loadTenants();
    if (state.view === "dash" && LOAD.dash) LOAD.dash();
  }, "确认回收");
  const cb = mV("rcV");
  if (cb) cb.onchange = () => {
    const el = mV("rcTotal");
    if (el) el.value = (base + (cb.checked ? uv : 0)) + " 张";
  };
}
function pwdModal(id, name) {
  const who = name ? ("\u300c" + name + "\u300d") : "\u8fd9\u4e2a\u8d26\u53f7";
  const body =
    '<div class="field"><label>\u767b\u5f55\u8d26\u53f7\uff08\u7559\u7a7a = \u4e0d\u6539\uff09</label>' +
    '<input id="pwU" type="text" placeholder="' + esc(name || "\u65b0\u7684\u767b\u5f55\u8d26\u53f7") + '" autocomplete="off" spellcheck="false"></div>' +
    '<div class="field"><label>\u65b0\u7684\u767b\u5f55\u5bc6\u7801\uff08\u7559\u7a7a = \u4e0d\u6539\uff09</label>' +
    '<input id="pwN" type="text" placeholder="\u81f3\u5c11 8 \u4f4d" autocomplete="off" spellcheck="false"></div>' +
    '<div class="field"><label>\u518d\u8f93\u4e00\u6b21\u65b0\u5bc6\u7801</label>' +
    '<input id="pwN2" type="text" placeholder="\u786e\u8ba4\u5bc6\u7801" autocomplete="off" spellcheck="false"></div>' +
    '<p class="hintline">\u4fdd\u5b58\u540e ' + who + ' \u5728\u6240\u6709\u8bbe\u5907\u4e0a\u7684\u767b\u5f55\u90fd\u4f1a\u5931\u6548\uff0c\u8bf7\u628a\u65b0\u7684\u8d26\u53f7\u5bc6\u7801\u8f6c\u53d1\u7ed9\u4ed6\uff1b\u6539\u5b8c\u53ef\u5728\u300c\u8d26\u53f7\u5bc6\u7801\u300d\u91cc\u968f\u65f6\u67e5\u770b\u3002</p>';
  openModal("\u4fee\u6539\u767b\u5f55\u8d26\u53f7 / \u5bc6\u7801", body, async () => {
    const u = (mV("pwU").value || "").trim();
    const v = (mV("pwN").value || "").trim();
    const v2 = (mV("pwN2").value || "").trim();
    if (u && /\s/.test(u)) throw new Error("\u767b\u5f55\u8d26\u53f7\u4e0d\u80fd\u6709\u7a7a\u683c");
    if (v && v.length < 8) throw new Error("\u65b0\u5bc6\u7801\u81f3\u5c11 8 \u4f4d");
    if (v && v2 !== v) throw new Error("\u4e24\u6b21\u8f93\u5165\u7684\u5bc6\u7801\u4e0d\u4e00\u81f4");
    if (!u && !v) throw new Error("\u8bf7\u586b\u5199\u8981\u4fee\u6539\u7684\u8d26\u53f7\u6216\u5bc6\u7801");
    const j = await req("/tenants/" + id + "/password", { method: "POST", body: { username: u, new: v } });
    toast((j && j.message) || "\u5df2\u4fdd\u5b58");
    loadTenants();
    setTimeout(() => secretModal(id), 600);
  }, "\u4fdd\u5b58");
}
function gotoCodes(id, name) {
  state.view = "codes";
  state.codesOwner = id;
  state.codesOwnerName = name || "";
  render();
}
function gotoLogs(id, name) {
  state.view = "logs";
  state.logsOwner = id;
  state.logsOwnerName = name || "";
  render();
}

/* ============================ 生成卡密 ============================ */
function genPanelHtml() {
  return '<div class="panel"><div class="ph"><h2>生成卡密</h2>' +
    '<span class="muted" id="myQuota">—</span></div>' +
    '<div class="tools">' +
    '<input id="genN" type="number" min="1" max="100000" value="10" data-plain="1" style="width:110px" placeholder="数量">' +
    '<input id="genNote" class="grow" placeholder="备注（可选）" style="min-width:150px">' +
    '<button class="btn btn--primary mini" type="button" id="genGo">生成卡密</button>' +
    "</div>" +
    '<p class="hintline">每生成 1 张卡密会从你的剩余额度里扣 1 张。<b>每张卡密固定只能成功领取 1 次</b>，领到后链接自动失效；需要再领请生成新卡密。</p>' +
    '<div id="genWrap" class="hide" style="margin-top:12px">' +
    '<div class="tools"><b>本次生成 <span id="genCnt">0</span> 条</b><span class="sp"></span>' +
    '<button class="btn ghost mini" type="button" id="genCopy">复制全部链接</button>' +
    '<button class="btn ghost mini" type="button" id="genTxt">导出链接 TXT</button></div>' +
    '<div class="genout" id="genOut"></div></div></div>';
}
let GEN_ITEMS = [];
function bindGen() {
  const go = $("genGo");
  if (!go) return;
  go.onclick = async () => {
    const n = num($("genN").value);
    if (n <= 0) { toast("请输入要生成的数量"); return; }
    go.disabled = true;
    try {
      const j = await req("/gen", { method: "POST", body: { count: n, note: ($("genNote").value || "").trim() } });
      GEN_ITEMS = j.items || [];
      $("genCnt").textContent = GEN_ITEMS.length;
      $("genWrap").classList.remove("hide");
      setHtml($("genOut"), GEN_ITEMS.map((x) =>
        '<span class="gl" data-copy="' + esc(x.url || x.code) + '">' + esc(x.code + "  →  " + x.url) + "</span>"
      ).join("") || "没有生成任何卡密");
      toast("已生成 " + GEN_ITEMS.length + " 张卡密");
      loadCodes();
      loadMyQuota();
    } catch (e) { toast(e.message || "生成失败"); }
    finally { go.disabled = false; }
  };
  $("genCopy").onclick = () => copyText(GEN_ITEMS.map((x) => x.url).join("\n"), $("genCopy"));
  $("genTxt").onclick = () => {
    if (!GEN_ITEMS.length) { toast("还没有可导出的内容"); return; }
    download(stampName("卡密链接"), GEN_ITEMS.map((x) => x.url).filter(Boolean).join("\r\n"));
  };
}
async function loadMyQuota() {
  const el = $("myQuota");
  if (!el) return;
  try {
    const j = await req("/me");
    const m = j.me || {};
    el.textContent = "剩余可用 " + num(m.available) + " 张（总额度 " + num(m.quota_total) + "，已划下级 " + num(m.quota_given) + "，自己已用 " + num(m.quota_used) + "）";
  } catch (e) { el.textContent = "额度加载失败"; }
}

/* ============================ 卡密列表 ============================ */
VIEWS.codes = {
  title: "卡密列表",
  sub: "点击卡密或链接即可复制；支持导出 TXT",
  mount(host) {
    const owner = num(state.codesOwner);
    host.innerHTML = genPanelHtml() +
      '<div class="panel"><div class="ph"><h2>卡密</h2><span class="muted" id="cdCount">—</span></div>' +
      '<div class="tools">' +
      '<select id="cdSt"><option value="">全部状态</option><option value="unused">未使用</option>' +
      '<option value="visiting">已进入领取</option><option value="done">已完成领取</option><option value="disabled">已停用</option></select>' +
      (owner && IS_TOTAL ? '<span class="tag lv1" id="cdOwner">只看：' + esc(state.codesOwnerName || ("#" + owner)) + '</span><button class="btn ghost mini" type="button" id="cdClear">清除</button>' : "") +
      '<span class="sp"></span>' +
      '<button class="btn ghost mini" type="button" id="cdCopy">复制本页链接</button>' +
      '<button class="btn ghost mini" type="button" id="cdTxt">导出 TXT</button>' +
      '<button class="btn ghost mini" type="button" id="cdDis">停用本页</button>' +
      "</div>" +
      '<div class="tablewrap"><table><thead><tr><th>卡密</th><th>链接（点一下即复制）</th><th>状态</th>' +
      "<th>已领</th><th>归属</th><th>生成时间</th><th>操作</th></tr></thead><tbody id=\"cdBody\"></tbody></table></div>" +
      pagerHtml("codes") + "</div>";
    bindGen();
    loadMyQuota();
    $("cdSt").onchange = () => { PAGES.codes.page = 0; loadCodes(); };
    const cl = $("cdClear");
    if (cl) cl.onclick = () => { state.codesOwner = 0; state.codesOwnerName = ""; loadCodes(); };
    $("cdCopy").onclick = () => copyText(LAST_CODES.map((r) => r.url || "").filter(Boolean).join("\n"), $("cdCopy"));
    $("cdTxt").onclick = exportCodesTxt;
    $("cdDis").onclick = async () => {
      const codes = LAST_CODES.filter((r) => r.status !== "disabled").map((r) => r.code);
      if (!codes.length) { toast("本页没有可停用的卡密"); return; }
      if (!confirm("确定停用本页 " + codes.length + " 条卡密？停用后买家打不开链接。")) return;
      try {
        const j = await req("/codes/disable", { method: "POST", body: { codes, disabled: true } });
        toast("已停用 " + num(j.count) + " 条");
        loadCodes();
      } catch (e) { toast(e.message || "停用失败"); }
    };
    $("cdBody").addEventListener("click", async (ev) => {
      const b = ev.target.closest("[data-tog]");
      if (!b) return;
      const code = b.getAttribute("data-tog");
      const dis = b.getAttribute("data-dis") === "1";
      b.disabled = true;
      try {
        await req("/codes/disable", { method: "POST", body: { code, disabled: dis } });
        toast(dis ? "已停用 " + code : "已启用 " + code);
        loadCodes();
      } catch (e) { toast(e.message || "操作失败"); b.disabled = false; }
    });
    loadCodes();
  },
  reload: () => loadCodes()
};
LOAD.codes = () => loadCodes();
let LAST_CODES = [];
function codesQs(limit, offset) {
  const st = ($("cdSt") && $("cdSt").value) || "";
  let qs = "?limit=" + limit + "&offset=" + offset + "&status=" + encodeURIComponent(st);
  if (IS_TOTAL && num(state.codesOwner)) qs += "&owner_id=" + num(state.codesOwner);
  return qs;
}
async function loadCodes() {
  const st = PAGES.codes;
  try {
    let j, rows;
    if (IS_TOTAL) {
      try { j = await req("/codes" + codesQs(st.size, st.page * st.size)); }
      catch (e) { j = null; }
      if (!j) {
        const lj = await req("/list" + codesQs(st.size * 4, 0));
        const total = num(lj.filtered !== undefined ? lj.filtered : lj.total);
        const all = (lj.rows || []);
        const off = st.page * st.size;
        rows = all.slice(off, off + st.size);
        j = { ok: true, rows: rows, total: total, pages: Math.max(1, Math.ceil(total / st.size)) };
      }
    } else {
      j = await req("/codes" + codesQs(st.size, st.page * st.size));
    }
    rows = j.rows || [];
    LAST_CODES = rows;
    applyPage(j, "codes");
    setHtml($("cdCount"), "共 " + st.total + " 条");
    setHtml($("cdBody"), rows.length ? rows.map((r) => {
      const url = r.url || "";
      const ownerName = pick(r, ["owner_name", "owner", "username"], "");
      const ownerLv = num(pick(r, ["owner_level"], IS_TOTAL ? 0 : lvNum()));
      return "<tr>" +
        '<td><span class="code cp" data-copy="' + esc(r.code) + '" title="点击复制卡密">' + esc(r.code) + "</span></td>" +
        '<td><span class="link cp" data-copy="' + esc(url) + '" title="点击复制链接">' + esc(url) + "</span></td>" +
        '<td><span class="tag ' + codeCls(r.status) + '">' + esc(codeSt(r.status)) + "</span></td>" +
        '<td class="mnum">' + esc(num(r.claims)) + "</td>" +
        "<td>" + (ownerName ? '<span class="tag ' + lvlCls(ownerLv) + '">' + esc(ownerName) + "</span>" : '<span class="muted">本人</span>') + "</td>" +
        '<td class="muted mono">' + esc(fmtTime(pick(r, ["created_at_str", "created_at"], ""))) + "</td>" +
        '<td><button class="btn ghost mini" type="button" data-tog="' + esc(r.code) + '" data-dis="' + (r.status === "disabled" ? "0" : "1") + '">' + (r.status === "disabled" ? "启用" : "停用") + "</button></td></tr>";
    }).join("") : '<tr><td colspan="7" class="empty">还没有卡密，先在上方「生成卡密」</td></tr>');
  } catch (e) { toast(e.message || "加载失败"); }
}
async function exportCodesTxt() {
  try {
    let rows = [];
    if (IS_TOTAL) {
      try { const j = await req("/codes" + codesQs(100000, 0)); rows = j.rows || []; }
      catch (e) {
        const lj = await req("/list" + codesQs(2000, 0));
        rows = lj.rows || [];
      }
    } else {
      const j = await req("/codes" + codesQs(100000, 0));
      rows = j.rows || [];
    }
    if (!rows.length) { toast("当前筛选下没有可导出的卡密"); return; }
    download(stampName("卡密链接"), rows.map((r) => r.url).filter(Boolean).join("\r\n"));
  } catch (e) { toast(e.message || "导出失败"); }
}

/* ============================ 领取记录 ============================ */
VIEWS.logs = {
  title: "领取记录",
  sub: "谁扫码、谁登录、谁领到、谁没到账，全部带时间和 IP",
  mount(host) {
    const owner = num(state.logsOwner);
    host.innerHTML =
      '<div class="panel"><div class="ph"><h2>领取记录</h2><span class="muted" id="lgStats">—</span></div>' +
      '<div class="tools">' +
      '<input id="lgCode" placeholder="按卡密筛选（可留空）" style="min-width:170px">' +
      '<select id="lgKind"><option value="">全部类型</option><option value="enter">进入领取</option>' +
      '<option value="scan">已扫码</option><option value="login">登录成功</option>' +
      '<option value="success">领取成功</option><option value="fail">未到账</option></select>' +
      (owner ? '<span class="tag lv1">只看：' + esc(state.logsOwnerName || ("#" + owner)) + '</span><button class="btn ghost mini" type="button" id="lgClear">清除</button>' : "") +
      '<button class="btn ghost mini" type="button" id="lgGo">筛选</button>' +
      "</div>" +
      '<div class="tablewrap"><table><thead><tr><th>时间</th><th>卡密</th><th>类型</th><th>账号</th><th>IP</th><th>说明</th></tr></thead>' +
      '<tbody id="lgBody"></tbody></table></div>' +
      pagerHtml("logs", [20, 50, 100, 200]) + "</div>";
    $("lgGo").onclick = () => { PAGES.logs.page = 0; loadLogs(); };
    $("lgCode").onkeydown = (e) => { if (e.key === "Enter") { PAGES.logs.page = 0; loadLogs(); } };
    $("lgKind").onchange = () => { PAGES.logs.page = 0; loadLogs(); };
    const cl = $("lgClear");
    if (cl) cl.onclick = () => { state.logsOwner = 0; state.logsOwnerName = ""; loadLogs(); };
    loadLogs();
  },
  reload: () => loadLogs()
};
LOAD.logs = () => loadLogs();
/* R82-L：领取记录里补 uid + 到期时间变化（例：个人订阅 10-24 → 11-23） */
function shortDayL(v) {
  const t = String(v == null ? "" : v).trim();
  if (t.length >= 10 && t.charAt(4) === "-" && t.charAt(7) === "-") return t.substring(5, 10);
  return t;
}
function uidSubC(x) {
  const d = (x && x.detail_obj) || {};
  const u = String((x && (x.uid || d.uid)) || "").trim();
  const bm = String(d.before_member || "").trim(), be = String(d.before_expiry || "").trim();
  const am = String(d.after_member || "").trim(), ae = String(d.after_expiry || "").trim();
  const parts = [];
  if (u) parts.push("uid " + esc(u));
  if (am || ae || bm || be) {
    const nm = am || bm || "";
    parts.push(esc((nm ? nm + " " : "") + (be ? shortDayL(be) : "—") + " → " + (ae ? shortDayL(ae) : "—") + "（北京）"));
  }
  return parts.length ? '<div class="muted mono" style="font-size:11px;line-height:1.5">' + parts.join(" · ") + "</div>" : "";
}

async function loadLogs() {
  const st = PAGES.logs;
  const code = (($("lgCode") && $("lgCode").value.trim()) || "").toUpperCase();
  const kind = ($("lgKind") && $("lgKind").value) || "";
  let qs = "?limit=" + st.size + "&offset=" + st.page * st.size + "&code=" + encodeURIComponent(code) + "&kind=" + encodeURIComponent(kind);
  if (num(state.logsOwner)) qs += "&owner_id=" + num(state.logsOwner);
  try {
    const j = await req("/logs" + qs);
    applyPage(j, "logs");
    const s = j.stats || {};
    setHtml($("lgStats"),
      '<span class="tag unused">进入 ' + num(s.enter) + '</span> ' +
      '<span class="tag visiting">扫码 ' + num(s.scan) + '</span> ' +
      '<span class="tag done">登录成功 ' + num(s.login) + '</span> ' +
      '<span class="tag done">领取成功 ' + num(s.success) + '</span> ' +
      '<span class="tag danger">未到账 ' + num(s.fail) + "</span>");
    const rows = j.rows || [];
    setHtml($("lgBody"), rows.length ? rows.map((x) => {
      const k = pick(x, ["kind"], "");
      return "<tr>" +
        '<td class="muted mono">' + esc(fmtTime(pick(x, ["time_str", "ts_str", "at_str", "ts"], ""))) + "</td>" +
        '<td class="code">' + esc(pick(x, ["code"], "—")) + "</td>" +
        '<td><span class="tag ' + kindCls(k) + '">' + esc(pick(x, ["kind_text"], kindText(k))) + "</span></td>" +
        "<td>" + esc(pick(x, ["account", "mobile", "phone"], "—")) + uidSubC(x) + "</td>" +
        '<td class="muted mono">' + esc(pick(x, ["ip"], "—")) + "</td>" +
        '<td class="muted">' + esc(pick(x, ["note", "detail", "msg", "reason"], "—")) + "</td></tr>";
    }).join("") : '<tr><td colspan="6" class="empty">暂无记录</td></tr>');
  } catch (e) { toast(e.message || "加载失败"); }
}

/* ============================ 额度流水 ============================ */
VIEWS.ledger = {
  title: "额度流水",
  sub: "每一次发放、划拨、回收、消耗都记账",
  mount(host) {
    host.innerHTML =
      '<div class="panel"><div class="ph"><h2>额度流水</h2><span class="muted" id="lqStats">—</span></div>' +
      '<div class="tools"><input id="lqQ" class="grow" placeholder="搜索账号 / 说明（可留空）">' +
      '<button class="btn ghost mini" type="button" id="lqGo">筛选</button></div>' +
      '<div class="tablewrap"><table><thead><tr><th>时间</th><th>变动</th><th>类型</th><th>操作人</th><th>说明</th><th>变动后余额</th></tr></thead>' +
      '<tbody id="lqBody"></tbody></table></div>' +
      pagerHtml("ledger") + "</div>";
    $("lqGo").onclick = () => { PAGES.ledger.page = 0; loadLedger(); };
    $("lqQ").onkeydown = (e) => { if (e.key === "Enter") { PAGES.ledger.page = 0; loadLedger(); } };
    loadLedger();
  },
  reload: () => loadLedger()
};
LOAD.ledger = () => loadLedger();
async function loadLedger() {
  const st = PAGES.ledger;
  const q = (($("lqQ") && $("lqQ").value.trim()) || "");
  const qs = "?limit=" + st.size + "&offset=" + st.page * st.size + (q ? "&q=" + encodeURIComponent(q) : "");
  try {
    const j = await req("/quota/ledger" + qs);
    applyPage(j, "ledger");
    const rows = j.rows || [];
    setHtml($("lqStats"), "共 " + st.total + " 条");
    setHtml($("lqBody"), rows.length ? rows.map(ledgerRowHtml).join("") : '<tr><td colspan="6" class="empty">暂无流水</td></tr>');
  } catch (e) { toast(e.message || "加载失败"); }
}

/* ============================ 充值（充值码） ============================ */
VIEWS.recharge = {
  get title() { return lvNum() === 0 ? "充值码" : "充值"; },
  get sub() {
    return lvNum() === 0
      ? "总台生成充值码 → 发给代理商，代理商在自己的「充值」里输入即可加张数"
      : "用充值码给自己加张数，进出的每一笔都在下面";
  },
  mount(host) {
    if (lvNum() === 0) return mountRcAdm(host);
    host.innerHTML =
      '<div class="cards" id="rcCards"></div>' +
      '<div class="panel"><div class="ph"><h2>充值码充值</h2><span class="muted">总台发放 · 长期有效 · 一次性使用</span></div>' +
      '<div class="tools">' +
      '<input id="rcInput" class="grow" placeholder="输入充值码，例如 DB-A2K9-7XQM-3PHE-R8TW" autocomplete="off" spellcheck="false">' +
      '<button class="btn btn--primary mini" type="button" id="rcDo">立即充值</button>' +
      "</div>" +
      '<p class="hintline">充值成功后张数立刻到账，可以马上用来生成卡密。充值码用一次就作废，别泄漏给别人。</p></div>' +
      '<div class="panel"><div class="ph"><h2>我的充值记录</h2><span class="muted" id="rcListN">—</span></div>' +
      '<div class="tablewrap"><table><thead><tr><th>充值时间</th><th>充值码</th><th>面额</th><th>状态</th><th>使用者</th></tr></thead>' +
      '<tbody id="rcListBody"></tbody></table></div></div>' +
      '<div class="panel"><div class="ph"><h2>额度明细（进出全记）</h2><span class="muted" id="rcLgN">—</span></div>' +
      '<div class="tablewrap"><table><thead><tr><th>时间</th><th>变动</th><th>类型</th><th>操作人</th><th>说明</th><th>变动后余额</th></tr></thead>' +
      '<tbody id="rcLgBody"></tbody></table></div>' + pagerHtml("rcLedger") + "</div>";
    const inp = $("rcInput");
    if (inp) {
      inp.onkeydown = (e) => { if (e.key === "Enter") doRecharge(); };
      inp.oninput = () => {
        const p = inp.selectionStart;
        inp.value = String(inp.value || "").toUpperCase();
        try { inp.setSelectionRange(p, p); } catch (e) {}
      };
    }
    $("rcDo").onclick = doRecharge;
    loadRecharge();
  },
  reload: () => (lvNum() === 0 ? loadRcAdm() : loadRecharge())
};
LOAD.recharge = () => (lvNum() === 0 ? loadRcAdm() : loadRecharge());
LOAD.rcAdm = () => loadRcAdm();
LOAD.rcLedger = () => loadRecharge();

async function doRecharge() {
  const el = $("rcInput");
  const code = ((el && el.value) || "").trim();
  if (!code) return toast("请先输入充值码");
  const btn = $("rcDo");
  if (btn) { btn.disabled = true; btn.textContent = "充值中…"; }
  try {
    const j = await req("/recharge", { method: "POST", body: { code: code } });
    if (el) el.value = "";
    if (j.me) ME = j.me;
    toast(j.message || "充值成功");
    loadRecharge();
  } catch (e) {
    toast((e && e.message) || "充值失败");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "立即充值"; }
  }
}

async function loadRecharge() {
  try {
    const j = await req("/recharge/mine?limit=50");
    const me = j.me || ME || {};
    const s = j.summary || {};
    const left = (me.available === undefined || me.available === null) ? avail(me) : num(me.available);
    const cards = [
      { k: "剩余兑换码额度", v: left, s: "总额度 " + num(me.quota_total) + " 张", c: "ok" },
      { k: "已消耗", v: num(me.quota_used), s: "生成卡密用掉的张数", c: "warn" },
      { k: "已划给下级", v: num(me.quota_given), s: "划拨出去的张数", c: "acc" },
      { k: "累计充值", v: num(s.amount), s: "共 " + num(s.count) + " 笔充值码", c: "ok" }
    ];
    setHtml($("rcCards"), cards.map((x) =>
      '<div class="stat ' + x.c + '"><div class="k">' + x.k + '</div><div class="v">' + x.v +
      '</div><div class="kd">' + x.s + "</div></div>").join(""));

    const rows = j.rows || [];
    setHtml($("rcListN"), "共 " + num(j.total) + " 条");
    setHtml($("rcListBody"), rows.length ? rows.map((x) => "<tr>" +
      '<td class="muted mono">' + esc(x.used_str || "—") + "</td>" +
      '<td><span class="code" data-copy="' + esc(x.code_str) + '" title="点击复制">' + esc(x.code_str) + "</span></td>" +
      '<td class="mnum">' + esc(num(x.amount)) + " 张</td>" +
      '<td><span class="tag done">' + esc(x.status_text || "已充值") + "</span></td>" +
      "<td>" + esc(x.used_by_name || "—") + "</td></tr>").join("")
      : '<tr><td colspan="5" class="empty">还没有充值记录</td></tr>');

    const st = PAGES.rcLedger;
    const lj = await req("/quota/ledger?limit=" + st.size + "&offset=" + st.page * st.size);
    applyPage(lj, "rcLedger");
    const lrows = lj.rows || [];
    setHtml($("rcLgN"), "共 " + st.total + " 条");
    setHtml($("rcLgBody"), lrows.length ? lrows.map(ledgerRowHtml).join("")
      : '<tr><td colspan="6" class="empty">暂无流水</td></tr>');
  } catch (e) { toast((e && e.message) || "加载失败"); }
}

/* ==================== 充值码（总台 L0：生成 / 列表 / 作废） ==================== */
let RC_ADM_LAST = [];

function rcStamp() {
  const d = new Date(), p = (n) => String(n).padStart(2, "0");
  return d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "_" + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds());
}

function rcSaveTxt(name, txt) {
  try {
    const blob = new Blob(["\ufeff" + txt], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { try { URL.revokeObjectURL(a.href); a.remove(); } catch (e) {} }, 1500);
    toast("已导出 " + name);
  } catch (e) { toast("导出失败，请改用「复制全部」"); }
}

function mountRcAdm(host) {
  host.innerHTML =
    '<div class="panel"><div class="ph"><h2>生成充值码</h2><span class="muted">长期有效 · 一次性使用 · 面额只存服务器</span></div>' +
    '<div class="tools">' +
    '<input id="rcAmt" type="number" min="1" value="100" placeholder="每个面额（张）" style="width:150px" data-plain="1">' +
    '<input id="rcCnt" type="number" min="1" value="10" placeholder="数量" style="width:110px" data-plain="1">' +
    '<input id="rcBatch" placeholder="批次（可选）" style="min-width:120px">' +
    '<input id="rcNote" class="grow" placeholder="备注（可选）" style="min-width:140px">' +
    '<button class="btn btn--primary mini" type="button" id="rcAdmGen">生成充值码</button>' +
    "</div>" +
    '<p class="hintline">码 = <b>DB</b> + 16 位随机字符（约 80 bit 熵，CSPRNG）；<b>面额不写进码里</b>，格式被猜中也推不出金额；兑换走原子条件更新，一个码只能用一次。生成限速 20 次/分钟/IP。</p>' +
    '<div id="rcAdmWrap" class="hide" style="margin-top:12px">' +
    '<div class="tools"><b>本次生成 <span id="rcAdmCnt">0</span></b><span class="sp"></span>' +
    '<button class="btn ghost mini" type="button" id="rcAdmCopy">复制全部</button>' +
    '<button class="btn ghost mini" type="button" id="rcAdmTxt">导出 TXT</button></div>' +
    '<div class="genout" id="rcAdmOut"></div></div></div>' +
    '<div class="panel"><div class="ph"><h2>充值码列表</h2><span class="muted" id="rcAdmStats">—</span></div>' +
    '<div class="tools">' +
    '<select id="rcAdmSt"><option value="">全部状态</option><option value="unused">未使用</option>' +
    '<option value="used">已充值</option><option value="void">已作废</option></select>' +
    '<select id="rcAdmBt"><option value="">全部批次</option></select>' +
    '<input id="rcAdmQ" class="grow" placeholder="搜索充值码 / 使用者 / 备注" style="min-width:170px">' +
    '<button class="btn ghost mini" type="button" id="rcAdmGo">筛选</button></div>' +
    '<div class="tablewrap"><table><thead><tr><th>充值码</th><th>面额</th><th>状态</th><th>使用者</th>' +
    '<th>使用时间</th><th>生成时间</th><th>批次 / 备注</th><th>操作</th></tr></thead>' +
    '<tbody id="rcAdmBody"></tbody></table></div>' + pagerHtml("rcAdm") + "</div>";
  $("rcAdmGen").onclick = genRcAdm;
  $("rcAdmGo").onclick = () => { PAGES.rcAdm.page = 0; loadRcAdm(); };
  $("rcAdmSt").onchange = () => { PAGES.rcAdm.page = 0; loadRcAdm(); };
  $("rcAdmBt").onchange = () => { PAGES.rcAdm.page = 0; loadRcAdm(); };
  $("rcAdmQ").onkeydown = (e) => { if (e.key === "Enter") { PAGES.rcAdm.page = 0; loadRcAdm(); } };
  $("rcAdmCopy").onclick = () => {
    if (!RC_ADM_LAST.length) return toast("还没有生成结果");
    copyText(RC_ADM_LAST.map((x) => x.code_str).join("\n"));
    toast("已复制 " + RC_ADM_LAST.length + " 个充值码");
  };
  $("rcAdmTxt").onclick = () => {
    if (!RC_ADM_LAST.length) return toast("还没有生成结果");
    rcSaveTxt("充值码_" + rcStamp() + ".txt",
      RC_ADM_LAST.map((x) => x.code_str + "\t" + x.amount + " 张").join("\r\n"));
  };
  enhanceSelects();
  loadRcAdm();
}

async function genRcAdm() {
  const amount = parseInt(($("rcAmt") || {}).value || "0", 10);
  const count = parseInt(($("rcCnt") || {}).value || "0", 10);
  if (!amount || amount < 1) return toast("请输入每个充值码的面额（张）");
  if (!count || count < 1) return toast("请输入要生成的数量");
  const btn = $("rcAdmGen");
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  try {
    const j = await req("/recharge/gen", { method: "POST", body: {
      amount: amount, count: count,
      batch: ($("rcBatch") || {}).value || "", note: ($("rcNote") || {}).value || ""
    } });
    RC_ADM_LAST = j.rows || [];
    setHtml($("rcAdmOut"), RC_ADM_LAST.map((x) =>
      '<span class="link cp" data-copy="' + esc(x.code_str) + '" title="点击复制">' + esc(x.code_str) + "</span>").join("\n") || "（无）");
    const wrap = $("rcAdmWrap");
    if (wrap) wrap.classList.remove("hide");
    setHtml($("rcAdmCnt"), RC_ADM_LAST.length + " 个 · 每个 " + num(amount) + " 张");
    toast(j.message || "已生成");
    PAGES.rcAdm.page = 0;
    loadRcAdm();
  } catch (e) {
    toast((e && e.message) || "生成失败");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "生成充值码"; }
  }
}

async function loadRcAdm() {
  const tb = $("rcAdmBody");
  if (!tb) return;
  const st = PAGES.rcAdm;
  const stt = (($("rcAdmSt") || {}).value) || "";
  const bt = (($("rcAdmBt") || {}).value) || "";
  const q = (($("rcAdmQ") || {}).value || "").trim();
  try {
    const j = await req("/recharge?status=" + encodeURIComponent(stt) + "&batch=" + encodeURIComponent(bt) +
      "&q=" + encodeURIComponent(q) + "&limit=" + st.size + "&offset=" + st.page * st.size);
    applyPage(j, "rcAdm");
    const rows = j.rows || [];
    tb.innerHTML = rows.length ? rows.map((x) => {
      const s = String(x.status || "");
      const cls = s === "used" ? "done" : (s === "void" ? "disabled" : "unused");
      const meta = (x.batch || "") + (x.note ? ((x.batch ? " · " : "") + x.note) : "");
      const ops = s === "unused"
        ? '<button class="btn ghost mini" type="button" data-rcvoid="' + num(x.id) + '">作废</button>' : "";
      return "<tr>" +
        '<td><span class="code" data-copy="' + esc(x.code_str) + '" title="点击复制">' + esc(x.code_str) + "</span></td>" +
        '<td class="mnum">' + esc(num(x.amount)) + " 张</td>" +
        '<td><span class="tag ' + cls + '">' + esc(x.status_text || "") + "</span></td>" +
        "<td>" + esc(x.used_by_name || "—") + "</td>" +
        '<td class="muted mono">' + esc(x.used_str || "—") + "</td>" +
        '<td class="muted mono">' + esc(x.created_str || "—") + "</td>" +
        '<td class="muted">' + esc(meta || "—") + "</td>" +
        "<td>" + ops + "</td></tr>";
    }).join("") : '<tr><td colspan="8" class="empty">还没有生成过充值码</td></tr>';
    const s2 = j.stats || {};
    setHtml($("rcAdmStats"), "共 " + num(s2.total) + " 个 · 未使用 " + num(s2.unused) + "（" + num(s2.amount_unused) +
      " 张） · 已充值 " + num(s2.used) + "（" + num(s2.amount_used) + " 张） · 已作废 " + num(s2.void));
    const sel = $("rcAdmBt");
    if (sel) {
      const bs = j.batches || [];
      const cur = sel.value;
      const html = '<option value="">全部批次</option>' + bs.map((b) =>
        '<option value="' + esc(b.batch) + '"' + (b.batch === cur ? " selected" : "") + ">" + esc(b.batch) +
        "（" + num(b.n) + "）</option>").join("");
      if (sel.innerHTML !== html) sel.innerHTML = html;
    }
    if (typeof labelizeAll === "function") labelizeAll();
    if (typeof enhanceSelects === "function") enhanceSelects();
  } catch (e) { toast((e && e.message) || "加载失败"); }
}

async function rcAdmVoid(id) {
  if (!confirm("确定作废这个充值码？作废后不可恢复（只能作废未使用的）")) return;
  try {
    const j = await req("/recharge/void", { method: "POST", body: { ids: [num(id)] } });
    toast(j.message || "已作废");
    loadRcAdm();
  } catch (e) { toast((e && e.message) || "作废失败"); }
}

document.addEventListener("click", (e) => {
  const b = e.target && e.target.closest ? e.target.closest("[data-rcvoid]") : null;
  if (b) rcAdmVoid(b.getAttribute("data-rcvoid"));
});

/* ============================ 审计日志 ============================ */
VIEWS.audit = {
  title: "审计日志",
  sub: "登录、建号、额度变更、封禁等敏感操作留痕（只保留最近 300 条）",
  mount(host) {
    host.innerHTML =
      '<div class="panel"><div class="ph"><h2>审计日志</h2><span class="muted" id="auStats">—</span></div>' +
      '<div class="tools"><input id="auQ" class="grow" placeholder="搜索操作 / 对象 / 详情">' +
      '<button class="btn ghost mini" type="button" id="auGo">筛选</button></div>' +
      '<div class="tablewrap"><table><thead><tr><th>时间</th><th>等级</th><th>操作者</th><th>操作</th><th>对象</th><th>详情</th><th>IP</th></tr></thead>' +
      '<tbody id="auBody"></tbody></table></div>' +
      pagerHtml("audit") + "</div>";
    $("auGo").onclick = () => { PAGES.audit.page = 0; loadAudit(); };
    $("auQ").onkeydown = (e) => { if (e.key === "Enter") { PAGES.audit.page = 0; loadAudit(); } };
    loadAudit();
  },
  reload: () => loadAudit()
};
LOAD.audit = () => loadAudit();
function audLvlCls(l) { return { info: "info", warn: "warn", danger: "danger" }[String(l || "info")] || "info"; }
function audLvlText(l) { return { info: "正常", warn: "注意", danger: "高危" }[String(l || "info")] || String(l); }
async function loadAudit() {
  const st = PAGES.audit;
  const q = (($("auQ") && $("auQ").value.trim()) || "");
  const qs = "?limit=" + st.size + "&offset=" + st.page * st.size + (q ? "&q=" + encodeURIComponent(q) : "");
  try {
    const j = await req("/audit" + qs);
    applyPage(j, "audit");
    const s = j.stats || {};
    setHtml($("auStats"), Object.keys(s).length
      ? "共 " + st.total + " 条 · 高危 " + num(s.danger) + " / 注意 " + num(s.warn)
      : "共 " + st.total + " 条");
    const rows = j.rows || [];
    setHtml($("auBody"), rows.length ? rows.map((x) => {
      const l = pick(x, ["level"], "info");
      return "<tr>" +
        '<td class="muted mono">' + esc(fmtTime(pick(x, ["time_str", "ts_str", "at_str", "ts"], ""))) + "</td>" +
        '<td><span class="tag ' + audLvlCls(l) + '">' + esc(audLvlText(l)) + "</span></td>" +
        "<td>" + esc(pick(x, ["actor_name", "actor", "operator"], "—")) + "</td>" +
        "<td>" + esc(pick(x, ["action_text", "action"], "—")) + "</td>" +
        "<td>" + esc(pick(x, ["target"], "—")) + "</td>" +
        '<td class="muted">' + esc(pick(x, ["detail", "note"], "—")) + "</td>" +
        '<td class="muted mono">' + esc(pick(x, ["ip"], "—")) + "</td></tr>";
    }).join("") : '<tr><td colspan="7" class="empty">暂无审计记录</td></tr>');
  } catch (e) { toast(e.message || "加载失败"); }
}

/* ============================ 代理池（仅总台） ============================ */
VIEWS.proxy = {
  title: "代理池",
  sub: "所有出网请求走代理出口，一个二维码从出码到核验全程同一个 IP",
  mount(host) {
    host.innerHTML =
      '<div class="panel"><div class="ph"><h2>当前状态</h2>' +
      '<button class="btn btn--primary mini" type="button" id="pxTest">测试出口</button></div>' +
      '<div class="pgrid" id="pxStats"></div>' +
      '<p class="hintline" id="pxTestOut">点「测试出口」会真的拿一个出口 IP 试试通不通。</p></div>' +
      '<div class="panel"><div class="ph"><h2>代理配置</h2>' +
      '<button class="btn btn--primary mini" type="button" id="pxSave">保存配置</button></div>' +
      '<div class="form-grid">' +
      '<div class="field"><label>开关</label><select id="pxOn"><option value="1">开启（走代理）</option><option value="0">关闭（直连）</option></select></div>' +
      '<div class="field"><label>模式</label><select id="pxMode"><option value="api">api（模板拼会话）</option><option value="list">list（固定名单轮换）</option></select></div>' +
      '<div class="field"><label>代理地址 host</label><input id="pxHost" placeholder="proxy.example.com"></div>' +
      '<div class="field"><label>端口 port</label><input id="pxPort" type="number" min="1" max="65535" data-plain="1" placeholder="3000"></div>' +
      '<div class="field full"><label>用户名模板（{sid} 会替换成随机会话号）</label><input id="pxUser" placeholder="账号-region-Rand-sid-{sid}-t-5"></div>' +
      '<div class="field"><label>出口地区（填具体国家；Rand/auto = 用右边兜底国家）</label>' +
      '<input id="pxRegion" list="pxRegionList" placeholder="Rand">' +
      '<datalist id="pxRegionList"><option value="Rand"></option><option value="US"></option>' +
      '<option value="SG"></option><option value="JP"></option><option value="DE"></option>' +
      '<option value="GB"></option><option value="FR"></option><option value="KR"></option>' +
      '<option value="CA"></option><option value="AU"></option></datalist></div>' +
      '<div class="field"><label>兜底地区（出口地区填 Rand/auto 时生效）</label>' +
      '<input id="pxFallback" list="pxRegionList" placeholder="US"></div>' +
      '<div class="field"><label>密码</label><input id="pxPwd" type="text" placeholder="代理密码"></div>' +
      '<div class="field"><label>会话号位数</label><input id="pxSid" type="number" min="4" max="24" data-plain="1" value="8"></div>' +
      '<div class="field"><label>粘性时长（分钟）</label><input id="pxSticky" type="number" min="1" max="120" data-plain="1" value="10"></div>' +
      '<div class="field full"><label>固定名单（模式选 list 时生效，每行一条 http://user:pass@host:port）</label>' +
      '<textarea id="pxList" spellcheck="false" style="min-height:96px;font-family:Consolas,monospace;font-size:13px"></textarea></div>' +
      "</div>" +
      '<p class="hintline">提示：代理挂了不会让整站瘫痪，测试失败会自动回落到直连。改完点「保存配置」立即生效。</p>' +
      '<p class="hintline">⚠ <b>出口地区（R71 更新 · 已实测 2026-10-13）</b>：<code>Rand/auto</code> 以前是「每次随机国家」（实测经常落到 HK/TH/RU），<b>现在是固定到「兜底地区」</b>（默认 US），避免同一张码出口乱跳。<br>' +
      '· 只读库统计：<b>账号地区 US + 出口地区 US = 146 次，礼包全在这里</b>；出口与账号地区不一致的组合（HK|US、GB|US…）<b>全部失败</b>；失败记录里约 48% 带「出口中途跳变」。<br>' +
      '· 已验证可用出口：<b>US/SG/TW/DE/FR/CA/MY/VN/IN/NL（命中 3/3）</b>、JP/HK/GB/AU/TH（2/3）、KR（1/3）、<b>CN 不支持（0/3）</b>。<br>' +
      '· 买家手机号/账号是什么国家，出口就该是什么国家：+1→US、+852→HK、+86→HK（1024 无大陆出口）。系统会自动跟随，并在登录后做一次「出口一致性守卫」（不一致就换个新 IP 重试）。<br>' +
      '· 同一个 sid 的出口 IP 大约每 <b>100~170 秒</b>换一次（同国家内换 IP），所以「一个二维码一个 IP」只在这段时间成立，建议让买家尽快扫码。</p></div>';
    $("pxTest").onclick = pxTest;
    $("pxSave").onclick = pxSave;
    loadProxy();
  },
  reload: () => loadProxy()
};
LOAD.proxy = () => loadProxy();
async function loadProxy() {
  try {
    const cfg = await req("/proxy");
    const s = cfg.stats || cfg.proxy_stats || {};
    const last = cfg.last_test || s.last_test || {};
    const ok = num(s.ok !== undefined ? s.ok : s.success), fail = num(s.fail !== undefined ? s.fail : s.failed);
    const rate = (ok + fail) > 0 ? Math.round(ok * 100 / (ok + fail)) + "%" : "—";
    const ip = pick(last, ["ip"], pick(s, ["current_ip", "ip"], "—"));
    const stats = [
      { k: "当前出口 IP", v: ip, c: ip && ip !== "—" ? "ok" : "" },
      { k: "池大小 / 存活", v: num(s.size) + " / " + num(s.alive), c: "" },
      { k: "成功率", v: rate, c: (ok + fail) > 0 && ok * 100 / (ok + fail) >= 50 ? "ok" : "bad" },
      { k: "粘性时长", v: num(pick(cfg, ["sticky_minutes", "sticky"], s.sticky_minutes)) + " 分钟", c: "" },
      { k: "协议", v: pick(cfg, ["protocol"], "http"), c: "" },
      { k: "最近错误", v: pick(s, ["last_error", "error"], "无"), c: s.last_error ? "bad" : "" }
    ];
    setHtml($("pxStats"), stats.map((x) =>
      '<div class="pstat"><div class="k">' + x.k + '</div><div class="v ' + x.c + '">' + esc(x.v) + "</div></div>"
    ).join(""));
    const en = cfg.enabled;
    $("pxOn").value = (en === true || en === 1 || en === "1" || en === "true") ? "1" : "0";
    $("pxMode").value = pick(cfg, ["mode"], "api") === "list" ? "list" : "api";
    $("pxHost").value = pick(cfg, ["host"], "");
    $("pxPort").value = num(pick(cfg, ["port"], 3000)) || "";
    $("pxUser").value = pick(cfg, ["username_template", "username"], "");
    $("pxRegion").value = pick(cfg, ["region"], "Rand") || "Rand";
    $("pxFallback").value = pick(cfg, ["fallback_region", "fallback"], "US") || "US";
    var _pwdSet = !!(cfg.password_set || (cfg.password && cfg.password !== ""));
    $("pxPwd").value = "";
    $("pxPwd").placeholder = _pwdSet ? "已设置（留空 = 不修改）" : "代理密码";
    $("pxSid").value = num(pick(cfg, ["sid_digits"], 8)) || 8;
    $("pxSticky").value = num(pick(cfg, ["sticky_minutes", "sticky"], 10)) || 10;
    $("pxList").value = pick(cfg, ["list_text", "list"], "");
    syncSelectLabels();
  } catch (e) { toast(e.message || "代理配置加载失败"); }
}
async function pxSave() {
  const btn = $("pxSave");
  btn.disabled = true;
  try {
    await req("/proxy", {
      method: "POST",
      body: {
        enabled: $("pxOn").value === "1",
        mode: $("pxMode").value,
        host: ($("pxHost").value || "").trim(),
        port: num($("pxPort").value),
        username_template: ($("pxUser").value || "").trim(),
        region: ($("pxRegion").value || "Rand").trim() || "Rand",
        fallback_region: ($("pxFallback").value || "US").trim().toUpperCase() || "US",
        password: $("pxPwd").value || "",
        sid_digits: num($("pxSid").value) || 8,
        sticky_minutes: num($("pxSticky").value) || 10,
        list_text: $("pxList").value || ""
      }
    });
    toast("代理配置已保存");
    loadProxy();
  } catch (e) { toast(e.message || "保存失败"); }
  finally { btn.disabled = false; }
}
async function pxTest() {
  const btn = $("pxTest");
  btn.disabled = true;
  $("pxTestOut").textContent = "正在测试…";
  try {
    const j = await req("/proxy/test", { method: "POST", body: { n: 1 } });
    const ip = pick(j, ["ip"], "");
    if (j.ok && ip) $("pxTestOut").textContent = "✅ 出口可用：IP " + ip + "，耗时 " + num(j.ms) + " ms";
    else $("pxTestOut").textContent = "❌ 出口不通：" + pick(j, ["error", "message"], "代理没连上，请检查账号密码或地址");
    loadProxy();
  } catch (e) { $("pxTestOut").textContent = "❌ 测试失败：" + (e.message || "请稍后再试"); }
  finally { btn.disabled = false; }
}

/* ============================ 账号密码 / 专属网址 ============================ */
async function secretModal(id) {
  let j;
  try { j = await req("/tenants/" + id + "/secret"); }
  catch (e) { toast(e.message || "加载失败"); return; }
  const pwd = j.password || "";
  const url = j.url || "";
  const un = j.username || "";
  const row = (label, val, mono) =>
    '<div class="field full"><label>' + label + '</label>' +
    '<div class="tools" style="margin:0">' +
    '<input class="grow" readonly value="' + esc(val || "—") + '"' +
    (mono ? ' style="font-family:Consolas,monospace"' : '') + '>' +
    (val ? '<button class="btn ghost mini" type="button" data-copy="' + esc(val) + '">复制</button>' : '') +
    '</div></div>';
  const body = '<div class="form-grid">' +
    row("登录账号", un) +
    row("登录密码", pwd || "（未保存，请点下面重置）", true) +
    row(num(j.level) >= 2 ? "子代理专属网址" : "专属后台网址", url, true) +
    '</div>' +
    (pwd ? '' : '<p class="hintline">这个账号是早期建的，系统没存明文密码。点「重置密码」会生成一个新密码并保存下来，之后随时能在这里查看。</p>') +
    '<p class="hintline">把「网址 + 账号 + 密码」一起发给对方，他只能从这个网址登录。</p>' +
    '<div class="tools">' +
    '<button class="btn ghost mini" type="button" data-sec-reset="' + num(id) + '">重置密码（自动生成并保存）</button>' +
    '<button class="btn ghost mini" type="button" data-sec-site="' + num(id) + '">换一个专属网址</button>' +
    '</div>';
  openModal("账号密码 · 专属网址", body, async () => {
    copyText("账号：" + un + "\n密码：" + (pwd || "（未保存，请重置）") + "\n网址：" + (url || "—"));
  }, "复制全部信息");
}

async function doSecretReset(id) {
  try {
    const j = await req("/tenants/" + id + "/newpwd", { method: "POST", body: {} });
    toast("已生成新密码：" + (j.password || ""));
    secretModal(id);
  } catch (e) { toast(e.message || "操作失败"); }
}

async function doSecretSite(id) {
  if (!confirm("换一个新网址？旧网址立刻失效，已经发出去的链接就用不了了。")) return;
  try {
    await req(IS_TOTAL ? "/sites/reset" : "/my_site/reset", { method: "POST", body: { tid: id } });
    toast("已生成新网址");
    secretModal(id);
  } catch (e) { toast(e.message || "操作失败"); }
}

document.addEventListener("click", (e) => {
  const b1 = e.target.closest("[data-sec-reset]");
  if (b1) { doSecretReset(num(b1.getAttribute("data-sec-reset"))); return; }
  const b2 = e.target.closest("[data-sec-site]");
  if (b2) { doSecretSite(num(b2.getAttribute("data-sec-site"))); }
});

/* ============================ 入口网址（绑定后台） ============================ */
VIEWS.site = {
  title: "入口网址",
  sub: "代理商专属后台网址：子代理只能从上级给的网址登录，跑不到别的分支",
  mount(host) {
    host.innerHTML =
      '<div class="panel" id="siteMain"><div class="ph"><h2>正在加载…</h2></div></div>' +
      '<div class="panel" id="siteSub"></div>';
    loadSite();
  },
  reload: () => loadSite()
};
LOAD.site = () => loadSite();

async function loadSite() {
  const main = $("siteMain"), subBox = $("siteSub");
  if (!main) return;
  try {
    if (IS_TOTAL) {
      const j = await req("/sites");
      const rows = j.rows || [];
      main.innerHTML = '<div class="ph"><h2>代理商专属入口</h2><span class="muted">共 ' + rows.length + ' 个</span></div>' +
        '<p class="muted" style="margin:0 0 12px">把网址发给对应的代理商，他只能在这个入口登录；他的子代理也只能从这里进，跑不到别的代理商。</p>' +
        '<div class="tablewrap"><table><thead><tr><th>代理商账号</th><th>状态</th><th>子代理</th><th>专属后台网址</th><th>操作</th></tr></thead><tbody>' +
        (rows.length ? rows.map((r) =>
          '<tr><td><b>' + esc(r.username) + '</b></td>' +
          '<td><span class="tag ' + stCls(r.status) + '">' + esc(stText(r.status)) + '</span></td>' +
          '<td class="mnum">' + esc(num(r.subs)) + '</td>' +
          '<td><span class="code" data-copy="' + esc(r.url) + '" title="点击复制">' + esc(r.url) + '</span></td>' +
          '<td><button class="btn ghost mini" type="button" data-sreset="' + num(r.id) + '">重置网址</button></td></tr>'
        ).join("") : '<tr><td colspan="5" class="empty">还没有代理商，先去「代理商」里开一个</td></tr>') +
        '</tbody></table></div>';
      subBox.style.display = "none";
      return;
    }

    const j = await req("/my_site");
    subBox.style.display = "";
    const url = j.my_url || j.url || "";

    if (lvNum() >= 2) {
      main.innerHTML = '<div class="ph"><h2>我的登录入口</h2></div>' +
        '<p class="muted" style="margin:0 0 12px">你的账号 <b>' + esc(j.account) + '</b> 只能通过下面这个网址登录（上级：' + esc(j.owner || "—") + '）。</p>' +
        '<div class="tools"><span class="code" data-copy="' + esc(url) + '" title="点击复制">' + esc(url) + '</span>' +
        '<button class="btn ghost mini" type="button" data-copy="' + esc(url) + '">复制网址</button></div>' +
        '<p class="muted" style="margin:12px 0 0">请收藏这个网址，以后从它进后台。</p>';
      subBox.innerHTML = "";
      return;
    }

    const subs = j.subs || [];
    main.innerHTML = '<div class="ph"><h2>我的专属后台网址</h2></div>' +
      '<p class="muted" style="margin:0 0 12px">把这个网址发给客户或子代理，他们只能从这里登录。</p>' +
      '<div class="tools"><span class="code" data-copy="' + esc(j.url || "") + '" title="点击复制">' + esc(j.url || "") + '</span>' +
      '<button class="btn ghost mini" type="button" data-copy="' + esc(j.url || "") + '">复制网址</button>' +
      '<button class="btn ghost mini" type="button" data-sreset2="0">重置网址</button></div>';

    subBox.innerHTML = '<div class="ph"><h2>子代理专属网址</h2><span class="muted">共 ' + subs.length + ' 个</span></div>' +
      '<p class="muted" style="margin:0 0 12px">每个子代理一个网址，只能用它登录自己的账号。</p>' +
      '<div class="tablewrap"><table><thead><tr><th>子代理账号</th><th>状态</th><th>专属网址</th><th>操作</th></tr></thead><tbody>' +
      (subs.length ? subs.map((x) =>
        '<tr><td><b>' + esc(x.username) + '</b></td>' +
        '<td><span class="tag ' + stCls(x.status) + '">' + esc(stText(x.status)) + '</span></td>' +
        '<td><span class="code" data-copy="' + esc(x.url) + '" title="点击复制">' + esc(x.url) + '</span></td>' +
        '<td><button class="btn ghost mini" type="button" data-sreset2="' + num(x.id) + '">重置</button></td></tr>'
      ).join("") : '<tr><td colspan="4" class="empty">还没有子代理</td></tr>') +
      '</tbody></table></div>';
  } catch (e) {
    main.innerHTML = '<div class="ph"><h2>加载失败</h2><span class="muted">' + esc(e.message || "请刷新重试") + '</span></div>';
  }
}

document.addEventListener("click", async (e) => {
  const b1 = e.target.closest("[data-sreset]");
  if (b1) {
    if (!confirm("重置后旧网址立刻失效，已经发出去的链接就用不了了。确定重置？")) return;
    try {
      await req("/sites/reset", { method: "POST", body: { tid: num(b1.getAttribute("data-sreset")) } });
      toast("已生成新网址"); loadSite();
    } catch (err) { toast(err.message || "操作失败"); }
    return;
  }
  const b2 = e.target.closest("[data-sreset2]");
  if (b2) {
    if (!confirm("重置后旧网址立刻失效，已经发出去的链接就用不了了。确定重置？")) return;
    try {
      await req("/my_site/reset", { method: "POST", body: { tid: num(b2.getAttribute("data-sreset2")) } });
      toast("已生成新网址"); loadSite();
    } catch (err) { toast(err.message || "操作失败"); }
  }
});

/* ============================ 修改自己的密码 ============================ */
function openChangePwd() {
  const lvl = lvNum();
  const who = (ME && ME.username) ? ME.username : "本账号";
  const body =
    '<div class="field"><label>登录账号</label><input readonly value="' + esc(who) + '"></div>' +
    '<div class="field"><label>原密码</label><input id="cpOld" type="password" autocomplete="current-password" placeholder="请输入现在用的密码"></div>' +
    '<div class="field"><label>新密码</label><input id="cpNew" type="password" autocomplete="new-password" placeholder="6-64 位，建议字母 + 数字"></div>' +
    '<div class="field"><label>确认新密码</label><input id="cpNew2" type="password" autocomplete="new-password" placeholder="再输一次新密码"></div>' +
    '<p class="hintline">改完请用新密码登录；忘了密码就找' + (lvl >= 2 ? "上级代理商" : "总台") + '重置。</p>';
  openModal("修改我的密码", body, async () => {
    const oldV = (mV("cpOld").value || "");
    const v1 = (mV("cpNew").value || "");
    const v2 = (mV("cpNew2").value || "");
    if (!oldV) { toast("请输入原密码"); return false; }
    if (v1.length < 6) { toast("新密码至少 6 位"); return false; }
    if (v1.length > 64) { toast("新密码最长 64 位"); return false; }
    if (/\s/.test(v1)) { toast("新密码不能包含空格"); return false; }
    if (v1 === oldV) { toast("新密码不能和原密码一样"); return false; }
    if (v1 !== v2) { toast("两次输入的新密码不一样"); return false; }
    const j = await req("/me/password", { method: "POST", body: { old: oldV, new: v1 } });
    toast(j.message || "密码已修改");
    return true;
  }, "确认修改");
}

/* ============================ 手机端：表格转卡片 ============================ */
/* 把表头文字写进每一格的 data-label，配合 console.css 的卡片样式在手机上显示成一叠卡片。 */
function labelizeTable(tb) {
  if (!tb) return;
  const ths = [];
  tb.querySelectorAll("thead th").forEach((th) => ths.push((th.textContent || "").trim()));
  if (!ths.length) return;
  tb.querySelectorAll("tbody tr").forEach((tr) => {
    const tds = [...tr.children];
    if (tds.length !== ths.length) return;      // 合并单元格（空态那行）跳过
    tds.forEach((td, i) => { if (ths[i]) td.setAttribute("data-label", ths[i]); });
  });
}
function labelizeAll() {
  document.querySelectorAll("#views table").forEach(labelizeTable);
}


/* ============================ R635 批量删除 / 复制 ============================ */
async function copyCred(id) {
  try {
    const j = await req("/tenants/" + id + "/secret");
    const txt = "账号：" + (j.username || "") +
      "\n密码：" + (j.password || "（未保存，请在详情里重置）") +
      "\n网址：" + (j.url || "—");
    copyText(txt);
  } catch (e) { toast(e.message || "加载失败"); }
}

function ensureSelBar() {
  let el = $("selBar");
  if (el) return el;
  el = document.createElement("div");
  el.id = "selBar";
  el.className = "selbar";
  el.innerHTML = '<span id="selCnt">已选 0 个</span>' +
    '<button class="btn ghost mini" type="button" id="selAll">全选</button>' +
    '<button class="btn btn--danger mini" type="button" id="selDel">删除</button>' +
    '<button class="btn ghost mini" type="button" id="selCancel">取消</button>';
  document.body.appendChild(el);
  $("selAll").onclick = () => {
    const boxes = Array.from(document.querySelectorAll("#tnBody .tnChk"));
    const all = boxes.length > 0 && boxes.every((b) => b.checked);
    boxes.forEach((b) => { b.checked = !all; });
    updateSelBar();
  };
  $("selDel").onclick = () => {
    const boxes = Array.from(document.querySelectorAll("#tnBody .tnChk")).filter((b) => b.checked);
    if (!boxes.length) { toast("先勾选要删除的账号"); return; }
    batchDeleteModal(boxes.map((b) => num(b.value)),
      boxes.map((b) => b.getAttribute("data-name") || ("#" + b.value)));
  };
  $("selCancel").onclick = () => exitSel();
  return el;
}
function enterSel() {
  state.tnSel = true;
  const p = $("tnPanel"); if (p) p.classList.add("tn-sel");
  const fab = $("fab"); if (fab) fab.classList.add("hide");
  ensureSelBar();
  updateSelBar();
  const bar = $("selBar"); if (bar) bar.classList.add("show");
}
function exitSel() {
  state.tnSel = false;
  const p = $("tnPanel"); if (p) p.classList.remove("tn-sel");
  const bar = $("selBar"); if (bar) bar.classList.remove("show");
  const fab = $("fab"); if (fab) fab.classList.remove("hide");
  document.querySelectorAll("#tnBody .tnChk").forEach((b) => { b.checked = false; });
}
function updateSelBar() {
  const el = ensureSelBar();
  const boxes = Array.from(document.querySelectorAll("#tnBody .tnChk"));
  const n = boxes.filter((b) => b.checked).length;
  $("selCnt").textContent = "已选 " + n + " 个";
  el.classList.toggle("show", !!state.tnSel && boxes.length > 0);
  $("selDel").disabled = n === 0;
}
function batchDeleteModal(ids, names) {
  const show = names.slice(0, 40).map((n) => '<span class="delchip">' + esc(n) + "</span>").join("");
  const more = names.length > 40 ? '<span class="delmore">… 还有 ' + (names.length - 40) + " 个</span>" : "";
  const body =
    '<p class="warnline">⚠️ 将永久删除 <b>' + names.length + "</b> 个账号，连同名下<b>全部卡密、领取记录、额度流水、专属网址、上传文件</b>一起删掉，<b>无法恢复</b>。</p>" +
    '<div class="delchips">' + show + more + "</div>" +
    '<p class="hintline">系统会在服务器 data/deletions/ 留一份删除前备份，方便出问题时找回。</p>';
  openModal("删除账号（不可恢复）", body, async () => {
    const j = await req("/tenants/batch_delete", { method: "POST", body: { ids: ids, confirm: true } });
    toast(j.message || "已删除");
    exitSel();
    PAGES.tenants.page = 0;
    loadTenants();
    return true;
  }, "确认删除");
  const ok = $("mOk"); if (ok) { ok.classList.add("danger"); ok.textContent = "确认删除 " + names.length + " 个"; }
}
function tenantMoreSheet(id, name, st) {
  const lv = lvNum();
  const items = [["secret", "账号密码 / 专属网址"]];
  if (lv === 0 || lv === 1) {
    items.push(["quota+", "加额度"]);
    items.push(["quota-", "回收额度"]);
    items.push(["pwd", "改账号密码"]);
    items.push([st === "disabled" ? "enable" : "disable", st === "disabled" ? "启用账号" : "停用账号"]);
    items.push([st === "banned" ? "unban" : "ban", st === "banned" ? "解封账号" : "封禁账号"]);
  }
  if (lv === 0) items.push(["del", "删除这个账号（含全部数据）"]);
  const body = '<div class="sheet-title">' + esc(name || ("#" + id)) + '</div><div class="sheet-list">' +
    items.map(([k, t]) => '<button type="button" class="sheet-item' + (k === "del" ? " danger" : "") +
      '" data-mact="' + k + '">' + t + "</button>").join("") + "</div>";
  openModal("更多操作", body, () => {}, "关闭");
  document.querySelectorAll("#modalRoot [data-mact]").forEach((b) => {
    b.onclick = () => {
      const k = b.getAttribute("data-mact");
      closeModal();
      if (k === "secret") secretModal(id);
      else if (k === "quota+") quotaModal(id, "1");
      else if (k === "quota-") quotaModal(id, "-1");
      else if (k === "pwd") pwdModal(id, name);
      else if (k === "enable" || k === "unban") setTenantStatus(id, "active");
      else if (k === "disable") setTenantStatus(id, "disabled");
      else if (k === "ban") setTenantStatus(id, "banned");
      else if (k === "del") batchDeleteModal([id], [name || ("#" + id)]);
    };
  });
}

/* ============================ R635 手机 APP 外壳 ============================ */
function buildTabbar() {
  const lv = lvNum();
  const items = MENU.filter((m) => m.lv.indexOf(lv) >= 0);
  const pri = ["dash", "tenants", "codes", "logs"];
  const main = pri.map((k) => items.find((m) => m.k === k)).filter(Boolean);
  const rest = items.filter((m) => main.indexOf(m) < 0);
  let el = $("tabbar");
  if (!el) { el = document.createElement("div"); el.id = "tabbar"; el.className = "tabbar"; document.body.appendChild(el); }
  el.innerHTML = main.map((m) => '<button type="button" data-view="' + m.k + '"><i>' + m.ic + "</i><span>" + m.t + "</span></button>").join("") +
    (rest.length ? '<button type="button" id="tabMore"><i>⋯</i><span>更多</span></button>' : "");
  el.querySelectorAll("[data-view]").forEach((b) => {
    b.onclick = () => { state.view = b.getAttribute("data-view"); render(); };
  });
  const more = $("tabMore");
  if (more) more.onclick = () => moreSheet(rest);
}
function syncTabbar() {
  const el = $("tabbar"); if (!el) return;
  el.querySelectorAll("[data-view]").forEach((b) => {
    b.classList.toggle("on", b.getAttribute("data-view") === state.view);
  });
  const more = $("tabMore");
  if (more) {
    const mainK = ["dash", "tenants", "codes", "logs"];
    more.classList.toggle("on", mainK.indexOf(state.view) < 0);
  }
}
function moreSheet(items) {
  const body = '<div class="sheet-list">' + items.map((m) =>
    '<button type="button" class="sheet-item" data-go="' + m.k + '"><i>' + m.ic + "</i>" + m.t + "</button>").join("") + "</div>";
  openModal("更多功能", body, () => {}, "关闭");
  document.querySelectorAll("#modalRoot [data-go]").forEach((b) => {
    b.onclick = () => { closeModal(); state.view = b.getAttribute("data-go"); render(); };
  });
}
function ensureFab() {
  const lv = lvNum();
  let f = $("fab");
  if (state.view !== "tenants" || (lv !== 0 && lv !== 1)) { if (f) f.remove(); return; }
  if (!f) {
    f = document.createElement("button");
    f.id = "fab"; f.className = "fab"; f.type = "button"; f.textContent = "+";
    document.body.appendChild(f);
  }
  f.onclick = () => fabSheet();
}
function fabSheet() {
  const lv = lvNum();
  const items = [["new", lv === 0 ? "新建代理商" : "新建子代理"]];
  if (lv === 0) items.push(["batch", "批量新建代理商"]);
  const body = '<div class="sheet-list">' + items.map(([k, t]) =>
    '<button type="button" class="sheet-item" data-fab="' + k + '">' + t + "</button>").join("") + "</div>";
  openModal("新建", body, () => {}, "关闭");
  document.querySelectorAll("#modalRoot [data-fab]").forEach((b) => {
    b.onclick = () => { const k = b.getAttribute("data-fab"); closeModal(); if (k === "batch") batchTenantModal(); else newTenantModal(); };
  });
}

/* ============================ 启动 ============================ */

async function boot() {
  const j = await rawGet("/a/api/me");
  if (j && j.ok && j.me) ME = j.me;
  if (!ME) {
    const j2 = await rawGet("/admin/api/dashboard");
    if (j2 && j2.ok) ME = { id: 0, username: "总台", level: 0, status: "active" };
  }
  if (!ME) { location.href = "/a/login"; return; }
  IS_TOTAL = lvNum() === 0;
  BASE = IS_TOTAL ? "/admin/api" : "/a/api";
  LOGIN_URL = IS_TOTAL ? "/admin"
    : (window.__SITE_LOGIN || localStorage.getItem("site_entry") || "/a/login");
  $("logoutBtn").onclick = () => { location.href = IS_TOTAL ? "/admin/logout" : "/a/logout"; };
  const pwdBtn = $("pwdBtn");
  if (pwdBtn) {
    if (IS_TOTAL) { pwdBtn.style.display = "none"; }
    else { pwdBtn.style.display = ""; pwdBtn.onclick = openChangePwd; }
  }
  $("refreshBtn").onclick = () => { if (VIEWS[state.view].reload) VIEWS[state.view].reload(); else render(); };
  $("whoV").textContent = ME.username || "—";
  const role = $("whoRole");
  role.textContent = lvlText(ME.level) + (String(ME.status) === "disabled" ? " · 只读" : "");
  role.className = "who-tag " + lvlCls(ME.level);
  buildNav();
  buildTabbar();
  render();
  labelizeAll();
  // 表格是随时重渲染的，这里盯着 #views：一有表格变化就补上 data-label（不加属性监听，不会死循环）
  const mo = new MutationObserver(() => {
    clearTimeout(mo._t);
    mo._t = setTimeout(labelizeAll, 40);
  });
  mo.observe($("views"), { childList: true, subtree: true });
  setInterval(() => {
    if (state.view === "dash") loadDash(true);
    else if (state.view === "logs") loadLogs();
  }, 15000);
}
boot();
