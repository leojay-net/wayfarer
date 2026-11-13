plugins {
    id("com.android.application")
    kotlin("android")
    kotlin("plugin.compose")
}

android {
    namespace = "com.wayfarer.app"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.wayfarer.tap"
        // 30: BiometricPrompt with device-credential fallback and time-limited Keystore authentication.
        minSdk = 30
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0-prototype"
    }

    buildTypes {
        release { isMinifyEnabled = false }
    }

    buildFeatures { compose = true }

    packaging {
        // Bouncy Castle and jspecify both ship these OSGi manifests; neither is needed at runtime.
        resources.excludes += "META-INF/versions/9/OSGI-INF/MANIFEST.MF"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
}

kotlin { jvmToolchain(17) }

dependencies {
    implementation(project(":wayfarer-tap"))
    implementation(platform("androidx.compose:compose-bom:2025.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.foundation:foundation")
    implementation("androidx.activity:activity-compose:1.11.0")
    implementation("androidx.fragment:fragment:1.8.9")
    implementation("com.google.zxing:core:3.5.3")
    implementation("com.google.android.gms:play-services-code-scanner:16.1.0")
}
