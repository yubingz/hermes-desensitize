#!/usr/bin/env python3
"""正则脱敏层回归测试 —— 确认重构没有改变脱敏行为。

用固定的输入/期望对照，覆盖：手机号、邮箱、身份证、公司名、路径、数量级。
"""
import importlib
import os
import sys
import tempfile
from pathlib import Path

PKG = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(PKG))

tmp_home = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(tmp_home)

from hermes_desensitize import config  # noqa: E402
config.get_config(reload=True)
plugin = importlib.import_module("hermes_desensitize.plugin")
plugin._sync_from_config()

failures = []


def check(label, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {label}" + (f"  {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(label)


def run(text):
    """调用正则脱敏层，返回 (脱敏后文本, 映射)"""
    fn = getattr(plugin, "regex_desensitize")
    try:
        return fn(text)
    except TypeError:
        return fn(text, {})


print("=== 正则脱敏层回归 ===")

cases = [
    ("手机号", "联系电话13812345678", "13812345678"),
    ("邮箱", "邮箱是 zhang.san@example.com", "zhang.san@example.com"),
    ("身份证", "身份证110101199003078515", "110101199003078515"),
    ("路径", "文件在 /home/alice/project/data.csv", "/home/alice/project/data.csv"),
]

# 注：公司名（含"有限公司"这类标准后缀）**不由正则层处理** —— 已对原始插件与重构版
# 双向验证：'北京某某有限公司中标了' 两者都不脱敏（org 正则被 jieba 分词改动破坏，
# 属重构前既有行为）。公司名靠 LLM 语义层。此处不写公司名断言，避免用错的期望值
# 污染回归基线。修复 org 正则列为独立议题。

for label, text, secret in cases:
    out, mapping = run(text)
    ok = secret not in out
    print(f"  {'✅' if ok else '❌'} {label} 被脱敏")
    if not ok:
        failures.append(f"{label} 未脱敏")
    else:
        print(f"      原文: {text}")
        print(f"      脱敏: {out}")
        # 还原验证
        restored = out
        for ph, orig in mapping.items():
            restored = restored.replace(ph, orig)
        same = restored == text
        print(f"      还原{'成功' if same else '失败'}: {restored}")
        if not same:
            failures.append(f"{label} 还原不一致")

print("\n=== 路径 + 数量级 ===")
out, mapping = run("文件在 /home/alice/project/data.csv")
check("家目录路径被脱敏", "/home/alice" not in out, out)

out2, mapping2 = run("该项目产值 3.5 亿元")
check("数量级被模糊", "3.5 亿" not in out2, out2)

print("\n=== 公开实体不脱敏（白名单生效）===")
out3, _ = run("我们和华为、清华大学都有合作")
check("华为未被脱敏", "华为" in out3, out3)
check("清华大学未被脱敏", "清华大学" in out3, out3)

print()
if failures:
    print(f"❌ {len(failures)} 项失败:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print("✅ 全部通过")
