"""Config flow / Options flow。

* 首次添加：**无需录入任何内容**，直接创建配置项。
* 修改集成：通过「选项」实现费用项的 **增加 / 修改 / 删除**。
"""

from __future__ import annotations

import copy
import logging
import re
from datetime import date, datetime
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, FlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BALANCE_DATE,
    CONF_HEATING_REMAINING,
    CONF_ITEM_ID,
    CONF_ITEM_NAME,
    CONF_ITEM_OBJECT_ID,
    CONF_ITEM_TYPE,
    CONF_ITEMS,
    CONF_LOAN_MONTHS,
    CONF_PAID_UNTIL,
    CONF_PERIOD_AMOUNT,
    CONF_PERIOD_END,
    CONF_PERIOD_START,
    CONF_PERIODS,
    CONF_REMAINING,
    CONF_REPAY_DAY,
    CONF_UPFRONT,
    DOMAIN,
    ITEM_TYPES,
    KIND_HEATING,
    KIND_LOAN,
    KIND_PROPERTY,
    NAME,
)

_LOGGER = logging.getLogger(__name__)

SELECT_INDEX = "index"
# 只在表单里用，不落库：勾上表示「再录入一段期间」
FIELD_MORE = "more"

STEP_INIT = "init"
STEP_ADD = "add"
STEP_ADD_NAME = "add_name"
STEP_EDIT = "edit"
STEP_EDIT_MENU = "edit_menu"
STEP_EDIT_NAME = "edit_name"
STEP_PERIODS = "periods"
STEP_PERIOD_ADD = "period_add"
STEP_PERIOD_EDIT = "period_edit"
STEP_PERIOD_EDIT_FORM = "period_edit_form"
STEP_PERIOD_DEL = "period_del"
STEP_EXTRA = "extra"
STEP_DELETE = "delete"
STEP_DELETE_CONFIRM = "delete_confirm"
STEP_DONE = "done"
STEP_BACK = "back"

ERROR_END_BEFORE_START = "end_before_start"
ERROR_NAME_EXISTS = "name_exists"
ERROR_INVALID_MONTH = "invalid_month"
ERROR_LAST_PERIOD = "last_period"
ERROR_DATE_REQUIRED = "date_required"
ERROR_INVALID_DATE = "invalid_date"


# ----------------------------------------------------------------------
# 选择器 / 表单小工具
# ----------------------------------------------------------------------
def _parse_date_text(text: str) -> str | None:
    """宽松解析日期文本，成功返回 ``YYYY-MM-DD``，失败返回 ``None``。

    接受 ``2026-01-31`` / ``2026/1/31`` / ``2026.1.31`` / ``2026年1月31日`` /
    ``20260131`` 等写法（8 位紧凑写法要求月份、日期两位）。
    """
    m = re.fullmatch(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})\D*", text) or re.fullmatch(
        r"(\d{4})(\d{2})(\d{2})", text
    )
    if not m:
        return None
    year, month, day = (int(g) for g in m.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


class _DateSelector(selector.TextSelector):
    """可**手工输入**的日期控件（原生 ``type="date"``），允许留空。

    HA 2025.10 起自带的 ``DateSelector`` 前端是「只读输入框 + cally 日历
    弹窗」：输入框不收键盘，弹窗里也只有左右翻月箭头（日历图标是「跳到
    今天」），低版本「点年份快速选年」的界面没有了。

    这里换成 ``TextSelector(type="date")``：前端渲染成原生
    ``<input type="date">`` —— 点击年/月/日分段即可直接敲数字，桌面
    Chrome/Edge 与 HA App 自带的日历选择器都支持快速跳年。

    ``__call__`` 在父类的 ``str()`` 强转**之前**做归一化（父类会把 ``None``
    变成字符串 ``"None"`` 放行）：
    - ``None`` / 空白串 → ``""``（留空 = 未填 / 长期，由流程自己决定是否报错）；
    - ``date`` / ``datetime``（YAML 导入等路径）→ ``YYYY-MM-DD``；
    - 其余按 :func:`_parse_date_text` 宽松解析并归一成 ``YYYY-MM-DD``，
      解析不了 → ``invalid_date``（翻译键，前端显示中文提示）。

    序列化行为与父类 TextSelector 完全一致，voluptuous_serialize 不受影响。
    """

    def __init__(self) -> None:
        super().__init__(
            selector.TextSelectorConfig(type=selector.TextSelectorType.DATE)
        )

    def __call__(self, data: Any) -> Any:
        if data is None:
            return ""
        if isinstance(data, datetime):  # datetime 是 date 的子类，必须先判
            return data.strftime("%Y-%m-%d")
        if isinstance(data, date):
            return data.isoformat()
        if not isinstance(data, str):
            raise vol.Invalid(ERROR_INVALID_DATE)
        text = data.strip()
        if not text:
            return ""
        parsed = _parse_date_text(text)
        if parsed is None:
            raise vol.Invalid(ERROR_INVALID_DATE)
        return parsed


class _OptionalMoneySelector(selector.NumberSelector):
    """可留空的金额控件（留空 = 未填）。

    父类的数字校验会把空串 / ``None`` 判成非法（用户清空输入框就提交不了），
    这里在调用父类之前把两者统一归一成 ``None``。序列化行为与父类一致。
    """

    def __init__(self) -> None:
        super().__init__(
            selector.NumberSelectorConfig(
                min=0,
                step=0.01,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="元",
            )
        )

    def __call__(self, data: Any) -> Any:
        if data is None:
            return None
        if isinstance(data, str) and not data.strip():
            return None
        return super().__call__(data)


def _type_selector() -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=key, label=info["name"])
                for key, info in ITEM_TYPES.items()
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _money_selector(unit: str = "元") -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0,
            step=0.01,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement=unit,
        )
    )


