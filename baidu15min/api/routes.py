"""HTTP API 路由：接口契约与 Flask 版完全一致。"""
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Body, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from .. import config
from ..core.baidu_client import baidu_get, geocode, reverse_geocode
from ..core.jobs import _jobs, _jobs_lock, _results_cache, create_job, run_job_async

router = APIRouter()

STATIC_INDEX = Path(__file__).resolve().parent.parent / "static" / "index.html"


class IsochroneRequest(BaseModel):
    """等时圈计算请求（宽松字段 + 业务手动校验，错误消息与旧版一致）"""
    lng: Optional[float] = None
    lat: Optional[float] = None
    minutes: Any = config.DEFAULT_MINUTES
    rays: Any = config.DEFAULT_RAYS
    steps: Any = config.DEFAULT_STEPS
    walk_speed: Any = config.DEFAULT_WALK_SPEED
    crossing_sec: Any = config.DEFAULT_CROSSING_SEC
    overpass_sec: Any = config.DEFAULT_OVERPASS_SEC
    obstacles: Any = Field(default_factory=list)
    heat_exact: Any = True


@router.get("/")
def index():
    html = STATIC_INDEX.read_text(encoding="utf-8")
    if config.BAIDU_DISPLAY_AK:
        # 直载 getscript 引擎（绕开百度入口脚本的内部 document.write），规避异步注入被浏览器拦截
        sdk = ('<script>window.BMAP_PROTOCOL = "https";</script>'
               '<script id="baidu-sdk" '
               'src="https://api.map.baidu.com/getscript?type=webgl&v=1.0&ak=%s&services=" '
               'onerror="window.__BAIDU_SDK_FAILED__=1"></script>') % config.BAIDU_DISPLAY_AK
        html = html.replace("__BAIDU_SDK__", sdk)
    else:
        html = html.replace("__BAIDU_SDK__", "")
    return HTMLResponse(html)


@router.get("/api/health")
def api_health():
    return JSONResponse({
        "status": "ok",
        "ak_configured": bool(config.BAIDU_DISPLAY_AK),
        "computed_ak_configured": bool(config.BAIDU_AK),
    })


@router.get("/api/config")
def api_config():
    return JSONResponse({
        "ak": config.BAIDU_DISPLAY_AK,
        "ak_configured": bool(config.BAIDU_DISPLAY_AK),
        "computed_ak_configured": bool(config.BAIDU_AK),
        "default_center": config.DEFAULT_CENTER,
        "samples": config.SAMPLE_STREETS,
        "minutes": config.DEFAULT_MINUTES,
        "rays": config.DEFAULT_RAYS,
        "categories": config.CATEGORIES,
        "residential_category": config.RESIDENTIAL_CATEGORY,
        "required_facility_labels": config.REQUIRED_FACILITY_LABELS,
        "v2_defaults": {
            "minutes_options": config.MINUTES_OPTIONS,
            "walk_speed": config.DEFAULT_WALK_SPEED,
            "crossing_sec": config.DEFAULT_CROSSING_SEC,
            "overpass_sec": config.DEFAULT_OVERPASS_SEC,
        },
        "blind_spot_radius": config.BLIND_RADIUS,
        "blind_spot_categories": config.BLIND_CATEGORIES,
        "version": "v4",
        "framework": "fastapi",
        "optimization": {
            "qps": config.API_QPS,
            "workers": config.WORKER_NUM,
            "matrix_batch": config.MATRIX_MAX_DEST,
            "batch_matrix": True,
            "concurrent_poi": True,
        },
    })


@router.get("/api/geocode")
def api_geocode(address: Optional[str] = Query(default=None), city: str = Query(default="")):
    address = (address or "").strip()
    if not address:
        return JSONResponse({"error": "缺少 address 参数"}, status_code=400)
    try:
        result = geocode(address, city)
    except Exception as exc:
        return JSONResponse({"error": "地理编码失败：%s" % exc}, status_code=502)
    if not result:
        return JSONResponse({"error": "地址解析失败，请检查地址或百度地图密钥配置"}, status_code=502)
    return JSONResponse(result)


