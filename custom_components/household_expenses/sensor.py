"""Sensor platform：每个费用项一个传感器实体。"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_DATE,
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
    CONF_ITEM_ID,
    CONF_ITEM_NAME,
    CONF_ITEM_OBJECT_ID,
    CONF_ITEM_TYPE,
    CONF_ITEMS,
    DEFAULT_ICON,
    DOMAIN,
    ITEM_TYPES,
    NAME,
)
from .coordinator import HouseholdExpensesCoordinator

_LOGGER = logging.getLogger(__name__)

# 实体属性顺序与「实体属性结构.txt」对齐
ATTR_ORDER = (
    ATTR_TYPE,
    ATTR_REMAINING,
    ATTR_DATE,
    ATTR_DAYLIST,
    ATTR_MONTHLIST,
    ATTR_YEARLIST,
    ATTR_SOURCE,
    ATTR_LAST_SYNC,
    ATTR_MONTH_TOTAL,
    ATTR_YEAR_TOTAL,
    # 贷款专属（其余类型为 None / 空串，顺序追加在后，不影响原有结构）
    ATTR_REPAY_DAY,
    ATTR_NEXT_REPAY,
)

_INVALID_OBJECT_ID = re.compile(r"[^0-9a-z_]+")


def ascii_object_id(value: Any) -> str:
    """把任意文本压成 HA 允许的 object_id 字符集（``[a-z0-9_]``）。

    HA 的 ``valid_entity_id`` 只接受 ``[a-z0-9_]``，并且 ``slugify`` 依赖
    python-slugify 做音译，中文会被整段丢掉（退化成 ``unknown``），
    所以实体 ID 必须自带 ASCII 标识，显示名再交给 ``friendly_name``。
    """
    cleaned = _INVALID_OBJECT_ID.sub("_", str(value or "").lower()).strip("_")
    return cleaned or "item"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up household expense sensors from a config entry."""
    config = {**entry.data, **entry.options}
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(
        entry.entry_id,
        {"config": config, "entities": []},
    )
    entry_data["config"] = config

    coordinator: HouseholdExpensesCoordinator | None = entry_data.get("coordinator")
    if coordinator is None:
        coordinator = HouseholdExpensesCoordinator(hass, config)
        entry_data["coordinator"] = coordinator

    await coordinator.async_prepare()
    await coordinator.async_config_entry_first_refresh()

    entities = [
        HouseholdExpenseSensor(coordinator, item)
        for item in config.get(CONF_ITEMS) or []
        if isinstance(item, dict) and item.get(CONF_ITEM_NAME)
    ]
    entry_data["entities"] = entities
    async_add_entities(entities)


class HouseholdExpenseSensor(CoordinatorEntity[HouseholdExpensesCoordinator], SensorEntity):
    """单个费用项（房贷 / 车贷 / 物业费 ...）。"""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_native_unit_of_measurement = "元"
    _attr_suggested_display_precision = 2
    # 日 / 月 / 年明细是纯展示数据且体积大（房贷 20 年 ≈ 17 KB）：
    # 用 HA 官方机制把它们挡在 Recorder 之外 —— 前端读 hass.states 照旧拿得到，
    # 只是不落历史库，避免整份属性超 16384 字节被整体丢弃（连「剩余金额」都没历史）。
    _unrecorded_attributes = frozenset({ATTR_DAYLIST, ATTR_MONTHLIST, ATTR_YEARLIST})

    def __init__(
        self, coordinator: HouseholdExpensesCoordinator, item: dict[str, Any]
    ) -> None:
        super().__init__(coordinator)
        self._item = item
        self._item_name = str(item.get(CONF_ITEM_NAME) or "未命名")
        item_id = str(item.get(CONF_ITEM_ID) or ascii_object_id(self._item_name))
        type_key = str(item.get(CONF_ITEM_TYPE) or "item")
        info = ITEM_TYPES.get(type_key) or {}

        # entity_id 必须是的 ASCII；object_id 优先取配置里保存的稳定值，
        # 老配置缺字段时回落到「类型_短ID」，保证同一项不会漂移。
        object_id = item.get(CONF_ITEM_OBJECT_ID) or f"{type_key}_{item_id[:8]}"
        self._attr_unique_id = f"{DOMAIN}_{item_id}"
        self._attr_name = f"{NAME} {self._item_name}"
        self._attr_icon = info.get("icon", DEFAULT_ICON)
        self.entity_id = f"sensor.{DOMAIN}_{ascii_object_id(object_id)}"

    # ------------------------------------------------------------------
    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers": {(DOMAIN, DOMAIN)},
            "name": NAME,
            "manufacturer": NAME,
            "model": "家庭支出账本",
        }

    @property
    def available(self) -> bool:
        return bool(self._snapshot())

    def _snapshot(self) -> dict[str, Any]:
        return self.coordinator.item_snapshot(self._item_name)

    # ------------------------------------------------------------------
    @property
    def native_value(self) -> float:
        """状态值取「剩余金额」。"""
        try:
            return float(self._snapshot().get(ATTR_REMAINING, 0.0))
        except (TypeError, ValueError):
            return 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        snapshot = self._snapshot()
        return {key: snapshot.get(key) for key in ATTR_ORDER}
