package com.example.claudeviewer

import android.content.res.ColorStateList
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

    private val adapter = MessageAdapter()
    private var ws: WebSocketClient? = null
    private val sessions = mutableListOf<SessionInfo>()
    private var selectedSessionId: String? = null
    private var historyLoadedFor: String? = null

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

        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = adapter

        val prefs = getSharedPreferences("claudeviewer", MODE_PRIVATE)
        urlInput.setText(prefs.getString("ws_url", "ws://192.168.1.38:9876"))
        tokenInput.setText(prefs.getString("ws_token", ""))

        connectBtn.setOnClickListener { toggleConnection() }
        configToggle.setOnClickListener {
            val show = configPanel.visibility != View.VISIBLE
            configPanel.visibility = if (show) View.VISIBLE else View.GONE
        }

        sessionSpinner.onItemSelectedListener = object : android.widget.AdapterView.OnItemSelectedListener {
            override fun onItemSelected(parent: android.widget.AdapterView<*>?, v: View?, pos: Int, id: Long) {
                loadSession(pos)
            }
            override fun onNothingSelected(parent: android.widget.AdapterView<*>?) {}
        }
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
            historyLoadedFor = null   // 重连后重新拉取当前会话历史，补齐断线间隙
            ws?.listSessions()
        }
    }

    override fun onSessions(sessions: List<SessionInfo>) {
        runOnUiThread {
            this.sessions.clear()
            this.sessions.addAll(sessions)
            rebuildSpinner()
        }
    }

    override fun onSessionStart(session: SessionInfo) {
        runOnUiThread {
            if (sessions.none { it.id == session.id }) sessions.add(session)
            rebuildSpinner()
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

    override fun onMessages(sessionId: String, isHistory: Boolean, isUpdate: Boolean, messages: List<ViewMessage>) {
        runOnUiThread {
            // 只渲染当前选中会话；历史帧整批替换，update 帧按 idx 替换，其余按 idx 去重追加
            if (sessionId != selectedSessionId) return@runOnUiThread
            if (isHistory) {
                adapter.submit(messages, clear = true)
            } else if (isUpdate) {
                adapter.upsert(messages, isUpdate = true)
            } else {
                adapter.upsert(messages, isUpdate = false)
            }
            updateEmptyState()
            if (messages.isNotEmpty()) maybeScrollToBottom(force = isHistory)
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
            toast(getString(R.string.connect_failed, t.message ?: ""))
        }
    }

    private fun rebuildSpinner() {
        val current = selectedSessionId
        sessionSpinner.adapter = SessionAdapter(sessions.toList())
        val pos = sessions.indexOfFirst { it.id == current }.takeIf { it >= 0 } ?: 0
        if (sessions.isNotEmpty()) {
            sessionSpinner.setSelection(pos)
            loadSession(pos)   // 初始自动选中不触发 onItemSelected，需显式加载
        }
    }

    private fun loadSession(pos: Int) {
        if (pos !in sessions.indices) return
        val s = sessions[pos]
        if (s.id != historyLoadedFor) {
            selectedSessionId = s.id
            historyLoadedFor = s.id
            adapter.submit(emptyList(), clear = true)
            updateEmptyState()
            ws?.getHistory(s.id)
        }
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
            val color = ContextCompat.getColor(context, statusColorRes(s.status))
            v.findViewById<View>(R.id.session_dot).backgroundTintList = ColorStateList.valueOf(color)
            v.findViewById<TextView>(R.id.session_title).text =
                if (s.title.isNotBlank()) s.title else getString(R.string.unnamed_session)
            val sub = v.findViewById<TextView>(R.id.session_subtitle)
            val secondary = s.cwd.ifBlank { s.model }
            if (secondary.isNotBlank()) {
                sub.text = secondary
                sub.visibility = View.VISIBLE
            } else {
                sub.visibility = View.GONE
            }
            return v
        }
    }
}
