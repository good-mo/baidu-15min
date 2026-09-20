# 15分钟便民生活圈体检报告 — 项目规格书

## 1. 项目目标

基于百度地图开放能力（地理编码 / 逆地理编码 / POI 检索 / 步行路线规划），开发一个 Web 应用：
用户输入任意中心点（一个社区/街道），系统基于真实路网计算「15 分钟步行等时圈」，
统计圈内各类民生设施覆盖情况，输出可视化「体检报告」，并自动标注设施匮乏的「灰色区域」。

## 2. 技术栈

- 后端：Python 3 + Flask + requests（依赖写入 requirements.txt：flask、requests）
- 前端：单页应用，原生 HTML/CSS/JS（不引入 Vue/React），图表用原生 Canvas 手绘条形图，不引入第三方图表库
- 地图：百度地图 GL 版 JS API（BMapGL），script 标签：
  `<script src="https://api.map.baidu.com/api?v=1.0&type=webgl&ak=AK"></script>`（AK 由后端 /api/config 下发）
- 所有百度 Web 服务 API 调用都放在后端（保护、限流、缓存）

## 3. 目录结构（全部在项目根目录下）

```
baidu-15min/
├── app.py            # Flask 后端（所有逻辑）
├── requirements.txt
├── static/
│   ├── index.html    # 页面骨架
│   ├── style.css
│   └── app.js        # 地图渲染 + 报告展示 + 交互
└── README.md         # 使用说明：获取 AK、环境变量、配额提醒、算法说明、安全建议
```

## 4. 百度地图 Web 服务 API（后端调用，AK 从环境变量 BAIDU_MAP_AK 读取）

所有请求 base = `https://api.map.baidu.com`，统一捕获 status!=0 的错误并返回友好提示。

1. **地理编码**（地址→坐标）：
   `GET /geocoding/v3/?address=<addr>&city=<city>&output=json&ak=<AK>`
   取 `result.location.lng / lat`（百度坐标 bd09ll）。
2. **逆地理编码**（坐标→地址）：用于展示中心点地址、灰色区域位置描述。
   `GET /reverse_geocoding/v3/?location=<lat>,<lng>&output=json&coordtype=bd09ll&ak=<AK>`
   取 `result.formatted_address` 与 `result.addressComponent.city/district`。
3. **POI 检索**（按关键词+半径）：
   `GET /place/v2/search?query=<关键词>&location=<lat>,<lng>&radius=<米>&output=json&scope=2&page_size=20&page_num=<n>&ak=<AK>`
   radius 用等时圈边界外扩的搜索半径（建议中心点 2500 米，保证圈内都能搜到）。
   取 `results[]`：`name / location.lat / location.lng / uid / address / province / city / area / detail_info.distance / detail_info.tag`。
   每个关键词最多拉 3 页（60 条），按 uid 去重（无 uid 按 name+坐标 去重）。
4. **步行路线规划**（算路网耗时）：
   `GET /directionlite/v1/walking?origin=<lat>,<lng>&destination=<lat>,<lng>&ak=<AK>`
   取 `result.routes[0].duration`（秒）与 `distance`（米）。`status!=0` 视为不可达（返回极大耗时，如 24h）。

## 5. 后端 API 设计

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/health | `{status:"ok", ak_configured: bool}` |
| GET | /api/config | 下发 `ak`、默认中心点、样例街道列表（前端用） |
| GET | /api/geocode | 参数 address, city；返回 `{lng,lat,address,level}` |
| GET | /api/reverse | 参数 lng,lat；返回地址详情 |
| POST | /api/isochrone | 参数 `{lng, lat, minutes=15, rays=36, steps=8}`；**异步任务**，立即返回 `{job_id}` |
| GET | /api/isochrone/<job_id> | 轮询进度 `{status:"running", progress, stage}` / `{status:"done", result}` / `{status:"error", message}` |
| GET | /api/report/<job_id> | 同一结果，别名返回完整报告（等价于 isochrone done 的 result） |
| GET | / | 返回 static/index.html |

