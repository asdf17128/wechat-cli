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
    """Auto-dismiss WeChat's repair flow dialogs:
      - 连续异常修复 page → 下一步
      - "尝试重启 iPhone" escalation page → 暂不重启
      - Bail with code 2 only on login wall.
    """
    if wda.find(sid, 'label CONTAINS "重新登录" OR label CONTAINS "请重新登录"'):
        raise WeChatError(2, "WeChat is not signed in")

    # Loop until no repair dialog visible. Handle both 下一步 and 暂不重启.
    for _ in range(10):
        if not (wda.find(sid, 'label CONTAINS "微信连续异常"') or
                wda.find(sid, 'label CONTAINS "尝试重启 iPhone" OR label CONTAINS "尝试重启"')):
            return  # no dialog
        btn = (wda.find(sid, '(name == "下一步" OR label == "下一步")') or
               wda.find(sid, '(name == "暂不重启" OR label == "暂不重启")'))
        if not btn:
            break
        wda.tap_rect_center(sid, btn)
        time.sleep(2.0)
    if (wda.find(sid, 'label CONTAINS "微信连续异常"') or
        wda.find(sid, 'label CONTAINS "尝试重启 iPhone" OR label CONTAINS "尝试重启"')):
        raise WeChatError(2, "WeChat repair dialog couldn't be auto-dismissed "
                             "(may need iPhone restart per WeChat's escalated flow)")
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
# subcommand: del-friend
# ==========================================================================


def ensure_contacts_tab(sid: str) -> None:
    """Switch to 通讯录 tab (bottom nav, position 2)."""
    # First find the 通讯录 Button in bottom nav
    tabs = wda.find_all(sid, 'type == "XCUIElementTypeButton" AND name == "通讯录"')
    for tab in tabs:
        r = wda.get_rect(sid, tab)
        if r.get("y", 0) > 700:
            wda.tap_rect_center(sid, tab)
            time.sleep(1.5)
            return
    # Fallback: coord tap at known position
    wda.tap_xy(sid, 146, 800)
    time.sleep(1.5)


def find_contact_in_list(sid: str, name: str) -> str | None:
    """Find a contact row by exact label match in the 通讯录 tab.

    Strategy: try the direct list first (most names are visible without
    scrolling for accounts with <30 contacts). If not found, use the search
    field at top to filter, then look again.
    """
    # First pass: look directly in the visible list.
    matches = wda.find_all(
        sid, f'type == "XCUIElementTypeStaticText" AND (label == "{name}" OR name == "{name}")'
    )
    for elem in matches:
        r = wda.get_rect(sid, elem)
        if 130 <= r.get("y", 0) <= 700:
            return elem

    # Not in visible list — try the search bar at top of 通讯录 page.
    sf = wda.find(sid, 'type == "XCUIElementTypeSearchField"')
    if sf:
        wda.tap_rect_center(sid, sf)
        time.sleep(1.0)
        wda.type_keys(sid, name)
        time.sleep(1.5)

        matches = wda.find_all(
            sid, f'type == "XCUIElementTypeStaticText" AND (label == "{name}" OR name == "{name}")'
        )
        for elem in matches:
            r = wda.get_rect(sid, elem)
            if 130 <= r.get("y", 0) <= 700:
                return elem
    return None


def open_contact_profile(sid: str, name: str) -> None:
    """Search and tap into the contact's profile page."""
    ensure_contacts_tab(sid)
    row = find_contact_in_list(sid, name)
    if not row:
        raise WeChatError(3, f"contact '{name}' not found in 通讯录")
    r = wda.get_rect(sid, row)
    wda.tap_xy(sid, 200, r.get("y", 200) + r.get("height", 25) // 2)
    time.sleep(2.5)

    # Verify by header text or a distinctive profile element
    if not wda.find(sid, f'type == "XCUIElementTypeStaticText" AND label == "{name}"'):
        raise WeChatError(3, f"opened a profile but it's not '{name}'")


def tap_delete_friend(sid: str) -> None:
    """On a contact's profile: 更多 (top-right) → 删除联系人 in popover."""
    more = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "更多" OR label == "更多")')
    if not more:
        raise WeChatError(3, "no '更多' button on profile page")
    wda.tap_rect_center(sid, more)
    time.sleep(1.8)

    del_btn = wda.find(sid, '(name == "删除联系人" OR label == "删除联系人")')
    if not del_btn:
        raise WeChatError(3, "no '删除联系人' in profile menu — wrong page or contact?")
    wda.tap_rect_center(sid, del_btn)
    time.sleep(1.8)


def confirm_delete(sid: str) -> None:
    """On the confirmation alert, tap the destructive 删除 button.
    iOS alerts surface as XCUIElementTypeAlert / XCUIElementTypeButton.
    """
    # Try alert first
    btn = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "删除" OR label == "删除")')
    if not btn:
        raise WeChatError(3, "no 删除 confirm button — confirmation dialog not visible")
    wda.tap_rect_center(sid, btn)
    time.sleep(2.0)


def cmd_del_friend(args: argparse.Namespace) -> int:
    name = args.name
    if not name:
        print("wechat-cli del-friend: need NAME", file=sys.stderr)
        return 5

    if not wda.wda_ready():
        print("wechat-cli: WDA at :8100 not ready — run wda-up.sh", file=sys.stderr)
        return 1

    sid = wda.new_session(WECHAT_BUNDLE)
    if args.verbose:
        print(f"[del-friend] session={sid[:8]}", file=sys.stderr)
    try:
        time.sleep(3.0)
        check_repair_dialog(sid)
        open_contact_profile(sid, name)
        if args.verbose:
            print(f"[del-friend] on profile for '{name}'", file=sys.stderr)

        if not args.confirm:
            print(f"[del-friend] DRY RUN — would now tap 更多 → 删除联系人 → confirm. "
                  f"Re-run with --confirm to actually delete.", file=sys.stderr)
            return 0

        tap_delete_friend(sid)
        if args.verbose:
            print(f"[del-friend] tapping confirmation 删除", file=sys.stderr)
        confirm_delete(sid)
        if args.verbose:
            print(f"[del-friend] deleted '{name}'", file=sys.stderr)
        return 0
    except WeChatError as e:
        print(f"wechat-cli del-friend: {e}", file=sys.stderr)
        return e.code
    finally:
        # Back to chat list to leave WeChat in known state
        try:
            for _ in range(4):
                back = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "返回" OR label == "返回")')
                if not back:
                    break
                wda.tap_rect_center(sid, back)
                time.sleep(0.8)
            ensure_chat_list(sid)
        except Exception:
            pass
        wda.end_session(sid)


# ==========================================================================
# subcommand: moments-post
# ==========================================================================


def ensure_discover_tab(sid: str) -> None:
    """Tap 发现 bottom-nav tab."""
    tabs = wda.find_all(sid, 'type == "XCUIElementTypeButton" AND name == "发现"')
    for tab in tabs:
        r = wda.get_rect(sid, tab)
        if r.get("y", 0) > 700:
            wda.tap_rect_center(sid, tab)
            time.sleep(1.5)
            return
    wda.tap_xy(sid, 230, 800)
    time.sleep(1.5)


def open_moments(sid: str) -> None:
    """From 发现 tab, tap 朋友圈 row."""
    ensure_discover_tab(sid)
    row = wda.find(sid, '(name == "朋友圈" OR label == "朋友圈") AND type == "XCUIElementTypeStaticText"')
    if not row:
        raise WeChatError(3, "no 朋友圈 entry on 发现 page")
    r = wda.get_rect(sid, row)
    if not (100 < r.get("y", 0) < 400):
        raise WeChatError(3, f"朋友圈 element found but at unexpected y={r.get('y')}")
    wda.tap_xy(sid, 200, r.get("y", 200) + r.get("height", 25) // 2)
    time.sleep(3.0)

    # Verify by 拍照 button at top-right
    if not wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "拍照" OR label == "拍照")'):
        raise WeChatError(3, "朋友圈 didn't open (no 拍照 button visible)")


def open_moments_text_editor(sid: str) -> None:
    """Long-press the 拍照 button to bring up the text-only post editor.
    This is the standard WeChat path for 纯文字 朋友圈.
    """
    cam = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "拍照" OR label == "拍照")')
    if not cam:
        raise WeChatError(3, "no 拍照 button on moments page")
    r = wda.get_rect(sid, cam)
    cx = r.get("x", 350) + r.get("width", 30) // 2
    cy = r.get("y", 47) + r.get("height", 30) // 2
    wda.long_press_xy(sid, cx, cy, 1500)
    time.sleep(2.5)

    # Verify by 发表 button
    if not wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "发表" OR label == "发表")'):
        raise WeChatError(3, "long-press 拍照 did not open text-post editor (no 发表 button)")


