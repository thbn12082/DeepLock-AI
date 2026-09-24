package com.example.deeplock.data

import android.content.Context
import androidx.datastore.preferences.core.booleanPreferencesKey
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.intPreferencesKey
import androidx.datastore.preferences.core.longPreferencesKey
import androidx.datastore.preferences.core.stringPreferencesKey
import androidx.datastore.preferences.preferencesDataStore
import androidx.glance.appwidget.updateAll
import com.example.deeplock.widget.DeepLockWidget
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.flow.map

private val Context.deepLockDataStore by preferencesDataStore("deeplock_settings")

data class AppSettings(
    val lockReviewEnabled: Boolean = false,
    val directExperimentalEnabled: Boolean = DEFAULT_DIRECT_PRIMARY_ENABLED,
    val showExplanations: Boolean = true,
    val lockThemeId: String = "blue",
    val preRecallSeconds: Int = 5,
    val publicLockContent: Boolean = DEFAULT_PUBLIC_LOCK_CONTENT,
    val widgetShowTitles: Boolean = true,
    val oneUiSetupConfirmed: Boolean = false,
    val testCardConfirmed: Boolean = false,
    val activeHourStart: Int = 0,
    val activeHourEnd: Int = 24,
    val runtimeStatus: String = "STOPPED",
    val serviceHeartbeatAt: Long = 0L,
)

@Singleton
class SettingsRepository @Inject constructor(@ApplicationContext private val context: Context) {
    val settings: Flow<AppSettings> = context.deepLockDataStore.data.map { values ->
        AppSettings(
            lockReviewEnabled = values[LOCK_ENABLED] ?: false,
            // Ignore notification-first values persisted by older APKs. The
            // learning surface is direct-only; the status notification remains.
            directExperimentalEnabled = resolveDirectPrimaryEnabled(values[DIRECT_EXPERIMENTAL_ENABLED]),
            showExplanations = values[SHOW_EXPLANATIONS] ?: true,
            lockThemeId = values[LOCK_THEME_ID] ?: "blue",
            preRecallSeconds = values[PRE_RECALL_SECONDS] ?: 5,
            // An older false value must not hide the requested lesson/quiz board.
            publicLockContent = resolvePublicLockContent(values[PUBLIC_LOCK_CONTENT]),
            widgetShowTitles = values[WIDGET_SHOW_TITLES] ?: true,
            oneUiSetupConfirmed = values[ONE_UI_SETUP_CONFIRMED] ?: false,
            testCardConfirmed = values[TEST_CARD_CONFIRMED] ?: false,
            activeHourStart = values[ACTIVE_HOUR_START] ?: 0,
            activeHourEnd = values[ACTIVE_HOUR_END] ?: 24,
            runtimeStatus = values[RUNTIME_STATUS] ?: "STOPPED",
            serviceHeartbeatAt = values[SERVICE_HEARTBEAT_AT] ?: 0L,
        )
    }

    suspend fun setLockEnabled(enabled: Boolean) = context.deepLockDataStore.edit { it[LOCK_ENABLED] = enabled }
    suspend fun setDirectExperimentalEnabled(@Suppress("UNUSED_PARAMETER") enabled: Boolean) =
        context.deepLockDataStore.edit { it[DIRECT_EXPERIMENTAL_ENABLED] = true }
    suspend fun settingsValue(): AppSettings = settings.first()
    suspend fun setShowExplanations(value: Boolean) = context.deepLockDataStore.edit { it[SHOW_EXPLANATIONS] = value }
    suspend fun setLockThemeId(value: String) = context.deepLockDataStore.edit { it[LOCK_THEME_ID] = value }
    suspend fun setPreRecallSeconds(value: Int) = context.deepLockDataStore.edit {
        it[PRE_RECALL_SECONDS] = value.coerceIn(0, 5)
    }
    suspend fun setPublicLockContent(@Suppress("UNUSED_PARAMETER") value: Boolean) =
        context.deepLockDataStore.edit { it[PUBLIC_LOCK_CONTENT] = true }
    suspend fun setWidgetShowTitles(value: Boolean) {
        context.deepLockDataStore.edit { it[WIDGET_SHOW_TITLES] = value }
        DeepLockWidget().updateAll(context)
    }
    suspend fun setOneUiSetupConfirmed(value: Boolean) = context.deepLockDataStore.edit { it[ONE_UI_SETUP_CONFIRMED] = value }
    suspend fun setTestCardConfirmed(value: Boolean) = context.deepLockDataStore.edit { it[TEST_CARD_CONFIRMED] = value }
    suspend fun setActiveHours(start: Int, end: Int) = context.deepLockDataStore.edit {
        it[ACTIVE_HOUR_START] = start.coerceIn(0, 23)
        it[ACTIVE_HOUR_END] = end.coerceIn(1, 24)
    }
    suspend fun setRuntimeStatus(status: String, heartbeatAt: Long = System.currentTimeMillis()) =
        context.deepLockDataStore.edit {
            it[RUNTIME_STATUS] = status
            it[SERVICE_HEARTBEAT_AT] = heartbeatAt
        }

    companion object {
        val LOCK_ENABLED = booleanPreferencesKey("lock_review_enabled")
        val DIRECT_EXPERIMENTAL_ENABLED = booleanPreferencesKey("direct_experimental_enabled")
        val SHOW_EXPLANATIONS = booleanPreferencesKey("show_explanations")
        val LOCK_THEME_ID = stringPreferencesKey("lock_theme_id")
        val PRE_RECALL_SECONDS = intPreferencesKey("pre_recall_seconds")
        val PUBLIC_LOCK_CONTENT = booleanPreferencesKey("public_lock_content")
        val WIDGET_SHOW_TITLES = booleanPreferencesKey("widget_show_titles")
        val ONE_UI_SETUP_CONFIRMED = booleanPreferencesKey("one_ui_setup_confirmed")
        val TEST_CARD_CONFIRMED = booleanPreferencesKey("test_card_confirmed")
        val ACTIVE_HOUR_START = intPreferencesKey("active_hour_start")
        val ACTIVE_HOUR_END = intPreferencesKey("active_hour_end")
        val RUNTIME_STATUS = stringPreferencesKey("lock_runtime_status")
        val SERVICE_HEARTBEAT_AT = longPreferencesKey("service_heartbeat_at")
    }
}
