"""1公里服务盲区核验（评分口径）：住宅小区/网格候选点缺失三类必需设施即判盲区。"""
import math

from .. import config
from .geometry import LAT_1M, _lng_1m, point_in_polygon
from .spatial import _SpatialBucket, bbox_of


def detect_blind_spots(center, boundary, all_pois, residential_pois=None):
    """
    1公里服务盲区核验（评分口径，与灰色区域并存互不影响）：
      对候选点检查其 1km（直线）内是否有【菜市场】【药店】【小学】三类设施；
      三类全部没有 → 判为「服务盲区点位」。
      候选来源：
        1) 住宅小区 POI（category=住宅）；
        2) 等时圈内网格点（步长 150m）。
      设施直接取自已收集 POI（菜市场=商业类含「菜市场」、药店=医疗类含「药店」、
      小学=教育类含「小学」），不额外调用接口。
    """
    # 设施池（网格分桶空间索引，最近距离查询从 O(N) 降到 O(桶内)）
    fac_pools = [[] for _ in config.BLIND_FACILITY_RULES]
    for poi in all_pois:
        cname = poi.get("category", "") or ""
        name = poi.get("name", "") or ""
        for i, (_label, cat_key, kws) in enumerate(config.BLIND_FACILITY_RULES):
            if cname == cat_key and any(kw in name for kw in kws):
                fac_pools[i].append(poi)
                break
    fac_buckets = [_SpatialBucket(pool) if pool else None for pool in fac_pools]

    def nearest_m(label_idx, lng, lat):
        """该类最近设施直线距离（米）；该类别无任何设施返回 None（视为缺失）"""
        bucket = fac_buckets[label_idx]
        if bucket is None:
            return None
        p, d = bucket.nearest(lng, lat, config.BLIND_RADIUS)
        return d if d != config.BLIND_RADIUS else None

    # 候选点：住宅 POI + 网格点
    candidates = [(rp["lng"], rp["lat"], "住宅小区") for rp in (residential_pois or [])]
    min_lng, min_lat, max_lng, max_lat = bbox_of(boundary)
    lng1m = _lng_1m(center[1])
    cols = int(math.ceil((max_lng - min_lng) / (config.BLIND_GRID_STEP * lng1m))) + 1
    rows = int(math.ceil((max_lat - min_lat) / (config.BLIND_GRID_STEP * LAT_1M))) + 1
    grid_added = 0
    if 0 < rows <= 400 and 0 < cols <= 400:
        for r in range(rows):
            lat = min_lat + r * config.BLIND_GRID_STEP * LAT_1M
            for c in range(cols):
                lng = min_lng + c * config.BLIND_GRID_STEP * lng1m
                if point_in_polygon(lng, lat, boundary):
                    candidates.append((lng, lat, "网格点"))
                    grid_added += 1
                    if grid_added >= config.BLIND_MAX_GRID:
                        break
            if grid_added >= config.BLIND_MAX_GRID:
                break

    points = []
    housing_count = 0
    for (lng, lat, source) in candidates:
        nearest_map = {}
        has_nearby = False   # 三类中是否至少一类在 1km 内
        for i, label in enumerate(config.BLIND_CATEGORIES):
            d = nearest_m(i, lng, lat)
            nearest_map[label] = None if d is None else round(d, 1)
            if d is not None and d <= config.BLIND_RADIUS:
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