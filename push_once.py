# -*- coding: utf-8 -*-
"""一次性推送: 把指定公告按最新排版推到指定房间/频道, 不看去重状态、不写 state.json。

用法:
  CHANNEL_ID=xxx ROOM_ID=yyy HEYCHAT_TOKEN=... python push_once.py <news_id> [--dry]

与 main.py 的区别(故意如此):
  - 不看 pushed_map / 不做远端复查: 已被正式频道推过的公告也能再发一份
  - 绝不 save_state(): 不影响定时任务对"哪些公告已推送"的判断
"""
import sys
import time

import main as M


def main() -> int:
    if len(sys.argv) < 2:
        print("[error] 用法: python push_once.py <news_id> [--dry]", flush=True)
        return 2
    news_id = str(sys.argv[1]).strip()
    dry = "--dry" in sys.argv

    print(f"[info] 目标: news_id={news_id} room={M.ROOM_ID or '(未设置)'} "
          f"channel={M.CHANNEL_ID or '(未设置)'} dry={int(dry)}", flush=True)
    if not dry and not (M.HEYCHAT_TOKEN and M.ROOM_ID and M.CHANNEL_ID):
        print("[error] 缺少 HEYCHAT_TOKEN / ROOM_ID / CHANNEL_ID", flush=True)
        return 1

    state = M.load_state()          # 只读, 用于复用行级翻译缓存
    M._migrate_state(state)
    news = M.fetch_news_list()
    article = next((a for a in news if str(a.get("id")) == news_id), None)
    if not article:
        print(f"[error] 公告列表里没有 id={news_id} (共 {len(news)} 条)", flush=True)
        return 1
    print(f"[info] 目标公告 id={article['id']} liveDate={article.get('liveDate')} "
          f"《{article.get('name')}》", flush=True)

    detail = M.fetch_news_detail(article["id"])
    blocks = M.classify_blocks(M.parse_body(detail.get("body") or "", M.original_link(article)))
    if not blocks:
        print("[error] 正文解析后无文本块", flush=True)
        return 1

    t0 = time.perf_counter()
    title_rep = M.apply_terms(article["name"])
    blocks, title_trs = M.translate_blocks(blocks, state, [(article["name"], title_rep)])
    article["title_cn"] = title_trs[0] if title_trs else title_rep
    t_tr = time.perf_counter() - t0

    parts = M.build_message_parts(article, blocks)
    chunks = M.add_continuation_marks(M.chunk_parts(parts, M.MAX_MSG_LEN, M.MAX_MSG_BYTES))
    print(f"[info] 排版完成：{len(parts)} 段 / {len(chunks)} 条消息 (翻译 {t_tr:.1f}s)", flush=True)
    for i, c in enumerate(chunks, 1):
        print(f"\n========== 第 {i}/{len(chunks)} 条（{len(c)} 字符 / "
              f"{len(c.encode('utf-8'))} 字节）==========", flush=True)
        print(c, flush=True)

    if dry:
        print("\n[info] --dry: 只排版不推送", flush=True)
        return 0

    sent = 0
    for c in chunks:
        if not M.send_chunk_robust(c, news_id):
            break
        sent += 1
        time.sleep(0.6)
    print(f"[info] 推送结果：{sent}/{len(chunks)} 条成功", flush=True)
    if sent == 0:
        print("[error] 一条都没发出去", flush=True)
        return 1
    print("[info] 未写 state.json（一次性推送不影响定时任务状态）", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
