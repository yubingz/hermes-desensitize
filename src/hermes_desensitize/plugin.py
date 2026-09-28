"""脱敏插件 v5 — 钩子方案（本地 LLM 主脱敏 + 正则超时回退）

架构：
  pre_llm_call:  → 本地 LLM（Ollama）语义识别敏感信息并替换
                  → 超时或解析失败 → 正规则则回退（<1ms）
                  → 长文本自动分段（2000 字/段）
                  → 本体意识：本公司 → [本公司], 本人 → [本人], 本项目 → [本项目]
                  → 项目产值/市场 → [xxx数量级]
  transform_llm_output: 还原最终响应中的占位符
  post_llm_call: 恢复会话历史原文，确保会话 DB 存储原文

不依赖外部代理，不改 base_url，始终可用。
"""

import difflib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Optional

# 相对导入：作为 Hermes 目录插件加载时，本模块的包名是 hermes_plugins.desensitize，
# 绝对导入 "hermes_desensitize" 会 ModuleNotFoundError（pip 安装场景才存在该顶层包）。
from . import config as _cfg

log = logging.getLogger("hermes_plugin.desensitize")

# ── 运行时配置 ──
# 三层来源（后者覆盖前者）：
#   1. 包内默认值 src/hermes_desensitize/default_config.yaml
#   2. 用户文件 ~/.hermes/desensitize.yaml
#   3. DESENSITIZE_* 环境变量（嵌套用双下划线，如 DESENSITIZE_LLM__TIMEOUT）
# 下面这些模块级变量是第 4 层：**会话内临时覆盖**（/desensitize 命令写，
# 不落盘、重启即失效）。启动时由 _sync_from_config() 从上面三层填充。
_enabled = True
# 命令输出语言：both（英中双语，默认）/ en（纯英文）/ zh（纯中文）。
# 只影响 /desensitize 的文字，不影响脱敏行为。
_UI_LANG = "both"
# help 输出的语言。与 ui.language 分开：help 面向「还不知道怎么用」的人（第一次
# 用、语言还没配），默认 en 保证读不了中文的人打开帮助时看得懂。可选 en/zh/both。
_HELP_LANG = "en"
# 会话内是否用 /desensitize lang 显式选过语言。选过后 help 也跟随该语言：
# 「help 默认英文」的理由是「语言还没配时也要看得懂」，用户明确表态后理由不成立。
_HELP_LANG_EXPLICIT = False


def L(en: str, zh: str) -> str:
    """按 ui.language 选择命令输出文字。

    ``L("Engine", "主引擎")`` → both 时 "Engine / 主引擎"，en 时 "Engine"，zh 时 "主引擎"。
    命令反馈是这个插件唯一对非中文使用者可见的界面，所以必须可选纯英文；
    双语作为默认，是因为它不丢任何读者（改动前只有中文）。
    """
    return _pick(_UI_LANG, en, zh)


def _HELP_L(en: str, zh: str) -> str:
    """按 help_language 选择 help 文字（默认 en，见 :data:`_HELP_LANG`）。

    会话内用 `/desensitize lang` 显式选过语言后，help 跟随该语言而不是
    help_language —— 否则用户设了 zh、再看 help 还是满屏英文，像「没生效」。
    """
    return _pick(_UI_LANG if _HELP_LANG_EXPLICIT else _HELP_LANG, en, zh)


def _pick(lang: str, en: str, zh: str) -> str:
    """三态取值。无法识别时按 both 渲染（调用方应先用 _norm_lang 校验）。"""
    if lang == "en":
        return en
    if lang == "zh":
        return zh
    return f"{en} / {zh}" if en and zh else (en or zh)


def _norm_lang(value: Any, default: str) -> str:
    """校验语言取值；无法识别时告警并回落。"""
    lang = str(value or default).strip().lower()
    if lang not in ("both", "en", "zh"):
        log.warning("desensitize: 语言值 %r 无法识别，回退 %s（可选 en/zh/both）", lang, default)
        return default
    return lang


# /desensitize lang 的输入别名。用户自然会写 cn / 中文 / english —— 只认 zh 会
# 让人以为「命令没生效」（实测笔误 leng cn 静默回落到 help）。取值仍是规范三元组。
_LANG_ALIASES = {
    "both": "both", "双语": "both", "中英": "both", "bilingual": "both",
    "en": "en", "english": "en", "英文": "en",
    "zh": "zh", "cn": "zh", "chinese": "zh", "中文": "zh",
}


def _sync_from_config() -> None:
    """把配置层的值同步到模块级变量。register() 时调用一次。"""
    global _LLM_PROVIDER, _LLM_MODEL, _LLM_TIMEOUT, _CHUNK_MAX_CHARS
    global _SELF_PATTERNS, _PUBLIC_ENTITIES, _QUANTITY_KEYWORDS, _PATH_PATTERNS
    global _REGEX_FALLBACK, _UI_LANG, _HELP_LANG, _HELP_LANG_EXPLICIT

    _enabled = bool(_cfg.get("behavior.enabled_by_default", True))
    _REGEX_FALLBACK = bool(_cfg.get("behavior.regex_fallback", True))
    _UI_LANG = _norm_lang(_cfg.get("ui.language", "both"), "both")
    _HELP_LANG = _norm_lang(_cfg.get("ui.help_language", "en"), "en")
    _HELP_LANG_EXPLICIT = False  # 会话内显式选择由 /desensitize lang 设置
    _LLM_PROVIDER = str(_cfg.get("llm.provider", "ollama"))
    _LLM_MODEL = str(_cfg.get("llm.model", "qwen3:8b"))
    _LLM_TIMEOUT = int(_cfg.get("llm.timeout", 10))
    _CHUNK_MAX_CHARS = int(_cfg.get("chunk_max_chars", 2000))

    sp = _cfg.get("self_patterns", [])
    if sp:
        _SELF_PATTERNS = [(p[0], p[1]) for p in sp if isinstance(p, (list, tuple)) and len(p) == 2]
    pe = _cfg.get("public_entities", [])
    if pe:
        _PUBLIC_ENTITIES = set(pe)

    qk = _cfg.get("quantity.keywords", "")
    if qk:
        _QUANTITY_KEYWORDS = str(qk)
    _QUANTITY_ENABLED = bool(_cfg.get("quantity.enabled", True))

    if _cfg.get("path_masking.enabled", True):
        pps = _cfg.get("path_masking.patterns", [])
        if pps:
            _PATH_PATTERNS = [re.compile(p) for p in pps]


_LLM_PROVIDER = "ollama"            # "ollama" | "openai"
_LLM_MODEL = "qwen3:8b"
_LLM_TIMEOUT = 10
_CHUNK_MAX_CHARS = 2000
_REGEX_FALLBACK = True
_QUANTITY_ENABLED = True
_QUANTITY_KEYWORDS = "产能|产量|产值|年产|月产|日产|营收|销售额|利润|市场份额|市值|估值"
_PATH_PATTERNS: list = []

_OLLAMA_BASE: Optional[str] = None  # Ollama 地址（仅 provider=ollama 时使用）
_SILICONFLOW_BASE = "https://api.siliconflow.cn/v1"


def _api_key() -> str:
    """按配置读 API key 的环境变量。不落盘、不硬编码 key 本身。"""
    env_name = str(_cfg.get("llm.api_key_env", "OPENAI_API_KEY"))
    return os.environ.get(env_name, "")


def _ollama_host() -> str:
    """Ollama 地址：配置 > OLLAMA_HOST > 本机默认。"""
    host = str(_cfg.get("llm.ollama_host", "") or os.environ.get("OLLAMA_HOST", ""))
    return host or "http://127.0.0.1:11434"

# ── Per-session 映射缓存 ──
# _mappings[session_id] = {"[本公司]": "原文", ...}
_mappings: dict[str, dict[str, str]] = {}

# ── Per-session 脱敏结果缓存 ──
# _desensitize_cache[session_id][content_hash] = (de_content, part_map)
# 会话历史消息在 post_llm_call 后被还原为原文，下次 pre_llm_call 会再次
# 看到同样的内容 —— 内容哈希缓存让已脱敏过的消息零 LLM 调用（之前是每次
# API 调用都全量重跑 LLM 脱敏，长会话一次工具循环前能卡 100 秒）。
# 与 _mappings 一起在 post_llm_call 后清理（_mappings.pop 处同步 pop）。
_desensitize_cache: dict[str, dict[str, tuple[str, dict]]] = {}

# ── Per-message 还原索引 ──
# _restore_index[session_id][de_content_hash] = part_map
# LLM 逐条消息独立编号，两条消息可能生成相同占位符（如都叫 [手机号码1]），
# 全局 _mappings 合并时后写覆盖先写 → restore 用全局 mapping 还原会把
# 第一条消息的手机号还原成第二条的。所以每条消息还原时用**自己的** mapping：
# 脱敏时记录 de_content_hash → part_map，restore 时按消息内容哈希查表。
_restore_index: dict[str, dict[str, dict]] = {}

# 内容哈希：整条消息内容 → 稳定短哈希
import hashlib as _hashlib


def _content_hash(content: str) -> str:
    return _hashlib.md5(content.encode("utf-8", errors="replace")).hexdigest()

# ── 本体占位符（固定，不编号）──
# 这些是对话上下文自明的引用，不需要模糊处理。
# 启动时由 _sync_from_config() 从 default_config.yaml 的 self_patterns 覆盖。
_SELF_PATTERNS: list[tuple[str, str]] = [
    (r'我们公司|我公司|我司|本公司', '[本公司]'),
    (r'我自己|我本人|本人', '[本人]'),
    (r'这个项目|此项目|本项目', '[本项目]'),
]

# 公开知名实体 — 分词器可能标为人名/机构名，但属于公共信息不脱敏。
# 启动时由 _sync_from_config() 从 default_config.yaml 的 public_entities 覆盖。
# 这里的内置表是"配置缺失时的保底"，用户应在 ~/.hermes/desensitize.yaml 里
# 按自己的国家/行业替换（比如加入本国运营商、监管机构、常见合作方）。
_PUBLIC_ENTITIES: set[str] = {
    '华为', '华为技术有限公司',
    '腾讯', '阿里', '阿里巴巴', '百度', '字节跳动', '京东', '小米', '网易', '美团',
    '中兴', '中兴通讯', '三星', '苹果', '微软', '谷歌',
    '亚马逊', 'Meta', 'OpenAI',
    '特斯拉', '英伟达', '英特尔', '高通', 'IBM', 'Oracle', 'SAP',
    '中国移动', '中国联通', '中国电信', '中国铁塔',
    '清华大学', '北京大学', '浙江大学', '上海交通大学', '复旦大学',
    '哈尔滨工业大学', '西安交通大学',
    'IEEE', '3GPP', 'ITU', 'ETSI', '5G', '4G', '3G',
}

# ──────────────────────────────────────────────
# 长文本分段
# ──────────────────────────────────────────────

def _chunk_text(text: str, max_chars: int = _CHUNK_MAX_CHARS) -> list[str]:
    """将长文本按句子边界分段，每段不超过 max_chars"""
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    # 先按句子分割（中文句号、问号、感叹号、分号、换行）
    parts = re.split(r'(?<=[。！？；\n])\s*', text)
    chunks: list[str] = []
    current = ""

    for part in parts:
        if not part.strip():
            continue
        if len(current) + len(part) <= max_chars:
            current += part
        else:
            if current:
                chunks.append(current.strip())
            # 如果 single part 本身就超长，强行截断
            if len(part) > max_chars:
                while len(part) > max_chars:
                    chunks.append(part[:max_chars])
                    part = part[max_chars:]
                current = part
            else:
                current = part

    if current.strip():
        chunks.append(current.strip())

    # 如果没有成功分割（比如纯长文本无标点），按字符硬切
    if not chunks:
        for i in range(0, len(text), max_chars):
            chunks.append(text[i:i + max_chars])

    return chunks


# ──────────────────────────────────────────────
# Ollama 地址检测
# ──────────────────────────────────────────────

def _detect_ollama_host() -> str:
    candidates = [
        "http://localhost:11434",    # 本机 Ollama（最常见部署，优先探测）
        "http://[内网IP1]:11434",    # WSL 默认网关（Windows 主机）
        "http://host.docker.internal:11434",
    ]
    for url in candidates:
        try:
            req = urllib.request.Request(f"{url}/api/tags")
            resp = urllib.request.urlopen(req, timeout=2)
            if resp.status == 200:
                return url
        except Exception:
            continue
    return candidates[0]  # 默认


# ──────────────────────────────────────────────
# 正规则则（回退用）
# ──────────────────────────────────────────────

# 自引用匹配：在全局匹配前先处理
_SELF_RE = re.compile(
    r'(?:我们公司|我公司|我司|本公司'
    r'|我自己|我本人|本人'
    r'|这个项目|此项目|本项目)'
)

# 数量级模式：产值/市场/产能等数据 → [数量级N]。关键词从配置读取（quantity.keywords）。
# 注意：正则必须是**函数**而非模块级常量 —— 因为 _QUANTITY_KEYWORDS 会被
# _sync_from_config() 在 register() 时覆盖，模块级 re.compile 会冻住旧值。
def _quantity_re() -> re.Pattern:
    return re.compile(
        r'(?:' + _QUANTITY_KEYWORDS + r')'
        r'.{0,30}?\d+[.,]?\d*\s*'
        r'(?:GWh|MWh|kWh|GW|MW|kW|亿元|万元|亿美元|欧元|美元|公斤|吨'
        r'|台|件|套|斤|辆|架|艘|只|条|批|次|亿|万|元|%|％)'
    )

