package com.example.deeplock.app

import android.content.Context
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.example.deeplock.data.AppSettings
import com.example.deeplock.data.LockDiagnostics
import com.example.deeplock.data.SettingsRepository
import com.example.deeplock.data.StudyCoordinator
import com.example.deeplock.data.StudyRepository
import com.example.deeplock.data.content.ContentCatalog
import com.example.deeplock.data.content.ContentPack
import com.example.deeplock.data.content.ContentPackRepository
import com.example.deeplock.data.local.AtomProgressEntity
import com.example.deeplock.data.local.AttemptEntity
import com.example.deeplock.data.local.AttemptActivityRow
import com.example.deeplock.data.local.LessonActivityRow
import com.example.deeplock.data.local.MistakeEntity
import com.example.deeplock.data.local.QuizSessionDao
import com.example.deeplock.data.local.QuizSessionEntity
import com.example.deeplock.lockmode.LockMonitorService
import com.example.deeplock.lockmode.LockNotificationController
import com.example.deeplock.lockmode.DirectLaunchSpecialAccess
import com.example.deeplock.lockmode.DirectLockActivityLauncher
import dagger.hilt.android.lifecycle.HiltViewModel
import dagger.hilt.android.qualifiers.ApplicationContext
import java.util.UUID
import javax.inject.Inject
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

data class AppUiState(
    val loading: Boolean = true,
    val catalog: ContentCatalog? = null,
    val activePackId: String? = null,
    val pack: ContentPack? = null,
    val progress: List<AtomProgressEntity> = emptyList(),
    val mistakes: List<MistakeEntity> = emptyList(),
    val attempts: List<AttemptEntity> = emptyList(),
    val attemptActivity: List<AttemptActivityRow> = emptyList(),
    val lessonActivity: List<LessonActivityRow> = emptyList(),
    val dashboardNow: Long = System.currentTimeMillis(),
    val quizSessions: Map<String, QuizSessionEntity> = emptyMap(),
    val settings: AppSettings = AppSettings(),
    val diagnostics: LockDiagnostics? = null,
    val error: String? = null,
)

