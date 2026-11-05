/* SSD助手 —— 前端 */
(function () {
  "use strict";

  var API = "";
  var state = {
    page: "overview",
    expandedSet: {},
    overview: null,
    events: null,
    history: null,
    bench: null,
    selected: null,       // 概况相关：多选中的设备
    historyDevice: null,  // 历史页当前选中的硬盘
    historyYear: null,    // 历史页当前查看的年份
    benchDevice: null,
    benchGauges: null,
    benchPlayer: null,   // { pending: [], shown: {read,write}, playing: false }
    timer: null,
    benchTimer: null
  };

  /* ------------------------------------------------------------ 工具 */

  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, html) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (html !== undefined) node.innerHTML = html;
    return node;
  }
  function esc(str) {
    return String(str === null || str === undefined ? "" : str)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function getJSON(path) {
    return fetch(API + path, { headers: { Accept: "application/json" } }).then(function (res) {
      if (!res.ok) throw new Error(path + " -> " + res.status);
      return res.json();
    });
  }
  function postJSON(path, body) {
    return fetch(API + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (res) { return res.json(); });
  }

  function fmtSize(bytes, digits) {
    if (!bytes) return "0GB";
    var units = ["B", "KB", "MB", "GB", "TB", "PB"];
    var i = 0, v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    if (digits === undefined) digits = v >= 10 ? 0 : 1;
    return v.toFixed(digits) + units[i];
  }
  function fmtTraffic(tb) {
    if (tb >= 1) return (tb >= 10 ? tb.toFixed(0) : tb.toFixed(1)) + "T";
    var gb = tb * 1024;
    if (gb >= 1) return (gb >= 10 ? gb.toFixed(0) : gb.toFixed(1)) + "G";
    return (gb * 1024).toFixed(0) + "M";
  }

  function fmtTemp(v) { return (v === null || v === undefined || v === 0) ? "—" : v + "℃"; }
  function fmtHealth(v) { return (v === null || v === undefined) ? "—" : v + "%"; }
  function fmtHours(v) { return (v === null || v === undefined) ? "—" : v + "小时"; }
  function fmtCount(v, unit) { return (v === null || v === undefined) ? "—" : v + (unit || ""); }
  /* 自动备份按"上次备份 + 间隔"轮转，这里算出下一次大概什么时候跑 */
  function fmtNextBackup(cfg) {
    if (!cfg.backup_enabled) return "自动备份已关闭";
    var days = Number(cfg.backup_interval_days) || 1;
    if (!cfg.backup_last_at) {
      var soon = new Date(Date.now() + 60 * 1000);
      return "安装后首次采样时执行（" + soon.getFullYear() + "年" + (soon.getMonth() + 1) + "月" +
        soon.getDate() + "日 " + pad2(soon.getHours()) + ":" + pad2(soon.getMinutes()) + " 起）";
    }
    var next = (Number(cfg.backup_last_at) + days * 86400) * 1000;
    var delta = Math.round((next - Date.now()) / 60000);   // 分钟
    var when = fmtTime(Math.floor(next / 1000));
    if (delta <= 0) return when + "（下次采样时执行）";
    if (delta < 60) return when + "（约 " + delta + " 分钟后）";
    if (delta < 60 * 48) return when + "（约 " + Math.round(delta / 60) + " 小时后）";
    return when + "（约 " + Math.round(delta / 1440) + " 天后）";
  }

  function pad2(n) { return n < 10 ? "0" + n : "" + n; }

  function fmtTime(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return d.getFullYear() + "年" + (d.getMonth() + 1) + "月" + d.getDate() + "日 " +
      p(d.getHours()) + ":" + p(d.getMinutes());
  }
  /* 手机端事件列表空间窄，只显示「11-05 09:12」 */
  function fmtTimeBrief(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
      p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function fmtDateTimeShort(ts) {
    if (!ts) return "—";
    var d = new Date(ts * 1000);
    function p(n) { return n < 10 ? "0" + n : "" + n; }
    return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " +
      p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function toast(msg) {
    var node = $("#toast");
    node.textContent = msg;
    node.classList.add("show");
    clearTimeout(node._t);
    node._t = setTimeout(function () { node.classList.remove("show"); }, 2200);
  }

  var ICONS = {
    disk: '<svg viewBox="0 0 20 20"><rect x="1.5" y="6.2" width="14.2" height="7.6" rx="1.8"/><path d="M15.1 8.2v3.6M16.5 8.2v3.6"/></svg>',
    count: '<svg viewBox="0 0 20 20"><rect x="3" y="4" width="14" height="12" rx="2.2"/><path d="M3 8.4h14"/></svg>',
    capacity: '<svg viewBox="0 0 20 20"><ellipse cx="10" cy="6" rx="6.4" ry="2.6"/><path d="M3.6 6v8c0 1.4 2.9 2.6 6.4 2.6s6.4-1.2 6.4-2.6V6"/></svg>',
    chevron: '<svg viewBox="0 0 20 20"><path d="M8 5l5 5-5 5"/></svg>',
    chevronDown: '<svg viewBox="0 0 20 20"><path d="M5 8l5 5 5-5"/></svg>',
    write: '<svg viewBox="0 0 20 20"><path d="M10 3.4v10.4M6.4 10.4L10 14l3.6-3.6M3.6 16.4h12.8"/></svg>',
    chart: '<svg viewBox="0 0 20 20"><path d="M3 16.5V9.5M7.7 16.5V5.5M12.3 16.5v-4M17 16.5V3.5"/></svg>',
    alert: '<svg viewBox="0 0 20 20"><path d="M10 3.4 18 16.6H2z"/><path d="M10 8.2v3.4M10 14.2v.2"/></svg>',
    speed: '<svg viewBox="0 0 20 20"><path d="M3.2 14.6a8 8 0 1 1 13.6 0"/><path d="M10 12.2l3.4-3.6"/><circle cx="10" cy="12.6" r="1.3"/></svg>'
  };

  /* ------------------------------------------------------------ 选择器 */

  function modelOf(device) {
    var list = (state.overview && state.overview.disks) || [];
    for (var i = 0; i < list.length; i += 1) {
      if (list[i].device === device) return list[i].model || device;
    }
    return device;
  }

  function mountSelector(host, opts) {
    var disks = opts.disks || [];
    var selected = opts.selected || [];
    var single = !!opts.single;
    var wrap = el("div", "selector");
    var label;
    if (single) {
      label = selected.length ? modelOf(selected[0]) : "请选择硬盘";
    } else {
      label = selected.length === disks.length ? "全部硬盘"
        : (selected.length ? selected.map(modelOf).join("、") : "请选择硬盘");
    }
    wrap.innerHTML =
      '<button class="selector-btn" type="button"><span class="label">' + esc(label) +
      '</span><span class="caret">▾</span></button><div class="selector-panel"></div>';
    var panel = $(".selector-panel", wrap);

    disks.forEach(function (d) {
      var item = el("label", "opt");
      var checked = selected.indexOf(d.device) >= 0;
      item.innerHTML = '<input type="' + (single ? "radio" : "checkbox") + '"' +
        (checked ? " checked" : "") + '><span class="name">' + esc(d.model || d.device) +
        '</span><span class="dev">' + esc(d.device) + '</span>';
      var input = $("input", item);
      input.addEventListener("change", function () {
        var next;
        if (single) {
          next = [d.device];
          panel.classList.remove("open");
        } else {
          next = selected.slice();
          var idx = next.indexOf(d.device);
          if (input.checked && idx < 0) next.push(d.device);
          if (!input.checked && idx >= 0) next.splice(idx, 1);
          if (!next.length) { input.checked = true; return; }
        }
        opts.onChange(next);
      });
      panel.appendChild(item);
    });

    $(".selector-btn", wrap).addEventListener("click", function (e) {
      e.stopPropagation();
      var open = panel.classList.contains("open");
      document.querySelectorAll(".selector-panel.open").forEach(function (p) { p.classList.remove("open"); });
      if (!open) panel.classList.add("open");
    });
    host.appendChild(wrap);
  }

  /* ------------------------------------------------------------ 概况页 */

  function statusOrb(state) {
    var glyph = state === "ok"
      ? '<path d="M5.4 11.3l3.7 3.7 7.3-7.6"/>'
      : state === "empty"
        ? '<path d="M6.4 11h9.2"/>'
        : '<path d="M11 5.6v6.6M11 15.4v.2"/>';
    return '<span class="status-orb"><svg viewBox="0 0 22 22" aria-hidden="true">' + glyph + '</svg></span>';
  }

  function fmtSizeRaw(bytes) {
    if (!bytes) return "0";
    var units = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0, v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return v >= 100 ? String(Math.round(v)) : (Math.round(v * 10) / 10).toString();
  }

  function fmtSizeUnit(bytes) {
    if (!bytes) return "B";
    var units = ["B", "KB", "MB", "GB", "TB", "PB"], i = 0, v = bytes;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return units[i];
  }

  function factRow(label, value, desc, extraClass) {
    return '<div class="fact' + (extraClass ? " " + extraClass : "") + '">' +
      '<span class="fact-label">' + esc(label) + '</span>' +
      '<span class="fact-value">' + (value === null || value === undefined || value === "" ? "—" : value) + '</span>' +
      (desc ? '<span class="fact-desc">' + esc(desc) + '</span>' : '') + '</div>';
  }

  function fmtMinutes(v) {
    if (v === null || v === undefined) return null;
    if (v >= 60) return Math.floor(v / 60) + " 小时 " + (v % 60) + " 分";
    return v + " 分钟";
  }

  function fmtInt(v, unit) {
    if (v === null || v === undefined) return null;
    return v + (unit || "");
  }

  function fmtLifeDays(days) {
    if (days === null || days === undefined) return "—";
    if (days >= 365 * 30) return "30 年以上";
    if (days >= 365) return Math.floor(days / 365) + " 年 " + Math.floor((days % 365) / 30) + " 个月";
    if (days >= 30) return Math.floor(days / 30) + " 个月";
    return days + " 天";
  }

  function lifeBand(d) {
    var life = d.life || {};
    var main, sub, pct = null, tone = "";
    if (life.status === "ok") {
      main = "预计还可使用 " + fmtLifeDays(life.remaining_days);
      sub = "每年消耗约 " + life.per_year_pct + "% 寿命" +
        (life.write_per_day ? " · 日均写入 " + fmtSize(life.write_per_day) : "");
      pct = life.used_pct;
      if (life.remaining_days !== null && life.remaining_days < 365) tone = " is-warn";
    } else if (life.status === "flat") {
      main = "暂未观测到寿命消耗";
      sub = "已用寿命 " + (life.used_pct === null || life.used_pct === undefined ? "—" : life.used_pct + "%") +
        " · 已记录 " + (life.span_days || 0) + " 天";
      pct = life.used_pct;
    } else {
      main = "需要更多数据才能预估";
      sub = life.note || "持续采集几天后即可估算剩余寿命";
      pct = null;
    }
    var track = "";
    if (pct !== null && pct !== undefined && pct > 0) {
      var w = Math.max(2, Math.min(100, pct));
      track = '<div class="life-track"><i style="width:' + w + '%"></i></div>';
    }
    return '<div class="life-band' + tone + '">' +
      '<div class="life-text"><span class="life-label">寿命预估</span>' +
      '<b>' + esc(main) + '</b><span class="life-sub">' + esc(sub) + '</span></div>' +
      track + '</div>';
  }

  function detailSection(title, rows) {
    if (!rows.length) return "";
    return '<section class="detail-block"><h4>' + esc(title) + '</h4>' +
      '<div class="fact-grid">' + rows.join("") + '</div></section>';
  }

  function diskDetail(d) {
    if (!d.smart_ok) {
      return lifeBand(d) +
        '<div class="detail-empty">该硬盘未提供 SMART 健康信息：' +
        esc(d.smart_error || "可能通过 USB 桥接或阵列卡接入，被系统屏蔽了底层 SMART 透传") + '</div>';
    }
    var life = [], run = [], rel = [], base = [];

    life.push(factRow("剩余寿命", fmtHealth(d.health), "厂商预估剩余寿命"));
    life.push(factRow("已用寿命", d.percent_used === null || d.percent_used === undefined ? null : d.percent_used + "%",
      "NVMe 已消耗寿命占比"));
    life.push(factRow("累计写入", fmtSize(d.tbw), "出厂以来写入总量"));
    life.push(factRow("累计读取", fmtSize(d.tbr), "出厂以来读取总量"));

    run.push(factRow("当前温度", fmtTemp(d.temp), "主控实时温度"));
    run.push(factRow("通电时长", fmtHours(d.power_on_hours), "累计上电时间"));
    run.push(factRow("通电次数", fmtInt(d.power_cycles, " 次"), "累计开关机次数"));
    run.push(factRow("异常断电", fmtInt(d.unsafe_shutdowns, " 次"), "非正常掉电次数"));

    rel.push(factRow("介质错误", fmtInt(d.media_errors, " 次"), "无法纠正的错误数",
      d.media_errors > 0 ? "is-warn" : ""));
    if (d.available_spare !== null && d.available_spare !== undefined) {
      rel.push(factRow("可用备用块", d.available_spare + "%", "预留空间剩余比例"));
    }
    if (d.num_err_log_entries !== null && d.num_err_log_entries !== undefined) {
      rel.push(factRow("错误日志条目", fmtInt(d.num_err_log_entries, " 条"), "主控异常事件总数"));
    }
    if (d.critical_warning !== null && d.critical_warning !== undefined) {
      rel.push(factRow("关键警告", "0x" + ("0" + d.critical_warning.toString(16)).slice(-2),
        "0x00 即全部正常"));
    }
    if (d.reallocated_sectors !== null && d.reallocated_sectors !== undefined) {
      rel.push(factRow("重映射扇区", fmtInt(d.reallocated_sectors), "已替换的坏扇区"));
      rel.push(factRow("待映射扇区", fmtInt(d.pending_sectors), "待替换的疑似坏扇区"));
      rel.push(factRow("不可纠正扇区", fmtInt(d.uncorrectable_sectors), "无法纠正的扇区数"));
      rel.push(factRow("接口 CRC 错误", fmtInt(d.crc_errors), "接口传输错误"));
    }

    base.push(factRow("型号", esc(d.model), "厂商型号标识"));
    base.push(factRow("序列号", esc(d.serial) || "—", "厂商唯一标识"));
    base.push(factRow("固件版本", esc(d.firmware) || "—", "可升级修复问题"));
    base.push(factRow("容量", fmtSize(d.capacity, 1), "标称可用容量"));

    return lifeBand(d) +
      '<div class="detail-sections">' +
      detailSection("寿命与写入量", life) +
      detailSection("运行状态", run) +
      detailSection("可靠性", rel) +
      detailSection("设备信息", base) +
      '</div>';
  }

  function diskCard(d) {
    var open = !!state.expandedSet[d.device];
    var tone = d.status === "warn" ? " warn" : (d.status === "bad" ? " bad" : "");
    var healthTone = d.status === "ok" ? "ok" : (d.status === "warn" ? "warn" : (d.status === "bad" ? "bad" : "unknown"));
    var statusText = d.status === "ok" ? "健康" : (d.status === "warn" ? "需关注" : (d.status === "bad" ? "异常" : "未知"));

    var html = '<article class="disk-card' + tone + '" data-device="' + esc(d.device) + '">';
    html += '<div class="disk-main">';
    html += '<div class="disk-id">';
    html += '<span class="disk-icon">' + ICONS.disk + '</span>';
    html += '<div class="disk-titles"><div class="disk-line">' +
      '<span class="disk-name" title="' + esc(d.model) + '">' + esc(d.model) + '</span>' +
      (d.protocol ? '<span class="proto">' + esc(d.protocol) + '</span>' : '') +
      '</div><div class="disk-sub"><span>' + esc(d.device) + '</span>' +
      '</div></div>';
    html += '</div>';

    html += '<div class="disk-facts">';
    html += '<div class="fact-mini"><span>健康状态</span><b class="v-' + healthTone + '">' +
      (d.health === null || d.health === undefined ? "—" : d.health + "%") + '</b></div>';
    html += '<div class="fact-mini"><span>温度</span><b>' + fmtTemp(d.temp) + '</b></div>';
    html += '<div class="fact-mini"><span>累计写入</span><b>' + (d.smart_ok ? fmtSize(d.tbw) : "—") + '</b></div>';
    html += '<div class="fact-mini"><span>累计读取</span><b>' + (d.smart_ok ? fmtSize(d.tbr) : "—") + '</b></div>';
    html += '</div></div>';

    html += '<div class="disk-detail' + (open ? "" : " is-hidden") + '">' + diskDetail(d) + '</div>';
    html += '</article>';
    return html;
  }

  function eventsCard() {
    var items = state.events || [];
    var html = '<section class="card events-card"><div class="chart-head"><h2>最近事件</h2>' +
      '<div class="chart-tools"><span class="chip">' +
      (items.length ? "最近 " + Math.min(items.length, 12) + " 条" : "暂无记录") + '</span>' +
      (items.length ? '<button class="btn btn-sm" id="btn-clear-events" type="button">清空</button>' : '') +
      '</div></div>';
    if (!items.length) {
      html += '<div class="events-empty">还没有告警或变化记录。健康度下降、温度过高、' +
        '介质增加、异常断电等情况会自动记录在这里。</div>';
    } else {
      html += '<ul class="event-list">';
      items.slice(0, 12).forEach(function (e) {
        var eName = e.model || e.device || "";
        html += '<li class="ev ev-' + esc(e.level || "info") + '">' +
          '<i class="ev-dot"></i><span class="ev-time">' +
          (window.innerWidth <= 720 ? fmtTimeBrief(e.ts) : fmtTime(e.ts)) + '</span>' +
          '<span class="ev-name" title="' + esc(eName) + '">' + esc(eName) + '</span>' +
          '<span class="ev-msg">' + esc(e.message || "") + '</span></li>';
      });
      html += '</ul>';
    }
    return html + '</section>';
  }

  function renderOverview() {
    var data = state.overview;
    var view = $("#view");
    if (!data) { view.innerHTML = '<div class="loading">正在采集磁盘信息…</div>'; return; }

    var s = data.summary;
    var tone = s.state === "ok" ? "ok" : (s.state === "warn" ? "warn" : (s.state === "bad" ? "bad" : "empty"));

    // 概况顶部只留一条运行状态，具体数字在下面的每块盘里看
    var html = '<section class="card status-card' + (tone === "ok" ? "" : " " + tone) + '">' +
      '<div class="status-main">' +
      statusOrb(tone) +
      '<div class="status-text"><h2>' + esc(tone === "ok" ? "SSD助手运行正常" : s.title) + '</h2>' +
      '<p>' + esc(s.desc) + '</p></div>' +
      '<button class="btn btn-sm status-btn" type="button" id="go-settings">助手设置</button>' +
      '</div></section>';

    html += '<section class="disk-list">';
    if (!data.disks.length) {
      html += '<div class="card"><div class="empty">尚未识别到固态硬盘</div></div>';
    } else {
      data.disks.forEach(function (d) { html += diskCard(d); });
    }
    html += '</section>';
    html += eventsCard();

    view.innerHTML = html;
    view.dataset.page = "overview";

    view.querySelectorAll(".disk-card").forEach(function (card) {
      var head = card.querySelector(".disk-main");
      if (!head) return;
      head.addEventListener("click", function () {
        // 正在框选文字时不触发折叠，方便复制
        var selection = window.getSelection();
        if (selection && selection.toString().trim().length > 0) return;

        var device = card.getAttribute("data-device");
        var detail = card.querySelector(".disk-detail");
        if (!detail) return;
        var open = !detail.classList.toggle("is-hidden");
        card.classList.toggle("is-open", open);
        if (open) { state.expandedSet[device] = true; } else { delete state.expandedSet[device]; }
      });
    });

    var clearEvents = $("#btn-clear-events");
    if (clearEvents) {
      clearEvents.addEventListener("click", function () {
        if (!window.confirm("确认清空全部事件记录？")) return;
        postJSON("api/events/clear", {}).then(function (res) {
          toast(res && res.removed ? "已清空 " + res.removed + " 条记录" : "已清空");
          loadEvents();
        }).catch(function () { toast("清空失败"); });
      });
    }

    var go = $("#go-settings");
    if (go) go.addEventListener("click", function () { location.hash = "#/about"; });
  }

  /* ------------------------------------------------------------ 历史页 */

  function emptyHeatmap() {
    var cols = 53, rows = 7, cell = 11, gap = 4;
    var w = cols * (cell + gap) - gap, h = rows * (cell + gap) - gap;
    var seed = 20261104;
    function rnd() {
      seed = (seed * 9301 + 49297) % 233280;
      return seed / 233280;
    }
    var rects = "";
    for (var c = 0; c < cols; c += 1) {
      for (var r = 0; r < rows; r += 1) {
        var v = rnd();
        var op = v > 0.82 ? 0.30 : v > 0.6 ? 0.18 : v > 0.32 ? 0.10 : 0.045;
        rects += '<rect x="' + (c * (cell + gap)) + '" y="' + (r * (cell + gap)) +
          '" width="' + cell + '" height="' + cell + '" rx="3" fill="#2f6feb" opacity="' + op + '"/>';
      }
    }
    return '<svg viewBox="0 0 ' + w + ' ' + h + '" width="' + w + '" height="' + h + '">' + rects + '</svg>';
  }

  function historyEmpty() {
    return '<section class="card empty-hero">' +
      '<div class="empty-heat">' + emptyHeatmap() + '</div>' +
      '<h3>还没有选择硬盘</h3>' +
      '<p>选择一块硬盘，查看它这一年里每天的读写量与温度分布</p>' +
      '<button class="btn btn-primary" type="button" id="btn-pick-disk">选择硬盘</button>' +
      '</section>';
  }

  function mountYearSwitcher(years, current, onChange) {
    var host = $("#topbar-right");
    if (!host || !years.length) return;

    var earlier = years.filter(function (y) { return y < current; }).sort(function (a, b) { return b - a; })[0];
    var later = years.filter(function (y) { return y > current; }).sort(function (a, b) { return a - b; })[0];

    var wrap = el("div", "year-switch");

    var prev = el("button", "year-btn", "‹");
    prev.type = "button";
    prev.disabled = !earlier;
    prev.title = earlier ? earlier + " 年" : "没有更早的数据";
    if (earlier) prev.addEventListener("click", function () { onChange(earlier); });

    var label = el("span", "year-label", current + " 年");

    var next = el("button", "year-btn", "›");
    next.type = "button";
    next.disabled = !later;
    next.title = later ? later + " 年" : "没有更新的数据";
    if (later) next.addEventListener("click", function () { onChange(later); });

    wrap.appendChild(prev);
    wrap.appendChild(label);
    wrap.appendChild(next);
    host.appendChild(wrap);
  }

  function mountExportButton(device) {
    var host = $("#topbar-right");
    if (!host || !device) return;   // 没选盘就没有可导出的数据，按钮不出现
    var btn = el("button", "btn", "导出 JSON");
    btn.type = "button";
    btn.disabled = !device;
    btn.title = device ? "导出该硬盘的全部采样记录（JSON，可用于日后导入）" : "请先选择硬盘";
    btn.addEventListener("click", function () {
      var dev = state.historyDevice;
      if (!dev) { toast("请先选择硬盘"); return; }
      var link = document.createElement("a");
      link.href = "api/export?device=" + encodeURIComponent(dev);
      link.download = "";
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      toast("正在导出 " + dev + " 的记录（JSON）…");
    });
    host.appendChild(btn);
  }

  /* -------------------------------------------------- 热力图悬停：当天明细 */
  var dayCache = {};
  var dayTipSeq = 0;

  function fmtRate(v) {
    v = Number(v) || 0;
    if (!v) return "0";
    var mb = v / 1048576;
    if (mb >= 1) return (mb >= 100 ? Math.round(mb) : Math.round(mb * 10) / 10) + " MB/s";
    return Math.round(v / 1024) + " KB/s";
  }

  function loadDay(device, date) {
    var key = device + "|" + date;
    if (dayCache[key]) return Promise.resolve(dayCache[key]);
    return getJSON("api/day?device=" + encodeURIComponent(device) + "&date=" + date)
      .then(function (res) { dayCache[key] = res || { points: [], total: 0 }; return dayCache[key]; })
      .catch(function () { return { points: [], total: 0 }; });
  }

  function dayTipHtml(data, metric, date) {
    var pts = (data && data.points) || [];
    if (!pts.length) {
      return '<div class="tip-day"><div class="tip-empty">这一天没有采样记录</div></div>';
    }
    var field = metric === "read" ? "read" : (metric === "write" ? "write" : "temp");
    var label = metric === "read" ? "读取速率" : (metric === "write" ? "写入速率" : "温度");

    var start = new Date(date + "T00:00:00").getTime() / 1000;
    var vals = [];
    pts.forEach(function (p) {
      var v = p[field];
      if (typeof v === "number" && v > 0) vals.push({ ts: p.ts, v: v });
    });
    if (!vals.length) {
      return '<div class="tip-day"><div class="tip-empty">这一天没有' + label + '记录</div></div>';
    }

    var W = 232, H = 46;
    var raw = vals.map(function (o) { return o.v; }).sort(function (a, b) { return a - b; });
    var max = raw[raw.length - 1];
    var min = metric === "temp" ? raw[0] - 2 : 0;
    // 速率图用 98% 分位当纵轴上限：一次极端尖峰不会把日常波动压成一条直线，
    // 超过上限的点贴顶显示（仍然是最高点，只是不再拉扁整张图）。
    var top = metric === "temp" ? max : Math.max(
      raw[Math.min(raw.length - 1, Math.floor(raw.length * 0.95))], 1);
    if (top <= min) top = min + 1;

    var line = "", area = "";
    vals.forEach(function (o, i) {
      var x = Math.min(W, Math.max(0, (o.ts - start) / 86400 * W));
      var v = Math.min(o.v, top);
      var y = H - 3 - (v - min) / (top - min) * (H - 8);
      line += (i ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    });
    area = line + "L" + W + " " + H + "L0 " + H + "Z";

    var peak = vals.reduce(function (a, b) { return b.v > a.v ? b : a; }, vals[0]);
    var pk = new Date(peak.ts * 1000);
    var peakTime = ("0" + pk.getHours()).slice(-2) + ":" + ("0" + pk.getMinutes()).slice(-2);
    var peakVal = metric === "temp" ? Math.round(peak.v) + "℃" : fmtRate(peak.v);

    var total = data.total || pts.length;
    return '<div class="tip-day">' +
      '<div class="tip-day-head"><span>' + esc(label) + ' · 24 小时</span>' +
      '<em>共 ' + total.toLocaleString("zh-CN") + ' 条</em></div>' +
      '<svg class="tip-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
      '<path d="' + area + '" fill="' + (metric === "temp" ? "rgba(239,156,58,.16)" : "rgba(47,111,235,.13)") + '"/>' +
      '<path d="' + line + '" fill="none" stroke="' + (metric === "temp" ? "#ef9c3a" : "#2f6feb") +
      '" stroke-width="1.4" stroke-linejoin="round"/></svg>' +
      '<div class="tip-axis"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>' +
      '<div class="tip-peak">峰值 <b>' + peakTime + "</b> · " + peakVal + '</div>' +
      '</div>';
  }

  function showDayTip(tip, item, device, metric) {
    var seq = ++dayTipSeq;
    var slot = document.createElement("div");
    slot.className = "tip-day";
    slot.innerHTML = '<div class="tip-load">正在读取当天明细…</div>';
    tip.appendChild(slot);
    return new Promise(function (resolve) {
      setTimeout(function () {
        if (seq !== dayTipSeq) { resolve(); return; }
        loadDay(device, item.date || item.d).then(function (data) {
          if (seq !== dayTipSeq) { resolve(); return; }
          slot.outerHTML = dayTipHtml(data, metric, item.date || item.d);
          resolve();
        });
      }, 90);
    });
  }

  function historyCharts(data) {
    var daily = (data.daily || []).filter(function (row) {
      return row.date && Number(row.date.slice(0, 4)) === state.historyYear;
    });
    var weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];

    function tipFor(row) {
      var parts = row.date.split("-");
      var d = new Date(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
      var lines = ["<b>" + parts[0] + "年" + Number(parts[1]) + "月" + Number(parts[2]) + "日</b> " +
        weekNames[d.getDay()]];
      lines.push("读取 " + (row.read ? fmtSize(row.read) : "0"));
      lines.push("写入 " + (row.write ? fmtSize(row.write) : "0"));
      lines.push("温度 " + (row.temp === null || row.temp === undefined ? "—" : row.temp + "℃"));
      return lines.join("<br>");
    }

    function buildItems(field) {
      return daily
        .filter(function (row) { return typeof row[field] === "number" && row[field] !== null; })
        .map(function (row) { return { date: row.date, value: row[field], tip: tipFor(row) }; });
    }

    var year = state.historyYear;

    var dev = data.device || state.historyDevice;
    Charts.heatmap($("#chart-read"), {
      year: year,
      items: buildItems("read"),
      colors: ["#ebedf0", "#c6f0d8", "#7fd8a8", "#3fb37f", "#1f7a55"],
      onTip: function (tip, item) { return showDayTip(tip, item, dev, "read"); }
    });

    Charts.heatmap($("#chart-write"), {
      year: year,
      items: buildItems("write"),
      colors: ["#ebedf0", "#c9dcff", "#8fb6ff", "#4b83f0", "#1e4fbf"],
      onTip: function (tip, item) { return showDayTip(tip, item, dev, "write"); }
    });

    Charts.heatmap($("#chart-temp"), {
      year: year,
      items: buildItems("temp"),
      colors: ["#ebedf0", "#fde2b3", "#f7c46c", "#ef9c3a", "#d9661f"],
      onTip: function (tip, item) { return showDayTip(tip, item, dev, "temp"); }
    });
  }

  /* 历史页顶部的硬盘基本信息条 */
  function diskBrief(d) {
    var statusTone = d.status === "ok" ? "ok" : (d.status === "warn" ? "warn" : (d.status === "bad" ? "bad" : "unknown"));
    var statusText = d.status === "ok" ? "健康" : (d.status === "warn" ? "需关注" : (d.status === "bad" ? "异常" : "未知"));

    return '<section class="card disk-brief">' +
      '<div class="brief-top">' +
      '<div class="disk-id">' +
      '<span class="disk-icon">' + ICONS.disk + '</span>' +
      '<div class="disk-titles"><div class="disk-line">' +
      '<span class="disk-name" title="' + esc(d.model) + '">' + esc(d.model) + '</span>' +
      (d.protocol ? '<span class="proto">' + esc(d.protocol) + '</span>' : '') +
      '</div><div class="disk-sub"><span>' + esc(d.device) + '</span>' +
      '</div></div></div>' +
      '<div class="disk-facts">' +
      '<div class="fact-mini"><span>健康状态</span><b class="v-' + statusTone + '">' +
      (d.health === null || d.health === undefined ? "—" : d.health + "%") + '</b></div>' +
      '<div class="fact-mini"><span>温度</span><b>' + fmtTemp(d.temp) + '</b></div>' +
      '<div class="fact-mini"><span>累计写入</span><b>' + (d.smart_ok ? fmtSize(d.tbw) : "—") + '</b></div>' +
      '<div class="fact-mini"><span>累计读取</span><b>' + (d.smart_ok ? fmtSize(d.tbr) : "—") + '</b></div>' +
      '</div></div>' +
      '</section>';
  }

  function renderHistory() {
    var view = $("#view");
    var disks = (state.overview && state.overview.disks) || [];
    var device = state.historyDevice;
    view.dataset.page = "history";

    if (!device) {
      view.innerHTML = historyEmpty();
    } else {
      var cur = null;
      disks.forEach(function (d) { if (!cur && d.device === device) cur = d; });
      view.innerHTML =
        (cur ? diskBrief(cur) : "") +
        '<section class="card chart-card">' +
        '<div class="chart-head"><h2>每日读取</h2></div>' +
        '<div class="chart-host" id="chart-read"></div></section>' +

        '<section class="card chart-card">' +
        '<div class="chart-head"><h2>每日写入</h2></div>' +
        '<div class="chart-host" id="chart-write"></div></section>' +

        '<section class="card chart-card">' +
        '<div class="chart-head"><h2>每日温度</h2></div>' +
        '<div class="chart-host" id="chart-temp"></div></section>';
    }

    updateTopbarSelector(disks, device ? [device] : [], function (next) {
      state.historyDevice = next[0] || null;
      state.historyYear = null;
      loadHistory();
    }, true);

    if (device && state.history) {
      mountYearSwitcher(state.history.years || [], state.historyYear, function (year) {
        state.historyYear = year;
        renderHistory();
      });
    }
    mountExportButton(device);

    if (!device) {
      var pick = $("#btn-pick-disk");
      if (pick) {
        pick.addEventListener("click", function () {
          var trigger = document.querySelector(".selector-btn");
          if (trigger) trigger.click();
        });
      }
      return;
    }
    if (state.history) historyCharts(state.history);
  }

  /* ------------------------------------------------------------ 测速页 */

  function fmtMbps(v) {
    if (v === null || v === undefined || isNaN(v)) return "—";
    return (v >= 100 ? Math.round(v) : Math.round(v * 10) / 10) + " MB/s";
  }

  function gaugeMax(v) {
    if (!v) return 500;
    var steps = [100, 200, 500, 1000, 2000, 4000, 8000, 16000];
    for (var i = 0; i < steps.length; i += 1) {
      if (v <= steps[i]) return steps[i];
    }
    return 16000;
  }

  function growGauge(gauge, value) {
    if (!gauge) return;
    if (value > gauge.getMax() * 0.92) gauge.setMax(value * 1.3);
  }

  function benchHasHistory(device) {
    if (!device) return false;
    var items = (state.bench || {}).items || [];
    for (var i = 0; i < items.length; i += 1) {
      if (items[i].device === device) return true;
    }
    return false;
  }

  /* 测速页顶部：选择硬盘 + 开始/再次测速，两个按钮常驻 */
  function mountBenchTopbar(disks, device, running) {
    updateTopbarSelector(disks, device ? [device] : [], function (next) {
      state.benchDevice = next[0] || null;
      state.benchRun = null;          // 换盘后不保留上一块盘的结果
      renderBench();
      if (state.benchDevice) loadBench();
    }, true);

    var host = $("#topbar-right");
    var label = running ? "测速中…" : (benchHasHistory(device) ? "再次测速" : "开始测速");
    var btn = el("button", "btn btn-primary", label);
    btn.type = "button";
    btn.id = "btn-bench";
    btn.disabled = !device || running;
    btn.title = device
      ? (benchHasHistory(device) ? "对该盘重新测速" : "对该盘测速")
      : "请先选择硬盘";
    if (device && !running) {
      btn.addEventListener("click", function () { startBench(device); });
    }
    host.appendChild(btn);
  }

  function startBench(device) {
    state.benchRun = { device: device, running: true, stage: "准备中", percent: 0, result: null, live: null };
    state.benchPlayer = { pending: [], shown: { read: 0, write: 0 }, playing: false };
    renderBench();
    postJSON("api/bench/start", { device: device }).then(function (res) {
      if (!res.ok) {
        toast(res.error || "启动失败");
        state.benchRun = null;
        renderBench();
        return;
      }
      pollBench();
    });
  }

  function benchMetaLine(device) {
    if (!device) return "选择硬盘后即可开始测速";
    var last = null;
    ((state.bench || {}).items || []).forEach(function (item) {
      if (!last && item.device === device) last = item;
    });
    if (!last) return "该盘尚未测速";
    return "该盘最近测速 " + fmtTime(last.ts) + " · 读取 " + fmtMbps(last.read_mbps) +
      " · 写入 " + fmtMbps(last.write_mbps);
  }

  function renderBench() {
    var view = $("#view");
    var disks = (state.overview && state.overview.disks) || [];
    var device = state.benchDevice;
    var run = state.benchRun && state.benchRun.device === device ? state.benchRun : null;
    var running = !!(run && run.running);
    view.dataset.page = "bench";

    mountBenchTopbar(disks, device, running);

    // 默认就展示仪表盘 + 全部历史记录，不因为没选盘而空着

    var result = run && run.result ? run.result : null;
    var plan = (state.bench || {}).plan || null;
    var hint = (plan && device)
      ? ("写入 " + (plan.write_dir || "未测") + (plan.read_device ? " · 直读 " + plan.read_device : ""))
      : "";

    var html = '<section class="card bench-gauge-card">';
    html += '<div class="bench-progress-wrap' + (running ? '' : ' is-hidden') + '" id="bench-progress-wrap">' +
      '<div class="bench-progress"><i id="bench-progress-bar" style="width:' + (run ? run.percent : 0) + '%"></i></div>' +
      '<span class="bench-stage" id="bench-stage">' + esc((run && run.stage) || "") + '</span></div>';
    html += '<div class="gauge-row"><div class="gauge-item" id="gauge-read"></div>' +
      '<div class="gauge-item" id="gauge-write"></div></div>';
    html += '<div class="bench-bar"><div class="bench-meta" id="bench-meta">' +
      esc(benchMetaLine(device)) + '</div>' +
      (hint ? '<span class="bench-plan">' + esc(hint) + '</span>' : '') + '</div></section>';

    html += '<section class="card table-card"><div class="table-head">' +
      '<h2>历史测速</h2>' +
      '<button class="btn btn-sm btn-dark" id="btn-clear" type="button">清空记录</button></div>' +
      '<table class="table bench-history"><thead><tr><th>时间</th><th>硬盘</th><th>读取</th><th>写入</th><th>说明</th></tr></thead><tbody>';
    var items = (state.bench || {}).items || [];
    if (!items.length) {
      html += '<tr><td colspan="5"><div class="empty">暂无测速记录</div></td></tr>';
    } else {
      items.forEach(function (row) {
        var when = window.innerWidth <= 720 ? fmtTimeBrief(row.ts) : fmtTime(row.ts);
        html += '<tr><td class="c-muted">' + when + '</td>' +
          '<td><div class="device-cell">' + ICONS.disk +
          '<span class="bench-name" title="' + esc(row.model || row.device) + '">' +
          esc(row.model || row.device) + '</span>' +
          (row.model ? '<span class="bench-dev">' + esc(row.device) + '</span>' : '') + '</div></td>' +
          '<td class="c-ok">' + fmtMbps(row.read_mbps) + '</td>' +
          '<td class="c-blue">' + fmtMbps(row.write_mbps) + '</td>' +
          '<td class="c-muted"><span class="bench-note" title="' + esc(row.note || "") + '">' +
          esc(row.note || "") + '</span></td></tr>';
      });
    }
    html += '</tbody></table></section>';
    view.innerHTML = html;

    // 本次刚跑完用本次结果；否则回落到该盘最近一条记录，页面刷新后也能看到上次成绩
    var lastRow = null;
    ((state.bench || {}).items || []).forEach(function (item) {
      if (!lastRow && item.device === device) lastRow = item;
    });
    var readInit = result ? result.read_mbps : (lastRow ? lastRow.read_mbps : 0);
    var writeInit = result ? result.write_mbps : (lastRow ? lastRow.write_mbps : 0);
    state.benchGauges = {
      read: Charts.gauge($("#gauge-read"), { label: "Read", value: readInit, max: gaugeMax(readInit) }),
      write: Charts.gauge($("#gauge-write"), { label: "Write", value: writeInit, max: gaugeMax(writeInit) })
    };

    var clear = $("#btn-clear");
    if (clear) {
      clear.addEventListener("click", function () {
        if (!window.confirm("确认清空全部测速记录？")) return;
        postJSON("api/bench/clear", {}).then(function () {
          toast("已清空");
          loadBench();
        });
      });
    }
  }

  /* 指针播放器：按队列一轮一轮地转，轮次太快也不会漏 */
  function ensurePlayer() {
    if (!state.benchPlayer) {
      state.benchPlayer = { pending: [], shown: { read: 0, write: 0 }, playing: false };
    }
    return state.benchPlayer;
  }

  /* 每轮实测 10 秒，节奏本来就够慢，指针直接跟着走，不再排队补动画 */
  function collectRounds(st) {
    var player = ensurePlayer();
    var rounds = st.rounds || {};
    var gauges = state.benchGauges;
    ["read", "write"].forEach(function (phase) {
      var arr = rounds[phase] || [];
      if (arr.length <= player.shown[phase]) return;
      var value = arr[arr.length - 1];
      player.shown[phase] = arr.length;
      var gauge = phase === "read" ? gauges && gauges.read : gauges && gauges.write;
      if (gauge) {
        growGauge(gauge, value);
        gauge.setValue(value);
      }
    });
  }

  /* 测的过程中指针跟着瞬时速度摆；轮间那 2 秒归零，让人看清一轮结束了 */
  function applyLiveGauge(st) {
    var live = st.live || {};
    var gauges = state.benchGauges;
    if (!live.phase || !gauges) return;
    var gauge = live.phase === "write" ? gauges.write : gauges.read;
    if (!gauge) return;
    if (live.gap) {
      gauge.setValue(0);
      return;
    }
    if (typeof live.rate === "number") {
      growGauge(gauge, live.rate);
      gauge.setValue(live.rate);
    }
  }

  /* 队列播完且测速已结束时，把指针落到本次最高成绩 */
  function settleIfDone() {
    var run = state.benchRun;
    var player = state.benchPlayer;
    if (!run || run.running || !player || player.pending.length) return;
    if (!run.result || run.settled) return;
    var gauges = state.benchGauges;
    if (gauges) {
      growGauge(gauges.read, run.result.read_mbps);
      growGauge(gauges.write, run.result.write_mbps || 0);
      gauges.read.setValue(run.result.read_mbps);
      gauges.write.setValue(run.result.write_mbps || 0);
    }
    run.settled = true;
    var meta = $("#bench-meta");
    if (meta) {
      meta.textContent = "本次测速完成 · 读取 " + fmtMbps(run.result.read_mbps) +
        " · 写入 " + fmtMbps(run.result.write_mbps);
    }
    setTimeout(function () {
      if (state.page === "bench") loadBench();
    }, 1200);
  }

  function applyBenchLive(res) {
    var st = (res && res.state) || {};
    var run = state.benchRun;
    if (!run) return;
    run.stage = st.stage;
    run.percent = st.percent || 0;
    run.running = !!st.running;

    applyLiveGauge(st);
    collectRounds(st);

    var bar = $("#bench-progress-bar");
    if (bar) bar.style.width = (run.percent || 0) + "%";
    var stage = $("#bench-stage");
    if (stage) stage.textContent = run.stage || "";
    var wrap = $("#bench-progress-wrap");
    if (wrap) wrap.classList.toggle("is-hidden", !run.running);

    if (!run.running) {
      if (st.error && !run.errorShown) {
        run.errorShown = true;
        toast(st.error);
      }
      if (st.result && st.device === run.device) run.result = st.result;
      var btn = $("#btn-bench");
      if (btn) {
        btn.disabled = false;
        btn.textContent = (st.result || benchHasHistory(run.device)) ? "再次测速" : "开始测速";
      }
      settleIfDone();
    }
  }

  function pollBench() {
    clearTimeout(state.benchTimer);
    var device = state.benchRun ? state.benchRun.device : state.benchDevice;
    var q = device ? ("?device=" + encodeURIComponent(device)) : "";
    getJSON("api/bench" + q).then(function (res) {
      state.bench = res;
      if (state.page !== "bench") return;
      if (!state.benchRun || !state.benchRun.running) {
        applyBenchLive(res);
        return;
      }
      applyBenchLive(res);
      if (state.benchRun && state.benchRun.running) {
        state.benchTimer = setTimeout(pollBench, 300);
      }
    }).catch(function () {
      state.benchTimer = setTimeout(pollBench, 1500);
    });
  }

  /* ------------------------------------------------------------ 关于页 */

  function mountHelp(info) {
    var host = $("#topbar-right");
    if (!host) return;
    var rows = [
      ["磁盘数量", info.disks + " 块"],
      ["数据目录", info.data_dir],
      ["配置目录", info.etc_dir],
      ["查询工具", info.smartctl]
    ];
    if (info.skipped && info.skipped.length) {
      var names = info.skipped.slice(0, 2).map(function (s) { return s.model || s.device; }).join("、");
      if (info.skipped.length > 2) names += " 等";
      rows.push(["未纳入统计", info.skipped.length + " 块（" + names + "）"]);
    }
    var rowsHtml = rows.map(function (pair) {
      return '<dt>' + esc(pair[0]) + '</dt><dd>' + esc(pair[1]) + '</dd>';
    }).join("");
    var wrap = el("div", "help-wrap");
    wrap.innerHTML =
      '<button class="help-btn" type="button" aria-label="运行信息">?</button>' +
      '<div class="help-tip"><div class="help-tip-head">运行信息</div>' +
      '<dl class="help-list">' + rowsHtml + '</dl></div>';
    host.appendChild(wrap);
  }

  function mountAboutActions(info) {
    var host = $("#topbar-right");
    if (!host) return;
    host.innerHTML = "";
    var save = el("button", "btn btn-primary btn-sm", "保存设置");
    save.type = "button";
    save.id = "btn-save";
    host.appendChild(save);
    mountHelp(info);
  }

  function helpDot(text) {
    return '<div class="help-wrap"><button class="help-btn" type="button" aria-label="说明">?</button>' +
      '<div class="help-tip help-tip-text">' + esc(text) + '</div></div>';
  }

  /* 「采集与测速」问号里的说明：把"每天写多少"算清楚，回应怕频繁采集伤盘的顾虑 */
  function sampleVolumeText(info, cfg) {
    // 只有样本够多时才用实测均值；刚装的库文件里还有表和索引的空页，除下来会虚高
    var perRow = 130;
    if (info.sample_count >= 5000 && info.db_size > 0) {
      perRow = Math.round(info.db_size / info.sample_count);
    }
    perRow = Math.max(80, Math.min(400, perRow));

    var interval = Math.max(5, Number(cfg.sample_interval) || 60);
    var perDay = Math.round(86400 / interval);
    var dayBytes = perDay * perRow;
    var monthMb = dayBytes * 30 / 1048576;
    var dayText = dayBytes >= 1048576
      ? (dayBytes / 1048576).toFixed(1) + " MB"
      : Math.round(dayBytes / 1024) + " KB";
    var disks = Math.max(1, Number(info.disks) || 1);
    var yearText = monthMb * disks * 12 >= 1024
      ? (monthMb * disks * 12 / 1024).toFixed(1) + " GB"
      : Math.round(monthMb * disks * 12) + " MB";

    return "采样间隔：后台每隔多少秒读取一次硬盘 SMART（写入量、通电时长、温度、健康度）。" +
      "数值越小，历史曲线颗粒越细、硬盘唤醒越频繁；60 秒足够日常使用。\n\n" +
      "数据量：每块盘每 " + interval + " 秒记录 1 条，一天 " + perDay.toLocaleString("zh-CN") +
      " 条；按当前数据库实测每条约 " + perRow + " 字节，一天约 " + dayText +
      "，一个月约 " + monthMb.toFixed(1) + " MB。\n\n" +
      "采集时只是读取 SMART，不往硬盘里写任何数据；这些记录存放在 NAS 的数据库里，" +
      disks + " 块盘一年约 " + yearText + "，对固态硬盘寿命的影响可以忽略。";
  }

  function renderAbout() {
    var view = $("#view");
    view.innerHTML = '<div class="loading">正在加载…</div>';
    view.dataset.page = "about";
    Promise.all([getJSON("api/about"), getJSON("api/settings")]).then(function (res) {
      var info = res[0], cfg = res[1];
      var lastBackup = cfg.backup_last_at
        ? fmtTime(cfg.backup_last_at) + " · " + (cfg.backup_last_path || "")
        : "还没有备份";
      var nextBackup = fmtNextBackup(cfg);

      view.innerHTML =
        '<section class="card settings-card">' +
        '<div class="chart-head"><h2>采样与测速</h2>' +
        helpDot(sampleVolumeText(info, cfg)) + '</div>' +
        '<div class="form-grid">' +
        '<div class="form-item"><span>采样间隔（秒）</span>' +
        '<input id="cfg-interval" class="input" value="' + cfg.sample_interval + '"></div>' +
        '<label class="form-item"><span>测速文件大小（MB）</span>' +
        '<input id="cfg-bench" class="input" value="' + cfg.bench_size_mb + '"></label>' +
        '</div>' +
        '</section>' +

        '<section class="card settings-card">' +
        '<div class="chart-head"><h2>面板告警</h2>' +
        helpDot("检测到健康度偏低、温度过高、介质错误增加、异常断电等情况时，会记录到概况页下方的「最近事件」。" +
          "同一块硬盘的同一个问题，在通知频率内只提醒一次。") + '</div>' +
        '<div class="form-grid">' +
        '<label class="form-item"><span>健康度低于（%）</span>' +
        '<input id="cfg-alert-health" class="input" value="' + cfg.notify_health_below + '"></label>' +
        '<label class="form-item"><span>温度高于（℃）</span>' +
        '<input id="cfg-alert-temp" class="input" value="' + cfg.alert_temp_high + '"></label>' +
        '<label class="form-item"><span>通知频率（秒）</span>' +
        '<input id="cfg-alert-interval" class="input" value="' + cfg.alert_interval_seconds + '"></label>' +
        '</div>' +
        '</section>' +

        '<section class="card settings-card">' +
        '<div class="chart-head"><h2>备份与恢复</h2>' +
        '<label class="switch"><input type="checkbox" id="cfg-backup-enabled"' +
        (cfg.backup_enabled ? " checked" : "") + '><span></span>自动备份</label></div>' +
        '<div class="form-grid">' +
        '<label class="form-item"><span>备份间隔（天）</span>' +
        '<input id="cfg-backup-interval" class="input" value="' + cfg.backup_interval_days + '"></label>' +
        '<label class="form-item"><span>保留份数</span>' +
        '<input id="cfg-backup-keep" class="input" value="' + cfg.backup_keep + '"></label>' +
        '</div>' +
        '<label class="form-item form-wide"><span>备份目录</span>' +
        '<input id="cfg-backup-dir" class="input" value="' + esc(cfg.backup_dir) + '"></label>' +
        '<p class="form-hint">上次备份：' + esc(lastBackup) + '</p>' +
        '<p class="form-hint">下次备份：' + esc(nextBackup) + '</p>' +
        '<div class="form-actions">' +
        '<button class="btn btn-sm" id="btn-backup-now" type="button">立即备份</button>' +
        '<button class="btn btn-sm" id="btn-import" type="button">导入 JSON</button>' +
        '<input type="file" id="file-import" accept=".json,application/json" hidden>' +
        '</div></section>' +

        '';

      mountAboutActions(info);

      $("#btn-save").addEventListener("click", function () {
        postJSON("api/settings", {
          sample_interval: $("#cfg-interval").value,
          notify_health_below: $("#cfg-alert-health").value,
          bench_size_mb: $("#cfg-bench").value,
          alert_enabled: true,
          alert_temp_high: $("#cfg-alert-temp").value,
          alert_interval_seconds: $("#cfg-alert-interval").value,
          backup_enabled: $("#cfg-backup-enabled").checked,
          backup_interval_days: $("#cfg-backup-interval").value,
          backup_keep: $("#cfg-backup-keep").value,
          backup_dir: $("#cfg-backup-dir").value
        }).then(function (r) {
          toast(r.ok ? "设置已保存" : (r.error || "保存失败"));
        });
      });

      $("#btn-backup-now").addEventListener("click", function () {
        var btn = this;
        btn.disabled = true;
        postJSON("api/backup/now", {}).then(function (r) {
          if (r.ok) {
            toast("已备份 " + r.disk_count + " 块盘到 " + r.path);
            renderAbout();
          } else {
            toast(r.error || "备份失败");
            btn.disabled = false;
          }
        });
      });

      var fileInput = $("#file-import");
      $("#btn-import").addEventListener("click", function () { fileInput.click(); });
      fileInput.addEventListener("change", function () {
        var file = fileInput.files && fileInput.files[0];
        if (!file) return;
        var reader = new FileReader();
        reader.onload = function () {
          var payload;
          try {
            payload = JSON.parse(reader.result);
          } catch (err) {
            toast("文件不是合法的 JSON");
            return;
          }
          toast("正在导入…");
          postJSON("api/import", payload).then(function (r) {
            if (!r.ok) { toast(r.error || "导入失败"); return; }
            var disks = (r.disks || []).length;
            toast("导入完成：新增 " + r.added + " 条采样，涉及 " + disks + " 块盘");
            loadOverview().then(function () { renderAbout(); });
          });
        };
        reader.readAsText(file);
        fileInput.value = "";
      });
    }).catch(function (err) {
      console.warn(err);
      view.innerHTML = '<div class="card"><div class="empty">设置加载失败，请刷新页面</div></div>';
    });
  }

  /* ------------------------------------------------------------ 顶栏 */

  function updateTopbarSelector(disks, selected, onChange, single) {
    var host = $("#topbar-right");
    host.innerHTML = "";
    if (!disks.length) return;
    var list = (selected || []).slice();
    // 多选场景默认全选；单选（历史页）默认不选，交给用户主动选择
    if (!single && !list.length) list = disks.map(function (d) { return d.device; });
    mountSelector(host, {
      disks: disks,
      selected: list,
      single: single,
      onChange: onChange
    });
  }

  /* ------------------------------------------------------------ 加载 */

  function loadEvents() {
    return getJSON("api/events?limit=20").then(function (res) {
      state.events = res.items || [];
      if (state.page === "overview" && state.overview) renderOverview();
    }).catch(function () {});
  }

  function loadOverview() {
    return getJSON("api/overview").then(function (data) {
      state.overview = data;
      var valid = data.disks.map(function (d) { return d.device; });
      if (!state.selected) {
        state.selected = valid;
      } else {
        var kept = state.selected.filter(function (d) { return valid.indexOf(d) >= 0; });
        state.selected = kept.length ? kept : valid;
      }
      if (state.page === "overview") renderOverview();
      if (state.page === "history") renderHistory();
      if (state.page === "bench") renderBench();
    }).catch(function (err) {
      console.warn(err);
      if (state.page === "overview") {
        $("#view").innerHTML = '<div class="card"><div class="empty">无法连接后端服务，请检查应用运行状态</div></div>';
      }
    });
  }

  function loadHistory() {
    var device = state.historyDevice;
    if (!device) {
      state.history = null;
      if (state.page === "history") renderHistory();
      return Promise.resolve();
    }
    return getJSON("api/history?devices=" + encodeURIComponent(device)).then(function (data) {
      state.history = data;
      var years = data.years || [];
      if (!state.historyYear || years.indexOf(state.historyYear) < 0) {
        state.historyYear = years.length ? years[0] : new Date().getFullYear();
      }
      if (state.page === "history") renderHistory();
    }).catch(function (err) {
      console.warn("[ssdplus] history", err);
      toast("历史数据加载失败");
      if (state.page === "history") {
        var view = $("#view");
        view.dataset.page = "error";
        view.innerHTML = '<section class="card"><div class="empty-state">' +
          '<h3>历史数据加载失败</h3><p>请刷新页面重试；若持续出现，请查看应用日志。</p></div></section>';
      }
    });
  }

  function loadBench() {
    var q = state.benchDevice ? ("?device=" + encodeURIComponent(state.benchDevice)) : "";
    return getJSON("api/bench" + q).then(function (data) {
      state.bench = data;
      if (state.page === "bench") renderBench();
      if (data.state && data.state.running) pollBench();
    });
  }

  /* ------------------------------------------------------------ 路由 */

  var TITLES = { overview: "硬盘概况", history: "历史数据", bench: "硬盘测速", about: "应用设置" };

  function route() {
    var hash = (location.hash || "#/overview").replace("#/", "");
    var page = TITLES[hash] ? hash : "overview";
    state.page = page;
    $("#page-title").textContent = TITLES[page];
    var topbar = document.querySelector(".topbar");
    if (topbar) topbar.setAttribute("data-page", page);
    document.querySelectorAll(".nav-item").forEach(function (item) {
      item.classList.toggle("is-active", item.getAttribute("data-page") === page);
    });
    $("#topbar-right").innerHTML = "";
    clearInterval(state.timer);

    if (page === "about") { renderAbout(); return; }

    var ready = state.overview ? Promise.resolve() : loadOverview();
    ready.then(function () {
      if (page === "overview") {
        renderOverview();
        loadEvents();
        state.timer = setInterval(function () {
          getJSON("api/overview").then(function (data) {
            state.overview = data;
            if (state.page === "overview") renderOverview();
          }).catch(function () {});
        }, 8000);
      } else if (page === "history") {
        if (state.historyDevice) {
          renderHistory();
          loadHistory();
        } else {
          renderHistory();
        }
      } else if (page === "bench") {
        renderBench();
        loadBench();
      }
    });
  }

  window.addEventListener("resize", function () {
    clearTimeout(window._rz);
    window._rz = setTimeout(function () {
      if (state.page === "history") renderHistory();
      if (state.page === "bench") renderBench();
    }, 200);
  });

  window.addEventListener("hashchange", route);
  route();

  // 兜底：如果脚本或后端有问题，别让页面一直停在“正在加载…”
  setTimeout(function () {
    var view = document.getElementById("view");
    if (view && view.querySelector(".loading")) {
      view.innerHTML = '<div class="card"><div class="empty">' +
        "页面加载超时：前端资源可能缺失，或后端接口无响应。<br>" +
        "请刷新页面；若持续出现，请查看 " +
        '<code>应用数据目录/info.log</code>，或卸载后重新安装应用。</div></div>';
    }
  }, 8000);
})();
