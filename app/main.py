"""数字出版版税结算服务 —— HTTP API（FastAPI + SQLite）。"""
from __future__ import annotations

import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from . import db
from .logic import compute_segments, refund_segments, split_cents

REPLAY_HEADER = "X-Idempotent-Replay"


# ---------------------------------------------------------------- 请求模型

class TierIn(BaseModel):
    threshold: int = Field(ge=0, description="累计销量达到该册数起适用本档费率")
    rate: Decimal = Field(ge=0, description="非负费率：每册版税 = 每册净价 × 费率")


class AuthorIn(BaseModel):
    author_id: str = Field(min_length=1, max_length=64)
    ratio: Decimal = Field(ge=0, description="分成比例（百分数），同一作品合计必须为 100")


class WorkIn(BaseModel):
    work_id: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=256)
    tiers: list[TierIn] = Field(min_length=1, description="阶梯门槛严格递增，首档门槛必须为 0")
    authors: list[AuthorIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _check(self) -> "WorkIn":
        thresholds = [t.threshold for t in self.tiers]
        if thresholds[0] != 0:
            raise ValueError("首个阶梯门槛必须为 0")
        if any(b <= a for a, b in zip(thresholds, thresholds[1:])):
            raise ValueError("阶梯销量门槛必须严格递增")
        ids = [a.author_id for a in self.authors]
        if len(set(ids)) != len(ids):
            raise ValueError("作者编号不能重复")
        if sum(a.ratio for a in self.authors) != Decimal(100):
            raise ValueError("作者分成比例合计必须为 100%")
        return self


class SaleIn(BaseModel):
    sale_id: str = Field(min_length=1, max_length=64, description="销售流水唯一编号")
    work_id: str = Field(min_length=1, max_length=64)
    business_time: datetime = Field(description="业务时间（ISO 8601）")
    copies: int = Field(ge=1, description="正整数册数")
    price_per_copy: Decimal = Field(ge=0, description="非负每册净价（元），最多两位小数")

    @field_validator("price_per_copy")
    @classmethod
    def _two_decimals(cls, v: Decimal) -> Decimal:
        if v != v.quantize(Decimal("0.01")):
            raise ValueError("每册净价最多两位小数（精确到分）")
        return v


class RefundIn(BaseModel):
    refund_id: str = Field(min_length=1, max_length=64, description="退款流水唯一编号")
    sale_id: str = Field(min_length=1, max_length=64, description="原销售编号")
    copies: int = Field(ge=1, description="正整数册数，累计退款不得超过原销售册数")


# ---------------------------------------------------------------- 通用工具

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def yuan(cents: int) -> str:
    """整数分 -> 元的字符串（保留两位），避免浮点误差。"""
    sign = "-" if cents < 0 else ""
    c = abs(cents)
    return f"{sign}{c // 100}.{c % 100:02d}"


def dec_str(d: Decimal) -> str:
    """十进制规范化字符串（不用科学计数法）。"""
    return format(d.normalize(), "f")


def _canon(value: Any):
    if isinstance(value, Decimal):
        return dec_str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot canonicalize {type(value)!r}")


def payload_hash(model: BaseModel) -> str:
    """请求内容指纹：数值规范化后的规范 JSON 的 SHA-256，用于幂等判定。"""
    text = json.dumps(model.model_dump(), default=_canon, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 查询与视图

def _load_work(conn, work_id: str) -> dict | None:
    w = conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
    if w is None:
        return None
    tiers = conn.execute(
        "SELECT tier_index, threshold, rate FROM work_tiers WHERE work_id = ? ORDER BY tier_index",
        (work_id,),
    ).fetchall()
    authors = conn.execute(
        "SELECT author_id, ratio FROM work_authors WHERE work_id = ? ORDER BY author_id",
        (work_id,),
    ).fetchall()
    return {
        "row": w,
        "tiers": [
            {"tier_index": t["tier_index"], "threshold": t["threshold"], "rate": Decimal(t["rate"])}
            for t in tiers
        ],
        "authors": [{"author_id": a["author_id"], "ratio": Decimal(a["ratio"])} for a in authors],
    }


def work_view(conn, work_id: str) -> dict:
    work = _load_work(conn, work_id)
    w = work["row"]
    return {
        "work_id": w["work_id"],
        "title": w["title"],
        "tiers": [
            {"tier_index": t["tier_index"], "threshold": t["threshold"], "rate": dec_str(t["rate"])}
            for t in work["tiers"]
        ],
        "authors": [
            {"author_id": a["author_id"], "ratio": dec_str(a["ratio"])} for a in work["authors"]
        ],
        "cumulative_copies": w["cumulative_copies"],
        "created_at": w["created_at"],
    }


def _sale_rows(conn, sale_id: str):
    s = conn.execute("SELECT * FROM sales WHERE sale_id = ?", (sale_id,)).fetchone()
    segs = conn.execute(
        "SELECT * FROM sale_segments WHERE sale_id = ? ORDER BY seg_index", (sale_id,)
    ).fetchall()
    splits = conn.execute(
        "SELECT author_id, amount_cents FROM sale_splits WHERE sale_id = ? ORDER BY author_id",
        (sale_id,),
    ).fetchall()
    return s, segs, splits


def sale_view(conn, sale_id: str) -> dict:
    s, segs, splits = _sale_rows(conn, sale_id)
    batch = conn.execute(
        "SELECT batch_id FROM entries WHERE entry_seq = ?", (s["entry_seq"],)
    ).fetchone()
    return {
        "sale_id": s["sale_id"],
        "entry_seq": s["entry_seq"],
        "work_id": s["work_id"],
        "business_time": s["business_time"],
        "copies": s["copies"],
        "price_per_copy": yuan(s["price_cents"]),
        "base_copies": s["base_copies"],
        "refunded_copies": s["refunded_copies"],
        "royalty": yuan(s["royalty_cents"]),
        "segments": [
            {
                "tier_index": g["tier_index"],
                "threshold": g["threshold"],
                "rate": g["rate"],
                "copies": g["copies"],
                "per_copy_royalty": yuan(g["per_copy_cents"]),
                "range": [g["start_no"], g["end_no"]],
                "amount": yuan(g["amount_cents"]),
            }
            for g in segs
        ],
        "splits": [{"author_id": sp["author_id"], "amount": yuan(sp["amount_cents"])} for sp in splits],
        "batch_id": batch["batch_id"],
    }


def _refund_rows(conn, refund_id: str):
    r = conn.execute("SELECT * FROM refunds WHERE refund_id = ?", (refund_id,)).fetchone()
    segs = conn.execute(
        "SELECT * FROM refund_segments WHERE refund_id = ? ORDER BY seg_index", (refund_id,)
    ).fetchall()
    splits = conn.execute(
        "SELECT author_id, amount_cents FROM refund_splits WHERE refund_id = ? ORDER BY author_id",
        (refund_id,),
    ).fetchall()
    return r, segs, splits


def refund_view(conn, refund_id: str) -> dict:
    r, segs, splits = _refund_rows(conn, refund_id)
    batch = conn.execute(
        "SELECT batch_id FROM entries WHERE entry_seq = ?", (r["entry_seq"],)
    ).fetchone()
    return {
        "refund_id": r["refund_id"],
        "entry_seq": r["entry_seq"],
        "sale_id": r["sale_id"],
        "copies": r["copies"],
        "royalty": yuan(r["royalty_cents"]),
        "segments": [
            {
                "tier_index": g["tier_index"],
                "rate": g["rate"],
                "copies": g["copies"],
                "per_copy_royalty": yuan(g["per_copy_cents"]),
                "amount": yuan(g["amount_cents"]),
            }
            for g in segs
        ],
        "splits": [{"author_id": sp["author_id"], "amount": yuan(sp["amount_cents"])} for sp in splits],
        "batch_id": batch["batch_id"],
    }


def _render_segment(seg: dict) -> dict:
    return {
        "tier_index": seg["tier_index"],
        "rate": seg["rate"],
        "copies": seg["copies"],
        "per_copy_royalty": yuan(seg["per_copy_cents"]),
        "amount": yuan(seg["amount_cents"]),
    }


def _render_batch_item(it) -> dict:
    detail = json.loads(it["detail_json"])
    splits = json.loads(it["splits_json"])
    item = {
        "item_seq": it["item_seq"],
        "entry_seq": it["entry_seq"],
        "type": it["entry_type"],
        "work_id": it["work_id"],
        "copies": it["copies"],
        "royalty": yuan(it["royalty_cents"]),
        "splits": [
            {"author_id": s["author_id"], "amount": yuan(s["amount_cents"])} for s in splits
        ],
        "segments": [_render_segment(s) for s in detail["segments"]],
    }
    if it["entry_type"] == "SALE":
        item["sale_id"] = it["ref_id"]
        item["business_time"] = detail["business_time"]
        item["price_per_copy"] = yuan(detail["price_cents"])
        item["base_copies"] = detail["base_copies"]
    else:
        item["refund_id"] = it["ref_id"]
        item["sale_id"] = detail["sale_id"]
    return item


def batch_view(conn, batch_id: int) -> dict | None:
    b = conn.execute("SELECT * FROM batches WHERE batch_id = ?", (batch_id,)).fetchone()
    if b is None:
        return None
    items = conn.execute(
        "SELECT * FROM batch_items WHERE batch_id = ? ORDER BY item_seq", (batch_id,)
    ).fetchall()
    totals = conn.execute(
        "SELECT author_id, amount_cents FROM batch_author_totals WHERE batch_id = ? ORDER BY author_id",
        (batch_id,),
    ).fetchall()
    return {
        "batch_id": b["batch_id"],
        "closed_at": b["closed_at"],
        "sales_count": b["sales_count"],
        "refunds_count": b["refunds_count"],
        "total_royalty": yuan(b["total_royalty_cents"]),
        "items": [_render_batch_item(it) for it in items],
        "author_totals": [
            {"author_id": t["author_id"], "amount": yuan(t["amount_cents"])} for t in totals
        ],
    }


# ---------------------------------------------------------------- 应用与路由

@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()  # 启动即初始化数据库（幂等）
    yield


app = FastAPI(
    title="数字出版版税结算服务",
    version="1.0.0",
    description="阶梯版税计提、作者分账、退款冲回与关账报表。金额均为元的字符串，精确到分。",
    lifespan=lifespan,
)


@app.get("/")
def index() -> dict:
    return {"service": "数字出版版税结算服务", "docs": "/docs", "health": "/health"}


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


# ---------------- 作品 ----------------

@app.post("/works", status_code=201, summary="录入作品（阶梯费率 + 作者分成）")
def create_work(body: WorkIn, response: Response) -> dict:
    h = payload_hash(body)
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT payload_hash FROM works WHERE work_id = ?", (body.work_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] == h:  # 同号同内容：返回原结果
                response.status_code = 200
                response.headers[REPLAY_HEADER] = "true"
                return work_view(conn, body.work_id)
            raise HTTPException(409, f"作品编号 {body.work_id} 已存在且内容不同")
        conn.execute(
            "INSERT INTO works (work_id, title, payload_hash, cumulative_copies, created_at)"
            " VALUES (?, ?, ?, 0, ?)",
            (body.work_id, body.title, h, _utcnow()),
        )
        for i, t in enumerate(body.tiers):
            conn.execute(
                "INSERT INTO work_tiers (work_id, tier_index, threshold, rate) VALUES (?, ?, ?, ?)",
                (body.work_id, i, t.threshold, dec_str(t.rate)),
            )
        for a in body.authors:
            conn.execute(
                "INSERT INTO work_authors (work_id, author_id, ratio) VALUES (?, ?, ?)",
                (body.work_id, a.author_id, dec_str(a.ratio)),
            )
        return work_view(conn, body.work_id)


@app.get("/works/{work_id}", summary="查询作品配置与累计销量")
def get_work(work_id: str) -> dict:
    with db.read_conn() as conn:
        if _load_work(conn, work_id) is None:
            raise HTTPException(404, f"作品 {work_id} 不存在")
        return work_view(conn, work_id)


# ---------------- 销售入账 ----------------

@app.post("/sales", status_code=201, summary="销售入账（按入账序号累计、跨档分档计提）")
def create_sale(body: SaleIn, response: Response) -> dict:
    h = payload_hash(body)
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT payload_hash FROM sales WHERE sale_id = ?", (body.sale_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] == h:  # 同号同内容重试：返回原结果
                response.status_code = 200
                response.headers[REPLAY_HEADER] = "true"
                return sale_view(conn, body.sale_id)
            raise HTTPException(409, f"销售编号 {body.sale_id} 已存在且内容不同")

        work = _load_work(conn, body.work_id)
        if work is None:
            raise HTTPException(404, f"作品 {body.work_id} 不存在")

        price_cents = int(body.price_per_copy * 100)
        base = work["row"]["cumulative_copies"]
        segments = compute_segments(work["tiers"], base, body.copies, price_cents)
        total = sum(s["amount_cents"] for s in segments)
        splits = split_cents(total, work["authors"])

        cur = conn.execute(
            "INSERT INTO entries (entry_type, ref_id) VALUES ('SALE', ?)", (body.sale_id,)
        )
        seq = cur.lastrowid
        conn.execute(
            "INSERT INTO sales (entry_seq, sale_id, work_id, business_time, copies, price_cents,"
            " base_copies, royalty_cents, refunded_copies, payload_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (
                seq, body.sale_id, body.work_id, body.business_time.isoformat(), body.copies,
                price_cents, base, total, h, _utcnow(),
            ),
        )
        for i, s in enumerate(segments):
            conn.execute(
                "INSERT INTO sale_segments (sale_id, seg_index, tier_index, threshold, rate,"
                " copies, per_copy_cents, start_no, end_no, amount_cents)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    body.sale_id, i, s["tier_index"], s["threshold"], dec_str(s["rate"]),
                    s["copies"], s["per_copy_cents"], s["start_no"], s["end_no"],
                    s["amount_cents"],
                ),
            )
        for sp in splits:
            conn.execute(
                "INSERT INTO sale_splits (sale_id, author_id, amount_cents) VALUES (?, ?, ?)",
                (body.sale_id, sp["author_id"], sp["amount_cents"]),
            )
        conn.execute(
            "UPDATE works SET cumulative_copies = cumulative_copies + ? WHERE work_id = ?",
            (body.copies, body.work_id),
        )
        return sale_view(conn, body.sale_id)


