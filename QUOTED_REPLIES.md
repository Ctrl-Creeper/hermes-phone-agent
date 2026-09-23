# WeChat text quoted replies (experimental)

`wechat_reply` optionally locates an observed original, long-presses it, selects
**引用 / Quote**, verifies the composer preview, types the response, and checks
the preview again before sending. Existing plain replies are unchanged.

```json
{
  "action": "wechat_reply",
  "chat": "Example Chat",
  "text": "好的，我会准备材料。",
  "quote_text": "明天下午三点开会，请带上材料",
  "quote_sender": "Alice",
  "quote_context": "请看最新的会议安排",
  "quote_max_pages": 3
}
```

Use original text actually observed through `wechat_collect_context`, whose
`message_candidates` provides text anchors and optional sender metadata. Bounds
and element indices expire after scrolling; the workflow reacquires them.
`quote_sender` and `quote_context` are optional, observed disambiguation hints.
Do not fabricate them. Search is bounded to 1–8 pages (default 3), stopping at the
first matching page; uniqueness is checked on that page, not across all history.

An 80% text similarity retrieves candidates only. Fuzzy matches additionally
require an exact nearby context anchor; short originals require exact matching.
Changes to recognized numbers or negation are rejected. This heuristic is not
a guarantee of semantic equivalence. Multiple candidates are rejected. Sender
metadata is often absent in OCR: a sender hint can verify the preview but cannot
alone distinguish two otherwise identical bubbles without sender metadata.

The initial preview recognizer requires full `sender: original` text above the
composer, including tightly wrapped OCR lines. Truncated previews, unsupported
layouts, missing menus and changed previews fail closed: **no plain-reply
fallback**. Text only; image, file and voice-message quoting is not supported.

A quoted send is attempted once. Uncertain delivery is returned in `meta`, not
retried; an already visible identical response cannot confirm a new delivery.
The automatic-task cache includes the quote target but is not a durable or
cross-process exactly-once guarantee. Failures can leave a draft/quote in WeChat
(`draft_may_remain`); returning Home does not clear it. Inspect before retrying.

## Validation and downstreams

Offline tests cover the public reply workflow, tool dispatch, context anchors,
ambiguity, changed previews, scrolling and send uncertainty. **Real-device menu
and preview validation is still pending.** Do not treat the simulated layouts as
proof of compatibility with a particular WeChat version.

No helper APK/protocol change is needed. Shared Python APIs retain positional
compatibility through optional keyword arguments. MCP and Neko can sync the
shared implementation; their host wrappers must explicitly expose the new
options. Neko's preview/confirmation contract remains host-owned. This feature
does not require changes to Hermes core.

## 中文说明

给 `wechat_reply` 添加 `quote_text` 即可请求引用原话回复；可补充实际看到的
`quote_sender`、相邻消息 `quote_context`，最多查找 `quote_max_pages` 页。
先读取聊天内容，再使用返回的文本定位，不要沿用旧截图的元素编号。

80% 相似度只用于找候选，模糊匹配还需要相邻上下文。重复原话无法区分、数字或
否定词不同、引用预览不完整时会停止，不会退化为普通发送。发送前再次核对预览，
发送后不确定也不重发。失败可能保留草稿，回桌面不代表草稿已清除。

首版仅支持文字引用；真实微信菜单、预览样式尚待设备验证。MCP、Neko 的公共代码
可以同步，但各自工具入口仍需单独开放引用参数，无需更新 helper APK。
