"""百度地图 API 客户端：限流、容错重试、步行算路（逐点/批量矩阵）、POI 检索、地理编码。"""
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

from .. import config

# 并发执行器：等时圈二分 / POI 采集 / 热力采样共用
_executor = ThreadPoolExecutor(max_workers=config.WORKER_NUM, thread_name_prefix="bdapi")

# 全局令牌桶限流器（模块级实例化；测试可通过替换本模块变量调整）
_rate_limit = None


# 步行算路内存缓存（逐点 directionlite 结果 / 批量 routermatrix 结果）
_walk_cache = {}
_walk_cache_lock = threading.Lock()
_matrix_cache = {}
_matrix_cache_lock = threading.Lock()


class RateLimiter:
    """全局令牌桶限流器：btime 秒补充 1 个令牌；并发安全，撑满 QPS 上限。"""

    def __init__(self, qps):
        self._qps = max(qps, 0.5)
        self._period = 1.0 / self._qps
        self._lock = threading.Lock()
        self._next_avail = 0.0

    def take(self):
        """阻塞直到获得一枚令牌（返回实际等待的秒数）"""
        with self._lock:
            now = time.monotonic()
            wait = self._next_avail - now
            if wait < 0:
                wait = 0.0
                self._next_avail = now + self._period
            else:
                self._next_avail += self._period
        if wait > 0:
            time.sleep(wait)
        return wait


_rate_limit = RateLimiter(config.API_QPS)


class BaiduStatusError(Exception):
    """百度 API 返回非 0 status（含配额/权限限制）"""

    def __init__(self, status, message=""):
        super().__init__("百度API status=%s: %s" % (status, message))
        self.status = status


class BaiduQuotaError(BaiduStatusError):
    """配额耗尽 / AK 权限受限（重试无意义，应走降级路径）"""


