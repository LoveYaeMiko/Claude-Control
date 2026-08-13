package com.example.claudeviewer

import android.graphics.Typeface
import android.text.SpannableString
import android.text.Spanned
import android.text.style.BackgroundColorSpan
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView
import com.google.android.material.card.MaterialCardView
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * RecyclerView 适配器：每条记录一张卡片，内容块在卡片内以 TextView 追加渲染。
 * 思考 / 工具调用折叠为可点击展开的摘要行；折叠态跨重绘保留；超长文本截断防 ANR。
 */
class MessageAdapter : RecyclerView.Adapter<MessageAdapter.VH>() {

    private val items = mutableListOf<ViewMessage>()
    private val seenIdxs = mutableSetOf<Int>()
    private val expandedKeys = mutableSetOf<String>()
    private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    companion object {
        private const val COLLAPSED_PREVIEW = 60
        private const val MAX_EXPANDED_CHARS = 4000
    }

    fun submit(list: List<ViewMessage>, clear: Boolean) {
        if (clear) { items.clear(); seenIdxs.clear(); expandedKeys.clear() }
        list.forEach { appendRecord(it) }
        notifyDataSetChanged()
    }

    /** 实时增量：按 idx 去重；isUpdate=true 时按 idx 替换（tool_result 补全）。 */
    fun upsert(list: List<ViewMessage>, isUpdate: Boolean) {
        for (m in list) {
            if (isUpdate) {
                val pos = m.idx?.let { i -> items.indexOfFirst { it.idx == i } } ?: -1
                if (pos >= 0) items[pos] = m else appendRecord(m)
            } else {
                if (m.idx != null && m.idx in seenIdxs) continue
                appendRecord(m)
            }
        }
        notifyDataSetChanged()
    }

    private fun appendRecord(m: ViewMessage) {
        m.idx?.let { seenIdxs.add(it) }
        items.add(m)
    }

