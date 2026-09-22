"""Hermes Desensitize — 中文语境脱敏插件。

Hermes 的插件加载器要求**包根**暴露 :func:`register`，而实现体在
:mod:`hermes_desensitize.plugin`。此处显式转发；其余公开符号一并转出，
方便 ``from hermes_desensitize import regex_desensitize`` 这类直接用法。
"""

from .plugin import (  # noqa: F401
    PATTERNS,
    TYPE_TO_CN,
    _handle_desensitize,
    _path_res,
    _sync_from_config,
    register,
    regex_desensitize,
)

__all__ = [
    "register",
    "regex_desensitize",
    "PATTERNS",
    "TYPE_TO_CN",
]

__version__ = "1.0.0"
