package com.example.claudeviewer

import android.content.Intent
import android.content.res.ColorStateList
import android.net.Uri
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.widget.AppCompatSpinner
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.button.MaterialButton
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

    companion object {
        private const val HISTORY_PAGE = 50
        // 占位项：spinner 首位“请选择会话”，选中它不加载任何历史
        private val PLACEHOLDER = SessionInfo(id = "", cwd = "", title = "", status = "", model = "")
    }

    private val scanLauncher = registerForActivityResult(ScanContract()) { result ->
        result.contents?.let { applyConfigUri(it) }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

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

        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = adapter
        recycler.addOnScrollListener(object : RecyclerView.OnScrollListener() {
            override fun onScrolled(rv: RecyclerView, dx: Int, dy: Int) {
                if (dy < 0) maybeLoadOlder()   // 向上翻找时按需加载更早记录
            }
        })

        val prefs = getSharedPreferences("claudeviewer", MODE_PRIVATE)
        urlInput.setText(prefs.getString("ws_url", "ws://192.168.1.38:9876"))
        tokenInput.setText(prefs.getString("ws_token", ""))

        connectBtn.setOnClickListener { toggleConnection() }
        scanBtn.setOnClickListener { launchScan() }
        sendBtn.setOnClickListener { sendPrompt() }
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
        if (text.isEmpty()) return
        w.sendPrompt(text, selectedSessionId)
        promptInput.setText("")
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
