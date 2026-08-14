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
  MAX_MSG_LEN                   单条消息最大长度(字符), 超出自动拆成多条（默认 1500）
  MAX_MSG_BYTES                 单条消息最大长度(UTF-8 字节), 0=不启用（默认 0;
                                若黑盒接口按字节限长可设置, 拆条时字符/字节双限制同时满足）
  PUSH_ON_CONTENT_UPDATE        1 = 同一条公告内容有更新也重新推送（默认 1）
  NEWS_API_BASE                 Nexon CMS API（默认 https://g.nexonstatic.com/maplestory/cms/v1）
  AUTHOR_NAME                   显示的作者名（默认 Classic World Announcement）
  RETRY_TIMES                   列表最高 id 未前进时的重试次数, 含首次（默认 3）
  RETRY_WAIT                    每次重试等待秒数（默认 90）
  TERMS_FILE                    美服->国服术语表 JSON 路径（默认 ./terms.json）
  TERMS_EXTRA_FILE              扩展术语表路径, 可选（默认 ./terms_extra.json）
  DISPLAY_TIMEZONE              展示发布时间所用时区（默认 Asia/Shanghai 北京时间）
  ALERT_AFTER                   连续失败达到该次数时推送告警, 成功推送后清零（默认 3）
  TR_CACHE_MAX                  行级翻译缓存上限(条), 超出丢最旧（默认 300）
