# 配送路线效率分析平台 · Delivery Route Efficiency Analytics

融合**订单、定位点、道路事件、司机状态**四类数据，端到端完成配送路线效率分析。
纯 Python 标准库后端（`http.server` + `sqlite3` + `zoneinfo`）+ 原生 JS 前端，
**零第三方依赖、可离线运行**。

## 解决的核心问题

| 问题 | 处理方式 |
| --- | --- |
| 定位缺失 | null 坐标标记为 `missing_fix`，连续缺失合并为 `missing_fix_run` 数据缺口 |
| 重复点 | 时间窗 + 空间半径双条件识别精确/空间重复，保留锚点 |
| GPS 跳点 | 相邻有效点间速度超阈值判为 `gps_jump`，且跳点不污染后续点的锚点 |
| 跨天路线 | 一切内部计算用 UTC 绝对时刻；仅用 IANA 时区打服务日标签，跨午夜保持单路线 |
| 停留误判 | 用**观测覆盖率**区分真实停留与日志中断；数据质量证据优先于状态/事件佐证 |
| 迟到/早到 | 以"首次进入地理围栏（前一点在围栏外）"为到达时刻，按订单时区渲染窗口与到达 |
| 异常混淆 | 源头二分：`DATA_ISSUE`（平台数据不可信）vs `REAL_WORLD`（可信数据描述的真实路况） |
| 结果不可回溯 | 每个指标/异常/清洗决策都绑定点 ID + 规则版本 + 规则阈值证据 |
| 规则修订重算过重 | 参数带 scope；修订时只重算受影响路线，旧结果标记 `superseded` 永不删除 |

## 目录结构

```
backend/
  delivery_analytics/
    geo.py          # haversine、速度、方位角、web mercator 投影
    tzutil.py       # UTC 绝对时刻存储、zoneinfo 本地渲染、DST 处理
    rules.py        # 版本化规则目录、参数校验、语义化版本号、scope 映射
    database.py     # SQLite schema（原始层 / 规则层 / 派生层）
    cleaning.py     # 阶段1：点清洗（缺失/重复/跳点/缺口）
    stops.py        # 阶段2：停留 vs 数据缺口分类（观测覆盖率）
    routes.py       # 阶段3：跨天路段组装、订单地理围栏挂载
    metrics.py      # 阶段4：效率指标、迟到判定、异常生成、评分
    pipeline.py     # 编排 + 版本戳 + 选择性重算
    service.py      # 摄入校验、规则修订发布
    repository.py   # 读侧：含完整回溯链的路线详情
    server.py       # stdlib HTTP API + 静态文件
  seed_data.py      # 覆盖全部边界场景的确定性种子数据
  run_server.py     # -I 隔离模式启动器
  run_tests.sh      # 全部测试
  tests/            # 40 个 Python 用例 + 15 项前端渲染检查
frontend/
  index.html  styles.css  app.js   # 无依赖 SPA：SVG 地图/时间线/证据/修订史
```

## 快速开始

```bash
cd backend

# 1. 初始化库并写入种子场景（自动跑一遍基线流水线）
python3 -I seed_data.py --reset

# 2. 启动服务（默认 8080）
python3 -I run_server.py --db ../data/analytics.db --port 8080
# 浏览器打开 http://localhost:8080
```

## 测试

```bash
cd backend && ./run_tests.sh
# 或分开运行
python3 -I -m unittest discover -s tests
node tests/smoke_frontend.js
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/dashboard` | 顶部统计（路线数、均分、两类异常计数、当前规则） |
| GET | `/api/routes[?all=1]` | 路线列表（默认仅 current） |
| GET | `/api/routes/{id}[?rule_version_id=]` | 路线详情：点/订单/停留/异常/指标/版本史 |
| GET | `/api/rules` / `/api/rules/catalogue` | 规则版本 / 参数目录（含 scope 与说明） |
| POST | `/api/rules/revision` | `{params:{...}, change_note}` 发布修订并按 scope 重算 |
| GET | `/api/revisions` | 修订历史（变更参数、scope、受影响/实算路线） |
| GET | `/api/cleaning[?driver_id=&rule_version_id=]` | 点级清洗轨迹与证据 |
| POST | `/api/ingest` | 校验并写入原始四类数据后重跑 |
| POST | `/api/recompute` | 手动全量重算 |

### 修订示例

```bash
curl -X POST http://localhost:8080/api/rules/revision \
  -H 'Content-Type: application/json' \
  -d '{"params":{"stop_radius_m":80},"change_note":"收紧停留半径"}'
# -> version 2.0.0（stop 属语义级 scope，主版本号 +1），仅受影响路线重算
```

## 规则版本与选择性重算

每个参数声明一个 scope：

- `cleaning` / `stop` / `route_assembly` → 主版本号 +1，影响全部路线
- `lateness` → 次版本号 +1，**仅影响含承诺窗口订单的路线**
- `metrics`（评分权重等）→ 修订号 +1，影响全部路线但清洗决策不变

派生表均以 `(route_id, rule_version_id)` 为主键的一部分；修订插入新行，
旧行置 `superseded`。`rule_revisions` 记录每次的精确影响集合与实际重算集合，
路线详情页可在任意历史版本间切换对比。

## 种子数据覆盖的验证场景

- **D001（Asia/Shanghai）**：精确/空间重复、GPS 跳点、缺失定位、时间缺口、
  真实停留、不可证实的真实配送（日志静默致覆盖率不足 → 仍判 DATA_ISSUE）、
  拥堵事件、迟到订单。
- **D002（Asia/Urumqi）**：跨区域 UTC+6 本地标签、迟到与早到各一。
- **D003（America/New_York → America/Chicago）**：跨午夜单路线、2026 美国 DST
  秋令时回退（EDT−4 → EST−5）、跨时区订单窗口、纯日志暂停导致的不可证实停留。

## 设计约束

- 所有时间戳以带偏移的 ISO-8601 入库并归一化为 UTC `Z`；naive 时间戳直接拒绝。
- 规则是纯数据：同规则版本 + 同输入必得同输出（确定性，测试覆盖）。
- 原始表只追加/upsert，流水线永不修改原始数据；所有派生结果可回溯到点与规则。
