"""版税计提、作者分账与退款冲回的核心计算（纯函数，金额一律用整数分）。"""
from __future__ import annotations

from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal


def per_copy_royalty_cents(price_cents: int, rate: Decimal) -> int:
    """每册版税 = 每册净价 × 费率，四舍五入到分。"""
    cents = (Decimal(price_cents) * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def compute_segments(tiers: list[dict], base_copies: int, copies: int, price_cents: int) -> list[dict]:
    """按入账前累计销量 base_copies 把本笔 copies 册切分到各阶梯，跨档分档计提。

    tiers 按 threshold 升序，首档 threshold 必须为 0，末档上不封顶。
    返回段列表，每段记录覆盖的累计册次区间 [start_no, end_no]（1 起算，含端点）。
    """
    segments = []
    for i, tier in enumerate(tiers):
        lo = max(base_copies, tier["threshold"])
        hi = base_copies + copies
        if i + 1 < len(tiers):
            hi = min(hi, tiers[i + 1]["threshold"])
        if hi > lo:
            pc = per_copy_royalty_cents(price_cents, tier["rate"])
            segments.append(
                {
                    "tier_index": i,
                    "threshold": tier["threshold"],
                    "rate": tier["rate"],
                    "copies": hi - lo,
                    "per_copy_cents": pc,
                    "start_no": lo + 1,
                    "end_no": hi,
                    "amount_cents": (hi - lo) * pc,
                }
            )
    return segments


def split_cents(total_cents: int, authors: list[dict]) -> list[dict]:
    """作者分账：先按比例向下取整到分，余分按作者编号顺序每人补 1 分。

    authors: [{"author_id": str, "ratio": Decimal}]，ratio 为百分数且合计 100。
    余分总数必小于作者人数（每人舍去不足 1 分），按 author_id 升序依次补齐。
    """
    ordered = sorted(authors, key=lambda a: a["author_id"])
    shares: list[list] = []
    for a in ordered:
        exact = Decimal(total_cents) * a["ratio"] / Decimal(100)
        floored = int(exact.to_integral_value(rounding=ROUND_FLOOR))
        shares.append([a["author_id"], floored])
    remainder = total_cents - sum(amt for _, amt in shares)
    for i in range(remainder):
        shares[i][1] += 1
    return [{"author_id": aid, "amount_cents": amt} for aid, amt in shares]


def refund_segments(segments: list[dict], already_refunded: int, refund_copies: int) -> list[dict]:
    """退款从原销售最后一册起冲回：跳过已退的 already_refunded 册，再取 refund_copies 册。

    按原销售分段（segments 升序）从末尾反向截取，返回正向排列的冲回段（金额为正，
    入库/展示时由调用方取负）。不回退累计销量，段归属沿用原销售入账时的口径。
    """
    skip = already_refunded
    take = refund_copies
    out: list[dict] = []
    for seg in reversed(segments):
        if take == 0:
            break
        available = seg["copies"]
        skipped = min(skip, available)
        skip -= skipped
        taken = min(take, available - skipped)
        take -= taken
        if taken > 0:
            out.append({**seg, "copies": taken, "amount_cents": taken * seg["per_copy_cents"]})
    out.reverse()
    return out
