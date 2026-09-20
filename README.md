# 15分钟便民生活圈体检报告 v2

基于百度地图开放能力（地理编码 / 逆地理编码 / POI 检索 / 步行路线规划）的 Web 应用：
输入任意中心点（一个社区/街道），系统基于真实路网计算 **15 分钟步行等时圈**（支持 5/10/15 分钟多圈），
统计圈内 8 类民生设施覆盖情况，输出可视化「体检报告」，并自动标注设施匮乏的「灰色区域」并给出**自动诊疗建议**。

## v2 新增特性

1. **多时圈 + 路网阻抗系数（量化）**：`minutes` 支持数组（如 `[5,10,15]`）一次计算多条等时圈；
   每条圈附带**直线参照圆**（半径 = 步行速度 × 分钟 × 60，默认 1.2 m/s），
   **阻抗系数 = 路网等时圈面积 / 直线圆面积**（0~1，越小说明路网绕行/阻断越严重，异常时钳制为 1.0）。
2. **过街阻抗模型（启发式估算，参数可调）**：解析步行路线 `steps` 指令文字，
   统计「过马路/过街/横穿/穿过/路口」（默认 30 秒/次）与「天桥/地下通道/人行天桥/地道」（默认 120 秒/次），
   `有效耗时 = duration + 过街成本`，用于二分算圈与「真实步行分钟」。报告如实标注：
   接口未返回路段明细时标 `unavailable`（过街按 0，不伪造数字），能解析时标 `heuristic`。
3. **手绘施工障碍区模拟（假设分析）**：前端地图可绘制障碍多边形（单击加点、双击闭合，可删除/清空），
   后端对每条射线的「中心→候选点」直线段做**线段与多边形相交判定**，相交即视为不可达，等时圈自然凹陷。为模拟施工围挡/道路封闭的「假设分析」工具，非实时施工数据。
4. **灰色区域自动诊疗**：辅助检索「住宅」POI（不计入 8 类达标）；
   每个灰色区域输出 500 米内受影响住宅数（影响规模 高/中/低）、严重度（缺失 1 类=匮乏、2 类=严重、≥3 类=极重）、
   缺失项→建议文案，以及优先级（极高/高/中/低）。结论建议按优先级排序。
5. **实时路况图层（参考层）**：地图「路况」开关显示机动车实时路况图块，仅作**过街拥挤/绕行的视觉参考，不参与步行耗时计算**（步行接口无实时路况口径）；若当前 BMapGL 版本/环境图块 URL 不可用，开关自动隐藏。

## 技术栈

- 后端：Python 3 + Flask + requests（`requirements.txt`：flask、requests）
- 前端：单页应用，原生 HTML/CSS/JS，图表用原生 Canvas 手绘条形图
- 地图：百度地图 GL 版 JS API（BMapGL），AK 由后端 `/api/config` 下发
- 所有百度 Web 服务 API 调用都放在后端（保护 AK、统一限流、内存缓存）

## 如何获取百度地图 AK

