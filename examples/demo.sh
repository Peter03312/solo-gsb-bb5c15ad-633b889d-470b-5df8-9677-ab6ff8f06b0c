#!/usr/bin/env bash
# 数字出版版税结算服务 —— 全流程调用示例
# 用法：先启动服务（docker compose up -d），再执行 ./examples/demo.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8000}"
pp() { python3 -m json.tool 2>/dev/null || cat; }

echo "=== 0. 健康检查 ==="
curl -s "$BASE/health" | pp

echo
echo "=== 1. 录入作品：三档阶梯费率 + 两位作者六四分成 ==="
curl -s -X POST "$BASE/works" -H 'Content-Type: application/json' -d '{
  "work_id": "W-1001",
  "title": "示例长篇小说",
  "tiers": [
    {"threshold": 0,    "rate": 0.08},
    {"threshold": 1000, "rate": 0.10},
    {"threshold": 5000, "rate": 0.12}
  ],
  "authors": [
    {"author_id": "A1", "ratio": 60},
    {"author_id": "A2", "ratio": 40}
  ]
}' | pp

echo
echo "=== 2. 销售入账 S-0001：1200 册 × 29.90 元（跨第一、二档） ==="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S-0001",
  "work_id": "W-1001",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200,
  "price_per_copy": "29.90"
}' | pp

echo
echo "=== 3. 同号同内容重试 S-0001：返回原结果（响应头 X-Idempotent-Replay: true） ==="
curl -s -i -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S-0001",
  "work_id": "W-1001",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1200,
  "price_per_copy": "29.90"
}' | head -20 || true

echo
echo "=== 4. 同号异内容：被拒绝（409） ==="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S-0001",
  "work_id": "W-1001",
  "business_time": "2026-09-01T10:00:00",
  "copies": 1201,
  "price_per_copy": "29.90"
}' | pp

echo
echo "=== 5. 退款 R-0001：退 S-0001 的 200 册（从最后一册起冲回，全部落在第二档） ==="
curl -s -X POST "$BASE/refunds" -H 'Content-Type: application/json' -d '{
  "refund_id": "R-0001",
  "sale_id": "S-0001",
  "copies": 200
}' | pp

echo
echo "=== 6. 销售入账 S-0002：4000 册（累计销量 1200 起，跨第二、三档；退款不回退累计） ==="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S-0002",
  "work_id": "W-1001",
  "business_time": "2026-09-05T09:30:00",
  "copies": 4000,
  "price_per_copy": "29.90"
}' | pp

echo
echo "=== 7. 第一次关账：汇总 S-0001 / R-0001 / S-0002，生成不可改报表 ==="
curl -s -X POST "$BASE/closings" | pp

echo
echo "=== 8. 关账后补录旧业务流水 S-0003（业务时间在上期，计入下一批） ==="
curl -s -X POST "$BASE/sales" -H 'Content-Type: application/json' -d '{
  "sale_id": "S-0003",
  "work_id": "W-1001",
  "business_time": "2026-08-20T08:00:00",
  "copies": 100,
  "price_per_copy": "29.90"
}' | pp

echo
echo "=== 9. 第二次关账：只含补录的 S-0003 ==="
curl -s -X POST "$BASE/closings" | pp

echo
echo "=== 10. 查询第一批关账报表（不可改快照）与批次列表 ==="
curl -s "$BASE/closings/1" | pp
curl -s "$BASE/closings" | pp
