/* 兑换码后台逻辑 */
const $ = (id) => document.getElementById(id);
let LAST = [];

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(window._tt); window._tt = setTimeout(() => t.classList.remove("show"), 2400);
}
function esc(v){return String(v==null?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}

/* ---- 兑换码列表分页 ---- */
let page = 0, pageSize = 20, total = 0, pages = 1;
function setPage(n) {
  page = Math.max(0, Math.min(Number(n) || 0, Math.max(0, pages - 1)));
  load();
}
function setPageSize(v) { pageSize = parseInt(v || "20", 10); page = 0; load(); }
function pageFirst() { setPage(0); }
function pagePrev() { setPage(page - 1); }
function pageNext() { setPage(page + 1); }
function pageLast() { setPage(pages - 1); }
function pageSizeChange(v) { setPageSize(v); }

/* ---- 领取记录分页 ---- */
let logPage = 0, logSize = 50, logTotal = 0, logPages = 1;
function setLogPage(n) {
  logPage = Math.max(0, Math.min(Number(n) || 0, Math.max(0, logPages - 1)));
  loadLogs();
}
function setLogSize(v) { logSize = parseInt(v || "50", 10); logPage = 0; loadLogs(); }
function logSizeChange(v) { setLogSize(v); }
function logPrevPage() { setLogPage(logPage - 1); }
function logNextPage() { setLogPage(logPage + 1); }
function onLogFilter() { logPage = 0; loadLogs(); }
function showLogsOf(code) {
  $("logCode").value = code;
  $("logKind").value = "";
  logPage = 0;
  loadLogs();
  const el = $("logPanel");
  if (el && el.scrollIntoView) el.scrollIntoView({ behavior: "smooth", block: "start" });
}

function claimText(v) {
  const n = parseInt(v || 0, 10);
  if (!n || n <= 0) return "不限次";
  return n + " 次";
}

let sigList = "", sigLog = "";

async function load() {
  const st = $("fStatus").value, batch = $("fBatch").value;
  const r = await fetch(`/admin/api/list?status=${encodeURIComponent(st)}&batch=${encodeURIComponent(batch)}`
    + `&limit=${pageSize}&offset=${page * pageSize}`, { cache: "no-store" });
  if (r.status === 403) { location.href = "/admin"; return; }
  const j = await r.json();
  // 统计
  const s = j.stats || {};
  $("sTotal").textContent = s.total || 0;
  $("sUnused").textContent = s.unused || 0;
  $("sVisiting").textContent = s.visiting || 0;
  $("sDone").textContent = s.done || 0;
  $("sClaims").textContent = s.claims || 0;
  if (j.base) $("baseHint").textContent = j.base + "/兑换码";
  // 批次下拉
  total = (j.total != null) ? j.total : ((j.rows || []).length);
  pages = j.pages || 1;
  if (page > pages - 1) page = pages - 1;
  if (page < 0) page = 0;
  $("fCount").textContent = `共 ${total} 条`;
  $("pageInfo").textContent = `第 ${page + 1} / ${pages} 页 · 每页 ${pageSize} 条 · 共 ${total} 条`;
  $("pageFirst").disabled = $("pagePrev").disabled = (page <= 0);
  $("pageNext").disabled = $("pageLast").disabled = (page >= pages - 1);
  const sel = $("fBatch");
  if (sel.options.length <= 1 && (j.batches || []).length) {
    (j.batches || []).forEach(b => {
      const o = document.createElement("option");
      o.value = b.batch; o.textContent = `${b.batch}（${b.n}）`;
      sel.appendChild(o);
    });
  }
  syncBatchOptions("dlBatch", j.batches);
  // 表格（内容没变就不重画 → 页面不抖）
  const tb = $("tbody");
  const rowsHtml = (j.rows || []).map(r => `
    <tr>
      <td class="code">${esc(r.code)}</td>
      <td><span class="link cp" data-cp="${esc(r.url)}" title="点击复制：${esc(r.url)}">${esc(r.url)}</span></td>
      <td><span class="tag ${esc(r.status)}">${esc(stText(r.status))}</span></td>
      <td class="mono">${r.claims || 0}</td>
      <td class="muted">${esc(r.created_at_str || "")}</td>
      <td class="muted">${esc(r.used_at_str || "—")}</td>
      <td class="muted">${esc(r.used_ip || "—")}</td>
      <td><button class="btn ghost mini" data-logs="${esc(r.code)}">记录</button>
        <button class="btn ghost mini" data-tog="${esc(r.code)}|${r.status === "disabled" ? "unused" : "disabled"}">
        ${r.status === "disabled" ? "启用" : "停用"}</button></td>
    </tr>`).join("") || `<tr><td colspan="8" class="muted" style="text-align:center;padding:22px">暂无数据</td></tr>`;
  if (sigList !== rowsHtml) { sigList = rowsHtml; tb.innerHTML = rowsHtml; }
  syncSelectLabels();
}

function stText(s) { return ({ unused: "未使用", visiting: "已进入", done: "已完成", disabled: "已停用" })[s] || s; }

async function gen() {
  const count = parseInt($("count").value || "0", 10);
  if (!count || count < 1) return toast("请输入数量");
  if (count > 100000) return toast("单次最多 10 万");
  $("genBtn").disabled = true; $("genBtn").textContent = "生成中…";
  try {
    const r = await fetch("/admin/api/gen", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ count, note: $("note").value || "" })
    });
    const j = await r.json();
    LAST = j.items || [];
    $("genOut").innerHTML = LAST.map(x => `<span class="link cp" title="点击复制：${esc(x.url)}" data-cp="${esc(x.url)}">${esc(x.url)}</span>`).join("\n") || "（无）";
    $("genWrap").classList.remove("hide");
    $("genCount").textContent = LAST.length;
    toast(`已生成 ${LAST.length} 个兑换码（每码限领 1 次）`);
    load();
  } catch (e) { toast("生成失败：" + e); }
  $("genBtn").disabled = false; $("genBtn").textContent = "生成兑换码";
}

