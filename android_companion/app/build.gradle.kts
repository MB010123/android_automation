import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

val releaseKeystorePropertiesFile = rootProject.file("keystore.properties")
val releaseKeystoreProperties = Properties()
if (releaseKeystorePropertiesFile.exists()) {
    releaseKeystorePropertiesFile.inputStream().use { releaseKeystoreProperties.load(it) }
}

fun requireReleaseSigningProperty(name: String): String {
    val value = releaseKeystoreProperties.getProperty(name)?.trim().orEmpty()
    require(value.isNotEmpty()) {
        "Release signing requires $name in keystore.properties (see keystore.properties.example). " +
            "Do not sign the production APK with the Android debug keystore."
    }
    return value
}

android {
    namespace = "com.mobirent.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.mobirent.companion"
        minSdk = 31
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        buildConfigField("boolean", "REAL_ESIM_ENABLED", "false")
        buildConfigField("boolean", "PRODUCTION_PROXY_ENABLED", "false")
        buildConfigField("boolean", "BACKEND_ENABLED", "false")
        buildConfigField("String", "BACKEND_BASE_URL", "\"\"")
        buildConfigField("String", "DEVICE_TOKEN", "\"\"")
    }

    signingConfigs {
        if (releaseKeystorePropertiesFile.exists()) {
            create("release") {
                storeFile = rootProject.file(requireReleaseSigningProperty("storeFile"))
                storePassword = requireReleaseSigningProperty("storePassword")
                keyAlias = requireReleaseSigningProperty("keyAlias")
                keyPassword = requireReleaseSigningProperty("keyPassword")
            }
        }
    }

    buildTypes {
        release {
            isDebuggable = false
            isMinifyEnabled = false
            // Arms the public download path after Android grants a real
            // authority. This flag is not WRITE_EMBEDDED / carrier / DO.
            buildConfigField("boolean", "REAL_ESIM_ENABLED", "true")
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
            val releaseSigning = signingConfigs.findByName("release")
            require(releaseSigning != null) {
                "assembleRelease requires keystore.properties and a dedicated release keystore. " +
                    "Copy keystore.properties.example and do not use the debug key."
            }
            signingConfig = releaseSigning
        }
        debug {
            isDebuggable = true
            buildConfigField("boolean", "REAL_ESIM_ENABLED", "true")
        }
    }

    buildFeatures {
        buildConfig = true
        viewBinding = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    testOptions {
        unitTests.isReturnDefaultValues = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
    testImplementation("junit:junit:4.13.2")
    testImplementation("com.google.truth:truth:1.4.4")
    testImplementation("org.json:json:20240303")
}
