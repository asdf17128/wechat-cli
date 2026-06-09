#!/usr/bin/env python3
"""
wechat-cli — multi-command WeChat automation via WebDriverAgent.

Subcommands:
    send TARGET MESSAGE         — send a message to a chat (same as wechat-send)
    add-friend WX_ID [--msg M]  — search for a wechat id / phone, send friend request
    del-friend NAME             — remove a contact (WARNING: irreversible)
    moments-post TEXT           — post a text-only update to Moments (朋友圈)
    moments-del N               — delete the Nth most-recent Moments post (1=newest)

All commands require:
    - WDA at http://localhost:8100 ready (run ~/code1/mobile_mcp/wda-up.sh)
    - iPhone unlocked, WeChat logged in

Exit codes are documented per-subcommand below (see --help).

Rate-limit warning: WeChat aggressively tracks friend-add / friend-delete /
moments-post frequency. Stay under ~5 friend ops per day per account.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

import wda

WECHAT_BUNDLE = "com.tencent.xin"

# UI constants — verified on iPhone 12 Pro / iOS 26 / WeChat 8.0.5x
PLUS_BUTTON_XY = (354, 79)        # top-right + in chat list
ADD_FRIEND_MENU_XY = (332, 175)   # 添加朋友 row in + popup
SEARCH_BAR_Y_ADD = 117            # add-friend page search field y-center
INPUT_BAR_Y_DEFAULT = 770         # WeChat chat input before keyboard rises
KEYBOARD_DELETE_X = 355
KEYBOARD_DELETE_Y = 668
SEND_BUTTON_X = 340
SEND_BUTTON_Y = 740


class WeChatError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


# ----- shared WeChat helpers ----------------------------------------------


def check_repair_dialog(sid: str) -> None:
    """Auto-dismiss WeChat's 连续异常修复 dialog by tapping 下一步 until
    it's gone. Bail with code 2 only on login wall.
    """
    if wda.find(sid, 'label CONTAINS "重新登录" OR label CONTAINS "请重新登录"'):
        raise WeChatError(2, "WeChat is not signed in")

    if not wda.find(sid, 'label CONTAINS "微信连续异常"'):
        return

    for _ in range(8):
        btn = wda.find(sid, '(name == "下一步" OR label == "下一步")')
        if not btn:
            break
        wda.tap_rect_center(sid, btn)
        time.sleep(2.0)
    else:
        raise WeChatError(2, "WeChat 异常修复 dialog couldn't be auto-dismissed")
    time.sleep(2.5)


def on_chat_list(sid: str) -> bool:
    """True iff we're on the chat list (微信 tab)."""
    return bool(wda.find(sid, 'type == "XCUIElementTypeSearchField" AND name == "搜索"'))


def on_discover_tab(sid: str) -> bool:
    """True iff we're on the 发现 (Discover) tab."""
    # 发现 page shows '朋友圈' / '扫一扫' rows
    return bool(wda.find(sid, 'type == "XCUIElementTypeStaticText" AND name == "朋友圈"'))


def ensure_chat_list(sid: str) -> None:
    """Navigate to WeChat chat list (微信 tab). Idempotent."""
    if on_chat_list(sid):
        return

    # Try back arrows first (climb out of nested pages)
    for _ in range(3):
        back = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "返回" OR label == "返回")')
        if not back:
            break
        wda.tap_rect_center(sid, back)
        time.sleep(0.8)
        if on_chat_list(sid):
            return

    # Tap 微信 bottom-nav tab
    for _ in range(3):
        tabs = wda.find_all(sid, 'type == "XCUIElementTypeButton" AND name == "微信"')
        for tab in tabs:
            r = wda.get_rect(sid, tab)
            if r.get("y", 0) > 700:
                wda.tap_rect_center(sid, tab)
                time.sleep(1.0)
                if on_chat_list(sid):
                    return
                break
        else:
            wda.tap_xy(sid, 35, 800)
            time.sleep(1.0)
            if on_chat_list(sid):
                return
        time.sleep(0.5)


def back_to_chat_list(sid: str) -> None:
    """Repeated 返回 button + ensure_chat_list."""
    for _ in range(3):
        back = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "返回" OR label == "返回")')
        if not back:
            break
        wda.tap_rect_center(sid, back)
        time.sleep(0.8)
    ensure_chat_list(sid)