// 复制兜底：http(非安全上下文) 下 navigator.clipboard 是 undefined，
// 回退 textarea + execCommand，再不行弹窗让用户手抄。
function _legacyCopy(t) {
  try {
    const ta = document.createElement("textarea");
    ta.value = t; ta.setAttribute("readonly", "");
    ta.style.position = "fixed"; ta.style.top = "-1000px"; ta.style.left = "-1000px"; ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.focus(); ta.select(); ta.setSelectionRange(0, ta.value.length);
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (e) { return false; }
}
async function copyText(t) {
  t = String(t == null ? "" : t);
  if (!t) return false;
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(t); return true; } catch (e) {}
  }
  if (_legacyCopy(t)) return true;
  if (t.length <= 2000) { try { window.prompt("请手动复制（Ctrl+C）：", t); } catch (e) {} }
  return false;
}
async function cpURLs() {
  const txt = LAST.map(x => x.url).join("\n");
  const ok = await copyText(txt);
  toast(ok ? ("已复制 " + LAST.length + " 条链接") : "复制失败，请手动选择复制");
}
async function cpOnlyCodes() {
  const txt = LAST.map(x => x.code).join("\n");
  const ok = await copyText(txt);
  toast(ok ? ("已复制 " + LAST.length + " 个兑换码") : "复制失败，请手动选择复制");
}
async function cp(t) {
  const ok = await copyText(t);
  toast(ok ? "已复制" : "复制失败，请手动选择复制");
}
/* 导出：把文本直接生成 .txt 文件下载（纯前端，无需服务端） */
function downloadTxt(name, text) {
  try {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url; a.download = name; a.style.display = "none";
    document.body.appendChild(a); a.click();
    setTimeout(() => { try { URL.revokeObjectURL(url); a.remove(); } catch (e) {} }, 1500);
    return true;
  } catch (e) { return false; }
}
function genStamp() {
  const d = new Date(), p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
}
function exportGen(fmt) {
  if (!LAST.length) return toast("还没有生成结果");
  const txt = LAST.map(x => (fmt === "code" ? x.code : x.url)).join("\n");
  const name = (fmt === "code" ? "兑换码_" : "兑换码链接_") + genStamp() + ".txt";
  const ok = downloadTxt(name, txt);
  toast(ok ? ("已导出 " + LAST.length + " 条 TXT 文件") : "导出失败，请改用「复制全部链接」");
}

async function toggle(code, status) {
  await fetch("/admin/api/set", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ code, status })
  });
  load();
}

async function copyFiltered() {
  const st = $("fStatus").value, batch = $("fBatch").value;
  try {
    const r = await fetch(`/admin/api/export?fmt=url&status=${encodeURIComponent(st)}&batch=${encodeURIComponent(batch)}`, { cache: "no-store" });
    if (r.status === 403) { location.href = "/admin"; return; }
    const txt = await r.text();
    const n = txt.split("\n").filter(Boolean).length;
    const ok = await copyText(txt);
    if (ok) toast(`已复制当前筛选的 ${n} 条链接`);
    else toast("复制失败，请改用「导出链接」");
  } catch (e) { toast("复制失败，请改用「导出链接」"); }
}

