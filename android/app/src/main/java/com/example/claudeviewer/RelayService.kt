package com.example.claudeviewer

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import androidx.core.app.NotificationCompat

/**
 * 前台服务：持有 WebSocket 连接，让手机切后台时连接不被系统冻结/杀掉。
 *
 * WebSocketClient 的永久 Listener 是本服务（而非 Activity），回调经 [uiListener] 转发给
 * 当前界面的 MainActivity；断连后按指数退避自动重连（网络抖动 / 隧道瞬断）。
 * 连接生命周期由 companion 的静态状态管理，MainActivity 经 [configure]/[disconnect] 控制。
 */
class RelayService : Service(), WebSocketClient.Listener {

    companion object {
        const val CHANNEL_ID = "claudecontrol_relay"
        const val NOTIFICATION_ID = 1

        @Volatile var client: WebSocketClient? = null
            private set
        @Volatile var uiListener: WebSocketClient.Listener? = null
        @Volatile var shouldReconnect = false
        @Volatile private var url = ""
        @Volatile private var token = ""

        /** 记录连接参数与 UI 监听器（不在此建连；由服务的 onStartCommand 建连）。 */
        fun configure(url: String, token: String, listener: WebSocketClient.Listener) {
            this.url = url
            this.token = token
            this.uiListener = listener
            this.shouldReconnect = true
        }

        /** 手动断开：停止重连、关闭连接并置空。 */
        fun disconnect() {
            shouldReconnect = false
            client?.close()
            client = null
            uiListener = null
        }
    }

    private val handler = Handler(Looper.getMainLooper())
    private var backoffMs = 1000L
    private var reconnectRunnable: Runnable? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
        startForegroundCompat(getString(R.string.status_connecting))
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // 进程被系统杀掉后按 START_STICKY 重启时，companion 静态态已丢失，从偏好读回连接参数
        if (url.isBlank()) {
            val prefs = getSharedPreferences("claudeviewer", MODE_PRIVATE)
            url = prefs.getString("ws_url", "") ?: ""
            token = prefs.getString("ws_token", "") ?: ""
            shouldReconnect = prefs.getBoolean("connected", false)
        }
        if (url.isBlank() || !shouldReconnect) {
            stopSelf()
            return START_NOT_STICKY
        }
        open()
        return START_STICKY
    }

    override fun onDestroy() {
        cancelReconnect()
        super.onDestroy()
    }

    private fun open() {
        cancelReconnect()
        val c = WebSocketClient(url, token)
        client = c
        c.connect(this)   // 本服务作为永久 Listener
    }

    private fun startForegroundCompat(text: String) {
        val n = buildNotification(text)
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
        } else {
            startForeground(NOTIFICATION_ID, n)
        }
    }

    // ---- WebSocketClient.Listener：转发给 MainActivity ----
    override fun onOpen() {
        backoffMs = 1000L
        handler.post { startForegroundCompat(getString(R.string.status_connected)) }
        uiListener?.onOpen()
    }

    override fun onSessions(sessions: List<SessionInfo>) {
        uiListener?.onSessions(sessions)
    }

    override fun onSessionStart(session: SessionInfo) {
        uiListener?.onSessionStart(session)
    }

    override fun onSessionUpdate(session: SessionInfo) {
        uiListener?.onSessionUpdate(session)
    }

    override fun onSessionEnd(sessionId: String) {
        uiListener?.onSessionEnd(sessionId)
    }

    override fun onMessages(sessionId: String, isHistory: Boolean, isUpdate: Boolean, messages: List<ViewMessage>, hasMore: Boolean) {
        uiListener?.onMessages(sessionId, isHistory, isUpdate, messages, hasMore)
    }

    override fun onInteraction(status: String, sessionId: String, message: String) {
        uiListener?.onInteraction(status, sessionId, message)
    }

    override fun onPermissionRequest(requestId: String, sessionId: String, toolName: String, displayName: String, description: String, input: String) {
        uiListener?.onPermissionRequest(requestId, sessionId, toolName, displayName, description, input)
    }

    override fun onPermissionResponseError(requestId: String, message: String) {
        uiListener?.onPermissionResponseError(requestId, message)
    }

    override fun onClosing(code: Int, reason: String) {
        uiListener?.onClosing(code, reason)
        scheduleReconnect()
    }

    override fun onFailure(t: Throwable) {
        uiListener?.onFailure(t)
        scheduleReconnect()
    }

    private fun scheduleReconnect() {
        if (!shouldReconnect || reconnectRunnable != null) return
        val delay = backoffMs
        backoffMs = (backoffMs * 2).coerceAtMost(30_000L)
        val runnable = Runnable {
            reconnectRunnable = null
            if (!shouldReconnect) return@Runnable
            handler.post { startForegroundCompat(getString(R.string.status_connecting)) }
            open()
        }
        reconnectRunnable = runnable
        handler.postDelayed(runnable, delay)
    }

    private fun cancelReconnect() {
        reconnectRunnable?.let { handler.removeCallbacks(it) }
        reconnectRunnable = null
    }

    private fun createChannel() {
        if (Build.VERSION.SDK_INT < 26) return
        val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        nm.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, getString(R.string.notification_channel), NotificationManager.IMPORTANCE_LOW)
        )
    }

    private fun buildNotification(text: String): Notification {
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_dialog_info)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setOngoing(true)
            .setContentIntent(pi)
            .build()
    }
}
