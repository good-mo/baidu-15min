#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
15分钟便民生活圈体检报告 v2 — Flask 后端

v1 功能（全部保留）：等时圈（射线二分法）、8 类 POI 统计、灰色区域、
健康指数、异步任务、结果缓存、AK 未配置降级。

v2 新增：
  - 多时圈 + 路网阻抗系数（直线参照圆，impendance_coef = 路网面积/直线圆面积）
  - 过街阻抗模型（红绿灯/天桥地道；指令解析，启发式估算，诚实标注 unavailable）
  - 手绘施工障碍区模拟（中心到候选点直线段与障碍多边形相交判定）
  - 灰色区域自动诊疗（住宅 POI 受影响人口 + 严重度 + 建议 + 优先级）

运行方式：python app.py --port <PORT>（默认 5000，监听 0.0.0.0）
环境变量：BAIDU_MAP_AK（必填，从 lbsyun.baidu.com 申请）
"""

import argparse
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

# v2 阻抗/过街参数（默认值）
DEFAULT_WALK_SPEED = 1.2       # 步行速度 m/s（仅用于直线参照圆）
DEFAULT_CROSSING_SEC = 30      # 每次过街（红绿灯）等待估算秒数
DEFAULT_OVERPASS_SEC = 120     # 每次天桥/地道上下行估算秒数
MINUTES_OPTIONS = [[15], [5, 10, 15]]   # 时间档：单圈 / 多圈
CROSSING_KEYWORDS = ["过马路", "过街", "横穿", "穿过", "路口"]
OVERPASS_KEYWORDS = ["天桥", "地下通道", "人行天桥", "地道"]

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

# 灰色区域辅助检索类别：住宅（不计入 8 类达标，仅用于受影响人口估计）
RESIDENTIAL_CATEGORY = {"key": "residential", "name": "住宅", "keywords": ["小区", "住宅区", "公寓"], "color": "#6b7280"}

# 灰色区域检测用的 4 类「日常必需设施」
REQUIRED_FACILITY_RULES = [
    ("医疗",   ["药店", "医院"]),
    ("商业",   ["超市", "便利店"]),
    ("教育",   ["小学", "中学", "幼儿园", "大学"]),
    ("交通",   ["公交", "地铁"]),
]
REQUIRED_FACILITY_LABELS = ["药店/医院", "超市/便利店", "学校", "公交/地铁站"]

# 灰色区域自动诊疗：缺失类别数 -> 严重度；缺失项 -> 建议文案
GRAY_SEVERITY_BY_MISS = {0: "匮乏", 1: "匮乏", 2: "严重", 3: "极重", 4: "极重"}
GRAY_MISS_SUGGESTION = {
    "药店/医院": "建议增设社区药店或便民诊室",
    "超市/便利店": "建议引入便民超市或无人便利店",
    "学校": "建议增设托幼点或社区学堂",
    "公交/地铁站": "建议增设微循环公交站点或共享单车驿站",
}

GRAY_GRID_STEP = 100.0    # 灰色检测网格步长（米）
GRAY_DIST = 500.0         # 必需设施缺失距离阈值（米）
GRAY_MIN_POINTS = 3       # 碎片簇过滤：点数 < 3 丢弃
RES_AFFECTED_RADIUS = 500.0  # 受影响住宅统计半径（米）

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


def walking_duration(origin, dest, crossing_sec, overpass_sec):
    """
    步行路线规划算路网耗时，并解析路段明细做「过街阻抗」统计。

    返回 (effective_seconds, duration, crossings, overpasses, steps_available)
      - effective_seconds = route.duration + 过街次数*crossing_sec + 天桥次数*overpass_sec
      - steps_available：接口是否返回了可解析的路段明细（steps）
    结果做内存缓存（key 含坐标取整 5 位小数 + 过街参数）。
    status != 0 或异常视为不可达（返回 24h，过街无法统计）。
    若无 steps 明细，过街成本按 0 处理（诚实标注，不伪造数字）。
    """
    key = (
        round(origin[0], 5), round(origin[1], 5),
        round(dest[0], 5), round(dest[1], 5),
        crossing_sec, overpass_sec,
    )
    if key in _walk_cache:
        return _walk_cache[key]
    params = {
        "origin": "%s,%s" % (origin[1], origin[0]),
        "destination": "%s,%s" % (dest[1], dest[0]),
    }
    duration = INF_TRAVEL
    crossings = 0
    overpasses = 0
    steps_available = False
    try:
        data = baidu_get("/directionlite/v1/walking", params)
        if data and data.get("status") == 0:
            routes = (data.get("result") or {}).get("routes") or []
            if routes:
                route = routes[0]
                duration = route.get("duration", INF_TRAVEL)
                steps = route.get("steps") or []
                if steps:
                    steps_available = True
                    for s in steps:
                        ins = str(s.get("instruction", "") or "")
                        if any(kw in ins for kw in CROSSING_KEYWORDS):
                            crossings += 1
                        if any(kw in ins for kw in OVERPASS_KEYWORDS):
                            overpasses += 1
    except Exception:
        duration = INF_TRAVEL
    effective = duration + crossings * crossing_sec + overpasses * overpass_sec
    result = (effective, duration, crossings, overpasses, steps_available)
    _walk_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# 几何工具：线段/障碍多边形相交
# ---------------------------------------------------------------------------
def _cross(o, p, q):
    return (p[0] - o[0]) * (q[1] - o[1]) - (p[1] - o[1]) * (q[0] - o[0])


def _on_segment(a, b, p):
    return (min(a[0], b[0]) <= p[0] <= max(a[0], b[0])) and (min(a[1], b[1]) <= p[1] <= max(a[1], b[1]))


def segments_intersect(a, b, c, d):
    """线段 ab 与 cd 是否相交（保守：端点落边也视为相交）"""
    d1 = _cross(c, d, a)
    d2 = _cross(c, d, b)
    d3 = _cross(a, b, c)
    d4 = _cross(a, b, d)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)):
        return True
    if d1 == 0 and _on_segment(c, d, a):
        return True
    if d2 == 0 and _on_segment(c, d, b):
        return True
    if d3 == 0 and _on_segment(a, b, c):
        return True
    if d4 == 0 and _on_segment(a, b, d):
        return True
    return False


def segment_hits_obstacles(a, b, obstacles):
    """线段 ab 是否与任一障碍多边形的任一边相交"""
    if not obstacles:
        return False
    for poly in obstacles:
        n = len(poly)
        for i in range(n):
            c = poly[i]
            d = poly[(i + 1) % n]
            if segments_intersect(a, b, c, d):
                return True
    return False


# ---------------------------------------------------------------------------
# 等时圈算法（核心）
# ---------------------------------------------------------------------------
def compute_isochrone(center, minutes, rays, steps, crossing_sec, overpass_sec,
                      obstacles=None, progress_cb=None):
    """
    沿中心点 360° 均匀发射 rays 条射线，每条射线上用二分法找「有效步行耗时 =
    minutes 分钟」的边界点，连接成多边形即为等时圈。

    有效耗时 = 路网 duration + 过街成本（v2）；若中心到候选点的直线段穿过
    障碍多边形，则视为不可达（INF），二分自然内缩形成「凹陷」。

    返回 (boundary, crossing_stats, any_steps_available)
      - boundary: [(lng,lat), ...]
      - crossing_stats: {crossings, overpasses}（该圈 36 条射线最终接受候选点之和）
      - any_steps_available: 是否至少一次解析到路段明细（决定 crossing_mode）
    """
    obstacles = obstacles or []
    max_dist = MAX_SEARCH_DIST
    lat1m = LAT_1M
    lng1m = _lng_1m(center[1])
    limit = minutes * 60.0
    boundary = []
    total_cross = 0
    total_over = 0
    any_steps = False
    for i in range(rays):
        angle = 2.0 * math.pi * i / rays
        dx, dy = math.cos(angle), math.sin(angle)  # 指东、指北
        lo, hi = 0.0, max_dist
        for _ in range(steps):                     # 8 次二分 ≈ 10 米精度
            mid = (lo + hi) / 2.0
            p = (center[0] + dx * mid * lng1m, center[1] + dy * mid * lat1m)
            if segment_hits_obstacles(center, p, obstacles):
                t = INF_TRAVEL           # 被障碍挡住：视为不可达
            else:
                t, _, _, _, _ = walking_duration(center, p, crossing_sec, overpass_sec)
            if t <= limit:
                lo = mid
            else:
                hi = mid
        bp = (center[0] + dx * lo * lng1m, center[1] + dy * lo * lat1m)
        # 过街统计口径：该射线最终接受的候选点（boundary 点）的路线
        if segment_hits_obstacles(center, bp, obstacles):
            pass
        else:
            _, _, cr, ov, sa = walking_duration(center, bp, crossing_sec, overpass_sec)
            total_cross += cr
            total_over += ov
            if sa:
                any_steps = True
        boundary.append(bp)
        if progress_cb:
            progress_cb(i + 1, rays)
    return boundary, {"crossings": total_cross, "overpasses": total_over}, any_steps


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


def straight_circle(minutes, walk_speed):
    """直线参照圆：半径 = 步行速度 * 分钟 * 60（假设无路网阻碍的理想圆）"""
    radius_m = walk_speed * minutes * 60.0
    return radius_m / 1000.0, math.pi * (radius_m / 1000.0) ** 2  # km, km²


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
# 灰色区域检测（v1）+ 自动诊疗（v2）
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


def _affected_level(n):
    """受影响人口规模分级：high(≥10住宅) / mid(3-9) / low(<3)"""
    if n >= 10:
        return "high"
    if n >= 3:
        return "mid"
    return "low"


def _gray_priority(severity, affected):
    """自动诊疗优先级：
       极高 = 极重 或 (严重 且 影响高)
       高   = 严重 或 (匮乏 且 影响高)
       中   = 匮乏 且 影响中
       低   = 其余
    """
    if severity == "极重" or (severity == "严重" and affected == "high"):
        return "极高"
    if severity == "严重" or (severity == "匮乏" and affected == "high"):
        return "高"
    if severity == "匮乏" and affected == "mid":
        return "中"
    return "低"


def detect_gray_areas(boundary, all_pois, center_lat, residential_pois=None):
    """
    灰色区域检测（v1）+ 自动诊疗（v2）：
      - 等时圈 bbox 内按 100m 网格取多边形内网格点
      - 每点统计 4 类必需设施最近直线距离，≥2 类 >500m 则标记灰色点
      - 灰色点 4-邻域 BFS 连通聚类，过滤 <3 点的碎片簇
      - 每簇：面积/质心/质心地址/缺失设施清单
        + v2 诊疗：500m 内住宅 POI 数（受影响人口）、严重度、建议、优先级
    """
    pois_by_rule = [[] for _ in REQUIRED_FACILITY_LABELS]
    for poi in all_pois:
        name = poi.get("name", "") or ""
        cname = poi.get("category", "") or ""
        for idx, (label, keywords) in enumerate(REQUIRED_FACILITY_RULES):
            if cname == label and any(kw in name for kw in keywords):
                pois_by_rule[idx].append(poi)

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

    residential_pois = residential_pois or []
    result = []
    for members in clusters:
        lngs = [min_lng + c * GRAY_GRID_STEP * lng1m for (r, c) in members]
        lats = [min_lat + r * GRAY_GRID_STEP * lat1m for (r, c) in members]
        clng = sum(lngs) / len(lngs)
        clat = sum(lats) / len(lats)
        # 簇 500 米内缺失的必需设施清单
        missing = []
        for idx, (label, keywords) in enumerate(REQUIRED_FACILITY_RULES):
            lst = pois_by_rule[idx]
            if any(nearest_m(lo, la, lst) > GRAY_DIST for (lo, la) in zip(lngs, lats)):
                missing.append(REQUIRED_FACILITY_LABELS[idx])
        # v2 自动诊疗
        affected_res_count = sum(
            1 for rp in residential_pois
            if _haversine_km(clat, clng, rp["lat"], rp["lng"]) * 1000.0 <= RES_AFFECTED_RADIUS
        )
        affected_level = _affected_level(affected_res_count)
        severity = GRAY_SEVERITY_BY_MISS.get(len(missing), "匮乏")
        suggestions = [GRAY_MISS_SUGGESTION[m] for m in missing if m in GRAY_MISS_SUGGESTION]
        priority = _gray_priority(severity, affected_level)
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
            "suggestions": suggestions,
            "affected_res_count": affected_res_count,
            "affected_level": affected_level,
            "severity": severity,
            "priority": priority,
            "bounds": {
                "min_lng": min(lngs), "min_lat": min(lats),
                "max_lng": max(lngs), "max_lat": max(lats),
            },
        })
    return result


def _poly_to_tuples(poly):
    """把障碍多边形统一为 [(lng,lat), ...]（兼容 {lng,lat} dict 与 tuple/list）"""
    out = []
    for p in poly:
        if isinstance(p, dict):
            out.append((float(p.get("lng")), float(p.get("lat"))))
        else:
            out.append((float(p[0]), float(p[1])))
    return out


# ---------------------------------------------------------------------------
# v3：等时圈热力图（网格采样）+ 1公里服务盲区核验
# ---------------------------------------------------------------------------
# 坐标转换（百度 geoconv）：from 值映射表
GEO_CONV_FROM = {"wgs84": 1, "gcj02": 3, "bd09ll": 5}

# 热力图参数
HEAT_MAX_POINTS = 120      # 热力网格点数上限（超限自动放大步长）
HEAT_BASE_STEP = 100.0     # 热力网格基础步长（米）

# 1公里服务盲区核验参数（评分口径）
BLIND_GRID_STEP = 150.0    # 盲区网格点步长（米）
BLIND_RADIUS = 1000.0      # 盲区判定半径（米）
BLIND_CATEGORIES = ["菜市场", "药店", "小学"]
BLIND_MAX_GRID = 1500      # 网格候选点保护上限
# 盲区设施提取规则：(输出名, POI类别, 名称包含关键词)
BLIND_FACILITY_RULES = [
    ("菜市场", "商业", ["菜市场"]),
    ("药店", "医疗", ["药店"]),
    ("小学", "教育", ["小学"]),
]


def compute_heat(center, boundary, minutes, rays, crossing_sec, overpass_sec,
                 obstacles=None, exact=True, max_points=HEAT_MAX_POINTS):
    """
    等时圈热力图：主圈 bbox 内按 100m 网格取多边形内点（点数超上限自动放大步长）。
      - exact=True（默认）：调一次步行 API（复用缓存）取 effective_duration 换算分钟；
      - exact=False（节省模式）：按点相对中心的角度，在相邻两条射线的边界半径之间
        线性插值出边界半径，再按 半径/边界半径 × minutes 估算分钟数，不增加 API 调用。
    返回 (heat_list, step_used)；heat = [{lng, lat, minutes} | minutes=None(不可达/失败)]。
    """
    obstacles = obstacles or []
    min_lng, min_lat, max_lng, max_lat = bbox_of(boundary)
    lat1m = LAT_1M
    lng1m = _lng_1m(center[1])

    # 网格采样：步长 100m 起步，点数超限自动放大步长重取
    factor = 1
    pts = []
    step = HEAT_BASE_STEP
    while True:
        step = HEAT_BASE_STEP * factor
        cols = int(math.ceil((max_lng - min_lng) / (step * lng1m))) + 1
        rows = int(math.ceil((max_lat - min_lat) / (step * lat1m))) + 1
        pts = []
        if 0 < rows <= 400 and 0 < cols <= 400:
            for r in range(rows):
                lat = min_lat + r * step * lat1m
                for c in range(cols):
                    lng = min_lng + c * step * lng1m
                    if point_in_polygon(lng, lat, boundary):
                        pts.append((lng, lat))
        if len(pts) <= max_points or factor >= 5:
            break
        factor += 1

    heat = []
    if exact:
        for (lng, lat) in pts:
            if segment_hits_obstacles(center, (lng, lat), obstacles):
                heat.append({"lng": round(lng, 6), "lat": round(lat, 6), "minutes": None})
                continue
            eff, _, _, _, _ = walking_duration(center, (lng, lat), crossing_sec, overpass_sec)
            m = round(eff / 60.0, 1) if eff < INF_TRAVEL else None
            heat.append({
                "lng": round(lng, 6), "lat": round(lat, 6),
                "minutes": min(m, 40.0) if m is not None else None,
            })
    else:
        # 射线插值：按角度重建每条射线的边界半径（防御 boundary 点数不足/乱序）
        ray_radii = [0.0] * rays
        step_ang = 2.0 * math.pi / rays
        for p in boundary:
            a_ang = math.atan2((p[1] - center[1]) / lat1m, (p[0] - center[0]) / lng1m)
            if a_ang < 0:
                a_ang += 2.0 * math.pi
            idx = int(a_ang / step_ang) % rays
            r_now = math.hypot((p[0] - center[0]) / lng1m, (p[1] - center[1]) / lat1m)
            if r_now > ray_radii[idx]:
                ray_radii[idx] = r_now
        for (lng, lat) in pts:
            radius_p = math.hypot((lng - center[0]) / lng1m, (lat - center[1]) / lat1m)
            a = math.atan2((lat - center[1]) / lat1m, (lng - center[0]) / lng1m)
            if a < 0:
                a += 2.0 * math.pi
            idx = int(a / step_ang) % rays
            f = (a - idx * step_ang) / step_ang
            r_b = ray_radii[idx] + (ray_radii[(idx + 1) % rays] - ray_radii[idx]) * f
            if r_b <= 1e-9:
                heat.append({"lng": round(lng, 6), "lat": round(lat, 6), "minutes": None})
            else:
                m = minutes * radius_p / r_b
                m = min(max(m, 0.1), 40.0)
                heat.append({"lng": round(lng, 6), "lat": round(lat, 6), "minutes": round(m, 1)})
    return heat, step


def detect_blind_spots(center, boundary, all_pois, residential_pois=None):
    """
    1公里服务盲区核验（评分口径，与 v2 灰色区域并存互不影响）：
      对候选点检查其 1km（直线）内是否有【菜市场】【药店】【小学】三类设施；
      三类全部没有 → 判为「服务盲区点位」。
      候选来源：
        1) 住宅小区 POI（category=住宅）；
        2) 等时圈内网格点（步长 150m）。
      设施直接取自已收集 POI（菜市场=商业类含「菜市场」、药店=医疗类含「药店」、
      小学=教育类含「小学」），不额外调用接口。
    """
    # 设施池
    fac_pools = [[] for _ in BLIND_FACILITY_RULES]
    for poi in all_pois:
        cname = poi.get("category", "") or ""
        name = poi.get("name", "") or ""
        for i, (_label, cat_key, kws) in enumerate(BLIND_FACILITY_RULES):
            if cname == cat_key and any(kw in name for kw in kws):
                fac_pools[i].append(poi)
                break

    def nearest_m(label_idx, lng, lat):
        """该类最近设施直线距离（米）；该类别无任何设施返回 None（视为缺失）"""
        pool = fac_pools[label_idx]
        if not pool:
            return None
        best = float("inf")
        for p in pool:
            d = _haversine_km(lat, lng, p["lat"], p["lng"]) * 1000.0
            if d < best:
                best = d
        return best

    # 候选点：住宅 POI + 网格点
    candidates = [(rp["lng"], rp["lat"], "住宅小区") for rp in (residential_pois or [])]
    min_lng, min_lat, max_lng, max_lat = bbox_of(boundary)
    lat1m = LAT_1M
    lng1m = _lng_1m(center[1])
    step = BLIND_GRID_STEP
    cols = int(math.ceil((max_lng - min_lng) / (step * lng1m))) + 1
    rows = int(math.ceil((max_lat - min_lat) / (step * lat1m))) + 1
    grid_added = 0
    if 0 < rows <= 400 and 0 < cols <= 400:
        for r in range(rows):
            lat = min_lat + r * step * lat1m
            for c in range(cols):
                lng = min_lng + c * step * lng1m
                if point_in_polygon(lng, lat, boundary):
                    candidates.append((lng, lat, "网格点"))
                    grid_added += 1
                    if grid_added >= BLIND_MAX_GRID:
                        break
            if grid_added >= BLIND_MAX_GRID:
                break

    points = []
    housing_count = 0
    for (lng, lat, source) in candidates:
        nearest_map = {}
        has_nearby = False   # 三类中是否至少一类在 1km 内
        for i, label in enumerate(BLIND_CATEGORIES):
            d = nearest_m(i, lng, lat)
            nearest_map[label] = None if d is None else round(d, 1)
            if d is not None and d <= BLIND_RADIUS:
                has_nearby = True
        if has_nearby:
            continue   # 1km 内有三类设施之一 → 非盲区
        points.append({
            "lng": round(lng, 6),
            "lat": round(lat, 6),
            "source": source,
            "nearest": nearest_map,
        })
        if source == "住宅小区":
            housing_count += 1

    return {
        "count": len(points),
        "affected_housing_count": housing_count,
        "points": points,
    }


# ---------------------------------------------------------------------------
# 报告生成（任务执行体）
# ---------------------------------------------------------------------------
def build_report(job):
    """
    完整报告流水线（v2）：
      多时圈（每圈：等时圈 → 平滑 → 度量 → 阻抗系数 → 过街统计）
      → POI 采集（8 类 + 住宅辅助）→ 圈内过滤 → 分类统计与最近设施步行分钟
      → 灰色区域自动诊疗 → 结论建议。
    任何环节出错都置 job 为 error 并返回友好 message。
    """
    center = (job["lng"], job["lat"])
    minutes_list = job["minutes_list"]
    rays = job["rays"]
    steps = job["steps"]
    walk_speed = job["walk_speed"]
    crossing_sec = job["crossing_sec"]
    overpass_sec = job["overpass_sec"]
    obstacles = [_poly_to_tuples(poly) for poly in (job["obstacles"] or [])]
    heat_exact = bool(job.get("heat_exact", True))
    total_rays = len(minutes_list) * rays

    def progress(ring_idx, i, total):
        done = ring_idx * rays + i
        job["progress"] = int(done / total_rays * 100)
        job["stage"] = "正在算路（圈 %d/%d）射线 %d/%d" % (ring_idx + 1, len(minutes_list), i, total)

    try:
        job["stage"] = "正在算路（圈 1/%d）射线 0/%d" % (len(minutes_list), rays)
        job["status"] = "running"
        job["progress"] = 0

        # ---- 多时圈 + 阻抗系数 + 过街统计 ----
        rings = []
        any_steps_global = False
        for ring_idx, minutes in enumerate(minutes_list):
            boundary, crossing_stats, any_steps = compute_isochrone(
                center, minutes, rays, steps,
                crossing_sec, overpass_sec,
                obstacles=obstacles,
                progress_cb=lambda i, total, _ridx=ring_idx: progress(_ridx, i, total),
            )
            if any_steps:
                any_steps_global = True
            boundary = smooth_boundary(boundary)
            area_km2, perimeter_km = polygon_metrics(boundary, center[1])
            straight_r_km, straight_area_km2 = straight_circle(minutes, walk_speed)
            coef = (area_km2 / straight_area_km2) if straight_area_km2 > 0 else 0.0
            coef = min(max(coef, 0.0), 1.0)   # 钳制到 0~1（API 数据异常时防 >1）
            rings.append({
                "minutes": minutes,
                "boundary": [[round(x, 6), round(y, 6)] for (x, y) in boundary],
                "area_km2": round(area_km2, 4),
                "perimeter_km": round(perimeter_km, 3),
                "straight_radius_km": round(straight_r_km, 3),
                "straight_area_km2": round(straight_area_km2, 4),
                "impedance_coef": round(coef, 4),
                "crossing": {
                    "crossing_count": crossing_stats["crossings"],
                    "crossing_wait_sec": crossing_stats["crossings"] * crossing_sec,
                    "overpass_count": crossing_stats["overpasses"],
                    "overpass_wait_sec": crossing_stats["overpasses"] * overpass_sec,
                },
                "degenerate": area_km2 < 0.001,   # 退化：无法形成有效等时圈
            })
            job["stage"] = "收集周边设施 POI…"
            job["progress"] = min(100, job["progress"] + 5)

        main_ring = max(rings, key=lambda r: r["minutes"])   # 主圈 = 分钟数最大
        crossing_mode = "heuristic" if any_steps_global else "unavailable"

        # ---- POI 采集：8 类 + 住宅（辅助，不计入达标） ----
        all_pois = []
        for cat in CATEGORIES:
            try:
                pois = search_pois(cat, center)
                for p in pois:
                    p["category"] = cat["name"]
                all_pois.extend(pois)
            except Exception:
                continue  # 单类 POI 检索失败不影响其它类别
        residential_pois = []
        try:
            residential_pois = search_pois(RESIDENTIAL_CATEGORY, center)
            for p in residential_pois:
                p["category"] = "住宅"
            all_pois.extend(residential_pois)
        except Exception:
            pass

        job["stage"] = "统计圈内设施…"

        # 只在最大等时圈（主圈）内的 POI 才算数
        main_boundary = [tuple(p) for p in main_ring["boundary"]]
        in_pois = [p for p in all_pois if point_in_polygon(p["lng"], p["lat"], main_boundary)]

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
                # 对每类最近设施调用一次步行 API，用 effective_duration 换算真实步行分钟（v2 含过街）
                eff, _, _, _, _ = walking_duration(center, (nearest["lng"], nearest["lat"]),
                                                   crossing_sec, overpass_sec)
                walk_minutes = round(eff / 60.0, 1) if eff < INF_TRAVEL else None
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

        # ---- v3 等时圈热力图（主圈网格采样） ----
        job["stage"] = "计算热力图…"
        heat_list, heat_step = compute_heat(
            center, main_boundary, main_ring["minutes"], rays,
            crossing_sec, overpass_sec, obstacles=obstacles,
            exact=heat_exact, max_points=HEAT_MAX_POINTS,
        )
        heat_vals = [h["minutes"] for h in heat_list if h["minutes"] is not None]
        heat_stats = {
            "points": len(heat_list),
            "sampled": len(heat_vals),
            "step_m": heat_step,
            "avg_minutes": round(sum(heat_vals) / len(heat_vals), 1) if heat_vals else None,
            "gt10_ratio": round(len([v for v in heat_vals if v > 10.0]) / len(heat_vals), 4) if heat_vals else 0.0,
        }

        # ---- v3 1公里服务盲区核验 ----
        job["stage"] = "核验 1km 服务盲区…"
        blind_spots = detect_blind_spots(center, main_boundary, all_pois, residential_pois)

        # ---- 灰色区域自动诊疗 ----
        job["stage"] = "检测灰色区域…"
        gray_areas = detect_gray_areas(main_boundary, all_pois, center[1], residential_pois)
        gray_count = len(gray_areas)
        gray_total_m2 = sum(ga["area_m2"] for ga in gray_areas)
        gray_ratio = round(gray_total_m2 / (main_ring["area_km2"] * 1e6), 4) if main_ring["area_km2"] > 0 else 0

        # ---- 结论建议（灰色区域按优先级排序在前） ----
        suggestions, verdict = build_suggestions(categories_report, gray_areas, health_index, blind_spots)

        result = {
            "center": {"lng": center[0], "lat": center[1]},
            "minutes": job["minutes_raw"],
            "minutes_list": minutes_list,
            "rays": rays,
            "steps": steps,
            # v2 多时圈
            "rings": rings,
            # v1 兼容字段：主圈
            "area_km2": main_ring["area_km2"],
            "perimeter_km": main_ring["perimeter_km"],
            "isochrone": main_ring,
            # POI
            "poi_total": len(in_pois),
            "residential_count": len([p for p in in_pois if p["category"] == "住宅"]),
            "pois": in_pois,
            # 分类
            "categories": categories_report,
            "health_index": health_index,
            "satisfied_count": satisfied_count,
            # 灰色区域（v1 字段兼容 + v2 诊疗字段）
            "gray_areas": gray_areas,
            "gray": {"regions": gray_areas, "stats": {
                "count": gray_count,
                "total_area_m2": gray_total_m2,
                "ratio": gray_ratio,
            }},
            "gray_stats": {"count": gray_count, "total_area_m2": gray_total_m2, "ratio": gray_ratio},
            # v3 热力图 + 1km 服务盲区
            "heat": heat_list,
            "heat_stats": heat_stats,
            "blind_spots": blind_spots,
            # 结论
            "suggestions": suggestions,
            "verdict": verdict,
            # v2 元信息（诚实标注估算）
            "meta": {
                "v2": True,
                "v3": True,
                "heat_exact": heat_exact,
                "walk_speed": walk_speed,
                "crossing_sec": crossing_sec,
                "overpass_sec": overpass_sec,
                "obstacles": [[list(p) for p in poly] for poly in obstacles],
                "crossing_mode": crossing_mode,
            },
        }
        job["result"] = result
        job["status"] = "done"
        job["progress"] = 100
        job["stage"] = "完成"
    except Exception as exc:  # 任务失败，明确错误 message 返回前端提示条
        job["status"] = "error"
        job["message"] = "计算失败：%s" % exc


def build_suggestions(categories_report, gray_areas, health_index, blind_spots=None):
    """规则生成中文建议（灰色区域按优先级排序在前，最多 6 条）+ 一句话总评"""
    suggestions = []
    template = {
        "医疗": "该区域存在社区医疗点（药店/诊所/医院）缺口，建议增设社区医疗或诊所以便应急。",
        "商业": "该区域日常购物设施不足，建议增设便利店或社区超市。",
        "交通": "该区域公共交通覆盖偏弱，建议优化公交线路或增设站点。",
        "教育": "该区域教育配套不足，建议补充普惠性幼儿园或学校。",
        "餐饮": "该区域餐饮选择较少，建议引入社区食堂或餐饮商户。",
        "金融": "该区域金融服务网点缺乏，建议增设自助银行或 ATM。",
        "生活服务": "该区域生活服务（快递/理发/洗衣）配套不足，建议引入便民服务站。",
        "文体": "该区域文体休闲设施欠缺，建议增设口袋公园或健身场地。",
    }
    order = {"极高": 0, "高": 1, "中": 2, "低": 3}
    sorted_gray = sorted(gray_areas, key=lambda g: order.get(g.get("priority"), 9))
    for ga in sorted_gray[:3]:
        loc = ga.get("address") or "等时圈内"
        for sg in ga.get("suggestions") or []:
            suggestions.append("（%s · %s）%s" % (ga.get("priority", "低"), loc, sg))
    for rep in categories_report:
        if not rep["satisfied"]:
            suggestions.append(template.get(rep["name"], rep["name"] + "设施不足。"))
    if blind_spots and blind_spots.get("affected_housing_count", 0) > 0:
        suggestions.append(
            "1km 服务盲区：%d 个住宅小区 1 公里内无菜市场/药店/小学，建议优先布局社区菜场与便民药房。"
            % blind_spots["affected_housing_count"])
    if len(suggestions) < 2:
        suggestions.append("建议持续跟踪等时圈内 POI 数据变化，定期评估便民设施覆盖情况。")
    suggestions = suggestions[:6]

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
        "residential_category": RESIDENTIAL_CATEGORY,
        "required_facility_labels": REQUIRED_FACILITY_LABELS,
        "v2_defaults": {
            "minutes_options": MINUTES_OPTIONS,
            "walk_speed": DEFAULT_WALK_SPEED,
            "crossing_sec": DEFAULT_CROSSING_SEC,
            "overpass_sec": DEFAULT_OVERPASS_SEC,
        },
        "blind_spot_radius": BLIND_RADIUS,
        "blind_spot_categories": BLIND_CATEGORIES,
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


@app.route("/api/coords/convert")
def api_coords_convert():
    """
    坐标转换（v3）：调用百度 geoconv/v1。
    参数：coords=<lng>,<lat>[;<lng>,<lat>...]（最多 50 个，顺序为经度,纬度）、
          from ∈ {wgs84, gcj02, bd09ll}（默认 wgs84），统一转为 BD-09(to=5)。
    返回 {status, coords: [{lng,lat},...]}；百度 status!=0 或网络失败返回 {error}。
    """
    raw = (request.args.get("coords") or "").strip()
    if not raw:
        return jsonify({"error": "缺少 coords 参数（lng,lat;lng,lat…）"}), 400
    from_key = (request.args.get("from") or "wgs84").strip().lower()
    if from_key not in GEO_CONV_FROM:
        return jsonify({"error": "from 仅支持 wgs84/gcj02/bd09ll"}), 400
    try:
        pairs = []
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(",")
            if len(parts) != 2:
                return jsonify({"error": "coords 每项需为 lng,lat"}), 400
            pairs.append([float(parts[0]), float(parts[1])])
    except (TypeError, ValueError):
        return jsonify({"error": "coords 坐标值非法"}), 400
    if not pairs or len(pairs) > 50:
        return jsonify({"error": "coords 需为 1-50 个 lng,lat 点"}), 400
    if from_key == "bd09ll":
        # BD-09 转 BD-09 无意义，直接回显
        return jsonify({"status": 0, "from": from_key,
                        "coords": [{"lng": p[0], "lat": p[1]} for p in pairs]})
    if not BAIDU_AK:
        return jsonify({"error": "请在环境变量 BAIDU_MAP_AK 配置百度地图密钥后重试。"}), 502
    try:
        pairs = []
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(",")
            if len(parts) != 2:
                return jsonify({"error": "coords 每项需为 lng,lat"}), 400
            pairs.append([float(parts[0]), float(parts[1])])
    except (TypeError, ValueError):
        return jsonify({"error": "coords 坐标值非法"}), 400
    if not pairs or len(pairs) > 50:
        return jsonify({"error": "coords 需为 1-50 个 lng,lat 点"}), 400
    try:
        data = baidu_get("/geoconv/v1/", {
            "coords": ";".join("%s,%s" % (p[0], p[1]) for p in pairs),
            "from": GEO_CONV_FROM[from_key],
            "to": 5,
        })
    except Exception as exc:
        return jsonify({"error": "坐标转换失败：%s" % exc}), 502
    if not data or data.get("status") != 0:
        return jsonify({"error": "坐标转换失败：%s" % ((data or {}).get("message") or "status!=0")}), 502
    results = data.get("result") or []
    return jsonify({
        "status": 0,
        "from": from_key,
        "coords": [{"lng": float(r["x"]), "lat": float(r["y"])} for r in results],
    })


def _norm_obstacles(obstacles):
    """规范化障碍多边形：[[{lng,lat},...],...] -> [[(lng,lat),...],...]；非法返回 None"""
    if not obstacles:
        return []
    out = []
    try:
        for poly in obstacles:
            pts = []
            for p in poly:
                pts.append((float(p.get("lng")), float(p.get("lat"))))
            if len(pts) >= 3:
                out.append(pts)
    except (TypeError, ValueError, AttributeError):
        return None
    return out


@app.route("/api/isochrone", methods=["POST"])
def api_isochrone():
    """
    创建异步等时圈计算任务，立即返回 {job_id}。
    参数：{lng, lat, minutes(数字或数组), rays=36, steps=8,
           walk_speed=1.2, crossing_sec=30, overpass_sec=120, obstacles=[...]}
    后端内存任务表：daemon 线程执行，任务完成后结果缓存
    （key 含 lng/lat/minutes/rays/steps/walk_speed/crossing_sec/overpass_sec/obstacles）。
    """
    body = request.get_json(silent=True) or {}
    try:
        lng = float(body.get("lng"))
        lat = float(body.get("lat"))
    except (TypeError, ValueError):
        return jsonify({"error": "缺少合法的 lng/lat 参数"}), 400
    # minutes 兼容数字或数组
    minutes_raw = body.get("minutes", DEFAULT_MINUTES)
    if isinstance(minutes_raw, (list, tuple)):
        try:
            minutes_list = [int(m) for m in minutes_raw]
        except (TypeError, ValueError):
            return jsonify({"error": "minutes 数组需为数字列表"}), 400
    else:
        try:
            minutes_list = [int(minutes_raw)]
        except (TypeError, ValueError):
            return jsonify({"error": "minutes 需为数字或数字数组"}), 400
    if not minutes_list or not all(0 < m <= 60 for m in minutes_list):
        return jsonify({"error": "minutes 需在 1-60 之间"}), 400
    minutes_list = sorted(set(minutes_list))   # 去重排序，保证 rings 顺序
    rays = int(body.get("rays", DEFAULT_RAYS) or DEFAULT_RAYS)
    steps = int(body.get("steps", DEFAULT_STEPS) or DEFAULT_STEPS)
    if rays not in (24, 36, 48):
        return jsonify({"error": "rays 仅支持 24/36/48"}), 400
    if not (1 <= steps <= 16):
        return jsonify({"error": "steps 需在 1-16 之间"}), 400
    try:
        walk_speed = float(body.get("walk_speed", DEFAULT_WALK_SPEED))
        crossing_sec = int(body.get("crossing_sec", DEFAULT_CROSSING_SEC))
        overpass_sec = int(body.get("overpass_sec", DEFAULT_OVERPASS_SEC))
    except (TypeError, ValueError):
        return jsonify({"error": "walk_speed/crossing_sec/overpass_sec 参数非法"}), 400
    if not (0.5 <= walk_speed <= 3.0):
        return jsonify({"error": "walk_speed 需在 0.5-3.0 m/s 之间"}), 400
    if crossing_sec < 0 or overpass_sec < 0:
        return jsonify({"error": "crossing_sec/overpass_sec 不能为负"}), 400
    obstacles = _norm_obstacles(body.get("obstacles"))
    if obstacles is None:
        return jsonify({"error": "obstacles 参数格式非法（应为若干 {lng,lat} 多边形）"}), 400
    # heat_exact=v3 热力图采样模式：true 调步行 API 精确计算，false 走射线插值（不额外调接口）
    heat_exact_raw = body.get("heat_exact", True)
    if not isinstance(heat_exact_raw, bool):
        return jsonify({"error": "heat_exact 需为布尔值"}), 400
    heat_exact = heat_exact_raw

    cache_key = (
        round(lng, 6), round(lat, 6), tuple(minutes_list), rays, steps,
        walk_speed, crossing_sec, overpass_sec,
        tuple(tuple(poly) for poly in obstacles),
        heat_exact,
    )
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
            "minutes_raw": minutes_raw,
            "minutes_list": minutes_list,
            "rays": rays,
            "steps": steps,
            "walk_speed": walk_speed,
            "crossing_sec": crossing_sec,
            "overpass_sec": overpass_sec,
            "obstacles": obstacles,
            "heat_exact": heat_exact,
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
    parser = argparse.ArgumentParser(description="15分钟便民生活圈体检报告 v2")
    parser.add_argument("--port", type=int, default=5000, help="监听端口（默认 5000）")
    args = parser.parse_args()
    print("15分钟便民生活圈服务 v3 启动，监听 0.0.0.0:%d，ak_configured=%s" % (args.port, bool(BAIDU_AK)))
    app.run(host="0.0.0.0", port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()