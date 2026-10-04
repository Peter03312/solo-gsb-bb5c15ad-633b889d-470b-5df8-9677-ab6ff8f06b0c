# 数字出版版税结算服务

基于 **Python / FastAPI + SQLite** 的版税结算服务：作品阶梯版税计提、作者分账、退款冲回、周期关账与不可改报表。

## 功能特性

- **作品管理**：录入递增的阶梯销量门槛、非负费率、作者分成比例（合计 100%）
- **销售入账**：按入账序号累计册数，跨档自动分档计提；每册版税四舍五入到分
- **作者分账**：先按比例向下取整到分，余分按作者编号顺序每人补 1 分
- **退款冲回**：从原销售最后一册起反向冲回原计提及分账，不回退累计销量；累计退款不得超过原册数
- **关账报表**：汇总上次关账后到本次提交前的全部流水，生成逐笔明细 + 作者合计的不可改快照；关账后补录的旧业务流水自动计入下一批
- **幂等重试**：同号同内容重试返回原结果（响应头 `X-Idempotent-Replay: true`），同号异内容拒绝（409）
- **并发安全**：入账与关账在数据库写事务内串行提交，归属按数据库提交顺序决定

## 快速开始

### Docker Compose（一键启动，自动初始化数据库）

```bash
docker compose up -d --build
```

服务地址：**http://localhost:8000**（交互式文档 http://localhost:8000/docs）

SQLite 数据库文件存放在命名卷 `royalty-data`（容器内 `/data/royalty.db`），首次启动自动建表。

### 本地开发

```bash
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

### 调用示例脚本

服务启动后执行完整业务演示（建作品 → 销售 → 幂等重试 → 退款 → 关账 → 补录 → 再关账）：

```bash
./examples/demo.sh            # 默认 http://localhost:8000，可用 BASE=... 覆盖
```

## 配置

| 环境变量          | 默认值              | 说明                     |
|-------------------|---------------------|--------------------------|
| `ROYALTY_DB_PATH` | `./royalty.db`（容器内 `/data/royalty.db`） | SQLite 数据库文件路径 |

端口：容器内固定 `8000`，宿主机端口在 `docker-compose.yml` 的 `ports` 中调整（默认 `8000:8000`）。

## 业务规则

### 阶梯计提

- 作品的阶梯 `tiers` 按 `threshold`（累计册数门槛）严格递增，**首档门槛必须为 0**，末档上不封顶；费率 `rate` 为非负十进制数。
- 销售按**入账序号**（提交入库顺序，与业务时间无关）累计册数。一笔销售覆盖的累计区间跨档时**分档计提**。
- 每册版税 = 每册净价 × 该档费率，**四舍五入到分**；各段金额 = 每册版税 × 段内册数，合计为本笔版税。

### 作者分账

- 每位作者应分 = 本笔版税 × 分成比例，**先向下取整到分**。
- 剩余零头（余分）按**作者编号升序**每人补 1 分，直至分完（余分总数必小于作者人数）。

### 退款

- 退款从原销售**最后一册**起反向冲回：先跳过已退册数，再按原销售入账时的分段口径取本次退款册数，冲回对应的计提及分账（金额为负）。
- **不回退累计销量**：后续销售的阶梯基数不受退款影响。
- 同一销售的累计退款册数不得超过原销售册数，超出拒绝（400）。
- 全部退完时，冲回的作者分账与原分账恰好互为相反数。

### 关账

- `POST /closings` 把上次关账后到本次提交前入账的全部流水（销售 + 退款）归入新批次，按入账序号排序生成**逐笔明细**和**作者合计**，快照落库后**不可改**（无任何修改接口）。
- 关账后补录的旧业务时间流水，按入账顺序自然计入**下一批**。
- 并发入账与关账在数据库写事务中串行提交，**归属由数据库提交顺序决定**。

### 幂等

- 作品、销售、退款的编号均由调用方提供并唯一。
- **同号同内容**重试：返回首次的结果（HTTP 200，响应头 `X-Idempotent-Replay: true`），不重复入账。
- **同号异内容**：拒绝（HTTP 409）。
- 内容比较基于数值规范化后的请求指纹（如 `29.90` 与 `29.9` 视为同内容）。

## API 一览

金额字段一律为**元的字符串**（精确到分，如 `"2988.00"`、退款为负值），避免浮点误差。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| POST | `/works` | 录入作品（阶梯费率 + 作者分成），幂等 |
| GET  | `/works/{work_id}` | 查询作品配置与累计销量 |
| POST | `/sales` | 销售入账，返回分档计提及分账明细，幂等 |
| GET  | `/sales/{sale_id}` | 查询销售流水 |
| POST | `/refunds` | 退款入账（从原销售最后一册起冲回），幂等 |
| GET  | `/refunds/{refund_id}` | 查询退款流水 |
| POST | `/closings` | 关账，生成不可改报表 |
| GET  | `/closings` | 关账批次列表 |
| GET  | `/closings/{batch_id}` | 查询关账报表快照 |

### 错误码

| 状态码 | 场景 |
|--------|------|
| 400 | 业务规则冲突（如累计退款超过原销售册数） |
| 404 | 引用的作品 / 销售 / 退款 / 批次不存在 |
| 409 | 同号异内容的幂等冲突 |
| 422 | 请求参数校验失败（负费率、比例合计非 100、门槛非递增、价格超过两位小数等） |

## 调用示例（curl）

```bash
BASE=http://localhost:8000