@HiltViewModel
class MainViewModel @Inject constructor(
    @ApplicationContext private val context: Context,
    private val content: ContentPackRepository,
    private val study: StudyRepository,
    private val settingsRepository: SettingsRepository,
    private val coordinator: StudyCoordinator,
    private val quizSessions: QuizSessionDao,
    private val notifications: LockNotificationController,
    private val directSpecialAccess: DirectLaunchSpecialAccess,
    private val directLauncher: DirectLockActivityLauncher,
) : ViewModel() {
    private val initialLoading = MutableStateFlow(true)
    private val catalog = MutableStateFlow<ContentCatalog?>(null)
    private val activePackId = MutableStateFlow<String?>(null)
    private val pack = MutableStateFlow<ContentPack?>(null)
    private val error = MutableStateFlow<String?>(null)
    private val sessionState = MutableStateFlow<Map<String, QuizSessionEntity>>(emptyMap())
    private val diagnostics = MutableStateFlow<LockDiagnostics?>(null)
    private val dashboardNow = MutableStateFlow(System.currentTimeMillis())
    @Volatile private var requestedPackId: String? = null

    val uiState: StateFlow<AppUiState> = combine(
        initialLoading,
        catalog,
        activePackId,
        pack,
        study.progress,
        study.mistakes,
        study.attempts,
        study.attemptActivity,
        study.lessonActivity,
        sessionState,
        settingsRepository.settings,
        diagnostics,
        dashboardNow,
        error,
    ) { values ->
        @Suppress("UNCHECKED_CAST")
        AppUiState(
            loading = values[0] as Boolean,
            catalog = values[1] as ContentCatalog?,
            activePackId = values[2] as String?,
            pack = values[3] as ContentPack?,
            progress = values[4] as List<AtomProgressEntity>,
            mistakes = values[5] as List<MistakeEntity>,
            attempts = values[6] as List<AttemptEntity>,
            attemptActivity = values[7] as List<AttemptActivityRow>,
            lessonActivity = values[8] as List<LessonActivityRow>,
            quizSessions = values[9] as Map<String, QuizSessionEntity>,
            settings = values[10] as AppSettings,
            diagnostics = values[11] as LockDiagnostics?,
            dashboardNow = values[12] as Long,
            error = values[13] as String?,
        )
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), AppUiState())

    init {
        viewModelScope.launch {
            while (isActive) {
                dashboardNow.value = System.currentTimeMillis()
                delay(60_000L)
            }
        }
        viewModelScope.launch {
            val loaded = runCatching {
                val loadedCatalog = content.catalog()
                catalog.value = loadedCatalog
                activePackId.value = loadedCatalog.default_pack_id
                content.load(loadedCatalog.default_pack_id)
            }
                .onSuccess { pack.value = it }
                .onFailure { error.value = "Không thể xác minh gói nội dung: ${it.message}" }
            initialLoading.value = false
            // Also runs the in-place legacy notification cleanup when Lock
            // Review is currently disabled and no monitor service will start.
            notifications.createChannels()
            val appSettings = settingsRepository.settingsValue()
            if (loaded.isSuccess && appSettings.lockReviewEnabled) {
                if (notifications.statusNotificationsAvailable()) {
                    val setupCanRecover = appSettings.runtimeStatus == "APP_ONLY" &&
                        directSpecialAccess.isGranted()
                    if (setupCanRecover) LockMonitorService.requestResume(context)
                    else LockMonitorService.setEnabled(context, true)
                } else {
                    coordinator.setAppOnly("notification_permission_or_channel_on_app_resume")
                }
            }
            refreshDiagnostics()
        }
    }

    fun start(atomId: String) = viewModelScope.launch { study.markStarted(atomId) }

    fun openPack(packId: String) = viewModelScope.launch {
        val known = catalog.value?.packById(packId) ?: return@launch
        requestedPackId = known.pack_id
        error.value = null
        runCatching { content.load(known.pack_id) }
            .onSuccess { loaded ->
                if (requestedPackId == known.pack_id) {
                    pack.value = loaded
                    activePackId.value = known.pack_id
                }
            }
            .onFailure { failure -> if (requestedPackId == known.pack_id) error.value = "Không thể mở lecture: ${failure.message}" }
    }

    fun quizSessionId(questionId: String, origin: String): String = "$origin:$questionId"

    fun ensureQuizSession(questionId: String, pairId: String, atomId: String, origin: String) = viewModelScope.launch {
        val id = quizSessionId(questionId, origin)
        val existing = quizSessions.get(id)
        val value = existing ?: QuizSessionEntity(
            sessionId = id,
            questionId = questionId,
            pairId = pairId,
            atomId = atomId,
            origin = origin,
            phase = "PRE_RECALL",
            preRecallStartedAt = System.currentTimeMillis(),
            submittedAttemptUid = UUID.randomUUID().toString(),
            updatedAt = System.currentTimeMillis(),
        ).also { quizSessions.put(it) }
        sessionState.value = sessionState.value + (id to value)
    }

    fun revealQuizOptions(sessionId: String, skipped: Boolean) = viewModelScope.launch {
        val old = quizSessions.get(sessionId) ?: return@launch
        if (old.phase != "PRE_RECALL") return@launch
        val next = old.copy(
            phase = "OPTIONS_VISIBLE",
            optionsVisibleAt = System.currentTimeMillis(),
            preRecallSkipped = skipped,
            updatedAt = System.currentTimeMillis(),
        )
        quizSessions.put(next)
        sessionState.value = sessionState.value + (sessionId to next)
    }

    fun submitQuiz(questionId: String, selectedOptionId: String, origin: String, sessionId: String) = viewModelScope.launch {
        val session = quizSessions.get(sessionId) ?: return@launch
        if (session.phase == "SUBMITTED") return@launch
        val attemptUid = session.submittedAttemptUid ?: UUID.randomUUID().toString()
        coordinator.answer(
            questionId = questionId,
            selectedOptionId = selectedOptionId,
            origin = origin,
            preRecallMs = ((session.optionsVisibleAt ?: System.currentTimeMillis()) - session.preRecallStartedAt).coerceAtLeast(0L),
            preRecallSkipped = session.preRecallSkipped,
            attemptUid = attemptUid,
            quizSessionId = sessionId,
        )
        quizSessions.get(sessionId)?.let { updated -> sessionState.value = sessionState.value + (sessionId to updated) }
        notifications.cancelLearning()
        refreshDiagnostics()
    }

    fun finishQuizQuestion(sessionId: String) = viewModelScope.launch {
        quizSessions.delete(sessionId)
        sessionState.value = sessionState.value - sessionId
    }

    fun updateMistakeNote(questionId: String, note: String) = viewModelScope.launch {
        study.updateMistakeNote(questionId, note)
    }

    fun setLockReview(enabled: Boolean) = viewModelScope.launch {
        if (enabled) {
            settingsRepository.setLockEnabled(true)
            LockMonitorService.setEnabled(context, true)
        } else {
            coordinator.disableLockReview()
            settingsRepository.setLockEnabled(false)
            notifications.cancelLearning()
            LockMonitorService.setEnabled(context, false)
        }
        refreshDiagnostics()
    }

    fun setShowExplanations(value: Boolean) = viewModelScope.launch { settingsRepository.setShowExplanations(value) }
    fun setLockThemeId(value: String) = viewModelScope.launch { settingsRepository.setLockThemeId(value) }
    fun setPreRecallSeconds(value: Int) = viewModelScope.launch { settingsRepository.setPreRecallSeconds(value) }

    fun showTestLockCard() = viewModelScope.launch {
        error.value = null
        val appSettings = settingsRepository.settingsValue()
        if (!appSettings.lockReviewEnabled) {
            error.value = "Bật Lock Review trước khi thử bảng học."
            return@launch
        }
        if (!directLauncher.hasRequiredSpecialAccess()) {
            error.value = "Cần quyền Hiện trên cùng để thử bảng học trực tiếp."
            return@launch
        }
        val now = System.currentTimeMillis()
        val prepared = runCatching { coordinator.onScreenOff(now - 1_000L) }.getOrElse {
            error.value = "Không chuẩn bị được thẻ thử: ${it.message}"
            return@launch
        }
        if (!prepared) {
            error.value = "Chưa có thẻ thử sẵn sàng. Hãy bật Lock Review hoặc đóng bảng học đang mở."
            return@launch
        }
        val sessionId = coordinator.onScreenOn(
            interactive = true,
            displayOn = true,
            keyguardLocked = false,
            now = now,
        )
        if (sessionId == null) {
            error.value = "Không tạo được phiên thử bảng học."
            return@launch
        }
        val card = coordinator.pendingCard(sessionId)
        if (card == null) {
            error.value = "Phiên thử không còn thẻ hợp lệ."
            return@launch
        }
        val delivery = directLauncher.launch(card)
        if (delivery.decision.setupRequired) {
            error.value = "Không mở được bảng học: ${delivery.failureReason ?: "cần kiểm tra quyền One UI"}"
            refreshDiagnostics()
            return@launch
        }
        if (delivery.decision.directLaunchVisible) {
            val accepted = coordinator.markDirectActivityVisible(sessionId)
            directLauncher.confirmVisibleDelivery(delivery.attemptToken, accepted)
            if (!accepted) error.value = "Bảng học đã mở nhưng phiên thử bị từ chối."
        } else {
            error.value = "Bảng học thử không hiện kịp."
        }
        refreshDiagnostics()
    }

    fun onDirectSpecialAccessResult(granted: Boolean) = viewModelScope.launch {
        val appSettings = settingsRepository.settingsValue()
        if (!appSettings.lockReviewEnabled) return@launch
        if (granted) {
            // Re-enter the persisted READY state even when the status FGS was
            // deliberately kept alive while Android special access was missing.
            LockMonitorService.requestResume(context)
        } else {
            coordinator.setAppOnly("direct_special_access_missing_in_setup")
        }
        refreshDiagnostics()
    }
    fun setWidgetShowTitles(value: Boolean) = viewModelScope.launch { settingsRepository.setWidgetShowTitles(value) }
    fun confirmOneUiSetup(value: Boolean) = viewModelScope.launch { settingsRepository.setOneUiSetupConfirmed(value) }
    fun confirmTestCard(value: Boolean) = viewModelScope.launch { settingsRepository.setTestCardConfirmed(value) }

    fun refreshDiagnostics() = viewModelScope.launch {
        diagnostics.value = runCatching { coordinator.diagnostics() }.getOrNull()
    }

    fun clearProgress() = viewModelScope.launch { study.clearAll() }
}