@app.get("/sales/{sale_id}", summary="查询销售流水及计提分账明细")
def get_sale(sale_id: str) -> dict:
    with db.read_conn() as conn:
        if conn.execute("SELECT 1 FROM sales WHERE sale_id = ?", (sale_id,)).fetchone() is None:
            raise HTTPException(404, f"销售流水 {sale_id} 不存在")
        return sale_view(conn, sale_id)


# ---------------- 退款入账 ----------------

@app.post("/refunds", status_code=201, summary="退款入账（从原销售最后一册起冲回）")
def create_refund(body: RefundIn, response: Response) -> dict:
    h = payload_hash(body)
    with db.write_tx() as conn:
        existing = conn.execute(
            "SELECT payload_hash FROM refunds WHERE refund_id = ?", (body.refund_id,)
        ).fetchone()
        if existing is not None:
            if existing["payload_hash"] == h:  # 同号同内容重试：返回原结果
                response.status_code = 200
                response.headers[REPLAY_HEADER] = "true"
                return refund_view(conn, body.refund_id)
            raise HTTPException(409, f"退款编号 {body.refund_id} 已存在且内容不同")

        sale = conn.execute("SELECT * FROM sales WHERE sale_id = ?", (body.sale_id,)).fetchone()
        if sale is None:
            raise HTTPException(404, f"原销售流水 {body.sale_id} 不存在")
        remaining = sale["copies"] - sale["refunded_copies"]
        if body.copies > remaining:
            raise HTTPException(
                400,
                f"累计退款不得超过原销售册数：原销售 {sale['copies']} 册，"
                f"已退 {sale['refunded_copies']} 册，本次申请 {body.copies} 册",
            )

        work = _load_work(conn, sale["work_id"])
        _, segs, _ = _sale_rows(conn, body.sale_id)
        seg_dicts = [dict(g) for g in segs]
        back = refund_segments(seg_dicts, sale["refunded_copies"], body.copies)
        amount = sum(s["amount_cents"] for s in back)          # 正数，入库时取负
        splits = split_cents(amount, work["authors"])          # 按同一规则分账后取负

        cur = conn.execute(
            "INSERT INTO entries (entry_type, ref_id) VALUES ('REFUND', ?)", (body.refund_id,)
        )
        seq = cur.lastrowid
        conn.execute(
            "INSERT INTO refunds (entry_seq, refund_id, sale_id, copies, royalty_cents,"
            " payload_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq, body.refund_id, body.sale_id, body.copies, -amount, h, _utcnow()),
        )
        for i, s in enumerate(back):
            conn.execute(
                "INSERT INTO refund_segments (refund_id, seg_index, tier_index, rate, copies,"
                " per_copy_cents, amount_cents) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    body.refund_id, i, s["tier_index"], dec_str(Decimal(s["rate"])),
                    s["copies"], -s["per_copy_cents"], -s["amount_cents"],
                ),
            )
        for sp in splits:
            conn.execute(
                "INSERT INTO refund_splits (refund_id, author_id, amount_cents) VALUES (?, ?, ?)",
                (body.refund_id, sp["author_id"], -sp["amount_cents"]),
            )
        conn.execute(
            "UPDATE sales SET refunded_copies = refunded_copies + ? WHERE sale_id = ?",
            (body.copies, body.sale_id),
        )
        return refund_view(conn, body.refund_id)


