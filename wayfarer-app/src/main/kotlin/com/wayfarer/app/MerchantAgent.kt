package com.wayfarer.app

import com.wayfarer.tap.MenuItem
import com.wayfarer.tap.OrderPricer
import com.wayfarer.tap.PricedOrder
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL

/**
 * The merchant's till agent. The shopkeeper says or types what the customer wants; a model on NEAR AI Cloud works out
 * which menu items and how many. The agent never sees or sets a price: [OrderPricer] computes the total from the shop's
 * own menu, and the merchant confirms before a request is created. The agent cannot move money. Only the order text is
 * sent, never any buyer data. The key is the merchant's own, entered in Setup (prototype, stored on the device).
 */
object MerchantAgent {
    val DEMO_MENU = listOf(
        MenuItem("Coffee", 3), MenuItem("Sandwich", 4), MenuItem("Pastry", 2), MenuItem("Bottled water", 1),
    )
    const val DEFAULT_BASE_URL = "https://cloud-api.near.ai/v1"
    const val DEFAULT_MODEL = "z-ai/glm-5.3-flash"

    class AgentException(message: String) : Exception(message)

    /** Blocking. Call off the main thread. */
    fun price(order: String, menu: List<MenuItem>, apiKey: String, baseUrl: String = DEFAULT_BASE_URL, model: String = DEFAULT_MODEL): PricedOrder {
        if (apiKey.isBlank()) throw AgentException("Add your NEAR AI key in Setup to use the agent.")
        val names = menu.joinToString(", ") { it.name }
        val system = "You are a till assistant for a cafe. The menu items are exactly: $names. Convert the customer's order " +
            "into JSON only, no other text: {\"lines\":[{\"item\":\"<exact menu name>\",\"qty\":<integer>}],\"unmatched\":[\"<phrases not on the menu>\"]}. " +
            "Never output prices. Use only exact menu names."
        var last = ""
        repeat(2) {
            val content = chat(system, order, apiKey, baseUrl, model)
            last = content
            val json = content.substringAfter('{', "").let { if (it.isEmpty()) null else "{" + it.substringBeforeLast('}') + "}" }
            if (json != null) return parse(json, menu)
        }
        throw AgentException("The agent did not answer clearly. Try again or use the keypad.")
    }

    private fun parse(json: String, menu: List<MenuItem>): PricedOrder {
        val o = try { JSONObject(json) } catch (e: Exception) { throw AgentException("The agent's answer was not readable.") }
        val understood = ArrayList<Pair<String, Int>>()
        o.optJSONArray("lines")?.let { arr ->
            for (i in 0 until arr.length()) { val l = arr.getJSONObject(i); understood += l.optString("item") to l.optInt("qty", 0) }
        }
        val unmatched = ArrayList<String>()
        o.optJSONArray("unmatched")?.let { arr -> for (i in 0 until arr.length()) unmatched += arr.optString(i) }
        return OrderPricer.price(menu, understood, unmatched)
    }

    private fun chat(system: String, user: String, key: String, baseUrl: String, model: String): String {
        val body = JSONObject()
            .put("model", model).put("max_tokens", 1500).put("temperature", 0)
            .put("messages", org.json.JSONArray()
                .put(JSONObject().put("role", "system").put("content", system))
                .put(JSONObject().put("role", "user").put("content", user.take(400))))
            .toString().toByteArray()
        val c = URL("${baseUrl.trimEnd('/')}/chat/completions").openConnection() as HttpURLConnection
        try {
            c.requestMethod = "POST"; c.connectTimeout = 15000; c.readTimeout = 45000; c.doOutput = true
            c.setRequestProperty("Authorization", "Bearer $key"); c.setRequestProperty("Content-Type", "application/json")
            c.outputStream.use { it.write(body) }
            if (c.responseCode !in 200..299) throw AgentException("The agent service said ${c.responseCode}. Check your key in Setup.")
            val text = c.inputStream.bufferedReader().readText()
            return JSONObject(text).getJSONArray("choices").getJSONObject(0).getJSONObject("message").optString("content", "")
        } catch (e: AgentException) { throw e
        } catch (e: java.io.IOException) { throw AgentException("No connection to the agent. You can still use the keypad.")
        } finally { c.disconnect() }
    }
}
