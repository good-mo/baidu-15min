"""空间索引与地理工具：经纬度网格分桶（_SpatialBucket）、bbox、球面距离。"""
import math

from .. import config
from .geometry import LAT_1M, _lng_1m


def bbox_of(boundary):
    """多边形包围盒 (min_lng, min_lat, max_lng, max_lat)"""
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


class _SpatialBucket:
    """
    经纬度网格分桶的空间索引：把大量设施点按桶散列，查询某点的最近设施时
    只扫描「以查询点为圆心、覆盖半径」所触及的桶，避免 O(N×M) 全量扫描。
    用于灰色区域（4 类必需设施）与 1km 服务盲区（3 类设施）的最近距离计算。
    """

    def __init__(self, points, bucket_deg=0.002):
        self._bucket_deg = bucket_deg
        self._buckets = {}
        for p in points:
            key = (int(math.floor(p["lng"] / bucket_deg)), int(math.floor(p["lat"] / bucket_deg)))
            self._buckets.setdefault(key, []).append(p)

    def nearest_m(self, lng, lat, max_m):
        """返回距 (lng,lat) 最近的设施直线距离（米）；max_m 内无设施则返回 max_m"""
        best = max_m
        for p in self._iter_nearby(lng, lat, max_m):
            d = _haversine_km(lat, lng, p["lat"], p["lng"]) * 1000.0
            if d < best:
                best = d
        return best

    def nearest(self, lng, lat, max_m):
        """返回 (距离米, 设施 dict)；max_m 内无设施返回 (None, None)"""
        best_p, best_d = None, max_m
        for p in self._iter_nearby(lng, lat, max_m):
            d = _haversine_km(lat, lng, p["lat"], p["lng"]) * 1000.0
            if d < best_d:
                best_p, best_d = p, d
        return (best_p, best_d) if best_p is not None else (None, None)

    def _iter_nearby(self, lng, lat, max_m):
        """遍历以查询点为圆心、覆盖半径 max_m 的桶内的所有设施点"""
        d_lng = max_m * _lng_1m(lat)
        d_lat = max_m * LAT_1M
        lo_lng = int(math.floor((lng - d_lng) / self._bucket_deg))
        hi_lng = int(math.floor((lng + d_lng) / self._bucket_deg))
        lo_lat = int(math.floor((lat - d_lat) / self._bucket_deg))
        hi_lat = int(math.floor((lat + d_lat) / self._bucket_deg))
        for bl in range(lo_lng, hi_lng + 1):
            for ba in range(lo_lat, hi_lat + 1):
                cell = self._buckets.get((bl, ba))
                if not cell:
                    continue
                for p in cell:
                    yield p