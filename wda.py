"""WDA HTTP helpers — shared across wechat-cli's commands.

Pure stdlib (urllib). Talks to WebDriverAgent at http://localhost:8100.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

WDA_BASE = "http://localhost:8100"


# ----- HTTP wrappers ------------------------------------------------------


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


# ----- session helpers ----------------------------------------------------


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


def terminate_app(sid: str, bundle_id: str) -> None:
    try:
        post(f"/session/{sid}/wda/apps/terminate", {"bundleId": bundle_id}, timeout=5)
    except Exception:
        pass


# ----- element lookups ----------------------------------------------------


def _extract_id(elem: dict[str, str]) -> str:
    """WDA element dicts use either 'ELEMENT' or 'element-6066-...' as key."""
    for k, v in elem.items():
        if k == "ELEMENT" or k.startswith("element-"):
            return v
    raise KeyError(f"no element id in {elem}")


def find(sid: str, predicate: str) -> str | None:
    """Find first element matching iOS NSPredicate. Returns element id or None."""
    try:
        r = post(f"/session/{sid}/element",
                 {"using": "predicate string", "value": predicate}, timeout=5)
        v = r.get("value", {})
        if not isinstance(v, dict):
            return None
        try:
            return _extract_id(v)
        except KeyError:
            return None
    except urllib.error.HTTPError:
        return None


def find_all(sid: str, predicate: str) -> list[str]:
    try:
        r = post(f"/session/{sid}/elements",
                 {"using": "predicate string", "value": predicate}, timeout=10)
        out = []
        for v in r.get("value", []):
            try:
                out.append(_extract_id(v))
            except KeyError:
                continue
        return out
    except urllib.error.HTTPError:
        return []


def get_rect(sid: str, elem: str) -> dict[str, int]:
    try:
        r = get(f"/session/{sid}/element/{elem}/rect", timeout=5)
        return r.get("value", {}) or {}
    except Exception:
        return {}


def get_attr(sid: str, elem: str, attr: str) -> str:
    """attr ∈ {'name', 'label', 'value', 'text', ...}"""
    try:
        if attr == "text":
            r = get(f"/session/{sid}/element/{elem}/text", timeout=5)
        else:
            r = get(f"/session/{sid}/element/{elem}/attribute/{attr}", timeout=5)
        return r.get("value") or ""
    except Exception:
        return ""


def click_elem(sid: str, elem: str) -> None:
    """WDA element click. NOTE: in WeChat, this often silently no-ops (the
    click is consumed by the element instead of propagating to its parent
    row/button). Prefer tap_xy with the element's rect center.
    """
    post(f"/session/{sid}/element/{elem}/click", timeout=5)


def tap_xy(sid: str, x: int, y: int) -> None:
    post(f"/session/{sid}/wda/tap", {"x": x, "y": y}, timeout=5)


def tap_rect_center(sid: str, elem: str) -> None:
    """Find an element's rect and tap its center via coords. More reliable
    than click_elem in WeChat."""
    r = get_rect(sid, elem)
    tap_xy(sid, r.get("x", 0) + r.get("width", 0) // 2,
                r.get("y", 0) + r.get("height", 0) // 2)


def long_press_xy(sid: str, x: int, y: int, duration_ms: int) -> None:
    post(f"/session/{sid}/wda/touchAndHold",
         {"x": x, "y": y, "duration": duration_ms / 1000.0},
         timeout=duration_ms / 1000 + 5)


def type_keys(sid: str, text: str) -> None:
    """Inject text into currently-focused input via iOS keyboard.
    NOTE: Chinese characters get garbled by iOS IME prediction — keep ASCII.
    """
    post(f"/session/{sid}/wda/keys", {"value": [text]}, timeout=15)


def clear_input(sid: str, elem: str) -> bool:
    """Atomically clear a TextField/TextView via WDA's /clear endpoint."""
    try:
        post(f"/session/{sid}/element/{elem}/clear", timeout=10)
        return True
    except Exception:
        return False


def screenshot(sid: str) -> bytes:
    """Returns raw PNG bytes."""
    import base64
    r = get(f"/session/{sid}/screenshot", timeout=10)
    return base64.b64decode(r.get("value", ""))