async function savePwd() {
  const old = $("pOld").value, p1 = $("pNew").value, p2 = $("pNew2").value;
  if (!old || !p1) return toast("请填写完整");
  if (p1.length < 8) return toast("新密码至少 8 位");
  if (p1 !== p2) return toast("两次输入的新密码不一致");
  $("pwdBtn").disabled = true; $("pwdBtn").textContent = "提交中…";
  try {
    const r = await fetch("/admin/api/password", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ old, new: p1 })
    });
    const j = await r.json();
    if (j.ok) { $("pOld").value = $("pNew").value = $("pNew2").value = ""; toast("✅ " + j.message); }
    else toast("❌ " + (j.message || "修改失败"));
  } catch (e) { toast("修改失败：" + e); }
  $("pwdBtn").disabled = false; $("pwdBtn").textContent = "修改后台密码";
}


/* ---------------- 安全 / 谷歌验证器 ---------------- */
async function loadSecurity() {
  try {
    const r = await fetch("/admin/api/security", { cache: "no-store" });
    if (r.status === 403) { location.href = "/admin"; return; }
    const j = await r.json();
    const on = !!j.totp_enabled;
    $("secDot").className = "sec-dot " + (on ? "on" : "off");
    $("secState").textContent = on ? "两步验证已启用" : "两步验证未启用（建议立刻绑定）";
    $("secMeta").textContent = on ? `恢复码剩余 ${j.recovery_left} 个 · 登录有效期 ${j.session_hours}h · 密码存于${j.password_source}`
                                  : "只靠密码，被撞库/泄露就能进后台";
    $("bindBtn").textContent = on ? "重新绑定验证器" : "生成二维码绑定";
    $("unbindBtn").classList.toggle("hide", !on);
    $("recBtn").classList.toggle("hide", !on);
    $("secRate").textContent = `登录 ${j.rate.login} 次/10 分钟 · 卡密入口 ${j.rate.code} 次/分钟`;
  } catch (e) { /* 忽略 */ }
}

async function totpSetup() {
  $("bindBtn").disabled = true;
  try {
    const j = await (await fetch("/admin/api/totp/setup", { method: "POST" })).json();
    if (!j.ok) return toast("生成失败");
    $("secSecret").textContent = j.secret;
    $("qrBox").innerHTML = j.qr_svg || "";
    if (!j.qr_svg) $("qrBox").innerHTML = '<p class="muted" style="margin:0">（服务器未安装 qrcode，请手动把密钥填进验证器）</p>';
    $("secRecovery").classList.add("hide");
    $("secSetup").classList.remove("hide");
    $("secCode").value = "";
    $("secCode").focus();
    toast("扫二维码后输入 6 位动态码");
  } catch (e) { toast("生成失败：" + e); }
  $("bindBtn").disabled = false;
}

async function totpEnable() {
  const code = $("secCode").value.trim();
  if (code.length !== 6) return toast("请输入 6 位动态码");
  $("enableBtn").disabled = true;
  try {
    const r = await fetch("/admin/api/totp/enable", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ code })
    });
    const j = await r.json();
    if (j.ok) {
      $("secSetup").classList.add("hide");
      showRecovery(j.recovery_codes);
      toast("✅ " + j.message);
      loadSecurity();
    } else toast("❌ " + (j.message || "启用失败"));
  } catch (e) { toast("启用失败：" + e); }
  $("enableBtn").disabled = false;
}

async function totpDisable() {
  const code = prompt("输入当前动态码（或一个恢复码）以关闭两步验证：");
  if (!code) return;
  const r = await fetch("/admin/api/totp/disable", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ code })
  });
  const j = await r.json();
  toast((j.ok ? "✅ " : "❌ ") + (j.message || ""));
  loadSecurity();
}

async function totpRecovery() {
  const code = prompt("输入当前动态码以重新生成 10 个恢复码（旧的作废）：");
  if (!code) return;
  const r = await fetch("/admin/api/totp/recovery", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ code })
  });
  const j = await r.json();
  if (j.ok) { showRecovery(j.recovery_codes); toast("✅ " + j.message); }
  else toast("❌ " + (j.message || "失败"));
  loadSecurity();
}

function cpSecret() { cp($("secSecret").textContent); }
function cpRecovery() { cp($("recList").textContent.replace(/\s+/g, " ")); }
function hideRecovery() { $("secRecovery").classList.add("hide"); }

function showRecovery(codes) {
  if (!codes || !codes.length) return;
  $("recList").innerHTML = codes.map(c => `<span class="rec">${esc(c)}</span>`).join("");
  $("secRecovery").classList.remove("hide");
}

function exportTxt(fmt) {
  const st = $("fStatus").value, batch = $("fBatch").value;
  location.href = `/admin/api/export?fmt=${fmt}&status=${encodeURIComponent(st)}&batch=${encodeURIComponent(batch)}`;
}

