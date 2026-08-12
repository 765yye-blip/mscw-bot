#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从《常用词.txt》(社区术语表 maplestory_cn_glossary_v2_starter) 筛选生成 terms_extra.json。
用法: 更新《常用词.txt》后重跑本脚本即可; 输出会自动补充大小写变体(小写 + Title Case)。

筛选规则(面向官方公告的正式文体):
  - 只保留公告相关类别: 维护/属性/装备/卷轴/著名物品/经典职业/技能/组队BOSS/地图/怪物/任务UI
  - 排除: 聊天口语、脏话、交易频道缩写、短语模式
  - 排除 3 字符及以下的短缩写(词边界匹配易误伤正式文本)
  - 排除带 "(看上下文)" 等注释的多义词
  - 排除纯小写普通英文单词中易误伤的"功能词"(use/need/full/return/daily 等)
  - 多译名取第一个(最接近官方名), 去掉括号注释
合并优先级: terms_extra.json 为基础, terms.json(用户主表)覆盖。
"""

import json
import os
import re

SRC = r"C:\Users\yanyu\Downloads\常用词.txt"
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "terms_extra.json")

# 公告相关类别(按序遍历, 后面的覆盖前面的同键)
KEEP = [
    "server_status",
    "stats_attributes",
    "equipment_slots",
    "items_scrolls_consumables",
    "famous_items",
    "jobs_explorer_classic",
    "skills_buffs",
    "party_boss_pq",
    "maps_regions",
    "monsters_common",
    "quest_ui_system",
]

# 正式公告里会作为普通词出现、替换会误伤的功能词(纯小写单单词才应用)
STOPWORDS = {
    "use", "need", "full", "set", "top", "bottom", "overall", "return",
    "daily", "build", "split", "carry", "pull", "taken", "permit",
    "setup", "page", "wait", "ready", "lead", "msg", "main", "site",
    "back", "good", "one", "up", "down", "rip", "door", "rock", "pig",
}


def clean_value(v: str) -> str:
    v = v.split("/")[0].strip()                # 多译名取第一个
    v = re.sub(r"\([^)]*\)", "", v).strip()    # 去掉英文括号注释
    v = v.split("（")[0].strip()                # 去掉中文括号注释
    return v


def main():
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)

    out = {}
    for cat in KEEP:
        section = data.get(cat)
        if not isinstance(section, dict):
            continue
        for en_raw, zh_raw in section.items():
            en = str(en_raw).strip()
            zh = str(zh_raw).strip()
            if len(en) <= 3:                    # 短缩写易误伤
                continue
            if "看上下文" in zh or "（" in zh and "）" in zh:
                continue
            # 纯小写单单词且命中功能词 -> 排除
            if (re.fullmatch(r"[a-z]+", en) and en in STOPWORDS):
                continue
            cv = clean_value(zh)
            if not cv:
                continue
            out[en] = cv                       # 原样(小写)
            if en != en.title():
                out[en.title()] = cv           # Title Case 变体, 匹配公告里的技能/物品名

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=True)
    print(f"写入 {OUT}: {len(out)} 条")


if __name__ == "__main__":
    main()
