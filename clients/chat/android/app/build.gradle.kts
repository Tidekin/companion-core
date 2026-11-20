plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

val releaseKeystore = providers.environmentVariable("COMPANION_ANDROID_KEYSTORE").orNull
val releaseAlias = providers.environmentVariable("COMPANION_ANDROID_KEY_ALIAS").orNull
val releaseStorePassword = providers.environmentVariable("COMPANION_ANDROID_STORE_PASSWORD").orNull
val releaseKeyPassword = providers.environmentVariable("COMPANION_ANDROID_KEY_PASSWORD").orNull

android {
    namespace = "dev.tidekin.companion_chat"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "dev.tidekin.companion_chat"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        create("distribution") {
            storeFile = releaseKeystore?.let { file(it) }
            keyAlias = releaseAlias
            storePassword = releaseStorePassword
            keyPassword = releaseKeyPassword
        }
    }
    buildTypes.getByName("release").signingConfig = signingConfigs.getByName("distribution")
}

tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    doFirst {
        require(!releaseKeystore.isNullOrBlank() && !releaseAlias.isNullOrBlank() &&
                !releaseStorePassword.isNullOrEmpty() && !releaseKeyPassword.isNullOrEmpty()) {
            "Set the four COMPANION_ANDROID signing variables documented in docs/native-chat-mvp.md."
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
