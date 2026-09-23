"""试剂库存批次分配（FEFO：失效日期优先，同日批号升序）。

给定待领用物种的 **有效摩尔需求**、核算日期与若干库存批次
（批号 / 纯度 / 可用名义摩尔数 / 失效日期 / 状态），按
**失效日期升序、同一失效日期按批号升序** 的顺序依次从各批次领用，
直到满足需求或可用批次耗尽：

* 每批的 **有效物质量 = 可用名义摩尔数 × 纯度**；领用按有效量计，
  同时换算回名义量从该批库存中扣减（``名义扣减 = 有效领用 ÷ 纯度``）；
* **已过期**（失效日期早于核算日期；核算日期当天到期仍可用）与
  **隔离**（``status = "quarantined"``）批次一律跳过，不参与分配，
  并在 ``skipped_batches`` 中给出跳过原因（同时满足两个条件时记为隔离）；
* 需求可 **跨批次拆分**：前一批耗尽后自动从下一批继续领用；
* 同一批次在一次分配中只被扣减一次（批号重复的请求由业务层拒绝），
  分配结果完全由请求体决定，服务不在请求间保留任何库存状态。

全程使用 :class:`fractions.Fraction` 精确计算（输入十进制字符串无损
转换），输出经 :func:`app.stoichiometry.fraction_to_decimal_string`
渲染为十进制字符串，规则与计量模块一致（无限小数 28 位有效数字
银行家舍入）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Iterable

from .stoichiometry import (
    decimal_to_fraction,
    fraction_to_decimal_string,
    parse_moles,
)

# 批次状态取值
STATUS_AVAILABLE = "available"
STATUS_QUARANTINED = "quarantined"

# 汇总状态取值
SUMMARY_FULFILLED = "fulfilled"      # 需求全部满足
SUMMARY_PARTIAL = "partial"          # 部分满足（0 < 已分配 < 需求）
SUMMARY_UNAVAILABLE = "unavailable"  # 完全无法分配


def parse_positive_moles(text: str) -> Decimal:
    """解析严格为正的有效摩尔需求（有限性/数量级校验复用 :func:`parse_moles`）。"""
    value = parse_moles(text)
    if value <= 0:
        raise ValueError("有效摩尔需求必须大于 0")
    return value


def parse_purity(text: str) -> Decimal:
    """解析纯度：区间 (0, 1] 的十进制字符串。"""
    value = parse_moles(text)
    if value <= 0 or value > 1:
        raise ValueError("纯度必须在 (0, 1] 区间内")
    return value


@dataclass(frozen=True)
class Batch:
    """一个已解析的库存批次（数量已无损转换为精确有理数）。"""

    batch_id: str
    purity: Fraction        # (0, 1]
    purity_text: str        # 客户端提交的纯度原文（回显用）
    nominal: Fraction       # 可用名义摩尔数（非负）
    expiry_date: date
    status: str             # STATUS_AVAILABLE / STATUS_QUARANTINED


def allocate_batches(
    required: Fraction,
    as_of: date,
    batches: Iterable[Batch],
) -> dict:
    """按 FEFO 顺序分配批次，返回可 JSON 序列化的结果字典。

    ``required`` 为严格为正的有效摩尔需求；``batches`` 中的每个批次
    至多被扣减一次。返回字典包含：

    * ``status``：``fulfilled`` / ``partial`` / ``unavailable``；
    * ``allocated_moles`` / ``unmet_moles``：已分配有效量合计与未满足需求；
    * ``allocations``：全部 **可领用** 批次按分配顺序的逐批明细
      （含未被动用、领用量为 0 的批次，便于核对每批余量）；
    * ``skipped_batches``：被跳过的过期 / 隔离批次及原因（按请求顺序）。
    """
    eligible: list[Batch] = []
    skipped: list[dict[str, str]] = []
    for batch in batches:
        if batch.status == STATUS_QUARANTINED:
            skipped.append({"batch_id": batch.batch_id, "reason": "quarantined"})
        elif batch.expiry_date < as_of:
            skipped.append({"batch_id": batch.batch_id, "reason": "expired"})
        else:
            eligible.append(batch)

    # FEFO：失效日期升序；同一失效日期按批号升序
    eligible.sort(key=lambda batch: (batch.expiry_date, batch.batch_id))

    remaining_need = required
    total_allocated = Fraction(0)
    lines: list[dict[str, str]] = []
    for batch in eligible:
        effective = batch.nominal * batch.purity
        take_effective = min(remaining_need, effective)
        take_nominal = take_effective / batch.purity  # purity > 0，精确除法
        remaining_need -= take_effective
        total_allocated += take_effective
        lines.append(
            {
                "batch_id": batch.batch_id,
                "purity": batch.purity_text,
                "expiry_date": batch.expiry_date.isoformat(),
                "allocated_effective_moles": fraction_to_decimal_string(take_effective),
                "allocated_nominal_moles": fraction_to_decimal_string(take_nominal),
                "remaining_nominal_moles": fraction_to_decimal_string(
                    batch.nominal - take_nominal
                ),
                "remaining_effective_moles": fraction_to_decimal_string(
                    effective - take_effective
                ),
            }
        )

    if remaining_need == 0:
        status = SUMMARY_FULFILLED
    elif total_allocated == 0:
        status = SUMMARY_UNAVAILABLE
    else:
        status = SUMMARY_PARTIAL

    return {
        "status": status,
        "allocated_moles": fraction_to_decimal_string(total_allocated),
        "unmet_moles": fraction_to_decimal_string(remaining_need),
        "allocations": lines,
        "skipped_batches": skipped,
    }
