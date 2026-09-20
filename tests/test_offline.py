#!/usr/bin/env python3
"""离线 mock 冒烟测试：v4 后端新增能力（不限流真调、不依赖 AK）。"""
import math
import time
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from baidu15min import config
from baidu15min.api.app import app as api_app
from baidu15min.core import baidu_client, blind, geometry, heat, isochrone, jobs, report, spatial


def seed_env(ak="test-ak"):
    config.BAIDU_AK = ak
    baidu_client._walk_cache.clear()
    baidu_client._matrix_cache.clear()


class TestRateLimiter(unittest.TestCase):
    def test_qps_throttle(self):
        rl = baidu_client.RateLimiter(qps=10.0)
        t0 = time.monotonic()
        for _ in range(20):
            rl.take()
        elapsed = time.monotonic() - t0
        # 20 个令牌按 10QPS 需要 ~2s
        self.assertGreaterEqual(elapsed, 1.7)
        self.assertLess(elapsed, 4.0)


class TestBaiduGetRetry(unittest.TestCase):
    def setUp(self):
        seed_env()

    def test_qps_402_backoff_then_success(self):
        calls = []

        def fake_get(url, params, timeout):
            calls.append(params)
            if len(calls) <= 2:
                return {"status": 402, "message": "并发配额超限"}
            return {"status": 0, "result": "ok"}

        with mock.patch("baidu15min.core.baidu_client.requests.get", side_effect=fake_get):
            data = baidu_client.baidu_get("/directionlite/v1/walking", {"origin": "1,2"})
        self.assertEqual(data["status"], 0)
        self.assertEqual(len(calls), 3)

    def test_quota_302_raises_immediately(self):
        with mock.patch("baidu15min.core.baidu_client.requests.get",
                        return_value={"status": 302, "message": "配额用完"}):
            with self.assertRaises(baidu_client.BaiduQuotaError):
                baidu_client.baidu_get("/geocoding/v3/", {"address": "x"})

    def test_network_error_retries(self):
        calls = {"n": 0}

        def flaky(url, params, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise OSError("boom")
            return {"status": 0}

        with mock.patch("baidu15min.core.baidu_client.requests.get", side_effect=flaky):
            data = baidu_client.baidu_get("/x", {})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(data["status"], 0)


class TestBatchMatrix(unittest.TestCase):
    def setUp(self):
        seed_env()

    def test_batch_split_and_cache(self):
        """>40 点自动分批；结果写 _matrix_cache 供复用"""
        origin = (113.9, 22.5)
        dests = [(113.9 + i * 0.0001, 22.5) for i in range(45)]
        seen_batches = []

        def fake_get(url, params, timeout):
            seen_batches.append(params["destinations"].count("|") + 1)
            results = []
            ds = params["destinations"].split("|")
            for d in ds:
                results.append({"status": 0, "distance": {"value": 100}, "duration": {"value": 90}})
            return {"status": 0, "result": results}

        with mock.patch("baidu15min.core.baidu_client.requests.get", side_effect=fake_get):
            out = baidu_client.batch_walking_matrix(origin, dests, 30, 120)
        self.assertEqual(len(out), 45)
        self.assertLessEqual(max(seen_batches), config.MATRIX_MAX_DEST)
        self.assertGreater(len(seen_batches), 1)
        # 缓存命中：再次调用不再发请求
        n0 = len(baidu_client._matrix_cache)
        with mock.patch("baidu15min.core.baidu_client.requests.get",
                        side_effect=AssertionError("不应发请求")):
            out2 = baidu_client.batch_walking_matrix(origin, dests[:5], 30, 120)
        self.assertEqual(len(out2), 5)

    def test_matrix_failure_fallback_none(self):
        origin = (113.9, 22.5)
        dests = [(113.9001, 22.5001)]
        with mock.patch("baidu15min.core.baidu_client.requests.get",
                        return_value={"status": 1, "message": "服务暂不可用"}):
            out = baidu_client.batch_walking_matrix(origin, dests, 30, 120)
        self.assertIsNone(out)


class TestIsochroneBatchBisect(unittest.TestCase):
    def setUp(self):
        seed_env()
        # 模拟"路网直线可得"：耗时 = 直线距离 / 1.2 m/s
        def fake_batch(center, pts, crossing_sec, overpass_sec):
            out = []
            for p in pts:
                d = math.hypot((p[0] - center[0]) / geometry._lng_1m(center[1]),
                               (p[1] - center[1]) / config.LAT_1M)
                out.append(d / 1.2)
            return out

        self.fake_batch = fake_batch

    def test_boundary_radius_approx(self):
        """15 分钟等时圈在"直线路网"下半径应 ≈ 15*60*1.2 = 1080m"""
        center = (113.9458, 22.5398)
        with mock.patch.object(isochrone, "_batch_durations", side_effect=self.fake_batch), \
             mock.patch.object(isochrone, "walking_duration", return_value=(0, 0, 0, 0, False)):
            boundary, stats, steps = isochrone.compute_isochrone(center, 15, 36, 8, 30, 120)
        self.assertEqual(len(boundary), 36)
        radii = [math.hypot((p[0] - center[0]) / geometry._lng_1m(center[1]),
                            (p[1] - center[1]) / config.LAT_1M) for p in boundary]
        avg_r = sum(radii) / len(radii)
        self.assertAlmostEqual(avg_r, 1080.0, delta=60.0)

    def test_batch_duration_call_count(self):
        """批二分下 API 记忆：_batch_durations 调用次数 = steps"""
        center = (113.9458, 22.5398)
        calls = {"n": 0}

        def counting_batch(c, pts, cs, osp):
            calls["n"] += 1
            return self.fake_batch(c, pts, cs, osp)

        with mock.patch.object(isochrone, "_batch_durations", side_effect=counting_batch), \
             mock.patch.object(isochrone, "walking_duration", return_value=(0, 0, 0, 0, False)):
            isochrone.compute_isochrone(center, 15, 36, 8, 30, 120)
        self.assertEqual(calls["n"], 8)


class TestHeatFallback(unittest.TestCase):
    def setUp(self):
        seed_env()
        # 构造一个真实形状的等时圈边界：半径 500m 的 36 边形
        c = (113.9458, 22.5398)
        lng1m = geometry._lng_1m(c[1])
        lat1m = config.LAT_1M
        self.center = c
        self.boundary = [
            (c[0] + 500 * math.cos(2 * math.pi * i / 36) * lng1m,
             c[1] + 500 * math.sin(2 * math.pi * i / 36) * lat1m)
            for i in range(36)
        ]

    def test_exact_with_failures_degrades_to_interp(self):
        """exact 模式下个别点失败 → 自动降级为射线插值，且非空点占多数"""
        def flaky(origin, dest, cs, osp):
            if abs(dest[0] - self.center[0]) < 1e-9 and abs(dest[1] - self.center[1]) < 1e-7:
                return (config.INF_TRAVEL, config.INF_TRAVEL, 0, 0, False)
            d = math.hypot((dest[0] - origin[0]) / geometry._lng_1m(origin[1]),
                           (dest[1] - origin[1]) / config.LAT_1M)
            return (d / 1.2, d / 1.2, 0, 0, True)

        sink = [None]
        with mock.patch.object(heat, "walking_duration", side_effect=flaky):
            heat_list, step = heat.compute_heat(self.center, self.boundary, 15, 36, 30, 120,
                                                exact=True, max_points=120, mode_sink=sink)
        self.assertGreater(len(heat_list), 0)
        vals = [h["minutes"] for h in heat_list if h["minutes"] is not None]
        self.assertGreater(len(vals), len(heat_list) * 0.8)
        for v in vals:
            self.assertTrue(0 < v <= 40, "minutes 超界: %s" % v)
        self.assertIn(sink[0], ("exact", "exact+interp", "interp"))

    def test_interp_mode_no_api(self):
        sink = [None]
        with mock.patch.object(heat, "walking_duration",
                               side_effect=AssertionError("不应调用 API")):
            heat_list, _ = heat.compute_heat(self.center, self.boundary, 15, 36, 30, 120,
                                             exact=False, max_points=120, mode_sink=sink)
        self.assertEqual(sink[0], "interp")
        vals = [h["minutes"] for h in heat_list if h["minutes"] is not None]
        self.assertGreater(len(vals), len(heat_list) * 0.7)


class TestBlindSpots(unittest.TestCase):
    def setUp(self):
        seed_env()
        self.center = (113.9458, 22.5398)
        lng1m = geometry._lng_1m(self.center[1])
        lat1m = config.LAT_1M
        self.boundary = [
            (self.center[0] + 1000 * lng1m, self.center[1]),
            (self.center[0], self.center[1] + 1000 * lat1m),
            (self.center[0] - 1000 * lng1m, self.center[1]),
            (self.center[0], self.center[1] - 1000 * lat1m),
        ]
        self.housing = [{"name": "幸福小区", "category": "住宅",
                         "lng": self.center[0], "lat": self.center[1]}]

    def _pois(self, with_market=False):
        pois = []
        if with_market:
            pois.append({"name": "永辉菜市场", "category": "商业",
                         "lng": self.center[0] + 200 * geometry._lng_1m(self.center[1]),
                         "lat": self.center[1] + 200 * config.LAT_1M})
        else:
            pois.append({"name": "某某写字楼", "category": "商业",
                         "lng": self.center[0] + 200 * geometry._lng_1m(self.center[1]),
                         "lat": self.center[1] + 200 * config.LAT_1M})
        return pois

    def test_housing_is_blind_when_no_facilities(self):
        r = blind.detect_blind_spots(self.center, self.boundary, self._pois(False), self.housing)
        self.assertEqual(r["affected_housing_count"], 1)
        self.assertGreaterEqual(r["count"], 1)

    def test_not_blind_with_market_1km(self):
        r = blind.detect_blind_spots(self.center, self.boundary, self._pois(True), self.housing)
        self.assertEqual(r["affected_housing_count"], 0)


class TestSpatialBucket(unittest.TestCase):
    def test_consistency_with_naive(self):
        import random
        random.seed(42)
        pts = [{"lng": 113.9 + random.uniform(-0.02, 0.02),
                "lat": 22.5 + random.uniform(-0.02, 0.02)} for _ in range(300)]
        idx = spatial._SpatialBucket(pts)

        def naive(lng, lat):
            best = float("inf")
            for p in pts:
                d = spatial._haversine_km(lat, lng, p["lat"], p["lng"]) * 1000.0
                best = min(best, d)
            return best

        for _ in range(60):
            lng = 113.9 + random.uniform(-0.01, 0.01)
            lat = 22.5 + random.uniform(-0.01, 0.01)
            n = naive(lng, lat)
            b = idx.nearest_m(lng, lat, 2000)
            self.assertAlmostEqual(min(n, 2000), b, delta=1.0)


class TestFullReportPipeline(unittest.TestCase):
    """完整报告流水线端到端：mock 全部百度接口，验证产出结构与降级共存"""

    def setUp(self):
        seed_env()
        jobs._jobs.clear()
        jobs._results_cache.clear()

    def _fake_baidu(self, url, params, timeout=None):
        path = url.replace(config.BAIDU_BASE, "")
        if "directionlite" in path or "routematrix" in path:
            # 步行耗时 ≈ 直线距离 / 1.2 m/s
            def conv(s):
                lat, lng = map(float, s.split(","))
                return lng, lat
            if "routematrix" in path:
                o = conv(params["origins"])
                ds = [conv(d) for d in params["destinations"].split("|")]
                results = []
                for d in ds:
                    dist = math.hypot((d[0] - o[0]) / geometry._lng_1m(o[1]),
                                      (d[1] - o[1]) / config.LAT_1M)
                    results.append({"status": 0, "distance": {"value": int(dist)},
                                    "duration": {"value": int(dist / 1.2)}})
                return {"status": 0, "result": results}
            o = conv(params["origin"])
            d = conv(params["destination"])
            dist = math.hypot((d[0] - o[0]) / geometry._lng_1m(o[1]), (d[1] - o[1]) / config.LAT_1M)
            return {"status": 0, "result": {"routes": [{
                "duration": int(dist / 1.2),
                "steps": [{"instruction": "沿道路直行，过路口"}]}]}}
        if "place/v2/search" in path:
            # 各类设施：在中心附近均匀放置 2 个，覆盖 8 类 + 住宅
            query = params["query"]
            cat_map = {
                "医院": "医疗", "超市": "商业", "公交站": "交通", "小学": "教育",
                "美食": "餐饮", "银行": "金融", "快递": "生活服务", "公园": "文体",
                "小区": "住宅",
            }
            name = query
            category = cat_map.get(query, "商业")
            results = []
            for i in range(2):
                results.append({
                    "name": query + str(i + 1), "uid": "uid_%s_%d" % (query, i),
                    "location": {"lng": 113.9400 + i * 0.01, "lat": 22.5300 + i * 0.01},
                    "category": category, "address": "测试路" + str(i),
                    "detail_info": {"distance": 100 + i, "tag": ""},
                })
            return {"status": 0, "results": results}
        if "geocoding" in path:
            return {"status": 0, "result": {"location": {"lng": 113.9458, "lat": 22.5398},
                                             "formatted_address": "科技园", "level": "区域"}}
        if "reverse_geocoding" in path:
            return {"status": 0, "result": {
                "formatted_address": "南山科技园",
                "addressComponent": {"city": "深圳市", "district": "南山区", "province": "广东省"}}}
        raise AssertionError("未覆盖接口: %s" % path)

    def test_full_pipeline(self):
        job = {
            "id": "e2e", "lng": 113.9458, "lat": 22.5398,
            "minutes_raw": [5, 10, 15], "minutes_list": [5, 10, 15],
            "rays": 24, "steps": 6, "walk_speed": 1.2,
            "crossing_sec": 30, "overpass_sec": 120,
            "obstacles": [], "heat_exact": True,
            "status": "queued", "progress": 0, "stage": "", "message": "",
            "result": None, "cache_key": ("e2e",),
        }
        with mock.patch("baidu15min.core.baidu_client.requests.get", side_effect=self._fake_baidu):
            report.build_report(job)
        self.assertEqual(job["status"], "done", job.get("message"))
        r = job["result"]
        # v1/v2 字段
        self.assertEqual(len(r["rings"]), 3)
        self.assertEqual(len(r["categories"]), 8)
        self.assertTrue(0 <= r["health_index"] <= 100)
        self.assertIn("isochrone", r)
        self.assertIn("gray_areas", r)
        # v3 字段
        self.assertTrue(isinstance(r["heat"], list) and len(r["heat"]) > 0)
        self.assertIn("heat_mode", r["heat_stats"])
        self.assertEqual(r["heat_stats"]["heat_mode"], "exact")
        self.assertIn("blind_spots", r)
        self.assertIn("count", r["blind_spots"])
        self.assertIn("affected_housing_count", r["blind_spots"])
        # 结论
        self.assertIsInstance(r["suggestions"], list)
        self.assertTrue(r["verdict"])
        # meta 诚实标注
        self.assertIn("crossing_mode", r["meta"])
        # 盲区网格点与住宅源点并存
        sources = {p["source"] for p in r["blind_spots"]["points"]}
        self.assertTrue(sources <= {"住宅小区", "网格点"}, sources)


class TestCoordConvert(unittest.TestCase):
    def setUp(self):
        seed_env()
        self.client = TestClient(api_app)

    def test_bd09ll_echo(self):
        r = self.client.get("/api/coords/convert?coords=113.9,22.5&from=bd09ll")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["coords"][0]["lng"], 113.9)

    def test_geoconv_mapped(self):
        captured = {}

        def fake_get(url, params, **kw):
            captured.update(params)
            return {"status": 0, "result": [{"x": 113.911, "y": 22.511}]}

        with mock.patch("baidu15min.core.baidu_client.requests.get", side_effect=fake_get):
            r = self.client.get("/api/coords/convert?coords=113.9,22.5&from=gcj02")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(captured["from"], 3)      # gcj02 -> 3
        self.assertEqual(captured["to"], 5)        # -> bd09ll
        self.assertAlmostEqual(r.json()["coords"][0]["lng"], 113.911)

    def test_error_message(self):
        r = self.client.get("/api/coords/convert?coords=bad&from=wgs84")
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)