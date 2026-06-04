#!/usr/bin/env python3
"""
wechat-send — send a WeChat message to a chat by name, via WebDriverAgent.

Usage:
    wechat-send TARGET MESSAGE
    wechat-send --to TARGET --msg MESSAGE
    wechat-send --to TARGET --stdin              # read message from stdin

Examples:
    wechat-send "1234" "[checkin] yuhang OK +2 · total 700 · 09:02"
    echo "alert from cron" | wechat-send --to "1234" --stdin
    wechat-send --to "文件传输助手" --msg "self note"

Prereqs:
    - WebDriverAgent stack up (Mac side: http://localhost:8100/status returns).
      Run ~/code1/mobile_mcp/wda-up.sh first if not.
    - iPhone unlocked, WeChat logged in.
    - Target name must match the chat row label EXACTLY (案名 / nickname /
      group name as it appears in chat list).

Exit codes:
    0  message sent (verified by chat-input clearing + send button greying)
    1  WDA unreachable (run wda-up.sh)
    2  WeChat not signed in / "异常修复" dialog blocking
    3  target chat not found in chat list (search returned no match)
    4  send failed (button untappable, draft persists, etc.)
    5  bad CLI args
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any

WDA_BASE = "http://localhost:8100"
WECHAT_BUNDLE = "com.tencent.xin"

# WeChat UI coords (verified on iPhone 12 Pro / iOS 26 / portrait). Some of
# these are stable across versions, others may drift — the script falls back
# to predicate-based lookup where possible.
SEARCH_BAR_Y = 91          # SearchField height covers full width at this y
INPUT_BAR_Y_DEFAULT = 770  # before keyboard rises
KEYBOARD_DELETE_X = 355    # iOS keyboard's delete glyph (right end of row 3)
KEYBOARD_DELETE_Y = 668
SEND_BUTTON_X = 340        # iOS keyboard send key when text present
SEND_BUTTON_Y = 740

# How long to long-press the delete key when clearing the draft. 5s clears
# ~50 characters which is plenty for any prior failed-send leftover.
CLEAR_DRAFT_MS = 5000


# ----- WDA HTTP helpers ---------------------------------------------------


def _request(method: str, path: str, body: dict[str, Any] | None = None,
             timeout: int = 15) -> dict[str, Any]:
    url = f"{WDA_BASE}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # WDA returns errors as JSON in the body
        try:
            return json.loads(e.read())
        except Exception:
            raise


def get(path: str, **kw) -> dict[str, Any]:
    return _request("GET", path, **kw)


def post(path: str, body: dict[str, Any] | None = None, **kw) -> dict[str, Any]:
    return _request("POST", path, body or {}, **kw)


def delete(path: str, **kw) -> dict[str, Any]:
    return _request("DELETE", path, **kw)


# ----- WDA session + element helpers --------------------------------------


def wda_ready() -> bool:
    try:
        r = get("/status", timeout=3)
        return r.get("value", {}).get("ready", False)
    except Exception:
        return False


def new_session(bundle_id: str) -> str:
    body = {"capabilities": {"alwaysMatch": {"bundleId": bundle_id}}}
    r = post("/session", body, timeout=30)
    sid = r.get("sessionId") or r.get("value", {}).get("sessionId")
    if not sid:
        raise RuntimeError(f"WDA refused session: {r}")
    return sid


def end_session(sid: str) -> None:
    try:
        delete(f"/session/{sid}")
    except Exception:
        pass


def find(sid: str, predicate: str) -> str | None:
    """Find first element matching iOS NSPredicate. Returns element id or None."""
    try:
        r = post(f"/session/{sid}/element",
                 {"using": "predicate string", "value": predicate}, timeout=5)
        v = r.get("value", {})
        # WDA returns either {"ELEMENT": "..."} or {"element-6066-...": "..."}
        for k, val in v.items():
            if k == "ELEMENT" or k.startswith("element-"):
                return val
    except urllib.error.HTTPError:
        pass
    return None


def find_all(sid: str, predicate: str) -> list[str]:
    try:
        r = post(f"/session/{sid}/elements",
                 {"using": "predicate string", "value": predicate}, timeout=5)
        out = []
        for v in r.get("value", []):
            for k, val in v.items():
                if k == "ELEMENT" or k.startswith("element-"):
                    out.append(val)
        return out
    except urllib.error.HTTPError:
        return []


def click_elem(sid: str, elem: str) -> None:
    post(f"/session/{sid}/element/{elem}/click", timeout=5)


def tap_xy(sid: str, x: int, y: int) -> None:
    """Tap absolute coordinates."""
    post(f"/session/{sid}/wda/tap", {"x": x, "y": y}, timeout=5)


def long_press_xy(sid: str, x: int, y: int, duration_ms: int) -> None:
    post(f"/session/{sid}/wda/touchAndHold",
         {"x": x, "y": y, "duration": duration_ms / 1000.0}, timeout=duration_ms / 1000 + 5)


def type_text(sid: str, elem: str, text: str) -> None:
    # WDA expects text as a string or list of chars; string works for ASCII.
    post(f"/session/{sid}/element/{elem}/value", {"value": [text]}, timeout=10)


def terminate(sid: str, bundle_id: str) -> None:
    try:
        post(f"/session/{sid}/wda/apps/terminate", {"bundleId": bundle_id}, timeout=5)
    except Exception:
        pass


def get_text(sid: str, elem: str) -> str:
    try:
        r = get(f"/session/{sid}/element/{elem}/text", timeout=5)
        return r.get("value") or ""
    except Exception:
        return ""


# ----- WeChat-specific flow -----------------------------------------------


class WeChatError(Exception):
    """Raised when WeChat is in an unusable state."""
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def on_chat_list(sid: str) -> bool:
    """True iff we're on the chat list (the 微信 tab) right now.

    Signal: the chat list view always has a SearchField at the top with name
    "搜索". The other bottom-nav tabs don't have it (or have a differently
    named one). This is more reliable than position-based checks.
    """
    return bool(find(sid, 'type == "XCUIElementTypeSearchField" AND name == "搜索"'))


def ensure_chat_list(sid: str) -> None:
    """Navigate to WeChat's chat list (微信 tab). Idempotent — if already
    there, no-op. Handles common pre-conditions: in-chat detail, on a
    different bottom-nav tab, or in a settings sub-page.
    """
    # Fast path: already on chat list.
    if on_chat_list(sid):
        return

    # Try a back button first — handles being inside a chat detail or a
    # nested settings page. Repeat a couple of times to climb out of nesting.
    for _ in range(3):
        back = find(sid, 'type == "XCUIElementTypeButton" AND (name == "返回" OR label == "返回")')
        if back:
            click_elem(sid, back)
            time.sleep(0.8)
            if on_chat_list(sid):
                return
        else:
            break

    # Try clicking the 微信 tab button. It's a Button with name="微信" in the
    # bottom tab bar (y > 750).
    for _ in range(3):
        tabs = find_all(sid, 'type == "XCUIElementTypeButton" AND name == "微信"')
        for tab in tabs:
            rect = get_elem_rect(sid, tab)
            if rect.get("y", 0) > 700:  # bottom-nav button
                tap_xy(sid, rect.get("x", 35) + rect.get("width", 28) // 2,
                            rect.get("y", 762) + rect.get("height", 28) // 2)
                time.sleep(1.0)
                if on_chat_list(sid):
                    return
                break
        else:
            # No 微信 tab button found — fall back to coordinate tap at the
            # leftmost bottom-nav position (iPhone 12 Pro layout).
            tap_xy(sid, 35, 800)
            time.sleep(1.0)
            if on_chat_list(sid):
                return
        time.sleep(0.5)


def find_chat_row(sid: str, target: str) -> str:
    """Find a chat list row whose label exactly matches target.

    Tries three strategies in order:
    1. Predicate on label/name (fastest, works most of the time)
    2. Get all StaticText and filter in Python (bypasses any predicate bug)
    3. Force-refresh accessibility tree by swiping the list, then retry (1)

    The 2nd strategy matters because WDA's predicate evaluator occasionally
    returns 0 matches when an alternative method walking the element tree
    does find the same element — a known accessibility-snapshot issue.

    Raises WeChatError(3) only after all 3 strategies exhaust.
    """
    # Strategy 1: predicate.
    elem = _find_chat_row_predicate(sid, target)
    if elem:
        return elem

    # Strategy 2: enumerate StaticTexts, filter by attribute, scope by y.
    elem = _find_chat_row_walk(sid, target)
    if elem:
        return elem

    # Strategy 3: nudge the list (1 px scroll) to force WDA re-snapshot, retry.
    try:
        post(f"/session/{sid}/wda/dragfromtoforduration",
             {"fromX": 200, "fromY": 400, "toX": 200, "toY": 401, "duration": 0.05},
             timeout=5)
        time.sleep(0.8)
    except Exception:
        pass
    elem = _find_chat_row_predicate(sid, target) or _find_chat_row_walk(sid, target)
    if elem:
        return elem

    # Diagnostic dump for debugging (only printed if no strategy worked).
    cnt = len(find_all(sid, 'type == "XCUIElementTypeStaticText"'))
    raise WeChatError(3, f"chat '{target}' not found in chat list "
                         f"(0 candidates after 3 lookup strategies; "
                         f"{cnt} total StaticText visible — accessibility tree may be stale)")


def _find_chat_row_predicate(sid: str, target: str) -> str | None:
    """Strategy 1: NSPredicate lookup."""
    candidates = find_all(
        sid,
        f'type == "XCUIElementTypeStaticText" AND (label == "{target}" OR name == "{target}")'
    )
    return _filter_chat_list_y(sid, candidates)


def _find_chat_row_walk(sid: str, target: str) -> str | None:
    """Strategy 2: enumerate all StaticTexts, filter by label/name in Python."""
    all_st = find_all(sid, 'type == "XCUIElementTypeStaticText"')
    matches = []
    for elem in all_st:
        try:
            r = get(f"/session/{sid}/element/{elem}/attribute/label")
            label = r.get("value") or ""
        except Exception:
            label = ""
        if label == target:
            matches.append(elem)
    return _filter_chat_list_y(sid, matches)


def _filter_chat_list_y(sid: str, candidates: list[str]) -> str | None:
    """From a list of candidate elements, return the first whose y is in the
    chat-list area (between top bar and bottom nav)."""
    for elem in candidates:
        try:
            rect = get_elem_rect(sid, elem)
            y = rect.get("y", 0)
            if 130 <= y <= 700:
                return elem
        except Exception:
            continue
    return None


def get_elem_rect(sid: str, elem: str) -> dict[str, int]:
    r = get(f"/session/{sid}/element/{elem}/rect", timeout=5)
    return r.get("value", {})


def back_to_chat_list(sid: str) -> None:
    """If we're inside a chat, tap the back arrow at top-left to return to
    chat list. Then ensure we're on the 微信 tab.
    """
    # WeChat's 返回 button responds to coordinate taps but NOT to
    # element/click (same quirk as chat-row StaticText and the Send button).
    # So resolve the element to its rect and tap by xy.
    for _ in range(2):
        back = find(sid, 'type == "XCUIElementTypeButton" AND (name == "返回" OR label == "返回")')
        if not back:
            break
        rect = get_elem_rect(sid, back)
        tap_xy(sid, rect.get("x", 16) + rect.get("width", 12) // 2,
                    rect.get("y", 47) + rect.get("height", 44) // 2)
        time.sleep(0.8)
    ensure_chat_list(sid)


def open_chat(sid: str, target: str) -> None:
    """Tap into the chat with the given name. Verifies via header text.

    NOTE: tapping the StaticText element via element/click does NOT activate the
    chat row in WeChat — the click is consumed by the StaticText itself (label
    selection or no-op). We must tap the chat-row Cell, which we approximate by
    tapping at the screen-mid x (200) and the label's y. The Cell has wider
    hit-target than the label.
    """
    row = find_chat_row(sid, target)
    rect = get_elem_rect(sid, row)
    label_y = rect.get("y", 200) + rect.get("height", 25) // 2

    # Coordinate tap at x=200 (mid screen-width, well inside row body).
    tap_xy(sid, 200, label_y)
    time.sleep(2.5)

    # Verify the chat opened is actually the right one. WeChat surfaces the
    # chat header as an XCUIElementTypeNavigationBar named EITHER `<target>`
    # (1:1 chats) OR `<target>,(N)` for groups (note: comma is part of the
    # rendered name, not a typo). The NavigationBar is at y≈47.
    for _ in range(6):
        hdr = find(sid, f'type == "XCUIElementTypeNavigationBar" '
                        f'AND (name == "{target}" OR name BEGINSWITH "{target},")')
        if hdr:
            return
        time.sleep(0.5)

    # Failed to land on target — back out so caller can retry from a clean state.
    back_to_chat_list(sid)
    raise WeChatError(3, f"opened a chat but header is not '{target}' — wrong chat opened "
                         f"(row was at y={rect.get('y')})")


def find_input_field(sid: str) -> str | None:
    """Locate WeChat's message input. It's the only XCUIElementTypeTextView
    in the chat detail page (Other text inputs are search boxes elsewhere).
    """
    elem = find(sid, 'type == "XCUIElementTypeTextView"')
    return elem


def focus_input(sid: str) -> None:
    """Tap WeChat's message input bar to raise the keyboard. Tries to click
    the located TextView first (most reliable), falls back to coordinate tap.
    """
    elem = find_input_field(sid)
    if elem:
        try:
            click_elem(sid, elem)
            time.sleep(1.5)
            return
        except Exception:
            pass
    tap_xy(sid, 130, INPUT_BAR_Y_DEFAULT)
    time.sleep(1.5)


def clear_draft(sid: str) -> None:
    """Wipe any leftover draft text in the chat input.

    The naive "long-press delete key" approach only removes ~10 chars/s, so a
    5-second long-press only handles ~50 char drafts. WeChat drafts can be
    hundreds of chars (e.g. a forwarded log message). Use WDA's element /clear
    endpoint instead — atomic, regardless of length.

    Must be called after the input is focused (keyboard up).
    """
    elem = find_input_field(sid)
    if elem:
        try:
            post(f"/session/{sid}/element/{elem}/clear", timeout=10)
            time.sleep(0.3)
            return
        except Exception:
            pass

    # Fallback: long-press iOS keyboard's delete key. Time it proportional to
    # whatever text exists in the input (read length first).
    n = 0
    if elem:
        n = len(get_text(sid, elem))
    duration_ms = max(2000, min(30000, (n + 20) * 150))  # ~7 chars/s, 30s cap
    long_press_xy(sid, KEYBOARD_DELETE_X, KEYBOARD_DELETE_Y, duration_ms)
    time.sleep(0.5)


def type_message(sid: str, message: str) -> None:
    """Send message text into the focused input. Uses TypingType endpoint
    to avoid having to find the input element (which is fragile in WeChat).
    """
    # WDA exposes /wda/keys for typing into currently-focused field. Each char
    # is sent through the same path the iOS keyboard would, so this triggers
    # the IME — keep messages ASCII to avoid prediction surprises.
    post(f"/session/{sid}/wda/keys", {"value": [message]}, timeout=15)
    time.sleep(0.5)


def tap_send(sid: str) -> None:
    """Tap the keyboard's Send button. Only enabled once text is in input.

    NOTE: WDA element/click on this button is unreliable (similar to chat-row
    StaticText click). We always resolve to the button rect's center and tap
    by coordinate, which actually fires the send action.
    """
    btn = find(sid, 'type == "XCUIElementTypeButton" AND (name == "Send" OR name == "send" OR label == "发送")')
    if btn:
        rect = get_elem_rect(sid, btn)
        x = rect.get("x", SEND_BUTTON_X) + rect.get("width", 60) // 2
        y = rect.get("y", SEND_BUTTON_Y) + rect.get("height", 30) // 2
        tap_xy(sid, x, y)
    else:
        tap_xy(sid, SEND_BUTTON_X, SEND_BUTTON_Y)
    time.sleep(2.0)


def verify_sent(sid: str, sent_message: str) -> bool:
    """Confirm the send actually happened.

    Heuristic: after a successful send, WeChat clears the input field. If the
    input still contains our message text (or any prefix of it), send didn't
    fire. We tolerate empty (sent) but reject any text that overlaps the
    message we sent.
    """
    elem = find_input_field(sid)
    if not elem:
        # Can't find input — assume we're no longer in chat detail (something
        # weird), don't claim success.
        return False
    cur = get_text(sid, elem)
    return cur.strip() == ""


def check_repair_dialog(sid: str) -> None:
    """Detect WeChat's 异常修复 self-protection dialog. If present, auto-dismiss
    by tapping `下一步` (which walks the user through a no-op repair flow and
    returns to the chat list). Only bail out if dismiss fails or we hit a
    different blocker (login wall).
    """
    if find(sid, 'label CONTAINS "重新登录" OR label CONTAINS "请重新登录"'):
        raise WeChatError(2, "WeChat is not signed in")

    if find(sid, 'label CONTAINS "微信连续异常"'):
        # Auto-recover: tap 下一步, then wait for chat list to come back.
        # The flow typically takes 3-5 seconds and lands on the chat list.
        for step_attempt in range(8):
            btn = find(sid, '(name == "下一步" OR label == "下一步")')
            if not btn:
                break  # past the modal, into the chat list (or somewhere else)
            rect = get_elem_rect(sid, btn)
            tap_xy(sid, rect.get("x", 195) + rect.get("width", 184) // 2,
                        rect.get("y", 670) + rect.get("height", 49) // 2)
            time.sleep(2.0)
        else:
            raise WeChatError(2, "WeChat 异常修复 dialog detected but auto-dismiss "
                                 "couldn't get past it after 8 attempts")
        # After dismiss, give WeChat a beat to load chat list before proceeding.
        time.sleep(2.5)


# ----- Top-level driver ---------------------------------------------------


def _open_chat_in_session(sid: str, target: str, *, verbose: bool) -> None:
    """Try once, within the given session, to navigate to chat list + open
    chat. Raises WeChatError on failure (predicate / chat not found etc).
    """
    time.sleep(2.0)  # let app activation settle
    check_repair_dialog(sid)
    ensure_chat_list(sid)
    open_chat(sid, target)


def send_message(target: str, message: str, *, terminate_after: bool = False,
                 verbose: bool = False) -> None:
    if not wda_ready():
        raise WeChatError(1, "WDA at http://localhost:8100 is not ready — run ~/code1/mobile_mcp/wda-up.sh first")

    # WeChat enters a recurring ~120 s window where the chat-list cells drop
    # out of the accessibility tree (only ~4 StaticTexts left in the tree, all
    # nav chrome). Within a session, that state is sticky — long-waits inside
    # the same session don't clear it. The reliable workaround is to END the
    # session and create a FRESH one (the new session activation re-syncs the
    # accessibility tree). So our outer loop is "try in a session; on chat-not-
    # found, kill the session and retry with a new one", up to a deadline.
    deadline = time.time() + 300  # 5 min absolute cap
    attempt = 0
    last_err: WeChatError | None = None

    while time.time() < deadline:
        attempt += 1
        sid = new_session(WECHAT_BUNDLE)
        if verbose:
            print(f"[wechat-send] session={sid} (attempt {attempt})", file=sys.stderr)

        try:
            _open_chat_in_session(sid, target, verbose=verbose)
        except WeChatError as e:
            last_err = e
            if e.code != 3:
                # auth / repair / unexpected — re-raise, no point retrying
                end_session(sid)
                raise
            if verbose:
                print(f"[wechat-send] chat-find failed in session {sid[:8]}: {e}; "
                      f"ending session and retrying", file=sys.stderr)
            end_session(sid)
            time.sleep(5.0)  # brief pause before recreating session
            continue

        # open_chat succeeded — finish the send in this same session.
        try:
            if verbose:
                print(f"[wechat-send] focusing input", file=sys.stderr)
            focus_input(sid)
            clear_draft(sid)

            if verbose:
                print(f"[wechat-send] typing {len(message)} chars", file=sys.stderr)
            type_message(sid, message)
            tap_send(sid)

            if not verify_sent(sid, message):
                raise WeChatError(4, "tap-send fired but input field still has text — "
                                     "send didn't go through (button missed? send disabled?)")
            if verbose:
                print(f"[wechat-send] verified sent (input is empty)", file=sys.stderr)

            if not terminate_after:
                try:
                    back_to_chat_list(sid)
                except Exception:
                    pass
            return  # success
        finally:
            if terminate_after:
                terminate(sid, WECHAT_BUNDLE)
            end_session(sid)

    raise last_err or WeChatError(3, f"chat '{target}' never appeared "
                                      f"within 5-minute budget across {attempt} sessions")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="wechat-send",
        description="Send a WeChat message to a chat by name, via WebDriverAgent.",
    )
    p.add_argument("target", nargs="?", help="Chat name (exact match)")
    p.add_argument("message", nargs="?", help="Message text (ASCII recommended)")
    p.add_argument("--to", help="Chat name (alternative to positional)")
    p.add_argument("--msg", help="Message text (alternative to positional)")
    p.add_argument("--stdin", action="store_true",
                   help="Read message from stdin instead of --msg/positional")
    p.add_argument("--terminate", action="store_true",
                   help="Terminate WeChat after sending (default: leave on chat list, "
                        "since kill/relaunch cycles trigger WeChat's 异常修复 mode)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="Print progress to stderr")
    args = p.parse_args()

    target = args.to or args.target
    if args.stdin:
        message = sys.stdin.read().rstrip("\n")
    else:
        message = args.msg or args.message

    if not target or not message:
        p.error("need both TARGET (--to) and MESSAGE (--msg or --stdin)")

    try:
        send_message(target, message,
                     terminate_after=args.terminate,
                     verbose=args.verbose)
    except WeChatError as e:
        print(f"wechat-send: {e}", file=sys.stderr)
        return e.code
    except Exception as e:
        print(f"wechat-send: unexpected error: {e}", file=sys.stderr)
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