def _trim_cache(cache, max_size, lock):
    """缓存容量治理：超上限时丢弃最早一半（字典插入序即 LRU）"""
    if len(cache) <= max_size:
        return
    with lock:
        keys = list(cache.keys())
        drop_n = len(keys) // 2
        for k in keys[:min(drop_n, len(keys) // 2)]:
            del cache[k]


def baidu_get(path, params, _retry=0):
    """
    调用百度 Web 服务 API（v4 容错升级）：
      - 全局令牌桶限流（并发安全，撑满 QPS）
      - 网络/超时：指数退避重试 API_RETRIES 次（0.4s/1.0s/1.8s + 抖动）
      - status=402（并发超限）自动退避重试（最多 2 次）
      - status=302/401（配额/AK 受限）抛 BaiduQuotaError，由上层降级，不盲重试
    返回解析后的 JSON；重试后仍失败则抛异常。
    """
    _rate_limit.take()
    time.sleep(config.API_SLEEP * random.uniform(0.6, 1.2))   # 保底小抖动，避免并发黏连
    merged = dict(params)
    merged["ak"] = config.BAIDU_AK
    merged.setdefault("output", "json")
    url = config.BAIDU_BASE + path

    def _do():
        resp = requests.get(url, params=merged, timeout=config.API_TIMEOUT)
        # 兼容 requests.Response 与 mock/自定义返回的 dict
        if hasattr(resp, "json"):
            try:
                return resp.json()
            except ValueError:
                raise BaiduStatusError(-1, "响应非 JSON（HTTP %s）" % getattr(resp, "status_code", "?"))
        return resp

    last_exc = None
    for attempt in range(config.API_RETRIES + 1):
        try:
            data = _do()
            if not isinstance(data, dict):
                raise BaiduStatusError(-1, "响应结构异常")
            status = data.get("status", 0)
            msg = str(data.get("message") or "")
            if status == config.BAIDU_STATUS_OK:
                return data
            if status in config.BAIDU_QUOTA_CODES:
                config.log.warning("百度配额/权限受限 status=%s %s（%s），走降级路径", status, msg, path)
                raise BaiduQuotaError(status, msg)
            if status == config.BAIDU_QPS_CODE and attempt < 2:
                backoff = 0.8 * (attempt + 1)
                time.sleep(backoff)
                continue
            raise BaiduStatusError(status, msg)
        except (requests.RequestException, OSError) as exc:
            last_exc = exc
            backoff = 0.4 * (2 ** attempt) + random.uniform(0, 0.2)
            time.sleep(backoff)
            continue
    raise last_exc if last_exc is not None else BaiduStatusError(-1, "重试后仍失败")


def walking_duration_uncached(origin, dest, crossing_sec, overpass_sec):
    """调用 directionlite 单点步行算路（被 walking_duration 缓存包装）"""
    convert = lambda pt: "%s,%s" % (pt[1], pt[0])
    params = {
        "origin": convert(origin),
        "destination": convert(dest),
    }
    duration = config.INF_TRAVEL
    crossings = 0
    overpasses = 0
    steps_available = False
    try:
        data = baidu_get("/directionlite/v1/walking", params)
        if data and data.get("status") == config.BAIDU_STATUS_OK:
            routes = (data.get("result") or {}).get("routes") or []
            if routes:
                route = routes[0]
                duration = route.get("duration", config.INF_TRAVEL)
                steps = route.get("steps") or []
                if steps:
                    steps_available = True
                    for s in steps:
                        ins = str(s.get("instruction", "") or "")
                        if any(kw in ins for kw in config.CROSSING_KEYWORDS):
                            crossings += 1
                        if any(kw in ins for kw in config.OVERPASS_KEYWORDS):
                            overpasses += 1
    except Exception:
        duration = config.INF_TRAVEL
    return duration, crossings, overpasses, steps_available


def walking_duration(origin, dest, crossing_sec, overpass_sec):
    """
    步行路线规划算路网耗时，并解析路段明细做「过街阻抗」统计（线程安全 + 缓存）。

    返回 (effective_seconds, duration, crossings, overpasses, steps_available)
    结果做内存缓存（key 含坐标取整 5 位小数 + 过街参数），缓存容量有上限治理。
    status != 0 或异常视为不可达（24h）；无 steps 明细时过街按 0（诚实标注）。
    """
    key = (
        round(origin[0], 5), round(origin[1], 5),
        round(dest[0], 5), round(dest[1], 5),
        crossing_sec, overpass_sec,
    )
    with _walk_cache_lock:
        hit = _walk_cache.get(key)
    if hit is not None:
        return hit
    duration, crossings, overpasses, steps_available = walking_duration_uncached(
        origin, dest, crossing_sec, overpass_sec)
    effective = duration + crossings * crossing_sec + overpasses * overpass_sec
    result = (effective, duration, crossings, overpasses, steps_available)
    with _walk_cache_lock:
        if len(_walk_cache) > config._WALK_CACHE_MAX:
            keys = list(_walk_cache.keys())[: config._WALK_CACHE_MAX // 2]
            for k in keys:
                del _walk_cache[k]
        _walk_cache[key] = result
    return result


def _matrix_cache_key(origin, dests):
    """返回逐点的缓存键（字符串列表，便于逐点命中）"""
    pts = [((round(origin[0], 5), round(origin[1], 5)),) +
           ((round(d[0], 5), round(d[1], 5)),) for d in dests]
    return [str(p) for p in pts]


def batch_walking_matrix(origin, dests, crossing_sec, overpass_sec, max_dest=config.MATRIX_MAX_DEST):
    """
    v4 批量距离矩阵：调用百度 routermatrix/v2/walking，一次请求算出
    「中心点 origin → 多个终点 dests」的真实路网步行耗时。

      - dests 自动分批（单批 ≤ max_dest，避免超百度限额），并发 + 令牌桶限流；
      - 结果写 _matrix_cache，之后同坐标的 directionlite 也可复用（有效耗时记为 duration）；
      - 接口不可用 / 限流严重 / 配额耗尽时自动回退：返回 None，调用方逐点改走
        directionlite（并发），保证功能不死。

    返回 {dest索引 -> duration(秒)}；失败返回 None。
    """
    if not dests or not config.BAIDU_AK:
        return None

    def one_batch(batch, offset=0):
        key = _matrix_cache_key(origin, batch)
        with _matrix_cache_lock:
            hits = {i: _matrix_cache[k] for i, k in enumerate(key) if k in _matrix_cache}
            miss_idx = [i for i in range(len(batch)) if i not in hits]
        merged_batch = {offset + i: hits[i] for i in range(len(batch)) if i in hits}
        if not miss_idx:
            return merged_batch
        miss_pts = [batch[i] for i in miss_idx]
        origins = "%s,%s" % (origin[1], origin[0])
        destinations = "|".join("%s,%s" % (p[1], p[0]) for p in miss_pts)
        try:
            data = baidu_get("/routematrix/v2/walking", {
                "origins": origins,
                "destinations": destinations,
            })
        except BaiduQuotaError as exc:
            config.log.warning("批量算路配额受限：%s，回退逐点", exc)
            return None
        except Exception as exc:
            config.log.warning("批量算路请求失败：%s，回退逐点", exc)
            return None
        if not data or data.get("status") != config.BAIDU_STATUS_OK:
            config.log.warning("批量算路 status!=0（%s），回退逐点", (data or {}).get("message") or data.get("status", "?"))
            return None
        results = data.get("result") or []
        if len(results) < len(miss_pts):
            config.log.warning("批量算路返回点数不足（%s/%s），回退逐点", len(results), len(miss_pts))
            return None
        with _matrix_cache_lock:
            if len(_matrix_cache) > config._MATRIX_CACHE_MAX:
                drop = list(_matrix_cache.keys())[: config._MATRIX_CACHE_MAX // 2]
                for k in drop:
                    del _matrix_cache[k]
            for i, idx in enumerate(miss_idx):
                dur = (results[i].get("duration") or {}).get("value")
                if dur is None:
                    continue
                if int(results[i].get("status", 0)) != config.BAIDU_STATUS_OK:
                    continue
                _matrix_cache[_matrix_cache_key(origin, [batch[idx]])[0]] = int(dur)
                merged_batch[offset + idx] = int(dur)
        return merged_batch

    batches = []
    for i in range(0, len(dests), max_dest):
        batches.append((i, dests[i:i + max_dest]))   # (全局起点偏移, 批次点)
    merged = {}
    if len(batches) == 1:
        res = one_batch(batches[0][1], batches[0][0])
        merged.update(res or {})
    else:
        futures = [_executor.submit(one_batch, b, off) for off, b in batches]
        for fut in futures:
            try:
                res = fut.result(timeout=120)
            except Exception as exc:
                config.log.warning("批量算路子任务异常：%s", exc)
                res = None
            merged.update(res or {})
    if len(merged) < len(dests):
        config.log.warning("批量算路完成度 %s/%s", len(merged), len(dests))
        return merged if merged else None
    return merged


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


def search_pois(category, center, radius=config.POI_RADIUS, max_pages=3):
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