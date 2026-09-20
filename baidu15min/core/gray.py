"""灰色区域检测（v1）+ 自动诊疗（v2）：网格采样 + 空间索引 + BFS 聚类。"""
import math

from .. import config
from .baidu_client import reverse_geocode
from .geometry import LAT_1M, _lng_1m, point_in_polygon
from .spatial import _SpatialBucket, _haversine_km, bbox_of


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
    pois_by_rule = [[] for _ in config.REQUIRED_FACILITY_LABELS]
    for poi in all_pois:
        name = poi.get("name", "") or ""
        cname = poi.get("category", "") or ""
        for idx, (label, keywords) in enumerate(config.REQUIRED_FACILITY_RULES):
            if cname == label and any(kw in name for kw in keywords):
                pois_by_rule[idx].append(poi)

    # 空间索引：为每类必需设施建网格分桶，最近距离查询从 O(N) 降到 O(桶数)
    rule_buckets = [_SpatialBucket(lst) if lst else None for lst in pois_by_rule]

    min_lng, min_lat, max_lng, max_lat = bbox_of(boundary)
    lng1m = _lng_1m(center_lat)
    cols = int(math.ceil((max_lng - min_lng) / (config.GRAY_GRID_STEP * lng1m))) + 1
    rows = int(math.ceil((max_lat - min_lat) / (config.GRAY_GRID_STEP * LAT_1M))) + 1
    if rows <= 0 or cols <= 0 or rows * cols > 200000:
        return []

    def nearest_m(lng, lat, bucket):
        """到该类最近设施直线距离（米）；无该设施直接返回 GRAY_DIST（视为缺失）"""
        if bucket is None:
            return config.GRAY_DIST
        return bucket.nearest_m(lng, lat, config.GRAY_DIST)

    gray_points = []   # (lng, lat)
    for r in range(rows):
        lat = min_lat + r * config.GRAY_GRID_STEP * LAT_1M
        for c in range(cols):
            lng = min_lng + c * config.GRAY_GRID_STEP * lng1m
            if not point_in_polygon(lng, lat, boundary):
                continue
            far_cnt = 0
            for bucket in rule_buckets:
                if bucket is None:
                    far_cnt += 1
                    continue
                if bucket.nearest_m(lng, lat, config.GRAY_DIST) > config.GRAY_DIST:
                    far_cnt += 1
            if far_cnt >= 2:
                gray_points.append((lng, lat))

    if not gray_points:
        return []

    # 转网格行列索引做 BFS 聚类（4-邻域）
    gset = set()
    for (lng, lat) in gray_points:
        r = int(round((lat - min_lat) / (config.GRAY_GRID_STEP * LAT_1M)))
        c = int(round((lng - min_lng) / (config.GRAY_GRID_STEP * lng1m)))
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
        if len(members) >= config.GRAY_MIN_POINTS:   # 过滤碎片簇
            clusters.append(members)

    residential_pois = residential_pois or []
    result = []
    for members in clusters:
        lngs = [min_lng + c * config.GRAY_GRID_STEP * lng1m for (r, c) in members]
        lats = [min_lat + r * config.GRAY_GRID_STEP * LAT_1M for (r, c) in members]
        clng = sum(lngs) / len(lngs)
        clat = sum(lats) / len(lats)
        # 簇 500 米内缺失的必需设施清单（空间索引加速）
        missing = []
        for idx, (label, keywords) in enumerate(config.REQUIRED_FACILITY_RULES):
            bucket = rule_buckets[idx]
            if bucket is None:
                missing.append(config.REQUIRED_FACILITY_LABELS[idx])
            elif any(bucket.nearest_m(lo, la, config.GRAY_DIST) > config.GRAY_DIST for (lo, la) in zip(lngs, lats)):
                missing.append(config.REQUIRED_FACILITY_LABELS[idx])
        # 自动诊疗
        affected_res_count = sum(
            1 for rp in residential_pois
            if _haversine_km(clat, clng, rp["lat"], rp["lng"]) * 1000.0 <= config.RES_AFFECTED_RADIUS
        )
        affected_level = _affected_level(affected_res_count)
        severity = config.GRAY_SEVERITY_BY_MISS.get(len(missing), "匮乏")
        suggestions = [config.GRAY_MISS_SUGGESTION[m] for m in missing if m in config.GRAY_MISS_SUGGESTION]
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
            "area_m2": len(members) * config.GRAY_GRID_STEP * config.GRAY_GRID_STEP,
            "area_km2": len(members) * config.GRAY_GRID_STEP * config.GRAY_GRID_STEP / 1e6,
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