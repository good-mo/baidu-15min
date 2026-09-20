"""平面/球面几何工具：线段相交、点在多边形内、多边形度量、经纬度换算。"""
import math

from .. import config

LAT_1M = config.LAT_1M


def _lng_1m(lat):
    """该纬度下每米对应的经度度数"""
    return 1.0 / (111320.0 * math.cos(math.radians(lat)))


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


def polygon_metrics(boundary, ref_lat):
    """
    计算多边形面积（km²，shoelace 公式，经纬度换算为平面米坐标）与周长（km）。
    """
    lng1m = _lng_1m(ref_lat)
    ox, oy = boundary[0]
    pts = [((x - ox) / lng1m, (y - oy) / LAT_1M) for (x, y) in boundary]
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


def _poly_to_tuples(poly):
    """把障碍多边形统一为 [(lng,lat), ...]（兼容 {lng,lat} dict 与 tuple/list）"""
    out = []
    for p in poly:
        if isinstance(p, dict):
            out.append((float(p.get("lng")), float(p.get("lat"))))
        else:
            out.append((float(p[0]), float(p[1])))
    return out