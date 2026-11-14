package com.wayfarer.tap

/** One thing the shop sells. Prices are whole units of the shop's currency and only ever come from the shop's own menu. */
data class MenuItem(val name: String, val price: Long) {
    init { require(name.isNotBlank() && price > 0) { "A menu item needs a name and a positive price." } }
}

data class OrderLine(val item: MenuItem, val quantity: Int) { val subtotal: Long get() = item.price * quantity }

/**
 * The outcome of pricing a spoken or typed order. [total] is computed here from the menu, never taken from a model.
 * If [unmatched] is not empty the order is not [ready]: the merchant must fix it before charging.
 */
data class PricedOrder(val lines: List<OrderLine>, val unmatched: List<String>) {
    val total: Long get() = lines.sumOf { it.subtotal }
    val ready: Boolean get() = lines.isNotEmpty() && unmatched.isEmpty() && total in 1..OrderPricer.MAX_TOTAL
}

/**
 * Turns what an AI model *understood* ("2 x Coffee") into a price. The model only picks menu items and counts; it
 * never supplies a price, so a wrong or manipulated answer can at worst select the wrong items, which the merchant sees
 * and confirms before anything is charged. Unknown names are reported, never guessed.
 */
object OrderPricer {
    const val MAX_QUANTITY = 50
    const val MAX_TOTAL = 1_000_000L

    fun price(menu: List<MenuItem>, understood: List<Pair<String, Int>>, notUnderstood: List<String> = emptyList()): PricedOrder {
        val byName = menu.associateBy { it.name.trim().lowercase() }
        val merged = LinkedHashMap<MenuItem, Int>()
        val unmatched = notUnderstood.filter { it.isNotBlank() }.toMutableList()
        for ((rawName, rawQty) in understood) {
            val item = byName[rawName.trim().lowercase()]
            if (item == null) { unmatched += rawName.trim(); continue }
            if (rawQty < 1 || rawQty > MAX_QUANTITY) { unmatched += "${rawName.trim()} x$rawQty"; continue }
            merged[item] = (merged[item] ?: 0) + rawQty
        }
        val lines = merged.map { (item, qty) -> OrderLine(item, qty.coerceAtMost(MAX_QUANTITY)) }
        return PricedOrder(lines, unmatched)
    }
}
