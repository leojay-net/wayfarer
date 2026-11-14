package com.wayfarer.tap

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class OrderPricerTest {
    private val menu = listOf(MenuItem("Jollof rice", 1500), MenuItem("Zobo", 500), MenuItem("Bottled water", 300))

    @Test fun pricesFromTheMenuOnly() {
        val o = OrderPricer.price(menu, listOf("jollof rice" to 2, "ZOBO" to 1))
        assertEquals(3500, o.totalNaira)
        assertTrue(o.ready)
    }

    @Test fun mergesRepeatedItems() {
        val o = OrderPricer.price(menu, listOf("Zobo" to 1, "Zobo" to 2))
        assertEquals(1, o.lines.size)
        assertEquals(1500, o.totalNaira)
    }

    @Test fun unknownItemsAreReportedNotGuessed() {
        val o = OrderPricer.price(menu, listOf("Zobo" to 1, "Pizza" to 1))
        assertFalse(o.ready)
        assertEquals(listOf("Pizza"), o.unmatched)
    }

    @Test fun badQuantitiesAreRefused() {
        assertFalse(OrderPricer.price(menu, listOf("Zobo" to 0)).ready)
        assertFalse(OrderPricer.price(menu, listOf("Zobo" to 51)).ready)
        assertFalse(OrderPricer.price(menu, listOf("Zobo" to -3)).ready)
    }

    @Test fun emptyOrderIsNotReady() { assertFalse(OrderPricer.price(menu, emptyList()).ready) }

    @Test fun modelNotUnderstoodPhrasesBlockTheCharge() {
        assertFalse(OrderPricer.price(menu, listOf("Zobo" to 1), listOf("something blue")).ready)
    }
}
