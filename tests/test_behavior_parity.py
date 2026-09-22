#!/usr/bin/env python3
"""原始（~/.hermes）与重构版（本仓库）的脱敏行为对比。

目的：证明重构**不改变行为**，只改变配置来源。
方法：同一批输入喂给两个版本，逐条对比输出。
"""
import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

ORIG = Path.home() / ".hermes" / "plugins" / "desensitize" / "__init__.py"

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

print(f"{'输入':<38} {'原始':<30} {'重构':<30} 一致")
print("-" * 110)
for c, o, n in zip(CASES, orig_out, new_out):
    same = o == n
    if not same:
        failures.append(c)
    mark = "✅" if same else "❌"
    print(f"{c[:36]:<38} {o[:28]:<30} {n[:28]:<30} {mark}")

print()
if failures:
    print(f"❌ {len(failures)} 条行为不一致:")
    for f in failures:
        print(f"   - {f}")
    sys.exit(1)
print(f"✅ 全部 {len(CASES)} 条行为一致 —— 重构未改变脱敏行为")
