# -*- coding: utf-8 -*-
"""列出机器人已加入的房间, 以及各房间里的频道(尽量), 方便找 channel_id。

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


def walk(obj, room_id="", depth=0):
    """从任意 JSON 里抽出 id/name 组合, 打印成一行一个。"""
    if depth > 8:
        return
    if isinstance(obj, dict):
        rid = next((obj[k] for k in ("channel_id", "cid", "id", "group_id")
                    if isinstance(obj.get(k), (str, int)) and str(obj.get(k)).isdigit()), None)
        name = next((obj[k] for k in ("channel_name", "name", "title", "group_name")
                     if isinstance(obj.get(k), str) and obj.get(k).strip()), None)
        if rid and name:
            print(f"  CHANNEL? id={rid} name={name!r} (room={room_id or '?'})", flush=True)
        for v in obj.values():
            walk(v, room_id, depth + 1)
    elif isinstance(obj, list):
        for v in obj[:200]:
            walk(v, room_id, depth + 1)


def main() -> int:
    if not TOKEN:
        print("[error] 缺少 HEYCHAT_TOKEN", flush=True)
        return 1
    try:
        data = req("/chatroom/v2/room/joined", {"offset": 0, "limit": 50})
    except Exception as e:
        print(f"[error] 房间列表查询失败: {e}", flush=True)
        return 1
    rooms = ((data.get("result") or {}).get("rooms") or [])
    print(f"== 机器人已加入 {len(rooms)} 个房间 ==")
    print(f"[debug] joined result keys={list(((data.get('result') or {})).keys())}")
    print(f"[debug] 原始返回(截断): {json.dumps(data, ensure_ascii=False)[:1200]}")
    room_ids = []
    for r in rooms:
        if isinstance(r, dict):
            print(f"ROOM id={r.get('room_id')} name={r.get('room_name')!r}", flush=True)
            room_ids.append(str(r.get("room_id")))
        else:
            print(f"ROOM? {r!r}", flush=True)
            room_ids.append(str(r))
    if not room_ids:
        room_ids = [str(x) for x in (os.environ.get("ROOM_ID", "").split(",")) if x.strip()]

    only = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("ROOM_ID", "")
    targets = [rid for rid in room_ids if not only or rid == str(only)]
    for rid in targets:
        if not rid.isdigit():
            print(f"[warn] 跳过非数字房间 id: {rid!r}", flush=True)
            continue
        print(f"\n== 房间 {rid} 的频道 ==", flush=True)
        try:
            view = req("/chatroom/v2/room/view", {"room_id": rid})
            print(f"  [room/view keys] {list((view.get('result') or {}).keys())}", flush=True)
            walk(view.get("result") or {}, str(rid))
        except Exception as e:
            print(f"  [warn] room/view 失败: {e}", flush=True)

    print("\n== 机器人所在频道 (channel/which_user) ==", flush=True)
    for params in ({"room_id": only} if only else {}, {}):
        try:
            u = req("/chatroom/v2/channel/which_user", params)
            print(f"  [which_user {params}] {json.dumps(u, ensure_ascii=False)[:600]}", flush=True)
        except Exception as e:
            print(f"  [warn] which_user {params} 失败: {e}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
