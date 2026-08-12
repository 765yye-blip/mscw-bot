#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MapleStory Classic World（冒险岛美服怀旧服）公告推送机器人
=========================================================
流程:
  抓取 Nexon CMS 公告列表 -> 取最新日期的怀旧服(MSCW)公告
  -> 解析正文(去掉图片/链接等噪音) -> 翻译成中文(时间行保留原文)
  -> 按黑盒语音 Markdown 排版规则美化(标题/小标题/粗体/分行/分隔线)
  -> 推送到指定房间的指定频道 -> 记录状态, 不重复推送

仅使用 Python 标准库, 无需 pip install 任何依赖。

环境变量:
  HEYCHAT_TOKEN                 黑盒语音机器人 token（推送时必填）
  ROOM_ID                       房间 ID（推送时必填）
  CHANNEL_ID                    频道 ID（推送时必填）
  DEEPSEEK_API_KEY              DeepSeek API 密钥（翻译必填，国内直连、速度快）
  DEEPSEEK_MODEL                DeepSeek 模型名（默认 deepseek-v4-flash）
  DRY_RUN                       1 = 只打印排版结果不推送（默认 0）
  STATE_FILE                    状态文件路径（默认 ./state.json）
  MAX_MSG_LEN                   单条消息最大长度, 超出自动拆成多条（默认 1500）
  PUSH_ON_CONTENT_UPDATE        1 = 同一条公告内容有更新也重新推送（默认 1）
  NEWS_API_BASE                 Nexon CMS API（默认 https://g.nexonstatic.com/maplestory/cms/v1）
  AUTHOR_NAME                   显示的作者名（默认 Classic World Announcement）
  RETRY_TIMES                   列表最高 id 未前进时的重试次数, 含首次（默认 3）
  RETRY_WAIT                    每次重试等待秒数（默认 90）
  TERMS_FILE                    美服->国服术语表 JSON 路径（默认 ./terms.json）
  TERMS_EXTRA_FILE              扩展术语表路径, 可选（默认 ./terms_extra.json）
  DISPLAY_TIMEZONE              展示发布时间所用时区（默认 Asia/Shanghai 北京时间）
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# 配置（从环境变量读取）
# ---------------------------------------------------------------------------
HEYCHAT_TOKEN = os.environ.get("HEYCHAT_TOKEN", "")
ROOM_ID = os.environ.get("ROOM_ID", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
MAX_MSG_LEN = int(os.environ.get("MAX_MSG_LEN", "1500"))
PUSH_ON_CONTENT_UPDATE = os.environ.get("PUSH_ON_CONTENT_UPDATE", "1") == "1"
NEWS_API_BASE = os.environ.get(
    "NEWS_API_BASE", "https://g.nexonstatic.com/maplestory/cms/v1"
)
AUTHOR_NAME = os.environ.get("AUTHOR_NAME", "Classic World Announcement")
# API 源站同步滞后时（官网先显示、API 后同步）的运行内重试
RETRY_TIMES = int(os.environ.get("RETRY_TIMES", "3"))   # 总尝试次数, 含首次
RETRY_WAIT = int(os.environ.get("RETRY_WAIT", "90"))    # 每次重试等待秒数
DISPLAY_TZ = timezone(timedelta(hours=8))  # 北京时间

# ---- 国服术语表: 美服英文 -> 国服《冒险岛》官方中文译名 ----
# terms.json 可自行增删, 翻译前按词边界强制替换(长的优先),
# 确保职业/技能/道具/现金道具等专有名词使用国服叫法
TERMS_FILE = os.environ.get(
    "TERMS_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "terms.json")
)
# 扩展术语表(社区词表, 由 build_terms_extra.py 从《常用词.txt》生成, 可选)
TERMS_EXTRA_FILE = os.environ.get(
    "TERMS_EXTRA_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "terms_extra.json"),
)


def _load_terms_file(path: str, required: bool = False) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not str(k).startswith("_")}
    except Exception as e:
        if required:
            print(f"[warn] 术语表加载失败({path}): {e}, 跳过术语替换", flush=True)
        else:
            print(f"[info] 扩展术语表不可用({path}): {e}", flush=True)
        return {}


