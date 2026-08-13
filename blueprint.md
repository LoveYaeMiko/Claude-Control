以下是为 agent 准备的完整项目蓝图，包含架构、文件结构、核心代码框架和运行说明。可直接用于生成一个可工作的原型。

---

# 📱 Claude Code 远程会话查看器 (内网穿透版)

> 目标：通过安卓手机 App 实时查看电脑上运行的 Claude Code 终端会话，无需公网服务器，仅靠内网穿透。

## 1. 整体架构

```
┌──────────────────────────────────────┐
│              电脑端                  │
│                                      │
│  Claude Code (script 启动)          │
│        │                             │
│        ▼                             │
│  日志文件 /tmp/claude_session.log    │
│        │                             │
│        ▼                             │
│  Python 采集与中继服务:              │
│  - 监控日志新行                     │
│  - WebSocket 服务器 (端口 9876)     │
│  - 内网穿透 (pyngrok) 暴露公网URL   │
│        │                             │
└────────┼─────────────────────────────┘
         │ ngrok 隧道 (wss://xxxx.ngrok.io)
         ▼
┌────────────────┐
│   安卓手机 App  │
│  - 输入公网URL  │
│  - 连接WebSocket│
│  - 实时显示会话 │
└────────────────┘
```

- **采集方式**：使用 `script` 命令无侵入地记录 Claude Code 的所有终端输出（包含用户输入回显和 AI 回复），Python 通过 `tail -f` 逻辑持续读取新行。
- **传输**：Python 内建 WebSocket 服务，手机 App 通过内网穿透后的公网地址直连。
- **安全**：WebSocket 连接携带预共享 token，防止匿名访问。

---

## 2. 项目文件结构

```
remote-claude-viewer/
├── computer/                 # 电脑端
│   ├── relay_server.py       # 采集 + WebSocket 服务 + 内网穿透
│   ├── requirements.txt      # Python 依赖
│   └── start.sh              # 一键启动脚本 (可选)
│
└── android/                  # 安卓 App (Android Studio 项目)
    ├── build.gradle (模块级)
    ├── src/main/
    │   ├── AndroidManifest.xml
    │   ├── java/com/example/claudeviewer/
    │   │   ├── MainActivity.kt
    │   │   └── WebSocketClient.kt
    │   └── res/
    │       └── layout/
    │           └── activity_main.xml
    └── ...
```

---

## 3. 环境准备

