# 15分钟便民生活圈体检报告 v3 — 面向评分标准的三个必达能力

在 v1 + v2 基础上增量实现。**v1/v2 全部功能必须保留**，v3 补齐评分标准明确要求的三个能力：
① 稳定调用地图 API 做坐标转换/POI/路径规划；② 等时圈热力图；③ 1km 内无菜市场/药店/小学的服务盲区识别。

## 1. 坐标转换（评分点①）

- 后端新增 `GET /api/coords/convert`：
  - 参数：`coords=<lng>,<lat>[;<lng>,<lat>...]`（最多 50 个，顺序是**经度,纬度**）、`from` ∈ {`wgs84`, `gcj02`, `bd09ll`}。
  - 调用百度 `geoconv/v1`：`params = {coords, from: {"wgs84":1,"gcj02":3,"bd09ll":5}[from], to: 5, output: "json", ak}`。
  - 返回 `{status, coords: [{lng, lat}, ...]}`；百度 status!=0 时返回 `{error: 描述}`。
  - 复用 baidu_get 的限流/超时/重试封装。**不硬编码坐标转换结果**；真实转换需要 AK，README 注明 from/to 映射表与坑（coords 为 lng,lat 顺序）。
- 前端：顶部输入区新增「坐标系」下拉：`BD-09(百度) / GCJ-02(高德) / WGS84(GPS)`，默认 BD-09。
  - 用户输入经纬度后，若所选坐标系不是 BD-09，先调 `/api/coords/convert` 转为 BD-09 再进入计算，输入框旁显示「已从 GCJ-02 转换 → 113.95210,22.54129」类提示。
  - 样例街道下拉选择时坐标系固定 BD-09，不触发转换。

## 2. 等时圈热力图（评分点②）

- 后端 `compute_isochrone` 结果新增 `heat`：
  - 在主圈（minutes 最大的一圈）bbox 内按网格步长 100m 取点，只保留落在主圈多边形内的点。
  - 每个点估算**步行耗时分钟数**：
    - `heat_exact=true`（请求参数，默认 true）：调一次 walking API（复用缓存），取 effective_duration（含过街模型），换算成分钟。
    - `heat_exact=false`（节省模式）：用射线插值估算——按该点相对于中心的角度，在相邻两条射线的边界半径之间线性插值出半径，再按 半径/该角度边界半径 × minutes 估算分钟数；不增加 API 调用。
  - 网格点数上限 120：若 bbox 内点数超限，自动放大步长（步长 = 100 × (超限倍数)）重取。
  - 输出 `heat: [{lng, lat, minutes}]`；不可达/失败点 minutes=null（前端显示为深灰）。
- 前端：主圈上叠加热力层：每个热力点画半透明填充圆（半径随 zoom 或固定 12px），颜色映射 `minutes<=5 绿 / <=10 黄 / <=15 橙 / null 深灰`。图例新增「热力：步行耗时（分）」。
- KPI 卡片补充：**圈内平均步行耗时**（heat 中 minutes 非空均值，1 位小数）与 **>10 分钟区域占比**。
- 报告里标注「热力为网格采样（约 100m 步长）」；README 说明精确热力会增加步行配额调用。

## 3. 1公里服务盲区核验（评分点③，采用评分口径）

- 后端新增 `detect_blind_spots(center, boundary, pois, params)` 独立模块，与 v2 灰色区域并存，互不影响：
  - **判定口径（严格按评分标准）**：对候选点，检查其 1 公里（1000m，直线距离）内是否有【菜市场】【药店】【小学】三类设施；**三类设施在该点 1km 内全部没有** → 判为「服务盲区点位」。
  - 候选点来源两类：
    1. **住宅小区 POI**（现有「小区|住宅区|公寓」辅助类，`category=住宅`）：对每个住宅 POI 做 1km 判定。
    2. **网格点**：等时圈内网格（步长 150m），对每个网格点做 1km 判定。
  - 距离计算用 haversine（直线），设施数据直接取已收集 POI（菜市场=商业类「菜市场」关键词、药店=医疗类「药店」关键词、小学=教育类「小学」关键词），**不额外调用接口**。
  - 输出：
    ```
    blind_spots = {
      count,
      affected_housing_count,          # 被判定为盲区的住宅 POI 数
      points: [
        { lng, lat, source: "住宅小区"|"网格点",
          nearest: { "菜市场": 米|None, "药店": 米|None, "小学": 米|None } }
      ]
    }
    ```
- 前端：
  - 地图渲染：盲区点位红色感叹号标记（可用 BMapGL.Marker + 自定义 icon 或 Label；住宅源用较大标记、网格源用小红点），图例新增「服务盲区点位（1km 内无菜市场/药店/小学）」。
  - 报告新增「1公里服务盲区核验」区块：盲区数量、受影响住宅数、点位列表（坐标、缺失设施、最近设施距离）。结论建议区把盲区点位按优先级列入。
- /api/config 新增 `blind_spot_radius: 1000`, `blind_spot_categories: ["菜市场","药店","小学"]`。
- README 明确：这是评分口径（1km/菜市场/药店/小学），与 v2 灰色区域（等时圈内 500m/四类必需设施）是两个并存指标。

## 4. 回归与验收

- v1/v2 全功能不坏（health/config/geocode 400/无 AK error/静态资源/多时圈/过街/障碍/灰区诊疗）。
- 新增参数（heat_exact 等）必须进入任务缓存 key，避免串缓存。
- 无 AK 时 /api/coords/convert 也要走统一「请配置 BAIDU_MAP_AK」error 路径。
- **离线 mock 冒烟测试**（mock baidu_get）至少覆盖：
  1. 坐标转换：mock geoconv 返回，断言 /api/coords/convert 正确透传并映射 from 值；
  2. 热力：heat 点数>0、minutes 在 (0, 40] 且非空点占多数；
  3. 盲区判定：构造「1km 内无任何三类设施」的 mock 点 → 判定为盲区；「1km 内有菜市场」的点 → 不判盲区；「住宅源/网格源」两类候选都存在；
  4. 无 AK 全链路 error 提示。
- 修改文件：app.py、static/index.html、static/style.css、static/app.js、README.md。
- 完成后从 /workspace/baidu-15min 重启服务监听 3000，并报告验证结果与 ls 输出。