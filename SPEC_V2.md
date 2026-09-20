# 15分钟便民生活圈体检报告 v2 — 升级规格书（在 v1 基础上增量修改）

本文件是 v1 SPEC.md 的增量扩展。**v1 已有功能必须全部保留**（8 类 POI、等时圈、灰色区域、健康指数、异步任务、AK 未配置降级、打印导出），v2 在此基础上增加「路网阻抗量化 + 过街模型 + 施工障碍模拟 + 盲区自动诊疗」。

## v2 目标（对应四类痛点）

1. 道路并非直线 → **多时圈 + 路网阻抗系数**（量化）
2. 红绿灯、过街天桥/地道 → **过街阻抗模型**（启发式估算，参数可调，诚实标注）
3. 施工围挡 → **手绘障碍区模拟**（假设分析）
4. 盲区精准发现 → **灰色区域自动诊疗**（受影响人口估算 + 缺失设施 + 建议 + 优先级）

## 1. 多时圈 + 直线参照圆（阻抗系数）

- POST /api/isochrone 的 `minutes` 参数**兼容**：可传数字（如 15）或数组（如 [5,10,15]）。传数组时计算多条等时圈；传数字时单圈，`rings` 数组长度为 1。前端默认提供「15 分钟单圈」「5/10/15 分钟多圈」选项。
- 每条圈附带**直线参照圆**：半径 `r = walk_speed(m/s) * minutes * 60`（默认步行速度 1.2 m/s），面积 `πr²`。v2 新增请求参数 `walk_speed`（默认 1.2）。
- **阻抗系数** `impedance_coef = 路网等时圈面积 / 直线圆面积`，范围 0~1，越小说明路网绕行/阻断越严重。检查并保证 `impedance_coef <= 1.0`（若因 API 数据异常出现 >1 则钳制为 1.0）。
- 返回结构改为 `result.rings[]`，每个元素含：`minutes, boundary, area_km2, perimeter_km, straight_radius_km, straight_area_km2, impedance_coef, crossing`（crossing 见下）。保留旧字段兼容：`result.area_km2 / perimeter_km` 取主圈（minutes 最大的一圈）。

## 2. 过街阻抗模型（红绿灯/天桥/地道）

- `directionlite/v1/walking` 每个路线的响应含 `result.routes[0]` 的 `distance / duration`，以及**路段明细 `steps[]`**（每个 step 含 `instruction` 指令文字、`distance`、`duration`）。**实现时先打印一次真实响应结构确认字段名；若线上字段与预期不符，按实际字段解析，不要硬编码**。
- 在二分法的每次 walking 调用中，解析当前候选路线的 `steps` 指令文字，统计：
  - **过街次数**：指令包含「过马路|过街|横穿|穿过|路口」→ 每次默认 `crossing_sec=30` 秒（请求参数可调）。
  - **天桥/地道次数**：指令包含「天桥|地下通道|人行天桥|地道」→ 每次默认 `overpass_sec=120` 秒（请求参数可调；上下行体力+时间成本）。
- **有效耗时** `effective_duration = route.duration + crossings*crossing_sec + overpasses*overpass_sec`。二分比较用 `effective_duration` 与 `minutes*60` 判断。
- 若 `steps` 为空/解析不到任何路段（接口未返回明细），**过街成本按 0 处理**，并在结果 `meta.crossing_mode` 标注 `"unavailable"`，报告里文案显示「当前接口未返回路段明细，过街估算不可用」；能解析则标注 `"heuristic"` 并在报告注明「过街等待为估算模型（可调参数）」。**绝不在无数据时伪造过街数字**。
- 每圈结果 `crossing = { crossing_count, crossing_wait_sec, overpass_count, overpass_wait_sec }`，统计口径：**该圈 36 条射线最终接受的那个候选点的路线**之和（约 36 次调用，代表性样本，不做全量统计，控制开销）。
- 对「每类最近设施的步行时间」同样应用过街模型（原有 8 次额外步行调用不变，只是把 effective_duration 换算成分钟展示）。

## 3. 施工围挡/道路封闭：手绘障碍区模拟

- POST /api/isochrone 新增请求参数 `obstacles: [ [ {lng,lat}, ... ], ... ]`（若干多边形，bd09ll）。
- 后端：对每条射线的候选点，判断**中心点到候选点的直线段**是否与任一障碍多边形相交；相交则记 `effective_duration = INF`（二分自然内缩，等时圈在障碍背后凹陷）。实现线段与多边形相交判定（多边形各边线段求交）。
- 前端：新增「添加障碍区」按钮。点击进入绘制模式（用 BMapGL 的鼠标工具/多边形工具，简单可靠方式即可；若 BMapGL 无现成绘制组件，用 `map.addEventListener('click')` 收集顶点 + 双击闭合的实现），绘制出的多边形以灰色半透明显示，可在列表中删除、可清空。障碍随请求发送。
- 报告灰区检测不变，但结果 `meta.obstacles` 回传，前端可据此重绘。
- 诚实标注：这是「假设分析」工具（模拟施工围挡/道路封闭的影响），不是实时施工数据。