/* ---------------- 安全 / 访问控制（IP 白名单 + 封禁） ---------------- */
async function loadGuard() {
  try {
    const r = await fetch("/admin/api/guard", { cache: "no-store" });
    if (r.status === 403) { location.href = "/admin"; return; }
    const j = await r.json();
    $("myIp").textContent = j.ip || "—";
    $("wlOn").checked = !!j.whitelist_on;
    if (document.activeElement !== $("wlList")) $("wlList").value = (j.whitelist || []).join("\n");
    const bans = j.bans || [];
    $("banCount").textContent = bans.length ? `共 ${bans.length} 个封禁` : "暂无封禁";
    $("banBody").innerHTML = bans.length ? bans.map(b => `
      <tr>
        <td class="mono">${esc(b.ip)}</td>
        <td class="mono">${b.until > 0 ? Math.max(1, Math.ceil(b.left / 60)) + " 分" : "永久"}</td>
        <td class="muted">${esc(b.reason || "")}${b.fails ? `（失败 ${b.fails} 次）` : ""}</td>
        <td><button class="btn ghost mini" data-unban="${esc(b.ip)}">解封</button></td>
      </tr>`).join("") : `<tr><td colspan="4" class="muted" style="text-align:center;padding:16px">暂无封禁</td></tr>`;
  } catch (e) { /* 忽略 */ }
}

async function saveWhitelist() {
  const items = $("wlList").value.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean);
  const on = $("wlOn").checked;
  try {
    const j = await (await fetch("/admin/api/guard/whitelist", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ items, on })
    })).json();
    toast((j.ok ? "✅ " : "❌ ") + (j.message || ""));
    loadGuard();
  } catch (e) { toast("保存失败：" + e); }
}

async function allowMyIp() {
  const ip = $("myIp").textContent.trim();
  if (!ip || ip === "—") return toast("拿不到当前 IP");
  const items = $("wlList").value.split(/[\n,;]+/).map(s => s.trim()).filter(Boolean);
  if (!items.includes(ip)) items.push(ip);
  $("wlList").value = items.join("\n");
  $("wlOn").checked = true;
  await saveWhitelist();
}

async function doUnban(ip) {
  const j = await (await fetch("/admin/api/guard/unban", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ ip })
  })).json();
  toast((j.ok ? "✅ " : "❌ ") + (j.message || ""));
  loadGuard();
}

async function doBan() {
  const ip = $("banIp").value.trim();
  if (!ip) return toast("请输入 IP");
  const mins = Math.max(1, parseInt($("banMin").value || "60", 10));
  const j = await (await fetch("/admin/api/guard/ban", {
    method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ ip, mins })
  })).json();
  toast((j.ok ? "✅ " : "❌ ") + (j.message || ""));
  $("banIp").value = "";
  loadGuard();
}

/* ---------------- 领取记录（扫码 / 成功 / 失败，带时间） ---------------- */
function kindCls(k) {
  // 复用现有 tag 配色：unused=蓝(信息) visiting=黄(进行中) done=绿(成功) disabled=红(失败)
  return ({ enter: "unused", scan: "visiting", login: "done", success: "done", fail: "disabled" })[k] || "";
}

/* R82-L：领取记录里补 uid + 到期时间变化（例：个人订阅 10-24 → 11-23） */
function shortDay(v) {
  const t = String(v == null ? "" : v).trim();
  if (t.length >= 10 && t.charAt(4) === "-" && t.charAt(7) === "-") return t.substring(5, 10);
  return t;
}
function uidSub(x) {
  const d = (x && x.detail_obj) || {};
  const u = String((x && (x.uid || d.uid)) || "").trim();
  const bm = String(d.before_member || "").trim(), be = String(d.before_expiry || "").trim();
  const am = String(d.after_member || "").trim(), ae = String(d.after_expiry || "").trim();
  const parts = [];
  if (u) parts.push("uid " + esc(u));
  if (am || ae || bm || be) {
    const nm = am || bm || "";
    parts.push(esc((nm ? nm + " " : "") + (be ? shortDay(be) : "—") + " → " + (ae ? shortDay(ae) : "—") + "（北京）"));
  }
  return parts.length ? '<div class="muted mono" style="font-size:11px;line-height:1.5">' + parts.join(" · ") + "</div>" : "";
}

