package com.example.claudeviewer

import android.content.res.ColorStateList
import android.graphics.Typeface
import android.text.SpannableString
import android.text.Spanned
import android.text.style.BackgroundColorSpan
import android.util.Base64
import android.view.Gravity
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.ImageView
import android.widget.LinearLayout
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.recyclerview.widget.RecyclerView
import coil.load
import com.google.android.material.card.MaterialCardView
import java.text.SimpleDateFormat
import java.util.Calendar
import java.util.Date
import java.util.Locale

/**
 * RecyclerView 适配器：气泡式消息列表。
 * 每条记录一张气泡（user 靠右、assistant/system 靠左，带头像）；思考 / 工具调用
 * 折叠为可点击展开的摘要行；图片经 Coil 渲染、PDF 显示文档 chip；按时间间隔插分隔条。
 */
class MessageAdapter : RecyclerView.Adapter<MessageAdapter.VH>() {

    private val items = mutableListOf<ViewMessage>()
    private val seenIdxs = mutableSetOf<Int>()
    private val expandedKeys = mutableSetOf<String>()

    companion object {
        private const val COLLAPSED_PREVIEW = 60
        private const val MAX_EXPANDED_CHARS = 4000
        private const val DIVIDER_GAP_MS = 5 * 60 * 1000L   // 两条消息间隔超 5 分钟插入时间分隔
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

    /** 向上翻页：把更早的记录（idx 升序）插到列表头部，返回实际插入条数。 */
    fun prepend(list: List<ViewMessage>): Int {
        val toAdd = list.filter { m -> m.idx == null || m.idx !in seenIdxs }
        toAdd.forEach { m -> m.idx?.let { seenIdxs.add(it) } }
        if (toAdd.isEmpty()) return 0
        items.addAll(0, toAdd)
        notifyItemRangeInserted(0, toAdd.size)
        return toAdd.size
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
        holder.bind(items[position], position)
    }

    inner class VH(v: View) : RecyclerView.ViewHolder(v) {
        private val root: LinearLayout = v.findViewById(R.id.msg_root)
        private val row: LinearLayout = v.findViewById(R.id.msg_row)
        private val timeDivider: TextView = v.findViewById(R.id.msg_time_divider)
        private val avatarLeft: TextView = v.findViewById(R.id.avatar_left)
        private val avatarRight: TextView = v.findViewById(R.id.avatar_right)
        private val card: MaterialCardView = v.findViewById(R.id.msg_card)
        private val header: TextView = v.findViewById(R.id.msg_header)
        private val body: LinearLayout = v.findViewById(R.id.msg_body)
        private val ctx = v.context
        private val maxTextWidth = (ctx.resources.displayMetrics.widthPixels * 0.78f).toInt()
        private val timeFmt = SimpleDateFormat("HH:mm:ss", Locale.getDefault())
        private val dividerHmFmt = SimpleDateFormat("HH:mm", Locale.getDefault())
        private val dividerDayFmt = SimpleDateFormat("MM-dd HH:mm", Locale.getDefault())

        fun bind(m: ViewMessage, position: Int) {
            val isUser = m.role == "user"

            // 气泡对齐 + 头像
            row.gravity = if (isUser) Gravity.END else Gravity.START
            card.setCardBackgroundColor(
                ContextCompat.getColor(ctx, if (isUser) R.color.msg_user_bg else R.color.surface)
            )
            val (emoji, bgRes, fgRes) = avatarStyle(m.role)
            if (isUser) {
                avatarRight.visibility = View.VISIBLE
                avatarLeft.visibility = View.GONE
                styleAvatar(avatarRight, emoji, bgRes, fgRes)
            } else {
                avatarLeft.visibility = View.VISIBLE
                avatarRight.visibility = View.GONE
                styleAvatar(avatarLeft, emoji, bgRes, fgRes)
            }

            bindTimeDivider(m, position)

            val headerParts = mutableListOf<String>()
            if (m.model.isNotBlank()) headerParts.add(m.model)
            val t = fmtTime(m.ts)
            if (t.isNotBlank()) headerParts.add(t)
            header.text = headerParts.joinToString("  ·  ")
            header.visibility = if (headerParts.isEmpty() || m.role == "system") View.GONE else View.VISIBLE

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
                    "image" -> if (b.data.isNotBlank()) body.addView(makeImage(b.data))
                    "document" -> body.addView(makeDocument(b.name))
                }
                blockIndex++
            }
            if (body.childCount == 0) {
                body.addView(makeText(ctx.getString(R.string.blank_block), isMono = false, dim = true))
            }
        }

