"""版税结算服务端到端测试：计提、分账、退款、关账、幂等与并发。"""
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app


@pytest.fixture()
def client():
    for suffix in ("", "-wal", "-shm"):
        path = db.DB_PATH + suffix
        if os.path.exists(path):
            os.remove(path)
    db.init_db()
    with TestClient(app) as c:
        yield c


def make_work(client, work_id="W1", tiers=None, authors=None):
    payload = {
        "work_id": work_id,
        "title": "测试作品",
        "tiers": tiers if tiers is not None else [{"threshold": 0, "rate": 0.1}],
        "authors": authors if authors is not None else [{"author_id": "A1", "ratio": 100}],
    }
    return client.post("/works", json=payload)


def make_sale(client, sale_id="S1", work_id="W1", copies=1, price="10.00",
              business_time="2026-09-01T10:00:00"):
    return client.post("/sales", json={
        "sale_id": sale_id, "work_id": work_id, "business_time": business_time,
        "copies": copies, "price_per_copy": price,
    })


# ---------------------------------------------------------------- 作品校验

def test_create_work_ok(client):
    r = make_work(client, tiers=[{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.1}],
                  authors=[{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["work_id"] == "W1"
    assert body["cumulative_copies"] == 0
    assert body["tiers"][1]["threshold"] == 1000


@pytest.mark.parametrize("tiers,authors", [
    ([{"threshold": 1, "rate": 0.1}], None),                                  # 首档门槛非 0
    ([{"threshold": 0, "rate": 0.1}, {"threshold": 0, "rate": 0.2}], None),   # 门槛不递增
    ([{"threshold": 0, "rate": 0.1}, {"threshold": 500, "rate": 0.2},
      {"threshold": 500, "rate": 0.3}], None),
    ([{"threshold": 0, "rate": -0.1}], None),                                 # 负费率
    (None, [{"author_id": "A1", "ratio": 60}]),                               # 比例合计不足 100
    (None, [{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 50}]),
    (None, [{"author_id": "A1", "ratio": 50}, {"author_id": "A1", "ratio": 50}]),  # 作者重复
])
def test_work_validation_rejected(client, tiers, authors):
    r = make_work(client, tiers=tiers, authors=authors)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 计提与分账

def test_sale_single_tier(client):
    make_work(client)
    r = make_sale(client, copies=3, price="10.00")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["royalty"] == "3.00"          # 3 册 × 10.00 × 0.1
    assert body["base_copies"] == 0
    assert body["splits"] == [{"author_id": "A1", "amount": "3.00"}]
    assert body["batch_id"] is None


def test_per_copy_rounding_half_up(client):
    # 每册净价 0.10 元 = 10 分
    make_work(client, tiers=[{"threshold": 0, "rate": 0.04}])
    r = make_sale(client, copies=1, price="0.10")
    assert r.json()["royalty"] == "0.00"      # 0.4 分 → 舍去为 0 分
    make_work(client, work_id="W2", tiers=[{"threshold": 0, "rate": 0.05}])
    r = client.post("/sales", json={"sale_id": "S2", "work_id": "W2",
                                    "business_time": "2026-09-01T10:00:00",
                                    "copies": 1, "price_per_copy": "0.10"})
    assert r.json()["royalty"] == "0.01"      # 0.5 分 → 四舍五入为 1 分
    make_work(client, work_id="W3", tiers=[{"threshold": 0, "rate": 0.049}])
    r = client.post("/sales", json={"sale_id": "S3", "work_id": "W3",
                                    "business_time": "2026-09-01T10:00:00",
                                    "copies": 1, "price_per_copy": "0.10"})
    assert r.json()["royalty"] == "0.00"      # 0.49 分 → 0 分


def test_cross_tier_royalty(client):
    make_work(client,
              tiers=[{"threshold": 0, "rate": 0.08}, {"threshold": 1000, "rate": 0.10},
                     {"threshold": 5000, "rate": 0.12}],
              authors=[{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}])
    r = make_sale(client, copies=1200, price="29.90")
    assert r.status_code == 201, r.text
    body = r.json()
    segs = body["segments"]
    assert [(s["tier_index"], s["copies"]) for s in segs] == [(0, 1000), (1, 200)]
    assert segs[0]["per_copy_royalty"] == "2.39"   # 29.90×0.08=2.392 → 2.39
    assert segs[1]["per_copy_royalty"] == "2.99"   # 29.90×0.10=2.99
    assert segs[0]["range"] == [1, 1000] and segs[1]["range"] == [1001, 1200]
    assert body["royalty"] == "2988.00"            # 2390.00 + 598.00
    assert body["splits"] == [{"author_id": "A1", "amount": "1792.80"},
                              {"author_id": "A2", "amount": "1195.20"}]
    # 累计销量推进
    assert client.get("/works/W1").json()["cumulative_copies"] == 1200


def test_cumulative_across_sales(client):
    make_work(client, tiers=[{"threshold": 0, "rate": 0.1}, {"threshold": 10, "rate": 0.5}])
    make_sale(client, sale_id="S1", copies=8)
    r = make_sale(client, sale_id="S2", copies=5)   # 第 9~13 册，跨档
    body = r.json()
    assert body["base_copies"] == 8
    assert [(s["tier_index"], s["copies"]) for s in body["segments"]] == [(0, 2), (1, 3)]
    # 2×1.00 + 3×5.00 = 17.00
    assert body["royalty"] == "17.00"


def test_split_remainder_filled_by_author_order(client):
    # 输入顺序打乱，余分应按作者编号顺序补齐
    make_work(client, tiers=[{"threshold": 0, "rate": 1}],
              authors=[{"author_id": "A3", "ratio": 33.34},
                       {"author_id": "A1", "ratio": 33.33},
                       {"author_id": "A2", "ratio": 33.33}])
    r = make_sale(client, copies=1, price="1.00")   # 版税 100 分
    assert r.json()["splits"] == [{"author_id": "A1", "amount": "0.34"},
                                  {"author_id": "A2", "amount": "0.33"},
                                  {"author_id": "A3", "amount": "0.33"}]


def test_sale_unknown_work(client):
    r = make_sale(client, work_id="NOPE")
    assert r.status_code == 404


@pytest.mark.parametrize("payload_patch", [
    {"copies": 0}, {"copies": -3}, {"price_per_copy": "-1"}, {"price_per_copy": "1.234"},
])
def test_sale_validation_rejected(client, payload_patch):
    make_work(client)
    payload = {"sale_id": "S1", "work_id": "W1", "business_time": "2026-09-01T10:00:00",
               "copies": 1, "price_per_copy": "10.00"}
    payload.update(payload_patch)
    assert client.post("/sales", json=payload).status_code == 422


# ---------------------------------------------------------------- 退款

def test_refund_reverses_from_last_copy(client):
    make_work(client, tiers=[{"threshold": 0, "rate": 0.10}, {"threshold": 10, "rate": 0.50}])
    make_sale(client, copies=12, price="10.00")     # 10×1.00 + 2×5.00 = 20.00
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 3})
    assert r.status_code == 201, r.text
    body = r.json()
    # 从最后一册起：2 册高档(5.00) + 1 册低档(1.00) = 11.00
    assert body["royalty"] == "-11.00"
    assert [(s["tier_index"], s["copies"]) for s in body["segments"]] == [(0, 1), (1, 2)]
    assert body["splits"] == [{"author_id": "A1", "amount": "-11.00"}]
    assert client.get("/sales/S1").json()["refunded_copies"] == 3

    r = client.post("/refunds", json={"refund_id": "R2", "sale_id": "S1", "copies": 9})
    assert r.json()["royalty"] == "-9.00"           # 剩余 9 册全部在低档
    # 已退 12 册 = 原册数，再退即拒绝
    r = client.post("/refunds", json={"refund_id": "R3", "sale_id": "S1", "copies": 1})
    assert r.status_code == 400


def test_full_refund_exactly_reverses_split(client):
    make_work(client, tiers=[{"threshold": 0, "rate": 1}],
              authors=[{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}])
    make_sale(client, copies=1, price="1.01")       # 101 分 → 61/40
    sale = client.get("/sales/S1").json()
    assert sale["splits"] == [{"author_id": "A1", "amount": "0.61"},
                              {"author_id": "A2", "amount": "0.40"}]
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 1})
    assert r.json()["splits"] == [{"author_id": "A1", "amount": "-0.61"},
                                  {"author_id": "A2", "amount": "-0.40"}]