async function loadLogs() {
  const code = ($("logCode").value || "").trim().toUpperCase();
  const kind = $("logKind").value || "";
  try {
    const r = await fetch(`/admin/api/logs?limit=${logSize}&offset=${logPage * logSize}`
      + `&code=${encodeURIComponent(code)}&kind=${encodeURIComponent(kind)}`, { cache: "no-store" });
    if (r.status === 403) { location.href = "/admin"; return; }
    const j = await r.json();
    logTotal = j.total || 0;
    logPages = j.pages || 1;
    if (logPage > logPages - 1) logPage = logPages - 1;
    if (logPage < 0) logPage = 0;
    const st = j.stats || {};
    const statsHtml =
      `<span class="tag unused">进入领取 ${st.enter || 0}</span>` +
      `<span class="tag visiting">已扫码 ${st.scan || 0}</span>` +
      `<span class="tag done">登录成功 ${st.login || 0}</span>` +
      `<span class="tag done">领取成功 ${st.success || 0}</span>` +
      `<span class="tag disabled">未到账 ${st.fail || 0}</span>`;
    if ($("logStats").innerHTML !== statsHtml) $("logStats").innerHTML = statsHtml;
    const rowsHtml = (j.rows || []).map(x => `
      <tr>
        <td class="muted mono">${esc(x.time_str || "")}</td>
        <td class="code">${esc(x.code || "")}</td>
        <td><span class="tag ${kindCls(x.kind)}">${esc(x.kind_text || x.kind || "")}</span></td>
        <td>${esc(x.account || "—")}${uidSub(x)}</td>
        <td class="muted mono">${esc(x.ip || "—")}</td>
        <td class="muted">${esc(x.note || "—")}</td>
      </tr>`).join("") || `<tr><td colspan="6" class="muted" style="text-align:center;padding:22px">暂无记录</td></tr>`;
    if (sigLog !== rowsHtml) { sigLog = rowsHtml; $("logBody").innerHTML = rowsHtml; }   // 内容没变就不重画 → 不抖
    $("logPageInfo").textContent = `第 ${logPage + 1} / ${logPages} 页 · 共 ${logTotal} 条`;
    $("logPrev").disabled = (logPage <= 0);
    $("logNext").disabled = (logPage >= logPages - 1);
    syncSelectLabels();
  } catch (e) { /* 忽略 */ }
}

/* ---------------- 自制深色下拉：把系统原生白底弹层换掉 ---------------- */
let _selDocBound = false;
function enhanceSelects() {
  document.querySelectorAll("select").forEach(sel => {
    if (sel.dataset.wzsel === "1" || sel.multiple) return;
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
    wrap.appendChild(btn);
    wrap.appendChild(list);
    if (/^\d/.test(sel.style.width || "")) wrap.style.minWidth = sel.style.width;
    wrap.addEventListener("click", ev => ev.stopPropagation());
    btn.addEventListener("click", ev => {
      ev.stopPropagation();
      if (wrap.classList.contains("open")) { closeAllSel(); return; }
      closeAllSel();
      buildSelList(sel, list);
      wrap.classList.add("open");
      const pn = wrap.closest(".panel");   // ★ 抬升所在面板，否则会被下面的面板盖住
      if (pn) pn.classList.add("z-top");
      const r = wrap.getBoundingClientRect();
      const below = window.innerHeight - r.bottom;
      wrap.classList.toggle("up", below < 330 && r.top > 350);
    });
    sel.addEventListener("change", syncSelectLabels);
  });
  if (!_selDocBound) {
    _selDocBound = true;
    document.addEventListener("click", closeAllSel);
    document.addEventListener("keydown", ev => { if (ev.key === "Escape") closeAllSel(); });
  }
  syncSelectLabels();
}
function closeAllSel() {
  document.querySelectorAll(".wzsel.open").forEach(w => w.classList.remove("open"));
  document.querySelectorAll(".panel.z-top").forEach(p => p.classList.remove("z-top"));
}
function buildSelList(sel, list) {
  list.innerHTML = "";
  Array.from(sel.options).forEach(op => {
    const it = document.createElement("div");
    it.className = "wzsel-item" + (op.value === sel.value ? " on" : "");
    it.innerHTML = `<span>${esc(op.textContent)}</span><span class="ck">\u2713</span>`;
    it.addEventListener("click", ev => {
      ev.stopPropagation();
      sel.value = op.value;
      sel.dispatchEvent(new Event("change", { bubbles: true }));
      closeAllSel();
      syncSelectLabels();
    });
    list.appendChild(it);
  });
}
function syncSelectLabels() {
  document.querySelectorAll(".wzsel").forEach(w => {
    const sel = w.querySelector("select"), lb = w.querySelector(".lb");
    if (sel && lb) {
      const op = sel.options[sel.selectedIndex];
      lb.textContent = op ? op.textContent : "";
    }
  });
}

/* ============================ R80：充值码 ============================ */
let RC_LAST = [];
let rcPage = 0, rcSize = 50, rcTotal = 0, rcPages = 1;

function rcSetPage(n) {
  rcPage = Math.max(0, Math.min(Number(n) || 0, Math.max(0, rcPages - 1)));
  loadRecharge();
}
function rcSetSize(v) { rcSize = parseInt(v || "50", 10); rcPage = 0; loadRecharge(); }