# ----- chat row finder (used by send + del-friend) ------------------------


def find_chat_row(sid: str, target: str) -> str:
    """Find a chat list row whose label matches target exactly."""
    elem = _find_chat_row_predicate(sid, target) or _find_chat_row_walk(sid, target)
    if elem:
        return elem
    raise WeChatError(3, f"chat '{target}' not found in chat list")


def _find_chat_row_predicate(sid: str, target: str) -> str | None:
    return _filter_chat_list_y(sid, wda.find_all(
        sid, f'type == "XCUIElementTypeStaticText" AND (label == "{target}" OR name == "{target}")'
    ))


def _find_chat_row_walk(sid: str, target: str) -> str | None:
    all_st = wda.find_all(sid, 'type == "XCUIElementTypeStaticText"')
    matches = [e for e in all_st if wda.get_attr(sid, e, "label") == target]
    return _filter_chat_list_y(sid, matches)


def _filter_chat_list_y(sid: str, candidates: list[str]) -> str | None:
    for elem in candidates:
        r = wda.get_rect(sid, elem)
        if 130 <= r.get("y", 0) <= 700:
            return elem
    return None


# ----- input + send shared by send / add-friend verification msg ---------


def find_input_field(sid: str) -> str | None:
    return wda.find(sid, 'type == "XCUIElementTypeTextView"')


def focus_input(sid: str) -> None:
    elem = find_input_field(sid)
    if elem:
        try:
            wda.tap_rect_center(sid, elem)
            time.sleep(1.5)
            return
        except Exception:
            pass
    wda.tap_xy(sid, 130, INPUT_BAR_Y_DEFAULT)
    time.sleep(1.5)


def clear_draft(sid: str) -> None:
    elem = find_input_field(sid)
    if elem and wda.clear_input(sid, elem):
        time.sleep(0.3)
        return
    # Fallback: long-press delete key.
    n = len(wda.get_attr(sid, elem, "text")) if elem else 0
    duration_ms = max(2000, min(30000, (n + 20) * 150))
    wda.long_press_xy(sid, KEYBOARD_DELETE_X, KEYBOARD_DELETE_Y, duration_ms)
    time.sleep(0.5)


def type_message(sid: str, message: str) -> None:
    wda.type_keys(sid, message)
    time.sleep(0.5)


def tap_send_button(sid: str) -> None:
    """Tap the keyboard's blue Send button (visible only when input has text)."""
    btn = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "Send" OR name == "send" OR label == "发送")')
    if btn:
        wda.tap_rect_center(sid, btn)
    else:
        wda.tap_xy(sid, SEND_BUTTON_X, SEND_BUTTON_Y)
    time.sleep(2.0)


def verify_input_empty(sid: str) -> bool:
    elem = find_input_field(sid)
    if not elem:
        return False
    return wda.get_attr(sid, elem, "text").strip() == ""


# ==========================================================================
# subcommand: send
# ==========================================================================


def open_chat_by_name(sid: str, target: str) -> None:
    row = find_chat_row(sid, target)
    r = wda.get_rect(sid, row)
    label_y = r.get("y", 200) + r.get("height", 25) // 2
    wda.tap_xy(sid, 200, label_y)
    time.sleep(2.5)

    for _ in range(6):
        hdr = wda.find(sid, f'type == "XCUIElementTypeNavigationBar" '
                            f'AND (name == "{target}" OR name BEGINSWITH "{target},")')
        if hdr:
            return
        time.sleep(0.5)

    back_to_chat_list(sid)
    raise WeChatError(3, f"opened a chat but header is not '{target}' (row was at y={r.get('y')})")