### 电脑端
- Python 3.8+
- [ngrok](https://ngrok.com/) 账号 (免费版即可)，获取 authtoken
- Claude Code 已安装并能正常运行

### 安卓端
- Android Studio Hedgehog 或更新版本
- 最低 SDK 26 (Android 8.0)，目标 SDK 34

---

## 4. 电脑端实现

### 4.1 Python 依赖 (`computer/requirements.txt`)
```text
websockets>=12.0
pyngrok>=7.0
```

安装：`pip install -r requirements.txt`

### 4.2 核心脚本 (`computer/relay_server.py`)
```python
#!/usr/bin/env python3
"""
Claude Code 会话远程查看 - 电脑端中继服务
功能：监控日志文件 -> 通过 WebSocket 广播 -> 使用 ngrok 暴露公网
"""
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

import websockets
from pyngrok import ngrok, conf

# ---------- 配置 ----------
LOG_FILE = "/tmp/claude_session.log"          # script 产生的日志文件
WS_HOST = "localhost"
WS_PORT = 9876
SECRET_TOKEN = "your-secret-token-change-me"  # 连接凭证
NGROK_AUTH_TOKEN = "your-ngrok-authtoken"     # 从 ngrok 面板获取
# --------------------------

connected_clients = set()

def log_clean_line(line: str) -> str:
    """简单清洗 ANSI 转义序列和多余空白，保留可读内容"""
    import re
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    clean = ansi_escape.sub('', line)
    return clean.rstrip('\n\r')

async def file_watcher():
    """模拟 tail -f 读取日志文件，有新行时广播给所有客户端"""
    # 等待文件出现
    while not Path(LOG_FILE).exists():
        await asyncio.sleep(1)
    
    with open(LOG_FILE, 'r', encoding='utf-8', errors='replace') as f:
        # 跳至文件末尾
        f.seek(0, 2)
        while True:
            line = f.readline()
            if line:
                clean = log_clean_line(line)
                if clean.strip():  # 过滤空行
                    print(f"[SEND] {clean}")
                    # 广播给所有连接的客户端
                    if connected_clients:
                        await asyncio.gather(
                            *[client.send(clean) for client in connected_clients],
                            return_exceptions=True
                        )
            else:
                await asyncio.sleep(0.1)

async def ws_handler(websocket):
    """处理每个 WebSocket 连接"""
    # 验证 token (从 URL 查询参数获取)
    try:
        path = websocket.request.path if hasattr(websocket, 'request') else ''
        # 对于 websockets 库，可以直接从请求路径解析
        # 客户端连接格式: ws://host:port?token=xxx
        query_string = path.split('?')[1] if '?' in path else ''
        params = dict(q.split('=') for q in query_string.split('&') if '=' in q)
        token = params.get('token', '')
        if token != SECRET_TOKEN:
            print(f"[REJECT] Invalid token from {websocket.remote_address}")
            await websocket.close(1008, "Invalid token")
            return
    except Exception:
        await websocket.close(1008, "Auth error")
        return

    connected_clients.add(websocket)
    remote = websocket.remote_address
    print(f"[CONNECT] Client {remote} connected. Total: {len(connected_clients)}")
    try:
        # 保持连接直到客户端断开
        await websocket.wait_closed()
    finally:
        connected_clients.remove(websocket)
        print(f"[DISCONNECT] Client {remote} disconnected. Remaining: {len(connected_clients)}")

async def main():
    # 1. 配置 ngrok
    conf.get_default().auth_token = NGROK_AUTH_TOKEN
    # 2. 启动内网穿透隧道 (提供 TCP 或 TLS 隧道，这里用 TLS 自动获得 wss://)
    try:
        tunnel = ngrok.connect(WS_PORT, "http")  # http 隧道兼容 websocket
        public_url = tunnel.public_url.replace("http://", "ws://").replace("https://", "wss://")
        print(f"\n✅ 公网地址: {public_url}?token={SECRET_TOKEN}\n")
        print(f"📋 请在手机 App 中输入上述完整地址（含 token 参数）\n")
    except Exception as e:
        print(f"❌ ngrok 启动失败: {e}")
        sys.exit(1)

    # 3. 启动 WebSocket 服务器
    print(f"🔌 WebSocket 服务监听 {WS_HOST}:{WS_PORT}")
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        # 4. 同时启动日志监控任务
        watcher_task = asyncio.create_task(file_watcher())
        print("📁 正在监控日志文件... (请先启动 script 会话)")
        print("🚀 服务就绪，按 Ctrl+C 退出")

        # 组合等待，直到被中断
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        
        await stop.wait()
        watcher_task.cancel()
        ngrok.disconnect(tunnel.public_url)
        print("\n🛑 服务已停止")

if __name__ == "__main__":
    asyncio.run(main())
```

### 4.3 启动 Claude Code 并记录日志
打开一个终端窗口，用 `script` 启动 Claude Code：
```bash
script -q -f /tmp/claude_session.log claude
```
然后正常使用 Claude Code，所有输入输出都会实时写入日志。

### 4.4 一键启动脚本 `computer/start.sh`
```bash
#!/bin/bash
echo "正在启动 Claude Code 远程查看服务..."
python3 relay_server.py
```

---

## 5. 安卓 App 实现

### 5.1 技术选型
- 语言：Kotlin
- WebSocket 库：OkHttp (自带 WebSocket 支持)
- UI：RecyclerView + TextView 等宽字体显示

### 5.2 依赖 (`android/build.gradle` 模块)
在 `app/build.gradle` 的 dependencies 中添加：
```groovy
implementation 'com.squareup.okhttp3:okhttp:4.12.0'
implementation 'androidx.recyclerview:recyclerview:1.3.2'
```

### 5.3 权限 (`AndroidManifest.xml`)
```xml
<uses-permission android:name="android.permission.INTERNET" />
<uses-permission android:name="android.permission.ACCESS_NETWORK_STATE" />
<!-- Android 9+ 需要明文流量配置？由于使用 wss，无需额外配置 -->
```

### 5.4 布局 (`res/layout/activity_main.xml`)
一个简单的布局：顶部输入 URL 和连接按钮，中部消息列表。
```xml
<?xml version="1.0" encoding="utf-8"?>
<LinearLayout xmlns:android="http://schemas.android.com/apk/res/android"
    android:layout_width="match_parent"
    android:layout_height="match_parent"
    android:orientation="vertical"
    android:padding="16dp">

    <LinearLayout
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:orientation="horizontal">

        <EditText
            android:id="@+id/urlInput"
            android:layout_width="0dp"
            android:layout_height="wrap_content"
            android:layout_weight="1"
            android:hint="wss://xxx.ngrok.io?token=..."
            android:inputType="textUri" />

        <Button
            android:id="@+id/connectBtn"
            android:layout_width="wrap_content"
            android:layout_height="wrap_content"
            android:text="连接" />
    </LinearLayout>

    <androidx.recyclerview.widget.RecyclerView
        android:id="@+id/messagesRecyclerView"
        android:layout_width="match_parent"
        android:layout_height="0dp"
        android:layout_weight="1"
        android:layout_marginTop="16dp" />

    <TextView
        android:id="@+id/statusText"
        android:layout_width="match_parent"
        android:layout_height="wrap_content"
        android:text="未连接"
        android:gravity="center" />
</LinearLayout>
```

### 5.5 WebSocket 客户端封装 (`WebSocketClient.kt`)
```kotlin
package com.example.claudeviewer

import android.util.Log
import okhttp3.*
import java.util.concurrent.TimeUnit

class WebSocketClient(private val listener: Listener) {
    interface Listener {
        fun onMessage(text: String)
        fun onStatusChange(connected: Boolean)
    }

    private var webSocket: WebSocket? = null
    private val client = OkHttpClient.Builder()
        .readTimeout(0, TimeUnit.MILLISECONDS) // 无超时
        .build()

    fun connect(url: String) {
        val request = Request.Builder().url(url).build()
        webSocket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                listener.onStatusChange(true)
                Log.d("WS", "Connected")
            }

            override fun onMessage(webSocket: WebSocket, text: String) {
                listener.onMessage(text)
            }

            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(1000, null)
                listener.onStatusChange(false)
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                Log.e("WS", "Connection failed", t)
                listener.onStatusChange(false)
            }
        })
    }

    fun disconnect() {
        webSocket?.close(1000, "User closed")
    }
}
```

### 5.6 MainActivity (`MainActivity.kt`)
```kotlin
package com.example.claudeviewer

import android.os.Bundle
import android.text.method.ScrollingMovementMethod
import android.widget.Button
import android.widget.EditText
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView

class MainActivity : AppCompatActivity(), WebSocketClient.Listener {
    private lateinit var urlInput: EditText
    private lateinit var connectBtn: Button
    private lateinit var messagesView: RecyclerView
    private lateinit var statusText: TextView
    private val messageAdapter = MessageAdapter()
    private var wsClient: WebSocketClient? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        urlInput = findViewById(R.id.urlInput)
        connectBtn = findViewById(R.id.connectBtn)
        messagesView = findViewById(R.id.messagesRecyclerView)
        statusText = findViewById(R.id.statusText)

        messagesView.layoutManager = LinearLayoutManager(this)
        messagesView.adapter = messageAdapter

        connectBtn.setOnClickListener {
            val url = urlInput.text.toString().trim()
            if (url.isNotEmpty()) {
                wsClient?.disconnect()
                wsClient = WebSocketClient(this)
                wsClient?.connect(url)
                statusText.text = "正在连接..."
            } else {
                Toast.makeText(this, "请输入 WebSocket URL", Toast.LENGTH_SHORT).show()
            }
        }
    }

    override fun onMessage(text: String) {
        runOnUiThread {
            messageAdapter.addMessage(text)
            messagesView.scrollToPosition(messageAdapter.itemCount - 1)
        }
    }

    override fun onStatusChange(connected: Boolean) {
        runOnUiThread {
            if (connected) {
                statusText.text = "已连接"
                connectBtn.text = "断开"
                connectBtn.setOnClickListener {
                    wsClient?.disconnect()
                    statusText.text = "已断开"
                    connectBtn.text = "连接"
                    connectBtn.setOnClickListener {
                        // 重新绑定连接逻辑
                    }
                }
            } else {
                statusText.text = "连接失败/断开"
                connectBtn.text = "连接"
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        wsClient?.disconnect()
    }
}
```

### 5.7 简易消息适配器 (`MessageAdapter.kt`)
```kotlin
package com.example.claudeviewer

import android.graphics.Typeface
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView

class MessageAdapter : RecyclerView.Adapter<MessageAdapter.ViewHolder>() {
    private val messages = mutableListOf<String>()

    fun addMessage(msg: String) {
        messages.add(msg)
        notifyItemInserted(messages.size - 1)
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): ViewHolder {
        val view = LayoutInflater.from(parent.context)
            .inflate(android.R.layout.simple_list_item_1, parent, false)
        val textView = view.findViewById<TextView>(android.R.id.text1)
        textView.typeface = Typeface.MONOSPACE
        textView.textSize = 12f
        return ViewHolder(view)
    }

    override fun onBindViewHolder(holder: ViewHolder, position: Int) {
        holder.textView.text = messages[position]
    }

    override fun getItemCount() = messages.size

    class ViewHolder(itemView: View) : RecyclerView.ViewHolder(itemView) {
        val textView: TextView = itemView.findViewById(android.R.id.text1)
    }
}
```

---

## 6. 部署与运行步骤

### 6.1 电脑端准备
1. 安装 Python 依赖：
   ```bash
   cd computer
   pip install -r requirements.txt
   ```
2. 修改 `relay_server.py` 中的 `SECRET_TOKEN` 和 `NGROK_AUTH_TOKEN`。
3. 在一个终端窗口启动脚本：
   ```bash
   python3 relay_server.py
   ```
   记下控制台输出的完整公网地址（如 `wss://abc123.ngrok.io?token=your-secret-token-change-me`）。
4. 在**另一个终端**中启动 Claude Code 会话：
   ```bash
   script -q -f /tmp/claude_session.log claude
   ```
   正常进行对话。

### 6.2 安卓 App 使用
1. 用 Android Studio 打开 `android/` 项目，编译安装到手机。
2. 打开 App，在顶部输入框中粘贴电脑端控制台显示的完整 WebSocket URL（包含 `?token=...`）。
3. 点击“连接”，若显示“已连接”，即可实时看到 Claude Code 的终端输出文本。
4. 由于使用了 `script`，输出会原样显示，包含部分控制字符，但已做基础清洗，可读性较好。

---

## 7. 可选的增强方向
- **消息结构化**：改用 Claude Code 的 `--output-format stream-json` 方式，采集 JSON 流，手机端可区用户/AI 消息气泡。
- **持久连接与后台**：加入前台服务，即使 App 切后台也能保持 WebSocket 连接（但注意电池优化）。
- **自动重连**：在 WebSocket 客户端加入指数退避重连逻辑。
- **分享 URL**：电脑端生成二维码，方便手机扫码直接连接。

---

## 8. 故障排除
| 现象 | 可能原因 | 解决方法 |
|------|----------|----------|
| App 连接后立即断开 | token 错误或为空 | 检查 URL 参数 `token=...` 是否与服务器一致 |
| 无法连接 | ngrok 隧道未建立或免费版限制 | 重启电脑端脚本，检查 ngrok 控制台是否活跃 |
| 没有消息显示 | 日志文件路径不对或 `script` 未启动 | 确认 `script` 命令正在运行且日志路径为 `/tmp/claude_session.log` |
| 消息乱码/ANSI码过多 | 清洗逻辑不够强 | 可调整 `log_clean_line` 函数，或手机端进一步过滤 |

---

*请 agent 根据此蓝图构建完整的项目文件，确保代码可直接运行。*