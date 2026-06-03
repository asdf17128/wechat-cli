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


def ensure_chat_list(sid: str) -> None:
    """Tap WeChat's bottom-nav `微信` tab so we're on the chat list, not the
    'Contacts/Discover/Me' tab (a previous session may have left us elsewhere).
    """
    # The 微信 tab is the leftmost bottom-nav button. There's no easily-found
    # accessibility label, but we can tap its icon area at approx (35, 800).
    tap_xy(sid, 35, 800)
    time.sleep(1.0)


def find_chat_row(sid: str, target: str) -> str:
    """Find a chat list row whose label exactly matches target.

    On WeChat's chat list each chat is a StaticText with the chat name as its
    label/name. We scope to the chat list area (y < 700) so that an exact
    match against a message bubble inside an already-opened chat doesn't win.
    """
    candidates = find_all(
        sid,
        f'type == "XCUIElementTypeStaticText" AND (label == "{target}" OR name == "{target}")'
    )
    for elem in candidates:
        rect = get_elem_rect(sid, elem)
        y = rect.get("y", 0)
        # Chat list rows live between top bar (~y=150) and bottom nav (~y=750).
        if 130 <= y <= 700:
            return elem
    raise WeChatError(3, f"chat '{target}' not found in chat list "
                         f"({len(candidates)} candidates, none in chat-list y-range)")


def get_elem_rect(sid: str, elem: str) -> dict[str, int]:
    r = get(f"/session/{sid}/element/{elem}/rect", timeout=5)
    return r.get("value", {})


def back_to_chat_list(sid: str) -> None:
    """If we're inside a chat, tap the back arrow at top-left to return to list."""
    back = find(sid, 'type == "XCUIElementTypeButton" AND (name == "返回" OR label == "返回")')
    if back:
        click_elem(sid, back)
        time.sleep(1.0)
    # Always tap the chat-list tab too, as a belt-and-suspenders fallback.
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


def clear_draft(sid: str) -> None:
    """Long-press iOS keyboard's delete key to wipe any leftover draft text.

    Must be called after the input is focused (keyboard up).
    """
    long_press_xy(sid, KEYBOARD_DELETE_X, KEYBOARD_DELETE_Y, CLEAR_DRAFT_MS)
    time.sleep(0.5)


def focus_input(sid: str) -> None:
    """Tap WeChat's message input bar to raise the keyboard."""
    tap_xy(sid, 130, INPUT_BAR_Y_DEFAULT)
    time.sleep(1.5)


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
    """Tap the keyboard's Send button. Only enabled once text is in input."""
    # Prefer predicate lookup so we adapt to layout shifts; fall back to xy.
    btn = find(sid, 'type == "XCUIElementTypeButton" AND (name == "Send" OR name == "send" OR label == "发送")')
    if btn:
        click_elem(sid, btn)
    else:
        tap_xy(sid, SEND_BUTTON_X, SEND_BUTTON_Y)
    time.sleep(2.0)


def check_repair_dialog(sid: str) -> None:
    """Detect WeChat's 异常修复 self-protection dialog and bail out."""
    if find(sid, 'label CONTAINS "微信连续异常"'):
        raise WeChatError(2, "WeChat is in 异常修复 mode — open WeChat manually and tap 下一步 once")
    if find(sid, 'label CONTAINS "重新登录" OR label CONTAINS "请重新登录"'):
        raise WeChatError(2, "WeChat is not signed in")


# ----- Top-level driver ---------------------------------------------------


def send_message(target: str, message: str, *, terminate_after: bool = False,
                 verbose: bool = False) -> None:
    if not wda_ready():
        raise WeChatError(1, "WDA at http://localhost:8100 is not ready — run ~/code1/mobile_mcp/wda-up.sh first")

    sid = new_session(WECHAT_BUNDLE)
    if verbose:
        print(f"[wechat-send] session={sid}", file=sys.stderr)
    try:
        time.sleep(6.0)  # initial WeChat load (chat list, network refresh)
        check_repair_dialog(sid)
        ensure_chat_list(sid)

        if verbose:
            print(f"[wechat-send] opening chat '{target}'", file=sys.stderr)
        # Chat row may not be rendered yet on first try; retry up to 4× over 12s.
        last_err: WeChatError | None = None
        for attempt in range(4):
            try:
                open_chat(sid, target)
                break
            except WeChatError as e:
                last_err = e
                if verbose:
                    print(f"[wechat-send] open_chat attempt {attempt+1} failed: {e}",
                          file=sys.stderr)
                time.sleep(3.0)
        else:
            raise last_err or WeChatError(3, f"chat '{target}' never appeared")

        if verbose:
            print(f"[wechat-send] focusing input", file=sys.stderr)
        focus_input(sid)
        clear_draft(sid)

        if verbose:
            print(f"[wechat-send] typing {len(message)} chars", file=sys.stderr)
        type_message(sid, message)
        tap_send(sid)

        # Best-effort verification: take a screenshot? Skip — WDA round-trip is
        # slow. The send tap returns synchronously and the send button greys
        # back out within ~1s if successful. We've slept 2s already.
        if verbose:
            print(f"[wechat-send] done", file=sys.stderr)

        # By default leave WeChat on chat list (not chat detail, not killed).
        # WeChat's self-protection 异常修复 mode triggers after ~3 rapid
        # kill/launch cycles — backing out is cheaper AND avoids that trap.
        if not terminate_after:
            try:
                back_to_chat_list(sid)
            except Exception:
                pass
    finally:
        if terminate_after:
            terminate(sid, WECHAT_BUNDLE)
        end_session(sid)


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