# 1. 录入作品：三档阶梯，两位作者六四分成
curl -s -X POST $BASE/works -H 'Content-Type: application/json' -d '{
  "work_id": "W-1001", "title": "示例长篇小说",
  "tiers":   [{"threshold": 0, "rate": 0.08},
              {"threshold": 1000, "rate": 0.10},
              {"threshold": 5000, "rate": 0.12}],
  "authors": [{"author_id": "A1", "ratio": 60},
              {"author_id": "A2", "ratio": 40}]
}'

# 2. 销售入账：1200 册 × 29.90 元，跨前两档
curl -s -X POST $BASE/sales -H 'Content-Type: application/json' -d '{
  "sale_id": "S-0001", "work_id": "W-1001",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200, "price_per_copy": "29.90"
}'
```

响应（节选）：第 1–1000 册按 8% 计提（每册 2.39 元），第 1001–1200 册按 10%（每册 2.99 元），版税合计 2988.00 元，A1 分 1792.80、A2 分 1195.20。

```json
{
  "sale_id": "S-0001", "entry_seq": 1, "base_copies": 0,
  "royalty": "2988.00",
  "segments": [
    {"tier_index": 0, "copies": 1000, "per_copy_royalty": "2.39", "range": [1, 1000], "amount": "2390.00"},
    {"tier_index": 1, "copies": 200,  "per_copy_royalty": "2.99", "range": [1001, 1200], "amount": "598.00"}
  ],
  "splits": [{"author_id": "A1", "amount": "1792.80"},
             {"author_id": "A2", "amount": "1195.20"}],
  "batch_id": null
}
```

```bash
# 3. 退款：退 S-0001 的 200 册（从最后一册起，全部落在第二档）
curl -s -X POST $BASE/refunds -H 'Content-Type: application/json' -d '{
  "refund_id": "R-0001", "sale_id": "S-0001", "copies": 200
}'
# → royalty "-598.00"，splits A1 "-358.80" / A2 "-239.20"

# 4. 关账：汇总本批全部流水，生成不可改报表
curl -s -X POST $BASE/closings

# 5. 查看报表快照与批次列表
curl -s $BASE/closings/1
curl -s $BASE/closings
```

关账报表（结构示例）：

```json
{
  "batch_id": 1,
  "closed_at": "2026-10-04T15:15:46+00:00",
  "sales_count": 2, "refunds_count": 1,
  "total_royalty": "14470.00",
  "items": [
    {"item_seq": 1, "type": "SALE",   "sale_id": "S-0001",   "copies": 1200, "royalty": "2988.00",  "segments": [...], "splits": [...]},
    {"item_seq": 2, "type": "REFUND", "refund_id": "R-0001", "sale_id": "S-0001", "copies": 200, "royalty": "-598.00", "splits": [...]},
    {"item_seq": 3, "type": "SALE",   "sale_id": "S-0002",   "copies": 4000, "royalty": "12080.00", "segments": [...], "splits": [...]}
  ],
  "author_totals": [{"author_id": "A1", "amount": "8682.00"},
                    {"author_id": "A2", "amount": "5788.00"}]
}
```

更多场景（幂等重试、同号异内容 409、关账后补录旧流水入下一批）见 `examples/demo.sh`。

## 项目结构

```
app/
  main.py    # FastAPI 路由、请求校验、视图组装、关账
  logic.py   # 阶梯计提 / 分账 / 退款冲回核心计算（纯函数，整数分）
  db.py      # SQLite 连接、建表、写事务（进程内串行 + BEGIN IMMEDIATE）
tests/
  test_api.py  # 计提、舍入、分账、退款、关账、幂等、并发共 30 个用例
examples/
  demo.sh    # 全流程 curl 调用示例
Dockerfile
docker-compose.yml
pytest.ini
requirements.txt / requirements-dev.txt
```

## 实现要点

- **金额**：内部一律整数分计算；每册版税用 `Decimal` 以 `ROUND_HALF_UP` 四舍五入到分；接口出入参均为元的字符串。
- **入账序号**：销售与退款共用一张 `entries` 自增序号表，保证全局入账顺序，累计销量与关账归属都以此为口径。
- **并发**：所有写操作经进程内写锁 + `BEGIN IMMEDIATE` 事务串行提交，因此并发入账与关账的归属严格按数据库提交顺序；读操作走 WAL 模式不阻塞。
- **不可改报表**：关账时把每笔流水的金额、分账、分段明细快照进 `batch_items` / `batch_author_totals`，之后任何入账/退款都不触碰历史批次。
