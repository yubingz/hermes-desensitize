#!/usr/bin/env python3
"""配置加载器测试 —— 验证三层覆盖（默认 → 用户文件 → 环境变量）"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# 隔离：用临时 HERMES_HOME，别碰真实 ~/.hermes
tmp_home = Path(tempfile.mkdtemp())
os.environ["HERMES_HOME"] = str(tmp_home)

from hermes_desensitize import config  # noqa: E402

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  {'✅' if ok else '❌'} {label}: {got!r}" + ("" if ok else f"  (期望 {want!r})"))
    if not ok:
        failures.append(label)


print("=== 1. 只读包内默认值 ===")
cfg = config.get_config(reload=True)
check("llm.provider", config.get("llm.provider"), "ollama")
check("llm.timeout", config.get("llm.timeout"), 10)
check("chunk_max_chars", config.get("chunk_max_chars"), 2000)
check("behavior.enabled_by_default", config.get("behavior.enabled_by_default"), True)
check("public_entities 非空", len(config.get("public_entities", [])) > 0, True)
check("quantity.enabled", config.get("quantity.enabled"), True)

print("\n=== 2. 用户文件覆盖 ===")
(tmp_home / "desensitize.yaml").write_text(
    "llm:\n  model: my-local-model\n  timeout: 25\nchunk_max_chars: 500\n",
    encoding="utf-8",
)
cfg = config.get_config(reload=True)
check("llm.model 被覆盖", config.get("llm.model"), "my-local-model")
check("llm.timeout 被覆盖", config.get("llm.timeout"), 25)
check("chunk_max_chars 被覆盖", config.get("chunk_max_chars"), 500)
check("llm.provider 保持默认", config.get("llm.provider"), "ollama")

print("\n=== 3. 环境变量覆盖（最高优先级）===")
os.environ["DESENSITIZE_LLM__TIMEOUT"] = "42"
os.environ["DESENSITIZE_LLM__PROVIDER"] = "openai"
os.environ["DESENSITIZE_BEHAVIOR__DEBUG"] = "true"
cfg = config.get_config(reload=True)
check("env 覆盖 timeout", config.get("llm.timeout"), 42)
check("env 覆盖 provider", config.get("llm.provider"), "openai")
check("env 布尔解析", config.get("behavior.debug"), True)
check("用户文件仍生效", config.get("llm.model"), "my-local-model")

print("\n=== 4. 坏 YAML 不应崩溃 ===")
(tmp_home / "desensitize.yaml").write_text("llm: [unclosed\n  bad: : :\n\t", encoding="utf-8")
cfg = config.get_config(reload=True)
check("坏文件被忽略，回退默认", config.get("llm.provider"), "openai")  # env 仍覆盖

print("\n=== 5. 未识别的 env 变量被忽略 ===")
os.environ["DESENSITIZE_NOT_A_REAL_KEY"] = "xxx"
cfg = config.get_config(reload=True)
check("未识别键未写入", "not_a_real_key" in cfg, False)
check("未知路径返回 default", config.get("no.such.path", "fallback"), "fallback")

print()
if failures:
    print(f"❌ {len(failures)} 项失败: {failures}")
    sys.exit(1)
print("✅ 全部通过")