1. 注册/登录 [lbsyun.baidu.com](https://lbsyun.baidu.com/)
2. 进入「控制台 → 应用管理 → 我的应用 → 创建应用」
3. 应用类型选择 **浏览器端 + 服务端**（或分别创建两个应用）：
   - 服务端 API 需勾选：**地理编码 / 逆地理编码 / 地点检索（POI）/ 路线规划（directionlite 步行）**
   - 浏览器端 JS API 使用同一个 AK（BMapGL）
4. 创建成功后，把得到的 AK（形如 `xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`）配置为环境变量：

```bash
export BAIDU_MAP_AK=你的AK
```

## 运行

```bash
pip install -r requirements.txt
python app.py --port 5000
```

- 默认监听 `0.0.0.0:5000`；`--port <PORT>` 可指定其它端口。
- 浏览器打开 `http://localhost:<PORT>/`
- 健康检查：`curl http://localhost:<PORT>/api/health`

若未配置 `BAIDU_MAP_AK`：`/api/health` 返回 `ak_configured=false`，
页面顶部显示红色横幅「请在环境变量 BAIDU_MAP_AK 配置百度地图密钥」，所有按钮禁用。

## 配额与性能提醒

- **步行路线 API（directionlite）配额有限**：默认 36 射线 × 8 次二分 ≈ **288 次算路请求/单圈**；
  多圈（5/10/15）为单圈的三倍；射线数 24/48 可降低/提高精度。
- POI 检索：8 类 + 住宅 1 类共 9 类 × 最多 3 页（每页 20 条）。
- 后端对每次百度 API 调用前 `sleep ~0.15s` 做限流（单线程顺序调用），多圈任务约需 2-5 分钟，请耐心等待（前端有进度条）。
- 计算结果做内存缓存（相同中心/时长档/射线/阻抗参数/障碍直接复用），步行算路结果也有内存缓存。

## 算法说明

- **15 分钟步行等时圈**：沿中心点 360° 均匀发射 N（默认 36）条射线，每条射线上用**二分法**
  找「有效步行耗时 = 目标分钟」的边界点（耗时来自百度步行路线规划 API，体现真实路网，不走直线；
  v2 叠加过街成本与障碍判定）；所有边界点连成多边形即为等时圈。8 次二分 ≈ 10 米精度。
- 步行 API 失败视为不可达（24h），该方向边界点自然内缩，多边形仍闭合。
- 边界点做轻量平滑（三点平均）避免锯齿；面积用鞋带公式换算平面坐标计算。
- **民生设施覆盖**：8 类设施按关键词+半径（2500 米）检索 POI，用「点在多边形内」（ray casting）过滤，
  只统计等时圈内的设施；每类按「数量 vs 达标阈值」判定达标，健康指数 = 100 × Σ(达标类别权重)。
- **灰色区域自动诊疗**：等时圈范围内按 100 米网格取点，统计 4 类日常必需设施
  （药店/医院、超市/便利店、学校、公交/地铁站）最近直线距离，存在 ≥2 类 >500 米即标记灰色点；
  灰色点做 4-邻域 BFS 连通聚类，过滤 <3 点的碎片簇；每簇按缺失类别数定严重度，
  按质心 500 米内住宅 POI 数定影响规模，按规则表定优先级。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 `{status, ak_configured}` |
| GET | `/api/config` | 下发 ak、默认中心点、样例街道、分类、`v2_defaults`、`required_facility_labels` |
| GET | `/api/geocode?address=&city=` | 地址转坐标 |
| GET | `/api/reverse?lng=&lat=` | 坐标转地址 |
| POST | `/api/isochrone` | `{lng, lat, minutes(数字或数组), rays=36, steps=8, walk_speed, crossing_sec, overpass_sec, obstacles}`，异步返回 `{job_id}` |
| GET | `/api/isochrone/<job_id>` | 轮询进度 / 完成结果（结果含 `rings[]`、`gray.regions[]`、`meta`） |
| GET | `/api/report/<job_id>` | 完整报告别名接口 |
| GET | `/` | 前端页面 |

返回结构关键点（v2）：`result.rings[]` 每条含
`minutes / boundary / area_km2 / perimeter_km / straight_radius_km / straight_area_km2 / impedance_coef / crossing / degenerate`；
旧字段 `result.area_km2 / perimeter_km / isochrone` 保留（= 主圈，即分钟数最大的一圈）；
`result.gray.regions[]`（兼容 alias `gray_areas`）每条含 `affected_res_count / affected_level / severity / suggestions / priority`；
`result.meta` 含 `walk_speed / crossing_sec / overpass_sec / obstacles / crossing_mode(heuristic|unavailable)`。

## 坐标系与安全建议

- 本项目全部使用**百度坐标系 bd09ll**（百度地图默认），请勿与 GCJ-02/WGS-84 混用。
- **AK 安全建议**：
  - 服务端调用建议在百度控制台配置 **IP 白名单**（仅允许部署服务器 IP 调用）。
  - 浏览器端 JS（地图）建议配置 **referer 白名单**（仅允许你的站点域名加载）。
  - 不要把 AK 提交到公开仓库。本项目 AK 从环境变量 `BAIDU_MAP_AK` 读取，不在代码中硬编码，也不会打印到日志。
- **诚实标注**：过街等待、天桥/地道成本均为**启发式估算模型**（参数可调，非实测数据）；
  障碍区模拟为**假设分析**（非实时施工数据）；路况图层仅作视觉参考，不参与耗时计算。