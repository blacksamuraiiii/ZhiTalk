/**
 * 智话通共享侧边栏
 * 功能：渲染菜单 + 折叠/展开 + 左右拖拽调宽 + localStorage 状态记忆
 * 使用：页面放 <div id="sidebar"></div>，在 </body> 前引入本脚本
 */
(function () {
  var MENUS = [
    { href: "/", label: "概览", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><rect x="2" y="2" width="7" height="7" rx="1.5"/><rect x="11" y="2" width="7" height="7" rx="1.5"/><rect x="2" y="11" width="7" height="7" rx="1.5"/><rect x="11" y="11" width="7" height="7" rx="1.5"/></svg>' },
    { href: "/freepbx", label: "FreePBX", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M3 4a1 1 0 011-1h12a1 1 0 011 1v2a1 1 0 01-1 1H4a1 1 0 01-1-1V4zm0 6a1 1 0 011-1h12a1 1 0 011 1v2a1 1 0 01-1 1H4a1 1 0 01-1-1v-2zm1 5a1 1 0 00-1 1v1a1 1 0 001 1h12a1 1 0 001-1v-1a1 1 0 00-1-1H4z"/></svg>' },
    { href: "/tftp", label: "TFTP 管理", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M4 3a2 2 0 00-2 2v10a2 2 0 002 2h12a2 2 0 002-2V7.414A2 2 0 0016.414 6L14 3.586A2 2 0 0012.586 3H4zm7 4a1 1 0 011-1h2a1 1 0 110 2h-2a1 1 0 01-1-1zm-3 3a1 1 0 011 1v2a1 1 0 11-2 0v-2a1 1 0 011-1z" clip-rule="evenodd"/></svg>' },
    { href: "/extensions", label: "分机管理", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M2 3.5A1.5 1.5 0 013.5 2h13A1.5 1.5 0 0118 3.5v13a1.5 1.5 0 01-1.5 1.5h-13A1.5 1.5 0 012 16.5v-13zM7 6a1 1 0 000 2h6a1 1 0 100-2H7zm0 4a1 1 0 100 2h6a1 1 0 100-2H7z"/></svg>' },
    { href: "/ai", label: "模型配置", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M10 2a1 1 0 011 1v1.5a1 1 0 11-2 0V3a1 1 0 011-1zm6.364 2.636a1 1 0 010 1.414l-1.06 1.06a1 1 0 11-1.415-1.414l1.06-1.06a1 1 0 011.415 0zM4.636 4.636a1 1 0 011.415 0l1.06 1.06A1 1 0 015.697 7.11l-1.06-1.06a1 1 0 010-1.414zM10 7a3 3 0 100 6 3 3 0 000-6zm-8 3a1 1 0 011-1h1.5a1 1 0 110 2H3a1 1 0 01-1-1zm13 0a1 1 0 011-1h1.5a1 1 0 110 2H16a1 1 0 01-1-1zm-6.364 3.536a1 1 0 011.414 0l1.06 1.06a1 1 0 11-1.414 1.415l-1.06-1.06a1 1 0 010-1.415zM6.11 12.95a1 1 0 010 1.415l-1.06 1.06a1 1 0 01-1.414-1.414l1.06-1.06a1 1 0 011.414 0zM10 16a1 1 0 011 1v1.5a1 1 0 11-2 0V17a1 1 0 011-1z"/></svg>' },
    { href: "/llama-server", label: "llama-server", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><circle cx="10" cy="10" r="3"/><path d="M10 1a9 9 0 100 18 9 9 0 000-18zm0 2a7 7 0 110 14 7 7 0 010-14z"/></svg>' },
    { href: "/callback", label: "通话测试", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M2 3.5A1.5 1.5 0 013.5 2h1.745a1.5 1.5 0 011.417.985l1.182 3.2a1.5 1.5 0 01-.48 1.666l-1.099.862a1.5 1.5 0 00-.38 1.643l1.696 4.636a1.5 1.5 0 001.643 1.017l2.182-.273a1.5 1.5 0 001.243-.82l1.42-2.673a1.5 1.5 0 00-.184-1.73l-.972-1.041a1.5 1.5 0 01-.336-1.3l.636-2.543a1.5 1.5 0 011.09-1.048l2.753-.688a1.5 1.5 0 011.764 1.31 13.911 13.911 0 01-3.5 10.936 13.911 13.911 0 01-10.936 3.5A1.5 1.5 0 012 16.755V3.5z"/></svg>' },
    { href: "/schedule", label: "定时任务", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M10 18a8 8 0 100-16 8 8 0 000 16zm1-13a1 1 0 10-2 0v4a1 1 0 00.293.707l3 3a1 1 0 001.414-1.414L11 8.586V5z"/></svg>' },
    { href: "/logs", label: "通话日志", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M9 2a1 1 0 000 2h2a1 1 0 100-2H9zM4 5a2 2 0 012-2h1.5a2.5 2.5 0 015 0H14a2 2 0 012 2v1a2 2 0 01-2 2H6a2 2 0 01-2-2V5zm10 5a1 1 0 011 1v3a2 2 0 01-2 2H7a2 2 0 01-2-2v-3a1 1 0 112 0v3h6v-3a1 1 0 011-1z"/></svg>' },
    { href: "/metrics", label: "通话指标", icon: '<svg viewBox="0 0 20 20" width="20" height="20" fill="currentColor"><path d="M2 11a1 1 0 011-1h2a1 1 0 011 1v5a1 1 0 01-1 1H3a1 1 0 01-1-1v-5zm6-4a1 1 0 011-1h2a1 1 0 011 1v9a1 1 0 01-1 1H9a1 1 0 01-1-1V7zm6-3a1 1 0 011-1h2a1 1 0 011 1v12a1 1 0 01-1 1h-2a1 1 0 01-1-1V4z"/></svg>' },
  ];

  var LS_KEY_W = "sidebar_width";
  var LS_KEY_C = "sidebar_collapsed";
  var DEFAULT_W = 160;
  var MIN_W = 60;
  var MAX_W = 400;

  var sidebar = document.getElementById("sidebar");
  if (!sidebar) return;

  // ── 注入 CSS ──
  var style = document.createElement("style");
  style.textContent =
    "#sidebar{" +
    "position:relative;background:#1e293b;border-right:1px solid #334155;" +
    "overflow:hidden;flex-shrink:0;transition:width .2s ease;" +
    "}" +
    "#sidebar .sidebar-title{" +
    "display:flex;align-items:center;justify-content:space-between;" +
    "font-size:16px;font-weight:700;padding:14px 16px 20px;" +
    "color:#f1f5f9;border-bottom:1px solid #334155;margin-bottom:10px;" +
    "white-space:nowrap;" +
    "}" +
    "#sidebar.collapsed .sidebar-title{padding:14px 8px 20px;justify-content:center}" +
    "#sidebar .title-text{overflow:hidden;white-space:nowrap}" +
    "#sidebar.collapsed .title-text{display:none}" +
    "#sidebar .collapse-btn{" +
    "background:none;border:none;color:#94a3b8;cursor:pointer;font-size:18px;" +
    "padding:0 4px;line-height:1;flex-shrink:0;" +
    "}" +
    "#sidebar .collapse-btn:hover{color:#e2e8f0}" +
    "#sidebar .nav-item{" +
    "display:flex;align-items:center;gap:10px;" +
    "padding:10px 16px;color:#94a3b8;text-decoration:none;font-size:14px;" +
    "white-space:nowrap;position:relative;" +
    "}" +
    "#sidebar.collapsed .nav-item{padding:10px 0;justify-content:center;gap:0}" +
    "#sidebar .nav-item:hover{background:#334155;color:#e2e8f0}" +
    "#sidebar .nav-item.active{background:#0ea5e9;color:#fff;font-weight:600}" +
    "#sidebar .nav-icon{font-size:18px;flex-shrink:0;width:24px;text-align:center;display:flex;align-items:center;justify-content:center}" +
    "#sidebar.collapsed .nav-icon{font-size:20px;width:auto}" +
    "#sidebar .nav-label{overflow:hidden}" +
    "#sidebar.collapsed .nav-label{display:none}" +
    "#sidebar.collapsed .nav-item:hover::after{" +
    "content:attr(data-tooltip);" +
    "position:absolute;left:62px;top:50%;transform:translateY(-50%);" +
    "background:#334155;color:#e2e8f0;padding:4px 10px;border-radius:6px;" +
    "font-size:12px;white-space:nowrap;z-index:300;pointer-events:none;" +
    "}" +
    "#sidebar .resize-handle{" +
    "position:absolute;top:0;right:0;width:4px;height:100%;cursor:col-resize;z-index:10;" +
    "}" +
    "#sidebar .resize-handle:hover{background:rgba(14,165,233,0.4)}" +
    "";
  document.head.appendChild(style);

  // ── 渲染 HTML ──
  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  var html = '<div class="sidebar-title">';
  html += '<span class="title-text">智话通</span>';
  html += '<button class="collapse-btn" id="collapseBtn">«</button>';
  html += "</div>";

  var curPath = window.location.pathname;
  for (var i = 0; i < MENUS.length; i++) {
    var m = MENUS[i];
    var isActive =
      curPath === m.href || (m.href !== "/" && curPath.indexOf(m.href) === 0);
    html +=
      '<a href="' + m.href + '" class="nav-item' + (isActive ? " active" : "") +
      '" data-tooltip="' + esc(m.label) + '">';
    html += '<span class="nav-icon">' + m.icon + "</span>";
    html += '<span class="nav-label">' + esc(m.label) + "</span>";
    html += "</a>";
  }
  html += '<div class="resize-handle" id="resizeHandle"></div>';
  sidebar.innerHTML = html;

  // ── 状态 ──
  var curWidth = DEFAULT_W; // 展开状态宽度
  var storedW = parseInt(localStorage.getItem(LS_KEY_W), 10);
  if (storedW >= MIN_W && storedW <= MAX_W) curWidth = storedW;

  function applyWidth(v) {
    curWidth = v;
    if (!sidebar.classList.contains("collapsed")) {
      sidebar.style.width = v + "px";
    }
  }

  function setCollapsed(flag) {
    var btn = document.getElementById("collapseBtn");
    if (flag) {
      sidebar.classList.add("collapsed");
      sidebar.style.width = MIN_W + "px";
      btn.textContent = "»";
    } else {
      sidebar.classList.remove("collapsed");
      sidebar.style.width = curWidth + "px";
      btn.textContent = "«";
    }
  }

  // 恢复初始状态
  applyWidth(curWidth);
  if (localStorage.getItem(LS_KEY_C) === "1") setCollapsed(true);

  // ── 折叠按钮 ──
  document.getElementById("collapseBtn").addEventListener("click", function () {
    var isCollapsed = sidebar.classList.contains("collapsed");
    setCollapsed(!isCollapsed);
    localStorage.setItem(LS_KEY_C, isCollapsed ? "0" : "1");
  });

  // ── 拖拽调宽 ──
  var handle = document.getElementById("resizeHandle");
  var dragging = false;
  var startX = 0;
  var startW = 0;
  handle.addEventListener("mousedown", function (e) {
    if (sidebar.classList.contains("collapsed")) return;
    dragging = true;
    startX = e.clientX;
    startW = sidebar.offsetWidth;
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    e.preventDefault();
  });
  document.addEventListener("mousemove", function (e) {
    if (!dragging) return;
    var newW = startW + (e.clientX - startX);
    newW = Math.max(MIN_W, Math.min(MAX_W, newW));
    applyWidth(newW);
  });
  document.addEventListener("mouseup", function () {
    if (!dragging) return;
    dragging = false;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
    localStorage.setItem(LS_KEY_W, String(curWidth));
  });
})();
