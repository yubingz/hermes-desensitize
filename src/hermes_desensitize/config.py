"""配置加载：包内默认值 → 用户文件 → 环境变量（后者覆盖前者）。

用户可编辑 ~/.hermes/desensitize.yaml 覆盖任何一项，无需改代码。
环境变量用 DESENSITIZE_ 前缀（如 DESENSITIZE_LLM_MODEL=qwen3:14b）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger("hermes_plugin.desensitize")

# 包内默认配置
_DEFAULT_PATH = Path(__file__).with_name("default_config.yaml")

# 用户覆盖文件（放在 Hermes home 下，与 config.yaml 同级）
_USER_FILENAME = "desensitize.yaml"

# 环境变量前缀
ENV_PREFIX = "DESENSITIZE_"

_config: dict[str, Any] | None = None


def _hermes_home() -> Path:
    """Hermes home 目录。遵循 HERMES_HOME（profile 感知），回退 ~/.hermes。"""
    env = os.environ.get("HERMES_HOME")
    return Path(env) if env else Path.home() / ".hermes"


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning("desensitize: PyYAML 未安装，无法读取 %s（用内置默认值）", path)
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:  # 用户写坏 YAML 不应该让插件崩
        log.warning("desensitize: 解析 %s 失败（%s），忽略该文件", path, e)
        return {}


def _deep_merge(base: dict, over: dict) -> dict:
    """递归合并：over 覆盖 base。列表整体替换，不做元素级合并。"""
    out = dict(base)
    for k, v in over.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _apply_env(cfg: dict) -> dict:
    """环境变量覆盖。点号路径映射：DESENSITIZE_LLM_MODEL → llm.model。

    只覆盖已存在的键，避免拼错变量名时静默写入垃圾配置。
    """
    def _set(d: dict, path: list[str], value: Any) -> bool:
        cur = d
        for p in path[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                return False
            cur = cur[p]
        if path[-1] not in cur:
            return False
        cur[path[-1]] = value
        return True

    def _coerce(s: str) -> Any:
        low = s.strip().lower()
        if low in ("true", "yes", "on"):
            return True
        if low in ("false", "no", "off"):
            return False
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return s

    for key, raw in os.environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = key[len(ENV_PREFIX):].lower().split("__")  # 双下划线分隔层级
        if not _set(cfg, path, _coerce(raw)):
            log.debug("desensitize: 忽略未识别的环境变量 %s", key)
    return cfg


def get_config(reload: bool = False) -> dict[str, Any]:
    """返回合并后的配置（带缓存）。reload=True 强制重读。"""
    global _config
    if _config is not None and not reload:
        return _config

    cfg = _load_yaml(_DEFAULT_PATH)
    user_path = _hermes_home() / _USER_FILENAME
    user_cfg = _load_yaml(user_path)
    if user_cfg:
        cfg = _deep_merge(cfg, user_cfg)
        log.info("desensitize: 已合并用户配置 %s", user_path)
    cfg = _apply_env(cfg)

    _config = cfg
    return cfg


def get(path: str, default: Any = None) -> Any:
    """按点号路径取值：get("llm.model")。"""
    cur: Any = get_config()
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur
