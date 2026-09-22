"""脱敏插件 end-to-end 测试"""
import sys, json, logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s|%(message)s")

sys.path.insert(0, '.')
import importlib.util
spec = importlib.util.spec_from_file_location('plugin', '__init__.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# 测试文本
test_text = "最近我们公司和华为合作了一个项目，预计明年销售额达到8000万元，项目负责人是张伟和李明，后续由王芳跟进。"

print("=" * 60)
print("📥 原文:", test_text)
print()

# 先走 LLM 路径（60s 超时）
print("🔄 LLM 脱敏路径（60s 超时）...")
sys.stdout.flush()
result = mod.llm_desensitize(test_text)
if result is not None:
    de_text, mapping = result
    print("✅ LLM 成功")
    print("🔒 脱敏:", de_text)
    print("🗺️ 映射:", json.dumps(mapping, ensure_ascii=False, indent=2))

    # 验证映射完整性
    missing_phs = [ph for ph in mapping if ph not in de_text]
    if missing_phs:
        print(f"⚠️  警告: {len(missing_phs)} 个占位符未出现在文本中: {missing_phs}")
    else:
        print("✅ 验证通过: 所有占位符均已实际替换")

    # 测试还原
    restored = mod.restore(de_text, mapping)
    print("🔓 还原:", restored)
    print(f"✅ 还原一致: {restored == test_text}")
else:
    print("ℹ️  LLM 路径未返回结果，回退正则测试：")
    # 回退正则
    de_text, mapping = mod.regex_desensitize(test_text)
    print("🔒 正则脱敏:", de_text)
    print("🗺️ 映射:", json.dumps(mapping, ensure_ascii=False, indent=2))
    if de_text == test_text:
        print("ℹ️  正则无匹配（可能无需脱敏）")
    else:
        restored = mod.restore(de_text, mapping)
        print("🔓 还原:", restored)
        print(f"✅ 还原一致: {restored == test_text}")

print("=" * 60)