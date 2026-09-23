# hermes-phone-agent

语音上下文：调用 `wechat_collect_context` 时传入 `transcribe_voice: true`、
`max_voice: 3`，可对明确识别的语音气泡使用微信内置“转文字”，返回
`voice_transcripts` 给 agent 理解。不会播放、下载或发送音频。
目前依赖无障碍树的语音标记，不会只凭 OCR 的秒数猜位置；转换菜单不可用、
超时或文字无法确认时会返回状态。结果来自气泡下方新增可见文字，并非音频级校验，
可能包含转写错误。MCP、Neko 保持默认关闭，需各自在接口中显式接入参数。

昵称含表情时，若微信通知将其变成 `[Sticker]`、`[emoji]` 或 `[表情]`，
搜索会改用保留的最长文字片段，再校验唯一结果及进入后的完整标题。
占位符不能还原原始 emoji；同名多结果、OCR 无法识别符号或昵称只剩占位符时，
需提供原始昵称或唯一微信备注，不会猜选联系人。真实 Unicode emoji 保持原样，
回复正文不会做此替换。

自动微信任务会持有设备直到本轮清理完成，并将发送记录持久化，供重启后的重试去重。
行为边界及离线验证方法见 [自动任务可靠性说明](AUTOMATION.md)。

[English](README.md) | 中文

两个 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 插件 + 一个 Android 辅助 APK，让 Hermes 控制并响应虚拟 Android 手机。

实验性[微信引用回复](QUOTED_REPLIES.md)：定位实际读到的原话，核对引用预览后再发送；尚待实机验证。

## 组件

| 组件 | 类型 | 用途 |
|------|------|------|
| `plugins/phone_use` | Hermes 工具插件 | Agent **控制**手机 — 点击、滑动、输入、截图、启动应用（通过 ADB，可选 Appium 混合模式支持 Unicode/WebView） |
| `plugins/phone_events` | Hermes 平台插件 | 手机**触发** Agent — 通知、应用切换、UI 变化 |
| `helper-apk` | Android 应用 | 在设备端运行服务，捕获丰富事件并转发给主机 |

## 环境要求