@app.get("/refunds/{refund_id}", summary="查询退款流水及冲回明细")
def get_refund(refund_id: str) -> dict:
    with db.read_conn() as conn:
        if conn.execute("SELECT 1 FROM refunds WHERE refund_id = ?", (refund_id,)).fetchone() is None:
            raise HTTPException(404, f"退款流水 {refund_id} 不存在")
        return refund_view(conn, refund_id)


# ---------------- 关账 ----------------

@app.post("/closings", status_code=201, summary="关账：汇总上次关账后的全部流水，生成不可改报表")
def close_period() -> dict:
    with db.write_tx() as conn:
        cur = conn.execute(
            "INSERT INTO batches (closed_at, sales_count, refunds_count, total_royalty_cents)"
            " VALUES (?, 0, 0, 0)",
            (_utcnow(),),
        )
        batch_id = cur.lastrowid
        pending = conn.execute(
            "SELECT entry_seq, entry_type, ref_id FROM entries WHERE batch_id IS NULL"
            " ORDER BY entry_seq"
        ).fetchall()

        sales_count = refunds_count = 0
        net = 0
        totals: dict[str, int] = {}
        for item_seq, e in enumerate(pending, start=1):
            if e["entry_type"] == "SALE":
                s, segs, splits = _sale_rows(conn, e["ref_id"])
                sales_count += 1
                work_id, copies, royalty = s["work_id"], s["copies"], s["royalty_cents"]
                detail = {
                    "business_time": s["business_time"],
                    "price_cents": s["price_cents"],
                    "base_copies": s["base_copies"],
                    "segments": [dict(g) for g in segs],
                }
            else:
                r, segs, splits = _refund_rows(conn, e["ref_id"])
                refunds_count += 1
                sale = conn.execute(
                    "SELECT work_id FROM sales WHERE sale_id = ?", (r["sale_id"],)
                ).fetchone()
                work_id, copies, royalty = sale["work_id"], r["copies"], r["royalty_cents"]
                detail = {"sale_id": r["sale_id"], "segments": [dict(g) for g in segs]}

            splits_payload = [
                {"author_id": sp["author_id"], "amount_cents": sp["amount_cents"]} for sp in splits
            ]
            conn.execute(
                "INSERT INTO batch_items (batch_id, item_seq, entry_type, ref_id, entry_seq,"
                " work_id, copies, royalty_cents, splits_json, detail_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id, item_seq, e["entry_type"], e["ref_id"], e["entry_seq"],
                    work_id, copies, royalty,
                    json.dumps(splits_payload, ensure_ascii=False),
                    json.dumps(detail, ensure_ascii=False, default=str),
                ),
            )
            conn.execute(
                "UPDATE entries SET batch_id = ? WHERE entry_seq = ?", (batch_id, e["entry_seq"])
            )
            net += royalty
            for sp in splits_payload:
                totals[sp["author_id"]] = totals.get(sp["author_id"], 0) + sp["amount_cents"]

        for author_id in sorted(totals):
            conn.execute(
                "INSERT INTO batch_author_totals (batch_id, author_id, amount_cents) VALUES (?, ?, ?)",
                (batch_id, author_id, totals[author_id]),
            )
        conn.execute(
            "UPDATE batches SET sales_count = ?, refunds_count = ?, total_royalty_cents = ?"
            " WHERE batch_id = ?",
            (sales_count, refunds_count, net, batch_id),
        )
        return batch_view(conn, batch_id)


@app.get("/closings", summary="关账批次列表")
def list_closings() -> dict:
    with db.read_conn() as conn:
        rows = conn.execute("SELECT * FROM batches ORDER BY batch_id").fetchall()
        return {
            "batches": [
                {
                    "batch_id": r["batch_id"],
                    "closed_at": r["closed_at"],
                    "sales_count": r["sales_count"],
                    "refunds_count": r["refunds_count"],
                    "total_royalty": yuan(r["total_royalty_cents"]),
                }
                for r in rows
            ]
        }


@app.get("/closings/{batch_id}", summary="查询关账报表（不可改快照）")
def get_closing(batch_id: int) -> dict:
    with db.read_conn() as conn:
        view = batch_view(conn, batch_id)
        if view is None:
            raise HTTPException(404, f"关账批次 {batch_id} 不存在")
        return view
