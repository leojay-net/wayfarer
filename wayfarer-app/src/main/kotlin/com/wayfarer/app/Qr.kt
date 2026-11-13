package com.wayfarer.app

import android.app.Activity
import android.graphics.Bitmap
import android.graphics.Color
import com.google.mlkit.vision.codescanner.GmsBarcodeScannerOptions
import com.google.mlkit.vision.codescanner.GmsBarcodeScanning
import com.google.zxing.BarcodeFormat
import com.google.zxing.EncodeHintType
import com.google.zxing.qrcode.QRCodeWriter
import com.google.zxing.qrcode.decoder.ErrorCorrectionLevel

/** Draw one QR frame. Low error correction keeps the code coarse enough to scan from another screen. */
fun qrBitmap(text: String, size: Int = 720): Bitmap {
    val matrix = QRCodeWriter().encode(
        text, BarcodeFormat.QR_CODE, size, size,
        mapOf(EncodeHintType.ERROR_CORRECTION to ErrorCorrectionLevel.L, EncodeHintType.MARGIN to 2),
    )
    val pixels = IntArray(size * size) { if (matrix[it % size, it / size]) Color.BLACK else Color.WHITE }
    return Bitmap.createBitmap(pixels, size, size, Bitmap.Config.ARGB_8888)
}

/** Scan one QR code with Google's code scanner (needs Google Play services). */
fun scanOne(activity: Activity, onText: (String) -> Unit, onError: (String) -> Unit) {
    val options = GmsBarcodeScannerOptions.Builder()
        .setBarcodeFormats(com.google.mlkit.vision.barcode.common.Barcode.FORMAT_QR_CODE)
        .build()
    GmsBarcodeScanning.getClient(activity, options).startScan()
        .addOnSuccessListener { onText(it.rawValue ?: "") }
        .addOnFailureListener { onError(it.message ?: "Scanning failed.") }
}