PATTERNS: list[tuple[str, str]] = [
    # 中文公司/机构名
    # 注意：CJK 区间的转义写成 `\\u4e00` 会变成"字面反斜杠 + u4e00"，
    # 整个字符类再也匹配不到汉字（原文件里的既有 bug）。
    # 高校那条用的是 `[一-龥]` 字面量，是对的；这里统一用字面量避免转义歧义。
    # [一-龥]{2,8} 是个贪心的"任意汉字"区间，会把前面的单字虚词一起吃进公司名。
    # 典型触发：文本先经过本体占位符替换（"本项目"→"[本项目]"），此时 "由" 前面
    # 不再是汉字，lookbehind 放行 → 匹配到 "由北京某某科技有限公司"。
    # 且 lookbehind 里的排除集若做成变量长 lookbehind，正则引擎会对每个起点回溯
    # 逐个试长度，代价高且行为难预测。稳妥做法：先以 [一-龥]{2,10} 匹配（多留余量），
    # 匹配后再剥掉开头的虚词（见"公司名清洗"）。
    (r"(?<![一-龥])[一-龥]{2,10}"
     r"(?:有限公司|集团公司|股份有限公司|有限责任公司"
     r"|研究院|研究所|设计院|设计研究院"
     r"|支行|分行|营业部|联社|总厂|分厂"
     r"|局|委员会|办公室|办公厅)", "org"),
    # 高校
    (r"(?<![一-龥])[一-龥]{2,8}(?:大学|学院|研究院|实验室|学校)", "school"),
    # 文件系统路径（含 WSL/Windows 盘符形态，由 _path_res() 的 _DEFAULT_PATH_PATTERNS 覆盖）
    ("(?:wsl|win|linux)_path", "path"),
    # 手机号
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "phone"),
    # 身份证号
    (r"(?<!\d)[1-9]\d{5}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?!\d)", "idcard"),
    # 邮箱
    (r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9]"
     r"(?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
     r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*\.[a-zA-Z]{2,}", "email"),
    # 中文地址：省市/市区/路巷门牌号
    (r"(?:[一-龥]{2,4}(?:省|自治区))?"
     r"[一-龥]{2,7}?(?:市|自治州)"
     r"[一-龥]{2,7}?(?:区|县|镇|乡|街道)"
     r"(?:[一-龥0-9]{1,8}(?:路|街|道|巷|弄|号|村|小区|栋|单元|室))?"
     r"(?![一-龥0-9])", "address"),
    # 内网 IP
    (r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
     r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
     r"|192\.168\.\d{1,3}\.\d{1,3}"
     r"|127\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", "ip"),
]
_DEFAULT_PATH_PATTERNS = [
    r"/home/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.\-/]+)?",
    r"/Users/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.\-/]+)?",
]


def _path_res() -> list[re.Pattern]:
    """路径脱敏正则。默认覆盖 Linux/macOS 家目录，可由配置 path_masking.patterns 覆盖。

    返回列表（配置可给多条），用 re.search 逐个试。
    """
    pats = _PATH_PATTERNS or [re.compile(p) for p in _DEFAULT_PATH_PATTERNS]
    return pats

# ── jieba 自定义词典（脱敏专用）──
_JIEBA_DICT_PATH = os.path.join(os.path.dirname(__file__), 'jieba_dict.txt')
if os.path.exists(_JIEBA_DICT_PATH):
    import jieba
    jieba.load_userdict(_JIEBA_DICT_PATH)

# ── 类型前缀映射（用于占位符命名）──
TYPE_TO_CN: dict[str, str] = {
    "org": "公司",
    "school": "学校",
    "address": "地址",
    "wsl_path": "路径",
    "win_path": "路径",
    "phone": "手机号码",
    "idcard": "身份证",
    "email": "邮箱",
    "ip": "内网IP",
    "linux_path": "路径",
    "path": "路径",
    "person": "人物",
    "org_jieba": "公司",
}
_CN_TYPES = set(TYPE_TO_CN.values()) | {"人物", "项目", "本公司", "本人", "本项目", "xxx数量级1", "xxx数量级2", "xxx数量级3"}

# ── 重叠优先级 ──
# 原插件把路径脱敏放在**独立一轮**（先路径、后通用模式），因此路径总是赢。
# 合并进单轮匹配后必须显式表达这个次序，否则更长的匹配会被同起点的短匹配挤掉
# （路径 4-32 输给 4-23，尾部残余变成裸文本）。
_PATTERN_PRIORITY: dict[str, int] = {
    "wsl_path": 100, "win_path": 100, "linux_path": 100, "paths": 100,
    "email": 90, "idcard": 90, "phone": 90, "ip": 90,
    "org": 50, "school": 50, "address": 50, "org_jieba": 50, "person": 60,
}

# 公司名正则的引导虚词：这些字若出现在匹配串开头，是从前面的句子成分里被
# "任意汉字"区间误吃的，需剥掉（见 regex_desensitize 里的清洗逻辑）。
_ORG_LEAD_STRIP = "由是为对向从与和及在"


# ──────────────────────────────────────────────
# 核心函数
# ──────────────────────────────────────────────

def _apply_self_placeholders(text: str, mapping: dict) -> str:
    """处理本体引用：本公司→[本公司], 本人→[本人], 本项目→[本项目]"""
    result = text
    for pattern, ph in _SELF_PATTERNS:
        for m in re.finditer(pattern, result):
            mapping.setdefault(ph, m.group())
            result = result[:m.start()] + ph + result[m.end():]
            break  # 每类只替换第一处
    return result


def _apply_quantity_placeholders(text: str, mapping: dict, counter: list) -> str:
    """将产值/市场等数据替换为 [xxx数量级N]（编号保还原）"""
    result = text
    offset = 0
    qty_n = 0
    for m in _quantity_re().finditer(text):
        matched = m.group()
        qty_n += 1
        ph = f"[xxx数量级{qty_n}]"
        mapping[ph] = matched
        start = m.start() + offset
        end = m.end() + offset
        result = result[:start] + ph + result[end:]
        offset += len(ph) - len(matched)
    return result


