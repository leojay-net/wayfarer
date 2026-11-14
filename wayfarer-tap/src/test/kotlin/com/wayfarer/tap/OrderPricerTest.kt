package com.wayfarer.tap

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class OrderPricerTest {
    private val menu = listOf(MenuItem("Coffee", 3), MenuItem("Pastry", 2), MenuItem("Bottled water", 1))

    @Test fun pricesFromTheMenuOnly() {
        val o = OrderPricer.price(menu, listOf("coffee" to 2, "PASTRY" to 1))
        assertEquals(8, o.total)
        assertTrue(o.ready)
    }

    @Test fun mergesRepeatedItems() {
        val o = OrderPricer.price(menu, listOf("Pastry" to 1, "Pastry" to 2))
        assertEquals(1, o.lines.size)
        assertEquals(6, o.total)
    }

    @Test fun unknownItemsAreReportedNotGuessed() {
        val o = OrderPricer.price(menu, listOf("Pastry" to 1, "Pizza" to 1))
        assertFalse(o.ready)
        assertEquals(listOf("Pizza"), o.unmatched)
    }

    @Test fun badQuantitiesAreRefused() {
        assertFalse(OrderPricer.price(menu, listOf("Pastry" to 0)).ready)
        assertFalse(OrderPricer.price(menu, listOf("Pastry" to 51)).ready)
        assertFalse(OrderPricer.price(menu, listOf("Pastry" to -3)).ready)
    }

    @Test fun emptyOrderIsNotReady() { assertFalse(OrderPricer.price(menu, emptyList()).ready) }

    @Test fun modelNotUnderstoodPhrasesBlockTheCharge() {
        assertFalse(OrderPricer.price(menu, listOf("Pastry" to 1), listOf("something blue")).ready)
    }
}
