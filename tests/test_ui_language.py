#!/usr/bin/env python3
"""命令输出语言（ui.language）的行为测试。

验证 en / zh / both 三种取值真的改变输出，且未知值回落 both。不依赖 Hermes
运行时：直接 import plugin 模块，驱动 _handle_desensitize。

背景：/desensitize 的反馈改动前只有中文，对非中文使用者不可读（review #122574）。

契约在 2026-09-27 有三处**有意变更**（本文件同步更新，旧断言保留为注释）：
  1. 显式 `/desensitize lang <x>` 后，help 跟随该语言（原先 help 恒由 help_language 决定，
     用户设了 zh 再看 help 还是英文，被当成「没生效」）。
  2. `lang <非法值>` 保持原值，不再回落 both（原先是 zh 的用户打错一次就被降级）。
  3. 未知子命令先报一行提示（走会话语言 L()），再给用法块（走 help_language）。
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
check("usage 含英文", "Usage" in usage, True)
check("错误串无中文", has_cjk(P._handle_desensitize("timeout 3")), False)
check("chunk 错误无中文", has_cjk(P._handle_desensitize("chunk 1")), False)

print()
print("── ui.language = zh（纯中文，最干净）──")
P._UI_LANG = "zh"
P._HELP_LANG = "en"  # help 有独立的 help_language，见下方专块
check("L() 只取中文", P.L("Engine", "主引擎"), "主引擎")
out = P._handle_desensitize("off")
check("off 无英文", "Desensitization" not in out, True)
check("off 保留中文", "脱敏已关闭" in out, True)
check("status 标签纯中文", has_cjk(P._handle_desensitize("status")), True)
# 注意：不在此断言 help 的语言 —— help 由 help_language 控制（默认 en），
# 与 ui.language 无关。help 的断言在下面 "help 默认英文" 专块。

print()
print("── /desensitize lang 会话内切换 ──")
P._sync_from_config()
P._UI_LANG = "both"
check("lang 无参数显示当前值", "both" in P._handle_desensitize("lang"), True)
P._handle_desensitize("lang en")
check("lang en 生效", P._UI_LANG, "en")
check("lang en 后 off 无中文", has_cjk(P._handle_desensitize("off")), False)
P._handle_desensitize("lang zh")
check("lang zh 生效", P._UI_LANG, "zh")
check("lang zh 后 off 无英文", "Desensitization" not in P._handle_desensitize("off"), True)
P._handle_desensitize("lang both")
check("lang both 生效", P._UI_LANG, "both")
P._handle_desensitize("lang EN")  # 大小写不敏感
check("lang 大小写不敏感", P._UI_LANG, "en")
P._handle_desensitize("lang fr")
# 2026-09-27 契约变更：原先是 `lang fr` → 回落 both（"已保持: both" 的回执还是假的），
# 现在保持原值 —— 已经是 en 的用户打错一次不该被降级到双语。
check("lang 非法值保持原值（不降级）", P._UI_LANG, "en")

print()
print("── /desensitize lang 别名 ──")
P._sync_from_config()
P._UI_LANG = "both"
P._handle_desensitize("lang cn")
check("cn → zh", P._UI_LANG, "zh")
P._handle_desensitize("lang 中文")
check("中文 → zh", P._UI_LANG, "zh")
P._handle_desensitize("lang english")
check("english → en", P._UI_LANG, "en")
P._handle_desensitize("lang 双语")
check("双语 → both", P._UI_LANG, "both")

print()
print("── 未知子命令的错误提示（新增）──")
P._sync_from_config()
P._UI_LANG = "both"
typo = P._handle_desensitize("sta")
check("笔误给出近似建议", "'status'" in typo, True)
check("未知子命令首行是提示不是 Usage", typo.split("\n")[0].startswith("Unknown subcommand"), True)
check("提示后仍给用法块", "Usage" in typo, True)
P._UI_LANG = "zh"
check("zh 会话下提示为中文", "未知子命令" in P._handle_desensitize("sta"), True)

print()
print("── help 默认英文（help_language）──")


def usage_block(out: str) -> str:
    """剥掉未知子命令的提示行，只留用法块。

    提示行走会话语言（用户已经会用命令了，缺的只是回执），用法块走 help_language
    （面向还没配语言的人）。两者语言不同，所以断言必须分开，否则会把提示行的
    中文误判成「help 不纯」。
    """
    return "\n".join(
        line for line in out.split("\n")
        if not line.startswith(("Unknown subcommand", "未知子命令"))
    ).lstrip("\n")


P._sync_from_config()
check("默认 help_language=en", P._HELP_LANG, "en")
help_out = P._handle_desensitize("bogus")
check("默认 help 用法块无中文", has_cjk(usage_block(help_out)), False)
check("默认 help 用法块首行 Usage:", usage_block(help_out).split("\n")[0], "Usage:")
check("默认 help 首行是提示行", help_out.split("\n")[0].startswith("Unknown subcommand"), True)
check("help 含 lang 子命令", "/desensitize lang" in help_out, True)

# 未显式选过语言时：用户把界面设成纯中文，help 仍应英文（help 面向还没配语言的人）
P._UI_LANG = "zh"
P._HELP_LANG = "en"
P._HELP_LANG_EXPLICIT = False
check("zh 界面下 off 纯中文", "Desensitization" not in P._handle_desensitize("off"), True)
check("zh 界面下 help 用法块仍英文", has_cjk(usage_block(P._handle_desensitize("bogus"))), False)

# 2026-09-27 契约变更：显式 /desensitize lang zh 之后，help 跟随会话语言。
# 旧行为是 help 恒由 help_language 决定 —— 用户设了 zh、再看 help 满屏英文，
# 直接投诉「没生效」（实测两轮）。
P._handle_desensitize("lang zh")
check("显式 lang zh 后 help 用法块变中文", "用法:" in P._handle_desensitize("bogus"), True)
P._handle_desensitize("lang en")
check("显式 lang en 后 help 用法块变英文", "Usage:" in P._handle_desensitize("bogus"), True)

# help_language 可配（未显式选过语言时生效 —— 显式选择优先于配置，见上）
P._HELP_LANG_EXPLICIT = False
P._HELP_LANG = "both"
check("help_language=both 有中文", has_cjk(usage_block(P._handle_desensitize("bogus"))), True)
check("help_language=both 有英文", "Usage" in P._handle_desensitize("bogus"), True)
P._HELP_LANG = "zh"
help_zh = P._handle_desensitize("bogus")
check("help_language=zh 是中文", "用法:" in help_zh, True)
check("help_language=zh 无英文标签", "Usage" not in usage_block(help_zh), True)

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

# help_language 也走配置层
probe_help = (
    "import sys; sys.path.insert(0, {pkg!r});"
    "import hermes_desensitize.plugin as P; P._sync_from_config();"
    "print(repr(P._HELP_LANG))"
)
env = dict(os.environ, DESENSITIZE_UI__HELP_LANGUAGE="zh")
env.pop("DESENSITIZE_UI__LANGUAGE", None)
got = subprocess.run([sys.executable, "-c", probe_help.format(pkg=str(PKG))],
                     env=env, capture_output=True, text=True).stdout.strip()
check("env ui.help_language=zh", got, repr("zh"))

env = dict(os.environ, DESENSITIZE_UI__HELP_LANGUAGE="nonsense")
env.pop("DESENSITIZE_UI__LANGUAGE", None)
got = subprocess.run([sys.executable, "-c", probe_help.format(pkg=str(PKG))],
                     env=env, capture_output=True, text=True).stdout.strip()
check("未知 help_language 回落 en", got, repr("en"))

print()
if failures:
    print(f"❌ {len(failures)} 项失败:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