- 后端内置**内存任务表**（dict + daemon 线程），任务完成后保留结果缓存（key 含 lng/lat/minutes/rays，重复请求直接返回缓存）。
- 每次调用百度 API 前 sleep ~0.15s 限流（单线程顺序调用即可）；步行算路结果做内存缓存（key=origin/destination 取整 5 位小数 + 参数），减少重复请求。
- 任务失败要有明确错误 message 返回前端提示条。

## 6. 15分钟步行等时圈算法（核心）

思路：沿中心点向外 360° 均匀发射 N（默认 36）条射线，每条射线上用**二分法**找「步行耗时=15 分钟」的边界点；所有边界点连成多边形即为等时圈。真实路网由步行路线 API 的 duration 体现（不走直线）。

```
compute_isochrone(center, minutes=15, rays=36, steps=8):
  最大搜索距离 max_dist = 2500 米（够 15 分钟步行）
  lat1m = 1/111320（度/米）
  lng1m = 1/(111320*cos(radians(lat)))（度/米）
  for i in range(rays):
      angle = 2*pi*i/rays
      dx, dy = cos(angle), sin(angle)   # 指东、指北
      lo, hi = 0, max_dist
      for _ in range(steps):            # 8 次二分 ≈ 10 米精度
          mid = (lo+hi)/2
          p = center + (dx*mid*lng1m, dy*mid*lat1m)
          t = walking_duration(center, p)   # 秒，走 API（带缓存）
          if t <= minutes*60: lo = mid
          else: hi = mid
      boundary.append(center + (dx*lo*lng1m, dy*lo*lat1m))
  返回边界点列表（按射线角度顺序）
```

- 步行 API 失败（status!=0）视为 `t=INF`，二分必然收敛在中心附近，边界点自然内缩，多边形仍闭合。
- 必须处理退化情况：所有边界点几乎重合（面积 < 0.001 km²）时，报告给出「该点周边路网无法形成有效等时圈」提示。
- 对边界点做**轻量平滑**：把每个点替换为与相邻两点取平均（一遍即可），避免锯齿。
- 结果中附带面积 km²（shoelace 公式，用 lng1m/lat1m 换算为平面坐标后再算）和周长 km。

## 7. 民生设施分类与检索词（POI）

8 大类，每类多个检索关键词（后端顺序请求并合并去重）：

| 类别 | 关键词 | 达标阈值 |
|---|---|---|
| 医疗 | 医院、药店、诊所 | ≥1 |
| 商业 | 超市、便利店、菜市场 | ≥3 |
| 交通 | 公交站、地铁站 | ≥2 |
| 教育 | 小学、中学、幼儿园、大学 | ≥1 |
| 餐饮 | 美食、餐厅 | ≥5 |
| 金融 | 银行、ATM | ≥1 |
| 生活服务 | 快递、理发店、洗衣店 | ≥2 |
| 文体 | 公园、体育馆、图书馆、健身房 | ≥1 |

- 检索半径：中心点外扩 2500 米（覆盖等时圈）。全部 POI 收集后用**点在多边形内**（ray casting）过滤，只统计等时圈内的设施。
- 每个 POI 记录：类别、名称、坐标、距中心点直线距离。

## 8. 体检报告指标

1. **基础数据**：中心点地址、等时圈面积 km²、周长 km、圈内 POI 总数。
2. **分类覆盖**：每类设施数量 + 每类距中心最近设施的直线距离；另外对每类最近设施调用一次步行 API 得到“真实步行分钟数”（8 次额外调用，标在报告里）。
3. **健康指数（0-100）**：8 类设施按上表阈值判定“达标”，权重 = [医疗0.20, 商业0.15, 交通0.15, 教育0.15, 餐饮0.10, 金融0.10, 生活服务0.10, 文体0.05]，指数 = 100 × Σ(达标类别权重)。未达标的类别自动标记为“⚠ 匮乏项”。
4. **灰色区域检测**：
   - 以等时圈 bbox 为范围，网格步长 100 米，取多边形内网格点。
   - 对每个网格点，统计 5 类“日常必需设施”最近距离（直线）：便利店/超市、药店、医院、学校、公交/地铁站。
   - 若某网格点存在 ≥2 类必需设施最近距离 > 500 米，标记为灰色点。
   - 对灰色点做 4-邻域连通聚类（BFS），过滤 <3 个点的碎片簇；每簇输出：簇内网格点数、近似面积（点数×100×100 米²）、质心坐标、质心逆地理编码地址、该簇 500 米内缺失的必需设施清单。
   - 输出：灰色区域数量、总灰色面积、灰色面积占等时圈面积比例。