# 合并顺序: 扩展表为基础, 主表覆盖(用户 terms.json 优先)
TERMS = _load_terms_file(TERMS_EXTRA_FILE)
TERMS.update(_load_terms_file(TERMS_FILE, required=True))


def apply_terms(text: str) -> str:
    """把美服英文术语替换为国服冒险岛中文译名(词边界匹配, 长词优先)。"""
    if not TERMS:
        return text
    for en in sorted(TERMS, key=len, reverse=True):
        text = re.sub(r"(?<![\w])" + re.escape(en) + r"(?![\w])", TERMS[en], text)
    return text

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DIVIDER = "──────────────────"  # 黑盒语音消息里的分隔线


# ---------------------------------------------------------------------------
# 网络请求
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": UA,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url: str, timeout: int = 20):
    return json.loads(http_get(url, timeout))


def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 20):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# 1. 抓取公告列表：取最新日期的 MSCW（怀旧服）公告
# ---------------------------------------------------------------------------
def fetch_latest_news():
    """返回最新一条怀旧服公告的 dict（含 id/name/liveDate/category）。"""
    items = []
    # 加时间戳参数, 尝试绕过边缘缓存(实测: 源站同步延迟无法靠参数绕过,
    # 真正的兜底是 main() 里的运行内重试)
    for ep in ("/news", "/archived"):
        try:
            url = NEWS_API_BASE + ep
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}_t={int(time.time())}"
            items.extend(http_get_json(url))
        except Exception as e:
            print(f"[warn] 拉取 {ep} 失败: {e}", flush=True)
    if not items:
        raise RuntimeError("公告列表为空, 抓取失败")

    # 只保留怀旧服(Classic World)公告, 按 (发布时间, id) 倒序取最新
    mscw = [n for n in items if n.get("isMSCW")]
    if not mscw:
        raise RuntimeError("没有找到怀旧服(MSCW)公告")
    mscw.sort(key=lambda n: (n.get("liveDate") or "", n.get("id") or 0), reverse=True)
    latest = mscw[0]
    print(f"[info] 最新怀旧服公告: id={latest['id']} date={latest.get('liveDate')} "
          f"title={latest.get('name')}", flush=True)
    return latest


def fetch_news_detail(news_id: int) -> dict:
    return http_get_json(f"{NEWS_API_BASE}/news/{news_id}")


