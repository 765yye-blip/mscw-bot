#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
行级 vs 段落级翻译对比工具
==========================
目的: 回答 main.py 里的一个主动取舍(Q4)——翻译按"行"做是为了行级缓存(TR_CACHE)
      安全, 但代价是多句段落被拆成多个独立翻译单元, 逐行送引擎, 无跨行上下文,
      指代/语义可能割裂。本工具抓取真实 MSCW 公告, 量化两种模式在
      "翻译单元数量(≈API 调用次数/成本)"与"上下文割裂程度"上的差异,
      可选(--translate)实际调用 DeepSeek 对比译文质量, 帮你决定要不要改。

用法:
  python bench_translate.py [--count N] [--translate] [--no-fetch]

  --count N      抓取最近 N 条公告(默认 5)
  --translate    实际调用 DeepSeek 对比行级/段落级译文(需设置 DEEPSEEK_API_KEY)
  --no-fetch     不联网, 用内置示例公告跑统计(离线自检用)

输出:
  每条公告报告 行级单元数 / 段落级单元数 / 多行段落分布;
  --translate 时附两条模式的译文对比片段。
"""

import argparse
import json
import os
import re
import sys

import main as M   # 复用 main.py 的抓取/解析/术语/翻译逻辑


# ---------------------------------------------------------------------------
# 1. 翻译单元收集: 与 main.translate_blocks 的判定口径完全一致
# ---------------------------------------------------------------------------
def collect_line_units(blocks):
    """行级模式: 需要送翻译引擎的单元列表(与 main.py 实际行为一致)。
    每条为 (块类型, 位置, 待翻译文本)。"""
    units = []
    for bi, blk in enumerate(blocks):
        if blk[0] == "heading":
            rep = M.apply_terms(blk[1])
            ov = M.HEADING_OVERRIDES.get(blk[1].strip())
            if ov is None and re.search(r"[A-Za-z]", rep):
                units.append(("heading", bi, rep))
        elif blk[0] == "para":
            for li, line in enumerate(blk[1].split("\n")):
                if not M._should_keep_original(line):
                    rep = M.apply_terms(line)
                    if re.search(r"[A-Za-z]", rep):
                        units.append(("para", (bi, li), rep))
        elif blk[0] == "list":
            for ii, item in enumerate(blk[1]):
                it = item[0] if isinstance(item, tuple) else item
                if not M._should_keep_original(it):
                    rep = M.apply_terms(it)
                    if re.search(r"[A-Za-z]", rep):
                        units.append(("list", (bi, ii), rep))
    return units


def collect_block_units(blocks):
    """段落级模式: 以块为翻译单元。块内任意一行需翻译则整块作为一个单元。
    注: 段落级模式下"时间行"会整段送引擎, 需要额外的行级保护逻辑,
    本统计假设最简实现(整段直送), 实际改造时需在提示词/后处理里保留时间行。"""
    units = []
    for bi, blk in enumerate(blocks):
        if blk[0] == "heading":
            rep = M.apply_terms(blk[1])
            ov = M.HEADING_OVERRIDES.get(blk[1].strip())
            if ov is None and re.search(r"[A-Za-z]", rep):
                units.append(("heading", bi, rep))
        elif blk[0] == "para":
            lines = blk[1].split("\n")
            if any(not M._should_keep_original(l) and
                   re.search(r"[A-Za-z]", M.apply_terms(l)) for l in lines):
                # 整段拼接为 1 个单元(保留换行, 提示词里要求保留结构)
                units.append(("para", bi, "\n".join(M.apply_terms(l) for l in lines)))
        elif blk[0] == "list":
            raws = [item[0] if isinstance(item, tuple) else item for item in blk[1]]
            reps = [M.apply_terms(r) for r in raws]
            if any(not M._should_keep_original(r) and re.search(r"[A-Za-z]", rep)
                   for r, rep in zip(raws, reps)):
                units.append(("list", bi, "\n".join(reps)))
    return units


def multi_line_para_stats(blocks):
    """统计多行段落(行级模式下上下文被割裂的位置)。"""
    stats = []
    for bi, blk in enumerate(blocks):
        if blk[0] == "para":
            lines = blk[1].split("\n")
            need = [l for l in lines if not M._should_keep_original(l)]
            if len(need) >= 2:      # 至少 2 行需要翻译 -> 上下文割裂
                stats.append((bi, len(need), len(lines), blk[1][:120]))
    return stats


# ---------------------------------------------------------------------------
# 2. 离线示例(自检用, 模拟真实公告里的多行段落)
# ---------------------------------------------------------------------------
SAMPLE_BLOCKS = [
    ("heading", "Times:"),
    ("para", "We will be performing scheduled maintenance starting on **Tuesday**, "
             "August 11, 2026 at 4:00 PM PDT (7:00 PM EDT)."),
    ("para", "We anticipate the maintenance to last approximately **2** hours.\n"
             "The following servers will be affected:\n"
             "All Classic World game servers."),
    ("list", [("Back up your items.", 0), ("Stay tuned for updates.", 0)]),
]


# ---------------------------------------------------------------------------
# 3. 报告
# ---------------------------------------------------------------------------
def analyze(article, blocks):
    line_units = collect_line_units(blocks)
    block_units = collect_block_units(blocks)
    mls = multi_line_para_stats(blocks)

    print("=" * 66)
    print(f"公告: {article.get('name')}  (id={article.get('id')})")
    print(f"  正文块: {len(blocks)}  (heading/para/list/divider 按解析结果)")
    print(f"  行级模式: {len(line_units)} 个翻译单元(按行, 无跨行上下文)")
    print(f"  段落级模式: {len(block_units)} 个翻译单元(按块, 有上下文)")
    if line_units:
        print(f"  单元数对比: 段落级可减少 {len(line_units) - len(block_units)} 次 API 调用 "
              f"({(1 - len(block_units) / len(line_units)) * 100:.0f}%)")
    if mls:
        print(f"  上下文割裂: {len(mls)} 个多行段落(行级模式下被拆开翻译):")
        for bi, n_need, n_all, preview in mls[:3]:
            print(f"    - 段落#{bi}: {n_need}/{n_all} 行需翻译 | 预览: {preview!r}...")
        if len(mls) > 3:
            print(f"    ... 共 {len(mls)} 个")
    else:
        print("  上下文割裂: 0 个多行段落(行级/段落级差异很小)")
    return line_units, block_units


def translate_compare(blocks, line_units, block_units):
    """实际调用 DeepSeek 对比两种模式的译文(需要 DEEPSEEK_API_KEY)。
    只对比"多行段落"——行级模式下这些段落被拆成多行分别翻译, 是上下文割裂的位置。
    注意: line_units 里 para 单元是单行(按行拆分), 所以这里直接遍历原始 blocks 定位。"""
    if not (M.DEEPSEEK_API_KEY and M.DEEPSEEK_API_KEY != "XXX"):
        print("  [--translate] 需要设置 DEEPSEEK_API_KEY 环境变量, 跳过译文对比")
        return
    # 定位多行段落: (段落块索引, 该段在 line_units 中的行单元下标, 该段在 block_units 中的下标)
    line_pos = {}          # (bi, li) -> line_units 下标
    for i, u in enumerate(line_units):
        if u[0] == "para":
            line_pos[u[1]] = i
    block_pos = {}         # bi -> block_units 下标
    for i, u in enumerate(block_units):
        if u[0] == "para":
            block_pos[u[1]] = i
    targets = []
    for bi, blk in enumerate(blocks):
        if blk[0] != "para":
            continue
        lines = blk[1].split("\n")
        idxs = [line_pos[(bi, li)] for li in range(len(lines)) if (bi, li) in line_pos]
        if len(idxs) >= 2 and bi in block_pos:
            targets.append((bi, idxs, block_pos[bi]))
    if not targets:
        print("  [--translate] 本次公告没有多行段落, 无需译文对比"
              "(真实维护公告段落基本单行, 行级/段落级差异很小)")
        return
    print("  [--translate] 调用 DeepSeek 对比译文...")
    all_line_texts = [u[2] for u in line_units]
    all_block_texts = [u[2] for u in block_units]
    tr_line = M._translate_deepseek_batch(all_line_texts)
    tr_block = M._translate_deepseek_batch(all_block_texts)
    if tr_line is None or tr_block is None:
        print("  [--translate] 翻译调用失败, 跳过对比")
        return
    for bi, line_idxs, blk_idx in targets[:3]:
        print(f"\n  ---- 多行段落#{bi} 译文对比 ----")
        print(f"  原文(整段): {blocks[bi][1]!r}")
        print(f"  行级译文(各行分别译): {' / '.join(tr_line[i] for i in line_idxs)}")
        print(f"  段落级译文(整段译):   {tr_block[blk_idx]!r}")


def offline_check():
    """不联网跑一遍统计, 验证工具自身逻辑(类似 main.py 的 self-test)。"""
    print("[offline] 内置示例公告统计(自检):")
    blocks = M.classify_blocks(SAMPLE_BLOCKS)
    line_units, block_units = analyze({"id": 0, "name": "[Sample] Maintenance Notice"}, blocks)
    ok = True
    if len(line_units) == 0 or len(block_units) == 0:
        print("[offline] FAIL: 示例公告应产生翻译单元")
        ok = False
    if not multi_line_para_stats(blocks):
        print("[offline] FAIL: 示例应包含多行段落")
        ok = False
    print(f"[offline] {'PASS' if ok else 'FAIL'}")
    return ok


def main():
    ap = argparse.ArgumentParser(description="行级 vs 段落级翻译对比工具")
    ap.add_argument("--count", type=int, default=5, help="抓取最近 N 条公告(默认 5)")
    ap.add_argument("--translate", action="store_true", help="实际调用 DeepSeek 对比译文")
    ap.add_argument("--no-fetch", action="store_true", help="不联网, 用内置示例跑统计")
    args = ap.parse_args()

    if args.no_fetch:
        sys.exit(0 if offline_check() else 1)

    print(f"[info] 抓取最近 {args.count} 条 MSCW 公告...")
    news = M.fetch_news_list()[:args.count]
    print(f"[info] 共 {len(news)} 条待分析\n")
    for n in news:
        try:
            detail = M.fetch_news_detail(n["id"])
        except Exception as e:
            print(f"[warn] id={n['id']} 详情拉取失败: {e}, 跳过")
            continue
        blocks = M.classify_blocks(M.parse_body(detail.get("body") or ""))
        if not blocks:
            print(f"[warn] id={n['id']} 无文本块(纯图片公告), 跳过")
            continue
        line_units, block_units = analyze(n, blocks)
        if args.translate:
            translate_compare(blocks, line_units, block_units)
    print("\n[info] 完成。单元数差异=API 调用次数差异; 多行段落=上下文割裂点。\n"
          "若差异显著且你/读者在意翻译自然度, 值得改段落级翻译(注意保留时间行保护);\n"
          "若差异很小, 维持行级缓存(成本/复杂度更低)即可。")


if __name__ == "__main__":
    main()
