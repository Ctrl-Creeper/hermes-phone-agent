# WeChat attachments (experimental)

`phone_use` action `wechat_send_attachment` accepts `chat` and `file_path`.
Documents and images are sent through WeChat's File picker; images remain original
files, not compressed album messages. Local regular files must be nonempty and
at most 20 MiB. The approval shows the exact destination, resolved path, byte
count and SHA-256. The contents are checked again after approval and an immutable
snapshot is copied to Android; a remote SHA-256 check verifies the transfer.

```json
{"action":"wechat_send_attachment","chat":"Example","file_path":"/path/to/report.pdf"}
```

This action requires a separate approval and is not added to automatic inbox
allowlists. An operator must explicitly allow it in the event policy if it is to
be requested from a phone-triggered turn. Ordinary text-reply permissions do not
grant access to host files.

The initial supported UI route is More → File → Phone storage → Download → exact
staged filename → a summary showing `Send to: <chat>` (or its Chinese equivalent),
the filename and a unique Send button. Missing, duplicate or unsupported controls
stop before Send. No coordinates are guessed. The final injection is never
retried, including exceptions. Upload completion is not yet verifiable, so a send
attempt returns `delivery_status=uncertain`; it must not be automatically retried.
The workflow always attempts to return Home.

The attachment is uniquely named `hermes-<uuid>.<extension>` on Android and in the
recipient's file message. The staged copy is retained in `/sdcard/Download/` to
avoid deleting a file WeChat may still be uploading. Users can remove it after
checking delivery. No APK change is required.

Offline tests cover checksum changes, actual snapshot contents passed to ADB,
approval metadata and simulated picker transitions. Real WeChat file-picker
validation remains pending; this is a draft, not a claim that every WeChat version
uses this summary layout. MCP/Neko may sync the shared backend/workflow, but their
own adapters must expose and authorize attachment sending separately.

中文：新增附件发送草稿，支持文件和原图（以文件形式发送），大小上限 20 MiB。
确认绑定聊天、文件路径和哈希；页面无法验证就停止。发送结果目前保守返回“不确定”，
不会自动重发。实机选择器验证待办；临时副本保留在 Android Download，便于上传完成。
