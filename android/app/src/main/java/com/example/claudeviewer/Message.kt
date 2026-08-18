package com.example.claudeviewer

import org.json.JSONObject

/**
 * 与电脑端 relay_server.py 协议一致的消息模型（org.json 解析，无额外依赖）。
 */
data class ContentBlock(
    val type: String,          // text | thinking | tool_use | tool_result | image | document
    val text: String = "",
    val thinking: String = "",
    val name: String = "",
    val input: String = "",
    val result: String = "",
    val isError: Boolean = false,
    val mediaType: String = "",   // image/document 块的 MIME 类型
    val data: String = "",        // image/document 块的 base64 数据
)

/** 手机端待发送的附件（图片/PDF），data 为无换行 base64。 */
data class Attachment(
    val type: String,          // "image" | "document"
    val mediaType: String,
    val data: String,
)

data class SessionInfo(
    val id: String,
    val cwd: String,
    val title: String,
    val status: String,
    val model: String,
)

data class ViewMessage(
    val idx: Int? = null,       // 服务端每会话单调递增的序号（用于去重/更新）
    val role: String,           // user | assistant | system
    val model: String,
    val ts: String,
    val content: List<ContentBlock>,
    val sysText: String = "",
)

object Protocol {

    fun parseSessions(obj: JSONObject): List<SessionInfo> {
        val arr = obj.optJSONArray("sessions") ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            arr.optJSONObject(i)?.let { parseSession(it) }
        }
    }

    fun parseSession(s: JSONObject): SessionInfo = SessionInfo(
        id = s.optString("id"),
        cwd = s.optString("cwd"),
        title = s.optString("title"),
        status = s.optString("status"),
        model = s.optString("model"),
    )

    fun parseMessages(obj: JSONObject): List<ViewMessage> {
        val arr = obj.optJSONArray("records") ?: return emptyList()
        return (0 until arr.length()).mapNotNull { i ->
            val r = arr.optJSONObject(i) ?: return@mapNotNull null
            val idx = r.optInt("idx", -1).takeIf { it >= 0 }
            if (r.optString("kind") == "sys") {
                return@mapNotNull ViewMessage(idx = idx, role = "system", model = "", ts = r.optString("ts"), content = emptyList(), sysText = r.optString("text"))
            }
            val role = r.optString("role")
            val content = r.optJSONArray("content") ?: return@mapNotNull null
            val blocks = (0 until content.length()).mapNotNull { j ->
                val b = content.optJSONObject(j) ?: return@mapNotNull null
                when (b.optString("type")) {
                    "text" -> ContentBlock("text", text = b.optString("text"))
                    "thinking" -> ContentBlock("thinking", thinking = b.optString("thinking"))
                    "tool_use" -> ContentBlock("tool_use", name = b.optString("name"), input = b.opt("input").toString(), result = b.optString("result"), isError = b.optBoolean("isError"))
                    "tool_result" -> ContentBlock("tool_result", result = b.optString("result"), isError = b.optBoolean("isError"))
                    "image" -> ContentBlock("image", mediaType = b.optString("mediaType"), data = b.optString("data"))
                    "document" -> ContentBlock("document", mediaType = b.optString("mediaType"), name = b.optString("name"), data = b.optString("data"))
                    else -> null
                }
            }
            ViewMessage(idx = idx, role = role, model = r.optString("model"), ts = r.optString("ts"), content = blocks)
        }
    }
}