def _index_selector(labels: list[str]) -> selector.SelectSelector:
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=str(idx), label=label)
                for idx, label in enumerate(labels)
            ],
            mode=selector.SelectSelectorMode.LIST,
        )
    )


def _item_label(item: dict[str, Any]) -> str:
    type_name = (ITEM_TYPES.get(str(item.get(CONF_ITEM_TYPE, ""))) or {}).get("name", "未知")
    return f"{item.get(CONF_ITEM_NAME) or '未命名'}（{type_name}）"


def _amount_field(item_type: str | None) -> tuple[str, str]:
    """期间表单里「金额」字段的**字段名 + 界面名称**（按类型区分，不做统一）。

    HA 的字段标签是按 ``data.<字段名>`` 查翻译的（字段名本身就是翻译 key），
    所以要想「房贷 = 每月还款金额」「租赁房屋 = 每月租赁金额」「取暖费 = 期间总金额」，
    就必须给它们各自一个字段名。字段名只在表单层用，落库时一律映射回
    ``CONF_PERIOD_AMOUNT``，对外的数据口径不变。
    """
    info = ITEM_TYPES.get(str(item_type or "")) or {}
    return (
        str(info.get("amount_key") or CONF_PERIOD_AMOUNT),
        str(info.get("amount_label") or "金额"),
    )


def _period_label(period: dict[str, Any], item_type: str | None = None) -> str:
    """期间在菜单里的回显，带上该类型自己的金额名称。"""
    start = _norm_date(period.get(CONF_PERIOD_START)) or "未填"
    end = _norm_date(period.get(CONF_PERIOD_END)) or "长期"
    _key, label = _amount_field(item_type)
    return f"{start} ~ {end}  {label} {period.get(CONF_PERIOD_AMOUNT, 0)} 元"


def _norm_date(value: Any) -> str:
    """把 selector 原样返回的 date / datetime 规范成 ``YYYY-MM-DD`` 字符串。"""
    if isinstance(value, datetime):  # datetime 是 date 的子类，必须先判
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "").strip()


def _as_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _valid_month(value: str) -> bool:
    parts = value.split("-")
    if len(parts) != 2:
        return False
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    return 1900 <= year <= 2999 and 1 <= month <= 12


def _today() -> str:
    """今天（``YYYY-MM-DD``），用作剩余金额基准日的默认值。"""
    return dt_util.now().date().isoformat()


def _repay_day_selector() -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=1,
            max=31,
            step=1,
            mode=selector.NumberSelectorMode.BOX,
            unit_of_measurement="号",
        )
    )


