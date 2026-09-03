"""配置读取与校验。

优先读取仓库根的 config.json（本机真实配置，已被 .gitignore 排除），
不存在时回退 config.example.json（模板，入库）。
任何非法配置抛 ValueError，消息用中文说明。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

# 运行形态两种：
#   源码：仓库根 = uptime/common/config.py 向上三级（common -> uptime -> 仓库根），
#         config.json / data/ 都在仓库根
#   PyInstaller 打包后：数据与配置模板打入 exe（解包在 _MEIPASS 临时目录），
#         真实 config.json 放在 exe 旁边（用户可编辑，首跑自动从内置模板生成）
if getattr(sys, "frozen", False):
    PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 24 小时制 HH:MM（同时校验数值范围：时 00-23，分 00-59）
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """读取并校验配置文件，返回 dict。

    Args:
        path: 指定配置文件路径；缺省时依次找 config.json -> config.example.json。
    """
    p = Path(path) if path is not None else _default_config_path()
    try:
        with open(p, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        raise ValueError(f"配置文件不存在：{p}") from None
    except json.JSONDecodeError as e:
        raise ValueError(f"配置文件不是合法 JSON：{p}（{e}）") from None

    if not isinstance(cfg, dict):
        raise ValueError("配置根节点必须是 JSON 对象（{...}）")

    _validate(cfg)
    return cfg


def _exe_config_path() -> Path | None:
    """打包形态：exe 旁的 config.json；首跑缺失时从内置模板生成一份（方便改月薪等）。

    生成失败（只读目录等）不报错，由调用方回退内置模板。
    """
    p = Path(sys.executable).resolve().parent / "config.json"
    if not p.is_file():
        bundled = PROJECT_ROOT / "config.example.json"
        if bundled.is_file():
            try:
                p.write_text(bundled.read_text(encoding="utf-8"), encoding="utf-8")
            except OSError:
                return p  # 生成不了就交给调用方回退
    return p


def _default_config_path() -> Path:
    """config.json 优先，其次 config.example.json，都没有则报错。"""
    if getattr(sys, "frozen", False):
        p = _exe_config_path()
        if p is not None and p.is_file():
            return p
        bundled = PROJECT_ROOT / "config.example.json"
        if bundled.is_file():
            return bundled
        raise ValueError("未找到配置文件：exe 旁 config.json 与内置模板均不存在")
    for name in ("config.json", "config.example.json"):
        candidate = PROJECT_ROOT / name
        if candidate.is_file():
            return candidate
    raise ValueError(
        f"未找到配置文件：{PROJECT_ROOT / 'config.json'} 与 config.example.json 均不存在"
    )


def config_path() -> Path:
    """当前生效的配置文件路径（与 load_config 缺省同源；写回用）。"""
    return _default_config_path()


def save_config(cfg: dict[str, Any], path: str | Path | None = None) -> Path:
    """整份写回配置文件（UTF-8，缩进 2）。调用方负责先 load 再改再存。"""
    p = Path(path) if path is not None else _default_config_path()
    _validate(cfg)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def _validate(cfg: dict[str, Any]) -> None:
    _check_number(cfg, "monthly_salary", minimum=0)
    _check_number(cfg, "monthly_workdays", minimum=0, exclusive=True)
    _check_time(cfg, "work_start")
    _check_time(cfg, "work_end")
    _check_workdays(cfg)
    _check_number(cfg, "lunch_break_minutes", minimum=0)


def _check_number(
    cfg: dict[str, Any], key: str, *, minimum: float, exclusive: bool = False
) -> None:
    val = cfg.get(key)
    # bool 是 int 的子类，需先排除，避免 True/False 混进数字配置
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"配置项 {key} 必须是数字，当前为 {val!r}")
    if exclusive and not val > minimum:
        raise ValueError(f"配置项 {key} 必须大于 {minimum}，当前为 {val!r}")
    if not exclusive and not val >= minimum:
        raise ValueError(f"配置项 {key} 必须不小于 {minimum}，当前为 {val!r}")


def _check_time(cfg: dict[str, Any], key: str) -> None:
    val = cfg.get(key)
    if not isinstance(val, str) or not _HHMM_RE.match(val):
        raise ValueError(
            f"配置项 {key} 必须是形如 HH:MM 的 24 小时制时间字符串（如 09:00），当前为 {val!r}"
        )


def _check_workdays(cfg: dict[str, Any]) -> None:
    val = cfg.get("workdays")
    if not isinstance(val, list) or not val:
        raise ValueError(
            f"配置项 workdays 必须是非空的 0-6 整数列表（0=周一 … 6=周日），当前为 {val!r}"
        )
    for d in val:
        if isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6:
            raise ValueError(
                f"配置项 workdays 中的 {d!r} 非法：必须是 0-6 的整数（0=周一 … 6=周日）"
            )
