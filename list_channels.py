# -*- coding: utf-8 -*-
"""列出机器人已加入的房间, 以及房间里的频道(多接口尝试), 方便找 channel_id。

用法: HEYCHAT_TOKEN=... python list_channels.py [room_id]
"""
import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN = os.environ.get("HEYCHAT_TOKEN", "")
BASE = "https://chat.xiaoheihe.cn"
COMMON = ("client_type=heybox_chat&x_client_type=web&os_type=web&x_os_type=bot"
          "&x_app=heybox_chat&chat_os_type=bot&chat_version=1.30.0")


def req(path, params=None):
    url = f"{BASE}{path}?{COMMON}"
    if params:
        url += "&" + urllib.parse.urlencode(params)
    r = urllib.request.Request(url, headers={"token": TOKEN,
                                             "Content-Type": "application/json;charset=UTF-8"})
    with urllib.request.urlopen(r, timeout=25) as resp:
        return json.loads(resp.read().decode("utf-8"))


def walk(obj, depth=0, out=None):
    """抽出所有 (id, 名字) 组合。"""
    out = [] if out is None else out
    if depth > 9:
        return out
    if isinstance(obj, dict):
        name = next((obj[k] for k in ("channel_name", "group_name", "name", "title")
                     if isinstance(obj.get(k), str) and obj.get(k).strip()), None)
        if name:
            ids = {k: obj[k] for k in ("channel_id", "group_id", "id", "cid")
                   if isinstance(obj.get(k), (str, int))}
            if ids:
                out.append((ids, name))
        for v in obj.values():
            walk(v, depth + 1, out)
    elif isinstance(obj, list):
        for v in obj[:300]:
            walk(v, depth + 1, out)
    return out


def main() -> int:
    if not TOKEN:
        print("[error] 缺少 HEYCHAT_TOKEN", flush=True)
        return 1
    data = req("/chatroom/v2/room/joined", {"offset": 0, "limit": 50})
    res = data.get("result") or {}
    rooms = res.get("rooms") or []
    if isinstance(rooms, dict):
        rooms = rooms.get("rooms") or []
    print(f"== 机器人已加入 {len(rooms)} 个房间 ==", flush=True)
    room_ids = []
    for r in rooms:
        if isinstance(r, dict):
            rid = str(r.get("room_id"))
            room_ids.append(rid)
            print(f"ROOM id_len={len(rid)} name={r.get('room_name')!r}", flush=True)
        else:
            room_ids.append(str(r))

    only = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ROOM_ID", "")
    targets = [rid for rid in room_ids if not only or rid == str(only)] or room_ids

    for rid in targets:
        print(f"\n== 房间 id_len={len(rid)} 的频道 ==", flush=True)
        for path, params in (
            ("/chatroom/v2/room/view", {"room_id": rid}),
            ("/chatroom/v2/channel/list", {"room_id": rid, "offset": 0, "limit": 100}),
            ("/chatroom/v2/room/channel/list", {"room_id": rid, "offset": 0, "limit": 100}),
            ("/chatroom/v2/room/get_channels", {"room_id": rid}),
        ):
            try:
                d = req(path, params)
            except Exception as e:
                print(f"  [{path}] 异常 {e}", flush=True)
                continue
            st = d.get("status")
            body = d.get("result")
            pairs = walk(body)
            print(f"  [{path}] status={st} 频道候选 {len(pairs)} 个 "
                  f"keys={list(body.keys()) if isinstance(body, dict) else type(body).__name__}",
                  flush=True)
            for ids, name in pairs:
                idd = ids.get("channel_id") or ids.get("group_id") or ids.get("cid") or ids.get("id")
                print(f"      CHANNEL channel_id={idd} name={name!r}", flush=True)
            if not pairs:
                print(f"      原始(截断): {json.dumps(d, ensure_ascii=False)[:500]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