def _period_schema(
    defaults: dict[str, Any] | None = None, item_type: str | None = None
) -> vol.Schema:
    """期间表单：开始日期 + 结束日期（可空 = 长期）+ 金额 + 「再录一段」。

    金额字段的名称随费用类型变化（见 ``_amount_field``），不做统一叫法。
    没有单独的「期间设置完成」入口 —— 录完这一段，勾上「再录一段」就继续，
    不勾就直接进入下一步参数。
    """
    defaults = defaults or {}
    amount_key = _amount_field(item_type)[0]
    fields: dict[Any, Any] = {
        vol.Required(
            CONF_PERIOD_START,
            description={
                "suggested_value": _norm_date(defaults.get(CONF_PERIOD_START)) or None
            },
        ): _DateSelector(),
        # 结束日期可以为空（= 长期）。
        # 不能写 vol.Any(selector.DateSelector(), vol.In(["", None])) ——
        # voluptuous_serialize 只认「vol.Any(None, X)」（即 vol.Maybe）这一种
        # 形式，其余 Any 会在把表单推给前端时直接抛
        # ValueError: Unable to convert schema，表单根本渲染不出来。
        # 「留空」的容错放在 _DateSelector 里，而不是靠 vol.In([""])。
        vol.Optional(
            CONF_PERIOD_END,
            description={
                "suggested_value": _norm_date(defaults.get(CONF_PERIOD_END)) or None
            },
        ): vol.Maybe(_DateSelector()),
        vol.Required(
            amount_key,
            description={"suggested_value": _as_float(defaults.get(CONF_PERIOD_AMOUNT))},
        ): _money_selector(),
    }
    # 修改既有期间时不需要「再录一段」
    if defaults.get(CONF_PERIOD_START):
        return vol.Schema(fields)
    fields[vol.Optional(FIELD_MORE, default=False)] = selector.BooleanSelector()
    return vol.Schema(fields)


def _extra_schema(item_type: str, item: dict[str, Any] | None = None) -> vol.Schema | None:
    """按费用类型生成「其他参数」表单；该类型没有额外参数时返回 ``None``。"""
    info = ITEM_TYPES.get(item_type)
    if not info:
        return None
    item = item or {}
    kind = info["kind"]
    fields: dict[Any, Any] = {}

    if kind == KIND_LOAN:
        # 字段顺序对齐参数表：首付款金额 → 贷款期间 → 剩余金额 → 还款日
        fields[
            vol.Required(
                CONF_UPFRONT,
                description={"suggested_value": _as_float(item.get(CONF_UPFRONT))},
            )
        ] = _money_selector()
        months = int(_as_float(item.get(CONF_LOAN_MONTHS))) or 1
        fields[
            vol.Required(CONF_LOAN_MONTHS, description={"suggested_value": months})
        ] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1,
                max=1200,
                step=1,
                mode=selector.NumberSelectorMode.BOX,
                unit_of_measurement="个月",
            )
        )
        fields[
            vol.Required(
                CONF_REMAINING,
                description={"suggested_value": _as_float(item.get(CONF_REMAINING))},
            )
        ] = _money_selector()
        # 还款日 + 剩余金额日期：到达还款日后自动扣减剩余金额
        fields[
            vol.Required(
                CONF_REPAY_DAY,
                description={
                    "suggested_value": int(_as_float(item.get(CONF_REPAY_DAY))) or 1
                },
            )
        ] = _repay_day_selector()
        fields[
            vol.Required(
                CONF_BALANCE_DATE,
                description={
                    "suggested_value": _norm_date(item.get(CONF_BALANCE_DATE))
                    or _today()
                },
            )
        ] = _DateSelector()
    else:
        onetime = info.get("onetime")
        if onetime:
            key, _label = onetime
            fields[
                vol.Required(key, description={"suggested_value": _as_float(item.get(key))})
            ] = _money_selector()
        elif kind == KIND_PROPERTY:
            fields[
                vol.Optional(
                    CONF_PAID_UNTIL,
                    description={
                        "suggested_value": str(item.get(CONF_PAID_UNTIL) or "") or None
                    },
                )
            ] = selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            )
        elif kind == KIND_HEATING:
            # 「剩余金额」：录多少，实体上的剩余金额就是多少，不做任何换算。
            # 可留空（留空 = 0）。
            remaining = item.get(CONF_HEATING_REMAINING)
            fields[
                vol.Optional(
                    CONF_HEATING_REMAINING,
                    description={
                        "suggested_value": (
                            None if remaining in (None, "") else _as_float(remaining)
                        )
                    },
                )
            ] = _OptionalMoneySelector()

    if not fields:
        return None
    return vol.Schema(fields)


