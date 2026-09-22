# Hermes Desensitize

双层脱敏插件（LLM 语义层 + 正则回退层）：在把对话发给模型之前，把**中文语境下的身份与商业敏感信息**替换成可还原的占位符。

面向的场景是中文技术/商务对话——项目名、公司名、人名、职务、路径、产值数字混在一起。这类信息用纯正则识别不准，用纯 LLM 又不稳定，所以这里做成两层：LLM 负责语义判断，正则负责兜底和精确格式（手机号、身份证、邮箱、IP、路径）。

## 它解决什么问题

把一段工作对话贴给模型时，泄露的往往不是密码，而是这些：

```
本项目由北京某某科技有限公司承建，联系人张三（13812345678），
合同额 3.5 亿元，资料在 /home/alice/project/招标文件.pdf
```

脱敏后：

```
[本项目]由[公司1]承建，联系人[人物1]（[手机号码1]），
合同额 [xxx数量级1]，资料在 [路径1]
```

占位符**一一可还原**（`mapping` 里保存了原文），所以模型回答后你能把原文换回来——脱敏不损失可用性。

## 与同类插件的区别

存量插件覆盖的是另一类信息，彼此不重叠：

| 插件 | 覆盖 |
|---|---|
| vaultknox | secrets：API key / token / password（28 条模式，全英文键名格式） |
| cjk_sanitizer | 反向——把 LLM 输出里的中文字符**删掉** |
| **hermes-desensitize** | **中文语境 PII + 商业数据**：人名、公司/机构、地址、路径、产值数量级 |

换句话说：vaultknox 保护"凭证"，本插件保护"内容和身份"。

## 工作方式

```
用户消息 ──▶ pre_llm_call ──▶ 脱敏 ──▶ 发往模型
                                        │
                                    映射存内存
                                        │
模型回复 ◀── transform_llm_output ◀── 还原原文 ──▶ 你看到的是原文
```

- `pre_llm_call` / `pre_api_request`：出站消息脱敏
- `transform_llm_output`：入站回复还原
- `post_llm_call`：会话收尾清理

LLM 不可用、超时、或返回无法解析的 JSON 时，**自动回退纯正则层**（<1ms），对话不会因此中断。

## 安装

```bash
# 1. 插件本体
git clone https://github.com/yubingz/hermes-desensitize
cp -r hermes-desensitize/src/hermes_desensitize ~/.hermes/plugins/desensitize

# 2. 依赖（jieba 用于中文分词，PyYAML 读配置）
pip install jieba pyyaml

# 3. 启用
# 在 ~/.hermes/config.yaml 的 plugins.enabled 中加入 desensitize
```

## 配置

三层覆盖，后者赢：包内 `default_config.yaml` → `~/.hermes/desensitize.yaml` → 环境变量 `DESENSITIZE_*`。

最需要按自己情况改的是**公开实体白名单**——分词器会把知名公司/高校标成机构名，但它们属于公共信息，不该脱敏：

```yaml
# ~/.hermes/desensitize.yaml
public_entities:
  - 华为
  - 清华大学
  - 你所在的行业里那些不算隐私的名字

quantity:
  # 只模糊你真正在意的指标
  keywords: "产能|产量|产值|营收|销售额|利润|市场份额"

llm:
  provider: ollama
  model: qwen3:8b      # 换更强的模型能提高语义层召回
  timeout: 10
```

用 OpenAI 兼容端点（SiliconFlow / vLLM / LM Studio）：

```yaml
llm:
  provider: openai
  base_url: "https://api.siliconflow.cn/v1"
  model: "Qwen/Qwen2.5-7B-Instruct"
  api_key_env: SILICONFLOW_API_KEY   # 只写环境变量名，不写明文 key
```

## 会话内命令

```
/desensitize on          开启
/desensitize off         关闭
/desensitize status      查看当前配置与状态
/desensitize model qwen3:14b
/desensitize timeout 20
/desensitize chunk 4000
```

## 已知边界

诚实说明，避免误用：

- **正则层可能误伤。** 例如形似身份证的 18 位数字串会被替换。可通过 `path_masking.patterns` / `public_entities` 调优。
- **数量级触发词必须显式配置。** 只有列在 `quantity.keywords` 里的指标词才会被模糊，默认表只覆盖
  `产值/营收/销售额/利润/市场份额` 等常见项（另含 `合同额/投资额/预算/成本`）。你自己的行业术语
  若不在表里，会**静默漏脱敏**——不报错、只是不替换。部署前务必按自己场景过一遍这一节。
- **公司名靠"任意汉字 + 后缀"匹配，边界会有取舍。** 正则先贪婪匹配再剥掉开头的引导虚词
  （由/是/为/对/向/从/与/和/及/在），所以 `本项目由北京某某科技有限公司承建` 会得到
  `由` + `[公司1]`。带修饰语的写法（如 `原北京某某有限公司`）会把修饰语并入公司名——
  占位符仍可完整还原，不影响脱敏安全性，只是还原出的名字范围偏大。
- **占位符映射只存在内存中，不落盘。** 这是刻意的——脱敏映射本身就是敏感数据。

## 测试

```bash
cd hermes-desensitize
python tests/test_config.py            # 三层配置覆盖 + 坏 YAML 容错
python tests/test_plugin_behavior.py   # 配置热生效 + register() + 命令
python tests/test_regex_regression.py  # 正则层逐类回归
python tests/test_behavior_parity.py   # 与原始版逐条行为对照
```

`test_behavior_parity.py` 会把 15 条固定输入的输出与原始单文件版本逐条对照，用来证明"配置外置重构没有改变脱敏行为"。

## License

MIT
