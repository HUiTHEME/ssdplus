/* 轻量 SVG 图表：面积图 / 分组柱状图 / 折线图 / 仪表盘
 * 不依赖任何第三方库，按容器宽度实时绘制。
 */
(function (global) {
  "use strict";

  var NS = "http://www.w3.org/2000/svg";

  function el(name, attrs) {
    var node = document.createElementNS(NS, name);
    for (var key in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, key)) {
        node.setAttribute(key, attrs[key]);
      }
    }
    return node;
  }

  function text(attrs, value) {
    var node = el("text", attrs);
    node.textContent = value;
    return node;
  }

  function h(tag, cls, content) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (content !== undefined) node.textContent = content;
    return node;
  }

  function clear(host) {
    while (host.firstChild) host.removeChild(host.firstChild);
  }

  /* Catmull-Rom 转三次贝塞尔，得到平滑曲线 */
  function smoothPath(pts, closeTo, width, clamp) {
    if (!pts.length) return "";
    if (pts.length === 1) return "M" + pts[0].x + "," + pts[0].y;
    function cy(v) {
      if (!clamp) return v;
      return Math.min(Math.max(v, clamp.min), clamp.max);
    }
    var d = "M" + pts[0].x + "," + pts[0].y;
    for (var i = 0; i < pts.length - 1; i += 1) {
      var p0 = pts[i - 1] || pts[i];
      var p1 = pts[i];
      var p2 = pts[i + 1];
      var p3 = pts[i + 2] || p2;
      var c1x = p1.x + (p2.x - p0.x) / 6;
      var c1y = cy(p1.y + (p2.y - p0.y) / 6);
      var c2x = p2.x - (p3.x - p1.x) / 6;
      var c2y = cy(p2.y - (p3.y - p1.y) / 6);
      d += " C" + c1x.toFixed(2) + "," + c1y.toFixed(2) + " " +
        c2x.toFixed(2) + "," + c2y.toFixed(2) + " " +
        p2.x.toFixed(2) + "," + p2.y.toFixed(2);
    }
    if (closeTo !== undefined) {
      d += " L" + (width || pts[pts.length - 1].x) + "," + closeTo +
        " L" + pts[0].x + "," + closeTo + " Z";
    }
    return d;
  }

  function niceMax(value) {
    if (!value || value <= 0) return 10;
    var exp = Math.pow(10, Math.floor(Math.log10(value)));
    var norm = value / exp;
    var steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < steps.length; i += 1) {
      if (norm <= steps[i] + 1e-9) return steps[i] * exp;
    }
    return 10 * exp;
  }

  function axes(svg, box, maxValue, labels, formatter, opts) {
    var ticks = opts.ticks || 4;
    var g = el("g", {});
    for (var i = 0; i <= ticks; i += 1) {
      var ratio = i / ticks;
      var y = box.y + box.h - box.h * ratio;
      g.appendChild(el("line", {
        x1: box.x, y1: y, x2: box.x + box.w, y2: y,
        stroke: "#f0f2f5", "stroke-width": 1
      }));
      g.appendChild(text({
        x: box.x - 10, y: y + 3.5, "text-anchor": "end",
        "font-size": 10, fill: "#b3b9c2"
      }, formatter(maxValue * ratio)));
    }
    svg.appendChild(g);

    var step = Math.ceil(labels.length / (opts.maxLabels || 12));
    labels.forEach(function (label, i) {
      if (i % step !== 0 && i !== labels.length - 1) return;
      var x = box.x + (labels.length === 1 ? box.w / 2 : (box.w * i) / (labels.length - 1));
      svg.appendChild(text({
        x: x, y: box.y + box.h + 16, "text-anchor": "middle",
        "font-size": 10, fill: "#b3b9c2"
      }, label));
    });
  }

  function mount(host, height) {
    clear(host);
    var width = host.clientWidth || host.parentNode.clientWidth || 640;
    var svg = el("svg", { viewBox: "0 0 " + width + " " + height, width: "100%", height: height });
    host.appendChild(svg);
    return { svg: svg, width: width };
  }

  var Charts = {
    /* 面积图：多条曲线叠加，带渐变填充 */
    area: function (host, opts) {
      var height = opts.height || 260;
      var m = mount(host, height);
      var svg = m.svg;
      var box = { x: 62, y: 12, w: m.width - 78, h: height - 46 };
      var series = opts.series || [];
      var max = 0;
      series.forEach(function (s) {
        s.data.forEach(function (v) { if (v > max) max = v; });
      });
      var empty = !(max > 0);
      max = max > 0 ? niceMax(max * 1.15) : 1;

      var defs = el("defs", {});
      series.forEach(function (s, i) {
        var grad = el("linearGradient", { id: "ag" + i, x1: 0, y1: 0, x2: 0, y2: 1 });
        grad.appendChild(el("stop", { offset: "0%", "stop-color": s.color, "stop-opacity": 0.35 }));
        grad.appendChild(el("stop", { offset: "100%", "stop-color": s.color, "stop-opacity": 0.02 }));
        defs.appendChild(grad);
      });
      svg.appendChild(defs);

      axes(svg, box, max, opts.labels || [], opts.format || function (v) { return Math.round(v); }, opts);

      series.forEach(function (s, si) {
        var pts = s.data.map(function (v, i) {
          var x = box.x + (s.data.length === 1 ? box.w / 2 : (box.w * i) / (s.data.length - 1));
          var y = box.y + box.h - (max ? (v / max) * box.h : 0);
          return { x: x, y: y };
        });
        var bounds = { min: box.y, max: box.y + box.h };
        svg.appendChild(el("path", {
          d: smoothPath(pts, box.y + box.h, box.x + box.w, bounds),
          fill: "url(#ag" + si + ")",
          stroke: "none"
        }));
        svg.appendChild(el("path", {
          d: smoothPath(pts, undefined, undefined, bounds), fill: "none", stroke: s.color,
          "stroke-width": 1.8, "stroke-linecap": "round"
        }));
        if (s.data.length <= 24) {
          pts.forEach(function (p) {
            svg.appendChild(el("circle", {
              cx: p.x, cy: p.y, r: 2.6, fill: "#fff", stroke: s.color, "stroke-width": 1.6
            }));
          });
        }
      });

      if (empty) {
        svg.appendChild(text({
          x: box.x + box.w / 2, y: box.y + box.h / 2 - 10, "text-anchor": "middle",
          "font-size": 12, fill: "#b3b9c2"
        }, "暂无采样数据"));
        svg.appendChild(text({
          x: box.x + box.w / 2, y: box.y + box.h / 2 + 10, "text-anchor": "middle",
          "font-size": 11, fill: "#cfd5dd"
        }, "采集已在后台运行，读写量会随时间累积"));
      }
      return m;
    },

    /* 分组柱状图 */
    bars: function (host, opts) {
      var height = opts.height || 240;
      var m = mount(host, height);
      var svg = m.svg;
      var box = { x: 62, y: 12, w: m.width - 78, h: height - 46 };
      var series = opts.series || [];
      var labels = opts.labels || [];
      var max = 0;
      series.forEach(function (s) {
        s.data.forEach(function (v) { if (v > max) max = v; });
      });
      var empty = !(max > 0);
      max = max > 0 ? niceMax(max * 1.1) : 1;

      axes(svg, box, max, labels, opts.format || function (v) { return Math.round(v); }, opts);

      var groupWidth = labels.length ? box.w / labels.length : box.w;
      var barWidth = Math.max(4, Math.min(14, (groupWidth * 0.55) / Math.max(1, series.length)));
      var gap = 3;

      labels.forEach(function (_, i) {
        var center = box.x + groupWidth * i + groupWidth / 2;
        var totalW = series.length * barWidth + (series.length - 1) * gap;
        series.forEach(function (s, si) {
          var value = s.data[i] || 0;
          var h = max ? (value / max) * box.h : 0;
          var x = center - totalW / 2 + si * (barWidth + gap);
          svg.appendChild(el("rect", {
            x: x, y: box.y + box.h - h, width: barWidth, height: Math.max(h, 0),
            rx: 2, fill: s.color
          }));
        });
      });

      if (empty) {
        svg.appendChild(text({
          x: box.x + box.w / 2, y: box.y + box.h / 2, "text-anchor": "middle",
          "font-size": 12, fill: "#b3b9c2"
        }, "暂无采样数据"));
      }
      return m;
    },

    /* 单折线图（温度） */
    line: function (host, opts) {
      var height = opts.height || 190;
      var m = mount(host, height);
      var svg = m.svg;
      var box = { x: 56, y: 16, w: m.width - 118, h: height - 48 };
      var data = opts.data || [];
      var values = data.map(function (d) { return d.value; });
      var min = opts.min !== undefined ? opts.min : Math.floor(Math.min.apply(null, values.length ? values : [0]) - 4);
      var max = opts.max !== undefined ? opts.max : Math.ceil(Math.max.apply(null, values.length ? values : [10]) + 4);
      if (max - min < 6) max = min + 6;

      var ticks = 4;
      for (var i = 0; i <= ticks; i += 1) {
        var ratio = i / ticks;
        var y = box.y + box.h - box.h * ratio;
        var value = min + (max - min) * ratio;
        svg.appendChild(el("line", {
          x1: box.x, y1: y, x2: box.x + box.w, y2: y,
          stroke: "#f0f2f5", "stroke-width": 1
        }));
        svg.appendChild(text({
          x: box.x - 10, y: y + 3.5, "text-anchor": "end", "font-size": 10, fill: "#b3b9c2"
        }, Math.round(value)));
      }

      if (!data.length) {
        svg.appendChild(text({
          x: box.x + box.w / 2, y: box.y + box.h / 2, "text-anchor": "middle",
          "font-size": 11.5, fill: "#b3b9c2"
        }, "暂无温度数据"));
        return m;
      }

      var pts = data.map(function (d, i) {
        var x = box.x + (data.length === 1 ? box.w / 2 : (box.w * i) / (data.length - 1));
        var y = box.y + box.h - ((d.value - min) / (max - min)) * box.h;
        return { x: x, y: y };
      });

      var defs = el("defs", {});
      var grad = el("linearGradient", { id: "lg", x1: 0, y1: 0, x2: 1, y2: 0 });
      grad.appendChild(el("stop", { offset: "0%", "stop-color": "#34c38f" }));
      grad.appendChild(el("stop", { offset: "60%", "stop-color": "#7ed957" }));
      grad.appendChild(el("stop", { offset: "100%", "stop-color": "#f0ad25" }));
      defs.appendChild(grad);
      svg.appendChild(defs);

      svg.appendChild(el("path", {
        d: smoothPath(pts), fill: "none", stroke: "url(#lg)",
        "stroke-width": 2, "stroke-linecap": "round"
      }));

      var last = pts[pts.length - 1];
      svg.appendChild(el("circle", { cx: last.x, cy: last.y, r: 3, fill: "#fff", stroke: "#f0ad25", "stroke-width": 1.8 }));
      svg.appendChild(el("line", {
        x1: last.x, y1: last.y, x2: box.x + box.w + 16, y2: last.y,
        stroke: "#e2e5e9", "stroke-width": 1, "stroke-dasharray": "3 3"
      }));
      svg.appendChild(text({
        x: box.x + box.w + 20, y: last.y + 4, "font-size": 11, fill: "#f0ad25", "font-weight": 600
      }, data[data.length - 1].value.toFixed(1)));

      var step = Math.ceil(data.length / 10);
      data.forEach(function (d, i) {
        if (i % step !== 0 && i !== data.length - 1) return;
        svg.appendChild(text({
          x: pts[i].x, y: box.y + box.h + 16, "text-anchor": "middle",
          "font-size": 10, fill: "#b3b9c2"
        }, opts.labelFormat ? opts.labelFormat(d.label) : d.label));
      });
      return m;
    },

    /* GitHub 风格日历热力图（按年份展示，宽度撑满容器） */
    heatmap: function (host, opts) {
      var items = opts.items || [];
      var colors = opts.colors || ["#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"];
      var GAP_MIN = 2, LEFT = 20, TOP = 20;

      var map = {};
      items.forEach(function (item) { map[item.date] = item; });

      var today = new Date();
      today.setHours(0, 0, 0, 0);
      var year = opts.year || today.getFullYear();
      var jan1 = new Date(year, 0, 1);
      var endOfYear = new Date(year, 11, 31);
      var limit = endOfYear.getTime() > today.getTime() ? today : endOfYear;

      var start = new Date(jan1.getTime());
      start.setDate(start.getDate() - start.getDay()); // 对齐到周日

      // 始终按完整一年排版（53 周左右），今天之后的日期留空白格；
      // 否则到 11 月就只剩 46 列，12 月的标签和格子根本没被创建出来。
      var totalDays = Math.round((endOfYear - start) / 86400000) + 1;
      var cols = Math.ceil(totalDays / 7);

      clear(host);
      var wrap = h("div", "heatmap-wrap");
      host.appendChild(wrap);

      // 让网格正好铺满容器宽度：先定格子大小，再按剩余空间分摊间距
      var hostW = Math.round(host.getBoundingClientRect().width) || host.clientWidth || 0;
      if (hostW < 160) hostW = Math.min(780, Math.max(240, window.innerWidth - 72));
      var avail = Math.max(120, hostW - LEFT - 2);
      var cell = 11;
      var gap = (avail - cols * cell) / Math.max(1, cols - 1);
      if (gap < GAP_MIN) {
        // 手机等窄屏：先把间距压到 1，再按剩余宽度缩格子，保证 53 周都铺得下
        gap = 1;
        cell = Math.max(3, Math.floor((avail - (cols - 1) * gap) / cols));
      }
      var width = LEFT + cols * cell + (cols - 1) * gap;
      var height = TOP + 7 * (cell + GAP_MIN) + (cell - GAP_MIN);

      var svg = el("svg", {
        viewBox: "0 0 " + width + " " + height,
        preserveAspectRatio: "xMidYMid meet",
        width: width,
        height: height
      });
      // 交给样式自适应：窄屏等比缩到容器宽度，宽屏不超过自然宽度（避免被裁掉 11、12 月）
      svg.style.display = "block";
      svg.style.width = "100%";
      svg.style.height = "auto";
      wrap.appendChild(svg);

      var tip = document.querySelector(".heatmap-tip");
      if (!tip) {
        tip = h("div", "heatmap-tip");
        document.body.appendChild(tip);
      }
      var lastEvt = null;
      function moveTip(evt) {
        if (evt) lastEvt = evt;
        if (!lastEvt) return;
        var tw = tip.offsetWidth, th = tip.offsetHeight;
        var left = lastEvt.clientX + 14;
        var top = lastEvt.clientY - th - 14;
        if (left + tw > window.innerWidth - 8) left = lastEvt.clientX - tw - 14;
        if (top < 8) top = lastEvt.clientY + 18;
        tip.style.left = Math.max(8, left) + "px";
        tip.style.top = top + "px";
      }
      function bindTip(node, item) {
        node.addEventListener("mouseenter", function (evt) {
          tip.innerHTML = item.tip;
          tip.classList.add("show");
          moveTip(evt);
          if (opts.onTip) {
            var pending = opts.onTip(tip, item);
            if (pending && typeof pending.then === "function") {
              pending.then(function () {
                if (tip.classList.contains("show")) moveTip(null);
              });
            }
          }
        });
        node.addEventListener("mousemove", moveTip);
        node.addEventListener("mouseleave", function () {
          tip.classList.remove("show");
        });
      }

      var values = items.map(function (i) { return i.value; })
        .filter(function (v) { return typeof v === "number" && v > 0; })
        .sort(function (a, b) { return a - b; });
      function quantile(p) {
        if (!values.length) return Infinity;
        return values[Math.min(values.length - 1, Math.floor(p * (values.length - 1)))];
      }
      var q1 = quantile(0.25), q2 = quantile(0.5), q3 = quantile(0.75);
      function level(v) {
        if (typeof v !== "number" || v <= 0) return 0;
        if (v <= q1) return 1;
        if (v <= q2) return 2;
        if (v <= q3) return 3;
        return 4;
      }

      var monthNames = ["1月", "2月", "3月", "4月", "5月", "6月",
        "7月", "8月", "9月", "10月", "11月", "12月"];
      var lastMonth = -1, lastLabelX = -999;

      function dateKey(d) {
        return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) +
          "-" + ("0" + d.getDate()).slice(-2);
      }

      for (var c = 0; c < cols; c += 1) {
        var x = LEFT + c * (cell + gap);
        var weekStart = new Date(start.getTime() + c * 7 * 86400000);

        // 列内第一个属于本年的日子，用它决定月份标签
        var firstInYear = new Date(Math.max(weekStart.getTime(), jan1.getTime()));
        var month = firstInYear.getMonth();
        if (month !== lastMonth) {
          // 按像素间距判断：窄屏格子只有 4-5px，按月数判断会让「10月」「11月」叠在一起
          var lx = Math.min(x, width - 24);
          if (lx - lastLabelX >= 20) {
            svg.appendChild(text({
              x: lx, y: 12, "font-size": 8.5, fill: "#b3b9c2"
            }, monthNames[month]));
            lastLabelX = lx;
          }
          lastMonth = month;
        }

        for (var r = 0; r < 7; r += 1) {
          var date = new Date(weekStart.getTime() + r * 86400000);
          if (date.getTime() < jan1.getTime()) continue;
          if (date.getTime() > limit.getTime()) {
            // 今年还没到：留一个极浅的占位格，整年的方块才连成一片
            svg.appendChild(el("rect", {
              x: x, y: TOP + r * (cell + GAP_MIN), width: cell, height: cell,
              rx: Math.min(2.4, cell * 0.2), fill: "#f6f7f9"
            }));
            continue;
          }

          var item = map[dateKey(date)];
          var rect = el("rect", {
            x: x, y: TOP + r * (cell + GAP_MIN), width: cell, height: cell,
            rx: Math.min(2.4, cell * 0.2), fill: colors[level(item ? item.value : null)]
          });
          if (item && item.tip) {
            rect.style.cursor = "pointer";
            bindTip(rect, item);
          }
          svg.appendChild(rect);
        }
      }

      ["日", "一", "二", "三", "四", "五", "六"].forEach(function (name, row) {
        svg.appendChild(text({
          x: LEFT - 5, y: TOP + row * (cell + GAP_MIN) + cell / 2 + 3,
          "text-anchor": "end", "font-size": 8.5, fill: "#b3b9c2"
        }, name));
      });

      // 图例放到卡片标题右侧，和标题同一条线，不再单独占一行
      var legend = h("div", "heatmap-legend");
      colors.forEach(function (color) {
        var sw = h("i", "sw");
        sw.style.background = color;
        legend.appendChild(sw);
      });
      var head = host.parentNode ? host.parentNode.querySelector(".chart-head") : null;
      (head || host).appendChild(legend);

      return { cell: cell, width: width };
    },

    /* 半圆仪表盘（指针可动画转动） */
    gauge: function (host, opts) {
      var W = 320, H = 215;
      var cx = W / 2, cy = 182, r = 112;
      var start = Math.PI, end = 0;
      var span = start - end;
      var label = opts.label || "";
      var scale = opts.scale || function (v) { return Math.round(v); };
      var state = {
        max: niceMax(opts.max || 1000),
        value: Math.max(0, opts.value || 0),
        shown: Math.max(0, opts.value || 0)
      };
      var needle = null, valueText = null, labelsGroup = null, animId = null;

      function polar(angle, radius) {
        return { x: cx + Math.cos(angle) * radius, y: cy - Math.sin(angle) * radius };
      }

      function arcPath(a0, a1, radius) {
        var p0 = polar(a0, radius), p1 = polar(a1, radius);
        var large = Math.abs(a1 - a0) > Math.PI ? 1 : 0;
        return "M" + p0.x.toFixed(2) + "," + p0.y.toFixed(2) +
          " A" + radius + "," + radius + " 0 " + large + " 1 " +
          p1.x.toFixed(2) + "," + p1.y.toFixed(2);
      }

      function placeNeedle(v) {
        if (!needle) return;
        var ratio = Math.max(0, Math.min(v / state.max, 1));
        var p = polar(start - span * ratio, r - 4);
        needle.setAttribute("x2", p.x.toFixed(2));
        needle.setAttribute("y2", p.y.toFixed(2));
        needle.setAttribute("opacity", v > 0 ? "1" : "0");
        if (valueText) valueText.textContent = v > 0 ? Math.round(v) : "--";
      }

      function drawLabels() {
        if (labelsGroup && labelsGroup.parentNode) labelsGroup.parentNode.removeChild(labelsGroup);
        labelsGroup = el("g", {});
        [0.25, 0.5, 0.75, 1].forEach(function (pos) {
          var a = start - span * pos;
          var p = polar(a, r + 17);
          labelsGroup.appendChild(text({
            x: Math.min(Math.max(p.x, 14), W - 14), y: p.y + 3,
            "text-anchor": "middle", "font-size": 9.5, fill: "#b3b9c2"
          }, scale(state.max * pos)));
        });
        svg.appendChild(labelsGroup);
      }

      function drawStatic() {
        clear(host);
        svg = el("svg", { viewBox: "0 0 " + W + " " + H, width: "100%", height: H });
        host.appendChild(svg);

        svg.appendChild(el("path", {
          d: arcPath(start, end, r), fill: "none", stroke: "#f0f2f5",
          "stroke-width": 16, "stroke-linecap": "butt"
        }));

        var segments = [
          { from: 0, to: 0.28, color: "#8fd8c8" },
          { from: 0.28, to: 0.5, color: "#7ed957" },
          { from: 0.5, to: 0.62, color: "#b7dd6a" },
          { from: 0.62, to: 0.82, color: "#f4c95d" },
          { from: 0.82, to: 1, color: "#f0a02e" }
        ];
        segments.forEach(function (seg) {
          svg.appendChild(el("path", {
            d: arcPath(start - span * seg.from, start - span * seg.to, r),
            fill: "none", stroke: seg.color, "stroke-width": 16, "stroke-linecap": "butt"
          }));
        });

        var tickCount = 10;
        for (var i = 0; i <= tickCount; i += 1) {
          var a = start - (span * i) / tickCount;
          var p0 = polar(a, r - 14), p1 = polar(a, r - 20);
          svg.appendChild(el("line", {
            x1: p0.x, y1: p0.y, x2: p1.x, y2: p1.y,
            stroke: "#dfe3e8", "stroke-width": 1.5, "stroke-linecap": "round"
          }));
        }

        drawLabels();

        needle = el("line", {
          x1: cx, y1: cy, x2: cx - r, y2: cy,
          stroke: "#9aa4b0", "stroke-width": 2, "stroke-linecap": "round",
          opacity: 0
        });
        svg.appendChild(needle);
        svg.appendChild(el("circle", {
          cx: cx, cy: cy, r: 4.5, fill: "#fff", stroke: "#9aa4b0", "stroke-width": 2
        }));

        svg.appendChild(text({
          x: cx, y: cy - 78, "text-anchor": "middle", "font-size": 11.5, fill: "#8a9099"
        }, label));
        valueText = text({
          x: cx, y: cy - 46, "text-anchor": "middle", "font-size": 30,
          "font-weight": 700, fill: "#1f2329", "font-variant-numeric": "tabular-nums"
        }, state.shown > 0 ? Math.round(state.shown) : "--");
        svg.appendChild(valueText);
        svg.appendChild(text({
          x: cx, y: cy - 26, "text-anchor": "middle", "font-size": 11, fill: "#8a9099"
        }, "MB/s"));
      }

      var svg = null;
      drawStatic();
      placeNeedle(state.shown);

      /* 用 setTimeout 而不是 rAF：后台标签页/WebView 里 rAF 可能完全不触发 */
      function animateTo(target) {
        if (animId) clearTimeout(animId);
        var from = state.shown;
        var to = Math.max(0, target);
        var dur = 520;
        var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
        function step() {
          var now = (window.performance && performance.now) ? performance.now() : Date.now();
          var p = Math.min(1, (now - t0) / dur);
          var eased = 1 - Math.pow(1 - p, 3);
          state.shown = from + (to - from) * eased;
          placeNeedle(state.shown);
          if (p < 1) animId = setTimeout(step, 16);
          else { state.shown = to; placeNeedle(to); animId = null; }
        }
        animId = setTimeout(step, 0);
      }

      return {
        getMax: function () { return state.max; },
        setMax: function (next) {
          var m = niceMax(next);
          if (m === state.max) return;
          state.max = m;
          drawStatic();
          placeNeedle(state.shown);
        },
        /* value <= 0 时归零并显示 -- */
        setValue: function (v, animate) {
          var target = Math.max(0, v || 0);
          if (animate === false) {
            state.shown = target;
            placeNeedle(target);
            return;
          }
          animateTo(target);
        }
      };
    }
  };

  global.Charts = Charts;
})(window);
