#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
黑盒语音 房间/频道 ID 查询工具
==============================
用法:
  1) 先设置环境变量 HEYCHAT_TOKEN（机器人 token，在 bot.xiaoheihe.cn 机器人详情页）
     Windows PowerShell:   $env:HEYCHAT_TOKEN = "你的token"
     macOS/Linux/GitBash:  export HEYCHAT_TOKEN="你的token"
  2) 列出机器人已加入的所有房间:
     python find_ids.py
  3) 查看某个房间的详细信息(结果里通常包含频道列表, 找到想推送的频道 id):
     python find_ids.py <room_id>

接口参考: https://apifox.com/apidoc/shared/43256fe4-9a8c-4f22-949a-74a3f8b431f5/
"""

import json
import os
import sys
import urllib.parse
import urllib.request

TOKEN = os.environ.get("HEYCHAT_TOKEN", "")
BASE = "https://chat.xiaoheihe.cn"
COMMON = (
    "client_type=heybox_chat&x_client_type=web&os_type=web&x_os_type=bot"
    "&x_app=heybox_chat&chat_os_type=bot&chat_version=1.30.0"
)


def req(path, params=None):
    url = f"{BASE}{path}?{COMMON}"
    if params:
        url += "&" + urllib.parse.urlencode(params)
    r = urllib.request.Request(url, headers={
        "token": TOKEN,
        "Content-Type": "application/json;charset=UTF-8",
    })
    with urllib.request.urlopen(r, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    if not TOKEN:
        print("[error] 请先设置环境变量 HEYCHAT_TOKEN（机器人 token）")
        sys.exit(1)

    print("== 机器人已加入的房间 ==")
    try:
        data = req("/chatroom/v2/room/joined", {"offset": 0, "limit": 50})
    except Exception as e:
        print(f"[error] 查询失败: {e}")
        print("提示: 请确认 token 正确, 且机器人已通过邀请链接加入目标房间")
        sys.exit(1)
    result = data.get("result") or {}
    rooms = result.get("rooms") or []
    total = result.get("total", len(rooms))
    print(f"共 {total} 个房间:\n")
    for r in rooms:
        print(f"  房间ID: {r.get('room_id')}")
        print(f"  房间名: {r.get('room_name')}")
        print()

    room_id = sys.argv[1] if len(sys.argv) > 1 else None
    if room_id:
        print(f"== 房间 {room_id} 详细信息 ==")
        try:
            view = req("/chatroom/v2/room/view", {"room_id": room_id})
        except Exception as e:
            print(f"[error] 查询失败: {e}")
            sys.exit(1)
        print(json.dumps(view, ensure_ascii=False, indent=2))
        print("\n从上面的返回结果中找到目标『频道』的 channel_id 即可。")
        print("如果结果里没有频道列表, 可以直接把机器人拖进目标频道, "
              "再调用 /chatroom/v2/channel/which_user 查看机器人所在频道。")
    else:
        print("提示: 想看某个房间的频道列表, 请执行: python find_ids.py <room_id>")


if __name__ == "__main__":
    main()