async function genRecharge() {
  const amount = parseInt(($("rcAmount") || {}).value || "0", 10);
  const count = parseInt(($("rcCount") || {}).value || "0", 10);
  if (!amount || amount < 1) return toast("请输入每个充值码的面额（张数）");
  if (!count || count < 1) return toast("请输入要生成的数量");
  const btn = $("rcGenBtn");
  if (btn) { btn.disabled = true; btn.textContent = "生成中…"; }
  try {
    const r = await fetch("/admin/api/recharge/gen", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        amount: amount, count: count,
        batch: ($("rcBatch") || {}).value || "", note: ($("rcNote") || {}).value || ""
      })
    });
    if (r.status === 401 || r.status === 403) { location.href = "/admin"; return; }
    const j = await r.json();
    if (!j.ok) throw new Error(j.message || "生成失败");
    RC_LAST = j.rows || [];
    $("rcOut").innerHTML = RC_LAST.map(function (x) {
      return '<span class="link cp" data-cp="' + esc(x.code_str) + '" title="点击复制">' + esc(x.code_str) + "</span>";
    }).join("\n") || "（无）";
    $("rcWrap").classList.remove("hide");
    $("rcGenCount").textContent = RC_LAST.length + " 个 · 每个 " + amount + " 张";
    toast(j.message || "已生成");
    rcPage = 0;
    loadRecharge();
  } catch (e) { toast("生成失败：" + (e && e.message ? e.message : e)); }
  if (btn) { btn.disabled = false; btn.textContent = "生成充值码"; }
}

function cpRecharge() {
  if (!RC_LAST.length) return toast("还没有生成结果");
  cp(RC_LAST.map(function (x) { return x.code_str; }).join("\n"));
}

function exportRecharge() {
  if (!RC_LAST.length) return toast("还没有生成结果");
  const txt = RC_LAST.map(function (x) { return x.code_str + "\t" + x.amount + " 张"; }).join("\r\n");
  const ok = downloadTxt("充值码_" + genStamp() + ".txt", txt);
  toast(ok ? ("已导出 " + RC_LAST.length + " 条 TXT 文件") : "导出失败，请改用「复制全部」");
}

async function voidRecharge(id) {
  if (!confirm("确定作废这个充值码？作废后不可恢复（只能作废未使用的）")) return;
  try {
    const r = await fetch("/admin/api/recharge/void", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ ids: [Number(id)] })
    });
    const j = await r.json();
    if (!j.ok) throw new Error(j.message || "作废失败");
    toast(j.message || "已作废");
    loadRecharge();
  } catch (e) { toast((e && e.message) || "作废失败"); }
}

async function loadRecharge() {
  const tb = $("rcBody");
  if (!tb) return;
  const st = (($("rcFStatus") || {}).value) || "";
  const bt = (($("rcFBatch") || {}).value) || "";
  const q = (($("rcQ") || {}).value || "").trim();
  try {
    const r = await fetch("/admin/api/recharge?status=" + encodeURIComponent(st)
      + "&batch=" + encodeURIComponent(bt) + "&q=" + encodeURIComponent(q)
      + "&limit=" + rcSize + "&offset=" + rcPage * rcSize, { cache: "no-store" });
    if (r.status === 401 || r.status === 403) { location.href = "/admin"; return; }
    const j = await r.json();
    const rows = j.rows || [];
    rcTotal = Number(j.total || 0);
    rcPages = Math.max(1, Number(j.pages || 1));
    if (rcPage > rcPages - 1) rcPage = rcPages - 1;
    tb.innerHTML = rows.length ? rows.map(function (x) {
      const s = String(x.status || "");
      const cls = s === "used" ? "done" : (s === "void" ? "disabled" : "unused");
      const meta = (x.batch || "") + (x.note ? ((x.batch ? " · " : "") + x.note) : "");
      const ops = s === "unused" ? '<button class="btn ghost mini" data-rcvoid="' + x.id + '">作废</button>' : "";
      return "<tr>"
        + '<td><span class="code cp" data-cp="' + esc(x.code_str) + '" title="点击复制">' + esc(x.code_str) + "</span></td>"
        + '<td class="mono">' + esc(String(x.amount || 0)) + " 张</td>"
        + '<td><span class="tag ' + cls + '">' + esc(x.status_text || "") + "</span></td>"
        + "<td>" + esc(x.used_by_name || "—") + "</td>"
        + '<td class="muted mono">' + esc(x.used_str || "—") + "</td>"
        + '<td class="muted mono">' + esc(x.created_str || "—") + "</td>"
        + '<td class="muted">' + esc(meta || "—") + "</td>"
        + "<td>" + ops + "</td></tr>";
    }).join("") : '<tr><td colspan="8" class="muted" style="text-align:center;padding:22px">还没有生成过充值码</td></tr>';
    const s2 = j.stats || {};
    if ($("rcStats")) {
      $("rcStats").textContent = "共 " + (s2.total || 0) + " 个 · 未使用 " + (s2.unused || 0)
        + "（" + (s2.amount_unused || 0) + " 张） · 已充值 " + (s2.used || 0)
        + "（" + (s2.amount_used || 0) + " 张） · 已作废 " + (s2.void || 0);
    }
    if ($("rcPageInfo")) {
      $("rcPageInfo").textContent = "第 " + (rcPage + 1) + " / " + rcPages + " 页 · 共 " + rcTotal + " 条";
    }
    if ($("rcFBatch")) {
      const bs = j.batches || [];
      const cur = $("rcFBatch").value;
      const html = '<option value="">全部批次</option>' + bs.map(function (b) {
        return '<option value="' + esc(b.batch) + '"' + (b.batch === cur ? " selected" : "") + ">"
          + esc(b.batch) + "（" + b.n + "）</option>";
      }).join("");
      if ($("rcFBatch").innerHTML !== html) $("rcFBatch").innerHTML = html;
    }
    if (typeof labelizeAll === "function") labelizeAll();
  } catch (e) { /* 忽略 */ }
}

