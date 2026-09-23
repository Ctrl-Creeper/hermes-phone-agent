# hermes-phone-agent

昵称含表情时，若微信通知将其变成 `[Sticker]`、`[emoji]` 或 `[表情]`，
搜索会改用保留的最长文字片段，再校验唯一结果及进入后的完整标题。
占位符不能还原原始 emoji；同名多结果、OCR 无法识别符号或昵称只剩占位符时，
需提供原始昵称或唯一微信备注，不会猜选联系人。真实 Unicode emoji 保持原样，
回复正文不会做此替换。

[English](README.md) | 中文

两个 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 插件 + 一个 Android 辅助 APK，让 Hermes 控制并响应虚拟 Android 手机。

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
