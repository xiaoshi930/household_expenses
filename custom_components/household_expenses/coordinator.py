"""DataUpdateCoordinator：负责把费用项参数换算成运行时快照。"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .calc import build_snapshot
from .const import CONF_ITEMS, DOMAIN, NAME
from .storage import async_write_snapshot

_LOGGER = logging.getLogger(__name__)

# 金额只与日期有关，半小时刷新一次足以跨过零点
UPDATE_INTERVAL = timedelta(minutes=30)


class HouseholdExpensesCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """家庭支出协调器。"""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}:{NAME}",
            update_interval=UPDATE_INTERVAL,
        )
        self.hass = hass
        self.config = config
        self.items: list[dict[str, Any]] = list(config.get(CONF_ITEMS) or [])
        self.runtime_snapshot: dict[str, Any] = {}

    # ------------------------------------------------------------------
    async def async_prepare(self) -> None:
        """在首次刷新前先算一遍，保证实体创建时就有数据。"""
        self.rebuild()

    async def _async_update_data(self) -> dict[str, Any]:
        """定时刷新：重算快照并落盘（落盘在线程池，不能阻塞事件循环）。"""
        self.rebuild()
        await async_write_snapshot(self.hass, self.runtime_snapshot)
        return self.runtime_snapshot

    # ------------------------------------------------------------------
    def rebuild(self) -> dict[str, Any]:
        """按当前配置重算运行时快照（实体属性唯一出口）。"""
        today = dt_util.now().date()
        now_text = dt_util.now().strftime("%Y-%m-%d %H:%M:%S")
        self.runtime_snapshot = build_snapshot(self.items, today, now_text)
        return self.runtime_snapshot

    def item_snapshot(self, name: str) -> dict[str, Any]:
        """取某个费用项的快照，缺失时返回空字典。"""
        items = self.runtime_snapshot.get("items") or {}
        snapshot = items.get(name)
        return snapshot if isinstance(snapshot, dict) else {}
