import groovy.json.JsonSlurper
import java.security.MessageDigest
import java.util.zip.ZipFile

plugins {
    alias(libs.plugins.android.application)
    alias(libs.plugins.kotlin.compose)
    alias(libs.plugins.kotlin.serialization)
    alias(libs.plugins.ksp)
    alias(libs.plugins.hilt)
    alias(libs.plugins.room)
}

android {
    namespace = "com.example.deeplock"
    compileSdk = 36

    defaultConfig {
        applicationId = "com.example.deeplock"
        minSdk = 26
        targetSdk = 36
        versionCode = 1
        versionName = "1.0.0"
        testInstrumentationRunner = "androidx.test.runner.AndroidJUnitRunner"
        vectorDrawables.useSupportLibrary = true
    }

    buildTypes {
        release {
            isMinifyEnabled = true
            isShrinkResources = true
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    buildFeatures {
        compose = true
        buildConfig = true
    }

    packaging.resources.excludes += setOf("/META-INF/{AL2.0,LGPL2.1}")
    testOptions.unitTests.isIncludeAndroidResources = true
}

room { schemaDirectory("$projectDir/schemas") }

dependencies {
    implementation(libs.androidx.core.ktx)
    implementation(libs.androidx.activity.compose)
    implementation(libs.androidx.lifecycle.runtime.compose)
    implementation(libs.androidx.lifecycle.viewmodel.compose)
    implementation(libs.androidx.navigation.compose)

    val composeBom = platform(libs.androidx.compose.bom)
    implementation(composeBom)
    androidTestImplementation(composeBom)
    implementation(libs.androidx.compose.ui)
    implementation(libs.androidx.compose.material3)
    implementation(libs.androidx.compose.icons)
    implementation(libs.androidx.compose.ui.tooling.preview)
    debugImplementation(libs.androidx.compose.ui.tooling)

    implementation(libs.androidx.room.runtime)
    implementation(libs.androidx.room.ktx)
    ksp(libs.androidx.room.compiler)
    implementation(libs.androidx.datastore)
    implementation(libs.hilt.android)
    ksp(libs.hilt.compiler)
    implementation(libs.androidx.hilt.navigation)
    implementation(libs.androidx.glance.appwidget)
    implementation(libs.kotlinx.serialization.json)
    implementation(libs.kotlinx.coroutines.android)

    testImplementation(libs.junit)
    testImplementation(libs.truth)
    testImplementation(libs.kotlinx.coroutines.test)
}

val verifyNoNetworkPermission by tasks.registering {
    group = "verification"
    doLast {
        val manifest = file("src/main/AndroidManifest.xml").readText()
        val forbidden = listOf("android.permission.INTERNET", "android.permission.ACCESS_NETWORK_STATE")
        check(forbidden.none { permission ->
            manifest.lineSequence().any { line -> permission in line && "tools:node=\"remove\"" !in line }
        }) { "Release source manifest declares a forbidden network permission" }
    }
}

val verifyReleaseMergedManifest by tasks.registering {
    group = "verification"
    val merged = layout.buildDirectory.file("intermediates/merged_manifest/release/processReleaseMainManifest/AndroidManifest.xml")
    inputs.file(merged)
    doLast {
        val manifest = merged.get().asFile.readText()
        val forbidden = listOf("android.permission.INTERNET", "android.permission.ACCESS_NETWORK_STATE")
        check(forbidden.none(manifest::contains)) { "Merged release manifest contains a forbidden network permission" }
    }
}

tasks.matching { it.name == "processReleaseMainManifest" }.configureEach {
    finalizedBy(verifyReleaseMergedManifest)
}

val verifyNoNetworkDependencies by tasks.registering {
    group = "verification"
    doLast {
        val forbidden = listOf("okhttp", "retrofit", "ktor-client", "openai", "firebase-messaging")
        val declared = configurations.flatMap { configuration ->
            configuration.dependencies.map { "${it.group}:${it.name}".lowercase() }
        }
        check(forbidden.none { needle -> declared.any { needle in it } }) {
            "Android dependency graph contains a forbidden network client"
        }
    }
}

val verifyBundledPack by tasks.registering {
    group = "verification"
    val assetRoot = file("src/main/assets")
    val catalog = file("src/main/assets/content/catalog.json")
    doLast {
        check(catalog.isFile) { "Missing content catalog: $catalog" }
        @Suppress("UNCHECKED_CAST")
        val root = JsonSlurper().parse(catalog) as Map<String, Any?>
        check(root["schema_version"] == "1.0") { "Unsupported content catalog schema" }
        val packs = root["packs"] as? List<Map<String, Any?>> ?: error("Catalog packs missing")
        check(packs.isNotEmpty()) { "Catalog contains no packs" }
        packs.forEach { descriptor ->
            val relative = descriptor["asset_path"] as? String ?: error("Catalog pack asset_path missing")
            check(relative.startsWith("content/") && ".." !in relative) { "Unsafe catalog pack path: $relative" }
            val pack = assetRoot.resolve(relative)
            check(pack.isFile) { "Missing AI-approved bundled pack: $pack" }
            (descriptor["asset_sha256"] as? String)?.let { expected ->
                val digest = MessageDigest.getInstance("SHA-256")
                pack.inputStream().buffered().use { input ->
                    val buffer = ByteArray(DEFAULT_BUFFER_SIZE)
                    while (true) {
                        val count = input.read(buffer)
                        if (count < 0) break
                        digest.update(buffer, 0, count)
                    }
                }
                val actual = "sha256:" + digest.digest().joinToString("") { "%02x".format(it.toInt() and 0xff) }
                check(actual == expected) { "Catalog pack checksum mismatch: $relative" }
            }
            ZipFile(pack).use { zip ->
                val manifestEntry = zip.getEntry("manifest.json") ?: error("Pack manifest missing: $relative")
                check(zip.getEntry("ai_review_manifest.json") != null) { "AI review manifest missing: $relative" }
                @Suppress("UNCHECKED_CAST")
                val manifest = zip.getInputStream(manifestEntry).reader().use {
                    JsonSlurper().parse(it) as Map<String, Any?>
                }
                check(manifest["content_pack_id"] == descriptor["pack_id"]) { "Catalog/pack id mismatch: $relative" }
                check(manifest["course_id"] == descriptor["course_id"]) { "Catalog/pack course mismatch: $relative" }
                check((manifest["rejected_count"] as? Number)?.toInt() == 0) { "Rejected content in pack: $relative" }
            }
        }
    }
}

tasks.matching { it.name == "preReleaseBuild" }.configureEach {
    dependsOn(verifyNoNetworkPermission, verifyNoNetworkDependencies, verifyBundledPack)
}
