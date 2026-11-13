package com.wayfarer.app

import android.app.Activity
import android.nfc.NfcAdapter
import android.nfc.Tag
import android.nfc.cardemulation.HostApduService
import android.nfc.tech.IsoDep
import android.os.Bundle
import com.wayfarer.tap.ApduClient
import com.wayfarer.tap.ApduServer
import com.wayfarer.tap.CommitResult
import com.wayfarer.tap.TapException

/** Process-wide holder of the merchant's card-side state machine. The terminal sets `outgoing` and the cheque handler. */
object MerchantCard {
    @Volatile
    var handler: (ByteArray) -> CommitResult = { CommitResult(false, "The merchant app is not ready.") }
    val server = ApduServer { handler(it) }
}

/** Android glue only: every command goes straight to [ApduServer], which is unit-tested in wayfarer-tap. */
class CardService : HostApduService() {
    override fun processCommandApdu(commandApdu: ByteArray, extras: Bundle?): ByteArray = MerchantCard.server.process(commandApdu)

    override fun onDeactivated(reason: Int) = MerchantCard.server.reset()
}

/** The buyer side: reader mode while the buyer screen is open. Calls [onTag] on a worker thread for each phone touched. */
class NfcReader(private val activity: Activity, private val onTag: (ApduClient, () -> Unit) -> Unit) {
    private val adapter: NfcAdapter? = NfcAdapter.getDefaultAdapter(activity)

    val available get() = adapter?.isEnabled == true

    fun start() {
        adapter?.enableReaderMode(
            activity,
            { tag: Tag -> handle(tag) },
            NfcAdapter.FLAG_READER_NFC_A or NfcAdapter.FLAG_READER_NFC_B or NfcAdapter.FLAG_READER_SKIP_NDEF_CHECK,
            null,
        )
    }

    fun stop() = adapter?.disableReaderMode(activity)

    private fun handle(tag: Tag) {
        val iso = IsoDep.get(tag) ?: return
        try {
            iso.connect()
            iso.timeout = 5000
            onTag(ApduClient { iso.transceive(it) }) { iso.close() }
        } catch (e: java.io.IOException) {
            throw TapException("The phones moved apart. Hold them together and try again.")
        } finally {
            runCatching { iso.close() }
        }
    }
}
