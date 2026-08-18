package com.example.claudeviewer

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.OpenableColumns
import android.util.Base64
import android.view.LayoutInflater
import android.view.Menu
import android.view.MenuItem
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.ImageButton
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AppCompatDelegate
import androidx.appcompat.widget.AppCompatSpinner
import androidx.appcompat.widget.Toolbar
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.textfield.TextInputEditText
import com.journeyapps.barcodescanner.ScanContract
import com.journeyapps.barcodescanner.ScanOptions
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 远程会话监视器：连接电脑端 relay_server，浏览会话并实时查看消息。
 */
class MainActivity : AppCompatActivity(), WebSocketClient.Listener {

    private lateinit var urlInput: TextInputEditText
    private lateinit var tokenInput: TextInputEditText
    private lateinit var connectBtn: MaterialButton
    private lateinit var sessionSpinner: AppCompatSpinner
    private lateinit var statusText: TextView
    private lateinit var statusDot: View
    private lateinit var configPanel: LinearLayout
    private lateinit var configToggle: TextView
    private lateinit var emptyView: TextView
    private lateinit var recycler: RecyclerView
    private lateinit var scanBtn: MaterialButton
    private lateinit var inputBar: LinearLayout
    private lateinit var promptInput: TextInputEditText
    private lateinit var sendBtn: MaterialButton
    private lateinit var attachBtn: ImageButton
    private lateinit var attachmentBar: LinearLayout
    private lateinit var newSessionBanner: LinearLayout

    private val adapter = MessageAdapter()
    // 连接由前台服务 RelayService 持有；这里只读其当前 client 以便发帧。
    private val ws: WebSocketClient? get() = RelayService.client
    private val sessions = mutableListOf<SessionInfo>()
    private var selectedSessionId: String? = null
    private var historyLoadedFor: String? = null
    private var pendingSessionId: String? = null
    // 新建会话：进入「客户端空白占位」模式，可取消；记住进入前会话以便取消时恢复
    private var newSessionMode = false
    private var preNewSessionId: String? = null
    // 分页状态：向上翻找时按页加载更早记录
    private var hasMoreOlder = false
    private var loadingOlder = false
    private var oldestLoadedIdx: Int? = null
    private var suppressSelection = false
    // 多模态附件 + 权限审批
    private val pendingAttachments = mutableListOf<Attachment>()
    private val permissionQueue = java.util.ArrayDeque<PermissionRequest>()
    private var permissionDialogShowing = false
    // 附件读取的后台协程作用域（避免在主线程 readBytes 导致 ANR/OOM）
    private val ioScope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    private data class PermissionRequest(
        val requestId: String,
        val sessionId: String,
        val toolName: String,
        val description: String,
        val input: String,
    )