def cmd_send(args: argparse.Namespace) -> int:
    target = args.to or args.target
    message = sys.stdin.read().rstrip("\n") if args.stdin else (args.msg or args.message)
    if not target or not message:
        print("wechat-cli send: need TARGET and MESSAGE", file=sys.stderr)
        return 5

    if not wda.wda_ready():
        print("wechat-cli: WDA at :8100 not ready — run wda-up.sh", file=sys.stderr)
        return 1

    deadline = time.time() + 300
    last_err: WeChatError | None = None
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        sid = wda.new_session(WECHAT_BUNDLE)
        if args.verbose:
            print(f"[send] session={sid[:8]} attempt={attempt}", file=sys.stderr)
        try:
            time.sleep(2.0)
            check_repair_dialog(sid)
            ensure_chat_list(sid)
            try:
                open_chat_by_name(sid, target)
            except WeChatError as e:
                last_err = e
                if e.code != 3:
                    raise
                if args.verbose:
                    print(f"[send] chat-find failed: {e}; recreating session", file=sys.stderr)
                continue

            focus_input(sid)
            clear_draft(sid)
            type_message(sid, message)
            tap_send_button(sid)

            if not verify_input_empty(sid):
                raise WeChatError(4, "send didn't go through (input still has text)")
            if args.verbose:
                print(f"[send] verified sent", file=sys.stderr)
            if not args.terminate:
                try:
                    back_to_chat_list(sid)
                except Exception:
                    pass
            return 0
        except WeChatError as e:
            print(f"wechat-cli send: {e}", file=sys.stderr)
            return e.code
        finally:
            if args.terminate:
                wda.terminate_app(sid, WECHAT_BUNDLE)
            wda.end_session(sid)

    print(f"wechat-cli send: {last_err}", file=sys.stderr)
    return last_err.code if last_err else 3


# ==========================================================================
# subcommand: add-friend
# ==========================================================================


def open_add_friend_page(sid: str) -> None:
    """From chat list: tap + (top-right) → tap 添加朋友. Lands on the
    "添加朋友" page with search field at top.
    """
    ensure_chat_list(sid)

    # Tap + (the top-right plus button)
    plus = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "快捷操作" OR label == "快捷操作")')
    if plus:
        wda.tap_rect_center(sid, plus)
    else:
        wda.tap_xy(sid, *PLUS_BUTTON_XY)
    time.sleep(1.8)

    # Tap 添加朋友
    add = wda.find(sid, 'type == "XCUIElementTypeStaticText" AND (name == "添加朋友" OR label == "添加朋友")')
    if add:
        wda.tap_rect_center(sid, add)
    else:
        wda.tap_xy(sid, *ADD_FRIEND_MENU_XY)
    time.sleep(2.5)

    # Verify by header text or search-field placeholder
    if not wda.find(sid, '(type == "XCUIElementTypeNavigationBar" AND name == "添加朋友") '
                         'OR (type == "XCUIElementTypeSearchField" AND label == "账号/手机号")'):
        raise WeChatError(3, "couldn't reach 添加朋友 page (UI shifted or popup blocked)")


def search_friend(sid: str, wx_id: str) -> None:
    """In add-friend page: tap the search field, type wx_id, hit search,
    wait for results to render.
    """
    # The search field is at top (y=101 height=30 typical). Tap it.
    sf = wda.find(sid, 'type == "XCUIElementTypeSearchField" AND label == "账号/手机号"')
    if sf:
        wda.tap_rect_center(sid, sf)
    else:
        wda.tap_xy(sid, 195, SEARCH_BAR_Y_ADD)
    time.sleep(1.2)

    wda.type_keys(sid, wx_id)
    time.sleep(1.2)

    # Tap the 搜索 button on the keyboard, or the in-page 搜索:<query> row
    # The latter is more reliable — WeChat shows a row "搜索: <query>" below
    # the search field. Tap that to trigger the search.
    row = wda.find(sid, f'type == "XCUIElementTypeStaticText" AND '
                        f'(label CONTAINS "搜索" OR name CONTAINS "搜索")')
    if row:
        wda.tap_rect_center(sid, row)
    else:
        # Press the keyboard Search/Return key (usually labeled "搜索")
        kbd = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "Search" OR name == "search" OR label == "搜索")')
        if kbd:
            wda.tap_rect_center(sid, kbd)
    time.sleep(3.0)


