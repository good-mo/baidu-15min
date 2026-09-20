"""报告生成流水线：多时圈 → POI 采集 → 分类统计 → 热力/盲区/灰色区域 → 健康指数与建议。"""
import threading

from .. import config
from .baidu_client import BaiduQuotaError, _executor, search_pois, walking_duration
from .blind import detect_blind_spots
from .geometry import _poly_to_tuples, point_in_polygon
from .gray import detect_gray_areas
from .heat import compute_heat
from .isochrone import compute_isochrone, polygon_metrics, smooth_boundary, straight_circle
from .spatial import _haversine_km


def build_report(job):
    """
    完整报告流水线：
      多时圈（每圈：等时圈 → 平滑 → 度量 → 阻抗系数 → 过街统计）
      → POI 采集（8 类 + 住宅辅助）→ 圈内过滤 → 分类统计与最近设施步行分钟
      → 等时圈热力图 → 1km 服务盲区核验 → 灰色区域自动诊疗 → 结论建议。
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

        # ---- POI 采集：8 类 + 住宅（辅助，不计入达标），并发采集加速 ----
        all_pois = []
        _poi_lock = threading.Lock()
        poi_quota = {"exhausted": False, "message": ""}

        def _collect(cat):
            try:
                pois = search_pois(cat, center)
                for p in pois:
                    p["category"] = cat["name"]
                with _poi_lock:
                    all_pois.extend(pois)
                return len(pois)
            except BaiduQuotaError as exc:
                with _poi_lock:
                    poi_quota["exhausted"] = True
                    poi_quota["message"] = getattr(exc, "message", "") or str(exc)
                return 0  # 单类 POI 检索失败不影响其它类别
            except Exception:
                return 0  # 单类 POI 检索失败不影响其它类别

        poi_futures = [_executor.submit(_collect, cat) for cat in config.CATEGORIES]
        poi_futures.append(_executor.submit(_collect, config.RESIDENTIAL_CATEGORY))
        _poi_hits = 0
        for fut in poi_futures:
            try:
                _poi_hits += fut.result(timeout=120)
            except Exception:
                pass
        residential_pois = [p for p in all_pois if p["category"] == "住宅"]

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
        for cat in config.CATEGORIES:
            lst = [p for p in in_pois if p["category"] == cat["name"]]
            nearest, nearest_d = nearest_poi(center[0], center[1], lst)
            walk_minutes = None
            if nearest:
                # 对每类最近设施调用一次步行 API，用 effective_duration 换算真实步行分钟（含过街）
                eff, _, _, _, _ = walking_duration(center, (nearest["lng"], nearest["lat"]),
                                                   crossing_sec, overpass_sec)
                walk_minutes = round(eff / 60.0, 1) if eff < config.INF_TRAVEL else None
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
        for cat, rep in zip(config.CATEGORIES, categories_report):
            if rep["satisfied"]:
                health_index += cat["weight"] * 100.0
                satisfied_count += 1
        health_index = round(health_index, 1)

        # ---- 等时圈热力图（主圈网格采样，并发 + 失败降级插值） ----
        job["stage"] = "计算热力图…"
        heat_mode_cell = [None]
        heat_list, heat_step = compute_heat(
            center, main_boundary, main_ring["minutes"], rays,
            crossing_sec, overpass_sec, obstacles=obstacles,
            exact=heat_exact, max_points=config.HEAT_MAX_POINTS,
            mode_sink=heat_mode_cell,
        )
        heat_vals = [h["minutes"] for h in heat_list if h["minutes"] is not None]
        heat_stats = {
            "points": len(heat_list),
            "sampled": len(heat_vals),
            "step_m": heat_step,
            "heat_mode": heat_mode_cell[0] or ("exact" if heat_exact else "interp"),
            "avg_minutes": round(sum(heat_vals) / len(heat_vals), 1) if heat_vals else None,
            "gt10_ratio": round(len([v for v in heat_vals if v > 10.0]) / len(heat_vals), 4) if heat_vals else 0.0,
        }

        # ---- 1公里服务盲区核验 ----
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
            # 多时圈
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
            # 热力图 + 1km 服务盲区
            "heat": heat_list,
            "heat_stats": heat_stats,
            "blind_spots": blind_spots,
            # 结论
            "suggestions": suggestions,
            "verdict": verdict,
            # 元信息（诚实标注估算）
            "meta": {
                "v2": True,
                "v3": True,
                "heat_exact": heat_exact,
                "walk_speed": walk_speed,
                "crossing_sec": crossing_sec,
                "overpass_sec": overpass_sec,
                "obstacles": [[list(p) for p in poly] for poly in obstacles],
                "crossing_mode": crossing_mode,
                # POI 配额提示：百度日配额/QPS 超限时报告仍完成，但设施统计为空
                "poi_warning": {
                    "quota_exhausted": poi_quota["exhausted"],
                    "count_warning": poi_quota["exhausted"] or len(in_pois) == 0,
                    "message": "百度地图日配额超限（302/401），设施（POI）数据暂不可用。请稍后重试或更换密钥。"
                    if poi_quota["exhausted"] else "",
                },
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
        "商业": "该区域商业零售配套薄弱，建议引入社区超市、菜市场或便利店。",
        "交通": "该区域公共交通接驳不足，建议增设公交线路或优化公交、地铁接驳。",
        "教育": "该区域教育资源不足，建议引入基础教育配套（小学/幼儿园）。",
        "餐饮": "该区域餐饮选择有限，建议丰富社区餐饮业态。",
        "金融": "该区域金融服务网点少，建议增设自助银行或社区金融服务点。",
        "生活服务": "该区域生活服务类设施不足，建议引入快递驿站、理发、洗衣等便民服务。",
        "文体": "该区域文体休闲设施缺乏，建议增设公园绿地、健身器材或社区活动中心。",
    }
    priority_rank = {"极高": 0, "高": 1, "中": 2, "低": 3}
    for ga in gray_areas:
        txt = "发现灰色区域（约 %.1f 万 m²，%s 缺失）：%s" % (
            ga["area_km2"] * 1e4, "/".join(ga["missing"]) or "设施薄弱", ga["suggestions"][0])
        suggestions.append((priority_rank.get(ga.get("priority", "低"), 3), txt))
    suggestions.sort(key=lambda x: x[0])
    suggestions = [txt for _, txt in suggestions][:3]

    for rep in categories_report:
        if not rep["satisfied"] and rep["name"] in template:
            suggestions.append(template[rep["name"]])
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