def regex_desensitize(text: str) -> tuple[str, dict]:
    """纯正则脱敏 — <1ms，精确映射（超时回退用）

    占位符格式: [本公司], [本人], [本项目], [公司1], [手机号码2], [xxx数量级]...
    相同原文复用同一占位符。
    """
    if not text:
        return text, {}

    mapping: dict[str, str] = {}
    result = text

    # 第1步：本体引用（提前处理，防止被公司名正则误吞）
    result = _apply_self_placeholders(result, mapping)

    # 第2步：数量级数据（提前处理，防止截断）
    result = _apply_quantity_placeholders(result, mapping, [0])

    # 第3步：jieba 分词 + 词性标注 — 识别人名(nr)/机构名(nt)
    try:
        import jieba.posseg as pseg
        words = list(pseg.cut(result))
        jieba_matches: list[tuple[int, int, str, str]] = []
        for word, flag in words:
            if flag in ("nr", "nr1", "nr2", "nrf"):  # 人名
                # 跳过已被之前步骤替换的区域
                if "[" in word or "]" in word:
                    continue
                # 跳过单字人名（jieba 经常把单个字标为人名）
                if len(word) <= 1:
                    continue
                # 跳过公开知名实体
                if word in _PUBLIC_ENTITIES:
                    continue
                idx = result.find(word)
                if idx >= 0:
                    jieba_matches.append((idx, idx + len(word), word, "person"))
            elif flag == "nt":
                # 跳过公开知名实体
                if word in _PUBLIC_ENTITIES:
                    continue  # 机构名
                if "[" in word or "]" in word:
                    continue
                idx = result.find(word)
                if idx >= 0:
                    jieba_matches.append((idx, idx + len(word), word, "org_jieba"))
        # 合并到 all_matches（排序+去重由后续统一处理）
    except ImportError:
        jieba_matches = []
    except Exception:
        jieba_matches = []

    # 第4步：标准正则模式
    text_to_ph: dict[str, str] = {}
    counter = 0
    all_matches: list[tuple[int, int, str, str]] = list(jieba_matches)

    for pattern, ptype in PATTERNS:
        for m in re.finditer(pattern, result):
            text_m, start_m, end_m = m.group(), m.start(), m.end()
            if ptype == "org":
                # 剥掉被贪心区间吃进来的引导虚词（"由/是/为/对/向/从/与/和/及/在"）。
                # 只剥开头，且保证剥完还剩 ≥2 个汉字，避免把公司名剥空。
                while text_m and text_m[0] in _ORG_LEAD_STRIP and len(text_m) - 1 >= 2:
                    text_m = text_m[1:]
                    start_m += 1
                if len(text_m) < 2:
                    continue
            all_matches.append((start_m, end_m, text_m, ptype))

    for _pre in _path_res():
        for m in _pre.finditer(result):
            # ptype 必须是 TYPE_TO_CN 里的键，否则占位符退化成 [敏感N]
            # （原插件此处写作 "paths"，因不在表中而丢掉了 [路径N] 标签）
            all_matches.append((m.start(), m.end(), m.group(), "path"))

    if not all_matches:
        return result, mapping

    # 排序 + 去重叠（同起点取最长；再按类型优先级打破交叉，路径优先于通用模式）
    all_matches.sort(key=lambda x: (x[0], -x[1]))
    deduped: list[tuple[int, int, str, str]] = []
    for start, end, matched, ptype in all_matches:
        # 与已接受区间重叠时：高优先级类型可取而代之（原插件里路径是独立一轮、优先级更高）
        overlapped = None
        for i, (s0, e0, _m0, pt0) in enumerate(deduped):
            if start < e0 and end > s0:      # 真重叠（不共用端点）
                overlapped = i
                break
        if overlapped is None:
            deduped.append((start, end, matched, ptype))
            continue
        if _PATTERN_PRIORITY.get(ptype, 0) > _PATTERN_PRIORITY.get(deduped[overlapped][3], 0):
            deduped[overlapped] = (start, end, matched, ptype)
    deduped.sort(key=lambda x: x[0])

    for start, end, matched, ptype in reversed(deduped):
        # 跳过公开实体（华为/北斗等公共名称不脱敏）
        if any(pe in matched or matched in pe for pe in _PUBLIC_ENTITIES):
            continue
        if matched in text_to_ph:
            ph = text_to_ph[matched]
        else:
            counter += 1
            cn_prefix = TYPE_TO_CN.get(ptype, "敏感")
            ph = f"[{cn_prefix}{counter}]"
            mapping[ph] = matched
            text_to_ph[matched] = ph
        result = result[:start] + ph + result[end:]

    return result, mapping


# ──────────────────────────────────────────────
# LLM 调用（支持 Ollama / SiliconFlow）
# ──────────────────────────────────────────────

_PROMPT_TEMPLATE = (
    "Replace sensitive Chinese text with placeholders. "
    "Output ONLY valid JSON, no markdown.\n"
    "Rules:\n"
    "- Self refs (我们公司/我司/本公司/本人/本项目) → "
    "[本公司], [本人], [本项目] (fixed)\n"
    "- Business data (销售额/产值/营收/产能/利润/市场份额) "
    "→ [xxx数量级1], [xxx数量级2] (numbered)\n"
    "- Person names → [人物1], [人物2] (numbered)\n"
    "- Company/org names → [公司1], [公司2] (numbered)\n"
    "- Phone/ID/email/IP/paths → "
    "[手机号码1], [身份证1], [邮箱1], [内网IP1], [路径1] (numbered)\n"
    "- Same text → same number\n"
    "- DO NOT touch: public companies (华为,腾讯,阿里,百度,字节跳动,京东,小米,网易,美团), "
    "technical terms, published papers, power station names\n"
    "- IMPORTANT: mapping value MUST be the EXACT text that was replaced "
    "(the whole substring from original that becomes the placeholder)\n"
    "- IMPORTANT 2: ALL placeholders MUST appear in the mapping dict\n\n"
    '{"transformed": "text with [本公司] and [xxx数量级1]", '
    '"mapping": {"[本公司]": "我们公司", "[xxx数量级1]": "销售额达到8000万元"}}\n\n'
    "Text:\n"
)


def _parse_llm_response(content: str, elapsed: float, source: str,
                        original: str = "") -> tuple[str, dict] | None:
    """解析 LLM 返回的 JSON 并验证"""
    json_match = re.search(
        r'\{\s*"transformed"[\s\S]*"mapping"[\s\S]*\}', content
    )
    if not json_match:
        log.warning("%s 响应未含有效 JSON (%.1fs)，回退正则", source, elapsed)
        return None

    result = json.loads(json_match.group())
    mapping = result.get("mapping", {})

    # 无映射 → 无需脱敏（LLM 判定无敏感项），直接返回原文
    if not mapping:
        log.info("%s 脱敏成功 (0 项, %.1fs)", source, elapsed)
        return original, {}

    # 验证占位符格式（以 mapping 的键为准，不再信任 transformed 字段）
    known_phs = {"[本公司]", "[本人]", "[本项目]"}
    pls = set(mapping.keys()) - known_phs
    # 数字占位符必须有编号（[人物1] 可，[人物] 不可）
    unnumbered = [p for p in pls if not re.search(r"\d+\]", p)]
    if unnumbered:
        log.warning(
            "%s 占位符缺少编号: %s (%.1fs)，回退正则",
            source, ",".join(sorted(unnumbered)), elapsed,
        )
        return None

    # 验证映射值是否为原始文本的确切子串
    paraphrased = []
    for ph, val in mapping.items():
        if val and val not in original:
            paraphrased.append((ph, val))
    if paraphrased:
        log.warning(
            "%s 改写了 %d 个映射值 (%.1fs)，回退正则",
            source, len(paraphrased), elapsed,
        )
        return None

    # 重建 transformed：不信任 LLM 手写的 transformed 字段（它经常只替换
    # 部分文本，如把 "销售额达到8000万元" 替换成 "销售额达到[xxx数量级1]"，
    # 导致还原后原文被污染 "销售额达到销售额达到8000万元"），而是用
    # mapping 对原文做精确替换。LLM 的价值在语义识别（哪些是敏感项、
    # 归哪类、编号），替换动作由代码保证精确。
    #
    # 长映射值先替换，避免短值误替换长值的子串（如 [手机号码] 的值
    # 13812345678 与 [身份证] 的值 110101...12345678 同尾）。
    rebuilt = original
    for ph, val in sorted(mapping.items(), key=lambda kv: len(kv[1]), reverse=True):
        if val:
            rebuilt = rebuilt.replace(val, ph)

    # 重建后必须真的发生了替换（mapping 非空且有效），且所有占位符
    # 都出现在重建结果中 —— 否则说明 mapping 与原文对不上，回退正则。
    if mapping and rebuilt == original:
        log.warning(
            "%s 映射值未在原文中命中任何替换 (%.1fs)，回退正则",
            source, elapsed,
        )
        return None
    if mapping:
        absent = [ph for ph in mapping if ph not in rebuilt]
        if absent:
            log.warning(
                "%s 重建后 %d 个占位符缺失 (%.1fs)，回退正则",
                source, len(absent), elapsed,
            )
            return None

    # 最终一致性保证：重建结果还原后必须等于原文（缓存依赖此不变量）
    if restore(rebuilt, mapping) != original:
        log.warning(
            "%s 重建结果无法精确还原原文 (%.1fs)，回退正则",
            source, elapsed,
        )
        return None

    log.info("%s 脱敏成功 (%d 项, %.1fs)", source, len(mapping), elapsed)
    return rebuilt, mapping


