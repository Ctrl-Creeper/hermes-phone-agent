// Top-level build file for the Hermes Phone Agent APK.
//
// Build: ./gradlew :app:assembleDebug
// Output: app/build/outputs/apk/debug/app-debug.apk
// Copy to releases/: cp app/build/outputs/apk/debug/app-debug.apk releases/hermes-phone-agent-v0.2.2.apk

buildscript {
    repositories {
        google()
        mavenCentral()
    }
}

plugins {
    id("com.android.application") version "8.2.0" apply false
    id("org.jetbrains.kotlin.android") version "1.9.22" apply false
}