"""

import hashlib
import json
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

try:
    from zoneinfo import ZoneInfo
except ImportError:      # Python < 3.9
    ZoneInfo = None

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
# 单条消息字节上限(UTF-8), 0 = 不启用。若黑盒接口按字节限长, 中文消息容易超(1500 字符≈4500 字节)
MAX_MSG_BYTES = int(os.environ.get("MAX_MSG_BYTES", "0"))
PUSH_ON_CONTENT_UPDATE = os.environ.get("PUSH_ON_CONTENT_UPDATE", "1") == "1"
NEWS_API_BASE = os.environ.get(
    "NEWS_API_BASE", "https://g.nexonstatic.com/maplestory/cms/v1"
)
AUTHOR_NAME = os.environ.get("AUTHOR_NAME", "Classic World Announcement")
# API 源站同步滞后时（官网先显示、API 后同步）的运行内重试
RETRY_TIMES = int(os.environ.get("RETRY_TIMES", "3"))   # 总尝试次数, 含首次
RETRY_WAIT = int(os.environ.get("RETRY_WAIT", "90"))    # 每次重试等待秒数
ALERT_AFTER = int(os.environ.get("ALERT_AFTER", "3"))    # 连续失败达到该次数时推送告警(成功推送后清零)
# 行级翻译缓存: 原文行 -> 译文行, 内容更新重推时只翻译变动的行(省 token/延迟)
TR_CACHE_MAX = int(os.environ.get("TR_CACHE_MAX", "300"))
# 展示发布时间所用时区(默认 Asia/Shanghai); Windows 无 tzdata 时回退固定 UTC+8
DISPLAY_TZ = timezone(timedelta(hours=8))                # 兜底: 北京时间(无 DST, 与 Asia/Shanghai 等价)
try:
    if ZoneInfo is not None:
        _tz_name = os.environ.get("DISPLAY_TIMEZONE", "Asia/Shanghai")
        DISPLAY_TZ = ZoneInfo(_tz_name)
except Exception as e:
    print(f"[warn] 时区 {os.environ.get('DISPLAY_TIMEZONE', 'Asia/Shanghai')} 不可用, 回退 UTC+8: {e}", flush=True)

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


def _load_terms_file(path: str, required: bool = False, quiet: bool = False) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not str(k).startswith("_")}
    except Exception as e:
        if required:
            print(f"[warn] 术语表加载失败({path}): {e}, 跳过术语替换", flush=True)
        elif not quiet:
            print(f"[warn] 术语表加载失败({path}): {e}", flush=True)
        return {}


# 合并顺序: 扩展表为基础, 主表覆盖(用户 terms.json 优先)
# 扩展表默认路径不存在属正常(未部署社区词表), 静默不刷屏; 显式配置了却读不到才提示
TERMS = _load_terms_file(TERMS_EXTRA_FILE, quiet=not bool(os.environ.get("TERMS_EXTRA_FILE")))
TERMS.update(_load_terms_file(TERMS_FILE, required=True))


# 预编译术语正则(启动时一次): 长词优先, 词边界匹配。
# 避免 apply_terms 每次调用都现编译上千条正则(每轮运行可省约 1 秒)
_TERM_PATTERNS = [
    (re.compile(r"(?<![\w])" + re.escape(en) + r"(?![\w])"), zh)
    for en, zh in sorted(TERMS.items(), key=lambda kv: len(kv[0]), reverse=True)
]


def apply_terms(text: str) -> str:
    """把美服英文术语替换为国服冒险岛中文译名(词边界匹配, 长词优先)。"""
    if not _TERM_PATTERNS:
        return text
    for pat, zh in _TERM_PATTERNS:
        text = pat.sub(zh, text)
    return text


def _terms_fingerprint() -> str:
    """术语表指纹(条数 + 文件 mtime): 术语表变更后翻译缓存作废, 避免旧译名滞留。"""
    mtime = 0.0
    try:
        mtime = os.path.getmtime(TERMS_FILE)
    except OSError:
        pass
    return f"{len(TERMS)}-{int(mtime)}"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

DIVIDER = "──────────────────"  # 黑盒语音消息里的分隔线


# ---------------------------------------------------------------------------
# 网络请求
# ---------------------------------------------------------------------------
def http_get(url: str, timeout: int = 20, retries: int = 2) -> str:
    """GET 请求; 网络抖动时自动重试(retries 次, 指数退避), 避免一次波动整轮失败。"""
    last_exc = None
    for attempt in range(retries + 1):
        try:
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
        except Exception as e:
            last_exc = e
            if attempt < retries:
                print(f"[warn] GET {url[:80]} 失败(第{attempt + 1}次): {e}, 重试", flush=True)
                time.sleep(2 * (attempt + 1))
    raise last_exc


def http_get_json(url: str, timeout: int = 20):
    return json.loads(http_get(url, timeout))


def http_post_json(url: str, payload: dict, headers: dict, timeout: int = 20, retries: int = 2):
    """POST 请求: 网络错误与 5xx 重试; 4xx 不重试(鉴权/参数问题重试无意义)。"""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_exc = e
            if e.code < 500 or attempt >= retries:
                break
        except Exception as e:
            last_exc = e
        if attempt < retries:
            print(f"[warn] POST {url[:60]} 失败(第{attempt + 1}次): {last_exc}, 重试", flush=True)
            time.sleep(2 * (attempt + 1))
    raise last_exc


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
        self._lists = []          # 列表栈(支持嵌套 ul/li, 收尾时展平为单个 list 块)
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
            if text and self._lists:
                # 列表项存 (文本, 嵌套层级), 层级用于排版时按官网格式缩进
                self._lists[-1].append((text, len(self._lists) - 1))
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
            self._flush_li()          # 先把父 li 的文本收进父列表(否则会被塞进嵌套层)
            self._flush_para()
            self._lists.append([])
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
            if self._lists:
                done = self._lists.pop()        # 当前层
                if self._lists:
                    self._lists[-1].extend(done)  # 嵌套列表展平到父层
                elif done:
                    self.blocks.append(("list", done))

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
        if self._in_li:
            if self._li is not None:
                self._li.append(data)
            return
        # 段外裸文本(如 <div>/<td> 里未用 <p> 包裹的文本)自动开段收集, 避免丢失
        if self._cur is None:
            if data.strip():
                self._cur = []
            else:
                return
        self._cur.append(data)


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
    while p._lists:
        done = p._lists.pop()
        if p._lists:
            p._lists[-1].extend(done)
        elif done:
            p.blocks.append(("list", done))
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
# 含美加(PDT/PST/CDT/CST/MDT/MST/EDT/EST/AKDT/AKST/HST)、欧亚(时区)及 am/pm 大小写变体
_TIME_LINE_RE = re.compile(
    r"\b(PDT|PST|CDT|CST|MDT|MST|EDT|EST|AKDT|AKST|HST|CEST|CET|AEST|AEDT|JST|KST|IST|UTC|GMT|BST)\b"
    r"|\b(AM|PM|A\.M\.|P\.M\.)\b"                        # 4:00 PM / 4:00 p.m.
    r"|\b\d{1,2}:\d{2}\b"                                # 14:00
    r"|\b(January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b\s+\d{1,2}"  # August 10
    r"|^\d{4}-\d{2}-\d{2}$",                             # 2026-08-10
    re.IGNORECASE,
)
# 纯日期行: [星期,] 月 日[,] 年 (可带首尾粗体标记) -> 翻译成中文(如 "2026 年 8 月 11 日星期二")
_DATE_ONLY_RE = re.compile(
    r"(?:\*\*)?\s*("
    r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)(?:,\s*)?)?"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"\s*(?:\*\*)?$",
    re.IGNORECASE,
)
# 短行(≤60字符)且基本只含时区/时间信息 -> 不翻译
def _should_keep_original(line: str) -> bool:
    if not line.strip():
        return True
    if re.search(r"[\u4e00-\u9fff]", line):          # 已含中文
        return True
    if re.match(r"^https?://|^www\.", line.strip()):  # 网址
        return True
    stripped = line.strip()
    # 纯日期行 -> 不保留, 交给翻译引擎(避免被上面的时间规则整行保留成英文)
    if _DATE_ONLY_RE.match(stripped):
        return False
    if len(stripped) <= 60 and _TIME_LINE_RE.search(line):
        return True
    return False


def _translate_deepseek_batch(texts, tl="zh-CN"):
    """用 DeepSeek 一次批量翻译多条文本（国内直连, 快）。
    止损策略: 最多降批 1 次(20 -> 10)。"返回条数不符"说明是输出格式问题,
    降批到 5/2 结果相同, 只会浪费数十秒重试(线上实测 3 次降批白耗 ~3 分钟);
    而截断场景 10 条一般足够。仍失败返回 None 由调用方转 Google 兜底。
    时间/网址/已含中文的短行在调用前已被 _should_keep_original 过滤。"""
    if not (DEEPSEEK_API_KEY and DEEPSEEK_API_KEY != "XXX") or not texts:
        return None

    for batch in (20, 10):
        results = [None] * len(texts)
        ok = True
        for start in range(0, len(texts), batch):
            chunk_tr = _deepseek_call(texts[start:start + batch], tl)
            if chunk_tr is None:
                ok = False
                break
            results[start:start + batch] = chunk_tr
        if ok:
            return results
        print(f"[warn] DeepSeek 批量翻译失败(batch={batch}), 降批重试一次; "
              f"再失败将转 Google 兜底", flush=True)
    return None


def _parse_llm_json_array(content):
    """鲁棒解析 LLM 输出的 JSON 数组。
    背景: 模型可能输出 ```json 代码块、前后解释文字, 且译文本身可能含方括号
    (如 "[Completed]")——贪婪正则 \\[.*\\] 会从第一个 [ 抓到最后一个 ],
    中间夹非 JSON 内容导致解析失败(线上 DeepSeek 批量全挂的根因)。
    方案: 剥离代码块后, 用 json.JSONDecoder.raw_decode 依次尝试每个 '[' 位置,
    取第一个能完整解析出的数组。返回 list 或 None。"""
    content = (content or "").strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", content, re.S)
    if m:
        content = m.group(1).strip()
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        idx = content.find("[", idx)
        if idx == -1:
            return None
        try:
            val, _ = decoder.raw_decode(content[idx:])
            if isinstance(val, list):
                return val
        except Exception:
            pass
        idx += 1


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
        t0 = time.perf_counter()
        print(f"[info] DeepSeek 翻译 {len(texts)} 条 (模型 {DEEPSEEK_MODEL})...", flush=True)
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
        arr = _parse_llm_json_array(content)
        print(f"[info] DeepSeek 返回 {len(content)} 字符, 耗时 {time.perf_counter() - t0:.1f}s", flush=True)
        if not isinstance(arr, list) or len(arr) != len(texts):
            preview = (content or "").replace("\n", " ")[:200]
            print(f"[warn] DeepSeek 返回条数不符: {len(arr) if isinstance(arr, list) else '?'} "
                  f"!= {len(texts)}; 响应预览: {preview}", flush=True)
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
    单条失败则保留原文并打日志, 不影响其他条。"""
    out = []
    for i, t in enumerate(texts):
        if not force and _should_keep_original(t):
            out.append(t)
            continue
        try:
            tr = _translate_google(t, tl)
            out.append(tr or t)
        except Exception as e:
            print(f"[warn] Google 翻译失败(第{i + 1}条): {e}, 保留原文", flush=True)
            out.append(t)
        time.sleep(0.1)   # 轻微限速, 避免触发 Google 风控
    return out


def _translate_with_cache(texts, state):
    """带行级缓存的批量翻译: 缓存命中的行直接复用, 未命中的才调用翻译引擎。
    缓存存于 state["tr_cache"]（限量 TR_CACHE_MAX 条, 超出丢最旧）。
    正确性约束:
      - 只缓存 DeepSeek 的结果; Google 兜底不写缓存——兜底失败时保留原文,
        若缓存"原文->原文"会在翻译服务恢复后继续命中, 导致该行永远不翻译。
      - 缓存带术语表指纹(tr_cache_ver): terms.json 变更后自动清空, 避免旧译名滞留。
    DeepSeek 逐行翻译、无跨行上下文, 因此行级缓存安全。"""
    cache = state.setdefault("tr_cache", {})
    ver = _terms_fingerprint()
    if state.get("tr_cache_ver") != ver:
        cache.clear()
        state["tr_cache_ver"] = ver
    out = [None] * len(texts)
    todo_idx, todo = [], []
    for i, t in enumerate(texts):
        if t in cache:
            out[i] = cache[t]
        else:
            todo_idx.append(i)
            todo.append(t)
    if todo:
        tr = _translate_deepseek_batch(todo)     # 内部自动降批
        if tr is not None:
            for idx, t, tv in zip(todo_idx, todo, tr):
                out[idx] = tv
                cache[t] = tv
        else:
            tr = _translate_google_batch(todo, force=True)   # 兜底, 不写缓存
            for idx, t, tv in zip(todo_idx, todo, tr):
                out[idx] = tv
        if len(cache) > TR_CACHE_MAX:            # 限量: 丢弃最旧的条目
            for k in list(cache)[:len(cache) - TR_CACHE_MAX]:
                del cache[k]
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


def translate_blocks(blocks, state=None, title_texts=None):
    """翻译所有块里的文本；返回 (新块列表, 标题译文列表或 None)。翻译失败时保留原文。
    title_texts: [(原文, 术语替换后)] 标题翻译单元, 与正文并入同一批量(省一次 API 调用)。
    state: 传入时启用行级翻译缓存(原文行 -> 译文), 内容更新重推时只翻变动的行。
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
                it = item[0] if isinstance(item, tuple) else item
                units.append((bi, "list", ii, it, apply_terms(it)))
    # 标题并入同一批次: kind="title" 不做保留原文判断, 永远送翻译(已纯中文则直接使用)
    if title_texts:
        for i, (raw, rep) in enumerate(title_texts):
            units.append((None, "title", i, raw, rep))

    # 决定哪些需要翻译: 时间行/网址/已含中文行基于"原始文本"直接保留;
    # 其余默认用"术语替换后文本"(至少术语已译), 交给翻译服务处理剩余英文。
    # 注意: 替换后已不含英文字母的行(纯中文术语/数字/符号)不再送翻译引擎,
    #       避免中文术语被翻译服务二次加工(如 风之前跃 -> 风先于跃)
    tr_map = {}
    need_units = []  # (bi, kind, li, 待翻译文本)
    for (b, k, li, raw, rep) in units:
        if k == "title":
            tr_map[(b, k, li)] = rep
            if re.search(r"[A-Za-z]", rep):
                need_units.append((b, k, li, rep))
        elif k == "heading":
            ov = HEADING_OVERRIDES.get(raw.strip(), None)
            if ov is not None:
                tr_map[(b, k, li)] = ov
            else:
                tr_map[(b, k, li)] = rep
                if re.search(r"[A-Za-z]", rep):
                    need_units.append((b, k, li, rep))
        elif _should_keep_original(raw):
            tr_map[(b, k, li)] = raw
        else:
            tr_map[(b, k, li)] = rep
            if re.search(r"[A-Za-z]", rep):
                need_units.append((b, k, li, rep))

    if need_units:
        need_texts = [u[3] for u in need_units]
        if state is not None:
            translated = _translate_with_cache(need_texts, state)   # 缓存 + DeepSeek + Google 兜底
        else:
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
                tr = tr_map.get((bi, "list", ii))
                if tr is not None:
                    if isinstance(items[ii], tuple):
                        items[ii] = (tr, items[ii][1])   # 保留嵌套层级
                    else:
                        items[ii] = tr
            new_blocks.append(("list", items))
        else:
            new_blocks.append(blk)

    title_out = None
    if title_texts:
        title_out = [tr_map.get((None, "title", i), title_texts[i][1])
                     for i in range(len(title_texts))]
    return new_blocks, title_out


# 网页锚点等噪音行（直接丢弃）
_NOISE_LINE_RE = re.compile(r"^(back\s*to\s*top|top)$", re.IGNORECASE)


def clean_markdown(text: str) -> str:
    """清理翻译后残留的 markdown 噪音: 空粗体、相邻粗体标记。"""
    text = re.sub(r"\*\*[ \t\u00a0]+\*\*", "**", text)   # ** ** -> **
    text = re.sub(r"\*{4,}", "**", text)                 # **** -> **
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
        raw_dt = article["liveDate"].replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw_dt)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)   # naive 一律按 UTC 解释, 保证本地/CI 显示一致
        pub_bj = dt.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pub_bj = article.get("liveDate", "")

    parts.append(f"# {article['title_cn']}")
    parts.append(f"**作者**：{AUTHOR_NAME}")
    # 时区文案随时区配置动态化: Asia/Shanghai 显示"北京时间", 其他显示时区 key
    tz_label = getattr(DISPLAY_TZ, "key", None) or "UTC+8"
    tz_display = "北京时间" if tz_label == "Asia/Shanghai" else tz_label
    parts.append(f"**发布时间**：{pub_bj}（{tz_display}）")
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
            # 列表项打包成一个整体。项间用 Markdown 硬换行(行尾两个空格+\n):
            # 黑盒语音里单 \n 会被当作空格(列表挤成一行), 空行 \n\n 又会被分成独立段落,
            # 行尾双空格是"紧凑但强制换行"的唯一方式。
            lines = []
            for item in blk[1]:
                if isinstance(item, tuple):
                    text, depth = item
                else:
                    text, depth = item, 0
                lines.append("  " * depth + "• " + fix_bold_balance(clean_markdown(text)) + "  ")
            if lines:
                parts.append("\n".join(lines))
        elif kind == "divider":
            parts.append(DIVIDER)

    # 过滤空段
    return [p for p in parts if p and p.strip()]


def split_long_part(part: str, max_len: int, max_bytes: int = 0) -> list:
    """把超长段落按行边界拆成多个子段, 每段尽量不超过 max_len(字符)和 max_bytes(字节)。
    按 \n 拆分不会切散列表项的 "• " 前缀, 也不会把单行拆开。"""
    lines = part.split("\n")
    subs, buf, buf_len, buf_bytes = [], [], 0, 0
    for ln in lines:
        ln_len = len(ln) + 1
        ln_bytes = len((ln + "\n").encode("utf-8"))
        over_chars = buf_len + ln_len > max_len
        over_bytes = max_bytes > 0 and buf_bytes + ln_bytes > max_bytes
        if buf and (over_chars or over_bytes):
            subs.append("\n".join(buf))
            buf, buf_len, buf_bytes = [], 0, 0
        buf.append(ln)
        buf_len += ln_len
        buf_bytes += ln_bytes
    if buf:
        subs.append("\n".join(buf))
    return subs


def chunk_parts(parts: list, max_len: int, max_bytes: int = 0) -> list:
    """按最大长度把段落拆成多条消息; 单段超过 max_len 时先按行拆分,
    避免整条消息超限被推送接口拒绝或截断。
    max_bytes > 0 时额外按 UTF-8 字节数限制(与字符数双限制同时满足)。"""
    chunks, cur, cur_len, cur_bytes = [], [], 0, 0

    def over_limit(p_chars, p_bytes):
        if cur_len + p_chars > max_len:
            return True
        return max_bytes > 0 and cur_bytes + p_bytes > max_bytes

    for p in parts:
        p_chars = len(p) + 2          # +2 用于 \n\n 连接
        p_bytes = len((p + "\n\n").encode("utf-8"))
        if cur and over_limit(p_chars, p_bytes):
            chunks.append("\n\n".join(cur))
            cur, cur_len, cur_bytes = [], 0, 0
        if p_chars > max_len or (max_bytes > 0 and p_bytes > max_bytes):
            sub_parts = split_long_part(p, max_len, max_bytes)
        else:
            sub_parts = [p]
        for sp in sub_parts:
            sp_chars = len(sp) + 2
            sp_bytes = len((sp + "\n\n").encode("utf-8"))
            if cur and over_limit(sp_chars, sp_bytes):
                chunks.append("\n\n".join(cur))
                cur, cur_len, cur_bytes = [], 0, 0
            cur.append(sp)
            cur_len += sp_chars
            cur_bytes += sp_bytes
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
    """原子写入状态文件(先写临时文件再 os.replace 替换), 避免写一半损坏导致误判重复推送。"""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


def _record_failure(state: dict, msg: str):
    """记录一次连续失败(写入状态); 达到 ALERT_AFTER 次且为其整数倍时推送一条告警消息。"""
    state["fail_count"] = state.get("fail_count", 0) + 1
    state["last_error"] = msg
    state["last_fail_at"] = datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
    save_state(state)
    if state["fail_count"] >= ALERT_AFTER and state["fail_count"] % ALERT_AFTER == 0:
        if not (HEYCHAT_TOKEN and ROOM_ID and CHANNEL_ID):
            print(f"[warn] 连续失败 {state['fail_count']} 次, 但缺少推送环境变量, 跳过告警", flush=True)
            return
        try:
            content = (f"**⚠️ 公告推送机器人连续失败 {state['fail_count']} 次**\n"
                       f"最近错误: {msg}\n"
                       f"最后失败时间: {state['last_fail_at']}")
            ack = f"{int(time.time() * 1000)}0"
            if send_heychat(content, ack):
                print(f"[info] 已发送失败告警(第 {state['fail_count']} 次失败)", flush=True)
        except Exception as e:
            print(f"[warn] 失败告警发送异常: {e}", flush=True)


def content_hash(article: dict, detail: dict) -> str:
    h = hashlib.sha256()
    h.update((article.get("name") or "").encode("utf-8"))
    h.update((detail.get("body") or "").encode("utf-8"))
    return h.hexdigest()[:16]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    # 推送所需环境变量前置校验: 缺配置就直接退出, 避免白跑抓取+翻译+排版一轮
    if not DRY_RUN and not (HEYCHAT_TOKEN and ROOM_ID and CHANNEL_ID):
        print("[error] 缺少 HEYCHAT_TOKEN / ROOM_ID / CHANNEL_ID, 无法推送 "
              "(本地预览请设置 DRY_RUN=1)", flush=True)
        sys.exit(1)

    print(f"[info] 开始检查 (DRY_RUN={'是' if DRY_RUN else '否'})", flush=True)
    if DEEPSEEK_API_KEY and DEEPSEEK_API_KEY != "XXX":
        print("[info] 翻译后端: DeepSeek (已配置 API key)", flush=True)
    else:
        print("[info] 翻译后端: Google 兜底 (未配置 DEEPSEEK_API_KEY)", flush=True)

    # 3) 去重状态（提前加载, 供重试判断用）
    state = load_state()
    pushed_id = state.get("pushed_id")

    # 1) 最新公告
    #    Nexon CMS API 源站同步可能滞后（官网网页先显示、API 后同步）:
    #    若列表最高 id 与已推送一致, 等待 RETRY_WAIT 秒重拉, 最多 RETRY_TIMES 次
    t_seg = time.perf_counter()
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
    t_fetch = time.perf_counter() - t_seg

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
    if not blocks:
        print("[warn] 正文解析后无文本块(可能为纯图片公告), 跳过推送", flush=True)
        return
    t_seg = time.perf_counter()
    title_rep = apply_terms(latest["name"])
    # 标题与正文并入同一翻译批次(带行级缓存, 内容更新重推时只翻变动的行)
    blocks, title_trs = translate_blocks(blocks, state, [(latest["name"], title_rep)])
    latest["title_cn"] = title_trs[0] if title_trs else title_rep
    t_trans = time.perf_counter() - t_seg

    t_seg = time.perf_counter()
    parts = build_message_parts(latest, blocks)
    chunks = chunk_parts(parts, MAX_MSG_LEN, MAX_MSG_BYTES)
    t_layout = time.perf_counter() - t_seg
    tr_n = len(state.get("tr_cache", {}))
    print(f"[info] 排版完成: {len(parts)} 段, 拆成 {len(chunks)} 条消息 "
          f"(抓取{t_fetch:.1f}s/翻译{t_trans:.1f}s/排版{t_layout:.1f}s, 翻译缓存{tr_n}条)", flush=True)
    for i, c in enumerate(chunks, 1):
        print(f"[info] 消息{i}: {len(c)} 字符 / {len(c.encode('utf-8'))} 字节", flush=True)

    if DRY_RUN:
        print("\n================ 排版预览（DRY_RUN 不推送） ================\n", flush=True)
        for i, c in enumerate(chunks, 1):
            print(f"-------- 第 {i}/{len(chunks)} 条 --------")
            print(c)
            print()
        return

    # 5) 推送
    # ack 基于内容 hash(同内容恒定, 纯数字格式兼容接口):
    # 若黑盒按 heychat_ack_id 幂等去重, 推送超时但实际成功后的重推
    # 不会在频道产生重复消息(原来是时间戳, 每次重推都不同)
    ack_base = str(int(h[:12], 16))   # 12 hex = 48bit, < 2^53, 保持数字字符串
    ok_all = True
    for i, c in enumerate(chunks, 1):
        ack = f"{ack_base}{i:02d}"
        if not send_heychat(c, ack):
            ok_all = False
            break
        time.sleep(0.6)  # 避免触发限频

    # 6) 记录状态（推送成功后）
    if ok_all:
        state["pushed_id"] = str(latest["id"])
        state["pushed_hash"] = h
        state["pushed_at"] = datetime.now(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M:%S")
        state["title"] = latest.get("name")
        state.pop("fail_count", None)   # 推送成功, 连续失败计数清零
        state.pop("last_error", None)
        state.pop("last_fail_at", None)
        save_state(state)
        print(f"[info] 状态已记录: {latest['id']} / {h}", flush=True)
    else:
        print("[error] 推送未全部成功, 不更新状态(下次会重试)", flush=True)
        _record_failure(state, "推送接口返回失败(部分消息未发出)")
        sys.exit(1)


def self_test() -> bool:
    """离线冒烟自检: 不联网、不翻译, 只验证解析/术语/时间行/拆条等本地逻辑。
    用法: python main.py --self-test  (CI 校验步骤也可调用)"""
    ok = True

    def check(name, cond):
        nonlocal ok
        print(("  [PASS] " if cond else "  [FAIL] ") + name, flush=True)
        if not cond:
            ok = False

    print("[self-test] 冒烟自检开始", flush=True)
    print(f"[self-test] 术语表 {len(TERMS)} 条, 预编译正则 {len(_TERM_PATTERNS)} 条", flush=True)

    # 1) 术语加载与替换
    t = apply_terms("I am a Hero in the MapleStory Classic World.")
    check("术语替换(英雄/冒险岛怀旧服)", "英雄" in t and "冒险岛怀旧服" in t)
    check("术语边界(不误替换 Heroine)", "英雄ine" not in apply_terms("Heroine"))

    # 2) HTML 解析 + 小标题识别
    html = ("<h2><strong>Times:</strong></h2>"
            "<p>August 11, 2026 (PDT): 4:00 PM - 6:00 PM</p>"
            "<ul><li>Item one</li><li>Item two<ul><li>Nested</li></ul></li></ul>"
            "<p>Text with <strong>bold</strong> and <img src='x.png'> image.</p>"
            "<script>var x=1;</script>")
    blocks = classify_blocks(parse_body(html))
    kinds = [b[0] for b in blocks]
    check("解析+小标题识别", kinds == ["heading", "para", "list", "para"], )
    list_block = next(b for b in blocks if b[0] == "list")
    items = list_block[1]
    check("嵌套列表展平(3项, 末项depth=1)", len(items) == 3 and items[-1][1] == 1)
    para_text = " ".join(b[1] for b in blocks if b[0] == "para")
    check("粗体转**/图片剔除/script跳过",
          "**bold**" in para_text and "x.png" not in para_text and "var x" not in para_text)

    # 3) 时间/日期行判断
    check("时区行保留(PDT)", _should_keep_original("4:00 PM (PDT): 5:00 PM (PDT)"))
    check("时区行保留(CDT)", _should_keep_original("4:00 PM (CDT)"))
    check("小写 pm 保留", _should_keep_original("4:00 p.m. (pdt)"))
    check("纯日期行交翻译", not _should_keep_original("Tuesday, August 11, 2026"))
    check("网址行保留", _should_keep_original("https://maplestory.nexon.net/news/43962"))
    check("含中文行保留", _should_keep_original("维护将在明天开始"))

    # 4) 粗体清理与平衡
    check("奇数粗体清理", fix_bold_balance("**a**b**") == "ab")
    check("偶数粗体保留", fix_bold_balance("**a**") == "**a**")
    check("clean_markdown 空粗体", clean_markdown("** **") == "**")

    # 5) 拆条(含超长单块按行拆分)
    long_part = "\n".join("Line %d " % i + "x" * 100 for i in range(30))
    chunks = chunk_parts(["# 标题", long_part, "尾部"], 300)
    check("超长单块被拆成多条", len(chunks) > 1)
    check("所有消息不超限", all(len(c) <= 310 for c in chunks))
    check("普通拆条", len(chunk_parts(["a" * 100] * 20, 500)) > 1)
    # 字节双限制: 30 行 × 100 汉字, 限制 3000 字节
    zh_part = "\n".join("中" * 100 for _ in range(30))
    zh_chunks = chunk_parts(["标题", zh_part], 1500, 3000)
    check("字节限制生效(拆成多条)", len(zh_chunks) > 1)
    check("字节限制下不超限", all(len(c.encode("utf-8")) <= 3020 for c in zh_chunks))

    # 6) 术语表质量: 大小写变体冲突 / 空值(CI 常驻拦截, 防止新增冲突悄悄进库)
    from collections import defaultdict
    grp = defaultdict(dict)
    for k, v in TERMS.items():
        grp[k.casefold()][k] = v
    conflicts = [d for d in grp.values() if len(d) > 1 and len(set(d.values())) > 1]
    empty_vals = [k for k, v in TERMS.items() if not str(v).strip()]
    check("术语表无大小写冲突(%d对)" % len(conflicts), not conflicts)
    if conflicts:
        for d in conflicts:
            print("       冲突:", d, flush=True)
    check("术语表无空值(%d个)" % len(empty_vals), not empty_vals)
    if empty_vals:
        print("       空值:", empty_vals, flush=True)

    # 7) LLM JSON 数组解析器(线上 DeepSeek 批量翻译全挂的回归点)
    p = _parse_llm_json_array
    check("标准 JSON 数组", p('["a", "b"]') == ["a", "b"])
    check("代码块包裹", p('```json\n["a", "b"]\n```') == ["a", "b"])
    check("前后解释文字", p('好的：\n["a", "b"]') == ["a", "b"])
    check("译文含方括号", p('翻译如下：\n["[已完成] 维护", "冷却 [Lv.1]"]') == ["[已完成] 维护", "冷却 [Lv.1]"])
    check("输出被截断", p('["a", "b') is None)
    check("非数组输出", p('{"foo": 1}') is None)

    print(("[self-test] 结果: " + ("全部通过" if ok else "存在失败")), flush=True)
    return ok


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(0 if self_test() else 1)
    try:
        main()
    except Exception as e:
        print(f"[error] 运行异常: {e}", flush=True)
        traceback.print_exc()   # 完整堆栈, 方便在 CI 日志里定位根因
        try:
            _record_failure(load_state(), f"{type(e).__name__}: {e}")
        except Exception:
            pass
        sys.exit(1)