# ---------------------------------------------------------------------------
# 2. 解析正文 HTML -> 结构块
#    ('heading', 文本) 小标题   ('para', 文本) 段落
#    ('list', [项...]) 列表     ('divider',)  分隔线
#    图片/链接被剔除; <strong> 转 **粗体**; <br> 转 \n
# ---------------------------------------------------------------------------
class _BodyParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks = []
        self._cur = None          # 当前段落字符列表
        self._list = None         # 当前列表项数组
        self._li = None           # 当前列表项字符列表
        self._in_li = False
        self._skip = 0            # 图片内部跳过计数

    # ---- 内部工具 ----
    @staticmethod
    def _finalize(parts):
        text = "".join(parts)
        text = re.sub(r"[ \t\u00a0]+", " ", text)   # 合并空格
        text = re.sub(r" *\n *", "\n", text)        # 去掉换行前后的空格
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = re.sub(r"\*{4,}", "**", text)        # 相邻粗体标记归一化
        return text.strip()

    def _flush_para(self):
        if self._cur is not None:
            text = self._finalize(self._cur)
            if text:
                self.blocks.append(("para", text))
            self._cur = None

    def _flush_li(self):
        if self._li is not None:
            text = self._finalize(self._li)
            if text and self._list is not None:
                self._list.append(text)
            self._li = None

    # ---- 事件 ----
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):          # 有闭合标签, 需要跳过内容
            self._skip += 1
        elif tag == "br":
            buf = self._li if self._in_li else self._cur
            if buf is not None:
                buf.append("\n")
        elif tag in ("strong", "b"):
            buf = self._li if self._in_li else self._cur
            if buf is not None:
                buf.append("**")
        elif tag in ("em", "i"):
            buf = self._li if self._in_li else self._cur
            if buf is not None:
                buf.append("*")
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_para()
            self._cur = []
        elif tag == "hr":
            self._flush_para()
            self.blocks.append(("divider", ""))
        elif tag in ("ul", "ol"):
            self._flush_para()
            self._list = []
        elif tag == "li":
            self._flush_li()
            self._li = []
            self._in_li = True
        # img/video/iframe 等自闭合元素: 无内容也无闭合标签, 直接忽略
        # （图片一律剔除, 不输出图片链接）

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag in ("strong", "b"):
            buf = self._li if self._in_li else self._cur
            if buf is not None:
                buf.append("**")
        elif tag in ("em", "i"):
            buf = self._li if self._in_li else self._cur
            if buf is not None:
                buf.append("*")
        elif tag in ("p", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_para()
        elif tag == "li":
            self._flush_li()
            self._in_li = False
        elif tag in ("ul", "ol"):
            self._flush_li()
            self._in_li = False
            if self._list is not None:
                if self._list:
                    self.blocks.append(("list", self._list))
                self._list = None

    def handle_startendtag(self, tag, attrs):
        # 自闭合标签 <img/> <br/> 等
        if tag == "br":
            buf = self._li if self._in_li else self._cur
            if buf is not None:
                buf.append("\n")
        elif tag == "hr":
            self._flush_para()
            self.blocks.append(("divider", ""))

    def handle_data(self, data):
        if self._skip > 0:
            return
        buf = self._li if self._in_li else self._cur
        if buf is not None:
            buf.append(data)


def parse_body(body_html: str):
    """解析正文 HTML, 返回结构块列表。"""
    if not body_html:
        return []
    p = _BodyParser()
    try:
        p.feed(body_html)
        p.close()
    except Exception as e:
        print(f"[warn] 正文解析异常: {e}", flush=True)
        # 兜底: 把整个 HTML 当纯文本
        return [("para", re.sub(r"<[^>]+>", " ", body_html))]
    if p._cur is not None:
        p._flush_para()
    if p._li is not None:
        p._flush_li()
    if p._list is not None and p._list:
        p.blocks.append(("list", p._list))
    return p.blocks


def classify_blocks(blocks):
    """把『整段都是粗体』的段落识别为小标题。"""
    out = []
    for blk in blocks:
        if blk[0] == "para":
            text = blk[1]
            stripped = text.strip()
            # 整个段落被 ** 包裹(且内部没有其他 **) -> 小标题
            if stripped.startswith("**") and stripped.endswith("**"):
                inner = stripped[2:-2]
                if "**" not in inner and inner.strip():
                    out.append(("heading", inner.strip()))
                    continue
        out.append(blk)
    return out


# ---------------------------------------------------------------------------
# 3. 翻译（DeepSeek 批量翻译, 国内直连快; 失败兜底 Google; 时间行保留原文）
# ---------------------------------------------------------------------------
# 看起来像时间/时区信息、数字、网址的行 -> 不翻译（保持原文排版）
_TIME_LINE_RE = re.compile(
    r"\b(PDT|PST|EDT|EST|CEST|CET|AEST|AEDT|UTC|GMT|BST)\b"   # 时区代码
    r"|\b(AM|PM)\b"
    r"|\b\d{1,2}:\d{2}\b"                                     # 14:00
    r"|\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b\s+\d{1,2}"  # August 10
    r"|^\d{4}-\d{2}-\d{2}$"                                   # 2026-08-10
)
# 短行(≤60字符)且基本只含时区/时间信息 -> 不翻译
def _should_keep_original(line: str) -> bool:
    if not line.strip():
        return True
    if re.search(r"[\u4e00-\u9fff]", line):          # 已含中文
        return True
    if re.match(r"^https?://|^www\.", line.strip()):  # 网址
        return True
    if len(line.strip()) <= 60 and _TIME_LINE_RE.search(line):
        return True
    return False


def _translate_deepseek_batch(texts, tl="zh-CN"):
    """用 DeepSeek 一次批量翻译多条文本（国内直连, 快）。
    文本过多时自动分批（每批最多 20 条），避免输出超长被截断。
    返回与输入等长的译文列表; 失败返回 None（由调用方兜底）。
    时间/网址/已含中文的短行在调用前已被 _should_keep_original 过滤。"""
    if not (DEEPSEEK_API_KEY and DEEPSEEK_API_KEY != "XXX") or not texts:
        return None

    # 分批调用, 每批 20 条, 合并结果
    results = [None] * len(texts)
    BATCH = 20
    for start in range(0, len(texts), BATCH):
        chunk = texts[start:start + BATCH]
        chunk_tr = _deepseek_call(chunk, tl)
        if chunk_tr is None:
            return None
        results[start:start + BATCH] = chunk_tr
    return results


def _deepseek_call(texts, tl="zh-CN"):
    """对一批文本调用一次 DeepSeek, 返回等长译文列表; 失败返回 None。"""
    lang_map = {
        "zh-CN": "简体中文", "zh-TW": "繁体中文", "en": "英文",
        "ja": "日文", "ko": "韩文", "ru": "俄文", "fr": "法文",
        "de": "德文", "es": "西班牙文",
    }
    target = lang_map.get(tl, "简体中文")
    prompt = (
        f"你是专业翻译。请把下面 JSON 数组中的每一项文本翻译成{target}。\n"
        "要求：\n"
        "1. 输出一个与输入等长的 JSON 字符串数组，第 i 个元素是第 i 项的译文；\n"
        "2. 只输出 JSON 数组本身，不要任何解释、不要 Markdown 代码块标记；\n"
        "3. 译文要自然通顺，保留原文的换行符、数字和网址；\n"
        f"4. 如果某项原文已经是{target}，原样保留。\n"
        "5. 游戏专有名词（职业、技能、道具、装备、现金道具、地图、NPC、活动名等）"
        "优先采用国服《冒险岛》官方简体中文译名（如 Hero=英雄、Paladin=圣骑士、"
        "Cash Shop=商城、Meso=金币），不要自创译名或逐词直译。\n\n"
        + json.dumps(texts, ensure_ascii=False)
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是专业翻译助手，只输出要求的 JSON 数组。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 8192,
        "temperature": 0.3,
        "stream": False,
    }
    try:
        req = urllib.request.Request(
            "https://api.deepseek.com/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + DEEPSEEK_API_KEY,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        content = data["choices"][0]["message"]["content"]
        # 鲁棒解析: 先整段试 json.loads, 失败再提取首个 [...] 块
        arr = None
        try:
            parsed = json.loads(content)
            if isinstance(parsed, list):
                arr = parsed
        except Exception:
            pass
        if arr is None:
            m = re.search(r"\[.*\]", content, re.S)
            if m:
                try:
                    arr = json.loads(m.group(0))
                except Exception:
                    arr = None
        if not isinstance(arr, list) or len(arr) != len(texts):
            print(f"[warn] DeepSeek 返回条数不符: {len(arr) if isinstance(arr, list) else '?'} != {len(texts)}", flush=True)
            return None
        return [str(x) if x is not None else t for x, t in zip(arr, texts)]
    except Exception as e:
        print(f"[warn] DeepSeek 翻译失败: {e}", flush=True)
        return None


def _translate_google(text: str, tl="zh-CN") -> str:
    """Google translate 免费接口, 单个文本翻译。返回译文, 失败返回空串。"""
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx"
           f"&sl=en&tl={tl}&dt=t&q=" + urllib.parse.quote(text, safe=""))
    data = http_get_json(url)
    return "".join(part[0] for part in data[0] if part and part[0])


def _translate_google_batch(texts, tl="zh-CN", force=False):
    """逐条调用 Google 翻译(免费接口不支持多 q 批量), 返回与输入等长的列表。
    force=True 时不做『保留原文』判断（用于标题等必须翻译的文本）。
    单条失败则保留原文, 不影响其他条。"""
    out = []
    for i, t in enumerate(texts):
        if not force and _should_keep_original(t):
            out.append(t)
            continue
        try:
            tr = _translate_google(t, tl)
            out.append(tr or t)
        except Exception:
            out.append(t)
        time.sleep(0.1)   # 轻微限速, 避免触发 Google 风控
    return out


# Nexon 公告常见小标题的固定译法（Google 对孤立单词翻译不准, 如 Times: -> 次数：）
HEADING_OVERRIDES = {
    "Times:": "时间：",
    "Time:": "时间：",
    "What will be unavailable:": "将不可用的内容：",
    "What will be available:": "将可用的内容：",
    "Changes and Updates:": "变更与更新：",
    "Maintenance Details:": "维护详情：",
    "Additional Notes:": "补充说明：",
    "Known Issues:": "已知问题：",
    "Fixed Issues:": "已修复问题：",
    "How to Participate:": "参与方式：",
    "Rewards:": "奖励：",
    "Common:": "通用：",
    "Common": "通用",
}


def translate_blocks(blocks):
    """翻译所有块里的文本；返回新的块列表。翻译失败时保留原文。
    段落按行拆分翻译（多时区段落每行独立判断, 时间行保留英文原文）。
    术语替换(国服译名)发生在翻译前; "是否保留原文"的判断基于替换前的原始文本,
    避免术语混入中文后整行被误判为"已翻译"而跳过（导致半中半英）。"""
    # 收集翻译单元: (block_index, 位置描述, 原始文本, 术语替换后文本)
    units = []
    for bi, blk in enumerate(blocks):
        if blk[0] == "heading":
            units.append((bi, "heading", None, blk[1], apply_terms(blk[1])))
        elif blk[0] == "para":
            for li, line in enumerate(blk[1].split("\n")):
                units.append((bi, "para", li, line, apply_terms(line)))
        elif blk[0] == "list":
            for ii, item in enumerate(blk[1]):
                units.append((bi, "list", ii, item, apply_terms(item)))

    # 决定哪些需要翻译: 时间行/网址/已含中文行基于"原始文本"直接保留;
    # 其余默认用"术语替换后文本"(至少术语已译), 交给翻译服务处理剩余英文
    tr_map = {}
    need_units = []  # (bi, kind, li, 待翻译文本)
    for (b, k, li, raw, rep) in units:
        if k == "heading":
            ov = HEADING_OVERRIDES.get(raw.strip(), None)
            if ov is not None:
                tr_map[(b, k, li)] = ov
            else:
                tr_map[(b, k, li)] = rep
                need_units.append((b, k, li, rep))
        elif _should_keep_original(raw):
            tr_map[(b, k, li)] = raw
        else:
            tr_map[(b, k, li)] = rep
            need_units.append((b, k, li, rep))

    if need_units:
        need_texts = [u[3] for u in need_units]
        # 优先 DeepSeek 批量翻译（国内直连、快）; 失败则逐条走 Google
        translated = _translate_deepseek_batch(need_texts)
        if translated is None:
            translated = _translate_google_batch(need_texts, force=True)
        for (b, k, li, _rep), tr in zip(need_units, translated):
            tr_map[(b, k, li)] = tr

    # 用翻译结果重建块: 建立 (bi, kind, li) -> 翻译文本 的映射
    new_blocks = []
    for bi, blk in enumerate(blocks):
        if blk[0] == "heading":
            new_blocks.append(("heading", tr_map.get((bi, "heading", None), blk[1])))
        elif blk[0] == "para":
            lines = blk[1].split("\n")
            for li in range(len(lines)):
                lines[li] = tr_map.get((bi, "para", li), lines[li])
            new_blocks.append(("para", "\n".join(lines)))
        elif blk[0] == "list":
            items = list(blk[1])
            for ii in range(len(items)):
                items[ii] = tr_map.get((bi, "list", ii), items[ii])
            new_blocks.append(("list", items))
        else:
            new_blocks.append(blk)
    return new_blocks


# 网页锚点等噪音行（直接丢弃）
_NOISE_LINE_RE = re.compile(r"^(back\s*to\s*top|top)$", re.IGNORECASE)


def clean_markdown(text: str) -> str:
    """清理翻译后残留的 markdown 噪音: 空粗体、相邻粗体标记。"""
    text = re.sub(r"\*\*[ \t\u00a0]+\*\*", "**", text)   # ** ** -> **
    text = re.sub(r"\*{4,}", "**", text)                 # **** -> **
    text = re.sub(r"\*\*\*\*", "", text)
    return text


def fix_bold_balance(text: str) -> str:
    """粗体标记数量为奇数时, 直接去掉全部粗体标记, 避免 markdown 渲染错乱。"""
    return text.replace("**", "") if text.count("**") % 2 == 1 else text


# ---------------------------------------------------------------------------
# 4. 排版：按黑盒语音 Markdown 规则生成消息
#    黑盒语音规则: 支持 # 与 ## 两级标题; **粗体**; 段落间用 \n\n 换行
# ---------------------------------------------------------------------------
def build_message_parts(article: dict, blocks) -> list:
    """生成消息分片列表（每个分片是一个独立段落串），再交给 chunk 拆条。"""
    parts = []

    # ---- 头部: 标题 + 作者 + 发布时间 ----
    try:
        dt_utc = datetime.fromisoformat(article["liveDate"].replace("Z", "+00:00"))
        pub_bj = dt_utc.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pub_bj = article.get("liveDate", "")

    parts.append(f"# {article['title_cn']}")
    parts.append(f"**作者**：{AUTHOR_NAME}")
    parts.append(f"**发布时间**：{pub_bj}（北京时间）")
    parts.append(DIVIDER)

    # ---- 正文 ----
    for blk in blocks:
        kind = blk[0]
        if kind == "heading":
            parts.append(f"## {fix_bold_balance(clean_markdown(blk[1]))}")
        elif kind == "para":
            text = fix_bold_balance(clean_markdown(blk[1]))
            stripped_outer = False
            # 整段被粗体包裹(如多时区段落) -> 去掉最外层粗体标记, 每行单独成段
            if text.startswith("**") and text.endswith("**"):
                text = text[2:-2]
                stripped_outer = True
            lines = text.split("\n")
            if stripped_outer and len(lines) > 1:
                # 去掉每行首尾悬空的粗体标记, 让每行独立展示
                lines = [re.sub(r"^\*{2,}", "", l) for l in lines]
                lines = [re.sub(r"\*{2,}$", "", l) for l in lines]
            for line in lines:
                line = line.strip()
                if line and not _NOISE_LINE_RE.match(line):
                    parts.append(line)
        elif kind == "list":
            for item in blk[1]:
                parts.append(f"• {fix_bold_balance(clean_markdown(item))}")
        elif kind == "divider":
            parts.append(DIVIDER)

    # 过滤空段
    return [p for p in parts if p and p.strip()]


def chunk_parts(parts: list, max_len: int) -> list:
    """按最大长度把段落拆成多条消息。"""
    chunks, cur, cur_len = [], [], 0
    for p in parts:
        p_len = len(p) + 2  # +2 用于 \n\n 连接
        if cur and cur_len + p_len > max_len:
            chunks.append("\n\n".join(cur))
            cur, cur_len = [], 0
        cur.append(p)
        cur_len += p_len
    if cur:
        chunks.append("\n\n".join(cur))
    return chunks


# ---------------------------------------------------------------------------
# 5. 推送: 黑盒语音 发送频道消息（markdown）
# ---------------------------------------------------------------------------
HEYCHAT_SEND_URL = "https://chat.xiaoheihe.cn/chatroom/v2/channel_msg/send"
HEYCHAT_COMMON_PARAMS = (
    "client_type=heybox_chat&x_client_type=web&os_type=web&x_os_type=bot"
    "&x_app=heybox_chat&chat_os_type=bot&chat_version=1.30.0"
)


def send_heychat(content: str, ack_id: str) -> bool:
    url = f"{HEYCHAT_SEND_URL}?{HEYCHAT_COMMON_PARAMS}"
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "token": HEYCHAT_TOKEN,
    }
    payload = {
        "msg": content,
        "msg_type": 4,            # 4 = markdown
        "heychat_ack_id": ack_id,
        "reply_id": "",
        "room_id": ROOM_ID,
        "channel_id": CHANNEL_ID,
        "addition": "{}",
    }
    resp = http_post_json(url, payload, headers)
    status = resp.get("status")
    if status != "ok":
        print(f"[error] 推送失败: {json.dumps(resp, ensure_ascii=False)[:300]}", flush=True)
        return False
    print(f"[info] 已推送({len(content)}字符)", flush=True)
    return True


