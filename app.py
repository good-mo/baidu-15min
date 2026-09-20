#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
15分钟便民生活圈体检报告 — Flask 后端

基于百度地图开放能力（地理编码 / 逆地理编码 / POI 检索 / 步行路线规划）：
  - 真实路网计算 15 分钟步行等时圈（射线二分法）
  - 统计圈内 8 类民生设施覆盖并输出体检报告
  - 自动标注设施匮乏的「灰色区域」

运行方式：python app.py --port <PORT>（默认 5000，监听 0.0.0.0）
环境变量：BAIDU_MAP_AK（必填，从 lbsyun.baidu.com 申请）
"""

import argparse
import json
import math
import os
import threading
import time
import uuid

import requests
from flask import Flask, jsonify, request

# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------
BAIDU_AK = os.environ.get("BAIDU_MAP_AK", "").strip()
BAIDU_BASE = "https://api.map.baidu.com"

# 默认中心点：深圳南山科技园（粤海街道）
DEFAULT_CENTER = {"lng": 113.9458, "lat": 22.5398, "name": "深圳南山科技园（粤海街道）"}
SAMPLE_STREETS = [
    {"name": "深圳市南山区粤海街道（科技园）", "lng": 113.9458, "lat": 22.5398},
    {"name": "成都市成华区猛追湾街道", "lng": 104.1012, "lat": 30.6612},
    {"name": "北京市海淀区中关村街道", "lng": 116.3155, "lat": 39.9843},
]

DEFAULT_MINUTES = 15
DEFAULT_RAYS = 36
DEFAULT_STEPS = 8          # 二分迭代次数，约 10 米精度
MAX_SEARCH_DIST = 2500.0   # 等时圈最大搜索距离（米）
POI_RADIUS = 2500          # POI 检索半径（米），覆盖等时圈
INF_TRAVEL = 24 * 3600     # 不可达时视为 24 小时

# 限流：每次百度 API 调用前休眠 0.15s（单线程顺序调用）
API_SLEEP = 0.15
API_TIMEOUT = 5            # 百度 API 超时（秒）
API_RETRIES = 1            # 失败重试次数

# 8 类民生设施：关键词/达标阈值/健康指数权重
CATEGORIES = [
    {"key": "medical",    "name": "医疗",     "keywords": ["医院", "药店", "诊所"],       "threshold": 1, "weight": 0.20, "color": "#ef4444"},
    {"key": "commercial", "name": "商业",     "keywords": ["超市", "便利店", "菜市场"],   "threshold": 3, "weight": 0.15, "color": "#f97316"},
    {"key": "transport",  "name": "交通",     "keywords": ["公交站", "地铁站"],           "threshold": 2, "weight": 0.15, "color": "#8b5cf6"},
    {"key": "education",  "name": "教育",     "keywords": ["小学", "中学", "幼儿园", "大学"], "threshold": 1, "weight": 0.15, "color": "#3b82f6"},
    {"key": "dining",     "name": "餐饮",     "keywords": ["美食", "餐厅"],               "threshold": 5, "weight": 0.10, "color": "#eab308"},
    {"key": "finance",    "name": "金融",     "keywords": ["银行", "ATM"],                "threshold": 1, "weight": 0.10, "color": "#22c55e"},
    {"key": "life",       "name": "生活服务", "keywords": ["快递", "理发店", "洗衣店"],   "threshold": 2, "weight": 0.10, "color": "#06b6d4"},
    {"key": "culture",    "name": "文体",     "keywords": ["公园", "体育馆", "图书馆", "健身房"], "threshold": 1, "weight": 0.05, "color": "#ec4899"},
]

# 灰色区域检测用的 5 类「日常必需设施」：类别 key + 名称过滤关键字
REQUIRED_FACILITY_RULES = [
    ("医疗",   ["药店", "医院"]),
    ("商业",   ["超市", "便利店"]),
    ("教育",   ["小学", "中学", "幼儿园", "大学"]),
    ("交通",   ["公交", "地铁"]),
]
REQUIRED_FACILITY_LABELS = ["药店/医院", "超市/便利店", "学校", "公交/地铁站"]

GRAY_GRID_STEP = 100.0    # 灰色检测网格步长（米）
GRAY_DIST = 500.0         # 必需设施缺失距离阈值（米）
GRAY_MIN_POINTS = 3       # 碎片簇过滤：点数 < 3 丢弃

# ---------------------------------------------------------------------------
# Flask 应用
# ---------------------------------------------------------------------------
app = Flask(__name__, static_folder="static", static_url_path="/static")

# 异步任务表 + 锁 + 结果缓存
_jobs = {}
_jobs_lock = threading.Lock()
_results_cache = {}   # key -> job_id（已完成的可直接复用）
_walk_cache = {}      # 步行算路内存缓存

# 百度坐标系参数（bd09ll）
LAT_1M = 1.0 / 111320.0


def _lng_1m(lat):
    """该纬度下每米对应的经度度数"""
    return 1.0 / (111320.0 * math.cos(math.radians(lat)))


# ---------------------------------------------------------------------------
# 百度 API 封装
# ---------------------------------------------------------------------------
def baidu_get(path, params):
    """
    调用百度 Web 服务 API：限流休眠 + 超时 5s + 重试 1 次。
    返回解析后的 JSON；网络失败且重试后仍失败则抛异常。
    """
    time.sleep(API_SLEEP)
    merged = dict(params)
    merged["ak"] = BAIDU_AK
    merged.setdefault("output", "json")
    url = BAIDU_BASE + path
    last_exc = None
    for _ in range(API_RETRIES + 1):
        try:
            resp = requests.get(url, params=merged, timeout=API_TIMEOUT)
            return resp.json()
        except Exception as exc:  # 网络/超时问题才重试
            last_exc = exc
    raise last_exc


def geocode(address, city=""):
    """地理编码：地址 -> 百度坐标（bd09ll）"""
    params = {"address": address, "city": city}
    data = baidu_get("/geocoding/v3/", params)
    if not data or data.get("status") != 0:
        return None
    loc = data["result"]["location"]
    return {
        "lng": loc["lng"],
        "lat": loc["lat"],
        "address": data.get("result", {}).get("formatted_address") or address,
        "level": data.get("result", {}).get("level", ""),
    }


def reverse_geocode(lng, lat):
    """逆地理编码：坐标 -> 地址详情"""
    params = {"location": "%s,%s" % (lat, lng), "coordtype": "bd09ll"}
    data = baidu_get("/reverse_geocoding/v3/", params)
    if not data or data.get("status") != 0:
        return None
    result = data.get("result", {})
    comp = result.get("addressComponent", {}) or {}
    return {
        "formatted_address": result.get("formatted_address", ""),
        "city": comp.get("city", ""),
        "district": comp.get("district", ""),
        "province": comp.get("province", ""),
    }


def search_pois(category, center, radius=POI_RADIUS, max_pages=3):
    """按关键词+半径检索 POI，最多 3 页（60 条），按 uid 去重（无 uid 按 name+坐标）"""
    seen = set()
    pois = []
    for keyword in category["keywords"]:
        for page in range(1, max_pages + 1):
            params = {
                "query": keyword,
                "location": "%s,%s" % (center[1], center[0]),
                "radius": radius,
                "scope": 2,
                "page_size": 20,
                "page_num": page,
            }
            data = baidu_get("/place/v2/search", params)
            if not data or data.get("status") != 0:
                break  # 单关键词失败不致命，跳过继续
            results = data.get("results") or []
            if not results:
                break
            for poi in results:
                loc = poi.get("location") or {}
                if not loc.get("lat") or not loc.get("lng"):
                    continue
                uid = poi.get("uid")
                key = uid or ("%s|%.6f,%.6f" % (poi.get("name", ""), loc["lat"], loc["lng"]))
                if key in seen:
                    continue
                seen.add(key)
                detail = poi.get("detail_info", {}) or {}
                pois.append({
                    "name": poi.get("name", ""),
                    "lng": loc["lng"],
                    "lat": loc["lat"],
                    "uid": uid or "",
                    "address": poi.get("address", ""),
                    "province": poi.get("province", ""),
                    "city": poi.get("city", ""),
                    "area": poi.get("area", ""),
                    "distance": detail.get("distance"),
                    "tag": detail.get("tag", ""),
                })
            if len(results) < 20:
                break
    return pois


def walking_duration(origin, dest):
    """
    步行路线规划算路网耗时（秒）。
    结果做内存缓存（key = origin/destination 坐标取整 5 位小数）。
    status != 0 或异常视为不可达（返回 24h）。
    """
    key = (
        round(origin[0], 5), round(origin[1], 5),
        round(dest[0], 5), round(dest[1], 5),
    )
    if key in _walk_cache:
        return _walk_cache[key]
    params = {
        "origin": "%s,%s" % (origin[1], origin[0]),
        "destination": "%s,%s" % (dest[1], dest[0]),
    }
    duration = INF_TRAVEL
    try:
        data = baidu_get("/directionlite/v1/walking", params)
        if data and data.get("status") == 0:
            routes = (data.get("result") or {}).get("routes") or []
            if routes:
                duration = routes[0].get("duration", INF_TRAVEL)
    except Exception:
        duration = INF_TRAVEL
    _walk_cache[key] = duration
    return duration


# ---------------------------------------------------------------------------
# 等时圈算法（核心）
# ---------------------------------------------------------------------------
def compute_isochrone(center, minutes, rays, steps, progress_cb=None):
    """
    沿中心点 360° 均匀发射 rays 条射线，每条射线上用二分法
    找「步行耗时 = minutes 分钟」的边界点，连接成多边形即为等时圈。
    真实路网由步行 API 的 duration 体现（不走直线）。
    """
    max_dist = MAX_SEARCH_DIST
    lat1m = LAT_1M
    lng1m = _lng_1m(center[1])
    limit = minutes * 60.0
    boundary = []
    for i in range(rays):
        angle = 2.0 * math.pi * i / rays
        dx, dy = math.cos(angle), math.sin(angle)  # 指东、指北
        lo, hi = 0.0, max_dist
        for _ in range(steps):                     # 8 次二分 ≈ 10 米精度
            mid = (lo + hi) / 2.0
            p = (center[0] + dx * mid * lng1m, center[1] + dy * mid * lat1m)
            t = walking_duration(center, p)        # 秒，走 API（带缓存）
            if t <= limit:
                lo = mid
            else:
                hi = mid
        boundary.append((center[0] + dx * lo * lng1m, center[1] + dy * lo * lat1m))
        if progress_cb:
            progress_cb(i + 1, rays)
    return boundary


def smooth_boundary(boundary):
    """轻量平滑：每个点替换为与相邻两点取平均（一遍），避免锯齿"""
    n = len(boundary)
    if n < 3:
        return boundary
    out = []
    for i in range(n):
        prev = boundary[(i - 1) % n]
        cur = boundary[i]
        nxt = boundary[(i + 1) % n]
        out.append(((prev[0] + cur[0] + nxt[0]) / 3.0, (prev[1] + cur[1] + nxt[1]) / 3.0))
    return out


def polygon_metrics(boundary, ref_lat):
    """
    计算多边形面积（km²，shoelace 公式，经纬度换算为平面米坐标）与周长（km）。
    """
    lat1m = LAT_1M
    lng1m = _lng_1m(ref_lat)
    ox, oy = boundary[0]
    pts = [((x - ox) / lng1m, (y - oy) / lat1m) for (x, y) in boundary]
    n = len(pts)
    area2 = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    area_m2 = abs(area2) / 2.0
    perim_m = 0.0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        perim_m += math.hypot(x2 - x1, y2 - y1)
    return area_m2 / 1e6, perim_m / 1000.0


def point_in_polygon(x, y, polygon):
    """点在多边形内判定（ray casting）"""
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


# ---------------------------------------------------------------------------
# 灰色区域检测
# ---------------------------------------------------------------------------
def bbox_of(boundary):
    lngs = [p[0] for p in boundary]
    lats = [p[1] for p in boundary]
    return min(lngs), min(lats), max(lngs), max(lats)


def _haversine_km(lat1, lng1, lat2, lng2):
    """两点球面距离（公里）"""
    radius = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


def detect_gray_areas(boundary, all_pois, center_lat):
    """
    灰色区域检测：
      - 等时圈 bbox 内按 100m 网格取多边形内网格点
      - 每点统计 5 类必需设施最近直线距离，≥2 类 >500m 则标记灰色点
      - 灰色点 4-邻域 BFS 连通聚类，过滤 <3 点的碎片簇
      - 每簇输出点数/面积/质心/质心地址/缺失设施清单
    """
    pois_by_rule = [[] for _ in REQUIRED_FACILITY_LABELS]
    for poi in all_pois:
        name = poi.get("name", "") or ""
        cname = poi.get("category", "") or ""
        for idx, (label, keywords) in enumerate(REQUIRED_FACILITY_RULES):
            if cname == label and any(kw in name for kw in keywords):
                pois_by_rule[idx].append(poi)
    flag = any(len(lst) == 0 for lst in pois_by_rule)
    # 若某类必需设施完全缺失，则网格点对该类距离记极大值

    min_lng, min_lat, max_lng, max_lat = bbox_of(boundary)
    lat1m = LAT_1M
    lng1m = _lng_1m(center_lat)
    cols = int(math.ceil((max_lng - min_lng) / (GRAY_GRID_STEP * lng1m))) + 1
    rows = int(math.ceil((max_lat - min_lat) / (GRAY_GRID_STEP * lat1m))) + 1
    if rows <= 0 or cols <= 0 or rows * cols > 200000:
        return []

    def nearest_m(lng, lat, lst):
        """到该类最近设施直线距离（米）"""
        best = float("inf")
        for p in lst:
            d = _haversine_km(lat, lng, p["lat"], p["lng"]) * 1000.0
            if d < best:
                best = d
        return best

    gray_points = []   # (lng, lat)
    for r in range(rows):
        lat = min_lat + r * GRAY_GRID_STEP * lat1m
        for c in range(cols):
            lng = min_lng + c * GRAY_GRID_STEP * lng1m
            if not point_in_polygon(lng, lat, boundary):
                continue
            far_cnt = 0
            for lst in pois_by_rule:
                if not lst:
                    far_cnt += 1
                    continue
                d = nearest_m(lng, lat, lst)
                if d > GRAY_DIST:
                    far_cnt += 1
            if far_cnt >= 2:
                gray_points.append((lng, lat))

    if not gray_points:
        return []

    # 转网格行列索引做 BFS 聚类（4-邻域）
    gset = set()
    for (lng, lat) in gray_points:
        r = int(round((lat - min_lat) / (GRAY_GRID_STEP * lat1m)))
        c = int(round((lng - min_lng) / (GRAY_GRID_STEP * lng1m)))
        gset.add((r, c))

    visited = set()
    clusters = []
    for start in gset:
        if start in visited:
            continue
        queue = [start]
        visited.add(start)
        members = []
        while queue:
            cur = queue.pop()
            members.append(cur)
            for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (cur[0] + dr, cur[1] + dc)
                if nxt in gset and nxt not in visited:
                    visited.add(nxt)
                    queue.append(nxt)
        if len(members) >= GRAY_MIN_POINTS:   # 过滤碎片簇
            clusters.append(members)

    result = []
    for members in clusters:
        lngs = [min_lng + c * GRAY_GRID_STEP * lng1m for (r, c) in members]
        lats = [min_lat + r * GRAY_GRID_STEP * lat1m for (r, c) in members]
        clng = sum(lngs) / len(lngs)
        clat = sum(lats) / len(lats)
        # 簇 500 米内缺失的必需设施清单：簇内任意点该类最近距离 >500m
        missing = []
        for idx, (label, keywords) in enumerate(REQUIRED_FACILITY_RULES):
            lst = pois_by_rule[idx]
            if any(nearest_m(lo, la, lst) > GRAY_DIST for (lo, la) in zip(lngs, lats)):
                missing.append(REQUIRED_FACILITY_LABELS[idx])
        addr = ""
        try:
            rv = reverse_geocode(clng, clat)
            if rv:
                addr = rv["formatted_address"]
        except Exception:
            addr = ""
        result.append({
            "points": len(members),
            "area_m2": len(members) * GRAY_GRID_STEP * GRAY_GRID_STEP,
            "area_km2": len(members) * GRAY_GRID_STEP * GRAY_GRID_STEP / 1e6,
            "centroid": {"lng": round(clng, 6), "lat": round(clat, 6)},
            "address": addr,
            "missing": missing,
            "bounds": {
                "min_lng": min(lngs), "min_lat": min(lats),
                "max_lng": max(lngs), "max_lat": max(lats),
            },
        })
    return result


# ---------------------------------------------------------------------------
# 报告生成（任务执行体）
# ---------------------------------------------------------------------------
def build_report(job):
    """
    完整报告流水线：等时圈 → POI 采集 → 圈内过滤 → 分类统计 →
    最近设施步行分钟 → 灰色区域 → 结论建议。
    任何环节出错都置 job 为 error 并返回友好 message。
    """
    center = (job["lng"], job["lat"])
    minutes = job["minutes"]
    rays = job["rays"]
    steps = job["steps"]

    def progress(i, total):
        job["progress"] = int(i / total * 100)
        job["stage"] = "正在算路 %d/%d" % (i, total)

    try:
        job["stage"] = "正在算路 0/%d" % rays
        job["status"] = "running"
        job["progress"] = 0
        boundary = compute_isochrone(center, minutes, rays, steps, progress_cb=progress)

        # 轻量平滑
        boundary = smooth_boundary(boundary)
        area_km2, perimeter_km = polygon_metrics(boundary, center[1])
        degenerate = area_km2 < 0.001  # 退化：基本无有效等时圈

        job["stage"] = "收集周边设施 POI…"
        job["progress"] = 100

        # 采集 8 类 POI（单类失败不影响其它类）
        all_pois = []
        for cat in CATEGORIES:
            try:
                pois = search_pois(cat, center)
                for p in pois:
                    p["category"] = cat["name"]
                all_pois.extend(pois)
            except Exception:
                continue  # 单类 POI 检索失败不影响其它类别

        job["stage"] = "统计圈内设施…"

        # 只在等时圈内的 POI 才算数
        in_pois = [p for p in all_pois if point_in_polygon(p["lng"], p["lat"], boundary)]

        def nearest_poi(lng, lat, lst):
            best, best_d = None, float("inf")
            for p in lst:
                d = _haversine_km(lat, lng, p["lat"], p["lng"]) * 1000.0
                if d < best_d:
                    best, best_d = p, d
            return best, best_d

        categories_report = []
        for cat in CATEGORIES:
            lst = [p for p in in_pois if p["category"] == cat["name"]]
            nearest, nearest_d = nearest_poi(center[0], center[1], lst)
            walk_minutes = None
            if nearest:
                # 对每类最近设施调用一次步行 API 得到真实步行分钟
                dur = walking_duration(center, (nearest["lng"], nearest["lat"]))
                walk_minutes = round(dur / 60.0, 1) if dur < INF_TRAVEL else None
            categories_report.append({
                "key": cat["key"],
                "name": cat["name"],
                "color": cat["color"],
                "count": len(lst),
                "nearest_distance_m": round(nearest_d, 1) if nearest else None,
                "walk_minutes": walk_minutes,
                "threshold": cat["threshold"],
                "satisfied": len(lst) >= cat["threshold"],
            })

        # 健康指数 = 100 × Σ(达标类别权重)
        health_index = 0.0
        satisfied_count = 0
        for cat, rep in zip(CATEGORIES, categories_report):
            if rep["satisfied"]:
                health_index += cat["weight"] * 100.0
                satisfied_count += 1
        health_index = round(health_index, 1)

        # 灰色区域检测
        job["stage"] = "检测灰色区域…"
        gray_areas = detect_gray_areas(boundary, all_pois, center[1])
        gray_count = len(gray_areas)
        gray_total_m2 = sum(ga["area_m2"] for ga in gray_areas)
        gray_ratio = round(gray_total_m2 / (area_km2 * 1e6), 4) if area_km2 > 0 else 0

        # 结论建议
        suggestions, verdict = build_suggestions(categories_report, gray_areas, health_index)

        job["result"] = {
            "center": {"lng": center[0], "lat": center[1]},
            "minutes": minutes,
            "rays": rays,
            "steps": steps,
            "isochrone": {
                "boundary": [[round(x, 6), round(y, 6)] for (x, y) in boundary],
                "area_km2": round(area_km2, 4),
                "perimeter_km": round(perimeter_km, 3),
                "degenerate": degenerate,
            },
            "poi_total": len(in_pois),
            "pois": in_pois,
            "categories": categories_report,
            "health_index": health_index,
            "satisfied_count": satisfied_count,
            "gray_areas": gray_areas,
            "gray_stats": {
                "count": gray_count,
                "total_area_m2": gray_total_m2,
                "ratio": gray_ratio,
            },
            "suggestions": suggestions,
            "verdict": verdict,
        }
        job["status"] = "done"
        job["progress"] = 100
        job["stage"] = "完成"
    except Exception as exc:  # 任务失败，明确错误 message 返回前端提示条
        job["status"] = "error"
        job["message"] = "计算失败：%s" % exc


def build_suggestions(categories_report, gray_areas, health_index):
    """规则生成 2-4 条中文建议 + 一句话总评"""
    suggestions = []
    template = {
        "医疗": "该区域存在至少一个社区医疗点（药店/诊所/医院）缺口，建议增设社区医疗或诊所以便应急。",
        "商业": "该区域日常购物设施不足，建议增设便利店或社区超市。",
        "交通": "该区域公共交通覆盖偏弱，建议优化公交线路或增设站点。",
        "教育": "该区域教育配套不足，建议补充普惠性幼儿园或学校。",
        "餐饮": "该区域餐饮选择较少，建议引入社区食堂或餐饮商户。",
        "金融": "该区域金融服务网点缺乏，建议增设自助银行或 ATM。",
        "生活服务": "该区域生活服务（快递/理发/洗衣）配套不足，建议引入便民服务站。",
        "文体": "该区域文体休闲设施欠缺，建议增设口袋公园或健身场地。",
    }
    for rep in categories_report:
        if not rep["satisfied"]:
            suggestions.append(template.get(rep["name"], rep["name"] + "设施不足。"))
    for ga in gray_areas[:2]:
        missing = "、".join(ga["missing"]) if ga["missing"] else "多项基础服务设施"
        loc = ga["address"] or "等时圈内"
        suggestions.append("灰色区域「%s」500 米内缺失 %s，建议重点补充相关设施。" % (loc, missing))
    if len(suggestions) < 2:
        suggestions.append("建议持续跟踪等时圈内 POI 数据变化，定期评估便民设施覆盖情况。")
    suggestions = suggestions[:4]

    if health_index >= 70:
        verdict = "整体宜居便利，15 分钟生活圈内绝大多数民生需求可步行满足。"
    elif health_index >= 50:
        verdict = "整体基本可用，仍有部分民生设施覆盖不足，建议针对性补齐。"
    else:
        verdict = "生活设施匮乏，建议重点关注并加大社区配套设施投入。"
    return suggestions, verdict


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/health")
def api_health():
    return jsonify({"status": "ok", "ak_configured": bool(BAIDU_AK)})


@app.route("/api/config")
def api_config():
    return jsonify({
        "ak": BAIDU_AK,
        "ak_configured": bool(BAIDU_AK),
        "default_center": DEFAULT_CENTER,
        "samples": SAMPLE_STREETS,
        "minutes": DEFAULT_MINUTES,
        "rays": DEFAULT_RAYS,
        "categories": CATEGORIES,
    })


@app.route("/api/geocode")
def api_geocode():
    address = (request.args.get("address") or "").strip()
    if not address:
        return jsonify({"error": "缺少 address 参数"}), 400
    try:
        result = geocode(address, request.args.get("city", ""))
    except Exception as exc:
        return jsonify({"error": "地理编码失败：%s" % exc}), 502
    if not result:
        return jsonify({"error": "地址解析失败，请检查地址或百度地图密钥配置"}), 502
    return jsonify(result)


@app.route("/api/reverse")
def api_reverse():
    try:
        lng = float(request.args.get("lng"))
        lat = float(request.args.get("lat"))
    except (TypeError, ValueError):
        return jsonify({"error": "缺少合法的 lng/lat 参数"}), 400
    try:
        result = reverse_geocode(lng, lat)
    except Exception as exc:
        return jsonify({"error": "逆地理编码失败：%s" % exc}), 502
    if not result:
        return jsonify({"error": "逆地理编码失败，请检查坐标或百度地图密钥配置"}), 502
    return jsonify(result)


@app.route("/api/isochrone", methods=["POST"])
def api_isochrone():
    """
    创建异步等时圈计算任务，立即返回 {job_id}。
    参数：{lng, lat, minutes=15, rays=36, steps=8}
    后端内存任务表：daemon 线程执行，任务完成后结果缓存（key 含 lng/lat/minutes/rays）。
    """
    body = request.get_json(silent=True) or {}
    try:
        lng = float(body.get("lng"))
        lat = float(body.get("lat"))
    except (TypeError, ValueError):
        return jsonify({"error": "缺少合法的 lng/lat 参数"}), 400
    minutes = int(body.get("minutes", DEFAULT_MINUTES) or DEFAULT_MINUTES)
    rays = int(body.get("rays", DEFAULT_RAYS) or DEFAULT_RAYS)
    steps = int(body.get("steps", DEFAULT_STEPS) or DEFAULT_STEPS)
    if not (0 < minutes <= 60):
        return jsonify({"error": "minutes 需在 1-60 之间"}), 400
    if rays not in (24, 36, 48):
        return jsonify({"error": "rays 仅支持 24/36/48"}), 400
    if not (1 <= steps <= 16):
        return jsonify({"error": "steps 需在 1-16 之间"}), 400

    cache_key = (round(lng, 6), round(lat, 6), minutes, rays)
    with _jobs_lock:
        cached_job_id = _results_cache.get(cache_key)
        if cached_job_id and cached_job_id in _jobs:
            job = _jobs[cached_job_id]
            if job["status"] in ("done", "running"):
                return jsonify({"job_id": cached_job_id, "cached": job["status"] == "done"})

        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "lng": lng,
            "lat": lat,
            "minutes": minutes,
            "rays": rays,
            "steps": steps,
            "status": "queued",
            "progress": 0,
            "stage": "排队中…",
            "message": "",
            "result": None,
            "cache_key": cache_key,
        }
        _jobs[job_id] = job
        thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
        thread.start()
        return jsonify({"job_id": job_id})


def _run_job(job_id):
    """daemon 线程执行任务体"""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return
    if not BAIDU_AK:
        with _jobs_lock:
            job["status"] = "error"
            job["message"] = "请在环境变量 BAIDU_MAP_AK 配置百度地图密钥后重试。"
        return
    try:
        build_report(job)
        with _jobs_lock:
            if job["status"] == "done":
                _results_cache[job["cache_key"]] = job_id
    except Exception as exc:
        with _jobs_lock:
            job["status"] = "error"
            job["message"] = "计算失败：%s" % exc


@app.route("/api/isochrone/<job_id>")
def api_isochrone_status(job_id):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"status": "error", "message": "任务不存在或已过期"}), 404
    if job["status"] == "running":
        return jsonify({"status": "running", "progress": job["progress"], "stage": job["stage"]})
    if job["status"] == "done":
        return jsonify({"status": "done", "result": job["result"]})
    return jsonify({"status": "error", "message": job.get("message") or "任务失败"})


@app.route("/api/report/<job_id>")
def api_report(job_id):
    """完整报告别名接口，等价于 isochrone done 的 result"""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return jsonify({"status": "error", "message": "任务不存在或已过期"}), 404
    if job["status"] == "done" and job["result"]:
        return jsonify(job["result"])
    if job["status"] == "error":
        return jsonify({"status": "error", "message": job.get("message") or "任务失败"}), 502
    return jsonify({"status": "running", "progress": job["progress"], "stage": job["stage"]})


def main():
    parser = argparse.ArgumentParser(description="15分钟便民生活圈体检报告")
    parser.add_argument("--port", type=int, default=5000, help="监听端口（默认 5000）")
    args = parser.parse_args()
    print("15分钟便民生活圈服务启动，监听 0.0.0.0:%d，ak_configured=%s" % (args.port, bool(BAIDU_AK)))
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()