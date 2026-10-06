"""Constants for the Household Expenses (家庭支出) integration."""

DOMAIN = "household_expenses"
NAME = "家庭支出"

# ----------------------------------------------------------------------
# 配置键
# ----------------------------------------------------------------------
CONF_ITEMS = "items"

CONF_ITEM_ID = "id"
CONF_ITEM_NAME = "name"
CONF_ITEM_TYPE = "type"
# 实体 ID 用的 ASCII 标识（HA 的 entity_id 只允许 [a-z0-9_]，中文名会被判非法）
CONF_ITEM_OBJECT_ID = "object_id"

CONF_PERIODS = "periods"
CONF_PERIOD_START = "start"
CONF_PERIOD_END = "end"
CONF_PERIOD_AMOUNT = "amount"

CONF_LOAN_MONTHS = "loan_months"
CONF_UPFRONT = "upfront"
CONF_REMAINING = "remaining"
# 每月还款日（1-31；当月不足该日号时取月末）。
# 到达该日即视为完成一次还款，从「剩余金额」里扣掉当月月供。
CONF_REPAY_DAY = "repay_day"
# 「剩余金额」的基准日期：这笔余额是哪天查到的。
# 扣减只统计**严格晚于**该日的还款日，避免把已经还过的月份重复扣掉。
CONF_BALANCE_DATE = "balance_date"
CONF_AGENCY_FEE = "agency_fee"
CONF_CUSTODY_FEE = "custody_fee"
CONF_PAID_UNTIL = "paid_until"

# ----------------------------------------------------------------------
# 收支方向
# ----------------------------------------------------------------------
DIRECTION_EXPENSE = "支出"
DIRECTION_INCOME = "收入"

# ----------------------------------------------------------------------
# 计费方式（决定参数集合与摊销口径）
# ----------------------------------------------------------------------
KIND_LOAN = "loan"          # 贷款：期间×每月还款金额 + 贷款期间 + 首付款 + 剩余金额
KIND_RENT = "rent"          # 租赁/出租：期间×期间每月金额 + 中介费/保管费
KIND_PROPERTY = "property"  # 物业费：期间×期间每月金额 + 缴纳截止月份
KIND_HEATING = "heating"    # 取暖费：期间×区间总额，按天摊分

ITEM_TYPES: dict[str, dict] = {
    # 每个类型都带自己的 `amount_key` / `amount_label`，**不做统一**：
    # 表单字段名 = 前端取翻译用的 key（HA 的字段标签靠 `data.<字段名>` 查），
    # 所以「每月还款金额 / 每月租赁金额 / 每月出租金额 / 每月金额 / 期间总金额」
    # 必须各自占一个字段名，否则界面上所有类型都只会显示同一个标签。
    # amount_key 只用于表单，落库时统一映射回 CONF_PERIOD_AMOUNT（旧配置兼容）。
    "mortgage": {
        "name": "房贷",
        "kind": KIND_LOAN,
        "direction": DIRECTION_EXPENSE,
        "icon": "mdi:home-city",
        "amount_key": "amount_repay",
        "amount_label": "每月还款金额",
        "onetime": (CONF_UPFRONT, "首付款金额"),
        "has_remaining": True,
    },
    "car_loan": {
        "name": "车贷",
        "kind": KIND_LOAN,
        "direction": DIRECTION_EXPENSE,
        "icon": "mdi:car",
        "amount_key": "amount_repay",
        "amount_label": "每月还款金额",
        "onetime": (CONF_UPFRONT, "首付款金额"),
        "has_remaining": True,
    },
    "consumer_loan": {
        "name": "消费贷",
        "kind": KIND_LOAN,
        "direction": DIRECTION_EXPENSE,
        "icon": "mdi:cash-clock",
        "amount_key": "amount_repay",
        "amount_label": "每月还款金额",
        "onetime": (CONF_UPFRONT, "首付款金额"),
        "has_remaining": True,
    },
    "rent_home": {
        "name": "租赁房屋",
        "kind": KIND_RENT,
        "direction": DIRECTION_EXPENSE,
        "icon": "mdi:home-account",
        "amount_key": "amount_rent",
        "amount_label": "每月租赁金额",
        "onetime": (CONF_AGENCY_FEE, "中介费"),
        "has_remaining": False,
    },
    "sublet_home": {
        "name": "出租房屋",
        "kind": KIND_RENT,
        "direction": DIRECTION_INCOME,
        "icon": "mdi:home-export-outline",
        "amount_key": "amount_sublet",
        "amount_label": "每月出租金额",
        "onetime": (CONF_AGENCY_FEE, "中介费"),
        "has_remaining": False,
    },
    "rent_parking": {
        "name": "租赁车位",
        "kind": KIND_RENT,
        "direction": DIRECTION_EXPENSE,
        "icon": "mdi:parking",
        "amount_key": "amount_rent",
        "amount_label": "每月租赁金额",
        "onetime": (CONF_CUSTODY_FEE, "保管费"),
        "has_remaining": False,
    },
    "sublet_parking": {
        "name": "出租车位",
        "kind": KIND_RENT,
        "direction": DIRECTION_INCOME,
        "icon": "mdi:parking",
        "amount_key": "amount_sublet",
        "amount_label": "每月出租金额",
        "onetime": (CONF_CUSTODY_FEE, "保管费"),
        "has_remaining": False,
    },
    "property_fee": {
        "name": "物业费",
        "kind": KIND_PROPERTY,
        "direction": DIRECTION_EXPENSE,
        "icon": "mdi:office-building",
        "amount_key": "amount_property",
        "amount_label": "每月金额",
        "onetime": None,
        "has_remaining": True,
    },
    "heating_fee": {
        "name": "取暖费",
        "kind": KIND_HEATING,
        "direction": DIRECTION_EXPENSE,
        "icon": "mdi:radiator",
        "amount_key": "amount_heating",
        "amount_label": "期间总金额",
        "onetime": None,
        "has_remaining": False,
    },
}

DEFAULT_ICON = "mdi:wallet"

# 只出「每月 / 每年」明细、不出每日明细的类型。
# 这四类的每日金额由前端 UI 现场计算（月金额 ÷ 当月天数），后端不再吐 daylist
# —— 房贷日明细曾把实体属性撑到 ~470 KB，远超 Recorder 的 16384 字节上限。
NO_DAILY_TYPES = ("mortgage", "car_loan", "consumer_loan", "property_fee")

# ----------------------------------------------------------------------
# 实体属性 / 快照文件
# ----------------------------------------------------------------------
CACHE_FILENAME = f"{DOMAIN}.json"

ATTR_TYPE = "类型"
ATTR_REMAINING = "剩余金额"
ATTR_DATE = "date"
ATTR_DAYLIST = "daylist"
ATTR_MONTHLIST = "monthlist"
ATTR_YEARLIST = "yearlist"
ATTR_SOURCE = "数据源"
ATTR_LAST_SYNC = "最后同步日期"
ATTR_MONTH_TOTAL = "当月合计"
ATTR_YEAR_TOTAL = "当年合计"
ATTR_REPAY_DAY = "还款日"
ATTR_NEXT_REPAY = "下次还款日"

# 日 / 月 / 年明细**一律按「期间」全量生成**（见 calc.py 的 data_span()），
# 不存在「最近 N 天 / N 月 / N 年」的滚动窗口，所以这里没有窗口长度常量。
# NO_DAILY_TYPES 只决定是否生成 daylist，不影响覆盖区间。
