#!/usr/bin/env python3
"""重构后插件的行为测试 —— 验证配置真的生效，且核心脱敏逻辑没被改坏。

不依赖 Hermes 运行时：直接 import plugin 模块，喂文本给正则层函数。
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

import importlib
from hermes_desensitize import config
config.get_config(reload=True)

plugin = importlib.import_module("hermes_desensitize.plugin")

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}")
    if not ok:
        print(f"      得到 {got!r}\n      期望 {want!r}")
        failures.append(label)


print("=== 1. 模块导入 + 同步配置 ===")
plugin._sync_from_config()
check("provider 默认 ollama", plugin._LLM_PROVIDER, "ollama")
check("model 默认 qwen3:8b（不再是硬编码 hermes3）", plugin._LLM_MODEL, "qwen3:8b")
check("大跨度行业实体已从内置表移除（国家电网）",
      "国家电网" in plugin._PUBLIC_ENTITIES, False)
check("通用实体保留（华为）", "华为" in plugin._PUBLIC_ENTITIES, True)

print("\n=== 2. 数量级正则读配置（真 bug 修复验证）===")
# 默认关键词含"产值" → 应命中
default_re = plugin._quantity_re()
check("默认关键词命中'产值 3.5 亿元'", bool(default_re.search("项目产值 3.5 亿元")), True)
check("默认不命中'用户 3 人'", bool(default_re.search("用户 3 人")), False)

# 覆盖关键词为只含"注册用户数" → "产值"应不再命中
(tmp_home / "desensitize.yaml").write_text(
    "quantity:\n  keywords: \"注册用户数|日活\"\n", encoding="utf-8")
config.get_config(reload=True)
plugin._sync_from_config()
new_re = plugin._quantity_re()
check("配置覆盖后'注册用户数 1200 万'被命中", bool(new_re.search("注册用户数 1200 万")), True)
check("配置覆盖后'产值 3.5 亿元'不再命中", bool(new_re.search("产值 3.5 亿元")), False)

print("\n=== 3. 路径脱敏读配置 ===")
(tmp_home / "desensitize.yaml").write_text(
    "path_masking:\n  patterns:\n    - \"/data/[a-z]+/[a-z0-9_/]+\"\n", encoding="utf-8")
config.get_config(reload=True)
plugin._sync_from_config()
res = plugin._path_res()
check("配置的路径模式生效", any(r.search("/data/proj/main.py") for r in res), True)
(tmp_home / "desensitize.yaml").unlink()
config.get_config(reload=True)
plugin._sync_from_config()
res = plugin._path_res()
check("回退默认模式匹配 /home/user/x", any(r.search("/home/alice/doc.txt") for r in res), True)

print("=== 4. 核心正则层仍工作（回归）===")
text = "联系人张三，电话13812345678，邮箱zhang@example.com"
out, mapping = plugin.regex_desensitize(text)
check("手机号被处理", "13812345678" not in out, True)
check("邮箱被处理", "zhang@example.com" not in out, True)
check("产生了映射（可还原）", len(mapping) > 0, True)
_restored = out
for _ph, _orig in mapping.items():
    _restored = _restored.replace(_ph, _orig)
check("可完整还原", _restored == text, True)
print(f"      输出: {out[:80]}")

print("\n=== 5. 命令处理器可调用 ===")
check("_handle_desensitize 存在", hasattr(plugin, "_handle_desensitize"), True)
# 真实签名是 (raw: str) -> Optional[str]（单参数），不是 (args, ctx)
try:
    r = plugin._handle_desensitize("/desensitize status")
    check("status 命令返回文本", isinstance(r, str) and len(r) > 0, True)
    print(f"      {r.splitlines()[0] if r else ''}")
except Exception as e:
    check(f"status 命令可调用（异常: {type(e).__name__}: {e}）", False, True)

print("\n=== 6. register() 不崩（用假 ctx）===")
class FakeCtx:
    def __init__(self):
        self.hooks, self.commands = [], []
    def register_hook(self, name, fn):
        self.hooks.append(name)
    def register_command(self, **kw):
        self.commands.append(kw.get("name"))

try:
    ctx = FakeCtx()
    plugin.register(ctx)
    check("注册了 4 个 hook", len(ctx.hooks), 4)
    check("注册了 desensitize 命令", ctx.commands, ["desensitize"])
    print(f"      hooks: {ctx.hooks}")
except Exception as e:
    check(f"register() 可执行（异常: {type(e).__name__}: {e}）", False, True)

print()
if failures:
    print(f"❌ {len(failures)} 项失败:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
