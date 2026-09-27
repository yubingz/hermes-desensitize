#!/usr/bin/env python3
"""原始（~/.hermes）与重构版（本仓库）的脱敏行为对比。

目的：证明重构**除了已知修复项之外**不改变行为。
方法：同一批输入喂给两个版本，逐条对比输出。

原始版有两处**已确认的 bug**（本次有意修复），因此这几条预期会不一致，
由 EXPECTED_DIVERGENCES 显式登记——出现预期外的差异仍会让本测试失败。
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ORIG = Path(__file__).parent / "fixtures" / "original_desensitize_20260817.py"
# 原先指向 ~/.hermes/plugins/desensitize/__init__.py。2026-09-25 该目录重装为包布局后，
# 那个 __init__.py 是包根（`from .plugin import ...`），以顶层模块名 exec 必然
# ModuleNotFoundError: No module named 'orig_desens' —— 测试从此一直红，且红的原因
# 与被测行为无关。原始版（8月17日单文件，39KB）已归档为本目录夹具，自包含、可复现，
# 不再依赖用户机器上的安装状态。

CASES = [
    "联系电话13812345678",
    "邮箱是 zhang.san@example.com",
    "身份证110101199003078515",
    "北京某某有限公司中标了",
    "中国石油天然气股份有限公司发布年报",
    "文件在 /home/alice/project/data.csv",
    "该项目产值 3.5 亿元",
    "我们和华为、清华大学都有合作",
    "我们公司今年业绩不错",
    "本项目预计明年投产",
    "IP 是 192.168.1.100",
    "服务器地址 10.0.0.5:8080",
    "我叫张三，住在北京市朝阳区",
    "合同金额 1200 万元",
    "联系电话 18600001111 和 13911112222",
]

failures = []

# 已知且**有意**的行为差异：原始版这里是坏的，本次修复。
# key = 输入，value = 为什么应该不同。
EXPECTED_DIVERGENCES = {
    "北京某某有限公司中标了":
        "原始版 org 正则的 CJK 区间写成 \\\\u4e00（字面反斜杠），永远匹配不到汉字，公司名不脱敏；已修",
    "中国石油天然气股份有限公司发布年报":
        "同上——公司名正则整体失效",
    "合同金额 1200 万元":
        "原始版 quantity 关键词表不含'合同额/合同金额'，金额静默漏脱敏；已加入默认关键词",
}


def load_orig():
    os.environ["HERMES_HOME"] = tempfile.mkdtemp()
    spec = importlib.util.spec_from_file_location("orig_desens", ORIG)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_new():
    os.environ["HERMES_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from hermes_desensitize import config
    config.get_config(reload=True)
    import hermes_desensitize.plugin as p
    importlib.reload(p)
    p._sync_from_config()
    return p


orig = load_orig()
orig_out = [orig.regex_desensitize(c)[0] for c in CASES]

new = load_new()
new_out = [new.regex_desensitize(c)[0] for c in CASES]

print(f"{'输入':<38} {'原始':<30} {'重构':<30} 判定")
print("-" * 110)
diverge_ok = 0
for c, o, n in zip(CASES, orig_out, new_out):
    same = o == n
    if same:
        mark = "✅ 一致"
    elif c in EXPECTED_DIVERGENCES:
        mark = "🔧 已修"
        diverge_ok += 1
    else:
        mark = "❌ 意外"
        failures.append(c)
    print(f"{c[:36]:<38} {o[:28]:<30} {n[:28]:<30} {mark}")

print()
if diverge_ok:
    print(f"🔧 {diverge_ok} 条为有意修复（原始版此处是 bug）:")
    for c in CASES:
        if c in EXPECTED_DIVERGENCES and c not in failures:
            print(f"   - {c}: {EXPECTED_DIVERGENCES[c]}")
    print()
if failures:
    print(f"❌ {len(failures)} 条**预期外**的行为不一致:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print(f"✅ 除 {diverge_ok} 条已登记修复外，其余 {len(CASES)-diverge_ok} 条行为与原始版一致")
