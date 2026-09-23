# WeChat history search (experimental)

```json
{"action":"wechat_search_history","chat":"Example","query":"meeting","max_pages":3}
```

Opens and verifies one conversation, chooses Chat Info → Search Chat History,
locates a unique native text field, submits a keyword and collects visible matching
snippets. `max_pages` is bounded to 1–8. A repeated page, no-result notice or page
limit stops scanning. Query changes or unexpected apps discard the results.
The workflow attempts Home on every exit; it does not send messages.

Results are untrusted visible snippets with page numbers, not guaranteed complete
messages or stable IDs for quoting. Coverage is always partial; this does not
provide a database search, sender filter or date-range filter. Use existing
`wechat_collect_context(scope=...)` for bounded time/context reading.

The workflow requests native hierarchy first via `image_hierarchy`. WeChat builds
that hide their search text field or lack labelled Chat Info controls are not yet
supported; no coordinate fallback is guessed. Actual device validation is pending.

Navigation uses the existing approval mechanism. Phone-event policies must
explicitly allow `wechat_search_history`; this change does not broaden their
allowlists. Shared Python callers can call `wechat.search_history`; MCP/Neko host
wrappers must expose the new action separately. No helper APK change is needed.

中文：新增指定聊天内的关键词历史搜索，返回实际看到的片段，最多翻 8 页。无法识别
聊天信息、搜索入口或原生输入框时停止。首版不提供发送者/日期筛选，不把搜索摘要
当完整原话；实机搜索页验证待办。