def test_refund_does_not_roll_back_cumulative(client):
    make_work(client)
    make_sale(client, sale_id="S1", copies=10)
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})
    r = make_sale(client, sale_id="S2", copies=5)
    assert r.json()["base_copies"] == 10            # 累计销量不回退
    assert client.get("/works/W1").json()["cumulative_copies"] == 15


def test_refund_unknown_sale(client):
    r = client.post("/refunds", json={"refund_id": "R1", "sale_id": "NOPE", "copies": 1})
    assert r.status_code == 404


# ---------------------------------------------------------------- 关账

def test_close_generates_immutable_report(client):
    make_work(client, authors=[{"author_id": "A1", "ratio": 60}, {"author_id": "A2", "ratio": 40}])
    make_sale(client, sale_id="S1", copies=100, price="10.00")   # 100.00 → 60/40
    client.post("/refunds", json={"refund_id": "R1", "sale_id": "S1", "copies": 10})  # -10.00

    r = client.post("/closings")
    assert r.status_code == 201, r.text
    batch = r.json()
    assert batch["batch_id"] == 1
    assert batch["sales_count"] == 1 and batch["refunds_count"] == 1
    assert batch["total_royalty"] == "90.00"
    assert [it["type"] for it in batch["items"]] == ["SALE", "REFUND"]
    assert batch["items"][0]["sale_id"] == "S1"
    assert batch["items"][1]["refund_id"] == "R1"
    assert batch["author_totals"] == [{"author_id": "A1", "amount": "54.00"},
                                      {"author_id": "A2", "amount": "36.00"}]

    # 关账后补录的旧业务流水计入下一批
    make_sale(client, sale_id="S2", copies=1, price="10.00",
              business_time="2020-01-01T00:00:00")
    batch2 = client.post("/closings").json()
    assert batch2["batch_id"] == 2
    assert batch2["sales_count"] == 1 and batch2["refunds_count"] == 0
    assert [it["sale_id"] for it in batch2["items"]] == ["S2"]

    # 第一批报表不可改：内容保持原样
    again = client.get("/closings/1").json()
    assert again["sales_count"] == 1
    assert [(it["type"], it.get("sale_id") if it["type"] == "SALE" else it.get("refund_id"))
            for it in again["items"]] == [("SALE", "S1"), ("REFUND", "R1")]
    assert again["author_totals"] == batch["author_totals"]
    assert client.get("/closings/999").status_code == 404


