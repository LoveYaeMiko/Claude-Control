package com.example.claudeviewer

import android.content.Intent
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Bundle
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

    private val adapter = MessageAdapter()
    private var ws: WebSocketClient? = null
    private val sessions = mutableListOf<SessionInfo>()
    private var selectedSessionId: String? = null
    private var historyLoadedFor: String? = null
    private var pendingSessionId: String? = null
    // 分页状态：向上翻找时按页加载更早记录
    private var hasMoreOlder = false
    private var loadingOlder = false
    private var oldestLoadedIdx: Int? = null
    private var suppressSelection = false
    // 多模态附件 + 权限审批
    private val pendingAttachments = mutableListOf<Attachment>()
    private val permissionQueue = java.util.ArrayDeque<PermissionRequest>()
    private var permissionDialogShowing = false

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
    }

    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let { applyConfigUri(it) }
    }

    private val attachLauncher = registerForActivityResult(ActivityResultContracts.OpenMultipleDocuments()) { uris ->
        onAttachmentsPicked(uris)
    }

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

        handleIntent(intent)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        handleIntent(intent)
    }

    override fun onDestroy() {
        ws?.close()
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

    /** 清空当前会话选择，下一条 prompt 将开新会话（无 --resume）。 */
    private fun newSession() {
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
        toast(getString(R.string.new_session_hint))
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
        attachLauncher.launch(arrayOf(
            "image/jpeg", "image/png", "image/gif", "image/webp", "application/pdf"))
    }

    private fun onAttachmentsPicked(uris: List<Uri>) {
        for (uri in uris) {
            if (pendingAttachments.size >= MAX_ATTACHMENTS) {
                toast(getString(R.string.attachment_max))
                break
            }
            val mime = contentResolver.getType(uri)
            if (mime == null || !(mime.startsWith("image/") || mime == "application/pdf")) {
                toast(getString(R.string.attachment_unsupported))
                continue
            }
            val bytes = try {
                contentResolver.openInputStream(uri)?.use { it.readBytes() }
            } catch (_: Exception) { null }
            if (bytes == null) { toast(getString(R.string.attachment_read_failed)); continue }
            if (bytes.size > MAX_ATTACHMENT_BYTES) { toast(getString(R.string.attachment_too_large)); continue }
            val type = if (mime.startsWith("image/")) "image" else "document"
            pendingAttachments.add(Attachment(type, mime, Base64.encodeToString(bytes, Base64.NO_WRAP)))
        }
        renderAttachmentBar()
    }

    private fun renderAttachmentBar() {
        attachmentBar.removeAllViews()
        attachmentBar.visibility = if (pendingAttachments.isEmpty()) View.GONE else View.VISIBLE
        for ((i, att) in pendingAttachments.withIndex()) {
            val chip = TextView(this)
            chip.text = (if (att.type == "image") "🖼️ " else "📄 ") + att.mediaType + "  ✕"
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
        if (ws != null) {
            ws?.close()
            ws = null
            connectBtn.text = getString(R.string.connect)
            setStatus(R.string.status_disconnected, R.color.status_idle)
            configPanel.visibility = View.VISIBLE
            return
        }
        val url = urlInput.text.toString().trim().trimEnd('/')
        if (url.isEmpty()) { toast(getString(R.string.input_url_required)); return }
        getSharedPreferences("claudeviewer", MODE_PRIVATE).edit()
            .putString("ws_url", url)
            .putString("ws_token", tokenInput.text.toString().trim())
            .apply()

        setStatus(R.string.status_connecting, R.color.status_connecting)
        connectBtn.isEnabled = false
        configPanel.visibility = View.GONE
        ws = WebSocketClient(url, tokenInput.text.toString().trim())
        ws?.connect(this)
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
            if (ws != null) {
                ws = null
                connectBtn.text = getString(R.string.connect)
                connectBtn.isEnabled = true
                setStatus(R.string.status_disconnected, R.color.status_idle)
                configPanel.visibility = View.VISIBLE
                inputBar.visibility = View.GONE
            }
        }
    }

    override fun onFailure(t: Throwable) {
        runOnUiThread {
            ws = null
            connectBtn.text = getString(R.string.connect)
            connectBtn.isEnabled = true
            setStatus(R.string.status_failed, R.color.status_failed)
            configPanel.visibility = View.VISIBLE
            inputBar.visibility = View.GONE
            toast(getString(R.string.connect_failed, t.message ?: ""))
        }
    }

    override fun onInteraction(status: String, sessionId: String, message: String) {
        runOnUiThread {
            when (status) {
                "started" -> toast(getString(R.string.prompt_sent))
                "session" -> selectOrWait(sessionId)
                "finished" -> {
                    selectOrWait(sessionId)
                    toast(getString(R.string.interact_done))
                }
                "error" -> toast(getString(R.string.interact_error, message))
            }
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
        urlInput.setText(url)
        tokenInput.setText(uri.getQueryParameter("token") ?: "")
        toast(getString(R.string.scan_configured))
        if (ws == null) toggleConnection()
    }

    private fun sendPrompt() {
        val w = ws ?: run { toast(getString(R.string.not_connected)); return }
        val text = promptInput.text.toString().trim()
        if (text.isEmpty() && pendingAttachments.isEmpty()) return
        w.sendPrompt(text, selectedSessionId, pendingAttachments.toList())
        promptInput.setText("")
        pendingAttachments.clear()
        renderAttachmentBar()
        toast(getString(R.string.prompt_sent))
    }

    private fun selectOrWait(sessionId: String) {
        if (sessionId.isBlank()) return
        val pos = sessions.indexOfFirst { it.id == sessionId }
        if (pos >= 0) {
            pendingSessionId = null
            if (selectedSessionId != sessionId) sessionSpinner.setSelection(pos + 1)
        } else {
            pendingSessionId = sessionId
            ws?.listSessions()
        }
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
            return v
        }
    }
}
