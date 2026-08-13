# android/ — 安卓 App

实时显示电脑上运行的 Claude Code 会话流（对应 `blueprint.md` §5）。当前已完整实现并通过模拟器 + 公网联调验证。

## 技术栈

- 语言：Kotlin 1.9.24
- 构建：Gradle 8.6 / AGP 8.4.2 / compileSdk 34 / minSdk 26（Android 8.0）
- UI：Material Components（Material3 配色）+ RecyclerView
- WebSocket：OkHttp 4.12.0
- Markdown 渲染：Markwon 4.6.2（core / ext-tables / ext-strikethrough）

## 源码结构

| 文件 | 说明 |
|------|------|
| `app/src/main/java/com/example/claudeviewer/MainActivity.kt` | 主界面：连接/断开、会话下拉选择、状态指示、自动滚动 |
| `app/src/main/java/com/example/claudeviewer/WebSocketClient.kt` | OkHttp WebSocket 封装（`?token=` URL 编码、心跳、断线回调） |
| `app/src/main/java/com/example/claudeviewer/Message.kt` | 数据模型与 WS 协议解析（`hello/sessions/messages` 等帧） |
| `app/src/main/java/com/example/claudeviewer/MessageAdapter.kt` | 消息卡片渲染：角色色条、思考/工具折叠、按 idx 去重/替换 |
| `app/src/main/java/com/example/claudeviewer/Markdown.kt` | 全应用共享 Markwon 实例（深色主题配色） |

## 构建

```bash
cd android
./gradlew assembleRelease          # 产物 app/build/outputs/apk/release/app-release.apk
```

**release 签名**：构建读取 `android/keystore.properties`（已 `.gitignore`），指向 `android/keystore/release.jks`。首次构建需自建：

```bash
keytool -genkeypair -v -keystore keystore/release.jks -alias claudecontrol \
  -keyalg RSA -keysize 2048 -validity 10000 \
  -storepass <密码> -keypass <密码> -dname "CN=Claude Control, O=Personal, C=CN"
```

> ⚠️ 升级 App 必须沿用同一个 keystore，否则覆盖安装会因签名不一致失败。请备份 `release.jks`。

## 连接配置

App 打开后在「设置」面板填入：

- **URL**：`wss://<公网隧道域名>`（cloudflared/ngrok）或 `ws://<电脑局域网IP>:9876`
- **Token**：与电脑端 `.env` 中 `CC_TOKEN` 一致

`AndroidManifest.xml` 已声明 `INTERNET` / `ACCESS_NETWORK_STATE` 权限及 `usesCleartextTraffic="true"`（局域网 `ws://` 可用）。
