"""等时圈算法（核心）：射线二分 + 批量矩阵算路 + 过街阻抗统计 + 边界平滑。"""
import math

from .. import config
from .baidu_client import _executor, batch_walking_matrix, walking_duration
from .geometry import LAT_1M, _lng_1m, polygon_metrics, segment_hits_obstacles, straight_circle


def _batch_durations(center, pts, crossing_sec, overpass_sec):
    """
    批量获取「中心 → 多个候选点」的路网步行耗时（秒）。
      - 优先走百度 routermatrix 批量矩阵（一次请求算多对，含内存缓存）；
      - 批量接口不可用/配额耗尽时自动回退：并发逐点调 directionlite；
    返回与 pts 对齐的耗时列表（失败点 = INF_TRAVEL）。
    """
    n = len(pts)
    if n == 0:
        return []
    matrix = batch_walking_matrix(center, pts, crossing_sec, overpass_sec)
    if matrix is not None:
        return [matrix.get(i, config.INF_TRAVEL) for i in range(n)]
    # 回退：并发逐点 directionlite
    futures = {i: _executor.submit(walking_duration, center, p, crossing_sec, overpass_sec)
               for i, p in enumerate(pts)}
    out = []
    for i in range(n):
        try:
            eff, _, _, _, _ = futures[i].result(timeout=60)
        except Exception:
            eff = config.INF_TRAVEL
        out.append(eff)
    return out


def compute_isochrone(center, minutes, rays, steps, crossing_sec, overpass_sec,
                      obstacles=None, progress_cb=None, force_walk=False):
    """
    沿中心点 360° 均匀发射 rays 条射线，每条射线上用二分法找「有效步行耗时 =
    minutes 分钟」的边界点，连接成多边形即为等时圈。

    v4 性能优化：二分每轮迭代把 rays 个候选点合并成【一次批量算路请求】
    （百度 routermatrix/v2/walking，间隔性回退 directionlite），把等时圈的
    API 调用量从近似 rays×steps 降到约 steps 次；批量接口不可用时自动回退
    到并发逐点调用，保证功能稳定。

    有效耗时 = 路网 duration + 过街成本（v2）；若中心到候选点的直线段穿过
    障碍多边形，则视为不可达（INF），二分自然内缩形成「凹陷」。

    返回 (boundary, crossing_stats, any_steps_available)
      - boundary: [(lng,lat), ...]
      - crossing_stats: {crossings, overpasses}（该圈 rays 条射线边界点的过街数之和）
      - any_steps_available: 是否至少一次解析到路段明细（决定 crossing_mode）
    """
    obstacles = obstacles or []
    max_dist = config.MAX_SEARCH_DIST
    lng1m = _lng_1m(center[1])
    limit = minutes * 60.0
    boundary_points = []
    total_cross = 0
    total_over = 0
    any_steps = False

    los = [0.0] * rays
    his = [max_dist] * rays
    dirs = [(math.cos(2.0 * math.pi * i / rays), math.sin(2.0 * math.pi * i / rays))
            for i in range(rays)]

    for it in range(steps):                      # 每轮迭代 = 1 次批量化请求
        mids = []
        idx_free = []
        for i, (dx, dy) in enumerate(dirs):
            mid = (los[i] + his[i]) / 2.0
            p = (center[0] + dx * mid * lng1m, center[1] + dy * mid * LAT_1M)
            if segment_hits_obstacles(center, p, obstacles):
                his[i] = mid                     # 被障碍挡住：视为不可达，收缩上界
                continue
            mids.append(p)
            idx_free.append(i)
        if mids:
            # 批二分统一走批量矩阵（含自动回退并发逐点），无 AK 时逐点返回不可达
            durs = _batch_durations(center, mids, crossing_sec, overpass_sec)
            for i, t in zip(idx_free, durs):
                if t <= limit:
                    los[i] = (los[i] + his[i]) / 2.0
                else:
                    his[i] = (los[i] + his[i]) / 2.0
        if progress_cb:
            progress_cb(min(rays, (it + 1) * rays // steps), rays)

    for i, (dx, dy) in enumerate(dirs):
        boundary_points.append(
            (center[0] + dx * los[i] * lng1m, center[1] + dy * los[i] * LAT_1M))

    # 过街统计口径：对每条射线的 boundary 候选点并发调 directionlite 解析路段明细
    stat_futures = []
    for (dx, dy), bp in zip(dirs, boundary_points):
        if segment_hits_obstacles(center, bp, obstacles):
            stat_futures.append(None)
        else:
            stat_futures.append(_executor.submit(
                walking_duration, center, bp, crossing_sec, overpass_sec))
    for f in stat_futures:
        if f is None:
            continue
        try:
            _, _, cr, ov, sa = f.result(timeout=60)
        except Exception:
            cr = ov = 0
            sa = False
        total_cross += cr
        total_over += ov
        if sa:
            any_steps = True
    return boundary_points, {"crossings": total_cross, "overpasses": total_over}, any_steps


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