def post_moments_text(sid: str, text: str) -> None:
    """In the text-only post editor, type text and tap 发表."""
    # Keyboard is already up; type directly.
    wda.type_keys(sid, text)
    time.sleep(1.5)

    pub = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "发表" OR label == "发表")')
    if not pub:
        raise WeChatError(4, "no 发表 button after typing (text too long? UI shift?)")
    wda.tap_rect_center(sid, pub)
    time.sleep(3.0)


def cmd_moments_post(args: argparse.Namespace) -> int:
    text = args.text
    if args.stdin:
        text = sys.stdin.read().rstrip("\n")
    if not text:
        print("wechat-cli moments-post: need TEXT (positional or --stdin)", file=sys.stderr)
        return 5
    if len(text) > 2000:
        print(f"wechat-cli moments-post: text too long ({len(text)} chars); WeChat caps at "
              f"~2000 for text-only posts", file=sys.stderr)
        return 5

    if not wda.wda_ready():
        print("wechat-cli: WDA at :8100 not ready", file=sys.stderr)
        return 1

    sid = wda.new_session(WECHAT_BUNDLE)
    if args.verbose:
        print(f"[moments-post] session={sid[:8]}", file=sys.stderr)
    try:
        time.sleep(3.0)
        check_repair_dialog(sid)
        open_moments(sid)
        if args.verbose:
            print(f"[moments-post] on moments page; long-press 拍照 → text editor", file=sys.stderr)
        open_moments_text_editor(sid)

        if not args.confirm:
            print(f"[moments-post] DRY RUN — would now type {len(text)} chars and tap 发表. "
                  f"Re-run with --confirm to actually post.", file=sys.stderr)
            # Tap 取消 to back out cleanly
            cancel = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "取消" OR label == "取消")')
            if cancel:
                wda.tap_rect_center(sid, cancel)
                time.sleep(1.0)
                # iOS may show "退出此次编辑？" → tap 取消 / 退出
                exit_btn = wda.find(sid, 'type == "XCUIElementTypeButton" AND (name == "退出" OR name == "不保留")')
                if exit_btn:
                    wda.tap_rect_center(sid, exit_btn)
                    time.sleep(1.0)
            return 0

        if args.verbose:
            print(f"[moments-post] typing {len(text)} chars + 发表", file=sys.stderr)
        post_moments_text(sid, text)
        if args.verbose:
            print(f"[moments-post] posted", file=sys.stderr)
        return 0
    except WeChatError as e:
        print(f"wechat-cli moments-post: {e}", file=sys.stderr)
        return e.code
    finally:
        try:
            ensure_chat_list(sid)
        except Exception:
            pass
        wda.end_session(sid)


# ==========================================================================
# subcommand: moments-del
# ==========================================================================
#
# UNTESTED ON LIVE POSTS — the test account had zero moments at build time
# so the long-press → 删除 → confirm path is implemented from standard
# WeChat UI conventions but not yet verified against a real post.
# Default is DRY RUN; pass --confirm to attempt actual deletion.
# Please report regressions: github.com/asdf17128/wechat-cli/issues


def open_my_moments(sid: str) -> None:
    """From 朋友圈 timeline, tap own avatar/cover (top of page) to enter
    我的朋友圈 page where only own posts are listed.
    """
    open_moments(sid)
    # WeChat shows a cover image at top with a small avatar in the corner.
    # Tapping the avatar opens 我的朋友圈. Avatar is typically in the
    # right portion of the cover area (around x=300, y=210).
    # Fallback: tap anywhere on the cover area at y=200.
    wda.tap_xy(sid, 300, 210)
    time.sleep(2.5)

    # Verify by 我的朋友圈 nav title or distinctive UI
    if not wda.find(sid, '(type == "XCUIElementTypeNavigationBar" AND name == "我的朋友圈") '
                         'OR (type == "XCUIElementTypeStaticText" AND label == "我的相册")'):
        # The exact element label varies — proceed but log a warning. Some
        # WeChat versions land on a profile-style view first.
        pass