document.addEventListener("click", function (e) {
  const t = e.target && e.target.closest ? e.target.closest("#rcListPanel [data-cp]") : null;
  if (t) { cp(t.getAttribute("data-cp") || ""); return; }
  const b = e.target && e.target.closest ? e.target.closest("[data-rcvoid]") : null;
  if (b) voidRecharge(b.getAttribute("data-rcvoid"));
});

enhanceSelects();
load();
loadSecurity();
loadGuard();
loadLogs();
setInterval(load, 8000);
setInterval(loadSecurity, 30000);
setInterval(loadGuard, 15000);
setInterval(loadLogs, 10000);

/* ---- R66：一次性下载链接 ---- */
bootDl();
setInterval(loadDl, 15000);

/* ---- R80：充值码 ---- */
loadRecharge();
setInterval(loadRecharge, 20000);

/* ---- R66：手机端表格转卡片（表头写进 data-label） ---- */
labelizeAll();
if (window.MutationObserver) {
  let _lblTimer = null;
  const _mo = new MutationObserver(() => {
    clearTimeout(_lblTimer);
    _lblTimer = setTimeout(labelizeAll, 40);
  });
  _mo.observe(document.body, { childList: true, subtree: true });
}


/* ========================================================================== */
/* R66 总台后台：手机端「表格转卡片」 + 一次性下载链接                          */
/* ========================================================================== */

/* ---- 把 thead 的文字写进每格 data-label，CSS 用它当字段名（手机端卡片） ---- */
function labelizeTable(tb) {
  const table = (tb && tb.closest) ? tb.closest("table") : null;
  if (!table) return;
  const head = table.querySelector("thead");
  if (!head) return;
  const labels = Array.from(head.querySelectorAll("th")).map(th => (th.textContent || "").trim());
  if (!labels.length) return;
  Array.from(tb.querySelectorAll("tr")).forEach(tr => {
    const tds = Array.from(tr.children);
    if (tds.length === 1 && tds[0].hasAttribute("colspan")) return;   // "暂无数据"行
    tds.forEach((td, i) => {
      const lb = labels[i];
      if (lb && !td.hasAttribute("data-label")) td.setAttribute("data-label", lb);
    });
  });
}
function labelizeAll() {
  Array.from(document.querySelectorAll("table tbody")).forEach(labelizeTable);
}

/* ---- 批次下拉：把卡密列表的批次同步到下载面板 ---- */
function syncBatchOptions(id, batches) {
  const sel = $(id);
  if (!sel || !batches || !batches.length) return;
  const cur = sel.value;
  const seen = new Set(Array.from(sel.options).map(o => o.value));
  batches.forEach(b => {
    if (seen.has(b.batch)) return;
    const o = document.createElement("option");
    o.value = b.batch; o.textContent = `${b.batch}（${b.n}）`;
    sel.appendChild(o);
  });
  sel.value = cur;
  syncSelectLabels();
}

/* ---- 下载面板：切换内容来源 ---- */
function onDlSource() {
  const src = $("dlSource") ? $("dlSource").value : "batch";
  const isText = (src === "text");
  if ($("dlText")) $("dlText").classList.toggle("hide", !isText);
  ["dlBatch", "dlFmt"].forEach(id => {
    const e = $(id);
    if (!e) return;
    const box = e.closest ? e.closest(".wzsel") : null;
    (box || e).classList.toggle("hide", isText);
  });
  syncSelectLabels();
}

