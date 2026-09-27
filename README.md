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

### 升级

包内容整目录替换即可：

```bash
git -C hermes-desensitize pull
rm -rf ~/.hermes/plugins/desensitize
cp -r hermes-desensitize/src/hermes_desensitize ~/.hermes/plugins/desensitize
```

**备份不要放在 `~/.hermes/plugins/` 里面。** Hermes 会把该目录下**每个**含
`plugin.yaml` 的子目录都当成一个插件加载，两个同 `name` 的目录会产生冲突 ——
加载到哪个不确定，`hermes` 里看到的版本号可能是旧的。要备份就放到别处：

```bash
cp -r ~/.hermes/plugins/desensitize ~/.hermes/plugin_backups/desensitize_$(date +%F)
```

升级/改代码后若命令行为没变，先确认加载的是哪一份：

```python
from hermes_cli.plugins import PluginManager
pm = PluginManager(); pm.discover_and_load()
print([(p['key'], p['version']) for p in pm.list_plugins() if 'desens' in p['key']])
```

版本号应是 `plugin.yaml` 里的值；若看到旧版本号或看到两个条目，就是有残留副本。

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

ui:
  # 命令输出语言：both（默认，英中双语）/ en（纯英文）/ zh（纯中文）
  # 只影响 /desensitize 的文字，不影响脱敏行为。未知值回落 both。
  language: both
  # help（未知子命令的输出）的语言，默认 en。与 language 分开：help 面向第一次
  # 使用、还没配语言的人，用英文保证读不了中文的人也看得懂。
  help_language: en
```

`ui.language` 也可以用环境变量设：`DESENSITIZE_UI__LANGUAGE=en`（`help_language` 同理：`DESENSITIZE_UI__HELP_LANGUAGE`）。

命令反馈是这个插件唯一对非中文使用者可见的界面，所以三种取值都给：

| 取值 | `status` 首行 |
|---|---|
| `both`（默认） | `Status / 脱敏状态: ...` |
| `en` | `Status: ...` |
| `zh` | `脱敏状态: ...` |

默认 `both` 而不是纯英文，是因为双语不丢任何读者（改动前只有中文）。**会话内可用 `/desensitize lang en|zh|both` 临时切换**（不落盘；本次运行有效，重启 Hermes 后回到配置值）。也接受别名：`cn` / `中文` → `zh`，`english` / `英文` → `en`，`双语` → `both`。传入无法识别的值时**保持原值**（不会把已经是 `zh` 的用户降级回 `both`，回执也如实报告保持的是哪个值）。

**help 单独用 `help_language`（默认 `en`）**：第一次用的人如果读不了中文，打开帮助时最需要的就是看得懂 —— 所以 help 默认英文，在用户还没表态时不受 `ui.language=zh` 影响。一旦用户**显式**用过 `/desensitize lang <x>`，help 就跟该选择走；否则「设了 zh、再看 help 还是满屏英文」会被当成命令没生效。

未知子命令先给一行提示、再给用法块。提示行走**会话语言**并带近似建议（`Unknown subcommand 'sta' — did you mean 'status'?`），用法块仍走 `help_language`；改动前是静默打印用法 —— 把 `lang` 打成 `leng` 的用户只会看到一屏英文，不知道自己打错了。

**注意 `[xxx数量级N]` 这类占位符是脱敏时真正写入文本的值，任何语言下都保持原样**——否则还原会失配。

用 OpenAI 兼容端点（SiliconFlow / vLLM / LM Studio）：

```yaml
llm:
  provider: openai
  base_url: "https://api.siliconflow.cn/v1"
  model: "Qwen/Qwen2.5-7B-Instruct"
  api_key_env: SILICONFLOW_API_KEY   # 只写环境变量名，不写明文 key
```

### 数据出口（务必先读）

**语义脱敏层需要把「尚未脱敏的原文」发给 LLM**——它必须先看到原文才能认出人名/公司名。这是设计使然，不是 bug，但你必须知道自己把数据发到了哪里：

| 配置 | 原文去向 | 是否出本机 |
|---|---|---|
| `provider: ollama`（默认） | 本机 `OLLAMA_HOST`（默认 `localhost:11434`） | 否 |
| `provider: openai` + 设了 `base_url` | 你指定的端点 | 取决于端点 |
| `provider: openai` **未设 `base_url`** | 回退到 `OPENAI_BASE_URL`，两者都没有则**回退到 `https://api.siliconflow.cn/v1`** | **是** |

第三行是容易踩的坑：只要你设了 `provider: openai` 而忘了 `base_url`，原文（含真实人名、公司名、金额）就会发往硅基流动的云端。想保证不出本机，二选一：

- 保持 `provider: ollama`（默认）；
- 或设 `provider: openai` 时**同时设 `base_url`** 指向你信任的端点（本地 vLLM / LM Studio 填 `http://localhost:8000/v1`）。

`/desensitize status` 会打印实际生效的端点和 Egress 标记，发数据前可先核对。若 LLM 不可达，插件按 `behavior.regex_fallback` 回退到纯正则脱敏（不出本机）。

## 会话内命令

命令的所有反馈（`on`/`off`/`model`/`timeout`/`chunk` 的确认、`status` 的字段标签、
错误提示、用法列表）都是**英中双语**，英文在前：

```
/desensitize on          开启 / enable
/desensitize off         关闭 / disable
/desensitize status      查看当前配置与状态 / show status, including egress endpoint
/desensitize model qwen3:14b
/desensitize timeout 20
/desensitize chunk 4000
```

命令反馈是插件唯一对非中文使用者可见的界面，因此不随 README 走中文单语 —— 只给中文
会让这条路径对英文用户不可读。README 本身仍以中文为主（面向的场景就是中文对话）。

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