## 4. 灰色区域自动诊疗升级

- **收集住宅 POI**：新增一个辅助检索类别（不计入 8 类达标统计），关键词 `小区|住宅区|公寓`，随 POI 收集流程一起拉取，标注 `category: "住宅"`。
- **受影响人口估计**：每个灰色聚类质心 500 米内住宅 POI 数量 → `affected_level`：`high`(≥10 个住宅 POI)、`mid`(3~9)、`low`(<3)；报告显示「影响人数规模 高/中/低」。
- **严重度**：按该灰色区域 500 米内缺失的「必需设施」类别数（必需设施 = 药店/医院、超市/便利店、学校、公交/地铁站）：缺失 1 类=「匮乏」、2 类=「严重」、≥3 类=「极重」。
- **自动诊疗建议规则**（缺失项 → 建议文案）：
  - 药店/医院 → `建议增设社区药店或便民诊室`
  - 超市/便利店 → `建议引入便民超市或无人便利店`
  - 学校 → `建议增设托幼点或社区学堂`
  - 公交/地铁站 → `建议增设微循环公交站点或共享单车驿站`
- **优先级**：`极高`（极重 或 (严重 且影响高)）、`高`（严重 或 (匮乏 且影响高)）、`中`（匮乏 且影响中）、`低`（其余）。
- 体检报告新增「问题清单」区块：每个灰色区域一行 = 地址 | 面积 | 影响规模 | 严重度 | 缺失设施 | 建议 | 优先级（badge 颜色）。
- 结论建议区按优先级排序输出。

## 5. 实时路况图层（参考层）

- 前端地图新增「路况」开关：显示机动车实时路况图层（BMapGL GL 版：优先尝试官方「交通路况」图块方式，例如 `new BMapGL.TileLayer({ getTilesUrl: t => "https://maponline0.bdimg.com/tile/?qt=traffic&x="+t.x+"&y="+t.y+"&z="+t.z+"&v=010&t=2" })` 这类官方图块 URL；若当前环境下该 URL 不生效或 BMapGL 版本不支持，则降级为隐藏开关并在控制台提示，**不要伪造或贴错误 URL**）。
- 该图层只作「过街拥挤/绕行」的视觉参考，**不参与步行耗时计算**（步行接口无实时路况口径），README 里写明。

## 6. 前端 UI 变更（v2）

- 顶部新增：
  - 「时间档」下拉：`15 分钟单圈` / `5·10·15 分钟多圈`（默认单圈，多圈时地图画 3 层半透明圈，颜色随分钟数由浅到深，图例说明）。
  - 「路况」复选框。
  - 「添加障碍区」按钮 + 障碍列表（可逐条删除/清空）。
  - 「阻抗参数」可折叠面板：过街等待秒数（默认 30）、天桥/地道秒数（默认 120）、步行速度 m/s（默认 1.2）。
- KPI 卡片新增：主圈**阻抗系数**（如 `0.87`，附简短说明「路网面积/直线圆面积」）、**过街等待估算**（如 `约 4.5 分钟`）。
- 灰色区域区块升级为问题清单（第 4 节格式），每条配优先级 badge。
- 报告打印样式包含新增指标。

## 7. 后端 API 变更汇总

- POST /api/isochrone：新增参数 `minutes`(数字或数组)、`walk_speed`、`crossing_sec`、`overpass_sec`、`obstacles`。任务缓存 key 需包含这些参数。
- 返回：`result.rings[]`（旧 `area_km2/perimeter_km` 保留=主圈）、`result.gray.regions[]` 新增 `affected_level / affected_res_count / severity / suggestions / priority`、`result.meta` 新增 `walk_speed/crossing_sec/overpass_sec/obstacles/crossing_mode`。
- /api/config 新增返回 `v2_defaults: {minutes_options, walk_speed, crossing_sec, overpass_sec}` 与 `required_facility_labels`（前端画问题清单用）。
- 离线冒烟测试（mock baidu_get）：至少覆盖 —— ① 多圈 rings 长度与排序；② 含「过马路/天桥」指令的 mock route 能正确统计 crossing；③ 障碍多边形挡住一条射线（边界明显内缩）；④ 灰色区域问题清单含建议与优先级；⑤ 无 AK 仍走 error 提示。

## 8. 质量与验收

- v1 功能回归不坏：单圈仍是单圈；旧的 curl 验证路径（health/config/geocode 400/isochrone 无 AK error/静态资源 200）全部保持。
- 新代码同样不硬编码 AK；不把估算当事实（报告文案要带「估算」字样）。
- README.md 更新：新增 v2 特性说明、阻抗模型假设与参数、障碍模拟用法、路况图层说明。
- 修改文件：app.py、static/index.html、static/style.css、static/app.js、README.md。