def _text_selector(suggested: str) -> selector.TextSelector:
    return selector.TextSelector(
        selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
    )


# ----------------------------------------------------------------------
# 初始配置流
# ----------------------------------------------------------------------
class HouseholdExpensesConfigFlow(ConfigFlow, domain=DOMAIN):
    """首次添加：直接完成，不需要录入任何内容。"""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """无需表单，直接创建配置项。"""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(title=NAME, data={})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return HouseholdExpensesOptionsFlow()


# ----------------------------------------------------------------------
# 选项流（增 / 改 / 删）
# ----------------------------------------------------------------------
class HouseholdExpensesOptionsFlow(OptionsFlow):
    """家庭支出费用项管理。"""

    _items: list[dict[str, Any]] | None = None
    _draft: dict[str, Any] | None = None
    _edit_index: int | None = None
    _period_index: int | None = None
    _delete_index: int | None = None
    _loaded: bool = False

    # ---------------- 内部工具 ----------------
    def _ensure_loaded(self) -> None:
        if not self._loaded:
            raw = self.config_entry.options.get(CONF_ITEMS) or []
            self._items = [copy.deepcopy(row) for row in raw if isinstance(row, dict)]
            self._loaded = True

    @property
    def _rows(self) -> list[dict[str, Any]]:
        # 注意：不能写 `self._items or []` —— 空列表是 falsy，会返回一个新列表，
        # 后续的 append / pop 全部作用在临时对象上，改动静默丢失。
        if self._items is None:
            self._items = []
        return self._items

    @property
    def _draft_type(self) -> str:
        """当前草稿的费用类型（决定期间表单里金额字段叫什么）。"""
        return str((self._draft or {}).get(CONF_ITEM_TYPE) or "")

    @property
    def _period_rows(self) -> list[dict[str, Any]]:
        if self._draft is None:
            return []
        return self._draft.setdefault(CONF_PERIODS, [])

    def _name_taken(self, name: str, exclude_index: int | None = None) -> bool:
        for idx, row in enumerate(self._rows):
            if exclude_index is not None and idx == exclude_index:
                continue
            if str(row.get(CONF_ITEM_NAME) or "") == name:
                return True
        return False

    def _default_name(self, item_type: str) -> str:
        base = (ITEM_TYPES.get(item_type) or {}).get("name", "费用")
        if not self._name_taken(base):
            return base
        suffix = 2
        while self._name_taken(f"{base}{suffix}"):
            suffix += 1
        return f"{base}{suffix}"

    def _object_id(self, item_type: str) -> str:
        """生成一个 ASCII 且唯一的实体 ID 标识（HA 的 entity_id 不接受中文）。"""
        base = "".join(ch if ch.isalnum() else "_" for ch in item_type.lower()).strip("_")
        base = base or "item"
        taken = {str(row.get(CONF_ITEM_OBJECT_ID) or "") for row in self._rows}
        if base not in taken:
            return base
        suffix = 2
        while f"{base}_{suffix}" in taken:
            suffix += 1
        return f"{base}_{suffix}"

    def _commit_draft(self) -> None:
        """把草稿写回列表。

        按**引用**写回（列表里存的就是同一个 dict），所以之后对 `_draft` 的
        任何改动都等于改列表里那一项 —— 不需要再有「保存」这个步骤。
        """
        draft = self._draft or {}
        if self._edit_index is not None and 0 <= self._edit_index < len(self._rows):
            self._rows[self._edit_index] = draft
        else:
            self._rows.append(draft)
        self._period_index = None

    def _commit_if_editing(self) -> None:
        """编辑模式下每次回到菜单都把草稿写回（没有独立的「保存」入口）。"""
        if self._edit_index is not None and self._draft is not None:
            self._commit_draft()

    # ---------------- 主菜单 ----------------
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        menu_options = [STEP_ADD]
        if self._rows:
            menu_options += [STEP_EDIT, STEP_DELETE]
        menu_options.append(STEP_DONE)
        return self.async_show_menu(step_id=STEP_INIT, menu_options=menu_options)

    async def async_step_done(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        return self.async_create_entry(
            title="",
            data={**self.config_entry.options, CONF_ITEMS: self._rows},
        )

    # ---------------- 新增 ----------------
    async def async_step_add(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        if user_input is not None:
            self._draft = {CONF_ITEM_TYPE: str(user_input[CONF_ITEM_TYPE])}
            self._edit_index = None
            return await self.async_step_add_name()
        self._draft = None
        self._edit_index = None
        return self.async_show_form(
            step_id=STEP_ADD,
            data_schema=vol.Schema({vol.Required(CONF_ITEM_TYPE): _type_selector()}),
        )

    async def async_step_add_name(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        item_type = str((self._draft or {}).get(CONF_ITEM_TYPE) or "")
        if not item_type:
            return await self.async_step_add()

        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_ITEM_NAME) or "").strip()
            if not name:
                name = self._default_name(item_type)
            if self._name_taken(name):
                errors[CONF_ITEM_NAME] = ERROR_NAME_EXISTS
            else:
                self._draft = {
                    CONF_ITEM_ID: uuid4().hex,
                    CONF_ITEM_OBJECT_ID: self._object_id(item_type),
                    CONF_ITEM_TYPE: item_type,
                    CONF_ITEM_NAME: name,
                    CONF_PERIODS: [],
                }
                # 新增流程没有「期间设置完成」这种菜单：直接接着录期间表单
                return await self.async_step_period_add()

        return self.async_show_form(
            step_id=STEP_ADD_NAME,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ITEM_NAME,
                        description={"suggested_value": self._default_name(item_type)},
                    ): _text_selector("")
                }
            ),
            errors=errors,
        )

    # ---------------- 修改 ----------------
    async def async_step_edit(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        if not self._rows:
            return await self.async_step_init()
        if user_input is not None:
            index = int(user_input[SELECT_INDEX])
            self._edit_index = index
            self._draft = copy.deepcopy(self._rows[index])
            return await self.async_step_edit_menu()
        return self.async_show_form(
            step_id=STEP_EDIT,
            data_schema=vol.Schema(
                {
                    vol.Required(SELECT_INDEX): _index_selector(
                        [_item_label(it) for it in self._rows]
                    )
                }
            ),
        )

    async def async_step_edit_menu(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        self._commit_if_editing()
        if self._draft is None:
            return await self.async_step_init()
        return self.async_show_menu(
            step_id=STEP_EDIT_MENU,
            menu_options=[STEP_EDIT_NAME, STEP_PERIODS, STEP_EXTRA, STEP_BACK],
        )

    async def async_step_back(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """返回主菜单（编辑中的改动已经在每次回到菜单时写回，无需「保存」）。"""
        self._ensure_loaded()
        self._commit_if_editing()
        self._draft = None
        self._edit_index = None
        self._period_index = None
        return await self.async_step_init()

    async def async_step_edit_name(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        draft = self._draft or {}
        item_type = str(draft.get(CONF_ITEM_TYPE) or "")
        errors: dict[str, str] = {}
        if user_input is not None:
            name = str(user_input.get(CONF_ITEM_NAME) or "").strip()
            if not name:
                name = str(draft.get(CONF_ITEM_NAME) or self._default_name(item_type))
            if self._name_taken(name, exclude_index=self._edit_index):
                errors[CONF_ITEM_NAME] = ERROR_NAME_EXISTS
            else:
                draft[CONF_ITEM_NAME] = name
                self._draft = draft
                return await self.async_step_edit_menu()
        return self.async_show_form(
            step_id=STEP_EDIT_NAME,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ITEM_NAME,
                        description={
                            "suggested_value": str(draft.get(CONF_ITEM_NAME) or "")
                        },
                    ): _text_selector("")
                }
            ),
            errors=errors,
        )

    # ---------------- 期间管理 ----------------
    async def async_step_periods(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """期间列表 —— 只有「修改」流程才需要菜单，新增流程直接连着录表单。"""
        self._ensure_loaded()
        self._commit_if_editing()
        if self._draft is None:
            return await self.async_step_init()
        menu_options = [STEP_PERIOD_ADD]
        if self._period_rows:
            menu_options += [STEP_PERIOD_EDIT, STEP_PERIOD_DEL]
        menu_options.append(STEP_BACK)
        return self.async_show_menu(step_id=STEP_PERIODS, menu_options=menu_options)

    async def async_step_period_add(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """录一段期间：勾「再录一段」继续录，不勾就进入下一步参数。"""
        self._ensure_loaded()
        if self._draft is None:
            return await self.async_step_init()
        item_type = str(self._draft.get(CONF_ITEM_TYPE) or "")
        amount_key = _amount_field(item_type)[0]
        errors: dict[str, str] = {}
        if user_input is not None:
            start = _norm_date(user_input.get(CONF_PERIOD_START))
            end = _norm_date(user_input.get(CONF_PERIOD_END))
            if not start:
                # 开始日期是必填的：空值在我们自己这里给出中文提示
                # （不能交给 DateSelector 抛，它只会吐英文 "Could not parse date"）
                errors[CONF_PERIOD_START] = ERROR_DATE_REQUIRED
            elif end and end < start:
                errors[CONF_PERIOD_END] = ERROR_END_BEFORE_START
            else:
                self._draft.setdefault(CONF_PERIODS, []).append(
                    {
                        CONF_PERIOD_START: start,
                        CONF_PERIOD_END: end,
                        # 表单字段名随类型变化，落库一律统一成 amount
                        CONF_PERIOD_AMOUNT: _as_float(user_input.get(amount_key)),
                    }
                )
                self._commit_if_editing()
                if user_input.get(FIELD_MORE):
                    # 再来一段：清空表单重新展示
                    return self.async_show_form(
                        step_id=STEP_PERIOD_ADD,
                        data_schema=_period_schema(item_type=item_type),
                    )
                return await self._after_periods()
        return self.async_show_form(
            step_id=STEP_PERIOD_ADD,
            data_schema=_period_schema(item_type=item_type),
            errors=errors,
        )

    async def _after_periods(self) -> FlowResult:
        """期间录完后的去向：修改流程回期间菜单，新增流程直接进「其他参数」。"""
        if self._edit_index is not None:
            return await self.async_step_periods()
        return await self.async_step_extra()

    async def async_step_period_edit(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        if not self._period_rows:
            return await self.async_step_periods()
        if user_input is not None:
            self._period_index = int(user_input[SELECT_INDEX])
            return await self.async_step_period_edit_form()
        return self.async_show_form(
            step_id=STEP_PERIOD_EDIT,
            data_schema=vol.Schema(
                {
                    vol.Required(SELECT_INDEX): _index_selector(
                        [_period_label(p, self._draft_type) for p in self._period_rows]
                    )
                }
            ),
        )

    async def async_step_period_edit_form(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._ensure_loaded()
        index = self._period_index
        if index is None or not (0 <= index < len(self._period_rows)):
            return await self.async_step_periods()
        target = self._period_rows[index]
        item_type = self._draft_type
        amount_key = _amount_field(item_type)[0]
        errors: dict[str, str] = {}
        if user_input is not None:
            start = _norm_date(user_input.get(CONF_PERIOD_START))
            end = _norm_date(user_input.get(CONF_PERIOD_END))
            if not start:
                errors[CONF_PERIOD_START] = ERROR_DATE_REQUIRED
            elif end and end < start:
                errors[CONF_PERIOD_END] = ERROR_END_BEFORE_START
            else:
                target.update(
                    {
                        CONF_PERIOD_START: start,
                        CONF_PERIOD_END: end,
                        # 表单字段名随类型变化，落库一律统一成 amount
                        CONF_PERIOD_AMOUNT: _as_float(user_input.get(amount_key)),
                    }
                )
                self._period_index = None
                return await self.async_step_periods()
        return self.async_show_form(
            step_id=STEP_PERIOD_EDIT_FORM,
            data_schema=_period_schema(target, item_type),
            errors=errors,
        )

    async def async_step_period_del(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        errors: dict[str, str] = {}
        if user_input is not None:
            index = int(user_input[SELECT_INDEX])
            if len(self._period_rows) <= 1:
                # 至少留一段，否则该项没有任何金额来源（取代原来的「完成」校验）
                errors["base"] = ERROR_LAST_PERIOD
            else:
                if 0 <= index < len(self._period_rows):
                    self._period_rows.pop(index)
                self._commit_if_editing()
                return await self.async_step_periods()
        if not self._period_rows:
            return await self.async_step_periods()
        return self.async_show_form(
            step_id=STEP_PERIOD_DEL,
            data_schema=vol.Schema(
                {
                    vol.Required(SELECT_INDEX): _index_selector(
                        [_period_label(p, self._draft_type) for p in self._period_rows]
                    )
                }
            ),
            errors=errors,
        )

    # ---------------- 其他参数 ----------------
    async def async_step_extra(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        draft = self._draft or {}
        item_type = str(draft.get(CONF_ITEM_TYPE) or "")
        schema = _extra_schema(item_type, draft)

        errors: dict[str, str] = {}
        if user_input is not None and schema is not None:
            payload = dict(user_input)
            if (ITEM_TYPES.get(item_type) or {}).get("kind") == KIND_LOAN:
                payload[CONF_LOAN_MONTHS] = int(_as_float(payload.get(CONF_LOAN_MONTHS)))
                # 还款日夹到 1-31；基准日留空视为「余额是今天查到的」
                repay_day = int(_as_float(payload.get(CONF_REPAY_DAY))) or 1
                payload[CONF_REPAY_DAY] = min(31, max(1, repay_day))
                payload[CONF_BALANCE_DATE] = (
                    _norm_date(payload.get(CONF_BALANCE_DATE)) or _today()
                )
            if (ITEM_TYPES.get(item_type) or {}).get("kind") == KIND_PROPERTY:
                raw_month = str(payload.get(CONF_PAID_UNTIL) or "").strip().replace("/", "-")
                if raw_month and not _valid_month(raw_month):
                    errors[CONF_PAID_UNTIL] = ERROR_INVALID_MONTH
                else:
                    payload[CONF_PAID_UNTIL] = raw_month
            if (ITEM_TYPES.get(item_type) or {}).get("kind") == KIND_HEATING:
                # 「剩余金额」留空 = 未填：必须同时删掉草稿里的旧值，
                # 否则 draft.update(payload) 会沿用上一次录入的值（清空无效）。
                raw_remaining = payload.pop(CONF_HEATING_REMAINING, None)
                draft.pop(CONF_HEATING_REMAINING, None)
                if not (
                    raw_remaining is None
                    or (isinstance(raw_remaining, str) and not raw_remaining.strip())
                ):
                    payload[CONF_HEATING_REMAINING] = round(_as_float(raw_remaining), 2)
            if not errors:
                draft.update(payload)
                self._draft = draft
                if self._edit_index is not None:
                    return await self.async_step_edit_menu()
                self._commit_draft()
                return await self.async_step_init()

        if schema is None:
            # 该类型没有额外参数（如取暖费）
            if self._edit_index is not None:
                return await self.async_step_edit_menu()
            self._commit_draft()
            return await self.async_step_init()

        return self.async_show_form(step_id=STEP_EXTRA, data_schema=schema, errors=errors)

    # ---------------- 删除 ----------------
    async def async_step_delete(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        self._ensure_loaded()
        if not self._rows:
            return await self.async_step_init()
        if user_input is not None:
            self._delete_index = int(user_input[SELECT_INDEX])
            return await self.async_step_delete_confirm()
        return self.async_show_form(
            step_id=STEP_DELETE,
            data_schema=vol.Schema(
                {
                    vol.Required(SELECT_INDEX): _index_selector(
                        [_item_label(it) for it in self._rows]
                    )
                }
            ),
        )

    async def async_step_delete_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        self._ensure_loaded()
        index = self._delete_index
        if index is None or not (0 <= index < len(self._rows)):
            self._delete_index = None
            return await self.async_step_init()

        if user_input is not None:
            if user_input.get("confirm"):
                self._rows.pop(index)
                self._delete_index = None
                return await self.async_step_init()
            self._delete_index = None
            return await self.async_step_init()

        return self.async_show_form(
            step_id=STEP_DELETE_CONFIRM,
            data_schema=vol.Schema({vol.Required("confirm"): selector.BooleanSelector()}),
        )