@router.get("/api/reverse")
def api_reverse(lng: Optional[str] = Query(default=None), lat: Optional[str] = Query(default=None)):
    try:
        lng = float(lng)
        lat = float(lat)
    except (TypeError, ValueError):
        return JSONResponse({"error": "缺少合法的 lng/lat 参数"}, status_code=400)
    try:
        result = reverse_geocode(lng, lat)
    except Exception as exc:
        return JSONResponse({"error": "逆地理编码失败：%s" % exc}, status_code=502)
    if not result:
        return JSONResponse({"error": "逆地理编码失败，请检查坐标或百度地图密钥配置"}, status_code=502)
    return JSONResponse(result)


@router.get("/api/coords/convert")
def api_coords_convert(coords: Optional[str] = Query(default=None),
                       from_key: str = Query(default="wgs84", alias="from")):
    """
    坐标转换（v3）：调用百度 geoconv/v1。
    参数：coords=<lng>,<lat>[;<lng>,<lat>...]（最多 50 个，顺序为经度,纬度）、
          from ∈ {wgs84, gcj02, bd09ll}（默认 wgs84），统一转为 BD-09(to=5)。
    返回 {status, coords: [{lng,lat},...]}；百度 status!=0 或网络失败返回 {error}。
    """
    raw = (coords or "").strip()
    if not raw:
        return JSONResponse({"error": "缺少 coords 参数（lng,lat;lng,lat…）"}, status_code=400)
    from_key = (from_key or "wgs84").strip().lower()
    if from_key not in config.GEO_CONV_FROM:
        return JSONResponse({"error": "from 仅支持 wgs84/gcj02/bd09ll"}, status_code=400)
    try:
        pairs = []
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            parts = chunk.split(",")
            if len(parts) != 2:
                return JSONResponse({"error": "coords 每项需为 lng,lat"}, status_code=400)
            pairs.append([float(parts[0]), float(parts[1])])
    except (TypeError, ValueError):
        return JSONResponse({"error": "coords 坐标值非法"}, status_code=400)
    if not pairs or len(pairs) > 50:
        return JSONResponse({"error": "coords 需为 1-50 个 lng,lat 点"}, status_code=400)
    if from_key == "bd09ll":
        # BD-09 转 BD-09 无意义，直接回显
        return JSONResponse({"status": 0, "from": from_key,
                             "coords": [{"lng": p[0], "lat": p[1]} for p in pairs]})
    if not config.BAIDU_AK:
        return JSONResponse({"error": "请在环境变量 BAIDU_MAP_AK 配置百度地图密钥后重试。"}, status_code=502)
    try:
        data = baidu_get("/geoconv/v1/", {
            "coords": ";".join("%s,%s" % (p[0], p[1]) for p in pairs),
            "from": config.GEO_CONV_FROM[from_key],
            "to": 5,
        })
    except Exception as exc:
        return JSONResponse({"error": "坐标转换失败：%s" % exc}, status_code=502)
    if not data or data.get("status") != 0:
        return JSONResponse({"error": "坐标转换失败：%s" % ((data or {}).get("message") or "status!=0")}, status_code=502)
    results = data.get("result") or []
    return JSONResponse({
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


@router.post("/api/isochrone")
def api_isochrone(body: IsochroneRequest = Body(...)):
    """
    创建异步等时圈计算任务，立即返回 {job_id}。
    参数：{lng, lat, minutes(数字或数组), rays=36, steps=8,
           walk_speed=1.2, crossing_sec=30, overpass_sec=120, obstacles=[...]}
    后端内存任务表：daemon 线程执行，任务完成后结果缓存
    （key 含 lng/lat/minutes/rays/steps/walk_speed/crossing_sec/overpass_sec/obstacles）。
    """
    data = body.model_dump()
    try:
        lng = float(data["lng"])
        lat = float(data["lat"])
    except (TypeError, ValueError, KeyError):
        return JSONResponse({"error": "缺少合法的 lng/lat 参数"}, status_code=400)
    # minutes 兼容数字或数组
    minutes_raw = data.get("minutes", config.DEFAULT_MINUTES)
    if isinstance(minutes_raw, (list, tuple)):
        try:
            minutes_list = [int(m) for m in minutes_raw]
        except (TypeError, ValueError):
            return JSONResponse({"error": "minutes 数组需为数字列表"}, status_code=400)
    else:
        try:
            minutes_list = [int(minutes_raw)]
        except (TypeError, ValueError):
            return JSONResponse({"error": "minutes 需为数字或数字数组"}, status_code=400)
    if not minutes_list or not all(0 < m <= 60 for m in minutes_list):
        return JSONResponse({"error": "minutes 需在 1-60 之间"}, status_code=400)
    minutes_list = sorted(set(minutes_list))   # 去重排序，保证 rings 顺序
    rays = int(data.get("rays", config.DEFAULT_RAYS) or config.DEFAULT_RAYS)
    steps = int(data.get("steps", config.DEFAULT_STEPS) or config.DEFAULT_STEPS)
    if rays not in (24, 36, 48):
        return JSONResponse({"error": "rays 仅支持 24/36/48"}, status_code=400)
    if not (1 <= steps <= 16):
        return JSONResponse({"error": "steps 需在 1-16 之间"}, status_code=400)
    try:
        walk_speed = float(data.get("walk_speed", config.DEFAULT_WALK_SPEED))
        crossing_sec = int(data.get("crossing_sec", config.DEFAULT_CROSSING_SEC))
        overpass_sec = int(data.get("overpass_sec", config.DEFAULT_OVERPASS_SEC))
    except (TypeError, ValueError):
        return JSONResponse({"error": "walk_speed/crossing_sec/overpass_sec 参数非法"}, status_code=400)
    if not (0.5 <= walk_speed <= 3.0):
        return JSONResponse({"error": "walk_speed 需在 0.5-3.0 m/s 之间"}, status_code=400)
    if crossing_sec < 0 or overpass_sec < 0:
        return JSONResponse({"error": "crossing_sec/overpass_sec 不能为负"}, status_code=400)
    obstacles = _norm_obstacles(data.get("obstacles"))
    if obstacles is None:
        return JSONResponse({"error": "obstacles 参数格式非法（应为若干 {lng,lat} 多边形）"}, status_code=400)
    # heat_exact=v3 热力图采样模式：true 调步行 API 精确计算，false 走射线插值（不额外调接口）
    heat_exact_raw = data.get("heat_exact", True)
    if not isinstance(heat_exact_raw, bool):
        return JSONResponse({"error": "heat_exact 需为布尔值"}, status_code=400)
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
                return JSONResponse({"job_id": cached_job_id, "cached": job["status"] == "done"})

        job = create_job({
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
            "cache_key": cache_key,
        })
        job_id = job["id"]
        _jobs[job_id] = job
        run_job_async(job_id)
        return JSONResponse({"job_id": job_id})


@router.get("/api/isochrone/{job_id}")
def api_isochrone_status(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return JSONResponse({"status": "error", "message": "任务不存在或已过期"}, status_code=404)
    if job["status"] == "running":
        return JSONResponse({"status": "running", "progress": job["progress"], "stage": job["stage"]})
    if job["status"] == "done":
        return JSONResponse({"status": "done", "result": job["result"]})
    return JSONResponse({"status": "error", "message": job.get("message") or "任务失败"})


@router.get("/api/report/{job_id}")
def api_report(job_id: str):
    """完整报告别名接口，等价于 isochrone done 的 result"""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return JSONResponse({"status": "error", "message": "任务不存在或已过期"}, status_code=404)
    if job["status"] == "done" and job["result"]:
        return JSONResponse(job["result"])
    if job["status"] == "error":
        return JSONResponse({"status": "error", "message": job.get("message") or "任务失败"}, status_code=502)
    return JSONResponse({"status": "running", "progress": job["progress"], "stage": job["stage"]})