5. **结论建议**：规则生成 2-4 条中文建议（如“XX 区域缺少药店/医院，建议增设社区医疗点”），以及一句话总评（健康指数 ≥70 “宜居便利”，50-70 “基本可用”，<50 “生活设施匮乏，建议重点关注”）。

## 9. 前端 UI（static/）

页面分左右两栏，左侧地图（占 60%），右侧报告面板（占 40%，可滚动）：

- **顶部工具条**：
  - 下拉样例街道（预设 3 个：深圳市南山区粤海街道（科技园）、成都市成华区猛追湾街道、北京市海淀区中关村街道），选择后自动填坐标并计算
  - 输入框：地址（调 /api/geocode）或直接输入经纬度
  - “计算 15 分钟等时圈”按钮
  - 精度选择：射线数 24/36/48
- **地图层**：
  - 中心点 Marker（红色图钉）+ 标签
  - 等时圈 Polygon（半透明蓝色填充，蓝色描边）
  - POI Marker：按 8 类用不同颜色圆点标注（医疗红、商业橙、交通紫、教育蓝、餐饮黄、金融绿、生活服务青、文体粉），图例显示在地图右上角
  - 灰色区域：每组灰色网格合并成矩形 Polygon，灰色描边 + 灰色半透明填充（透明度 ~0.35），点击弹窗显示该区域缺失设施清单
  - 计算过程中显示进度条（轮询 /api/isochrone/<job_id>，展示“正在算路 12/36”）
- **报告面板**：
  - KPI 卡片行：面积 / POI 总数 / 达标类别 x/8 / 健康指数（环形或大字）
  - 类别达标表：每类 数量、最近距离、真实步行分钟、达标状态（✓/⚠）
  - Canvas 条形图：8 类数量柱状图（手绘，带坐标轴和数值）
  - 灰色区域列表：每条含地址、面积、缺失项
  - 结论建议区
  - “打印/导出报告”按钮：window.print()，用 @media print 样式让报告打印为 A4（地图区域打印时隐藏或固定截图可接受，不强求截图）
- **错误提示条**：AK 未配置时页面顶部红色横幅“请在环境变量 BAIDU_MAP_AK 配置百度地图密钥”，所有按钮禁用；调用失败时黄色提示条展示后端 message。
- 地图需要 `map.enableScrollWheelZoom(true)`；POI 过多时（>1500）只渲染全部但样式可简化，注意性能。

## 10. 默认值/样例

- 默认中心：深圳南山科技园（粤海街道） `lng=113.9458, lat=22.5398`，minutes=15，rays=36，steps=8。
- 三个样例街道具体坐标：
  - 粤海街道（科技园）：113.9458, 22.5398
  - 成都猛追湾街道：104.1012, 30.6612
  - 北京中关村街道：116.3155, 39.9843

## 11. 运行与配置

- `BAIDU_MAP_AK` 环境变量必填（缺省则 /api/health 报 ak_configured=false，页面横幅提示）。
- 启动：`python app.py --port <PORT>`（默认 5000；监听 0.0.0.0）。requirements.txt 允许 `pip install -r requirements.txt`。
- README.md 要写清楚：如何去 lbsyun.baidu.com 注册应用拿 AK（服务端 API 勾选 地理编码/逆地理编码/地点检索/路线规划）；配额参考（directionlite 步行配额有限，36 射线×8 次二分 ≈ 288 次算路请求/每次计算）；AK 安全建议（服务端调用建议勾选 IP 白名单、浏览器端 JS 建议勾选 referer 白名单）；坐标系都是百度坐标 bd09ll。
- 不要在代码里硬编码任何真实 AK；不打印 AK 到日志。

## 12. 质量要求

- 代码可读、有注释（中文注释解释关键算法步骤）。
- 后端逻辑要容错：百度 API 超时 5 秒、重试 1 次；单类 POI 检索失败不影响其它类。
- 前端无 console 报错；AK 未配置和计算失败两种异常路径必须走通。
- 启动后 `curl /api/health` 返回 200。