    companion object {
        private const val HISTORY_PAGE = 50
        private const val MAX_ATTACHMENTS = 5
        private const val MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
        // 占位项：spinner 首位“请选择会话”，选中它不加载任何历史
        private val PLACEHOLDER = SessionInfo(id = "", cwd = "", title = "", status = "", model = "")
        private val IMAGE_MIMES = setOf("image/jpeg", "image/png", "image/gif", "image/webp")
    }

    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let { applyConfigUri(it) }
    }

    private val attachLauncher = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        onAttachmentsPicked(uris)
    }

    // Android 13+ 前台服务常驻通知需要运行时授权；授权与否不影响服务启动，仅影响通知展示。
    private val notificationPermissionLauncher = registerForActivityResult(ActivityResultContracts.RequestPermission()) {}

    override fun onCreate(savedInstanceState: Bundle?) {
        val prefs = getSharedPreferences("claudeviewer", MODE_PRIVATE)
        applyThemeMode(prefs.getString("theme", "system") ?: "system")
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val toolbar = findViewById<Toolbar>(R.id.toolbar)
        setSupportActionBar(toolbar)

        urlInput = findViewById(R.id.url_input)
        tokenInput = findViewById(R.id.token_input)
        connectBtn = findViewById(R.id.connect_btn)
        sessionSpinner = findViewById(R.id.session_spinner)
        statusText = findViewById(R.id.status_text)
        statusDot = findViewById(R.id.status_dot)
        configPanel = findViewById(R.id.config_panel)
        configToggle = findViewById(R.id.config_toggle)
        emptyView = findViewById(R.id.empty_view)
        recycler = findViewById(R.id.message_list)
        scanBtn = findViewById(R.id.scan_btn)
        inputBar = findViewById(R.id.input_bar)
        promptInput = findViewById(R.id.prompt_input)
        sendBtn = findViewById(R.id.send_btn)
        attachBtn = findViewById(R.id.attach_btn)
        attachmentBar = findViewById(R.id.attachment_bar)
        newSessionBanner = findViewById(R.id.new_session_banner)
        findViewById<TextView>(R.id.new_session_cancel).setOnClickListener { cancelNewSession() }

        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = adapter
        recycler.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) {
                if (dy < 0) maybeLoadOlder()   // 向上翻找时按需加载更早记录
            }
        })

        urlInput.setText(prefs.getString("ws_url", "ws://192.168.1.38:9876"))
        tokenInput.setText(prefs.getString("ws_token", ""))

        connectBtn.setOnClickListener { toggleConnection() }
        scanBtn.setOnClickListener { launchScan() }
        sendBtn.setOnClickListener { sendPrompt() }
        attachBtn.setOnClickListener { launchAttach() }
        configToggle.setOnClickListener {
            val show = configPanel.visibility != View.VISIBLE
            configPanel.visibility = if (show) View.VISIBLE else View.GONE
        }

        sessionSpinner.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, v: View?, pos: Int, id: Long) {
                if (suppressSelection) return
                loadSession(pos)
            }
            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
        }

        // 主题切换 / 进程重建后恢复上次选中的会话（等会话列表到达后由 maybeSelectPending 选中）
        pendingSessionId = prefs.getString("last_session", "")?.takeIf { it.isNotBlank() }
        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onResume() {
        super.onResume()
        RelayService.uiListener = this   // 进程重建/主题切换后把回调重新指向当前 Activity
        autoConnectIfNeeded()
    }

    override fun onDestroy() {
        // 前台服务继续持有连接（后台保活），这里只解绑 UI 回调，不再关闭 socket
        if (RelayService.uiListener === this) RelayService.uiListener = null
        super.onDestroy()
    }

    // ---- 菜单：新建会话 / 主题切换 ----
    override fun onCreateOptionsMenu(menu: Menu): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_new_session -> { newSession(); true }
            R.id.action_theme -> { cycleTheme(); true }
            else -> super.onOptionsItemSelected(item)
        }
    }

    /** 进入「新会话」空白占位模式：记住进入前会话，清空选择，下一条 prompt 开新会话；可经 banner「取消」退出。 */
    private fun newSession() {
        preNewSessionId = selectedSessionId
        newSessionMode = true
        selectedSessionId = null
        historyLoadedFor = null
        oldestLoadedIdx = null
        hasMoreOlder = false
        loadingOlder = false
        suppressSelection = true
        sessionSpinner.setSelection(0)
        suppressSelection = false
        adapter.submit(emptyList(), clear = true)
        updateEmptyState()
        // 显式提示条：让用户第一时间察觉已切到「新会话」状态，下一条 prompt 开新会话
        newSessionBanner.visibility = View.VISIBLE
        newSessionBanner.alpha = 0f
        newSessionBanner.animate().alpha(1f).setDuration(180).start()
    }

    /** 取消新建：恢复到进入前选中的会话（若有），否则保持空白。 */
    private fun cancelNewSession() {
        if (!newSessionMode) return
        newSessionMode = false
        newSessionBanner.visibility = View.GONE
        val prev = preNewSessionId
        preNewSessionId = null
        if (prev != null) {
            val pos = sessions.indexOfFirst { it.id == prev }
            if (pos >= 0) {
                sessionSpinner.setSelection(pos + 1)   // 触发 onItemSelected -> loadSession 恢复历史
                return
            }
        }
        selectedSessionId = null
        historyLoadedFor = null
        oldestLoadedIdx = null
        hasMoreOlder = false
        loadingOlder = false
        adapter.submit(emptyList(), clear = true)
        updateEmptyState()
    }

    private fun applyThemeMode(mode: String) {
        when (mode) {
            "light" -> AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_NO)
            "dark" -> AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_YES)
            else -> AppCompatDelegate.setDefaultNightMode(AppCompatDelegate.MODE_NIGHT_FOLLOW_SYSTEM)
        }
    }

    private fun cycleTheme() {
        val prefs = getSharedPreferences("claudeviewer", MODE_PRIVATE)
        val next = when (prefs.getString("theme", "system") ?: "system") {
            "system" -> "light"
            "light" -> "dark"
            else -> "system"
        }
        prefs.edit().putString("theme", next).apply()
        Markdown.reset()
        toast(when (next) {
            "light" -> getString(R.string.theme_light)
            "dark" -> getString(R.string.theme_dark)
            else -> getString(R.string.theme_system)
        })
        recreate()
    }

    // ---- 附件（多模态） ----
    private fun launchAttach() {
        attachLauncher.launch((IMAGE_MIMES + "application/pdf").toTypedArray())
    }

    /** 在后台协程读取选中的附件，主线程只回填结果（避免大文件在主线程 readBytes 卡死/超内存）。 */
    private fun onAttachmentsPicked(uris: List<Uri>) {
        ioScope.launch {
            val added = mutableListOf<Attachment>()
            for (uri in uris) {
                if (pendingAttachments.size + added.size >= MAX_ATTACHMENTS) {
                    withContext(Dispatchers.Main) { toast(getString(R.string.attachment_max)) }
                    break
                }
                val mime = contentResolver.getType(uri)
                if (mime == null || !(mime.startsWith("image/") || mime == "application/pdf")) {
                    withContext(Dispatchers.Main) { toast(getString(R.string.attachment_unsupported)) }
                    continue
                }
                val name = queryDisplayName(uri) ?: ""
                val bytes = try { readLimited(uri, MAX_ATTACHMENT_BYTES) } catch (_: Exception) { null }
                if (bytes == null) {
                    withContext(Dispatchers.Main) { toast(getString(R.string.attachment_read_failed)) }
                    continue
                }
                if (bytes.size > MAX_ATTACHMENT_BYTES) {
                    withContext(Dispatchers.Main) { toast(getString(R.string.attachment_too_large)) }
                    continue
                }
                val type = if (mime.startsWith("image/")) "image" else "document"
                added.add(Attachment(type, mime, Base64.encodeToString(bytes, Base64.NO_WRAP), name))
            }
            withContext(Dispatchers.Main) {
                pendingAttachments.addAll(added)
                renderAttachmentBar()
            }
        }
    }

    /** 流式读取附件，读满 maxBytes 即止（返回的字节可能略超，交由调用方按大小拒绝），避免超大文件一次读入内存。 */
    private fun readLimited(uri: Uri, maxBytes: Int): ByteArray? {
        val stream = try { contentResolver.openInputStream(uri) } catch (_: Exception) { return null }
            ?: return null
        return stream.use { input ->
            val out = java.io.ByteArrayOutputStream()
            val buf = ByteArray(8192)
            var total = 0
            while (true) {
                val n = input.read(buf)
                if (n < 0) break
                total += n
                out.write(buf, 0, n)
                if (total >= maxBytes) break
            }
            out.toByteArray()
        }
    }

    /** 从 URI 元数据取文件名（OpenableColumns.DISPLAY_NAME），失败返回 null。 */
    private fun queryDisplayName(uri: Uri): String? = try {
        contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
            if (c.moveToFirst()) c.getString(0) else null
        }
    } catch (_: Exception) {
        null
    }

    private fun renderAttachmentBar() {
        attachmentBar.removeAllViews()
        attachmentBar.visibility = if (pendingAttachments.isEmpty()) View.GONE else View.VISIBLE
        for ((i, att) in pendingAttachments.withIndex()) {
            val chip = TextView(this)
            val label = att.name.ifBlank { att.mediaType }
            chip.text = (if (att.type == "image") "🖼️ " else "📄 ") + label + "  ✕"
            chip.setTextColor(ContextCompat.getColor(this, R.color.msg_text_primary))
            chip.setBackgroundResource(R.drawable.document_chip_bg)
            chip.setPadding(dp(10), dp(6), dp(10), dp(6))
            chip.setOnClickListener {
                if (i < pendingAttachments.size) {
                    pendingAttachments.removeAt(i)
                    renderAttachmentBar()
                }
            }
            val lp = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.WRAP_CONTENT,
                LinearLayout.LayoutParams.WRAP_CONTENT)
            lp.marginEnd = dp(6)
            attachmentBar.addView(chip, lp)
        }
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    private fun toggleConnection() {
        if (ws != null || RelayService.shouldReconnect) {
            RelayService.disconnect()
            stopService(Intent(this, RelayService::class.java))
            getSharedPreferences("claudeviewer", MODE_PRIVATE).edit()
                .putBoolean("connected", false).apply()
            connectBtn.text = getString(R.string.connect)
            setStatus(R.string.status_disconnected, R.color.status_idle)
            configPanel.visibility = View.VISIBLE
            inputBar.visibility = View.GONE
            return
        }
        val url = urlInput.text.toString().trim().trimEnd('/')
        if (url.isEmpty()) { toast(getString(R.string.input_url_required)); return }
        val token = tokenInput.text.toString().trim()
        getSharedPreferences("claudeviewer", MODE_PRIVATE).edit()
            .putString("ws_url", url)
            .putString("ws_token", token)
            .putBoolean("connected", true)
            .apply()
        doConnect(url, token)
    }

    /** 建立 WebSocket 连接（首次连接与自动重连共用）：配置服务并以前台服务方式启动。 */
    private fun doConnect(url: String, token: String) {
        setStatus(R.string.status_connecting, R.color.status_connecting)
        connectBtn.isEnabled = false
        configPanel.visibility = View.GONE
        RelayService.configure(url, token, this)
        requestNotificationPermissionIfNeeded()
        ContextCompat.startForegroundService(this, Intent(this, RelayService::class.java))
    }

    /** 从后台返回 / 主题切换重建后，若之前处于已连接状态但连接已丢则自动重连。 */
    private fun autoConnectIfNeeded() {
        // 服务已配置（shouldReconnect=true 表示正在连接或已连接，含前台服务尚未完成启动的窗口期）
        // 时不重复发起连接，避免与 doConnect 竞态产生重复 WebSocket 连接。
        if (RelayService.client != null || RelayService.shouldReconnect) return
        if (!getSharedPreferences("claudeviewer", MODE_PRIVATE).getBoolean("connected", false)) return
        val url = urlInput.text.toString().trim().trimEnd('/')
        if (url.isEmpty()) return
        doConnect(url, tokenInput.text.toString().trim())
    }

    /** Android 13+ 前台服务常驻通知需运行时授权；未授权仅影响通知展示，不阻断服务启动。 */
    private fun requestNotificationPermissionIfNeeded() {
        if (Build.VERSION.SDK_INT < 33) return
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED) return
        notificationPermissionLauncher.launch(Manifest.permission.POST_NOTIFICATIONS)
    }

    override fun onOpen() {
        runOnUiThread {
            connectBtn.text = getString(R.string.disconnect)
            connectBtn.isEnabled = true
            setStatus(R.string.status_connected, R.color.status_connected)
            inputBar.visibility = View.VISIBLE
            historyLoadedFor = null   // 重连后重新拉取当前会话历史，补齐断线间隙
            ws?.listSessions()
        }
    }

    override fun onSessions(sessions: List<SessionInfo>) {
        runOnUiThread {
            this.sessions.clear()
            this.sessions.addAll(sessions)
            rebuildSpinner()
            maybeSelectPending()
            reloadCurrentIfNeeded()
        }
    }

    /** 重连后 historyLoadedFor 被置空：若用户之前已选中某会话，则重新拉取第一页补齐断线间隙。 */
    private fun reloadCurrentIfNeeded() {
        val sid = selectedSessionId ?: return
        if (historyLoadedFor == sid) return
        val real = sessions.indexOfFirst { it.id == sid }
        if (real >= 0) loadSession(real + 1)
    }

    override fun onSessionStart(session: SessionInfo) {
        runOnUiThread {
            if (sessions.none { it.id == session.id }) sessions.add(session)
            rebuildSpinner()
            maybeSelectPending()
        }
    }

    override fun onSessionUpdate(session: SessionInfo) {
        runOnUiThread {
            val i = sessions.indexOfFirst { it.id == session.id }
            if (i >= 0) sessions[i] = session
            rebuildSpinner()
        }
    }

    override fun onSessionEnd(sessionId: String) {
        runOnUiThread {
            val i = sessions.indexOfFirst { it.id == sessionId }
            if (i >= 0) { sessions[i] = sessions[i].copy(status = "ended") }
            rebuildSpinner()
        }
    }

    override fun onMessages(sessionId: String, isHistory: Boolean, isUpdate: Boolean, messages: List<ViewMessage>, hasMore: Boolean) {
        runOnUiThread {
            // 只渲染当前选中会话；历史帧分页（首屏替换 / 上翻插头），update 按 idx 替换，其余按 idx 去重追加
            if (sessionId != selectedSessionId) return@runOnUiThread
            if (isHistory) {
                // 记录本页最旧 idx，作为向上翻页的 beforeIdx 游标
                val oldest = messages.mapNotNull { it.idx }.minOrNull()
                if (oldest != null && (oldestLoadedIdx == null || oldest < oldestLoadedIdx!!)) {
                    oldestLoadedIdx = oldest
                }
                if (loadingOlder) {
                    loadingOlder = false
                    hasMoreOlder = hasMore
                    prependHistory(messages)
                } else {
                    hasMoreOlder = hasMore
                    adapter.submit(messages, clear = true)
                    updateEmptyState()
                    if (messages.isNotEmpty()) maybeScrollToBottom(force = true)
                }
            } else if (isUpdate) {
                adapter.upsert(messages, isUpdate = true)
            } else {
                adapter.upsert(messages, isUpdate = false)
            }
            if (!isHistory) {
                updateEmptyState()
                if (messages.isNotEmpty()) maybeScrollToBottom(force = false)
            }
        }
    }

    override fun onClosing(code: Int, reason: String) {
        runOnUiThread {
            // 前台服务会自动重连；这里只提示「连接中」，保持「断开」按钮可用
            connectBtn.text = getString(R.string.disconnect)
            connectBtn.isEnabled = true
            setStatus(R.string.status_connecting, R.color.status_connecting)
        }
    }

    override fun onFailure(t: Throwable) {
        runOnUiThread {
            connectBtn.text = getString(R.string.disconnect)
            connectBtn.isEnabled = true
            setStatus(R.string.status_connecting, R.color.status_connecting)
            toast(getString(R.string.connect_failed, t.message ?: ""))
        }
    }

    override fun onInteraction(status: String, sessionId: String, message: String) {
        runOnUiThread {
            when (status) {
                "started" -> toast(getString(R.string.prompt_sent))
                "session" -> onInteractionSession(sessionId)
                "finished" -> {
                    onInteractionSession(sessionId)
                    toast(getString(R.string.interact_done))
                }
                "error" -> toast(getString(R.string.interact_error, message))
            }
        }
    }

    /** 交互回显到具体会话：仅在「新建中」或尚未选中会话时才切焦点；正在看其它会话时不打断（后台会话完成仅刷新列表状态）。 */
    private fun onInteractionSession(sessionId: String) {
        if (sessionId.isBlank()) return
        if (!newSessionMode && selectedSessionId != null) return
        newSessionMode = false
        preNewSessionId = null
        newSessionBanner.visibility = View.GONE
        // 直接选中新会话，不再依赖 listSessions 回环（修复 latch：回显丢失会导致每次命令都开新会话）
        val pos = sessions.indexOfFirst { it.id == sessionId }
        if (pos >= 0) {
            pendingSessionId = null
            sessionSpinner.setSelection(pos + 1)
        } else {
            pendingSessionId = sessionId
            ws?.listSessions()
        }
    }

    override fun onPermissionRequest(requestId: String, sessionId: String, toolName: String, displayName: String, description: String, input: String) {
        runOnUiThread {
            if (requestId.isBlank()) return@runOnUiThread
            permissionQueue.addLast(PermissionRequest(requestId, sessionId, toolName, description, input))
            showNextPermission()
        }
    }

    override fun onPermissionResponseError(requestId: String, message: String) {
        runOnUiThread { toast(getString(R.string.permission_unknown)) }
    }

    /** 一次只弹一个审批框，决策后出队下一个（并行工具调用会产生多个审批请求）。 */
    private fun showNextPermission() {
        if (permissionDialogShowing) return
        val req = permissionQueue.pollFirst() ?: return
        permissionDialogShowing = true
        val detail = buildString {
            if (req.description.isNotBlank()) append(req.description).append("\n\n")
            if (req.input.isNotBlank()) append(req.input.take(1200))
        }.trim()
        MaterialAlertDialogBuilder(this)
            .setTitle(getString(R.string.permission_title, req.toolName))
            .setMessage(detail.ifBlank { getString(R.string.permission_title, req.toolName) })
            .setPositiveButton(getString(R.string.permission_allow)) { _, _ ->
                ws?.sendPermissionResponse(req.requestId, true)
                permissionDialogShowing = false
                showNextPermission()
            }
            .setNegativeButton(getString(R.string.permission_deny)) { _, _ ->
                ws?.sendPermissionResponse(req.requestId, false, "Denied by user")
                toast(getString(R.string.permission_denied))
                permissionDialogShowing = false
                showNextPermission()
            }
            .setOnCancelListener {
                ws?.sendPermissionResponse(req.requestId, false, "Dismissed by user")
                permissionDialogShowing = false
                showNextPermission()
            }
            .show()
    }

    private fun rebuildSpinner() {
        val current = selectedSessionId
        // 首位是占位项“选择要查看的会话”，真实会话从位置 1 开始
        suppressSelection = true   // setAdapter/setSelection 都会触发 onItemSelected，一并抑制
        sessionSpinner.adapter = SessionAdapter(listOf(PLACEHOLDER) + sessions)
        val real = sessions.indexOfFirst { it.id == current }
        val pos = if (real >= 0) real + 1 else 0
        sessionSpinner.setSelection(pos)
        suppressSelection = false
        // 惰性加载：连接后不自动拉历史，等用户选中会话再现场加载
    }

    /** 惰性加载：用户选中某会话时只拉最近一页；更早记录靠向上翻页加载。 */
    private fun loadSession(pos: Int) {
        val real = pos - 1   // 去掉占位项
        if (real !in sessions.indices) {
            // 选中占位项：清空，不加载
            selectedSessionId = null
            historyLoadedFor = null
            oldestLoadedIdx = null
            hasMoreOlder = false
            loadingOlder = false
            adapter.submit(emptyList(), clear = true)
            updateEmptyState()
            return
        }
        val s = sessions[real]
        if (s.id != historyLoadedFor) {
            selectedSessionId = s.id
            historyLoadedFor = s.id
            oldestLoadedIdx = null
            hasMoreOlder = false
            loadingOlder = false
            getSharedPreferences("claudeviewer", MODE_PRIVATE).edit()
                .putString("last_session", s.id).apply()
            adapter.submit(emptyList(), clear = true)
            updateEmptyState()
            ws?.getHistory(s.id, limit = HISTORY_PAGE)
        }
    }

    /** 向上翻页：滚动到顶部且还有更早记录时，按页加载。 */
    private fun maybeLoadOlder() {
        if (!hasMoreOlder || loadingOlder || selectedSessionId == null) return
        if (recycler.canScrollVertically(-1)) return   // 尚未到顶
        loadingOlder = true
        ws?.getHistory(selectedSessionId!!, limit = HISTORY_PAGE, beforeIdx = oldestLoadedIdx)
    }

    /** 把更早的一页插到列表头部，并保持当前阅读位置不跳动。 */
    private fun prependHistory(messages: List<ViewMessage>) {
        val lm = recycler.layoutManager as? LinearLayoutManager ?: return
        val firstPos = lm.findFirstVisibleItemPosition()
        val offsetTop = lm.findViewByPosition(firstPos)?.top ?: 0
        val added = adapter.prepend(messages)
        if (added > 0) lm.scrollToPositionWithOffset(firstPos + added, offsetTop)
        updateEmptyState()
    }

    /** 仅当接近底部（或强制）时滚动到底，避免用户上翻阅读时被新消息打断。 */
    private fun maybeScrollToBottom(force: Boolean) {
        val lm = recycler.layoutManager as? LinearLayoutManager ?: return
        val nearBottom = lm.findLastVisibleItemPosition() >= adapter.itemCount - 3
        if (force || nearBottom) recycler.scrollToPosition(adapter.itemCount - 1)
    }

    private fun updateEmptyState() {
        val empty = adapter.itemCount == 0
        emptyView.visibility = if (empty) View.VISIBLE else View.GONE
        recycler.visibility = if (empty) View.GONE else View.VISIBLE
    }

    private fun setStatus(resId: Int, colorRes: Int) {
        statusText.setText(resId)
        val color = ContextCompat.getColor(this, colorRes)
        statusText.setTextColor(color)
        statusDot.backgroundTintList = ColorStateList.valueOf(color)
    }

    private fun statusColorRes(status: String): Int = when (status) {
        "active" -> R.color.status_active
        "attention" -> R.color.status_attention
        "ended" -> R.color.status_ended
        else -> R.color.status_idle
    }

    private fun sessionStatusLabel(status: String): String = when (status) {
        "active" -> getString(R.string.session_status_active)
        "attention" -> getString(R.string.session_status_attention)
        "ended" -> getString(R.string.session_status_ended)
        else -> getString(R.string.session_status_idle)
    }

    private fun toast(msg: String) = Toast.makeText(this, msg, Toast.LENGTH_SHORT).show()

    private fun launchScan() {
        scanLauncher.launch(
            ScanOptions()
                .setDesiredBarcodeFormats(ScanOptions.QR_CODE)
                .setPrompt(getString(R.string.scan_connect))
                .setBeepEnabled(true)
        )
    }

    private fun handleIntent(intent: Intent?) {
        val data = intent?.data ?: return
        applyConfigUri(data.toString())
    }

    private fun applyConfigUri(uriStr: String) {
        val uri = Uri.parse(uriStr)
        if (uri.scheme != "claudecontrol" || uri.host != "connect") {
            toast(getString(R.string.scan_unrecognized))
            return
        }
        val url = uri.getQueryParameter("url")
        if (url.isNullOrBlank()) {
            toast(getString(R.string.scan_unrecognized))
            return
        }
        val token = uri.getQueryParameter("token") ?: ""
        urlInput.setText(url)
        tokenInput.setText(token)
        getSharedPreferences("claudeviewer", MODE_PRIVATE).edit()
            .putString("ws_url", url)
            .putString("ws_token", token)
            .putBoolean("connected", true)
            .apply()
        toast(getString(R.string.scan_configured))
        // 扫码总是强制用新地址重连：当前若正连着一个过期隧道地址（旧域名 DNS 已失效，
        // 处于重连循环），也要先断开旧连接再切到新地址，否则会一直对旧域名报
        // “unable to resolve host”。旧逻辑 `if (ws == null)` 在重连循环中 ws 非空，导致不切新地址。
        RelayService.disconnect()
        doConnect(url, token)
    }

    private fun sendPrompt() {
        val w = ws ?: run { toast(getString(R.string.not_connected)); return }
        val text = promptInput.text.toString().trim()
        if (text.isEmpty() && pendingAttachments.isEmpty()) return
        // 新建模式下传空 sessionId，由服务端开新会话；否则 --resume 当前会话
        val sid = if (newSessionMode) "" else selectedSessionId
        w.sendPrompt(text, sid, pendingAttachments.toList())
        promptInput.setText("")
        pendingAttachments.clear()
        renderAttachmentBar()
        // banner 由 onInteractionSession 或 cancelNewSession 收起（新建会话被回显确认后才退出新模式）
        toast(getString(R.string.prompt_sent))
    }

    private fun maybeSelectPending() {
        val pid = pendingSessionId ?: return
        val pos = sessions.indexOfFirst { it.id == pid }
        if (pos >= 0 && selectedSessionId != pid) {
            pendingSessionId = null
            sessionSpinner.setSelection(pos + 1)
        }
    }

    /** 会话下拉：状态色点 + 标题 + 副标题（cwd/模型）。 */
    private inner class SessionAdapter(list: List<SessionInfo>) :
        ArrayAdapter<SessionInfo>(this, R.layout.item_session, list) {

        override fun getView(position: Int, convertView: View?, parent: ViewGroup): View =
            bind(convertView ?: inflate(parent), position)

        override fun getDropDownView(position: Int, convertView: View?, parent: ViewGroup): View =
            bind(convertView ?: inflate(parent), position)

        private fun inflate(parent: ViewGroup): View =
            LayoutInflater.from(context).inflate(R.layout.item_session, parent, false)

        private fun bind(v: View, position: Int): View {
            val s = getItem(position) ?: return v
            val isPlaceholder = s.id.isBlank()
            val colorRes = if (isPlaceholder) R.color.status_idle else statusColorRes(s.status)
            val color = ContextCompat.getColor(context, colorRes)
            v.findViewById<View>(R.id.session_dot).backgroundTintList = ColorStateList.valueOf(color)
            v.findViewById<TextView>(R.id.session_title).text = when {
                isPlaceholder -> getString(R.string.select_session_hint)
                s.title.isNotBlank() -> s.title
                else -> getString(R.string.unnamed_session)
            }
            val sub = v.findViewById<TextView>(R.id.session_subtitle)
            if (isPlaceholder) {
                sub.visibility = View.GONE
            } else {
                val secondary = s.cwd.ifBlank { s.model }
                if (secondary.isNotBlank()) {
                    sub.text = secondary
                    sub.visibility = View.VISIBLE
                } else {
                    sub.visibility = View.GONE
                }
            }
            // 右侧状态标签（活跃/待审批/空闲/已结束），与左侧状态点同色
            val statusLbl = v.findViewById<TextView>(R.id.session_status)
            if (isPlaceholder) {
                statusLbl.visibility = View.GONE
            } else {
                statusLbl.visibility = View.VISIBLE
                statusLbl.text = sessionStatusLabel(s.status)
                statusLbl.setTextColor(color)
            }
            return v
        }
    }
}