def test_close_empty_period(client):
    r = client.post("/closings")
    assert r.status_code == 201
    body = r.json()
    assert body["sales_count"] == 0 and body["refunds_count"] == 0
    assert body["total_royalty"] == "0.00" and body["items"] == []


def test_list_closings(client):
    make_work(client)
    make_sale(client)
    client.post("/closings")
    client.post("/closings")
    r = client.get("/closings")
    assert [b["batch_id"] for b in r.json()["batches"]] == [1, 2]


# ---------------------------------------------------------------- 幂等

def test_sale_idempotent_replay_and_conflict(client):
    make_work(client)
    r1 = make_sale(client, copies=5)
    assert r1.status_code == 201
    r2 = make_sale(client, copies=5)                # 同号同内容
    assert r2.status_code == 200
    assert r2.headers.get("X-Idempotent-Replay") == "true"
    assert r2.json() == r1.json()                   # 返回原结果
    r3 = make_sale(client, copies=6)                # 同号异内容
    assert r3.status_code == 409
    # 只入账一次
    assert client.post("/closings").json()["sales_count"] == 1


def test_refund_idempotent_replay_and_conflict(client):
    make_work(client)
    make_sale(client, copies=5)
    p = {"refund_id": "R1", "sale_id": "S1", "copies": 2}
    r1 = client.post("/refunds", json=p)
    r2 = client.post("/refunds", json=p)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.json() == r1.json()
    assert client.post("/refunds", json={**p, "copies": 3}).status_code == 409
    assert client.get("/sales/S1").json()["refunded_copies"] == 2


def test_work_idempotent_replay_and_conflict(client):
    r1 = make_work(client)
    r2 = make_work(client)
    assert r1.status_code == 201 and r2.status_code == 200
    assert r2.json() == r1.json()
    r3 = make_work(client, authors=[{"author_id": "A9", "ratio": 100}])
    assert r3.status_code == 409


# ---------------------------------------------------------------- 并发

def test_concurrent_sales_then_close(client):
    make_work(client, tiers=[{"threshold": 0, "rate": 1}])

    def post(i):
        return client.post("/sales", json={
            "sale_id": f"S{i}", "work_id": "W1",
            "business_time": "2026-09-01T10:00:00", "copies": 1, "price_per_copy": "1.00",
        })

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(post, range(8)))
    assert all(r.status_code == 201 for r in results)
    assert client.get("/works/W1").json()["cumulative_copies"] == 8

    batch = client.post("/closings").json()
    assert batch["sales_count"] == 8
    assert batch["total_royalty"] == "8.00"
    # 每笔销售归属且仅归属本批次
    for i in range(8):
        assert client.get(f"/sales/S{i}").json()["batch_id"] == batch["batch_id"]


def test_concurrent_same_sale_id_settles_once(client):
    make_work(client, tiers=[{"threshold": 0, "rate": 1}])
    payload = {"sale_id": "S1", "work_id": "W1", "business_time": "2026-09-01T10:00:00",
               "copies": 1, "price_per_copy": "1.00"}

    def post(_):
        return client.post("/sales", json=payload)

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(post, range(6)))
    assert {r.status_code for r in results} == {200, 201}
    assert len({r.json()["entry_seq"] for r in results}) == 1   # 同一笔入账序号
    assert client.post("/closings").json()["sales_count"] == 1