def long_press_nth_post(sid: str, n: int) -> None:
    """Long-press the Nth post (1-indexed, newest first) on the current
    moments page to bring up the action menu.

    Heuristic: find all visible Cell-like containers in the post list,
    sort by y, pick the (n-1)th, long-press its center.
    """
    # Posts on 我的朋友圈 are usually Cells, but WeChat sometimes uses Other.
    # Try Cell first.
    cells = wda.find_all(sid, 'type == "XCUIElementTypeCell"')
    if not cells:
        cells = wda.find_all(sid, 'type == "XCUIElementTypeOther"')

    # Filter to cells whose y is in the timeline body and height is reasonable.
    timeline = []
    for c in cells:
        r = wda.get_rect(sid, c)
        if 200 <= r.get("y", 0) <= 800 and r.get("height", 0) > 80:
            timeline.append((r.get("y", 0), c, r))
    timeline.sort()

    if len(timeline) < n:
        raise WeChatError(3, f"only {len(timeline)} posts visible; cannot target N={n} "
                             f"(scrolling to load more not implemented yet)")

    _, elem, rect = timeline[n - 1]
    cx = rect.get("x", 0) + rect.get("width", 200) // 2
    cy = rect.get("y", 0) + rect.get("height", 100) // 2
    wda.long_press_xy(sid, cx, cy, 800)
    time.sleep(1.5)


def tap_delete_in_post_menu(sid: str) -> None:
    """After long-press on a post, WeChat shows a small menu with 评论 /
    点赞 / ... / 删除 . Tap 删除.
    """
    btn = wda.find(sid, '(name == "删除" OR label == "删除") AND type != "XCUIElementTypeAlert"')
    if not btn:
        raise WeChatError(3, "no 删除 in long-press post menu — wrong page or post is not own?")
    wda.tap_rect_center(sid, btn)
    time.sleep(1.5)


def cmd_moments_del(args: argparse.Namespace) -> int:
    n = args.n
    if n < 1:
        print(f"wechat-cli moments-del: N must be >= 1 (1 = newest)", file=sys.stderr)
        return 5

    if not wda.wda_ready():
        print("wechat-cli: WDA at :8100 not ready", file=sys.stderr)
        return 1

    sid = wda.new_session(WECHAT_BUNDLE)
    if args.verbose:
        print(f"[moments-del] session={sid[:8]}", file=sys.stderr)
    try:
        time.sleep(3.0)
        check_repair_dialog(sid)
        if args.verbose:
            print(f"[moments-del] navigating to 我的朋友圈", file=sys.stderr)
        open_my_moments(sid)
        if args.verbose:
            print(f"[moments-del] long-pressing post #{n}", file=sys.stderr)
        long_press_nth_post(sid, n)

        if not args.confirm:
            print(f"[moments-del] DRY RUN — would now tap 删除 + confirm. "
                  f"Re-run with --confirm to actually delete.", file=sys.stderr)
            return 0

        tap_delete_in_post_menu(sid)
        confirm_delete(sid)
        if args.verbose:
            print(f"[moments-del] deleted post #{n}", file=sys.stderr)
        return 0
    except WeChatError as e:
        print(f"wechat-cli moments-del: {e}", file=sys.stderr)
        return e.code
    finally:
        try:
            ensure_chat_list(sid)
        except Exception:
            pass
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

    # del-friend
    pd = sub.add_parser("del-friend", help="delete a contact (IRREVERSIBLE; default is dry-run)")
    pd.add_argument("name", help="contact display name (exact match)")
    pd.add_argument("--confirm", action="store_true",
                    help="actually delete; without this flag, navigates to confirmation step only")

    # moments-post
    pm = sub.add_parser("moments-post", help="post a text-only update to 朋友圈 (dry-run unless --confirm)")
    pm.add_argument("text", nargs="?", help="post body (or use --stdin)")
    pm.add_argument("--stdin", action="store_true", help="read text from stdin")
    pm.add_argument("--confirm", action="store_true", help="actually post; default is dry-run")

    # moments-del
    pmd = sub.add_parser("moments-del", help="delete the Nth-newest own moments post (UNTESTED; dry-run unless --confirm)")
    pmd.add_argument("n", type=int, default=1, nargs="?", help="post index, 1=newest (default 1)")
    pmd.add_argument("--confirm", action="store_true", help="actually delete; default is dry-run")

    args = p.parse_args()
    if args.cmd == "send":
        return cmd_send(args)
    if args.cmd == "add-friend":
        return cmd_add_friend(args)
    if args.cmd == "del-friend":
        return cmd_del_friend(args)
    if args.cmd == "moments-post":
        return cmd_moments_post(args)
    if args.cmd == "moments-del":
        return cmd_moments_del(args)
    print(f"wechat-cli: unknown command {args.cmd}", file=sys.stderr)
    return 5


if __name__ == "__main__":
    sys.exit(main())