def _call_openai_compat(text: str, timeout: int) -> tuple[str, dict] | None:
    """调用任何 OpenAI 兼容的 /chat/completions 端点。

    base_url 优先读配置 llm.base_url，其次 OPENAI_BASE_URL，最后回退 SiliconFlow。
    key 从 llm.api_key_env 指定的环境变量读取（不落盘）。
    """
    base = str(_cfg.get("llm.base_url", "") or os.environ.get("OPENAI_BASE_URL", "")
               or _SILICONFLOW_BASE).rstrip("/")
    key = _api_key()
    if not key:
        env_name = str(_cfg.get("llm.api_key_env", "OPENAI_API_KEY"))
        log.warning("desensitize: 环境变量 %s 未设置，无法调用 %s", env_name, base)
        return None

    prompt = _PROMPT_TEMPLATE + text
    data = json.dumps({
        "model": _LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4096,
        "temperature": 0.1,
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{base}/chat/completions",
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        },
    )

    try:
        t0 = time.time()
        resp = urllib.request.urlopen(req, timeout=timeout)
        elapsed = time.time() - t0
        body = json.loads(resp.read())
        content = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        if not content:
            log.warning("SiliconFlow 返回空内容 (%.1fs)，回退正则", elapsed)
            return None
        return _parse_llm_response(content, elapsed, "SiliconFlow", text)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            log.warning("SiliconFlow 脱敏超时 (%ds)，回退正则", timeout)
        else:
            log.warning("SiliconFlow 脱敏连接失败: %s，回退正则", e)
    except json.JSONDecodeError as e:
        log.warning("SiliconFlow 脱敏 JSON 解析失败: %s，回退正则", e)
    except Exception as e:
        log.warning("SiliconFlow 脱敏异常: %s，回退正则", e)

    return None


def _call_ollama(text: str, timeout: int) -> tuple[str, dict] | None:
    """调用 Ollama 做一段文本的 LLM 脱敏"""
    global _OLLAMA_BASE
    if _OLLAMA_BASE is None:
        _OLLAMA_BASE = _detect_ollama_host()

    prompt = _PROMPT_TEMPLATE + text
    data = json.dumps({
        "model": _LLM_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "think": False,  # qwen3 默认开 thinking，脱敏任务不需要思维链，关掉提速
        "keep_alive": "300m",  # 模型常驻 5 小时，避免冷启动
        "options": {"num_predict": 4096, "temperature": 0.1},
    }).encode()

    req = urllib.request.Request(
        f"{_OLLAMA_BASE}/api/chat",
        data=data,
        headers={"Content-Type": "application/json"},
    )

    try:
        t0 = time.time()
        resp = urllib.request.urlopen(req, timeout=timeout)
        elapsed = time.time() - t0
        body = json.loads(resp.read())
        content = body.get("message", {}).get("content", "")
        if not content:
            log.warning("Ollama 返回空内容 (%.1fs)，回退正则", elapsed)
            return None
        return _parse_llm_response(content, elapsed, "Ollama", text)
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError):
            log.warning("Ollama 脱敏超时 (%ds)，回退正则", timeout)
        else:
            log.warning("Ollama 脱敏连接失败: %s，回退正则", e)
    except json.JSONDecodeError as e:
        log.warning("Ollama 脱敏 JSON 解析失败: %s，回退正则", e)
    except Exception as e:
        log.warning("Ollama 脱敏异常: %s，回退正则", e)

    return None


def _call_llm(text: str, timeout: int) -> tuple[str, dict] | None:
    """按 _LLM_PROVIDER 调用对应后端。

    "openai" = 任何 OpenAI 兼容端点（SiliconFlow / vLLM / LM Studio / DeepSeek ...）。
    "siliconflow" 是 v5 的旧名，保留兼容。
    """
    if _LLM_PROVIDER in ("openai", "siliconflow"):
        return _call_openai_compat(text, timeout)
    return _call_ollama(text, timeout)


