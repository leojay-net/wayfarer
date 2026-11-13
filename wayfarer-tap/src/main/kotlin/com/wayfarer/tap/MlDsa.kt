package com.wayfarer.tap

import java.security.SecureRandom
import org.bouncycastle.pqc.crypto.mldsa.MLDSAKeyGenerationParameters
import org.bouncycastle.pqc.crypto.mldsa.MLDSAKeyPairGenerator
import org.bouncycastle.pqc.crypto.mldsa.MLDSAParameters
import org.bouncycastle.pqc.crypto.mldsa.MLDSAPrivateKeyParameters
import org.bouncycastle.pqc.crypto.mldsa.MLDSAPublicKeyParameters
import org.bouncycastle.pqc.crypto.mldsa.MLDSASigner

/**
 * ML-DSA-65 (FIPS 204) through Bouncy Castle. Not audited by us. Keys are the 32-byte FIPS 204 seed, the same form the
 * Python signer and the key files use, so one seed gives the same public key everywhere (checked by the shared vectors).
 */
object MlDsa {
    const val PUBLIC_KEY_SIZE = 1952
    const val SIGNATURE_SIZE = 3309
    const val SEED_SIZE = 32
    private val PARAMS = MLDSAParameters.ml_dsa_65

    /** A `SecureRandom` that hands out exactly `seed` once: key generation reads its 32-byte seed from it. */
    private class SeedRandom(private val seed: ByteArray) : SecureRandom() {
        private var used = false

        override fun nextBytes(bytes: ByteArray) {
            check(!used && bytes.size == seed.size) { "Unexpected request for randomness." }
            seed.copyInto(bytes)
            used = true
        }
    }

    private fun keyPair(seed: ByteArray): Pair<MLDSAPublicKeyParameters, MLDSAPrivateKeyParameters> {
        if (seed.size != SEED_SIZE) throw TapException("An ML-DSA seed must be $SEED_SIZE bytes.")
        val generator = MLDSAKeyPairGenerator()
        generator.init(MLDSAKeyGenerationParameters(SeedRandom(seed), PARAMS))
        val pair = generator.generateKeyPair()
        return (pair.public as MLDSAPublicKeyParameters) to (pair.private as MLDSAPrivateKeyParameters)
    }

    fun publicKeyFromSeed(seed: ByteArray): ByteArray = keyPair(seed).first.encoded

    fun newSeed(random: SecureRandom = SecureRandom()): ByteArray = ByteArray(SEED_SIZE).also(random::nextBytes)

    /** Sign `message` (pure ML-DSA, empty context). `randomness` of 32 zero bytes gives the deterministic variant. */
    fun sign(seed: ByteArray, message: ByteArray, deterministic: Boolean = false): ByteArray {
        val signer = MLDSASigner()
        val key = keyPair(seed).second
        if (deterministic) {
            signer.init(true, key)
        } else {
            signer.init(true, org.bouncycastle.crypto.params.ParametersWithRandom(key, SecureRandom()))
        }
        signer.update(message, 0, message.size)
        return signer.generateSignature()
    }

    fun verify(publicKey: ByteArray, signature: ByteArray, message: ByteArray): Boolean {
        if (publicKey.size != PUBLIC_KEY_SIZE || signature.size != SIGNATURE_SIZE) return false
        val signer = MLDSASigner()
        signer.init(false, MLDSAPublicKeyParameters(PARAMS, publicKey))
        signer.update(message, 0, message.size)
        return signer.verifySignature(signature)
    }
}
