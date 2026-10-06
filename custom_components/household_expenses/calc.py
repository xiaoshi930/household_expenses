"""纯计算逻辑：把费用项参数换算成 每日/每月/每年 金额。

口径（对应要求表）：

* **金额1（主项）** —— 期间内按期缴纳的经常性费用
  - 每月金额1 = 该月适用的「每月还款金额 / 期间每月金额」（整月落在期间内时取原值）
  - 每日金额1 = 每月金额1 / 当月天数
  - 取暖费例外：期间内录入的是「总金额」，按 总金额 / 期间天数 逐日摊分，再按月合计

* **金额2（一次性项）** —— 首付款 / 中介费 / 保管费 按摊销期间均摊
  - 贷款类：摊销期间 = 贷款期间（月），自首个期间起点起算
  - 其余：摊销期间 = 各期间本身
  - 每月金额2 = 一次性金额 x 该月落在摊销期间内的比例
  - 每日金额2 = 每月金额2 / 当月天数

* **剩余金额**
  - 贷款类（房贷/车贷/消费贷）：``0 - 当前欠款本金``，欠款以负数体现。
    当前欠款 = **录入的剩余金额 − 基准日之后已经发生过的各期月供**；
    「还款日」即每月固定扣款日号（1-31，当月不足取月末），每到一个还款日
    就扣掉当期月供。未配置还款日或基准日时退回静态余额。
  - 物业费：已交金额(至缴纳截止月份) - 应缴金额(至当前月)，未交为负
  - 其余：0

* **数据覆盖区间**
  - 九种类型**一律按「期间」全量生成** monthlist / yearlist：覆盖「首段起点 ~
    末段终点」的每一月 / 每一年（含尚未到来的计划期），降序（末月 / 末年在前）。
    期间没有结束日期时用「贷款期间（月）」推算，仍不可得就取今天。
  - ``daylist`` 同样按期间全量生成，但「无日明细」四类（房贷 / 车贷 / 消费贷 /
    物业费，见 ``NO_DAILY_TYPES``）恒为空数组 —— 它们的每日金额交给前端 UI
    现场计算（月金额 ÷ 当月天数）。
  - 期间为空（未配置）时三个列表都是空数组。
"""

from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from typing import Any

from .const import (
    ATTR_DAYLIST,
    ATTR_LAST_SYNC,
    ATTR_MONTH_TOTAL,
    ATTR_MONTHLIST,
    ATTR_NEXT_REPAY,
    ATTR_REMAINING,
    ATTR_REPAY_DAY,
    ATTR_SOURCE,
    ATTR_TYPE,
    ATTR_YEAR_TOTAL,
    ATTR_YEARLIST,
    CONF_BALANCE_DATE,
    CONF_ITEM_NAME,
    CONF_ITEM_TYPE,
    CONF_LOAN_MONTHS,
    CONF_PAID_UNTIL,
    CONF_PERIOD_AMOUNT,
    CONF_PERIOD_END,
    CONF_PERIOD_START,
    CONF_PERIODS,
    CONF_REMAINING,
    CONF_REPAY_DAY,
    DIRECTION_EXPENSE,
    ITEM_TYPES,
    KIND_HEATING,
    KIND_LOAN,
    KIND_PROPERTY,
    NO_DAILY_TYPES,
)

MAX_DATE = date(2999, 12, 31)
# 按期间生成时的硬顶：防止误填极远日期（如 2999 年）把实体属性撑到无法序列化
MAX_SPAN_DAYS = 366 * 60


# ----------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------
def parse_date(value: Any) -> date | None:
    """把配置里的日期（str / date / datetime）解析成 ``date``。"""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y%m%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 2].strip(), fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def parse_month(value: Any) -> tuple[int, int] | None:
    """解析 ``YYYY-MM`` 形式的月份，返回 ``(year, month)``。"""
    if value is None or value == "":
        return None
    if isinstance(value, (date, datetime)):
        return (value.year, value.month)
    text = str(value).strip().replace("/", "-")
    parts = text.split("-")
    if len(parts) < 2:
        return None
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not 1 <= month <= 12 or not 1900 <= year <= 2999:
        return None
    return (year, month)