def llm_desensitize(text: str) -> tuple[str, dict] | None:
    """LLM 语义脱敏 — 主路径（自动分段防超时）

    长文本自动拆分为 ≤2000 字的分段，每段独立调 LLM。
    根据 _LLM_PROVIDER 自动选择 Ollama 或 SiliconFlow。
    所有分段结果合并后返回。
    """
    chunks = _chunk_text(text)
    if not chunks:
        return None

    if len(chunks) == 1:
        return _call_llm(chunks[0], _LLM_TIMEOUT)

    # 多段 → 逐段处理，合并结果
    all_mapping: dict[str, str] = {}
    transformed_parts: list[str] = []
    per_chunk_timeout = max(10, _LLM_TIMEOUT // len(chunks))

    for i, chunk in enumerate(chunks):
        log.info("LLM 脱敏分段 %d/%d (%d 字)", i + 1, len(chunks), len(chunk))
        result = _call_llm(chunk, per_chunk_timeout)
        if result is not None:
            de_text, part_map = result
            transformed_parts.append(de_text)
            # 合并映射（冲突时保留第一个，同一个占位符在各段指同一原文）
            for ph, original in part_map.items():
                if ph not in all_mapping:
                    all_mapping[ph] = original
        else:
            # 该段 LLM 失败 → 正则回退
            log.info("分段 %d LLM 失败，回退正则", i + 1)
            de_text, part_map = regex_desensitize(chunk)
            transformed_parts.append(de_text)
            for ph, original in part_map.items():
                if ph not in all_mapping:
                    all_mapping[ph] = original

    merged = "".join(transformed_parts)
    return merged, all_mapping


def restore(text: str, mapping: dict) -> str:
    """将占位符替换回原文"""
    if not text or not mapping:
        return text
    result = text
    # 先替换长占位符（避免短占位符误替换长占位符的子串）
    for ph in sorted(mapping, key=len, reverse=True):
        val = mapping.get(ph, "")
        if not val:
            continue
        result = result.replace(ph, val)
    return result


def _desensitize_all_messages(messages: list[dict], session_id: str) -> None:
    """遍历所有消息：先试 LLM，失败回退正则。

    性能关键路径：本函数在每次 API 调用前触发，而会话历史在
    post_llm_call 后会被还原为原文 —— 同一个 content 会反复出现。
    因此：
      1. 内容哈希缓存：已脱敏过的消息零 LLM 调用（直接复用结果）
      2. 快筛：消息已含占位符（上轮脱敏产物）或长度过短/无敏感形态
         时跳过 LLM，直接走正则补充
    """
    if session_id not in _mappings:
        _mappings[session_id] = {}
    cache = _desensitize_cache.setdefault(session_id, {})
    rindex = _restore_index.setdefault(session_id, {})

    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        # 1) 内容哈希缓存命中 → 直接复用上次脱敏结果
        h = _content_hash(content)
        cached = cache.get(h)
        if cached is not None:
            de_content, part_map = cached
            if part_map:
                msg["content"] = de_content
                _mappings[session_id].update(part_map)
                rindex[_content_hash(de_content)] = part_map
            continue

        # 2) 快筛：消息已含占位符（[本公司]/[人物1]/[公司2] 等）说明是
        #    上一轮脱敏的产物或模型回复，跳过 LLM 避免重复编号
        if _looks_already_desensitized(content):
            de_content, part_map = regex_desensitize(content)
            if part_map:
                msg["content"] = de_content
                _mappings[session_id].update(part_map)
                rindex[_content_hash(de_content)] = part_map
            cache[h] = (msg.get("content", content), part_map)
            continue

        # 3) 优先本地 LLM 语义脱敏（自动分段）
        llm_result = llm_desensitize(content)
        if llm_result is not None:
            de_content, part_map = llm_result
        else:
            # LLM 失败或超时 → 正则回退
            de_content, part_map = regex_desensitize(content)

        if part_map:
            msg["content"] = de_content
            _mappings[session_id].update(part_map)
            rindex[_content_hash(de_content)] = part_map

        # 缓存脱敏结果（无论是否命中敏感项，下次直接复用）
        cache[h] = (de_content, part_map)


# 已含占位符/无敏感形态的消息快筛
_PLACEHOLDER_RE = re.compile(r"\[[一-龥A-Za-z0-9]{2,12}\]")


def _looks_already_desensitized(content: str) -> bool:
    """判断消息是否已脱敏过（含占位符）或明显无敏感信息。

    返回 True 表示不需要 LLM 脱敏（跳过），False 表示含敏感形态、
    值得交给 LLM 语义识别。
    """
    if not content:
        return True
    # 已含占位符 → 上轮脱敏产物或模型回复
    if _PLACEHOLDER_RE.search(content):
        return True
    # 正则快扫：无任何敏感形态 → 跳过 LLM
    # （assistant 回复、工具输出、纯代码等通常不含敏感信息，
    #   让它们走 LLM 是浪费——之前每条 3-30s）
    for pattern, _ptype in PATTERNS:
        if re.search(pattern, content):
            return False
    if any(_pre.search(content) for _pre in _path_res()):
        return False
    if _SELF_RE.search(content) or _quantity_re().search(content):
        return False
    # 手机号/身份证/邮箱等数字敏感项由 PATTERNS 覆盖；这里兜底常见数字形态
    if re.search(r"(?<!\d)\d{6,}(?!\d)", content):
        return False
    return True


def _restore_all_messages(messages: list[dict], session_id: str) -> None:
    """还原所有消息中的占位符（每条消息用**自己的** mapping，避免跨消息
    占位符编号冲突导致还原错乱）。"""
    rindex = _restore_index.get(session_id)
    if not rindex:
        # 无索引（老会话/直连路径）→ 退回全局 mapping
        mapping = _mappings.get(session_id)
        if not mapping:
            return
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                msg["content"] = restore(content, mapping)
        return
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            part_map = rindex.get(_content_hash(content))
            if part_map:
                msg["content"] = restore(content, part_map)


# ──────────────────────────────────────────────
# 钩子
# ──────────────────────────────────────────────

def _pre_llm_call(**kwargs: Any) -> Optional[str]:
    """pre_llm_call 钩子：原位脱敏所有消息"""
    if not _enabled:
        return None

    session_id = kwargs.get("session_id", "")
    conversation_history = kwargs.get("conversation_history", [])
    if not session_id or not conversation_history:
        return None

    _desensitize_all_messages(conversation_history, session_id)
    return None


def _transform_llm_output(**kwargs: Any) -> Optional[str]:
    """transform_llm_output 钩子：还原最终响应"""
    if not _enabled:
        return None

    response_text = kwargs.get("response_text", "")
    session_id = kwargs.get("session_id", "")
    if not response_text or not session_id:
        return None

    mapping = _mappings.get(session_id, {})
    if not mapping:
        return None

    restored = restore(response_text, mapping)
    return restored if restored != response_text else None


def _post_llm_call(**kwargs: Any) -> None:
    """post_llm_call 钩子：还原会话历史"""
    if not _enabled:
        return

    session_id = kwargs.get("session_id", "")
    conversation_history = kwargs.get("conversation_history", [])
    if not session_id or not conversation_history:
        return

    _restore_all_messages(conversation_history, session_id)
    _mappings.pop(session_id, None)
    # 注意：_desensitize_cache 故意不清 —— 它是跨 API 调用轮次的持久缓存。
    # 每次 API 调用前 pre_llm_call 都会看到同样的历史原文，靠它实现零 LLM
    # 调用。清理只在会话切换/插件关闭时做（见 _clear_session_cache）。


def _pre_api_request(**kwargs: Any) -> None:
    """pre_api_request 钩子：API 发出前最后一刻拦截

    仅处理含 <memory-context> 注入的消息（NOESIS 记忆预取等），
    这些是 pre_llm_call 无法覆盖的内容。

    只走正则路径（<1ms），不调 LLM。原因：
    - 对话消息已被 pre_llm_call 脱敏，再调 LLM 浪费且可能重新编号
    - 正则可抓 memory 块中带有后缀的公司/机构名（xx大学、xx研究院等）
    - 短名（国网、智芯）已通过 jieba 自定义词典覆盖
    """
    if not _enabled:
        return

    session_id = kwargs.get("session_id", "")
    request_messages = kwargs.get("request_messages", [])
    if not session_id or not request_messages:
        return

    if session_id not in _mappings:
        _mappings[session_id] = {}

    has_new = False
    for msg in request_messages:
        content = msg.get("content")
        if not isinstance(content, str) or "<memory-context>" not in content:
            continue

        # 只走正则，不调 LLM——不重复处理已脱敏内容，不改变编号
        de_content, part_map = regex_desensitize(content)

        if part_map:
            msg["content"] = de_content
            # 不覆盖已有映射（已脱敏的编号不变）
            for ph, val in part_map.items():
                if ph not in _mappings[session_id]:
                    _mappings[session_id][ph] = val
            has_new = True

    if has_new:
        log.info("pre_api_request: 正则补充 %d 项（memory-context 注入）",
                 len(_mappings.get(session_id, {})))


# ──────────────────────────────────────────────
# 命令处理器
# ──────────────────────────────────────────────

def _handle_desensitize(raw: str) -> Optional[str]:
    """处理 /desensitize on|off|status|model|timeout|chunk

    文字按 ui.language 输出（both / en / zh），见 :func:`L`。命令反馈是插件唯一对
    非中文使用者可见的界面；改动前一律只给中文，英文用户读到的是不可读的路径
    （review #122574 提出）。
    """
    global _enabled, _LLM_PROVIDER, _LLM_MODEL, _LLM_TIMEOUT, _CHUNK_MAX_CHARS, _OLLAMA_BASE
    global _UI_LANG, _HELP_LANG_EXPLICIT

    args = raw.strip().split()
    if not args:
        args = ["status"]

    cmd = args[0].lower()

    if cmd == "on":
        _enabled = False  # 保持关闭——需要 config.yaml 和重启才能启用
        return (
            f"{L('Desensitization enabled', '脱敏已启用')}\n"
            f"  {L('Provider', '提供方')}: {_LLM_PROVIDER}\n"
            f"  {L('Engine', '主引擎')}:   {_LLM_MODEL} ({L('timeout', '超时')} {_LLM_TIMEOUT}s)\n"
            f"  {L('Chunk', '分段')}:      ≤{_CHUNK_MAX_CHARS} {L('chars', '字')}\n"
            f"  {L('Fallback', '回退')}:   {L('regex rules', '正则规则')} ({len(PATTERNS)} {L('kinds', '类')}, <1ms)\n"
            f"  {L('Self', '本体')}:       {L('this-company/self/this-project → fixed placeholder', '本公司/本人/本项目 → 固定占位符')}\n"
            f"  {L('Magnitude', '数量级')}: {L('output/market figures → [xxx数量级]', '产值/市场等数据 → [xxx数量级]')}"
        )

    elif cmd == "off":
        _enabled = False
        _mappings.clear()
        _desensitize_cache.clear()
        _restore_index.clear()
        return L("Desensitization disabled — original text goes to the model unredacted",
                 "脱敏已关闭，消息原文直发云端")

    elif cmd == "model":
        if len(args) < 2:
            return (
                f"{L('Current provider', '当前提供方')}: {_LLM_PROVIDER}\n"
                f"{L('Current model', '当前模型')}: {_LLM_MODEL}\n"
                f"{L('Usage', '用法')}:\n"
                f"  /desensitize model <name>          {L('switch model', '切换模型')}\n"
                f"  /desensitize model ollama:<name>   {L('switch to local Ollama', '切换到 Ollama（本地）')}\n"
                f"  /desensitize model openai:<name>   {L('switch to an OpenAI-compatible endpoint', '切换到 OpenAI 兼容端点')}\n"
                f"                                     {L('(base_url: config llm.base_url, else OPENAI_BASE_URL)', '（base_url 读配置 llm.base_url 或 OPENAI_BASE_URL）')}"
            )
        val = args[1]
        if val.startswith("ollama:"):
            _LLM_PROVIDER = "ollama"
            _LLM_MODEL = val[7:]
            return L(f"Switched to Ollama, model: {_LLM_MODEL}",
                     f"已切换到 Ollama，模型: {_LLM_MODEL}")
        elif val.startswith("openai:") or val.startswith("siliconflow:"):
            _LLM_PROVIDER = "openai"
            _LLM_MODEL = val.split(":", 1)[1]
            return L(f"Switched to OpenAI-compatible endpoint, model: {_LLM_MODEL}",
                     f"已切换到 OpenAI 兼容端点，模型: {_LLM_MODEL}")
        _LLM_MODEL = val
        return L(f"LLM model set to {_LLM_MODEL}", f"LLM 模型已切换为 {_LLM_MODEL}")

    elif cmd == "timeout":
        if len(args) < 2:
            return (f"{L('Current timeout', '当前超时')}: {_LLM_TIMEOUT}s "
                    f"{L('(per chunk)', '（每段独立计时）')}\n"
                    f"{L('Usage', '用法')}: /desensitize timeout <{L('seconds', '秒数')}>")
        try:
            val = int(args[1])
            if val < 5:
                return L("Timeout must be at least 5s", "超时最少 5s")
            _LLM_TIMEOUT = val
            return L(f"LLM timeout set to {_LLM_TIMEOUT}s (split across chunks)",
                     f"LLM 超时已设为 {_LLM_TIMEOUT}s（多段时每段均分）")
        except ValueError:
            return L("Invalid value — enter an integer number of seconds",
                     "无效值，请输入整数秒")

    elif cmd == "chunk":
        if len(args) < 2:
            return (f"{L('Current chunk size', '当前分段')}: {_CHUNK_MAX_CHARS} {L('chars', '字')}\n"
                    f"{L('Usage', '用法')}: /desensitize chunk <{L('characters', '字符数')}>")
        try:
            val = int(args[1])
            if val < 500:
                return L("Chunk size must be at least 500", "分段最少 500 字")
            _CHUNK_MAX_CHARS = val
            return L(f"Chunk size set to {_CHUNK_MAX_CHARS} chars",
                     f"分段大小已设为 {_CHUNK_MAX_CHARS} 字")
        except ValueError:
            return L("Invalid value — enter an integer", "无效值，请输入整数")

    elif cmd in ("status", ""):
        status = L("enabled", "已启用") if _enabled else L("disabled", "已关闭")
        count = sum(len(m) for m in _mappings.values())
        # 报告真实端点而非硬编码标签：配了 llm.base_url（或 OPENAI_BASE_URL）时
        # 走的是自定义端点，旧代码一律显示 api.siliconflow.cn，会掩盖原文实际
        # 发往哪里 —— 对隐私工具这是必须准确的字段。
        if _LLM_PROVIDER == "ollama":
            provider_info = _OLLAMA_BASE or "N/A"
        else:
            provider_info = str(
                _cfg.get("llm.base_url", "") or os.environ.get("OPENAI_BASE_URL", "")
                or _SILICONFLOW_BASE).rstrip("/")
        egress = (L("local", "本地") if _LLM_PROVIDER == "ollama"
                  else L("REMOTE — text is sent here BEFORE redaction",
                         "云端 — 原文未脱敏前发往此端点"))
        return (
            f"{L('Status', '脱敏状态')}: {status}\n"
            f"  Provider: {_LLM_PROVIDER}\n"
            f"  {L('Engine', '主引擎')}:   {_LLM_MODEL} ({L('timeout', '超时')} {_LLM_TIMEOUT}s)\n"
            f"  Endpoint: {provider_info}\n"
            f"  {L('Egress', '出网')}:     {egress}\n"
            f"  {L('Chunk', '分段')}:      ≤{_CHUNK_MAX_CHARS} {L('chars', '字')}\n"
            f"  {L('Fallback', '回退')}:   {L('regex rules', '正则规则')} ({len(PATTERNS)} {L('kinds', '类')})\n"
            f"  {L('Self', '本体')}:       {L('this-company/self/this-project → fixed placeholder', '本公司/本人/本项目 → 固定占位符')}\n"
            f"  {L('Magnitude', '数量级')}: {L('output/market figures → [xxx数量级]', '产值/市场等数据 → [xxx数量级]')}\n"
            f"  {L('Skipped', '不处理')}:  {L('papers/patents/site names/tech parameters', '论文/专利/站名/技术参数')}\n"
            f"  {L('Architecture', '架构')}: {L('pre_llm_call in-place substitution', 'pre_llm_call 原位替换')}\n"
            f"                       {L('+ transform_llm_output restore', '+ transform_llm_output 还原')}\n"
            f"                       {L('+ post_llm_call session restore (keeps the DB original)', '+ post_llm_call 恢复会话（确保 DB 存原文）')}\n"
            f"  {L('Mappings', '当前映射')}: {count} {L('items', '项')}"
        )

    elif cmd == "lang":
        # 会话内切换输出语言。不落盘——要持久化请改 ui.language（配置文件或
        # DESENSITIZE_UI__LANGUAGE）。语言是会话级偏好，落盘会在用户换终端/
        # 换语言环境时变成意外残留。
        if len(args) < 2:
            return (
                f"{L('Current language', '当前语言')}: {_UI_LANG}\n"
                f"{L('Usage', '用法')}: /desensitize lang <both|en|zh>\n"
                f"  both — {L('English + Chinese, English first', '英中双语，英文在前')}\n"
                f"  en   — {L('English only', '纯英文')}\n"
                f"  zh   — {L('Chinese only', '纯中文')}\n"
                f"{L('Aliases accepted: cn / chinese → zh, english → en. Help follows this choice too. Effective for this run only (restart Hermes to reset); to persist, set ui.language in config or DESENSITIZE_UI__LANGUAGE.', '也接受别名：cn / chinese → zh，english → en。help 也跟随该选择。仅本次运行有效（重启 Hermes 后失效）；要持久化请改 ui.language 配置或 DESENSITIZE_UI__LANGUAGE。')}"
            )
        raw_lang = args[1].strip().lower()
        new_lang = _LANG_ALIASES.get(raw_lang, raw_lang)
        if new_lang not in ("both", "en", "zh"):
            # 保持原值而不是回落 both：原本是 zh 的用户打错一次不该被降级。
            return L(f"Unrecognized language {raw_lang!r} — expected both/en/zh. Kept: {_UI_LANG}",
                     f"无法识别的语言 {raw_lang!r}，可选 both/en/zh。已保持: {_UI_LANG}")
        _UI_LANG = new_lang
        _HELP_LANG_EXPLICIT = True
        return L(f"Output language set to '{_UI_LANG}' (this run)",
                 f"输出语言已设为 '{_UI_LANG}'（本次运行有效）")

    else:
        # help 面向的是「不知道怎么用」的人——第一次用、语言还没配。所以这里用
        # help_language（默认 en）而不是 ui.language：一个读不了中文的人，打开
        # 帮助时最需要的就是看得懂。会话内可用 /desensitize lang 立刻覆盖。
        #
        # 但未知子命令必须先显式报错再给 help：`leng cn`（`lang` 的笔误）静默
        # 回落到一屏英文用法，用户根本不知道自己打错了（实测投诉「没生效」）。
        # 报错文字走 L()（会话语言），因为此时用户已经会用命令了，缺的只是回执。
        known = ("on", "off", "status", "lang", "model", "timeout", "chunk", "help", "?")
        notice = ""
        if cmd not in known:
            close = difflib.get_close_matches(cmd, known, n=1)
            notice = L(
                f"Unknown subcommand {cmd!r}" + (f" — did you mean '{close[0]}'?" if close else ""),
                f"未知子命令 {cmd!r}" + (f"，是不是想输 '{close[0]}'？" if close else ""),
            ) + "\n\n"
        return (
            notice
            + f"{_HELP_L('Usage', '用法')}:\n"
            f"  /desensitize on                            {_HELP_L('enable', '启用脱敏')}\n"
            f"  /desensitize off                           {_HELP_L('disable (original text goes to the model)', '关闭脱敏')}\n"
            f"  /desensitize status                        {_HELP_L('show status, including egress endpoint', '查看状态')}\n"
            f"  /desensitize lang <both|en|zh>             {_HELP_L('set output language for this session', '设置本次会话的输出语言')}\n"
            f"  /desensitize model <name>                  {_HELP_L('switch model', '切换模型')}\n"
            f"  /desensitize model ollama:<name>           {_HELP_L('switch to local Ollama', '切到 Ollama（本地）')}\n"
            f"  /desensitize model openai:<name>           {_HELP_L('switch to an OpenAI-compatible endpoint', '切到 OpenAI 兼容端点')}\n"
            f"  /desensitize model siliconflow:<name>      {_HELP_L('switch to SiliconFlow', '切到 SiliconFlow')}\n"
            f"  /desensitize timeout <seconds>             {_HELP_L('set LLM timeout', '设置 LLM 超时')}\n"
            f"  /desensitize chunk <characters>            {_HELP_L('set chunk size', '设置分段大小')}\n"
            f"\n"
            f"{_HELP_L('Output language is ui.language (both|en|zh, default both); /desensitize lang overrides it for this run. This help starts in help_language (default en) so it stays readable before the language is set; once you pick a language with /desensitize lang, this help follows it too.', '输出语言由 ui.language 控制（both|en|zh，默认 both）；/desensitize lang 可覆盖（本次运行有效）。本帮助初始用 help_language（默认 en）渲染，保证语言尚未设置时也读得懂；一旦用 /desensitize lang 选过语言，help 也跟随该选择。')}\n"
            f"\n"
            f"{_HELP_L('When an OpenAI-compatible endpoint is used, base_url comes from config llm.base_url or OPENAI_BASE_URL; it falls back to api.siliconflow.cn when neither is set.', '使用 OpenAI 兼容端点时，base_url 读配置 llm.base_url 或 OPENAI_BASE_URL；两者都未设置时回退到 api.siliconflow.cn。')}"
        )


# ──────────────────────────────────────────────
# 插件入口
# ──────────────────────────────────────────────

def register(ctx):
    # 先把配置层的值同步到模块级变量（三层覆盖 → 运行时变量）
    try:
        _sync_from_config()
    except Exception as e:
        log.warning("desensitize: 配置加载失败（%s），使用内置默认值", e)

    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_api_request", _pre_api_request)
    ctx.register_hook("transform_llm_output", _transform_llm_output)
    ctx.register_hook("post_llm_call", _post_llm_call)

    ctx.register_command(
        name="desensitize",
        handler=_handle_desensitize,
        description="Manage Chinese-context desensitization (LLM semantic layer + regex fallback)",
        args_hint="on|off|status|lang|model|timeout|chunk",
    )

    log.info(
        "desensitize v5 loaded (provider: %s, model: %s, timeout: %ds, chunk: %d, regex: %d patterns)",
        _LLM_PROVIDER, _LLM_MODEL, _LLM_TIMEOUT, _CHUNK_MAX_CHARS, len(PATTERNS),
    )