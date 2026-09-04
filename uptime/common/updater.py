"""更新检查：向可配置源询问最新发布版本，与本机内置版本号比对。

对外形象＝后台监控服务，故 UI 文案（notify/菜单）用英文；源默认 GitHub
releases/latest，config 里 `updater.url` 可换成任意"返回兼容 JSON"的国内源
（见下），无代理/连不上时静默失败（返回 None），完全不影响使用。

更新源 JSON 约定（与 GitHub Releases API latest 兼容的子集）：
{
  "tag_name": "v1.1.0",                     // 必填：最新版本号（可带 v 前缀）
  "name": "uptime v1.1.0",                  // 可选
  "body": "…更新说明…",                      // 可选
  "browser_download_url": "https://…",      // 可选：直接下载链接
  "assets": [ {"name": "uptime.exe", "browser_download_url": "…"} ]  // 可选
}
解析时先找顶层 tag_name；下载链接优先取 assets 中名为 uptime.exe 的项，
其次顶层 browser_download_url，都没有则退回 release 页面。
"""

from __future__ import annotations

import json
import re
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from uptime.common.config import load_config
from uptime import __version__ as _PACKAGE_VERSION

# 默认源：本仓库 GitHub Releases API（需能直连 github 才可达；国内无代理会超时 → 静默）
DEFAULT_UPDATE_URL = (
    "https://api.github.com/repos/BoloJoe-stack/uptime/releases/latest"
)
RELEASE_PAGE_URL = "https://github.com/BoloJoe-stack/uptime/releases/latest"
_UA = "uptime-updater/1.0 (+https://github.com/BoloJoe-stack/uptime)"
_TIMEOUT = 5.0  # 秒；网络差也不拖慢启动


@dataclass
class UpdateInfo:
    """一次"有新版"的结果。"""

    version: str                 # 不带 v 前缀，如 "1.1.0"
    name: str = ""               # release 名称（可空）
    body: str = ""               # 更新说明（可空）
    download_url: str | None = None
    page_url: str = RELEASE_PAGE_URL
    extra: dict[str, Any] = field(default_factory=dict)


def current_version() -> str:
    """本机内置版本号（不带 v 前缀）。"""
    return _PACKAGE_VERSION.lstrip("v")


def _norm_version(ver: str) -> tuple[int, ...]:
    """'v1.10.2' → (1,10,2)；非法返回空元组。"""
    m = re.search(r"(\d+(?:\.\d+)*)", str(ver).strip())
    if not m:
        return ()
    return tuple(int(x) for x in m.group(1).split("."))


def is_newer(remote: str, current: str) -> bool:
    """远端版本号是否比本机新（逐段数字比较，避免 '1.10'>'1.9' 误判）。"""
    return _norm_version(remote) > _norm_version(current)


def parse_release(data: dict[str, Any]) -> UpdateInfo:
    """把 GitHub(或兼容) Releases 返回 JSON 解析成 UpdateInfo。非法字段取默认。"""
    tag = str(data.get("tag_name") or "").strip()
    info = UpdateInfo(version=tag.lstrip("v"))
    info.name = str(data.get("name") or "")
    info.body = str(data.get("body") or "")
    page = str(data.get("html_url") or RELEASE_PAGE_URL)
    if page:
        info.page_url = page
    url: str | None = None
    assets = data.get("assets")
    if isinstance(assets, list):
        for a in assets:
            if isinstance(a, dict) and str(a.get("name") or "") == "uptime.exe":
                url = str(a.get("browser_download_url") or "") or None
                break
    if not url:
        cand = str(data.get("browser_download_url") or "")
        url = cand or None
    info.download_url = url
    return info


def updater_config(cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """读 config 的 updater 段：返回 (enabled, url)。缺省 enabled=True，url=''→默认源。

    类型非法一律回退默认（宽松，不让坏配置破坏启动）。
    """
    c = (cfg if cfg is not None else load_config()).get("updater")
    if not isinstance(c, dict):
        return True, DEFAULT_UPDATE_URL
    enabled = c.get("enabled", True)
    if not isinstance(enabled, bool):
        enabled = True
    url = str(c.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        url = DEFAULT_UPDATE_URL
    return enabled, url


def fetch_latest(url: str, timeout: float = _TIMEOUT) -> dict[str, Any] | None:
    """GET 更新源，返回 JSON dict。任何失败（网络/超时/解析/非 dict）返回 None。"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 —— 尽力而为，失败静默
        return None
    return data if isinstance(data, dict) else None


def check_update(cfg: dict[str, Any] | None = None) -> UpdateInfo | None:
    """聚合入口：读配置 → 查源 → 比对版本。

    返回：有新版 → UpdateInfo；禁用/无新版/源不可达 → None（调用方静默处理）。
    """
    try:
        enabled, url = updater_config(cfg)
        if not enabled:
            return None
        data = fetch_latest(url)
        if data is None:
            return None
        info = parse_release(data)
        if not info.version or not is_newer(info.version, current_version()):
            return None
        return info
    except Exception:  # noqa: BLE001 —— 任何意外都不打扰用户
        return None