def days_in_month(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def month_bounds(year: int, month: int) -> tuple[date, date]:
    return date(year, month, 1), date(year, month, days_in_month(year, month))


def add_months(value: date, months: int) -> date:
    """在 ``value`` 上增加 ``months`` 个月（保持日号，超出月末则取月末）。"""
    total = value.year * 12 + (value.month - 1) + months
    year, month = divmod(total, 12)
    month += 1
    day = min(value.day, days_in_month(year, month))
    return date(year, month, day)


def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    """月份平移 ``delta`` 个月。"""
    total = year * 12 + (month - 1) + delta
    new_year, new_month = divmod(total, 12)
    return new_year, new_month + 1


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _overlap_days(start: date, end: date, lo: date, hi: date) -> int:
    """``[start, end]`` 与 ``[lo, hi]`` 的重叠天数。"""
    left = max(start, lo)
    right = min(end, hi)
    if right < left:
        return 0
    return (right - left).days + 1


# ----------------------------------------------------------------------
# 参数解析
# ----------------------------------------------------------------------
def periods_of(item: dict[str, Any]) -> list[tuple[date, date, float]]:
    """返回 ``[(start, end, amount), ...]``，按开始日期排序并丢弃非法项。"""
    out: list[tuple[date, date, float]] = []
    for raw in item.get(CONF_PERIODS) or []:
        if not isinstance(raw, dict):
            continue
        start = parse_date(raw.get(CONF_PERIOD_START))
        if start is None:
            continue
        end = parse_date(raw.get(CONF_PERIOD_END)) or MAX_DATE
        if end < start:
            end = start
        out.append((start, end, _to_float(raw.get(CONF_PERIOD_AMOUNT))))
    out.sort(key=lambda row: row[0])
    return out


def kind_of(item: dict[str, Any]) -> str:
    return (ITEM_TYPES.get(str(item.get(CONF_ITEM_TYPE, ""))) or {}).get("kind", "")


def direction_of(item: dict[str, Any]) -> str:
    return (ITEM_TYPES.get(str(item.get(CONF_ITEM_TYPE, ""))) or {}).get(
        "direction", DIRECTION_EXPENSE
    )


def onetime_allocations(item: dict[str, Any]) -> list[tuple[date, date, float]]:
    """一次性金额的摊销区间 ``[(start, end, amount), ...]``。"""
    info = ITEM_TYPES.get(str(item.get(CONF_ITEM_TYPE, ""))) or {}
    onetime = info.get("onetime")
    if not onetime:
        return []
    key, _label = onetime
    amount = _to_float(item.get(key))
    if amount == 0:
        return []

    periods = periods_of(item)
    if not periods:
        return []

    if info.get("kind") == KIND_LOAN:
        months = int(_to_float(item.get(CONF_LOAN_MONTHS)))
        if months <= 0:
            return []
        start = periods[0][0]
        end = add_months(start, months) - timedelta(days=1)
        return [(start, end, amount)]

    return [(start, end, amount) for start, end, _amount in periods]


# ----------------------------------------------------------------------
# 贷款：还款日与自动扣减
# ----------------------------------------------------------------------
MAX_REPAY_STEPS = 1200  # 保护：最多向前推演 100 年


def repay_day_of(item: dict[str, Any]) -> int | None:
    """每月还款日（1-31）；未配置或非法时返回 ``None``。"""
    raw = item.get(CONF_REPAY_DAY)
    if raw is None or raw == "":
        return None
    try:
        day = int(float(raw))
    except (TypeError, ValueError):
        return None
    return day if 1 <= day <= 31 else None


def due_date_of_month(year: int, month: int, repay_day: int) -> date:
    """某月的还款日；当月天数不足时取月末（例如 31 号 → 2 月 28/29 号）。"""
    return date(year, month, min(repay_day, days_in_month(year, month)))


def period_amount_at(periods: list[tuple[date, date, float]], day: date) -> float:
    """覆盖 ``day`` 的期间金额之和。

    取期间里录入的**原值**（当期月供），不按天分摊 —— 还款日扣的是月供本身，
    不是日均值。
    """
    total = 0.0
    for start, end, amount in periods:
        if start <= day <= end:
            total += amount
    return round(total, 2)


def repaid_dates(
    repay_day: int, base: date, today: date, limit: int = MAX_REPAY_STEPS
) -> list[date]:
    """``(base, today]`` 之间已经发生过的还款日。

    基准日**当天不算**：余额就是那天查到的，当天的还款视为已含在余额里，
    否则用户填「今天」会被立刻多扣一期。
    """
    if today <= base:
        return []
    out: list[date] = []
    year, month = base.year, base.month
    while len(out) < limit:
        current = due_date_of_month(year, month, repay_day)
        if current > today:
            break
        if current > base:
            out.append(current)
        year, month = shift_month(year, month, 1)
    return out


def current_loan_balance(
    item: dict[str, Any], periods: list[tuple[date, date, float]], today: date
) -> float:
    """当前欠款本金 = 录入余额 − 基准日之后每期已还月供（扣到 0 为止）。

    未配置还款日 / 基准日（例如旧配置）时退回静态余额，不做任何扣减。
    """
    balance = _to_float(item.get(CONF_REMAINING))
    repay_day = repay_day_of(item)
    base = parse_date(item.get(CONF_BALANCE_DATE))
    if repay_day is None or base is None or balance <= 0:
        return round(balance, 2)

    left = balance
    for due in repaid_dates(repay_day, base, today):
        amount = period_amount_at(periods, due)
        if amount <= 0:
            continue
        left -= amount
        if left <= 0:
            return 0.0
    return round(left, 2)


def next_repay_date(
    item: dict[str, Any], periods: list[tuple[date, date, float]], today: date
) -> str:
    """下一个还款日（``YYYY-MM-DD``）；未配置或后续期间已走完时返回 ``""``。"""
    repay_day = repay_day_of(item)
    if repay_day is None:
        return ""
    year, month = today.year, today.month
    for _ in range(24):
        current = due_date_of_month(year, month, repay_day)
        if current >= today and period_amount_at(periods, current) > 0:
            return current.isoformat()
        year, month = shift_month(year, month, 1)
    return ""


def item_type_key(item: dict[str, Any]) -> str:
    """费用类型标识（mortgage / car_loan / consumer_loan / property_fee ...）。"""
    return str(item.get(CONF_ITEM_TYPE, ""))


def has_daily_breakdown(item: dict[str, Any]) -> bool:
    """是否生成日明细 ``daylist``。

    房贷 / 车贷 / 消费贷 / 物业费 返回 ``False`` —— 这四类只出月 / 年明细，
    每日金额由前端 UI 现场算（月金额 ÷ 当月天数）。
    """
    return item_type_key(item) not in NO_DAILY_TYPES


def data_span(
    item: dict[str, Any], periods: list[tuple[date, date, float]], today: date
) -> tuple[date, date] | None:
    """按月 / 年全量生成时的覆盖区间 ``(首日, 末日)``。

    九种类型共用（``NO_DAILY_TYPES`` 只决定要不要日明细，不影响本区间）：
    取「首段期间起点 ~ 末段期间终点」。末段没有结束日期（长期）时，退回
    「贷款期间（月）」推算；仍不可得（例如物业费不填结束日期）就取今天。
    最后加一道硬顶，避免误填 2999 年之类的日期把实体属性撑到无法序列化。
    """
    if not periods:
        return None
    start = periods[0][0]
    end = max(row[1] for row in periods)
    if end >= MAX_DATE:
        months = int(_to_float(item.get(CONF_LOAN_MONTHS)))
        end = (
            add_months(start, months) - timedelta(days=1)
            if months > 0
            else max(start, today)
        )
    if end < start:
        end = start
    if (end - start).days >= MAX_SPAN_DAYS:
        end = start + timedelta(days=MAX_SPAN_DAYS - 1)
    return start, end


# ----------------------------------------------------------------------
# 金额计算
# ----------------------------------------------------------------------
def monthly_primary(periods: list[tuple[date, date, float]], year: int, month: int, kind: str) -> float:
    """某月的「金额1」。"""
    first, last = month_bounds(year, month)
    dim = days_in_month(year, month)
    total = 0.0
    for start, end, amount in periods:
        covered = _overlap_days(start, end, first, last)
        if covered <= 0:
            continue
        if kind == KIND_HEATING:
            span = (end - start).days + 1
            total += amount * covered / span
        else:
            total += amount * covered / dim
    return round(total, 2)


def daily_primary(periods: list[tuple[date, date, float]], day: date, kind: str) -> float:
    """某天的「金额1」。"""
    dim = days_in_month(day.year, day.month)
    total = 0.0
    for start, end, amount in periods:
        if not start <= day <= end:
            continue
        if kind == KIND_HEATING:
            span = (end - start).days + 1
            total += amount / span
        else:
            total += amount / dim
    return round(total, 2)


def monthly_secondary(allocs: list[tuple[date, date, float]], year: int, month: int) -> float:
    """某月的「金额2」。"""
    first, last = month_bounds(year, month)
    total = 0.0
    for start, end, amount in allocs:
        span = (end - start).days + 1
        covered = _overlap_days(start, end, first, last)
        if span <= 0 or covered <= 0:
            continue
        total += amount * covered / span
    return round(total, 2)


def daily_secondary(allocs: list[tuple[date, date, float]], day: date) -> float:
    """某天的「金额2」。

    一次性金额在摊销期间内逐日均摊（``金额 / 摊销天数``），
    保证「Σ 每日 == Σ 每月 == 一次性金额」。
    """
    total = 0.0
    for start, end, amount in allocs:
        if not start <= day <= end:
            continue
        span = (end - start).days + 1
        if span <= 0:
            continue
        total += amount / span
    return round(total, 2)


def _sum_months(
    periods: list[tuple[date, date, float]],
    start_ym: tuple[int, int],
    end_ym: tuple[int, int],
    kind: str,
) -> float:
    """区间 ``[start_ym, end_ym]``（含首尾）内按月合计「金额1」。"""
    if end_ym < start_ym:
        return 0.0
    total = 0.0
    year, month = start_ym
    while (year, month) <= end_ym:
        total += monthly_primary(periods, year, month, kind)
        month += 1
        if month > 12:
            month = 1
            year += 1
    return round(total, 2)


def remaining_value(item: dict[str, Any], periods: list[tuple[date, date, float]], today: date) -> float:
    """「剩余金额」属性。"""
    kind = kind_of(item)
    if kind == KIND_LOAN:
        # 欠款以负数体现；余额随每次还款日自动递减
        return round(0.0 - current_loan_balance(item, periods, today), 2)
    if kind == KIND_PROPERTY:
        if not periods:
            return 0.0
        start_ym = (periods[0][0].year, periods[0][0].month)
        current_ym = (today.year, today.month)
        paid_until = parse_month(item.get(CONF_PAID_UNTIL)) or current_ym
        paid = _sum_months(periods, start_ym, paid_until, kind)
        due = _sum_months(periods, start_ym, current_ym, kind)
        return round(paid - due, 2)
    return 0.0


# ----------------------------------------------------------------------
# 快照
# ----------------------------------------------------------------------
def _day_row(
    periods: list[tuple[date, date, float]],
    allocs: list[tuple[date, date, float]],
    day: date,
    kind: str,
) -> dict[str, Any]:
    return {
        "day": day.isoformat(),
        "dayCost1": daily_primary(periods, day, kind),
        "dayCost2": daily_secondary(allocs, day),
    }


def _month_row(
    periods: list[tuple[date, date, float]],
    allocs: list[tuple[date, date, float]],
    year: int,
    month: int,
    kind: str,
) -> dict[str, Any]:
    return {
        "month": f"{year:04d}-{month:02d}",
        "monthCost1": monthly_primary(periods, year, month, kind),
        "monthCost2": monthly_secondary(allocs, year, month),
    }


def _year_row(
    periods: list[tuple[date, date, float]],
    allocs: list[tuple[date, date, float]],
    year: int,
    kind: str,
) -> dict[str, Any]:
    total1 = round(sum(monthly_primary(periods, year, m, kind) for m in range(1, 13)), 2)
    total2 = round(sum(monthly_secondary(allocs, year, m) for m in range(1, 13)), 2)
    return {"year": str(year), "yearCost1": total1, "yearCost2": total2}


def build_item_snapshot(item: dict[str, Any], today: date, now_text: str) -> dict[str, Any]:
    """生成单个费用项的实体属性快照（结构参照 实体属性结构.txt）。

    九种类型**一律按「期间」全量生成**（区间 = ``data_span()``，降序，含尚未
    到来的计划期）：

    * 房贷 / 车贷 / 消费贷 / 物业费（``NO_DAILY_TYPES``）：只出 ``monthlist`` /
      ``yearlist``，``daylist`` 恒为空数组（每日金额由前端现场算）；
    * 其余类型（租赁 / 出租 / 车位 / 取暖费）：``daylist`` / ``monthlist`` /
      ``yearlist`` 全部按期间生成（含中介费 / 保管费摊销、取暖费按天摊分）。

    期间为空（未配置）时三个列表都是空数组。
    """
    kind = kind_of(item)
    periods = periods_of(item)
    allocs = onetime_allocations(item)
    with_daily = has_daily_breakdown(item)
    span = data_span(item, periods, today)

    daylist: list[dict[str, Any]] = []
    monthlist: list[dict[str, Any]] = []
    yearlist: list[dict[str, Any]] = []

    if span is not None:
        # ---- 日 ----（降序，末日在前；「无日明细」四类跳过，daylist 留空数组）
        if with_daily:
            cursor = span[1]
            while cursor >= span[0]:
                daylist.append(_day_row(periods, allocs, cursor, kind))
                cursor -= timedelta(days=1)

        # ---- 月 ----（降序，末月在前）
        year, month = span[1].year, span[1].month
        floor = (span[0].year, span[0].month)
        while (year, month) >= floor:
            monthlist.append(_month_row(periods, allocs, year, month, kind))
            year, month = shift_month(year, month, -1)

        # ---- 年 ----（降序，末年在前）
        for target in range(span[1].year, span[0].year - 1, -1):
            yearlist.append(_year_row(periods, allocs, target, kind))

    # 「当月合计 / 当年合计」独立按当前自然月 / 年算 —— 不能再取列表首项：
    # 列表是按期间全量生成的，首项已经是期末（未来）那一期，取首项会串月。
    month_total = round(
        monthly_primary(periods, today.year, today.month, kind)
        + monthly_secondary(allocs, today.year, today.month),
        2,
    )
    year1 = round(
        sum(monthly_primary(periods, today.year, m, kind) for m in range(1, 13)), 2
    )
    year2 = round(sum(monthly_secondary(allocs, today.year, m) for m in range(1, 13)), 2)

    return {
        ATTR_TYPE: direction_of(item),
        ATTR_REMAINING: remaining_value(item, periods, today),
        ATTR_DAYLIST: daylist,
        ATTR_MONTHLIST: monthlist,
        ATTR_YEARLIST: yearlist,
        ATTR_SOURCE: "",
        ATTR_LAST_SYNC: now_text,
        ATTR_MONTH_TOTAL: month_total,
        ATTR_YEAR_TOTAL: round(year1 + year2, 2),
        # 贷款专属：每月还款日 / 下次还款日（其余类型为空）
        ATTR_REPAY_DAY: repay_day_of(item) if kind == KIND_LOAN else None,
        ATTR_NEXT_REPAY: (
            next_repay_date(item, periods, today) if kind == KIND_LOAN else ""
        ),
    }


def build_snapshot(items: list[dict[str, Any]], today: date, now_text: str) -> dict[str, Any]:
    """生成整个集成的运行时快照。"""
    entries: dict[str, Any] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get(CONF_ITEM_NAME) or "未命名")
        snapshot = build_item_snapshot(item, today, now_text)
        snapshot["date"] = now_text
        entries[name] = snapshot
    return {
        "date": now_text,
        ATTR_LAST_SYNC: now_text,
        ATTR_SOURCE: "",
        "items": entries,
    }