# ---------------------------------------------------------------------------
# 6. 去重状态
# ---------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def content_hash(article: dict, detail: dict) -> str:
    h = hashlib.sha256()
    h.update((article.get("name") or "").encode("utf-8"))
    h.update((detail.get("body") or "").encode("utf-8"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    print(f"[info] 开始检查 (DRY_RUN={'是' if DRY_RUN else '否'})", flush=True)

    # 3) 去重状态（提前加载, 供重试判断用）
    state = load_state()
    pushed_id = state.get("pushed_id")

    # 1) 最新公告
    #    Nexon CMS API 源站同步可能滞后（官网网页先显示、API 后同步）:
    #    若列表最高 id 与已推送一致, 等待 RETRY_WAIT 秒重拉, 最多 RETRY_TIMES 次
    latest = None
    for attempt in range(1, RETRY_TIMES + 1):
        latest = fetch_latest_news()
        if str(latest["id"]) != pushed_id:
            break
        if attempt < RETRY_TIMES:
            print(f"[info] 列表最高仍是 id={latest['id']}（疑似 API 源站同步滞后）, "
                  f"等待 {RETRY_WAIT}s 重试 ({attempt + 1}/{RETRY_TIMES})", flush=True)
            time.sleep(RETRY_WAIT)

    # 2) 详情
    detail = fetch_news_detail(latest["id"])
    h = content_hash(latest, detail)

    # 3) 去重判断
    pushed_hash = state.get("pushed_hash")
    if pushed_id == str(latest["id"]):
        if pushed_hash == h:
            print("[info] 没有新公告, 跳过", flush=True)
            return
        if not PUSH_ON_CONTENT_UPDATE:
            print(f"[info] 公告未变(id={latest['id']}), 内容虽有更新但已配置不重推", flush=True)
            return
        print(f"[info] 公告内容有更新, 重新推送 (id={latest['id']})", flush=True)

    # 4) 解析 + 翻译 + 排版
    blocks = classify_blocks(parse_body(detail.get("body") or ""))
    try:
        title_tr = _translate_deepseek_batch([apply_terms(latest["name"])])
        if title_tr:
            title_cn = title_tr[0]
        else:
            # DeepSeek 不可用时用 Google 兜底(标题短, 单条很快)
            title_cn = _translate_google_batch([apply_terms(latest["name"])], force=True)[0]
    except Exception:
        title_cn = apply_terms(latest["name"])
    latest["title_cn"] = title_cn
    blocks = translate_blocks(blocks)

    parts = build_message_parts(latest, blocks)
    chunks = chunk_parts(parts, MAX_MSG_LEN)
    print(f"[info] 排版完成: {len(parts)} 段, 拆成 {len(chunks)} 条消息", flush=True)

    if DRY_RUN:
        print("\n================ 排版预览（DRY_RUN 不推送） ================\n", flush=True)
        for i, c in enumerate(chunks, 1):
            print(f"-------- 第 {i}/{len(chunks)} 条 --------")
            print(c)
            print()
        return

    # 5) 推送
    if not (HEYCHAT_TOKEN and ROOM_ID and CHANNEL_ID):
        print("[error] 缺少 HEYCHAT_TOKEN / ROOM_ID / CHANNEL_ID, 无法推送", flush=True)
        sys.exit(1)
    ok_all = True
    for i, c in enumerate(chunks, 1):
        ack = f"{int(time.time() * 1000)}{i}"
        if not send_heychat(c, ack):
            ok_all = False
            break
        time.sleep(0.6)  # 避免触发限频

    # 6) 记录状态（推送成功后）
    if ok_all:
        save_state({"pushed_id": str(latest["id"]), "pushed_hash": h,
                    "pushed_at": datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
                    "title": latest.get("name")})
        print(f"[info] 状态已记录: {latest['id']} / {h}", flush=True)
    else:
        print("[error] 推送未全部成功, 不更新状态(下次会重试)", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[error] 运行异常: {e}", flush=True)
        sys.exit(1)
