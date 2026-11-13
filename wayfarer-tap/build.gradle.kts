plugins {
    kotlin("jvm")
    kotlin("plugin.serialization")
}

kotlin {
    jvmToolchain(17)
}

dependencies {
    // Bouncy Castle 1.79 has ML-DSA (FIPS 204) in org.bouncycastle.pqc.crypto.mldsa.
    api("org.bouncycastle:bcprov-jdk18on:1.79")
    testImplementation(kotlin("test"))
    testImplementation("org.jetbrains.kotlinx:kotlinx-serialization-json:1.11.0")
}

tasks.test {
    useJUnitPlatform()
    // The shared vectors live at the repository root, next to the Python tests.
    systemProperty("wayfarer.vectors", rootProject.file("tests/vectors").absolutePath)
}
