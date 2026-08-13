package com.example.claudeviewer

import android.content.Context
import androidx.core.content.ContextCompat
import io.noties.markwon.AbstractMarkwonPlugin
import io.noties.markwon.Markwon
import io.noties.markwon.core.CorePlugin
import io.noties.markwon.core.MarkwonTheme
import io.noties.markwon.ext.strikethrough.StrikethroughPlugin
import io.noties.markwon.ext.tables.TablePlugin

/**
 * 全应用共享的 Markwon 实例，按深色主题配色。
 * 用于把 Claude Code 转录中的 Markdown 文本渲染为富文本（标题/加粗/列表/表格/代码块）。
 */
object Markdown {
    @Volatile
    private var instance: Markwon? = null

    fun get(context: Context): Markwon {
        instance?.let { return it }
        synchronized(this) {
            instance?.let { return it }
            val c = context.applicationContext
            val markwon = Markwon.builder(c)
                .usePlugin(object : AbstractMarkwonPlugin() {
                    override fun configureTheme(builder: MarkwonTheme.Builder) {
                        builder
                            .linkColor(ContextCompat.getColor(c, R.color.primary))
                            .codeTextColor(ContextCompat.getColor(c, R.color.msg_label))
                            .codeBackgroundColor(ContextCompat.getColor(c, R.color.surface_variant))
                            .codeBlockTextColor(ContextCompat.getColor(c, R.color.msg_text_primary))
                            .codeBlockBackgroundColor(ContextCompat.getColor(c, R.color.surface_variant))
                            .blockQuoteColor(ContextCompat.getColor(c, R.color.msg_text_dim))
                    }
                })
                .usePlugin(CorePlugin.create())
                .usePlugin(TablePlugin.create(c))
                .usePlugin(StrikethroughPlugin.create())
                .build()
            instance = markwon
            return markwon
        }
    }
}
