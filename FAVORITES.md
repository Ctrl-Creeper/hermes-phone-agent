# WeChat text favorites (experimental)

```json
{"action":"wechat_favorite","chat":"Example","message_text":"Keep this note"}
```

After approval of the exact conversation and original, the workflow opens the
chat, refreshes the screen and requires one exact visible text match in the
message area. It long-presses once, verifies that the chat and original remain
visible, and chooses a unique 收藏/Favorite menu action. Only a fresh success
notice confirms completion. Missing evidence or an exception after the tap is
`favorite_status=uncertain`, not a reason to retry. Home is attempted on exit.

This first version handles text messages, not image/link cards, folders, tags or
multi-select. It does not search older pages; use observed unique text. OCR-wrapped
or truncated originals may be unsupported. Real-device menu/confirmation testing
is pending. No Android helper change is required.

The operation requires separate approval; existing automatic event allowlists
are not broadened. MCP/Neko can sync the shared implementation but need their own
host entry and confirmation behavior to expose it.

中文：首版收藏指定聊天内唯一、完整可见的文字消息。确认绑定原话，出现新的“已收藏”
提示才确认成功；不确定则不自动重试。不支持图片/链接卡片、批量收藏或标签管理。
