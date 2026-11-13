package com.wayfarer.app

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import com.wayfarer.tap.MlDsa
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/**
 * Keeps the 32-byte ML-DSA seed encrypted at rest under an Android Keystore AES key that needs a fresh biometric or
 * device-credential unlock (valid [AUTH_SECONDS]). Android's Keystore has no ML-DSA, so the seed itself lives in app
 * memory while signing; the Keystore only guards it at rest. Prototype: not audited, not tested on many devices.
 */
class SeedStore(context: Context) {
    private val prefs = context.getSharedPreferences("wayfarer-seed", Context.MODE_PRIVATE)

    val exists get() = prefs.contains("blob")

    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        val spec = KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setUserAuthenticationRequired(true)
            .setUserAuthenticationParameters(AUTH_SECONDS, KeyProperties.AUTH_BIOMETRIC_STRONG or KeyProperties.AUTH_DEVICE_CREDENTIAL)
            .build()
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(spec)
            generateKey()
        }
    }

    /** Call right after the user authenticated. Creates a fresh seed on first use. */
    fun createIfMissing() {
        if (exists) return
        val seed = MlDsa.newSeed()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val blob = cipher.iv + cipher.doFinal(seed)
        prefs.edit().putString("blob", android.util.Base64.encodeToString(blob, android.util.Base64.NO_WRAP)).apply()
        seed.fill(0)
    }

    /** Call right after the user authenticated. Throws if the unlock is older than [AUTH_SECONDS]. */
    fun load(): ByteArray {
        val blob = android.util.Base64.decode(prefs.getString("blob", null) ?: error("No key yet."), android.util.Base64.NO_WRAP)
        val cipher = Cipher.getInstance("AES/GCM/NoPadding")
            .apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, blob.copyOfRange(0, IV_BYTES))) }
        return cipher.doFinal(blob.copyOfRange(IV_BYTES, blob.size))
    }

    companion object {
        const val ALIAS = "wayfarer-tap-seed"
        const val AUTH_SECONDS = 30
        private const val IV_BYTES = 12
    }
}