async function genDl() {
  if (!$("dlSource")) return;
  let hits = parseInt($("dlHits").value || "1", 10);
  if (!hits || hits <= 0) hits = 100000;                       // 「不限次数」
  const body = {
    source: $("dlSource").value,
    fmt: $("dlFmt").value,
    batch: $("dlBatch").value,
    status: $("fStatus") ? $("fStatus").value : "",
    name: ($("dlName").value || "").trim(),
    text: $("dlText") ? $("dlText").value : "",
    ttl_hours: parseInt($("dlTtl").value || "24", 10),
    max_hits: hits
  };
  $("dlBtn").disabled = true; $("dlBtn").textContent = "生成中…";
  try {
    const r = await fetch("/admin/api/dl", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body)
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) throw new Error(j.message || ("HTTP " + r.status));
    const it = j.item || {};
    $("dlUrl").textContent = it.url || "";
    $("dlWrap").classList.remove("hide");
    toast(j.message || "下载链接已生成");
    loadDl();
  } catch (e) { toast("生成失败：" + (e.message || e)); }
  $("dlBtn").disabled = false; $("dlBtn").textContent = "生成下载链接";
}

function cpDlUrl() {
  const el = $("dlUrl");
  cp(el ? (el.textContent || "") : "");
}

async function revokeDl(tok) {
  if (!tok) return;
  if (!window.confirm("撤销后这条下载链接立刻失效，确定？")) return;
  try {
    const r = await fetch("/admin/api/dl/revoke", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ token: tok })
    });
    const j = await r.json().catch(() => ({}));
    toast(j.message || (j.ok ? "已撤销" : "操作失败"));
  } catch (e) { toast("操作失败：" + e); }
  loadDl();
}

let sigDl = "";
async function loadDl() {
  const tb = $("dlBody");
  if (!tb) return;
  const st = $("dlFilter") ? $("dlFilter").value : "";
  try {
    const r = await fetch(`/admin/api/dl?status=${encodeURIComponent(st)}&limit=50`, { cache: "no-store" });
    if (r.status === 403) { location.href = "/admin"; return; }
    const j = await r.json();
    const rows = j.rows || [];
    const html = rows.map(x => `
      <tr>
        <td class="mono">${esc(x.name || "")}</td>
        <td class="dlurl"><span class="link cp" data-cp="${esc(x.url || "")}" title="点击复制">${esc(x.url || "")}</span></td>
        <td class="muted">${esc(x.size_str || "")}</td>
        <td class="mono">${esc(String(x.left_hits || 0))} / ${esc(String(x.max_hits || 1))}</td>
        <td><span class="tag ${esc(x.status || "")}">${esc(x.status_text || "")}</span></td>
        <td class="muted">${esc(x.created_str || "")}</td>
        <td class="muted">${esc(x.used_str || "—")}</td>
        <td class="muted">${esc(x.used_ip || "—")}</td>
        <td><button class="btn ghost mini" data-cp="${esc(x.url || "")}">复制</button>
          <button class="btn ghost mini" data-tok="${esc(x.token || "")}">撤销</button></td>
      </tr>`).join("") || `<tr><td colspan="9" class="muted" style="text-align:center;padding:22px">还没有生成过下载链接</td></tr>`;
    if (sigDl !== html) { sigDl = html; tb.innerHTML = html; }
    const s = j.stats || {};
    if ($("dlStats")) {
      $("dlStats").textContent = `共 ${j.total || 0} 条 · 可下载 ${s.active || 0} / 已下载 ${s.used || 0} / 过期 ${s.expired || 0} / 已撤销 ${s.revoked || 0}`;
    }
  } catch (e) { /* 忽略 */ }
}

/* ★R84 全局复制委托：改用 data-cp 属性，杜绝 onclick="cp('…')" 里 JS 字符串拼接被变量击穿 */
document.addEventListener("click", ev => {
  const t = ev.target && ev.target.closest ? ev.target.closest("[data-cp]") : null;
  if (t) cp(t.getAttribute("data-cp") || "");
});

/* ★R85 收尾：记录 / 启停 / 解封 也全改 data 属性 + 委托，后台页面内联 onclick 归零 */
document.addEventListener("click", ev => {
  const el = ev.target && ev.target.closest ? ev.target.closest("[data-logs],[data-tog],[data-unban]") : null;
  if (!el) return;
  const lg = el.getAttribute("data-logs");
  if (lg) return showLogsOf(lg);
  const tg = el.getAttribute("data-tog") || "";
  if (tg) { const p = tg.split("|"); return toggle(p[0] || "", p[1] || "disabled"); }
  const ub = el.getAttribute("data-unban");
  if (ub) return doUnban(ub);
});

function bootDl() {
  const tb = $("dlBody");
  if (tb) {
    tb.addEventListener("click", ev => {
      const b = ev.target.closest ? ev.target.closest("button[data-tok]") : null;
      if (b) revokeDl(b.getAttribute("data-tok") || "");
    });
  }
  onDlSource();
  loadDl();
}
