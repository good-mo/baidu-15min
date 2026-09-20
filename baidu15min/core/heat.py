"""v3 等时圈热力图：主圈内网格采样，exact 精确算路 / interp 射线插值，支持失败降级。"""
import math

from .. import config
from .baidu_client import _executor, batch_walking_matrix, walking_duration
from .geometry import LAT_1M, _lng_1m, point_in_polygon, segment_hits_obstacles
from .spatial import bbox_of


def compute_heat(center, boundary, minutes, rays, crossing_sec, overpass_sec,
                 obstacles=None, exact=True, max_points=config.HEAT_MAX_POINTS,
                 mode_sink=None):
    """
    等时圈热力图：主圈 bbox 内按 100m 网格取多边形内点（点数超上限自动放大步长）。
      - exact=True（默认）：并发调步行 API（复用缓存）取 effective_duration 换算分钟；
        单个点失败/不可达时自动降级为「射线插值」补齐（诚实标注 exact+interp）；
      - exact=False（节省模式）：纯射线插值估算，按点相对中心的角度在相邻两条射线的
        边界半径之间线性插值出边界半径，再按 半径/边界半径 × minutes 估算分钟数，
        不增加 API 调用。
    返回 (heat_list, step_used)；heat = [{lng, lat, minutes} | minutes=None(不可达/失败)]。
    """
    obstacles = obstacles or []
    min_lng, min_lat, max_lng, max_lat = bbox_of(boundary)
    lng1m = _lng_1m(center[1])

    # 网格采样：步长 100m 起步，点数超限自动放大步长重取
    factor = 1
    pts = []
    step = config.HEAT_BASE_STEP
    while True:
        step = config.HEAT_BASE_STEP * factor
        cols = int(math.ceil((max_lng - min_lng) / (step * lng1m))) + 1
        rows = int(math.ceil((max_lat - min_lat) / (step * LAT_1M))) + 1
        pts = []
        if 0 < rows <= 400 and 0 < cols <= 400:
            for r in range(rows):
                lat = min_lat + r * step * LAT_1M
                for c in range(cols):
                    lng = min_lng + c * step * lng1m
                    if point_in_polygon(lng, lat, boundary):
                        pts.append((lng, lat))
        if len(pts) <= max_points or factor >= 5:
            break
        factor += 1

    # 从 boundary 重建每条射线的边界半径（射线插值基础设施）
    ray_radii = [0.0] * rays
    step_ang = 2.0 * math.pi / rays
    for p in boundary:
        a_ang = math.atan2((p[1] - center[1]) / LAT_1M, (p[0] - center[0]) / lng1m)
        if a_ang < 0:
            a_ang += 2.0 * math.pi
        idx = int(a_ang / step_ang) % rays
        r_now = math.hypot((p[0] - center[0]) / lng1m, (p[1] - center[1]) / LAT_1M)
        if r_now > ray_radii[idx]:
            ray_radii[idx] = r_now

    def _interpolated_minutes(lng, lat):
        """射线插值估算（供 exact 降级与节省模式共用）"""
        radius_p = math.hypot((lng - center[0]) / lng1m, (lat - center[1]) / LAT_1M)
        a = math.atan2((lat - center[1]) / LAT_1M, (lng - center[0]) / lng1m)
        if a < 0:
            a += 2.0 * math.pi
        idx = int(a / step_ang) % rays
        f = (a - idx * step_ang) / step_ang
        r_b = ray_radii[idx] + (ray_radii[(idx + 1) % rays] - ray_radii[idx]) * f
        if r_b <= 1e-9:
            return None
        return round(min(max(minutes * radius_p / r_b, 0.1), 40.0), 1)

    heat = []
    if exact:
        # 批量算路（routermatrix 一次 40 终点）替代逐点调用，QPS 下快几倍~几十倍；
        # 命中缓存或接口失败时精细化处理：失败/不可达的点降级为射线插值补齐。
        blocked = [(lng, lat) for (lng, lat) in pts
                   if segment_hits_obstacles(center, (lng, lat), obstacles)]
        pending = [(lng, lat) for (lng, lat) in pts
                   if not segment_hits_obstacles(center, (lng, lat), obstacles)]
        for (lng, lat) in blocked:
            heat.append({"lng": round(lng, 6), "lat": round(lat, 6), "minutes": None})
        durations = batch_walking_matrix(center, pending, crossing_sec, overpass_sec) if pending else None
        pending_index = {p: i for i, p in enumerate(pending)}
        degraded = 0
        for (lng, lat) in pending:
            dur = (durations or {}).get(pending_index[(lng, lat)])
            if dur is None:
                # 批量接口不可用/超额：回退单点 directionlite（并发）
                try:
                    eff, _, _, _, _ = walking_duration(center, (lng, lat),
                                                       crossing_sec, overpass_sec)
                except Exception:
                    eff = config.INF_TRAVEL
            else:
                eff = dur
            if eff >= config.INF_TRAVEL:
                m = _interpolated_minutes(lng, lat)
                if m is not None:
                    degraded += 1
                heat.append({"lng": round(lng, 6), "lat": round(lat, 6), "minutes": m})
            else:
                m = round(eff / 60.0, 1)
                heat.append({"lng": round(lng, 6), "lat": round(lat, 6),
                             "minutes": min(m, 40.0)})
        if degraded == 0:
            heat_mode = "exact"
        elif degraded < len([h for h in heat if h["minutes"] is not None]):
            heat_mode = "exact+interp"
        else:
            heat_mode = "interp"
        if mode_sink is not None:
            mode_sink[0] = heat_mode
    else:
        for (lng, lat) in pts:
            m = _interpolated_minutes(lng, lat)
            heat.append({"lng": round(lng, 6), "lat": round(lat, 6), "minutes": m})
        if mode_sink is not None:
            mode_sink[0] = "interp"
    return heat, step