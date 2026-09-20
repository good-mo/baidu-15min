"""异步任务管理：内存任务表、结果缓存、daemon 线程执行体。"""
import threading
import uuid

from .. import config
from .report import build_report

# 异步任务表 + 锁 + 结果缓存
_jobs = {}
_jobs_lock = threading.Lock()
_results_cache = {}   # key -> job_id（已完成的可直接复用）


def create_job(payload):
    """构建任务字典并登记到任务表（不启动线程，由调用方决定何时触发）"""
    return {
        "id": uuid.uuid4().hex[:12],
        "lng": payload["lng"],
        "lat": payload["lat"],
        "minutes_raw": payload["minutes_raw"],
        "minutes_list": payload["minutes_list"],
        "rays": payload["rays"],
        "steps": payload["steps"],
        "walk_speed": payload["walk_speed"],
        "crossing_sec": payload["crossing_sec"],
        "overpass_sec": payload["overpass_sec"],
        "obstacles": payload["obstacles"],
        "heat_exact": payload["heat_exact"],
        "status": "queued",
        "progress": 0,
        "stage": "排队中…",
        "message": "",
        "result": None,
        "cache_key": payload["cache_key"],
    }


def run_job_async(job_id):
    """启动 daemon 线程执行任务体（_run_job）并立即返回"""
    thread = threading.Thread(target=_run_job, args=(job_id,), daemon=True)
    thread.start()


def _run_job(job_id):
    """daemon 线程执行任务体"""
    with _jobs_lock:
        job = _jobs.get(job_id)
    if job is None:
        return
    if not config.BAIDU_AK:
        with _jobs_lock:
            job["status"] = "error"
            job["message"] = "请在环境变量 BAIDU_MAP_AK 配置百度地图密钥后重试。"
        return
    try:
        build_report(job)
        with _jobs_lock:
            if job["status"] == "done":
                if len(_results_cache) > config._RESULTS_CACHE_MAX:
                    drop = list(_results_cache.keys())[: config._RESULTS_CACHE_MAX // 2]
                    for k in drop:
                        _results_cache.pop(k, None)
                _results_cache[job["cache_key"]] = job_id
    except Exception as exc:
        with _jobs_lock:
            job["status"] = "error"
            job["message"] = "计算失败：%s" % exc