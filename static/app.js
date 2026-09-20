/* 15分钟便民生活圈体检报告 — 前端逻辑（原生 JS + BMapGL） */
(function () {
  "use strict";

  var config = null;
  var map = null;
  var overlays = [];          // 所有动态覆盖物
  var currentResult = null;
  var pollTimer = null;

  var $ = function (id) { return document.getElementById(id); };
  var banner = $("ak-banner");
  var notice = $("notice-bar");

  /* ---------------- 工具 ---------------- */

  function showNotice(msg, seconds) {
    notice.textContent = msg;
    notice.classList.remove("hidden");
    if (seconds) setTimeout(function () { notice.classList.add("hidden"); }, seconds * 1000);
  }

  function hideNotice() { notice.classList.add("hidden"); }

  function clearOverlays() {
    if (!map) return;
    overlays.forEach(function (o) {
      try { map.removeOverlay(o); } catch (e) { /* 忽略 */ }
    });
    overlays = [];
  }

  /* 生成不同颜色的圆点图标 */
  function dotIcon(color, size) {
    size = size || 10;
    var svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size + '">' +
      '<circle cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + (size / 2 - 1) +
      '" fill="' + color + '" stroke="#ffffff" stroke-width="1.2"/></svg>';
    var url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    var icon = new BMapGL.Icon(url, new BMapGL.Size(size, size), {
      anchor: new BMapGL.Size(size / 2, size / 2)
    });
    return icon;
  }

  /* 解析输入：优先 lng,lat 坐标，否则视为地址文本 */
  function parseInput(text) {
    var m = /^\s*(-?\d+\.?\d*)\s*[,，\s]+(-?\d+\.?\d*)\s*$/.exec(text);
    if (m) return { lng: parseFloat(m[1]), lat: parseFloat(m[2]) };
    return null;
  }

  function distKm(lat1, lng1, lat2, lng2) {
    var R = 6371.0088;
    var dLat = (lat2 - lat1) * Math.PI / 180;
    var dLng = (lng2 - lng1) * Math.PI / 180;
    var a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
      Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
      Math.sin(dLng / 2) * Math.sin(dLng / 2);
    return 2 * R * Math.asin(Math.sqrt(a));
  }

  function fetchJson(url, opts) {
    return fetch(url, opts).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error((j && j.error) || ("HTTP " + r.status)); });
      return r.json();
    });
  }

  /* ---------------- 初始化 ---------------- */

  function init() {
    fetchJson("/api/config")
      .then(function (cfg) {
        config = cfg;
        fillSamples(cfg.samples);
        $("addr-input").value = cfg.default_center.name + "（" + cfg.default_center.lng + "," + cfg.default_center.lat + "）";
        if (!cfg.ak_configured || !cfg.ak) {
          banner.classList.remove("hidden");
          $("calc-btn").disabled = true;
          $("map-placeholder").classList.remove("hidden");
          $("map-placeholder").textContent = "未配置百度地图密钥，无法加载地图。请在环境变量 BAIDU_MAP_AK 配置后重启服务。";
          return;
        }
        loadBaiduScript(cfg.ak);
      })
      .catch(function (err) {
        showNotice("加载配置失败：" + err.message, 8);
        $("calc-btn").disabled = true;
      });
  }

  function fillSamples(samples) {
    var sel = $("sample-select");
    (samples || []).forEach(function (s) {
      var opt = document.createElement("option");
      opt.value = s.lng + "," + s.lat;
      opt.textContent = s.name;
      sel.appendChild(opt);
    });
  }

  function loadBaiduScript(ak) {
    var imgPlaceholder = $("map-placeholder");
    imgPlaceholder.classList.remove("hidden");
    var s = document.createElement("script");
    s.src = "https://api.map.baidu.com/api?v=1.0&type=webgl&ak=" + ak;
    s.onload = function () { setTimeout(initMap, 50); };
    s.onerror = function () {
      imgPlaceholder.textContent = "百度地图脚本加载失败，请检查网络或 AK 有效性。";
      showNotice("百度地图脚本加载失败，请检查 AK 是否有效。", 10);
    };
    document.head.appendChild(s);
  }

  /* ---------------- 地图 ---------------- */

  function initMap() {
    if (!window.BMapGL) {
      $("map-placeholder").textContent = "地图组件初始化失败。";
      return;
    }
    var center = config.default_center;
    map = new BMapGL.Map("map");
    map.centerAndZoom(new BMapGL.Point(center.lng, center.lat), 15);
    map.enableScrollWheelZoom(true);
    map.addControl(new BMapGL.ScaleControl());
    map.addControl(new BMapGL.NavigationControl3D());

    $("map-placeholder").classList.add("hidden");
    $("legend").classList.remove("hidden");
    renderLegend();
    $("calc-btn").disabled = false;
  }

  function renderLegend() {
    var items = $("legend-items");
    items.innerHTML = "";
    (config.categories || []).forEach(function (c) {
      var div = document.createElement("div");
      div.className = "legend-item";
      var dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = c.color;
      div.appendChild(dot);
      div.appendChild(document.createTextNode(c.name + "（≥" + c.threshold + "）"));
      items.appendChild(div);
    });
  }

  /* ---------------- 计算流程 ---------------- */

  $("sample-select").addEventListener("change", function () {
    var v = this.value;
    if (!v) return;
    var parts = v.split(",");
    $("addr-input").value = this.options[this.selectedIndex].text + "（" + v + "）";
    startCalc(parseFloat(parts[0]), parseFloat(parts[1]));
  });

  $("calc-btn").addEventListener("click", function () {
    var text = $("addr-input").value.trim();
    if (!text) { showNotice("请先输入地址或经纬度。", 6); return; }
    var coord = parseInput(text);
    if (coord) {
      startCalc(coord.lng, coord.lat);
    } else {
      fetchJson("/api/geocode?address=" + encodeURIComponent(text))
        .then(function (g) {
          startCalc(g.lng, g.lat);
        })
        .catch(function (err) {
          showNotice(err.message || "地址解析失败，请检查输入。", 8);
        });
    }
  });

  function startCalc(lng, lat) {
    hideNotice();
    clearOverlays();
    $("panel").classList.add("hidden");
    currentResult = null;

    var rays = parseInt($("rays-select").value, 10) || 36;
    var body = JSON.stringify({ lng: lng, lat: lat, minutes: 15, rays: rays, steps: 8 });

    fetchJson("/api/isochrone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body
    }).then(function (resp) {
      showProgress(resp.job_id);
    }).catch(function (err) {
      showNotice(err.message || "创建计算任务失败。", 8);
    });
  }

  function showProgress(jobId) {
    $("progress-wrap").classList.remove("hidden");
    $("progress-fill").style.width = "0%";
    $("progress-text").textContent = "排队中…";
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(function () { pollJob(jobId); }, 1200);
  }

  function pollJob(jobId) {
    fetchJson("/api/isochrone/" + jobId)
      .then(function (data) {
        if (data.status === "running") {
          $("progress-fill").style.width = (data.progress || 0) + "%";
          $("progress-text").textContent = data.stage || "计算中…";
        } else if (data.status === "done") {
          stopProgress();
          renderResult(data.result);
        } else {
          stopProgress();
          showNotice(data.message || "计算失败，请重试。", 10);
        }
      })
      .catch(function (err) {
        stopProgress();
        showNotice("查询任务状态失败：" + err.message, 8);
      });
  }

  function stopProgress() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    $("progress-wrap").classList.add("hidden");
  }

  /* ---------------- 结果渲染 ---------------- */

  function renderResult(result) {
    currentResult = result;
    if (!map) { $("panel").classList.remove("hidden"); renderReport(result); return; }

    clearOverlays();
    var center = result.center;

    // 等时圈退化提示
    if (result.isochrone.degenerate) {
      showNotice("该点周边路网无法形成有效的 15 分钟等时圈（面积过小），请调整中心点后重试。", 12);
    }

    // 等时圈多边形（半透明蓝色）
    var pts = result.isochrone.boundary.map(function (p) { return new BMapGL.Point(p[0], p[1]); });
    var poly = new BMapGL.Polygon(pts, {
      strokeColor: "#2563eb",
      strokeWeight: 2,
      strokeOpacity: 0.9,
      fillColor: "#3b82f6",
      fillOpacity: 0.22
    });
    map.addOverlay(poly);
    overlays.push(poly);

    // 中心点红色图钉 + 标签
    var centerPt = new BMapGL.Point(center.lng, center.lat);
    var marker = new BMapGL.Marker(centerPt, { icon: dotIcon("#dc2626", 18) });
    marker.setLabel(new BMapGL.Label("中心点", { position: centerPt, offset: new BMapGL.Size(10, -22) }));
    map.addOverlay(marker);
    overlays.push(marker);

    // POI 圆点，按类别颜色
    var colorMap = {};
    (config.categories || []).forEach(function (c) { colorMap[c.name] = c.color; });
    var pol = result.categories;
    var pois = result.pois || [];
    var simplified = pois.length > 1500;   // POI 过多时简化样式
    var dotSize = simplified ? 6 : 8;
    pois.forEach(function (p) {
      p.distance_km = distKm(center.lat, center.lng, p.lat, p.lng);
      var color = colorMap[p.category] || "#94a3b8";
      var m = new BMapGL.Marker(new BMapGL.Point(p.lng, p.lat), { icon: dotIcon(color, dotSize) });
      map.addOverlay(m);
      overlays.push(m);
      if (!simplified) {
        m.addEventListener("click", (function (poi) {
          return function () {
            var win = new BMapGL.InfoWindow(
              "<div style='font-size:12px;line-height:1.7'><b>" + poi.name + "</b><br>" +
              "类别：" + poi.category + "<br>" +
              (poi.address ? "地址：" + poi.address + "<br>" : "") +
              "距中心：" + (poi.distance_km ? poi.distance_km.toFixed(2) + " km" : "-") + "</div>",
              { width: 220, title: "设施" }
            );
            map.openInfoWindow(win, new BMapGL.Point(poi.lng, poi.lat));
          };
        })(p));
      }
    });

    // 灰色区域：矩形 Polygon（灰色描边 + 灰色半透明填充），点击弹窗显示缺失清单
    (result.gray_areas || []).forEach(function (ga) {
      var b = ga.bounds;
      if (!b) return;
      var rect = new BMapGL.Polygon([
        new BMapGL.Point(b.min_lng, b.min_lat),
        new BMapGL.Point(b.max_lng, b.min_lat),
        new BMapGL.Point(b.max_lng, b.max_lat),
        new BMapGL.Point(b.min_lng, b.max_lat)
      ], {
        strokeColor: "#4b5563",
        strokeWeight: 1.5,
        strokeOpacity: 0.9,
        fillColor: "#6b7280",
        fillOpacity: 0.35
      });
      rect.addEventListener("click", (function (g) {
        return function () {
          var missing = (g.missing && g.missing.length) ? g.missing.join("、") : "多项基础服务设施";
          var win = new BMapGL.InfoWindow(
            "<div style='font-size:12px;line-height:1.8'><b>灰色区域</b><br>" +
            "地址：" + (g.address || "未知") + "<br>" +
            "面积：约 " + g.area_km2.toFixed(3) + " km²<br>" +
            "500 米内缺失：<span style='color:#b45309'>" + missing + "</span></div>",
            { width: 240, title: "设施匮乏区域" }
          );
          map.openInfoWindow(win, new BMapGL.Point(g.centroid.lng, g.centroid.lat));
        };
      })(ga));
      map.addOverlay(rect);
      overlays.push(rect);
    });

    // 视野适配所有覆盖物
    map.setViewport(pts, { padding: 60 });
    renderReport(result);
  }

  /* ---------------- 报告渲染 ---------------- */

  function renderReport(r) {
    $("panel").classList.remove("hidden");

    // KPI 卡片
    var iso = r.isochrone;
    var kpiHtml =
      kpiCard("等时圈面积", iso.area_km2.toFixed(3) + " km²") +
      kpiCard("圈内 POI 总数", String(r.poi_total)) +
      kpiCard("达标类别", r.satisfied_count + " / 8") +
      kpiCard("健康指数", String(r.health_index), "health");
    $("kpis").innerHTML = kpiHtml;

    // 分类达标表
    var tbody = $("category-table").querySelector("tbody");
    tbody.innerHTML = "";
    r.categories.forEach(function (c) {
      var tr = document.createElement("tr");
      var ok = c.satisfied;
      tr.innerHTML =
        "<td>" + c.name + "</td>" +
        "<td>" + c.count + (c.threshold > 1 ? "（阈值" + c.threshold + "）" : "") + (ok ? "" : " <span class='pill-bad'>⚠ 匮乏项</span>") + "</td>" +
        "<td>" + (c.nearest_distance_m != null ? fmtDist(c.nearest_distance_m) : "—") + "</td>" +
        "<td>" + (c.walk_minutes != null ? c.walk_minutes + " 分钟" : "—") + "</td>" +
        "<td class='" + (ok ? "pill-ok" : "pill-bad") + "'>" + (ok ? "✓ 达标" : "⚠ 未达标") + "</td>";
      tbody.appendChild(tr);
    });

    // Canvas 条形图
    drawBarChart(r.categories, r.health_index);

    // 灰色区域列表
    var gb = $("gray-block");
    if (r.gray_areas && r.gray_areas.length) {
      gb.classList.remove("hidden");
      var gl = $("gray-list");
      gl.innerHTML = "";
      r.gray_areas.forEach(function (ga, i) {
        var div = document.createElement("div");
        div.className = "gray-card";
        var missing = ga.missing && ga.missing.length ? ga.missing.join("、") : "多项必需设施";
        div.innerHTML =
          "<div class='title'>区域 " + (i + 1) + " · " + (ga.address || "未知地址") + "</div>" +
          "网格点数 " + ga.points + "，面积约 " + ga.area_km2.toFixed(3) + " km²<br>" +
          "<span class='missing'>500 米内缺失：" + missing + "</span>";
        gl.appendChild(div);
      });
      var stat = r.gray_stats;
      $("gray-block").querySelector("h3").textContent =
        "灰色区域（" + stat.count + " 处 · 共 " + stat.total_area_m2.toFixed(0) + " m² · 占比 " + (Math.round(stat.ratio * 10000) / 100) + "%）";
    } else {
      gb.classList.add("hidden");
    }

    // 结论建议
    $("verdict").textContent = r.verdict || "";
    var ul = $("suggestions");
    ul.innerHTML = "";
    (r.suggestions || []).forEach(function (s) {
      var li = document.createElement("li");
      li.textContent = s;
      ul.appendChild(li);
    });
  }

  function kpiCard(label, value, extraCls) {
    return "<div class='kpi " + (extraCls || "") + "'><div class='value'>" + value +
      "</div><div class='label'>" + label + "</div></div>";
  }

  function fmtDist(m) {
    if (m == null) return "—";
    if (m >= 1000) return (m / 1000).toFixed(2) + " km";
    return Math.round(m) + " m";
  }

  /* 手绘 Canvas 条形图：8 类数量柱状图，带坐标轴和数值 */
  function drawBarChart(categories, healthIndex) {
    var canvas = $("bar-chart");
    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 500;
    var cssH = 240;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    canvas.style.width = cssW + "px";
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var padL = 34, padR = 12, padT = 16, padB = 34;
    var w = cssW - padL - padR;
    var h = cssH - padT - padB;
    var maxVal = 1;
    categories.forEach(function (c) { if (c.count > maxVal) maxVal = c.count; });
    maxVal = Math.ceil(maxVal / 2) * 2;

    // 坐标轴
    ctx.strokeStyle = "#cbd5e1";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, padT);
    ctx.lineTo(padL, padT + h);
    ctx.lineTo(padL + w, padT + h);
    ctx.stroke();

    // Y 轴刻度
    ctx.fillStyle = "#64748b";
    ctx.font = "11px sans-serif";
    ctx.textAlign = "right";
    for (var g = 0; g <= 4; g++) {
      var val = Math.round(maxVal * g / 4);
      var y = padT + h - (h * g / 4);
      ctx.fillText(String(val), padL - 6, y + 4);
      ctx.strokeStyle = "#eef2f7";
      ctx.beginPath();
      ctx.moveTo(padL, y);
      ctx.lineTo(padL + w, y);
      ctx.stroke();
    }

    var n = categories.length;
    var slot = w / n;
    var barW = Math.min(slot * 0.56, 36);
    categories.forEach(function (c, i) {
      var x = padL + slot * i + (slot - barW) / 2;
      var bh = h * (c.count / maxVal);
      var y = padT + h - bh;
      ctx.fillStyle = c.color;
      ctx.fillRect(x, y, barW, Math.max(bh, 1));
      // 数值
      ctx.fillStyle = "#334155";
      ctx.textAlign = "center";
      ctx.fillText(String(c.count), x + barW / 2, y - 4);
      // 类别名
      ctx.fillStyle = "#64748b";
      var name = c.name.length > 2 ? c.name.slice(0, 2) : c.name;
      ctx.fillText(name, x + barW / 2, padT + h + 14);
      ctx.fillText(c.satisfied ? "✓" : "⚠", x + barW / 2, padT + h + 27);
    });

    // 标题
    ctx.fillStyle = "#334155";
    ctx.textAlign = "left";
    ctx.font = "12px sans-serif";
    ctx.fillText("8 类民生设施数量（健康指数 " + healthIndex + "）", padL, 12);
  }

  /* 打印 */
  $("print-btn").addEventListener("click", function () {
    if (!currentResult) { showNotice("请先生成体检报告再打印。", 4); return; }
    window.print();
  });

  init();
})();