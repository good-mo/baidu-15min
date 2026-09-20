/* 15分钟便民生活圈体检报告 v3 — 前端逻辑（原生 JS + BMapGL）
   v1 功能（单圈/POI/灰色区域/健康指数）+ v2 功能（多时圈/阻抗系数/
   过街模型参数/障碍绘制/路况图层/问题清单诊疗）
   + v3 功能（坐标系下拉与坐标转换/等时圈热力图/1km 服务盲区核验） */
(function () {
  "use strict";

  var config = null;
  var map = null;
  var overlays = [];          // 所有动态覆盖物
  var currentResult = null;
  var pollTimer = null;
  var failedResources = [];   // 页面级资源加载失败诊断（脚本/CSS 等）

  // 捕获资源加载失败（SCRIPT/LINK 404、证书、拦截等），用于精确定位百度 SDK 问题
  window.addEventListener("error", function (e) {
    var tgt = e.target;
    if (tgt && (tgt.tagName === "SCRIPT" || tgt.tagName === "LINK" || tgt.tagName === "IMG")) {
      failedResources.push(tgt.src || tgt.href || tgt.currentSrc || "(inline)");
    }
  }, true);

  /* 收集加载失败的资源清单：优先刚才捕获的，再交叉核对 performance 条目中
     的 map.baidu.com 且非 2xx 请求（浏览器不支持 responseStatus 时按 0 处理）。 */
  function diagFailedResources() {
    var out = [];
    failedResources.forEach(function (u) {
      if (out.indexOf(u) < 0) out.push(u);
    });
    if (window.performance && performance.getEntriesByType) {
      performance.getEntriesByType("resource").forEach(function (r) {
        if (/map\.baidu\.com/.test(r.name)) {
          var st = r.responseStatus;
          if (st == null) st = r.transferSize === 0 && r.decodedBodySize === 0 ? "no-response" : "";
          var key = r.name + (st ? " [" + st + "]" : "");
          if ((st === "no-response" || (typeof st === "number" && (st < 200 || st >= 300))) && out.indexOf(key) < 0) {
            out.push(key);
          }
        }
      });
    }
    return out;
  }

  // v3 坐标系状态（默认 BD-09，样例街道固定 BD-09 不转换）
  var coordSys = "bd09ll";
  var COORD_SYS_LABEL = { bd09ll: "BD-09", gcj02: "GCJ-02", wgs84: "WGS-84" };

  // v2 障碍区状态
  var obstacles = [];         // [[{lng,lat},...], ...]
  var drawing = false;
  var tempPts = [];
  var drawHandlers = null;
  var trafficLayer = null;
  // v4 取点设中心状态
  var pickingCenter = false;
  var pickMarker = null;
  var heatLayer = null;

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

  /* 示例数据演示模式：百度 POI 日配额耗尽、无真实设施数据时，
     用内置样例填充看板（仪表盘/雷达图/柱状图/达标表），并明确标注演示。 */
  var DEMO_CATEGORIES = [
    { key: "medical",    name: "医疗",     color: "#ef4444", count: 4,  nearest_distance_m: 320,  walk_minutes: 4.5, threshold: 1, satisfied: true },
    { key: "commercial", name: "商业",     color: "#f97316", count: 12, nearest_distance_m: 150,  walk_minutes: 2.1, threshold: 3, satisfied: true },
    { key: "transport",  name: "交通",     color: "#8b5cf6", count: 6,  nearest_distance_m: 90,   walk_minutes: 1.4, threshold: 2, satisfied: true },
    { key: "education",  name: "教育",     color: "#3b82f6", count: 3,  nearest_distance_m: 480,  walk_minutes: 6.2, threshold: 1, satisfied: true },
    { key: "dining",     name: "餐饮",     color: "#eab308", count: 18, nearest_distance_m: 60,   walk_minutes: 1.1, threshold: 5, satisfied: true },
    { key: "finance",    name: "金融",     color: "#22c55e", count: 2,  nearest_distance_m: 410,  walk_minutes: 5.6, threshold: 1, satisfied: true },
    { key: "life",       name: "生活服务", color: "#06b6d4", count: 5,  nearest_distance_m: 260,  walk_minutes: 3.8, threshold: 2, satisfied: true },
    { key: "culture",    name: "文体",     color: "#ec4899", count: 1,  nearest_distance_m: 720,  walk_minutes: 9.3, threshold: 1, satisfied: true },
  ];
  var demoActive = false;
  var demoTimer = null;

  function demoHealthIndex() {
    var sum = 0, sat = 0;
    ([
      ["medical", .20], ["commercial", .15], ["transport", .15], ["education", .15],
      ["dining", .10], ["finance", .10], ["life", .10], ["culture", .05]
    ]).forEach(function (row) {
      var c = DEMO_CATEGORIES.filter(function (x) { return x.key === row[0]; })[0];
      if (c && c.satisfied) { sum += row[1] * 100; sat++; }
    });
    return { health: Math.round(sum * 10) / 10, satisfied: sat };
  }

  function enterDemoMode() {
    demoActive = true;
    $("demo-banner").classList.remove("hidden");
    $("poi-warning").classList.add("hidden");
    var h = demoHealthIndex();
    renderCategoryTable(DEMO_CATEGORIES);
    drawBarChart(DEMO_CATEGORIES, h.health);
    drawRadarChart(DEMO_CATEGORIES);
    drawGauge(h.health);
    showNotice("已切换为示例数据演示模式（内置样例），退出后可查看真实结果。", 8);
  }

  function renderCategoryTable(categories) {
    var tbody = $("category-table").querySelector("tbody");
    tbody.innerHTML = "";
    categories.forEach(function (c) {
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
  }

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

  /* v3：红色感叹号图标（较大=住宅小区源，较小=网格源） */
  function blindIcon(big) {
    var size = big ? 18 : 12;
    var svg =
      '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size + '">' +
      '<circle cx="' + size / 2 + '" cy="' + size / 2 + '" r="' + (size / 2 - 1) +
      '" fill="#dc2626" stroke="#ffffff" stroke-width="1"/>' +
      '<text x="' + size / 2 + '" y="' + (size * 0.78) + '" font-size="' + (size * 0.68) +
      '" font-weight="bold" fill="#ffffff" text-anchor="middle">!</text></svg>';
    var url = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    return new BMapGL.Icon(url, new BMapGL.Size(size, size), {
      anchor: new BMapGL.Size(size / 2, size / 2)
    });
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
        applyV2Defaults(cfg);
        $("addr-input").value = cfg.default_center.name + "（" + cfg.default_center.lng + "," + cfg.default_center.lat + "）";
        if (!cfg.ak_configured || !cfg.ak) {
          banner.classList.remove("hidden");
          $("calc-btn").disabled = true;
          $("draw-obstacle-btn").disabled = true;
          $("map-placeholder").classList.remove("hidden");
          $("map-placeholder").textContent = "未配置百度地图密钥，无法加载地图。请在环境变量 BAIDU_MAP_AK 配置后重启服务。";
          return;
        }
        startMap();
      })
      .catch(function (err) {
        showNotice("加载配置失败：" + err.message, 8);
        $("calc-btn").disabled = true;
      });
  }

  function applyV2Defaults(cfg) {
    var d = cfg.v2_defaults || {};
    if (d.crossing_sec != null) $("crossing-sec").value = d.crossing_sec;
    if (d.overpass_sec != null) $("overpass-sec").value = d.overpass_sec;
    if (d.walk_speed != null) $("walk-speed").value = d.walk_speed;
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

  /* 百度 SDK 已由 index.html 头部同步注入（AK 由后端渲染）。这里做就绪检测与失败提示。 */
  function startMap() {
    var ph = $("map-placeholder");
    ph.classList.remove("hidden");
    if (window.BMapGL) {
      ph.classList.add("hidden");
      initMap();
      return;
    }
    var tries = 0;
    var timer = setInterval(function () {
      if (window.__BAIDU_SDK_FAILED__) {
        clearInterval(timer);
        var diagF = diagFailedResources();
        var diagTxt = diagF.length ? " 具体失败资源：\n" + diagF.join("\n") : "";
        ph.textContent = "百度地图脚本加载失败，请检查 AK 是否有效。" + diagTxt;
        showNotice("百度地图脚本加载失败：请确认该 AK 已添加当前域名的 referer 白名单（开放平台-应用管理），且已开通「地图GL JS版」服务。" + diagTxt, 15);
        return;
      }
      if (window.BMapGL) {
        clearInterval(timer);
        ph.classList.add("hidden");
        initMap();
        return;
      }
      if (++tries > 60) {   // 12s 超时兜底
        clearInterval(timer);
        var diagT = diagFailedResources();
        var diagStr = diagT.length ? " 具体失败资源：\n" + diagT.join("\n") : "";
        ph.textContent = "地图组件初始化失败：请确认 AK 为浏览器端类型、已开通地图 GL JS 服务，且当前域已加入 referer 白名单（控制台可暂填 * 放行）。" + diagStr;
        showNotice("地图初始化失败：百度 SDK 已返回但 BMapGL 未就绪，最常见原因为 referer 白名单未放行或 GL 服务未开通。" + diagStr, 15);
      }
    }, 200);
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
    $("draw-obstacle-btn").disabled = false;
    $("pick-center-btn").disabled = false;
    // 单击地图即设中心（障碍绘制/取点设中心进行中时自动让位）
    map.addEventListener("click", function (e) {
      if (drawing || pickingCenter) return;
      setCenterFromMap(e.lnglat.lng, e.lnglat.lat);
    });
    initObstacleDrawing();
    initTrafficToggle();
    initPickCenter();
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
    if (config.residential_category) {
      var div = document.createElement("div");
      div.className = "legend-item";
      var dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = config.residential_category.color;
      div.appendChild(dot);
      div.appendChild(document.createTextNode("住宅（辅助）"));
      items.appendChild(div);
    }
    // v3：热力色阶 + 盲区
    [["#22c55e", "≤5 分钟（热力）"], ["#eab308", "≤10 分钟"], ["#f97316", "&gt;10 分钟"], ["#374151", "不可达"]].forEach(function (row) {
      var div = document.createElement("div");
      div.className = "legend-item";
      var sq = document.createElement("span");
      sq.className = "legend-sq";
      sq.style.background = row[0];
      div.appendChild(sq);
      div.appendChild(document.createTextNode(row[1]));
      items.appendChild(div);
    });
    var bd = document.createElement("div");
    bd.className = "legend-item";
    bd.innerHTML = "<span style='color:#dc2626;font-weight:800'>!</span> 1km 服务盲区";
    items.appendChild(bd);
  }

  /* v2：路况图层（视觉参考，不参与耗时计算）。
     若当前环境图块 URL 不生效或 BMapGL 不支持，则降级隐藏开关。 */
  function initTrafficToggle() {
    var cb = $("traffic-cb");
    cb.addEventListener("change", function () {
      if (!map) return;
      if (this.checked) {
        try {
          trafficLayer = new BMapGL.TileLayer({
            getTilesUrl: function (t) {
              return "https://maponline0.bdimg.com/tile/?qt=traffic&x=" + t.x +
                "&y=" + t.y + "&z=" + t.z + "&v=010&t=2";
            }
          });
          map.addTileLayer(trafficLayer);
        } catch (e) {
          console.warn("路况图层不可用：", e);
          this.checked = false;
          cb.parentElement.classList.add("hidden");
        }
      } else if (trafficLayer) {
        try { map.removeTileLayer(trafficLayer); } catch (e) { /* 忽略 */ }
        trafficLayer = null;
      }
    });
  }

  /* ---------------- 障碍区绘制（单击加点，双击闭合） ---------------- */

  function initObstacleDrawing() {
    $("draw-obstacle-btn").addEventListener("click", toggleDraw);
    $("clear-obstacle-btn").addEventListener("click", function () {
      obstacles = [];
      renderObstacleList();
      renderObstacleOverlays();
    });
    $("obstacle-items").addEventListener("click", function (e) {
      var btn = e.target;
      if (btn && btn.dataset && btn.dataset.idx !== undefined) {
        obstacles.splice(parseInt(btn.dataset.idx, 10), 1);
        renderObstacleList();
        renderObstacleOverlays();
      }
    });
  }

  function toggleDraw() {
    if (!map) return;
    if (pickingCenter) exitPickCenter();   // 互斥：先退出取点模式
    if (drawing) {
      finishDraw(false);
    } else {
      drawing = true;
      var btn = $("draw-obstacle-btn");
      btn.classList.add("active");
      btn.textContent = "完成绘制";
      $("draw-hint").classList.remove("hidden");
      drawHandlers = {
        click: addDrawPoint,
        dblclick: finishDrawByDbl
      };
      map.addEventListener("click", drawHandlers.click);
      map.addEventListener("dblclick", drawHandlers.dblclick);
    }
  }

  function addDrawPoint(e) {
    if (!drawing) return;
    tempPts.push({ lng: e.lnglat.lng, lat: e.lnglat.lat });
    var m = new BMapGL.Marker(new BMapGL.Point(tempPts[tempPts.length - 1].lng, tempPts[tempPts.length - 1].lat),
      { icon: dotIcon("#111827", 7) });
    map.addOverlay(m);
    overlays.push(m);
  }

  function finishDrawByDbl() {
    // 双击会先触发两个冗余 click（同一坐标），去掉最后一个重复点
    if (tempPts.length >= 2 && tempPts[tempPts.length - 1].lng === tempPts[tempPts.length - 2].lng &&
      tempPts[tempPts.length - 1].lat === tempPts[tempPts.length - 2].lat) {
      tempPts.pop();
    }
    finishDraw(false);
  }

  function finishDraw(save) {
    if (drawHandlers) {
      map.removeEventListener("click", drawHandlers.click);
      map.removeEventListener("dblclick", drawHandlers.dblclick);
      drawHandlers = null;
    }
    if (save && tempPts.length >= 3) {
      obstacles.push(tempPts.slice());
    } else if (save && tempPts.length < 3) {
      showNotice("障碍区至少需要 3 个顶点。", 5);
    }
    tempPts = [];
    drawing = false;
    var btn = $("draw-obstacle-btn");
    btn.classList.remove("active");
    btn.textContent = "添加障碍区";
    $("draw-hint").classList.add("hidden");
    renderObstacleList();
    renderObstacleOverlays();
  }

  function renderObstacleList() {
    var wrap = $("obstacle-list");
    var items = $("obstacle-items");
    if (!obstacles.length) {
      wrap.classList.add("hidden");
      return;
    }
    wrap.classList.remove("hidden");
    items.innerHTML = "";
    obstacles.forEach(function (poly, i) {
      var span = document.createElement("span");
      span.className = "obstacle-chip";
      span.innerHTML = "障碍 " + (i + 1) + "（" + poly.length + " 顶点）";
      var del = document.createElement("button");
      del.textContent = "×";
      del.dataset.idx = String(i);
      span.appendChild(del);
      items.appendChild(span);
    });
  }

  function renderObstacleOverlays() {
    if (!map) return;
    obstacles.forEach(function (poly) {
      if (poly.length < 3) return;
      var pts = poly.map(function (p) { return new BMapGL.Point(p.lng, p.lat); });
      var pg = new BMapGL.Polygon(pts, {
        strokeColor: "#1f2937",
        strokeWeight: 1.5,
        fillColor: "#374151",
        fillOpacity: 0.35
      });
      map.addOverlay(pg);
      overlays.push(pg);
    });
  }

  /* ---------------- v4：地图取点自定义中心点 ---------------- */

  function initPickCenter() {
    $("pick-center-btn").addEventListener("click", function () {
      if (pickingCenter) { exitPickCenter(); return; }
      if (drawing) finishDraw(false);          // 互斥：先结束障碍绘制
      pickingCenter = true;
      this.classList.add("active");
      this.textContent = "点地图选点（ESC 取消）";
      $("draw-hint").textContent = "取点模式：单击地图任意位置设为计算中心点";
      $("draw-hint").classList.remove("hidden");
      pickMarker = new BMapGL.Marker(map.getCenter(), { icon: dotIcon("#dc2626", 18) });
      pickMarker.setLabel(new BMapGL.Label("新中心点", { position: map.getCenter(), offset: new BMapGL.Size(10, -22) }));
      map.addOverlay(pickMarker);
      overlays.push(pickMarker);
      if (!pickHandlers) {
        pickHandlers = {
          click: onPickMapClick,
          keydown: onPickKeydown
        };
      }
      map.addEventListener("click", pickHandlers.click);
      document.addEventListener("keydown", pickHandlers.keydown);
    });
  }

  var pickHandlers = null;

  function onPickKeydown(e) {
    if (e && e.key === "Escape") exitPickCenter();
  }

  /* 把 lng/lat 设为计算中心点，并同步到输入框（单击地图 / 取点模式共用） */
  function setCenterFromMap(lng, lat) {
    $("addr-input").value = lng.toFixed(6) + "," + lat.toFixed(6);
    showNotice("已设置计算中心点：" + lng.toFixed(6) + "," + lat.toFixed(6) + "，点击「计算等时圈」开始分析。", 6);
  }

  function onPickMapClick(e) {
    if (!pickingCenter) return;
    var lng = e.lnglat.lng, lat = e.lnglat.lat;
    if (pickMarker) pickMarker.setPosition(new BMapGL.Point(lng, lat));
    setCenterFromMap(lng, lat);
  }

  function exitPickCenter() {
    if (!pickingCenter) return;
    pickingCenter = false;
    var btn = $("pick-center-btn");
    btn.classList.remove("active");
    btn.textContent = "取点设中心";
    $("draw-hint").classList.add("hidden");
    if (pickHandlers) {
      map.removeEventListener("click", pickHandlers.click);
      document.removeEventListener("keydown", pickHandlers.keydown);
    }
    if (pickMarker) {
      try { map.removeOverlay(pickMarker); } catch (err) { /* 忽略 */ }
      pickMarker = null;
    }
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
      ensureBd09(coord.lng, coord.lat, function (lng, lat) { startCalc(lng, lat); });
    } else {
      fetchJson("/api/geocode?address=" + encodeURIComponent(text))
        .then(function (g) {
          ensureBd09(g.lng, g.lat, function (lng, lat) { startCalc(lng, lat); });
        })
        .catch(function (err) {
          showNotice(err.message || "地址解析失败，请检查输入。", 8);
        });
    }
  });

  // v3：坐标系下拉（仅影响「输入坐标 / 地址解析结果」的换算；样例街道固定 BD-09 不转换）
  $("coord-sys-select").addEventListener("change", function () {
    coordSys = this.value;
    var label = COORD_SYS_LABEL[coordSys] || coordSys;
    if (coordSys === "bd09ll") {
      showNotice("坐标系已切换为 BD-09（百度，默认），无需转换。", 4);
    } else {
      showNotice("当前坐标系为 " + label + "，计算时将自动调用百度 geoconv 转换为 BD-09。", 5);
    }
  });

  // v3：非 BD-09 坐标 → 调用 /api/coords/convert 统一转 BD-09 后再计算
  function ensureBd09(lng, lat, cb) {
    if (coordSys === "bd09ll") { cb(lng, lat); return; }
    fetchJson("/api/coords/convert?coords=" + lng + "," + lat + "&from=" + coordSys)
      .then(function (res) {
        if (!res.coords || !res.coords.length) throw new Error("坐标转换无返回结果");
        var c = res.coords[0];
        showNotice("已将 " + (COORD_SYS_LABEL[coordSys] || coordSys) + " 坐标转换为 BD-09：" +
          c.lng.toFixed(6) + "," + c.lat.toFixed(6), 6);
        cb(c.lng, c.lat);
      })
      .catch(function (err) {
        showNotice("坐标转换失败：" + err.message + "（无百度密钥时请输入 BD-09 坐标）", 9);
      });
  }

  function startCalc(lng, lat) {
    hideNotice();
    clearOverlays();
    $("panel").classList.add("hidden");
    currentResult = null;

    var minutesRaw = $("minutes-select").value || "15";
    var minutes = minutesRaw.split(",").map(Number);
    var rays = parseInt($("rays-select").value, 10) || 36;
    var body = {
      lng: lng,
      lat: lat,
      minutes: minutes.length > 1 ? minutes : minutes[0],
      rays: rays,
      steps: 8,
      walk_speed: parseFloat($("walk-speed").value) || 1.2,
      crossing_sec: parseInt($("crossing-sec").value, 10) || 0,
      overpass_sec: parseInt($("overpass-sec").value, 10) || 0,
      heat_exact: $("heat-exact-cb").checked,
      obstacles: obstacles.map(function (poly) {
        return poly.map(function (p) { return { lng: p.lng, lat: p.lat }; });
      })
    };

    fetchJson("/api/isochrone", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
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

  function ringColor(idx) {
    var palette = ["#bfdbfe", "#93c5fd", "#60a5fa", "#3b82f6"];
    return palette[Math.min(idx, palette.length - 1)];
  }

  function renderResult(result) {
    currentResult = result;
    if (!map) { $("panel").classList.remove("hidden"); renderReport(result); return; }

    clearOverlays();
    var center = result.center;
    var rings = result.rings || [];
    var mainRing = rings[rings.length - 1] || {};

    // 等时圈退化提示（主圈面积过小）
    if (mainRing.degenerate) {
      showNotice("该点周边路网无法形成有效的 15 分钟等时圈（面积过小），请调整中心点后重试。", 12);
    }

    // 多时圈：颜色随分钟数由浅到深
    var allPoints = [];
    rings.forEach(function (ring, idx) {
      var pts = ring.boundary.map(function (p) { return new BMapGL.Point(p[0], p[1]); });
      allPoints = allPoints.concat(pts);
      var color = ringColor(idx);
      var poly = new BMapGL.Polygon(pts, {
        strokeColor: color,
        strokeWeight: 2,
        strokeOpacity: 0.9,
        fillColor: color,
        fillOpacity: 0.14 + idx * 0.05
      });
      map.addOverlay(poly);
      overlays.push(poly);
    });

    // v4 热力：真实路网耗时热力叠加层（Canvas 高斯叠加 → GroundOverlay）+ 小圆点辅助
    renderHeatLayer(result.heat || []);

    // 辅助圆点（覆盖热力叠加层之上，便于核对采样位置与分级）
    (result.heat || []).forEach(function (h) {
      var color = h.minutes == null ? "#374151"
        : h.minutes <= 5 ? "#22c55e"
        : h.minutes <= 10 ? "#eab308"
        : "#f97316";
      var m = new BMapGL.Marker(new BMapGL.Point(h.lng, h.lat), { icon: dotIcon(color, 5) });
      map.addOverlay(m);
      overlays.push(m);
    });

    // 中心点红色图钉 + 标签
    var centerPt = new BMapGL.Point(center.lng, center.lat);
    var marker = new BMapGL.Marker(centerPt, { icon: dotIcon("#dc2626", 18) });
    marker.setLabel(new BMapGL.Label("中心点", { position: centerPt, offset: new BMapGL.Size(10, -22) }));
    map.addOverlay(marker);
    overlays.push(marker);

    // POI 圆点，按类别颜色
    var colorMap = {};
    (config.categories || []).forEach(function (c) { colorMap[c.name] = c.color; });
    if (config.residential_category) colorMap["住宅"] = config.residential_category.color;
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

    // 灰色区域矩形（点击弹窗显示诊疗信息）
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
            "<div style='font-size:12px;line-height:1.8'><b>灰色区域 · " + g.priority + "优先级</b><br>" +
            "地址：" + (g.address || "未知") + "<br>" +
            "面积：约 " + g.area_km2.toFixed(3) + " km²<br>" +
            "影响规模：" + levelText(g.affected_level) + "（500m 内 " + g.affected_res_count + " 个住宅）<br>" +
            "严重度：" + g.severity + "<br>" +
            "500 米内缺失：<span style='color:#b45309'>" + missing + "</span></div>",
            { width: 250, title: "设施匮乏区域" }
          );
          map.openInfoWindow(win, new BMapGL.Point(g.centroid.lng, g.centroid.lat));
        };
      })(ga));
      map.addOverlay(rect);
      overlays.push(rect);
    });

    // 障碍多边形（meta 回传，重绘；带说明）
    (result.meta && result.meta.obstacles || []).forEach(function (poly) {
      if (!poly || poly.length < 3) return;
      var pts = poly.map(function (p) { return new BMapGL.Point(p[0], p[1]); });
      var pg = new BMapGL.Polygon(pts, {
        strokeColor: "#111827",
        strokeWeight: 2,
        fillColor: "#1f2937",
        fillOpacity: 0.45
      });
      pg.addEventListener("click", function () {
        map.openInfoWindow(new BMapGL.InfoWindow(
          "模拟施工障碍区（假设分析，非实时施工数据）", { width: 220, title: "障碍" }),
          pointsCenter(pts));
      });
      map.addOverlay(pg);
      overlays.push(pg);
    });

    // v3 1km 服务盲区标记（红色感叹号；住宅源大、网格源小，点击查看三类设施最近距离）
    (result.blind_spots && result.blind_spots.points || []).forEach(function (b) {
      var big = b.source === "住宅小区";
      var m = new BMapGL.Marker(new BMapGL.Point(b.lng, b.lat), { icon: blindIcon(big) });
      m.addEventListener("click", (function (pt) {
        return function () {
          var near = pt.nearest || {};
          var rows = (config.blind_spot_categories || ["菜市场", "药店", "小学"]).map(function (c) {
            var d = near[c];
            return c + "：" + (d != null ? (d >= 1000 ? (d / 1000).toFixed(2) + " km" : Math.round(d) + " m") : "无");
          }).join(" / ");
          var win = new BMapGL.InfoWindow(
            "<div style='font-size:12px;line-height:1.8'><b>1km 服务盲区 · " +
            (pt.source === "住宅小区" ? "住宅小区" : "网格点位") + "</b><br>" +
            "坐标：" + pt.lng.toFixed(6) + "," + pt.lat.toFixed(6) + "<br>" +
            "1 公里内：<span style='color:#b91c1c'>三类设施均缺失</span><br>" +
            "最近设施：" + rows + "</div>",
            { width: 260, title: "服务盲区" }
          );
          map.openInfoWindow(win, new BMapGL.Point(pt.lng, pt.lat));
        };
      })(b));
      map.addOverlay(m);
      overlays.push(m);
    });

    if (allPoints.length) map.setViewport(allPoints, { padding: 60 });
    renderReport(result);
  }

  /* ---------------- v4：热力叠加层（Canvas 高斯叠加） ---------------- */

  function heatColor(minutes, alpha) {
    // 三阶渐变：绿(0~5) → 黄(5~10) → 橙(10~15) → 红(>15)
    var stops = [
      [0, [34, 197, 94]],
      [5, [234, 179, 8]],
      [10, [249, 115, 22]],
      [15, [220, 38, 38]]
    ];
    var t = Math.max(0, Math.min(minutes, 20));
    var col = stops[stops.length - 1][1];
    for (var i = 1; i < stops.length; i++) {
      if (t <= stops[i][0]) {
        var a = stops[i - 1], b = stops[i];
        var k = (t - a[0]) / Math.max(1e-6, b[0] - a[0]);
        col = [
          Math.round(a[1][0] + (b[1][0] - a[1][0]) * k),
          Math.round(a[1][1] + (b[1][1] - a[1][1]) * k),
          Math.round(a[1][2] + (b[1][2] - a[1][2]) * k)
        ];
        break;
      }
    }
    return "rgba(" + col[0] + "," + col[1] + "," + col[2] + "," + alpha + ")";
  }

  function renderHeatLayer(heat) {
    if (!map || !window.BMapGL || !BMapGL.GroundOverlay) return;
    var ok = (heat || []).filter(function (h) { return h.minutes != null; });
    if (!ok.length) return;
    try {
      var minLng = Infinity, minLat = Infinity, maxLng = -Infinity, maxLat = -Infinity;
      ok.forEach(function (h) {
        if (h.lng < minLng) minLng = h.lng;
        if (h.lat < minLat) minLat = h.lat;
        if (h.lng > maxLng) maxLng = h.lng;
        if (h.lat > maxLat) maxLat = h.lat;
      });
      var midLat = (minLat + maxLat) / 2;
      var lat1m = 1 / 111320;
      var lng1m = 1 / (111320 * Math.cos(midLat * Math.PI / 180));
      var wM = (maxLng - minLng) / lng1m;
      var hM = (maxLat - minLat) / lat1m;
      var W = 640;
      var H = Math.max(48, Math.round(W * hM / Math.max(wM, 1e-6)));
      var cv = document.createElement("canvas");
      cv.width = W; cv.height = H;
      var ctx = cv.getContext("2d");
      var R = Math.max(8, Math.round(W / wM * 26));   // 高斯核半径 ≈ 26m 网格覆盖
      ok.forEach(function (h) {
        var x = (h.lng - minLng) / lng1m / wM * W;
        var y = H - (h.lat - minLat) / lat1m / hM * H;
        var g = ctx.createRadialGradient(x, y, 0, x, y, R);
        g.addColorStop(0, heatColor(h.minutes, 0.62));
        g.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = g;
        ctx.fillRect(x - R, y - R, R * 2, R * 2);
      });
      var url = cv.toDataURL("image/png");
      var bounds = new BMapGL.Bounds(new BMapGL.Point(minLng, minLat), new BMapGL.Point(maxLng, maxLat));
      heatLayer = new BMapGL.GroundOverlay(bounds, {
        type: "image",
        url: url,
        opacity: 0.62,
        clickable: false
      });
      map.addOverlay(heatLayer);
      overlays.push(heatLayer);
    } catch (e) {
      heatLayer = null;
      console.warn("热力叠加层渲染失败，已保留采样圆点：", e);
    }
  }

  function levelText(level) {
    return level === "high" ? "高" : level === "mid" ? "中" : "低";
  }

  function pointsCenter(pts) {
    var lng = 0, lat = 0;
    pts.forEach(function (p) { lng += p.lng; lat += p.lat; });
    return new BMapGL.Point(lng / pts.length, lat / pts.length);
  }

  /* ---------------- 报告渲染 ---------------- */

  function renderReport(r) {
    $("panel").classList.remove("hidden");

    var rings = r.rings || [];
    var mainRing = rings[rings.length - 1] || {};
    var crossing = mainRing.crossing || {};
    var crossWaitMin = (crossing.crossing_wait_sec || 0) + (crossing.overpass_wait_sec || 0);
    var meta = r.meta || {};

    var kpiHtml =
      kpiCard("等时圈面积", (r.area_km2 || 0).toFixed(3) + " km²") +
      kpiCard("圈内 POI 总数", String(r.poi_total)) +
      kpiCard("达标类别", r.satisfied_count + " / " + (r.categories || []).length) +
      kpiCard("健康指数", String(r.health_index), "health") +
      kpiCard("阻抗系数", (mainRing.impedance_coef != null ? mainRing.impedance_coef.toFixed(2) : "-"),
        "", "路网面积 / 直线圆面积") +
      kpiCard("过街等待估算", (crossWaitMin > 0 ? "约 " + (crossWaitMin / 60).toFixed(1) + " 分钟" : "无"),
        "", "主圈 36 条射线过街/天桥估算之和");
    var hs = r.heat_stats || {};
    if (hs.avg_minutes != null) {
      kpiHtml += kpiCard("圈内平均步行耗时", hs.avg_minutes + " 分钟",
        "", "热力网格采样（约 " + (hs.step_m || 100) + " m 步长）");
    }
    if (hs.gt10_ratio != null) {
      kpiHtml += kpiCard("&gt;10 分钟区域占比", (Math.round(hs.gt10_ratio * 10000) / 100) + "%",
        "", "占可步行采样点比例");
    }
    $("kpis").innerHTML = kpiHtml;

    // POI 配额提示：百度 302/401 超限时设施统计为空，明确告知而非显示为 0
    var pw = $("poi-warning");
    var poiWarn = meta.poi_warning || {};
    $("poi-warning-text").textContent = poiWarn.message || "未能获取周边设施（POI）数据，设备统计与健康指数暂不可用。请稍后重试或更换密钥。";
    var demoBtn = $("enter-demo-btn");
    demoBtn.onclick = null;
    demoBtn.onclick = function () { enterDemoMode(); };
    if (demoActive) {
      pw.classList.add("hidden");
      $("demo-banner").classList.remove("hidden");
    } else if (!r.poi_total && (poiWarn.quota_exhausted || r.health_index === 0 && r.satisfied_count === 0)) {
      pw.classList.remove("hidden");
      $("demo-banner").classList.add("hidden");
    } else if (r.poi_total) {
      pw.classList.add("hidden");
      $("demo-banner").classList.add("hidden");
    }

    // 热力说明（报告标注：热力为网格采样 + 实际精度）
    var hn = $("heat-note");
    if (r.heat && r.heat.length) {
      hn.classList.remove("hidden");
      var hMode = hs.heat_mode || (meta.heat_exact === false ? "interp" : "exact");
      var hTxt = hMode === "interp" ? "已按射线插值估算（不增加 API 配额）。"
        : hMode === "exact+interp" ? "步行 API 精确计算为主，个别失败点已用射线插值补齐。"
          : "已按步行 API 精确计算（含过街模型）。";
      hn.textContent = "热力为网格采样（约 " + (hs.step_m || 100) + " m 步长）" + hTxt;
    } else {
      hn.classList.add("hidden");
    }

    // 分类达标表
    var tbody = $("category-table").querySelector("tbody");
    tbody.innerHTML = "";
    (r.categories || []).forEach(function (c) {
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

    // Canvas 条形图 + v4 雷达图 + 健康指数仪表盘
    drawBarChart(r.categories || [], r.health_index);
    drawRadarChart(r.categories || []);
    drawGauge(r.health_index || 0);

    // 问题清单（灰色区域自动诊疗）
    var gb = $("gray-block");
    if (r.gray_areas && r.gray_areas.length) {
      gb.classList.remove("hidden");
      var gl = $("gray-list");
      gl.innerHTML = "";
      r.gray_areas.forEach(function (ga, i) {
        var div = document.createElement("div");
        div.className = "gray-card";
        var missing = ga.missing && ga.missing.length ? ga.missing.join("、") : "多项必需设施";
        var sugg = (ga.suggestions && ga.suggestions.length) ? ga.suggestions.join("；") : "";
        div.innerHTML =
          "<div class='title'>区域 " + (i + 1) + " · " + (ga.address || "未知地址") +
          " <span class='priority-badge priority-" + ga.priority + "'>" + ga.priority + "</span></div>" +
          "<div class='meta'>" +
          "<span>面积 约 " + ga.area_km2.toFixed(3) + " km²</span>" +
          "<span>影响规模 <span class='sev-tag'>" + levelText(ga.affected_level) + "</span>（500m 内 " + ga.affected_res_count + " 个住宅）</span>" +
          "<span>严重度 <span class='sev-tag sev-" + ga.severity + "'>" + ga.severity + "</span></span>" +
          "</div>" +
          "<div><span class='missing'>500 米内缺失：" + missing + "</span></div>" +
          (sugg ? "<div class='sugg'>" + sugg + "</div>" : "");
        gl.appendChild(div);
      });
      var stat = r.gray_stats || {};
      gb.querySelector("h3").textContent =
        "问题清单（灰色区域 " + stat.count + " 处 · 共 " + fmtArea(stat.total_area_m2) +
        " · 占等时圈 " + (Math.round((stat.ratio || 0) * 10000) / 100) + "%）";
    } else {
      gb.classList.add("hidden");
    }

    // v3 1km 服务盲区核验
    var bb = $("blind-block");
    var bs = r.blind_spots;
    var bl = $("blind-list");
    if (bs && bs.count > 0) {
      bb.classList.remove("hidden");
      bl.innerHTML = "";
      var sum = document.createElement("div");
      sum.className = "blind-summary";
      sum.textContent = "共 " + bs.count + " 个服务盲区点位（含 " + bs.affected_housing_count +
        " 个住宅小区源点），其 1km 直线范围内菜市场/药店/小学三类设施均缺失。";
      bl.appendChild(sum);
      bs.points.slice(0, 8).forEach(function (p) {
        var div = document.createElement("div");
        div.className = "blind-card";
        var near = p.nearest || {};
        var facs = (config.blind_spot_categories || ["菜市场", "药店", "小学"]).map(function (c) {
          var d = near[c];
          var txt = d != null ? (d >= 1000 ? (d / 1000).toFixed(2) + " km" : Math.round(d) + " m") : "无";
          return "<span class='" + (d != null ? "fac-ok" : "fac-miss") + "'>" + c + " " + txt + "</span>";
        }).join("");
        div.innerHTML =
          "<div class='blind-kind'>" + (p.source === "住宅小区" ? "住宅小区 · " : "网格点位 · ") + p.lng.toFixed(5) + "," + p.lat.toFixed(5) + "</div>" +
          "<div class='fac-ruler'>1km 内最近：" + facs + "</div>";
        bl.appendChild(div);
      });
      if (bs.points.length > 8) {
        var mor = document.createElement("div");
        mor.className = "hint";
        mor.textContent = "…其余 " + (bs.points.length - 8) + " 个盲区点位请在地图上查看红点。";
        bl.appendChild(mor);
      }
    } else {
      bb.classList.add("hidden");
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

    // 元信息说明（诚实标注）
    var mn = $("meta-note");
    var notes = [];
    if (meta.crossing_mode === "heuristic") {
      notes.push("过街等待为估算模型：红绿灯 " + meta.crossing_sec + " 秒/次、天桥/地道 " +
        meta.overpass_sec + " 秒/次（参数可调），非实测数据。");
    } else {
      notes.push("当前接口未返回路段明细，过街估算不可用（过街成本按 0 处理）。");
    }
    if (meta.obstacles && meta.obstacles.length) {
      notes.push("已开启施工障碍模拟（假设分析工具，非实时施工数据）。");
    }
    if (meta.v2) {
      notes.push("步行速度 " + meta.walk_speed + " m/s（直线参照圆口径）；阻抗系数 = 路网等时圈面积/直线圆面积，越小越表示路网绕行严重。");
    }
    if (meta.v3) {
      notes.push("热力为网格采样（约 " + (r.heat_stats && r.heat_stats.step_m || 100) + " m 步长）" +
        ((r.heat_stats && r.heat_stats.heat_mode) === "interp" ? "按射线插值估算（节省 API 配额）。"
          : (r.heat_stats && r.heat_stats.heat_mode) === "exact+interp" ? "步行 API 精确计算为主，失败点已射线插值补齐。"
          : "按步行 API 精确计算（含过街模型）。") +
        " 盲区核验为 1km 直线口径（菜市场/药店/小学），与灰色区域（500m 网格覆盖分析）口径不同、两者并存。");
    }
    if (notes.length) {
      mn.classList.remove("hidden");
      mn.innerHTML = notes.join("<br>");
    } else {
      mn.classList.add("hidden");
    }
  }

  function kpiCard(label, value, extraCls, note) {
    return "<div class='kpi " + (extraCls || "") + "'><div class='value'>" + value +
      "</div><div class='label'>" + label + "</div>" +
      (note ? "<div class='kpi-card-note'>" + note + "</div>" : "") + "</div>";
  }

  function fmtDist(m) {
    if (m == null) return "—";
    if (m >= 1000) return (m / 1000).toFixed(2) + " km";
    return Math.round(m) + " m";
  }

  function fmtArea(m2) {
    if (m2 == null) return "0 m²";
    if (m2 >= 1e6) return (m2 / 1e6).toFixed(2) + " km²";
    return Math.round(m2) + " m²";
  }

  /* v4 雷达图：8 类设施覆盖度（达标=满值，未达标按 数量/阈值 归一化） */
  function drawRadarChart(categories) {
    var canvas = $("radar-canvas");
    if (!canvas) return;
    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 300;
    var cssH = 240;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    canvas.style.width = cssW + "px";
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var n = categories.length;
    if (!n) return;
    var cx = cssW / 2, cy = cssH / 2 + 8;
    var R = Math.min(cssW, cssH) / 2 - 30;
    var padT = 26;
    var valueOf = function (c) {
      if (c.satisfied) return 1;
      return Math.min(0.15 + c.count / Math.max(c.threshold, 1) * 0.85, 0.55);
    };

    function pt(i, v) {
      var a = -Math.PI / 2 + i * 2 * Math.PI / n;
      return [cx + Math.cos(a) * R * v, cy + Math.sin(a) * R * v];
    }

    // 网格（0.25/0.5/0.75/1.0）
    ctx.strokeStyle = "#e2e8f0";
    ctx.lineWidth = 1;
    for (var g = 0.25; g <= 1.001; g += 0.25) {
      ctx.beginPath();
      for (var i = 0; i <= n; i++) {
        var p = pt(i % n, g);
        if (i === 0) ctx.moveTo(p[0], p[1]); else ctx.lineTo(p[0], p[1]);
      }
      ctx.stroke();
    }
    // 轴线
    ctx.strokeStyle = "#cbd5e1";
    for (var j = 0; j < n; j++) {
      var a2 = pt(j, 1);
      var a1 = pt(j, 0);
      ctx.beginPath();
      ctx.moveTo(a1[0], a1[1]);
      ctx.lineTo(a2[0], a2[1]);
      ctx.stroke();
    }
    // 数据多边形
    ctx.beginPath();
    for (var k = 0; k < n; k++) {
      var dp = pt(k, valueOf(categories[k]));
      if (k === 0) ctx.moveTo(dp[0], dp[1]); else ctx.lineTo(dp[0], dp[1]);
    }
    ctx.closePath();
    ctx.fillStyle = "rgba(59,130,246,0.22)";
    ctx.fill();
    ctx.strokeStyle = "#3b82f6";
    ctx.lineWidth = 1.6;
    ctx.stroke();
    // 顶点 + 标签
    ctx.font = "11px sans-serif";
    ctx.textAlign = "center";
    categories.forEach(function (c, i) {
      var dp = pt(i, valueOf(c));
      ctx.fillStyle = c.color || "#3b82f6";
      ctx.beginPath();
      ctx.arc(dp[0], dp[1], 3, 0, 2 * Math.PI);
      ctx.fill();
      var lp = pt(i, 1.18);
      ctx.fillStyle = "#334155";
      ctx.fillText(c.name.length > 2 ? c.name.slice(0, 2) : c.name, lp[0], lp[1] + 4);
    });
    ctx.fillStyle = "#64748b";
    ctx.font = "12px sans-serif";
    ctx.fillText("8 类设施覆盖度（外圈=达标）", cx, 16);
  }

  /* v4 健康指数仪表盘：270° 环形进度 */
  function drawGauge(health) {
    var canvas = $("gauge-canvas");
    if (!canvas) return;
    var dpr = window.devicePixelRatio || 1;
    var cssW = canvas.clientWidth || 200;
    var cssH = 160;
    canvas.width = cssW * dpr;
    canvas.height = cssH * dpr;
    canvas.style.width = cssW + "px";
    canvas.style.height = cssH + "px";
    var ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var cx = cssW / 2, cy = cssH - 28;
    var R = Math.min(cssW / 2 - 14, cssH - 46);
    var start = Math.PI * 0.75, end = Math.PI * 2.25;   // 270°
    var frac = Math.max(0, Math.min(health / 100, 1));

    function arc(a0, a1, color, width) {
      ctx.beginPath();
      ctx.arc(cx, cy, R, a0, a1);
      ctx.strokeStyle = color;
      ctx.lineWidth = width;
      ctx.lineCap = "round";
      ctx.stroke();
    }
    arc(start, end, "#e5e7eb", 16);
    var col = health >= 70 ? "#22c55e" : health >= 50 ? "#eab308" : "#ef4444";
    arc(start, start + (end - start) * frac, col, 16);
    // 刻度
    for (var i = 0; i <= 5; i++) {
      var a = start + (end - start) * i / 5;
      arc(a, a - 0.02, "#ffffff", 2);
    }
    // 数值与标签
    ctx.fillStyle = col;
    ctx.font = "bold 30px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(String(Math.round(health)), cx, cy - 8);
    ctx.fillStyle = "#475569";
    ctx.font = "12px sans-serif";
    ctx.fillText("健康指数（满分 100）", cx, cy + 24);
    var v = health >= 70 ? "宜居便利" : health >= 50 ? "基本可用" : "设施匮乏";
    ctx.fillStyle = col;
    ctx.fillText(v, cx, cy + 42);
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

    ctx.strokeStyle = "#cbd5e1";
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(padL, padT);
    ctx.lineTo(padL, padT + h);
    ctx.lineTo(padL + w, padT + h);
    ctx.stroke();

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
      ctx.fillStyle = "#334155";
      ctx.textAlign = "center";
      ctx.fillText(String(c.count), x + barW / 2, y - 4);
      ctx.fillStyle = "#64748b";
      var name = c.name.length > 2 ? c.name.slice(0, 2) : c.name;
      ctx.fillText(name, x + barW / 2, padT + h + 14);
      ctx.fillText(c.satisfied ? "✓" : "⚠", x + barW / 2, padT + h + 27);
    });

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

  /* 退出示例演示：隐藏横幅、重绘真实（或空缺）看板 */
  $("leave-demo-btn").addEventListener("click", function () {
    demoActive = false;
    $("demo-banner").classList.add("hidden");
    if (currentResult) {
      var rr = currentResult;
      var pw2 = rr.meta && (rr.meta.poi_warning || {});
      if (!rr.poi_total && (pw2 && pw2.quota_exhausted || rr.health_index === 0 && rr.satisfied_count === 0)) {
        $("poi-warning").classList.remove("hidden");
      }
      var h0 = rr.health_index || 0;
      renderCategoryTable(rr.categories || []);
      drawBarChart(rr.categories || [], h0);
      drawRadarChart(rr.categories || []);
      drawGauge(h0);
    }
    showNotice("已退出示例演示模式。设施数据仍不可用，可稍后重试或更换密钥。", 7);
  });

  init();
})();