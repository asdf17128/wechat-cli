# wechat-cli

A 200-line Python CLI that sends WeChat messages on an iPhone by driving
**WebDriverAgent** (no Claude / no MCP at runtime). Used as the WeChat-output
layer for daily-routine agents and scripts.

## What it does

```bash
$ wechat-send "1234" "[checkin] yuhang OK +2 · total 700 · 09:02"
```

→ opens WeChat on the iPhone, navigates to the chat named `1234`, types the
message, taps send, terminates WeChat. Total round-trip ~10-15 s.

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

## License

MIT — do whatever you want with it.