    override fun getItemCount() = items.size

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): VH {
        val v = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_message, parent, false)
        return VH(v)
    }

    override fun onBindViewHolder(holder: VH, position: Int) {
        holder.bind(items[position])
    }

    inner class VH(v: View) : RecyclerView.ViewHolder(v) {
        private val card: MaterialCardView = v.findViewById(R.id.msg_card)
        private val accent: View = v.findViewById(R.id.msg_accent)
        private val header: TextView = v.findViewById(R.id.msg_header)
        private val body: LinearLayout = v.findViewById(R.id.msg_body)
        private val ctx = v.context

        fun bind(m: ViewMessage) {
            accent.setBackgroundColor(ContextCompat.getColor(ctx, roleColorRes(m.role)))
            card.setCardBackgroundColor(
                ContextCompat.getColor(ctx, if (m.role == "user") R.color.msg_user_bg else R.color.surface)
            )
            header.text = buildString {
                append(roleLabel(m.role))
                if (m.model.isNotBlank()) append("  ·  ").append(m.model)
                if (m.ts.isNotBlank()) {
                    val t = fmtTime(m.ts)
                    if (t.isNotBlank()) append("  ·  ").append(t)
                }
            }

            body.removeAllViews()
            if (m.role == "system") {
                body.addView(makeText(m.sysText, isMono = false, dim = true))
                return
            }
            var blockIndex = 0
            for (b in m.content) {
                when (b.type) {
                    "text" -> if (b.text.isNotBlank())
                        body.addView(makeMarkdownText(b.text))
                    "thinking" -> if (b.thinking.isNotBlank())
                        body.addView(makeCollapsible(ctx.getString(R.string.thinking_label), b.thinking, dim = true, key = key(m, blockIndex)))
                    "tool_use" -> body.addView(makeCollapsible(ctx.getString(R.string.tool_label, b.name), b.input, dim = true, key = key(m, blockIndex)))
                    "tool_result" -> if (b.result.isNotBlank()) {
                        val summary = b.result.take(120) + if (b.result.length > 120) "…" else ""
                        body.addView(makeText(summary, isMono = true, dim = true, isError = b.isError))
                    }
                }
                blockIndex++
            }
            if (body.childCount == 0) {
                body.addView(makeText(ctx.getString(R.string.blank_block), isMono = false, dim = true))
            }
        }

        private fun key(m: ViewMessage, blockIndex: Int) = "${m.idx ?: m.ts}:$blockIndex"

        // 服务端 ts 为 epoch 秒（浮点/整型）
        private fun fmtTime(ts: String): String {
            val epochMillis = try { (ts.toDouble() * 1000).toLong() } catch (_: Exception) { return "" }
            if (epochMillis <= 0) return ""
            return timeFmt.format(Date(epochMillis))
        }

        private fun roleLabel(role: String): String = when (role) {
            "user" -> ctx.getString(R.string.role_user)
            "assistant" -> ctx.getString(R.string.role_assistant)
            else -> ctx.getString(R.string.role_system)
        }

        private fun roleColorRes(role: String): Int = when (role) {
            "user" -> R.color.role_user
            "assistant" -> R.color.role_assistant
            else -> R.color.role_system
        }

        private fun makeText(text: String, isMono: Boolean, dim: Boolean = false, isError: Boolean = false): TextView {
            val tv = TextView(ctx)
            tv.text = truncate(text)
            tv.setTextIsSelectable(true)
            tv.setTextColor(ContextCompat.getColor(ctx, when {
                isError -> R.color.msg_text_error
                dim -> R.color.msg_text_dim
                else -> R.color.msg_text_primary
            }))
            tv.textSize = 14f
            if (isMono) tv.typeface = Typeface.MONOSPACE
            tv.setPadding(dp(2), dp(3), dp(2), dp(3))
            return tv
        }

        /** 用 Markwon 把 Markdown 文本渲染为富文本（标题/加粗/列表/表格/代码块）。 */
        private fun makeMarkdownText(text: String): TextView {
            val tv = TextView(ctx)
            tv.setTextColor(ContextCompat.getColor(ctx, R.color.msg_text_primary))
            tv.textSize = 15f
            tv.setPadding(dp(2), dp(4), dp(2), dp(4))
            Markdown.get(ctx).setMarkdown(tv, truncate(text))
            tv.setTextIsSelectable(true)
            return tv
        }

        private fun makeCollapsible(label: String, content: String, dim: Boolean, key: String): TextView {
            val tv = TextView(ctx)
            tv.setTextColor(ContextCompat.getColor(ctx, if (dim) R.color.msg_text_muted else R.color.msg_label))
            tv.textSize = 12f
            tv.typeface = Typeface.MONOSPACE
            tv.setPadding(dp(2), dp(3), dp(2), dp(3))
            val isExpanded = key in expandedKeys
            tv.text = collapsibleText(label, content, isExpanded)
            tv.setOnClickListener {
                val nowExpanded = key !in expandedKeys
                if (nowExpanded) expandedKeys.add(key) else expandedKeys.remove(key)
                tv.text = collapsibleText(label, content, nowExpanded)
            }
            return tv
        }

        private fun collapsibleText(label: String, content: String, expanded: Boolean): CharSequence {
            return if (expanded) {
                val span = SpannableString(truncate(content))
                span.setSpan(
                    BackgroundColorSpan(ContextCompat.getColor(ctx, R.color.msg_highlight)),
                    0, span.length, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE
                )
                span
            } else {
                "$label  ·  ${content.replace('\n', ' ').take(COLLAPSED_PREVIEW)}${ctx.getString(R.string.tap_expand)}"
            }
        }

        private fun truncate(s: String): String =
            if (s.length <= MAX_EXPANDED_CHARS) s
            else s.substring(0, MAX_EXPANDED_CHARS) + ctx.getString(R.string.truncated)

        private fun dp(v: Int): Int =
            (v * ctx.resources.displayMetrics.density).toInt()
    }
}
