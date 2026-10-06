"""快照落盘：把计算结果写到 HA 配置目录下的 household_expenses.json。

文件结构与「实体属性结构.txt」保持一致，方便外部脚本 / 前端卡片直接读取。

⚠️ HA 的事件循环**禁止同步磁盘 IO**：`open()` 写在协程里会被
`homeassistant.util.loop` 抓成 "Detected blocking call to open ... inside the
event loop"（日志刷屏 + 卡住整个 HA）。所以真正的读写都放在
`hass.async_add_executor_job` 的线程池里执行，对外只暴露 async 接口。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CACHE_FILENAME

_LOGGER = logging.getLogger(__name__)


def snapshot_path(hass: HomeAssistant) -> str:
    """快照文件的绝对路径。"""
    return hass.config.path(CACHE_FILENAME)


# ----------------------------------------------------------------------
# 同步实现（只能在线程池里调用）
# ----------------------------------------------------------------------
def _write_snapshot_file(path: str, payload: dict[str, Any]) -> None:
    """原子写入：先写 .tmp 再 os.replace，避免读到写了一半的文件。"""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _read_snapshot_file(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ----------------------------------------------------------------------
# 异步接口（事件循环里用这两个）
# ----------------------------------------------------------------------
async def async_write_snapshot(hass: HomeAssistant, payload: dict[str, Any]) -> str | None:
    """在线程池里原子写入快照文件，失败只记日志不抛出。"""
    path = snapshot_path(hass)
    try:
        await hass.async_add_executor_job(_write_snapshot_file, path, payload)
    except OSError as err:
        _LOGGER.warning("写入家庭支出快照文件失败: %s", err)
        return None
    return path


async def async_read_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    """在线程池里读取快照文件，不存在或损坏时返回空字典。"""
    return await hass.async_add_executor_job(_read_snapshot_file, snapshot_path(hass))
