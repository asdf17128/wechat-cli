# wechat-cli

Python CLI for automating common WeChat operations on an iPhone via
**WebDriverAgent**. Pure stdlib (urllib) — no Claude/LLM at runtime, no pip
deps.

## Commands

```
wechat-cli send TARGET MESSAGE             # send a chat message
wechat-cli add-friend WX_ID [--msg M]      # search wechat-id/phone, send request
wechat-cli del-friend NAME [--confirm]     # remove contact (dry-run default)
wechat-cli moments-post TEXT [--confirm]   # post text-only 朋友圈 (dry-run default)
wechat-cli moments-del N [--confirm]       # delete Nth-newest own post (UNTESTED)
```

There is also a legacy single-purpose `wechat-send` script kept for
backwards-compat with existing launchd jobs:

```bash
$ wechat-send "1234" "[checkin] yuhang OK +2 · total 700 · 09:02"
```

→ opens WeChat, navigates to the chat named `1234`, types, sends. ~10-15 s.

The chat name is matched **exactly** against the StaticText label in WeChat's
chat list (case-sensitive, no fuzzy match). Works for groups, contacts, and
the "文件传输助手" self-chat.

## Why this exists

I was using `claude -p "/wechat-send ..."` for WeChat reports from launchd
routines. That worked but was slow (30-60 s per send) and burned tokens.
This CLI is the deterministic recipe — same WDA stack, no LLM in the loop.

## Prereqs

- macOS Mac with iPhone connected via USB
- WebDriverAgent running and reachable at `http://localhost:8100`. Use
  `~/code1/mobile_mcp/wda-up.sh` from the parallel mobile_mcp repo.
- iPhone unlocked (no passcode helps for headless use)
- WeChat (`com.tencent.xin`) installed and signed in

Python 3.9+ stdlib only (`urllib.request`) — no pip install required.

## Install

```bash
git clone https://github.com/<you>/wechat-cli ~/code1/wechat-cli
ln -s ~/code1/wechat-cli/wechat_send.py /usr/local/bin/wechat-send
```

Or just call it directly without symlinking:

```bash
~/code1/wechat-cli/wechat_send.py --to "1234" --msg "hello"
```

## CLI

```
wechat-send TARGET MESSAGE                  # positional
wechat-send --to TARGET --msg MESSAGE       # flag form
wechat-send --to TARGET --stdin             # message from stdin
                                            # plus: -v / --verbose
                                            # plus: --terminate (don't, by default)
```

By default the tool **does not kill WeChat after sending** — it just backs
out of the chat to the home list. WeChat has a `连续异常修复` self-protection
mode that triggers after about 3 rapid kill+launch cycles; staying on the
chat list avoids that trap entirely.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | message sent (best-effort verified) |
| 1 | WDA at :8100 not reachable — run `wda-up.sh` |
| 2 | WeChat not signed in OR `连续异常修复` dialog blocking — open WeChat manually and tap 下一步 once |
| 3 | target chat name not found in chat list |
| 4 | send failed (button untappable, draft persists, network) |
| 5 | bad CLI args |

## Reliability

Stress-tested at **25/25 (100%)** sends over ~15 minutes at 8-second intervals.

The CLI handles three quirks internally so callers don't need to retry:

1. **WeChat's `连续异常修复` self-protection** — after rapid kill+launch cycles
   WeChat shows a repair dialog. CLI auto-taps `下一步` to dismiss.
2. **iOS keyboard delete-key is too slow for long drafts** — atomic
   `/element/{id}/clear` is used instead, which empties the input regardless
   of length.
3. **~120 s window where WeChat's chat-list cells drop out of the
   accessibility tree** — the entire chat list goes invisible to WDA
   predicate queries (~4 StaticTexts left in the tree, all nav chrome)
   despite the screen looking normal. Within a single WDA session this
   state is STICKY. The CLI's retry loop creates a FRESH WDA session on
   chat-not-found (which re-syncs the accessibility tree), up to a 5-minute
   absolute deadline. In practice a single recreate is enough.

For routines that need maximum belt-and-suspenders coverage, you can still
wrap in an outer retry, but it shouldn't be necessary anymore:

```bash
~/code1/wechat-cli/wechat_send.py --to "$T" --msg "$M" || true
```

## Limits + known issues

- **ASCII only is safest.** WDA's `keys` endpoint goes through iOS IME, which
  inserts predictions for Chinese characters. The script does not type Chinese
  reliably — for Chinese content, paste from clipboard or pre-stage the text
  outside this tool.
- **The default flow keeps WeChat alive between sends.** If you do pass
  `--terminate`, don't run the CLI in tight loops — WeChat's self-protection
  triggers after ~3 rapid kill+launch cycles, shows `连续异常修复`, and blocks
  further runs. The CLI detects this and exits with code 2.
- **Chat name matching is exact.** Sub-string / fuzzy match isn't supported;
  groups renamed since you tested will not be found.
- **No screenshot verification of "message delivered".** The CLI verifies the
  send button was tapped and the input cleared, not that the recipient
  received the message. WeChat handles delivery; if the device is offline,
  the message stays in WeChat's outbox and will send when the device reconnects.

## Skill wrapper

A Claude Code / Codex skill that wraps this CLI is at
`~/.claude/skills/wechat-send/SKILL.md`. Agents can call:

```
Bash: ~/code1/wechat-cli/wechat_send.py --to "1234" --msg "..."
```

instead of having to drive the iPhone themselves via mobile-mcp. The skill
just teaches the agent which exit codes mean what, and reminds it to keep
messages ASCII.

## Rate-limit warning (important)

WeChat **aggressively tracks** friend-add / friend-delete / moments-post
frequency for anti-abuse. Stress-testing this CLI by running these commands
in a loop will trigger:

1. WeChat's `连续异常修复` self-protection (dialog blocks the app; CLI
   auto-dismisses it for `下一步` and `暂不重启` variants)
2. **Account-level rate limits** — too many friend ops in a day can lock
   the friend-add feature for hours-to-days. We don't have a programmatic
   way to detect this; it just silently fails or shows a Chinese warning.

**Recommended limits per account per day:**
- `add-friend`: ≤ 5/day
- `del-friend`: ≤ 5/day
- `moments-post`: ≤ 10/day
- `send`: no observed limit (we've done 100+/day fine)

## Per-command notes

- `send` — production-ready, stress-tested 35/35 (100%) including session-
  recreate auto-recovery for WeChat's ~120s accessibility-tree-stale window.
- `add-friend` — tested with a fake wechat-id (correctly reports "user not
  found" with exit 3). Live "actually added a friend" path is implemented
  but only the dry-run side has been exercised.
- `del-friend` — `--confirm` defaults off. Tested dry-run on a real contact
  (reaches the profile page). The 更多 → 删除联系人 → confirm alert path is
  implemented from observed UI but the final `--confirm` step hasn't been
  exercised end-to-end (intentionally, to avoid losing the test contact).
- `moments-post` — tested dry-run (opens text editor, cancels out). Live
  `--confirm` posting works in theory but was not exercised against the test
  account (which doesn't actively use Moments).
- `moments-del` — **UNTESTED**. The test account had zero moments at build
  time, so the long-press → 删除 → confirm path is implemented from standard
  WeChat patterns but not verified. Please test against a disposable post.

## License

MIT — do whatever you want with it.
