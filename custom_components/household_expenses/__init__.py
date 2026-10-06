"""家庭支出集成入口。"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the Household Expenses component."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Household Expenses from a config entry."""
    merged = {**entry.data, **entry.options}
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "config": merged,
        "entities": [],
        "coordinator": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # 选项（增删改费用项）变更后重载，实体列表随之重建
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项变更回调：重载配置项。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    coordinator = (
        hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("coordinator")
    )

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if coordinator is not None and hasattr(coordinator, "async_shutdown"):
        try:
            await coordinator.async_shutdown()
        except Exception as err:  # noqa: BLE001 - 卸载阶段不应阻塞
            _LOGGER.debug("关闭家庭支出协调器时出现异常: %s", err)

    if unload_ok:
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)

    return unload_ok
