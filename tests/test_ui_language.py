#!/usr/bin/env python3
"""命令输出语言（ui.language）的行为测试。

验证 en / zh / both 三种取值真的改变输出，且未知值回落 both。不依赖 Hermes
运行时：直接 import plugin 模块，驱动 _handle_desensitize。

背景：/desensitize 的反馈改动前只有中文，对非中文使用者不可读（review #122574）。
"""
import os
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(PKG))

# 隔离 HERMES_HOME，别碰真实配置
tmp_home = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(tmp_home)
os.environ.pop("DESENSITIZE_UI__LANGUAGE", None)

import importlib
from hermes_desensitize import config
config.get_config(reload=True)

P = importlib.import_module("hermes_desensitize.plugin")

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}")
    if not ok:
        failures.append(f"{label}: got={got!r} want={want!r}")
    return ok


def has_cjk(s: str) -> bool:
    return any("\u4e00" <= c <= "\u9fff" for c in s)


print("── ui.language = both（默认）──")
P._UI_LANG = "both"
check("L() 拼接", P.L("Engine", "主引擎"), "Engine / 主引擎")
out = P._handle_desensitize("off")
check("off 含英文", "Desensitization disabled" in out, True)
check("off 含中文", has_cjk(out), True)
check("status 标签双语", "Status / 脱敏状态" in P._handle_desensitize("status"), True)

print()
print("── ui.language = en（纯英文）──")
P._UI_LANG = "en"
check("L() 只取英文", P.L("Engine", "主引擎"), "Engine")
out = P._handle_desensitize("off")
check("off 无中文", has_cjk(out), False)
check("off 保留英文", out.startswith("Desensitization disabled"), True)
status = P._handle_desensitize("status")
# [xxx数量级N] 是脱敏时真正写入文本的占位符（plugin.py:371），属「值」不属「标签」，
# 任何语言下都必须原样保留，否则 transform_llm_output 的还原会失配。
status_labels = "\n".join(l for l in status.split("\n") if "xxx数量级" not in l)
check("status 标签无中文（占位符除外）", has_cjk(status_labels), False)
check("status 英文标签", "Status: " in status, True)
check("status 保留占位符原文", "[xxx数量级]" in status, True)
usage = P._handle_desensitize("bogus")
check("usage 无中文", has_cjk(usage), False)
check("usage 含英文", "Usage:" in usage, True)
check("错误串无中文", has_cjk(P._handle_desensitize("timeout 3")), False)
check("chunk 错误无中文", has_cjk(P._handle_desensitize("chunk 1")), False)

print()
print("── ui.language = zh（纯中文，最干净）──")
P._UI_LANG = "zh"
check("L() 只取中文", P.L("Engine", "主引擎"), "主引擎")
out = P._handle_desensitize("off")
check("off 无英文", "Desensitization" not in out, True)
check("off 保留中文", "脱敏已关闭" in out, True)
check("usage 无英文标签", "Usage:" not in P._handle_desensitize("bogus"), True)

print()
print("── 空串处理（L 的边界）──")
for lang in ("both", "en", "zh"):
    P._UI_LANG = lang
    check(f"{lang}: L('', '主引擎') 不为 None", P.L("", "主引擎") is not None, True)
    check(f"{lang}: L('Engine', '') 不为 None", P.L("Engine", "") is not None, True)

print()
print("── 配置层驱动（环境变量与文件，真实路径）──")
import subprocess

probe = (
    "import sys, os; sys.path.insert(0, {pkg!r});"
    "import hermes_desensitize.plugin as P; P._sync_from_config();"
    "print(repr(P._UI_LANG))"
)

for val, want in (("en", "en"), ("zh", "zh"), ("both", "both")):
    env = dict(os.environ, DESENSITIZE_UI__LANGUAGE=val)
    got = subprocess.run([sys.executable, "-c", probe.format(pkg=str(PKG))],
                         env=env, capture_output=True, text=True).stdout.strip()
    check(f"env ui.language={val}", got, repr(want))

# 未知值必须回落 both，不能静默取中文
env = dict(os.environ, DESENSITIZE_UI__LANGUAGE="fr")
got = subprocess.run([sys.executable, "-c", probe.format(pkg=str(PKG))],
                     env=env, capture_output=True, text=True).stdout.strip()
check("未知值回落 both", got, repr("both"))

# 配置文件路径
cfg_home = Path(tempfile.mkdtemp())
(cfg_home / "desensitize.yaml").write_text("ui:\n  language: en\n", encoding="utf-8")
env = dict(os.environ, HERMES_HOME=str(cfg_home))
env.pop("DESENSITIZE_UI__LANGUAGE", None)
got = subprocess.run([sys.executable, "-c", probe.format(pkg=str(PKG))],
                     env=env, capture_output=True, text=True).stdout.strip()
check("配置文件 ui.language=en", got, repr("en"))

print()
if failures:
    print(f"❌ {len(failures)} 项失败:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
