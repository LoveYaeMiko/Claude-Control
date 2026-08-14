package com.example.claudeviewer

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * OkHttp WebSocket 封装，与电脑端 /ws 端点通讯。
 * 协议：
 *   connect   -> 请求 ws://host:port/ws?token=xxx（token 经 URL 编码）
 *   服务端推  -> hello | sessions | session-start | session-update | session-end | messages | ping
 *   messages  -> { sessionId, isHistory?, update?, records[] }（update=true 表示按 idx 替换）
 *   客户端发  -> get-history | list-sessions | ping
 * 所有回调已在 OkHttp 的线程池线程触发，UI 线程需自行 post。
 */
class WebSocketClient(
    private val baseUrl: String,   // 形如 ws://192.168.1.10:8233
    private val token: String,
) {
    interface Listener {
        fun onOpen()
        fun onSessions(sessions: List<SessionInfo>)
        fun onSessionStart(session: SessionInfo)
        fun onSessionUpdate(session: SessionInfo)
        fun onSessionEnd(sessionId: String)
        fun onMessages(sessionId: String, isHistory: Boolean, isUpdate: Boolean, messages: List<ViewMessage>)
        fun onInteraction(status: String, sessionId: String, message: String)
        fun onClosing(code: Int, reason: String)
        fun onFailure(t: Throwable)
    }

    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)
        .build()
    private var ws: WebSocket? = null

    private val wsListener = object : WebSocketListener() {
        override fun onOpen(webSocket: WebSocket, response: Response) {
            listenerRef?.onOpen()
        }

        override fun onMessage(webSocket: WebSocket, text: String) {
            try {
                val obj = JSONObject(text)
                when (obj.optString("type")) {
                    "hello", "sessions" -> listenerRef?.onSessions(Protocol.parseSessions(obj))
                    "session-start" -> obj.optJSONObject("session")?.let {
                        listenerRef?.onSessionStart(Protocol.parseSession(it))
                    }
                    "session-update" -> obj.optJSONObject("session")?.let {
                        listenerRef?.onSessionUpdate(Protocol.parseSession(it))
                    }
                    "session-end" -> listenerRef?.onSessionEnd(obj.optString("sessionId"))
                    "messages" -> {
                        val sid = obj.optString("sessionId")
                        val isHistory = obj.optBoolean("isHistory")
                        val isUpdate = obj.optBoolean("update")
                        listenerRef?.onMessages(sid, isHistory, isUpdate, Protocol.parseMessages(obj))
                    }
                    "interaction" -> listenerRef?.onInteraction(
                        obj.optString("status"),
                        obj.optString("sessionId"),
                        obj.optString("message"),
                    )
                }
            } catch (_: Exception) {
                // 忽略无法解析的帧
            }
        }

        override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
            listenerRef?.onClosing(code, reason)
        }

        override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
            listenerRef?.onFailure(t)
        }
    }

    private var listenerRef: Listener? = null

    fun connect(listener: Listener) {
        this.listenerRef = listener
        // 兼容两种输入：手动填的基础地址（无 /ws）与扫码得到的完整 WS 地址（含 /ws），
        // 统一剥掉末尾 /ws 后重拼，避免拼出 /ws/ws 导致连接失败。
        val host = baseUrl.trimEnd('/').removeSuffix("/ws")
        val url = if (token.isBlank()) "$host/ws"
        else "$host/ws?token=${URLEncoder.encode(token, "UTF-8")}"
        val request = Request.Builder().url(url).build()
        ws = client.newWebSocket(request, wsListener)
    }

    fun listSessions() = ws?.send(JSONObject().put("type", "list-sessions").toString())

    fun getHistory(sessionId: String) =
        ws?.send(JSONObject().put("type", "get-history").put("sessionId", sessionId).toString())

    fun sendPrompt(prompt: String, sessionId: String?) =
        ws?.send(JSONObject()
            .put("type", "send-prompt")
            .put("prompt", prompt)
            .put("sessionId", sessionId ?: "")
            .toString())

    fun close() {
        listenerRef = null
        ws?.close(1000, "bye")
        ws = null
    }
}