- 已安装 [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- Android SDK Platform Tools（`adb` 在 PATH 中）
- 正在运行的 Android 模拟器（Android Studio AVD 或 `emulator` 命令行）
  注：Android Studio 安装说明在 README 末尾

**可选**（混合后端 — Unicode 文本输入和 WebView 支持）：
- [Appium](https://appium.io/)（`npm install -g appium`）
- Appium Python 客户端（`pip install Appium-Python-Client`）

## 安装

```bash
# 安装两个 Hermes 插件
hermes plugins install Ctrl-Creeper/hermes-phone-agent/plugins/phone_use
hermes plugins install Ctrl-Creeper/hermes-phone-agent/plugins/phone_events

# 在运行中的 Android 模拟器上安装辅助 APK 并授予权限
git clone https://github.com/Ctrl-Creeper/hermes-phone-agent.git
cd hermes-phone-agent
./setup.sh

# 启用插件
hermes plugins enable phone_use
hermes plugins enable phone_events
```

## 策略引擎

Agent 的自主权由 `phone-policy.yaml` 控制 — 一个配置文件，定义 Agent 可以自主执行哪些操作，哪些需要你的批准。完整参考和示例见 [POLICY.md](POLICY.md)。

```bash
# 复制默认策略
cp phone-policy.yaml ~/.hermes/phone-policy.yaml

# 按需编辑 — 修改立即生效
```

你也可以使用 POLICY.md 中的提示模板，让任何 AI 助手帮你自动生成配置。

## 后端配置

插件通过 `HERMES_PHONE_BACKEND` 环境变量支持多种后端：

| 值 | 说明 |
|----|------|
| `adb`（默认） | 纯 ADB — 快速，无额外依赖 |
| `hybrid` | 大部分操作使用 ADB + Unicode 文本输入和 WebView 交互使用 Appium。自动启动和管理 Appium 服务器。 |
| `noop` | 空操作后端，用于测试 |

```bash
# 使用混合后端
export HERMES_PHONE_BACKEND=hybrid

# 自定义 Appium 端口（默认：4723）
export APPIUM_PORT=4724

```

Hermes 用户应在 `~/.hermes/config.yaml` 中配置模拟器和 Telegram 目标；
`phone_use` 与 `phone_events` 会共用同一个序列号：

```yaml
platforms:
  phone_events:
    enabled: true
    extra:
      telegram_chat_id: "<你的-Telegram-chat-id>"
      telegram_user_id: "<你的-Telegram-user-id>"
      serial: "emulator-5554"
      raw_notifications: false
```

`ANDROID_SERIAL` 仅保留为独立运行时的兼容回退。
通知默认经过脱敏和截断。只有 Telegram 目标为私人会话且你明确需要原文时，
才设置 `raw_notifications: true`。

## 确定性微信工作流

语音转写需连续两次观察到相同文字；变化中的片段、气泡移位、转换失败或切到其他应用
不会被当作转写成功。文字仍依据气泡下方的位置关联，不能保证音频识别准确，尚待实机验证。

读取图片时同时返回截图及 `image_analysis`：OCR 文字、二维码内容，通过从 1 开始的
`image_index` 对应截图。调用 `wechat_collect_context` 并设置 `include_images=true`、
`open_images=true`、`max_images=3` 即可。模型继续通过截图理解图片；二维码仅解码为
数据，不自动打开链接、登录或支付。

解码依赖 macOS Vision；安装时运行 `setup.sh` 重建新版宿主 OCR 程序即可，Android
APK 无需更新。缺少解码器返回 `unavailable`，旧程序返回 `helper_upgrade_required`，
不会误报成“没有二维码”。返回内容有长度上限，二维码截断会标注。预览截图可能带有
界面控件，提取文字不保证全部来自原图。

每次看图都会确认进入微信图片预览，保存截图，再确认回到原聊天后进行本地识别。
退出后重新获取控件，不沿用旧编号；恢复失败返回 `chat_restored=false` 并停止，
避免继续在错误页面点图或翻页。`max_images` 限制尝试次数（包括打开失败），
原生控件与 Vision 对同一缩略图的重叠识别会合并。

测试覆盖翻页找图、连续多图、控件编号刷新、真实 Vision 文字/二维码解码、返回失败，
以及保留草稿后继续调用手机工具读取聊天。设备跳转使用模拟测试，真实微信仍待验证。
收集结束停留在检查过的聊天记录位置，不保证恢复进入前的滚动位置；不会发送、输入、
触发微信扫码或回桌面。目前是有限历史范围内找图片候选，不保证按任意描述找出某张图，
也不保证下载到原始分辨率文件。
上述行为针对收集调用本身。phone-events 网关仍有独立的任务结束回桌面策略，
并不提供人或其他宿主同时操作时的设备独占保证。

实验性[附件发送](ATTACHMENTS.md)通过“文件”入口发送文档和原图，确认绑定文件哈希；尚待实机验证。

实验性[历史搜索](HISTORY_SEARCH.md)可在指定聊天的“查找聊天记录”中按关键词读取有限页结果；尚待实机验证。

实验性[文字收藏](FAVORITES.md)在确认后收藏唯一可见的原话；尚待实机菜单验证。

`phone_use` 提供打开会话、收集最近聊天记录和回复消息的组合操作。它通过
微信搜索定位会话，并在进入后校验标题，不依赖会话列表中的固定位置。对于
无障碍控件树为空或不完整的微信界面，会自动使用宿主机 OCR。
宿主 OCR 回退目前需要 macOS 和 `swiftc`，`setup.sh` 会自动编译安装；其他平台
仍可使用 Android 无障碍控件树与截图。

如需自动任务收件箱，请在复制到 `~/.hermes/phone-policy.yaml` 的策略中添加
`event_rules`。只有经过认证且包含明确触发词的规则才应设置
`instruction_source: true`，同时只允许该工作流需要的操作。回复流程会在发送前
恢复导航错误；一旦尝试发送，就不会因为确认失败而再次发送，从而避免联网搜索后
的长回复重复出现。

对于经过认证的微信自动回复，进入聊天时会记录已确认的对方消息，并在回到桌面前
只补查一次新增消息。新增私聊消息会按原有事件策略排队；群聊只接受新增、左侧且
含配置的 `@YourBot` 触发词的消息。方向或群/私聊类型不明、类似 `发送者:` 的引用摘要、
以及无法归类的 OCR 都会忽略，不会猜测。这是前台时系统通知可能缺失的补偿，不是
聊天记录同步；投入生产前仍需针对实际微信布局验证。

经过认证的微信好友请求通知不会调用模型，而是直接发送到配置的 Telegram 目标。
回复 `/approve` 可接受指定申请人且不设置备注，回复 `/deny` 则忽略。等待审批时
不会占用手机操作队列，因此其他手机任务仍可继续执行。

混合后端使用 ADB 执行快速操作（截图、点击、滑动、按键、应用管理），仅在需要 Unicode 文本输入或 ADB 的 `uiautomator dump` 失败时才懒加载启动 Appium。如果未安装 Appium，自动回退到纯 ADB。

## 脱离 Hermes 使用

如需从 Claude、Codex、GPT、Gemini 或其他 Agent 框架（无需 Hermes）使用手机控制功能，请参阅独立的 [phone-mcp-server](https://github.com/Ctrl-Creeper/phone-mcp-server) 仓库。它提供 MCP 和 HTTP 服务器，使用相同的后端和安全模型。

另一个直接下游是 [Neko phone_workflows 插件](https://github.com/Ctrl-Creeper/n.e.k.o_plugin_phone_workflows)。维护者和 coding agent 请先阅读 [DOWNSTREAMS.md](DOWNSTREAMS.md)：其中记录了两个下游的同步方式、统一的 helper APK 标识，以及必须单独适配的宿主功能。

## 安全

完整威胁模型和缓解措施见 [SECURITY.md](SECURITY.md)。

要点：
- 所有 ADB 命令使用参数列表方式调用 subprocess — 不可能发生 shell 注入
- 手机内容（通知、UI 文本）被视为不可信数据，永远不会被当作指令
- 策略引擎强制执行按应用的操作限制（例如，金融类应用为只读）
- 危险操作（`install_apk`、`shell`）始终需要用户明确批准
- 辅助 APK socket 使用每会话认证令牌
- 可配置的事件过滤和敏感数据脱敏

## 架构

```
┌─────────────────────────────────────────────────┐
│  主机 (PC / Mac)                                │
│  ┌─────────────────────┐  ┌───────────────┐     │
│  │  phone_use           │  │ phone_events  │     │
│  │  (工具插件)           │  │ (平台插件)     │     │
│  │                      │  └───────┬───────┘     │
│  │  ┌────────────────┐  │          │             │
│  │  │ ADB 后端       │──┼── ADB ───┤             │
│  │  └────────────────┘  │          │             │
│  │  ┌────────────────┐  │          │             │
│  │  │ Appium (可选)  │──┼── HTTP ──┤             │
│  │  └────────────────┘  │          │             │
│  └──────────────────────┘          │             │
│  ┌──────────────────┐              │             │
│  │  Appium 服务器    │  (自动管理，懒加载启动)     │
│  └──────────────────┘              │             │
├────────────────────────────────────┼─────────────┤
│  Android 模拟器                    │             │
│  ┌───────────────────────────────────────────┐   │
│  │  hermes-phone-agent-v0.2.4.apk           │   │
│  │  • NotificationListenerService            │   │
│  │  • AccessibilityService                   │   │
│  │  • BroadcastReceiver                      │   │
│  └───────────────────────────────────────────┘   │
└──────────────────────────────────────────────────┘
```

## 从零开始搭建 Android Studio / 模拟器

本指南介绍如何安装 Android Studio、创建 Android 虚拟手机，以及通过命令行使用 `adb` 和 `emulator` 启动它。

### 1. 安装 Android Studio

从 Android 开发者官网下载并安装 Android Studio：

```text
https://developer.android.com/studio
```

### 2. 创建 Android 虚拟设备

打开 Android Studio 并创建虚拟手机：

```text
Android Studio
→ Device Manager（设备管理器）
→ Create Virtual Device（创建虚拟设备）
```

推荐设置：

```text
设备：Pixel / Medium Phone
系统镜像：Android 12 或更新版本
镜像类型：Google Play 或 Google APIs
架构：Apple Silicon Mac 上选择 arm64-v8a
```

创建完成后，从 Android Studio 启动一次以确认能正常开机。

### 3. 将 Android SDK 工具添加到 PATH

在 macOS 上，Android SDK 通常安装在：

```bash
~/Library/Android/sdk
```

将 emulator 和 platform-tools 目录添加到 shell PATH：

```bash
echo 'export ANDROID_HOME="$HOME/Library/Android/sdk"' >> ~/.zshrc
echo 'export PATH="$ANDROID_HOME/emulator:$ANDROID_HOME/platform-tools:$PATH"' >> ~/.zshrc
source ~/.zshrc
```

验证工具可用：

```bash
adb version
emulator -version
```

### 4. 列出可用虚拟设备

运行：

```bash
emulator -list-avds
```

示例输出：

```text
Medium_Phone_API_36.1
```

### 5. 从命令行启动虚拟手机

使用上一步中的 AVD 名称。

示例：

```bash
emulator @Medium_Phone_API_36.1
```

也可以以更低开销启动：

```bash
emulator @Medium_Phone_API_36.1 -no-boot-anim -no-audio
```

不要忘记 AVD 名称前的 `@`。

或者使用以下格式：

```bash
emulator -avd Medium_Phone_API_36.1
```

### 6. 检查 ADB 能否识别设备

打开另一个终端窗口，运行：

```bash
adb devices
```

预期输出：

```text
List of devices attached
emulator-5554   device
```

如果设备显示为 `device`，则已就绪。

### 7. 常用 ADB 命令

截图：

```bash
adb exec-out screencap -p > screen.png
```

点击屏幕：

```bash
adb shell input tap 500 1200
```

滑动/滚动：

```bash
adb shell input swipe 500 1600 500 500 300
```

返回键：

```bash
adb shell input keyevent 4
```

Home 键：

```bash
adb shell input keyevent 3
```

## 许可证

AGPL-3.0 license