        private fun avatarStyle(role: String): Triple<String, Int, Int> = when (role) {
            "user" -> Triple("👤", R.color.avatar_user_bg, R.color.avatar_user_fg)
            "assistant" -> Triple("🤖", R.color.avatar_assistant_bg, R.color.avatar_assistant_fg)
            else -> Triple("⚙️", R.color.avatar_system_bg, R.color.avatar_system_fg)
        }

        private fun styleAvatar(avatar: TextView, emoji: String, bgRes: Int, fgRes: Int) {
            avatar.text = emoji
            avatar.backgroundTintList = ColorStateList.valueOf(ContextCompat.getColor(ctx, bgRes))
            avatar.setTextColor(ContextCompat.getColor(ctx, fgRes))
        }

        private fun bindTimeDivider(m: ViewMessage, position: Int) {
            val ts = parseEpochMillis(m.ts)
            val prev = if (position > 0) parseEpochMillis(items[position - 1].ts) else -1L
            val show = ts > 0 && (prev < 0 || ts - prev > DIVIDER_GAP_MS)
            if (show) {
                timeDivider.text = fmtDividerTime(m.ts)
                timeDivider.visibility = View.VISIBLE
            } else {
                timeDivider.visibility = View.GONE
            }
        }

        private fun key(m: ViewMessage, blockIndex: Int) = "${m.idx ?: m.ts}:$blockIndex"

        // 服务端 ts 为 epoch 秒（浮点/整型）
        private fun parseEpochMillis(ts: String): Long =
            try { (ts.toDouble() * 1000).toLong() } catch (_: Exception) { -1L }

        private fun fmtTime(ts: String): String {
            val millis = parseEpochMillis(ts)
            if (millis <= 0) return ""
            return timeFmt.format(Date(millis))
        }

        private fun fmtDividerTime(ts: String): String {
            val millis = parseEpochMillis(ts)
            if (millis <= 0) return ""
            val d = Date(millis)
            val cal = Calendar.getInstance()
            val today = Calendar.getInstance()
            cal.time = d
            val sameDay = cal.get(Calendar.YEAR) == today.get(Calendar.YEAR) &&
                cal.get(Calendar.DAY_OF_YEAR) == today.get(Calendar.DAY_OF_YEAR)
            return if (sameDay) dividerHmFmt.format(d) else dividerDayFmt.format(d)
        }

        private fun makeText(text: String, isMono: Boolean, dim: Boolean = false, isError: Boolean = false): TextView {
            val tv = TextView(ctx)
            tv.text = truncate(text)
            tv.setTextIsSelectable(true)
            tv.maxWidth = maxTextWidth
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
            tv.maxWidth = maxTextWidth
            tv.setPadding(dp(2), dp(4), dp(2), dp(4))
            Markdown.get(ctx).setMarkdown(tv, truncate(text))
            tv.setTextIsSelectable(true)
            return tv
        }

        private fun makeImage(base64: String): View {
            val bytes = try { Base64.decode(base64, Base64.NO_WRAP) } catch (_: Exception) { null }
            if (bytes == null || bytes.isEmpty()) {
                val tv = TextView(ctx)
                tv.text = ctx.getString(R.string.blank_block)
                tv.setTextColor(ContextCompat.getColor(ctx, R.color.msg_text_dim))
                return tv
            }
            val iv = ImageView(ctx)
            iv.adjustViewBounds = true
            iv.scaleType = ImageView.ScaleType.FIT_CENTER
            iv.layoutParams = LinearLayout.LayoutParams(minOf(dp(240), maxTextWidth), ViewGroup.LayoutParams.WRAP_CONTENT)
            iv.setPadding(dp(2), dp(4), dp(2), dp(4))
            iv.load(bytes)
            return iv
        }

        private fun makeDocument(name: String): TextView {
            val tv = TextView(ctx)
            tv.text = "${ctx.getString(R.string.document_label)} ${name.ifBlank { "PDF" }}"
            tv.setTextColor(ContextCompat.getColor(ctx, R.color.msg_text_primary))
            tv.textSize = 13f
            tv.setBackgroundResource(R.drawable.document_chip_bg)
            tv.setPadding(dp(12), dp(8), dp(12), dp(8))
            return tv
        }

        private fun makeCollapsible(label: String, content: String, dim: Boolean, key: String): TextView {
            val tv = TextView(ctx)
            tv.setTextColor(ContextCompat.getColor(ctx, if (dim) R.color.msg_text_muted else R.color.msg_label))
            tv.textSize = 12f
            tv.typeface = Typeface.MONOSPACE
            tv.maxWidth = maxTextWidth
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