def tap_add_to_contacts(sid: str) -> None:
    """On the user-profile page, tap 添加到通讯录 button."""
    btn = wda.find(sid, '(name == "添加到通讯录" OR label == "添加到通讯录")')
    if not btn:
        raise WeChatError(3, "no '添加到通讯录' button — user may already be a friend, or "
                             "wechat-id not found, or page layout shifted")
    wda.tap_rect_center(sid, btn)
    time.sleep(2.0)


def send_friend_request(sid: str, verification_msg: str | None) -> None:
    """On the friend-request page: optionally edit the verification message,
    then tap 发送."""
    if verification_msg:
        # The verification msg is in a TextView. Tap it, clear, type new.
        tv = wda.find(sid, 'type == "XCUIElementTypeTextView"')
        if tv:
            wda.tap_rect_center(sid, tv)
            time.sleep(0.8)
            wda.clear_input(sid, tv)
            time.sleep(0.3)
            wda.type_keys(sid, verification_msg)
            time.sleep(0.5)

    # Tap 发送 button (top-right of request page, or 完成)
    send = wda.find(sid, '(name == "发送" OR label == "发送" OR name == "完成" OR label == "完成")')
    if not send:
        raise WeChatError(3, "no 发送 button on friend-request page")
    wda.tap_rect_center(sid, send)
    time.sleep(2.0)


def cmd_add_friend(args: argparse.Namespace) -> int:
    wx_id = args.wx_id
    if not wx_id:
        print("wechat-cli add-friend: need WX_ID", file=sys.stderr)
        return 5

    # Sanity: WX ID format. Reject obviously bad input.
    if not re.match(r'^[A-Za-z0-9_\-+]{4,30}$|^1\d{10}$', wx_id):
        print(f"wechat-cli add-friend: '{wx_id}' doesn't look like a wechat-id or phone "
              f"(expected 4-30 chars [A-Za-z0-9_-+] or 11-digit cn phone)", file=sys.stderr)
        return 5

    if not wda.wda_ready():
        print("wechat-cli: WDA at :8100 not ready — run wda-up.sh", file=sys.stderr)
        return 1

    sid = wda.new_session(WECHAT_BUNDLE)
    if args.verbose:
        print(f"[add-friend] session={sid[:8]}", file=sys.stderr)
    try:
        time.sleep(3.0)
        check_repair_dialog(sid)
        open_add_friend_page(sid)
        if args.verbose:
            print(f"[add-friend] on add-friend page; searching '{wx_id}'", file=sys.stderr)
        search_friend(sid, wx_id)
        if args.verbose:
            print(f"[add-friend] tapping 添加到通讯录", file=sys.stderr)
        tap_add_to_contacts(sid)
        if args.verbose:
            print(f"[add-friend] sending request", file=sys.stderr)
        send_friend_request(sid, args.msg)
        if args.verbose:
            print(f"[add-friend] done", file=sys.stderr)

        # back out to chat list to leave WeChat in a known state
        for _ in range(4):
            back = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "返回" OR label == "返回")')
            if not back:
                break
            wda.tap_rect_center(sid, back)
            time.sleep(0.8)
        ensure_chat_list(sid)
        return 0
    except WeChatError as e:
        print(f"wechat-cli add-friend: {e}", file=sys.stderr)
        return e.code
    finally:
        wda.end_session(sid)


# ==========================================================================
# main dispatcher
# ==========================================================================


def main() -> int:
    p = argparse.ArgumentParser(prog="wechat-cli", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    # send
    ps = sub.add_parser("send", help="send a message to a chat by name")
    ps.add_argument("target", nargs="?")
    ps.add_argument("message", nargs="?")
    ps.add_argument("--to")
    ps.add_argument("--msg")
    ps.add_argument("--stdin", action="store_true")
    ps.add_argument("--terminate", action="store_true",
                    help="terminate WeChat after send (default: leave on chat list)")

    # add-friend
    pa = sub.add_parser("add-friend", help="search wechat-id/phone and send friend request")
    pa.add_argument("wx_id", help="wechat ID or 11-digit CN phone")
    pa.add_argument("--msg", help="verification message (ASCII recommended)")

    args = p.parse_args()
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "add-friend":
        return cmd_add_friend(args)
    print(f"wechat-cli: unknown command {args.cmd}", file=sys.stderr)
    return 5


if __name__ == "__main__":
    sys.exit(main())
