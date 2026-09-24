package com.example.deeplock.lockmode

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.view.WindowManager
import androidx.activity.ComponentActivity
import androidx.activity.compose.BackHandler
import androidx.activity.compose.setContent
import androidx.activity.viewModels
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.RadioButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.SideEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.key
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.toArgb
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.ViewModel
import com.example.deeplock.MainActivity
import com.example.deeplock.data.LockItemType
import com.example.deeplock.data.AppSettings
import com.example.deeplock.data.PendingLockCard
import com.example.deeplock.data.SettingsRepository
import com.example.deeplock.data.StudyCoordinator
import com.example.deeplock.data.content.ContentPackRepository
import com.example.deeplock.data.content.ContentPack
import com.example.deeplock.data.content.Lesson
import com.example.deeplock.data.content.Question
import com.example.deeplock.ui.OfflineIllustration
import com.example.deeplock.ui.DeepLockTheme
import com.example.deeplock.ui.GameProgressIndicator
import com.example.deeplock.ui.lockScreenThemeById
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.NonCancellable
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * Primary personal-APK delivery surface. It never turns the display on and never
 * uses a full-screen intent or a free-floating overlay. The user-granted "appear
 * on top" special access only exempts this exact Activity launch from One UI's
 * background-start block. The screen remains blank until the service confirms
 * that this exact persisted session owns delivery.
 */
@AndroidEntryPoint
class LearningCardActivity : ComponentActivity() {
    @Inject lateinit var content: ContentPackRepository
    @Inject lateinit var coordinator: StudyCoordinator
    @Inject lateinit var settingsRepository: SettingsRepository

    private val retainedDelivery by viewModels<DirectDeliveryViewModel>()
    private val lessonPagePreferences by lazy {
        getSharedPreferences("lesson_card_pages", MODE_PRIVATE)
    }
    private var foregroundReported = false
    private var deliveryState by mutableStateOf(TokenOwnedDeliveryState<DirectCardRequest>())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        if (!acceptIntent(intent)) {
            finishAndRemoveTask()
            return
        }
        if (Build.VERSION.SDK_INT >= 27) setShowWhenLocked(true)
        else @Suppress("DEPRECATION") window.addFlags(WindowManager.LayoutParams.FLAG_SHOW_WHEN_LOCKED)
        // Samsung's keyguard normally turns the display off again after only a
        // few seconds. A lesson needs enough time to be read; otherwise the
        // Activity disappears just before the learner taps "Tiếp tục" and the
        // tap lands on the screen underneath it.
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)

        setContent {
            val settings by settingsRepository.settings.collectAsState(initial = AppSettings())
            val lockTheme = lockScreenThemeById(settings.lockThemeId)
            SideEffect {
                window.statusBarColor = lockTheme.background.toArgb()
                window.navigationBarColor = lockTheme.background.toArgb()
            }
            DeepLockTheme(lockTheme = lockTheme) {
                Surface(
                    modifier = Modifier.fillMaxSize(),
                    color = MaterialTheme.colorScheme.background,
                ) {
                    Box(Modifier.fillMaxSize().safeDrawingPadding()) {
                        val currentRequest = deliveryState.visible?.value
                        if (currentRequest != null) {
                            DirectLockCard(
                                request = currentRequest,
                                repository = content,
                                initialLessonPageId = savedLessonPage(currentRequest),
                                onLessonPageChanged = { pageId ->
                                    saveLessonPage(currentRequest, pageId)
                                },
                                onCompleteLesson = { onResult ->
                                    completeLesson(currentRequest, onResult)
                                },
                                onSubmit = { question, optionId, onResult ->
                                    submit(currentRequest, question, optionId, onResult)
                                },
                                onDashboard = { openDashboard(currentRequest) },
                                onDefer = { onResult -> defer(currentRequest, onResult) },
                                onClose = { close(currentRequest) },
                            )
                        }
                    }
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        if (!acceptIntent(intent)) {
            deliveryState = TokenOwnedDeliveryState()
            finishAndRemoveTask()
            return
        }
        // A screen cycle can arrive while the previous lesson board is still
        // the top Activity. SINGLE_TOP deliberately routes that quiz here, so
        // acknowledge its fresh token even when Android does not call onResume.
        if (lifecycle.currentState.isAtLeast(Lifecycle.State.RESUMED)) {
            reportForegroundForCurrentRequest()
        }
    }

    override fun onResume() {
        super.onResume()
        reportForegroundForCurrentRequest()
    }

    private fun acceptIntent(intent: Intent): Boolean {
        val incoming = DirectCardRequest.from(intent) ?: return false
        if (!retainedDelivery.canReceive(incoming)) return false
        val alreadyAccepted = retainedDelivery.isAccepted(incoming)
        val updatedState = deliveryState.receive(
            token = incoming.attemptToken,
            value = incoming,
            alreadyAccepted = alreadyAccepted,
        ) ?: return false
        retainedDelivery.markIncoming(incoming)
        deliveryState = updatedState
        foregroundReported = alreadyAccepted
        return true
    }

    private fun reportForegroundForCurrentRequest() {
        val currentRequest = deliveryState.pending?.value ?: return
        if (foregroundReported) return
        foregroundReported = true
        val token = currentRequest.attemptToken
        if (!retainedDelivery.wasForegroundReported(currentRequest)) {
            if (!DirectActivityVisibilityRegistry.reportForeground(token)) {
                finishAndRemoveTask()
                return
            }
            retainedDelivery.markForegroundReported(currentRequest)
        }
        lifecycleScope.launch {
            val accepted = DirectActivityVisibilityRegistry.awaitDeliveryAccepted(token)
            // A newer screen cycle may have replaced the card while this old
            // handshake was suspended. Never let the old result close or reveal
            // the new request.
            if (!deliveryState.ownsPending(token, currentRequest)) return@launch
            if (accepted) {
                retainedDelivery.markAccepted(currentRequest)
                deliveryState = deliveryState.accept(token, currentRequest)
            } else {
                finishAndRemoveTask()
            }
        }
    }

    private fun submit(
        renderedRequest: DirectCardRequest,
        question: Question,
        selectedOptionId: String,
        onResult: (Result<Unit>) -> Unit,
    ) {
        if (renderedRequest.itemType != LockItemType.QUIZ ||
            renderedRequest.questionId != question.question_id ||
            !deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)
        ) {
            onResult(Result.failure(IllegalStateException("Thẻ quiz không còn hiệu lực")))
            return
        }
        lifecycleScope.launch {
            if (!deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)) {
                return@launch
            }
            val result = suspendResult {
                val outcome = coordinator.answer(
                    questionId = question.question_id,
                    selectedOptionId = selectedOptionId,
                    origin = "LOCK_REVIEW",
                    preRecallMs = 0L,
                    preRecallSkipped = true,
                    attemptUid = "direct:${renderedRequest.sessionId}:${question.question_id}",
                    expectedLockSessionId = renderedRequest.sessionId,
                )
                check(outcome.accepted) { "Phiên quiz đã được thay bằng một thẻ mới" }
            }
            if (deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)) {
                onResult(result)
            }
        }
    }

    private fun completeLesson(
        renderedRequest: DirectCardRequest,
        onResult: (Result<Unit>) -> Unit,
    ) {
        if (renderedRequest.itemType != LockItemType.LESSON ||
            !deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)
        ) {
            onResult(Result.failure(IllegalStateException("Thẻ bài học không còn hiệu lực")))
            return
        }
        lifecycleScope.launch {
            val result = suspendResult {
                check(coordinator.completeLesson(renderedRequest.sessionId)) {
                    "Phiên bài học không còn hiệu lực"
                }
            }
            if (!deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)) return@launch
            if (result.isSuccess) {
                clearSavedLessonPage(renderedRequest)
                val nextCard = runCatching {
                    coordinator.immediateQuizAfterCompletedLesson(renderedRequest.sessionId)
                }.getOrNull()
                val nextRequest = nextCard?.toDirectCardRequest(renderedRequest.attemptToken)
                if (nextRequest != null &&
                    nextRequest != renderedRequest &&
                    nextRequest.itemType == LockItemType.QUIZ
                ) {
                    val owned = TokenOwnedValue(renderedRequest.attemptToken, nextRequest)
                    deliveryState = TokenOwnedDeliveryState(pending = owned, visible = owned)
                    retainedDelivery.markIncoming(nextRequest)
                    retainedDelivery.markForegroundReported(nextRequest)
                    retainedDelivery.markAccepted(nextRequest)
                    onResult(Result.success(Unit))
                } else {
                    finishAndRemoveTask()
                }
            } else {
                onResult(result)
            }
        }
    }

    private fun lessonPageKey(request: DirectCardRequest): String =
        "${request.pairId}:${request.lessonId}"

    private fun savedLessonPage(request: DirectCardRequest): String? =
        lessonPagePreferences.all[lessonPageKey(request)] as? String

    private fun saveLessonPage(request: DirectCardRequest, pageId: String) {
        lessonPagePreferences.edit().putString(lessonPageKey(request), pageId).apply()
    }

    private fun clearSavedLessonPage(request: DirectCardRequest) {
        lessonPagePreferences.edit().remove(lessonPageKey(request)).apply()
    }

    private fun defer(
        renderedRequest: DirectCardRequest,
        onResult: (Result<Unit>) -> Unit,
    ) {
        if (!deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)) {
            onResult(Result.failure(IllegalStateException("Thẻ học không còn hiệu lực")))
            return
        }
        lifecycleScope.launch {
            val result = suspendResult {
                check(coordinator.deferLockCard(renderedRequest.sessionId)) {
                    "Phiên học không còn hiệu lực"
                }
            }
            if (!deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)) return@launch
            if (result.isSuccess) finishAndRemoveTask() else onResult(result)
        }
    }

    private suspend fun suspendResult(block: suspend () -> Unit): Result<Unit> = try {
        withContext(NonCancellable) { block() }
        Result.success(Unit)
    } catch (cancelled: CancellationException) {
        throw cancelled
    } catch (error: Throwable) {
        Result.failure(error)
    }

    private fun openDashboard(renderedRequest: DirectCardRequest) {
        if (!deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)) return
        startActivity(
            Intent(this, MainActivity::class.java)
                .addFlags(
                    Intent.FLAG_ACTIVITY_NEW_TASK or
                        Intent.FLAG_ACTIVITY_CLEAR_TOP or
                        Intent.FLAG_ACTIVITY_SINGLE_TOP,
                )
                .putExtra(LockNotificationController.EXTRA_DESTINATION, LockNotificationController.DEST_HOME)
                .putExtra(LockNotificationController.EXTRA_PACK_ID, renderedRequest.packId)
                .putExtra(LockNotificationController.EXTRA_ATOM_ID, renderedRequest.atomId),
        )
        finishAndRemoveTask()
    }

    private fun close(renderedRequest: DirectCardRequest) {
        if (deliveryState.ownsVisible(renderedRequest.attemptToken, renderedRequest)) {
            finishAndRemoveTask()
        }
    }
}

internal class DirectDeliveryViewModel : ViewModel() {
    private var currentRequest: DirectCardRequest? = null
    private var foregroundReportedRequest: DirectCardRequest? = null
    private var acceptedRequest: DirectCardRequest? = null

    fun canReceive(incoming: DirectCardRequest): Boolean {
        val current = currentRequest ?: return true
        return current.attemptToken != incoming.attemptToken || current == incoming
    }

    fun markIncoming(incoming: DirectCardRequest) {
        if (currentRequest?.attemptToken != incoming.attemptToken) {
            foregroundReportedRequest = null
            acceptedRequest = null
        }
        currentRequest = incoming
    }

    fun wasForegroundReported(request: DirectCardRequest): Boolean =
        currentRequest == request && foregroundReportedRequest == request

    fun isAccepted(request: DirectCardRequest): Boolean =
        currentRequest == request && acceptedRequest == request

    fun markForegroundReported(request: DirectCardRequest) {
        if (currentRequest == request) foregroundReportedRequest = request
    }

    fun markAccepted(request: DirectCardRequest) {
        if (currentRequest == request) acceptedRequest = request
    }
}

internal data class DirectCardRequest(
    val attemptToken: String,
    val sessionId: String,
    val pairId: String,
    val atomId: String,
    val lessonId: String,
    val questionId: String?,
    val itemType: LockItemType,
    val packId: String? = null,
    val quizPurpose: String? = null,
) {
    companion object {
        fun from(intent: Intent): DirectCardRequest? {
            val token = intent.getStringExtra(DirectLockActivityLauncher.EXTRA_DIRECT_ATTEMPT_TOKEN)
                ?.takeIf(String::isNotBlank) ?: return null
            val sessionId = intent.getStringExtra(LockNotificationController.EXTRA_UNLOCK_SESSION_ID)
                ?.takeIf(String::isNotBlank) ?: return null
            val pairId = intent.getStringExtra(DirectLockActivityLauncher.EXTRA_PAIR_ID)
                ?.takeIf(String::isNotBlank) ?: return null
            val atomId = intent.getStringExtra(LockNotificationController.EXTRA_ATOM_ID)
                ?.takeIf(String::isNotBlank) ?: return null
            val lessonId = intent.getStringExtra(DirectLockActivityLauncher.EXTRA_LESSON_ID)
                ?.takeIf(String::isNotBlank) ?: return null
            val itemType = intent.getStringExtra(DirectLockActivityLauncher.EXTRA_ITEM_TYPE)
                ?.let { runCatching { LockItemType.valueOf(it) }.getOrNull() } ?: return null
            val questionId = intent.getStringExtra(LockNotificationController.EXTRA_QUESTION_ID)
            val packId = intent.getStringExtra(LockNotificationController.EXTRA_PACK_ID)?.takeIf(String::isNotBlank)
            val quizPurpose = intent.getStringExtra(DirectLockActivityLauncher.EXTRA_QUIZ_PURPOSE)
                ?.takeIf(String::isNotBlank)
            if (itemType == LockItemType.QUIZ && questionId.isNullOrBlank()) return null
            if (itemType == LockItemType.LESSON && questionId != null) return null
            return DirectCardRequest(
                token,
                sessionId,
                pairId,
                atomId,
                lessonId,
                questionId,
                itemType,
                packId,
                quizPurpose,
            )
        }
    }
}

private fun PendingLockCard.toDirectCardRequest(attemptToken: String): DirectCardRequest =
    DirectCardRequest(
        attemptToken = attemptToken,
        sessionId = unlockSessionId,
        pairId = pairId,
        atomId = atomId,
        lessonId = lessonId,
        questionId = questionId,
        itemType = itemType,
        packId = packId,
        quizPurpose = quizPurpose,
    )

private data class ValidatedDirectCard(
    val pack: ContentPack,
    val lesson: Lesson,
    val question: Question?,
    val questionNumber: Int?,
    val questionCount: Int,
)

@Composable
private fun DirectLockCard(
    request: DirectCardRequest,
    repository: ContentPackRepository,
    initialLessonPageId: String?,
    onLessonPageChanged: (String) -> Unit,
    onCompleteLesson: ((Result<Unit>) -> Unit) -> Unit,
    onSubmit: (Question, String, (Result<Unit>) -> Unit) -> Unit,
    onDashboard: () -> Unit,
    onDefer: ((Result<Unit>) -> Unit) -> Unit,
    onClose: () -> Unit,
) {
    val loaded by produceState<Result<ValidatedDirectCard>?>(initialValue = null, request) {
        value = runCatching {
            val packId = request.packId ?: requireNotNull(repository.packIdForPair(request.pairId))
            val pack = repository.load(packId)
            val pair = requireNotNull(pack.pairById(request.pairId))
            val lesson = requireNotNull(pack.lessonById(request.lessonId))
            require(pair.atom_id == request.atomId && pair.micro_lesson_id == request.lessonId)
            require(lesson.pair_id == request.pairId && lesson.atom_id == request.atomId)
            val question = request.questionId?.let { requireNotNull(pack.questionById(it)) }
            if (request.itemType == LockItemType.QUIZ) {
                requireNotNull(question)
                require(question.question_id in pair.question_ids)
                require(question.pair_id == request.pairId && question.atom_id == request.atomId)
            } else {
                require(question == null)
            }
            ValidatedDirectCard(
                pack = pack,
                lesson = lesson,
                question = question,
                questionNumber = question?.let { pair.question_ids.indexOf(it.question_id) + 1 },
                questionCount = pair.question_ids.size,
            )
        }
    }
    val card = loaded?.getOrNull()
    when {
        loaded == null -> Text("Đang xác minh thẻ đã chuẩn bị…", Modifier.padding(24.dp))
        card == null -> Column(Modifier.padding(24.dp)) {
            Text("Thẻ học không còn hợp lệ")
            Button(onClick = onClose) { Text("Đóng") }
        }
        request.itemType == LockItemType.LESSON -> DirectLesson(
            cardKey = request.attemptToken,
            pack = card.pack,
            lesson = card.lesson,
            quizCount = card.questionCount,
            initialPageId = initialLessonPageId,
            onPageChanged = onLessonPageChanged,
            onComplete = onCompleteLesson,
            onDefer = onDefer,
        )
        else -> DirectQuiz(
            cardKey = request.attemptToken,
            question = requireNotNull(card.question),
            questionNumber = requireNotNull(card.questionNumber),
            questionCount = card.questionCount,
            spacedReview = request.quizPurpose == StudyCoordinator.SPACED_MISTAKE,
            onSubmit = onSubmit,
            onDashboard = onDashboard,
            onDefer = onDefer,
            onClose = onClose,
        )
    }
}

@Composable
private fun DirectLesson(
    cardKey: String,
    pack: ContentPack,
    lesson: Lesson,
    quizCount: Int,
    initialPageId: String?,
    onPageChanged: (String) -> Unit,
    onComplete: ((Result<Unit>) -> Unit) -> Unit,
    onDefer: ((Result<Unit>) -> Unit) -> Unit,
) {
    val illustrations = pack.illustrationsForLesson(lesson)
    val pages = remember(lesson, illustrations) { buildLessonCardPages(lesson, illustrations) }
    var restoredPageIndex by rememberSaveable(cardKey, lesson.lesson_id) {
        mutableStateOf(resolveLessonPageIndex(initialPageId, pages))
    }
    val pageIndex = clampLessonPageIndex(restoredPageIndex, pages.size)
    val page = pages[pageIndex]
    var pendingAction by remember(cardKey, lesson.lesson_id) { mutableStateOf<String?>(null) }
    var actionError by remember(cardKey, lesson.lesson_id) { mutableStateOf<String?>(null) }
    fun moveTo(targetPage: Int) {
        val nextPage = clampLessonPageIndex(targetPage, pages.size)
        restoredPageIndex = nextPage
        onPageChanged(pages[nextPage].id)
    }
    fun complete() {
        if (pendingAction != null) return
        pendingAction = "COMPLETE"
        actionError = null
        onComplete { result ->
            result.onFailure { error ->
                pendingAction = null
                actionError = error.message ?: "Không thể hoàn tất bài học. Hãy thử lại."
            }
        }
    }
    fun defer() {
        if (pendingAction != null) return
        pendingAction = "DEFER"
        actionError = null
        onDefer { result ->
            result.onFailure { error ->
                pendingAction = null
                actionError = error.message ?: "Không thể để bài học lại sau. Hãy thử lại."
            }
        }
    }
    BackHandler {
        if (pendingAction != null) return@BackHandler
        when (lessonBackAction(pageIndex)) {
            LessonBackAction.PREVIOUS -> moveTo(pageIndex - 1)
            LessonBackAction.DEFER -> defer()
        }
    }
    Column(Modifier.fillMaxSize()) {
        LockCardHeader(
            eyebrow = "MÀN HỌC",
            title = lesson.title,
            position = pageIndex + 1,
            count = pages.size,
            deferLabel = if (pendingAction == "DEFER") "Đang lưu…" else "Để sau",
            deferEnabled = pendingAction == null,
            onDefer = ::defer,
        )
        Box(Modifier.weight(1f).fillMaxWidth()) {
            key(page.id) {
                LessonPageContent(
                    page = page,
                    pack = pack,
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }
        Surface(tonalElevation = 4.dp) {
            Column(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                actionError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                val primaryAction = lessonPrimaryAction(pageIndex, pages.size)
                Button(
                    onClick = {
                        when (primaryAction) {
                            LessonPrimaryAction.NEXT -> moveTo(pageIndex + 1)
                            LessonPrimaryAction.COMPLETE -> complete()
                        }
                    },
                    enabled = pendingAction == null,
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(
                        when {
                            pendingAction == "COMPLETE" -> "Đang lưu…"
                            primaryAction == LessonPrimaryAction.COMPLETE -> "BẮT ĐẦU $quizCount CÂU"
                            else -> "TIẾP TỤC"
                        },
                    )
                }
                if (pageIndex > 0) {
                    TextButton(
                        onClick = { moveTo(pageIndex - 1) },
                        enabled = pendingAction == null,
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Quay lại") }
                }
            }
        }
    }
}

@Composable
private fun LockCardHeader(
    eyebrow: String,
    title: String,
    position: Int,
    count: Int,
    deferLabel: String,
    deferEnabled: Boolean,
    onDefer: () -> Unit,
) {
    Column(Modifier.fillMaxWidth().padding(start = 20.dp, end = 12.dp, top = 14.dp)) {
        Row(
            Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Text(
                "$eyebrow · $position/$count",
                color = MaterialTheme.colorScheme.primary,
                fontWeight = FontWeight.Bold,
            )
            TextButton(onClick = onDefer, enabled = deferEnabled) { Text(deferLabel) }
        }
        Text(
            title,
            style = MaterialTheme.typography.titleLarge,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(end = 8.dp),
        )
        GameProgressIndicator(
            progress = position.toFloat() / count,
            modifier = Modifier.fillMaxWidth().padding(top = 6.dp),
        )
    }
}

@Composable
private fun LessonPageContent(
    page: LessonCardPage,
    pack: ContentPack,
    modifier: Modifier = Modifier,
) {
    LazyColumn(
        modifier.fillMaxWidth().padding(horizontal = 20.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item { Spacer(Modifier.height(4.dp)) }
        item { Text(page.title, style = MaterialTheme.typography.headlineMedium, fontWeight = FontWeight.Bold) }
        when (page) {
            is LessonCardPage.Overview -> {
                item { FocusBlock("Mục tiêu", page.objective) }
                item { FocusBlock("Vì sao cần biết?", page.hook, emphasized = true) }
            }
            is LessonCardPage.Intuition -> item {
                Text(page.text, style = MaterialTheme.typography.titleMedium)
            }
            is LessonCardPage.Visual -> item {
                OfflineIllustration(page.illustration, pack.bytesForIllustration(page.illustration))
            }
            is LessonCardPage.Example -> {
                item { FocusBlock("Input", page.example.input) }
                items(page.example.steps.size) { index ->
                    NumberedStep(index + 1, page.example.steps[index])
                }
                item { FocusBlock("Output", page.example.output, emphasized = true) }
            }
            is LessonCardPage.Formula -> {
                item { FocusBlock("Biểu thức", page.formula.plain_text, emphasized = true) }
                if (page.formula.latex.isNotBlank()) {
                    item { FormulaFocusBlock(page.formula.latex) }
                }
                items(page.formula.symbols.size) { index ->
                    val symbol = page.formula.symbols[index]
                    FocusBlock(humanizeMathText(symbol.symbol), symbol.meaning)
                }
            }
            is LessonCardPage.Process -> items(page.steps.size) { index ->
                NumberedStep(page.startNumber + index, page.steps[index])
            }
            is LessonCardPage.Mistake -> item {
                FocusBlock("Dễ nhầm", page.text, emphasized = true)
            }
            is LessonCardPage.Takeaways -> items(page.items.size) { index ->
                NumberedStep(page.startNumber + index, page.items[index])
            }
            is LessonCardPage.Recall -> item {
                FocusBlock("Tự nhắc lại", page.prompt, emphasized = true)
            }
        }
        item { Spacer(Modifier.height(8.dp)) }
    }
}

@Composable
private fun FocusBlock(label: String, text: String, emphasized: Boolean = false) {
    Card(
        Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = if (emphasized) {
                MaterialTheme.colorScheme.primaryContainer
            } else {
                MaterialTheme.colorScheme.surfaceContainerLow
            },
        ),
    ) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(6.dp)) {
            Text(humanizeMathText(label), color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold)
            Text(humanizeMathText(text), style = MaterialTheme.typography.bodyLarge)
        }
    }
}

@Composable
private fun NumberedStep(number: Int, text: String) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("$number", color = MaterialTheme.colorScheme.primary, fontWeight = FontWeight.Bold)
        Text(humanizeMathText(text), Modifier.weight(1f), style = MaterialTheme.typography.bodyLarge)
    }
}

@Composable
private fun DirectQuiz(
    cardKey: String,
    question: Question,
    questionNumber: Int,
    questionCount: Int,
    spacedReview: Boolean,
    onSubmit: (Question, String, (Result<Unit>) -> Unit) -> Unit,
    onDashboard: () -> Unit,
    onDefer: ((Result<Unit>) -> Unit) -> Unit,
    onClose: () -> Unit,
) {
    var candidate by rememberSaveable(cardKey, question.question_id) { mutableStateOf<String?>(null) }
    var selected by rememberSaveable(cardKey, question.question_id) { mutableStateOf<String?>(null) }
    var submitting by remember(cardKey, question.question_id) { mutableStateOf(false) }
    var submitError by remember(cardKey, question.question_id) { mutableStateOf<String?>(null) }
    var deferring by remember(cardKey, question.question_id) { mutableStateOf(false) }
    var deferError by remember(cardKey, question.question_id) { mutableStateOf<String?>(null) }
    var explanationExpanded by rememberSaveable(cardKey, question.question_id) { mutableStateOf(false) }

    fun submit() {
        val optionId = candidate ?: return
        if (selected != null || submitting || deferring) return
        submitting = true
        submitError = null
        onSubmit(question, optionId) { result ->
            result.onSuccess {
                selected = optionId
                submitting = false
            }.onFailure { error ->
                submitting = false
                submitError = error.message ?: "Không thể lưu câu trả lời. Hãy thử lại."
            }
        }
    }

    fun defer() {
        if (submitting || deferring || selected != null) return
        deferring = true
        deferError = null
        onDefer { result ->
            result.onFailure { error ->
                deferring = false
                deferError = error.message ?: "Không thể để câu hỏi lại sau. Hãy thử lại."
            }
        }
    }

    BackHandler {
        when {
            submitting || deferring -> Unit
            selected != null -> onClose()
            else -> defer()
        }
    }

    val submittedAnswer = selected
    if (explanationExpanded && submittedAnswer != null) {
        val isCorrect = submittedAnswer == question.correct_option_id
        val selectedOption = question.options.first { it.id == submittedAnswer }
        val correctOption = question.options.first { it.id == question.correct_option_id }
        AlertDialog(
            onDismissRequest = { explanationExpanded = false },
            title = { Text(if (isCorrect) "Vì sao đáp án đúng?" else "Cùng sửa câu này") },
            text = {
                LazyColumn(
                    Modifier.heightIn(max = 420.dp),
                    verticalArrangement = Arrangement.spacedBy(10.dp),
                ) {
                    if (!isCorrect) {
                        item {
                            FocusBlock(
                                "Lựa chọn ${selectedOption.id}",
                                selectedOption.rationale,
                            )
                        }
                    }
                    item {
                        FocusBlock(
                            "Đáp án ${correctOption.id}",
                            correctOption.rationale,
                            emphasized = true,
                        )
                    }
                    item { Text(humanizeMathText(question.explanation), style = MaterialTheme.typography.bodyLarge) }
                }
            },
            confirmButton = {
                TextButton(onClick = { explanationExpanded = false }) { Text("Đã hiểu") }
            },
            dismissButton = {
                TextButton(onClick = onDashboard) { Text("Xem thống kê") }
            },
        )
    }

    Column(Modifier.fillMaxSize()) {
        LockCardHeader(
            eyebrow = if (spacedReview) "ÔN LẠI CÂU TỪNG SAI" else "THỬ THÁCH",
            title = if (spacedReview) "Câu nhắc lại" else "Câu $questionNumber/$questionCount",
            position = if (spacedReview) 1 else questionNumber,
            count = if (spacedReview) 1 else questionCount,
            deferLabel = if (deferring) "Đang lưu…" else "Để sau",
            deferEnabled = selected == null && !submitting && !deferring,
            onDefer = ::defer,
        )
        LazyColumn(
            Modifier.weight(1f).fillMaxWidth().padding(horizontal = 18.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item { Spacer(Modifier.height(4.dp)) }
            item { Text(humanizeMathText(question.stem), style = MaterialTheme.typography.headlineSmall, fontWeight = FontWeight.SemiBold) }
            items(question.options, key = { it.id }) { option ->
                val submitted = submittedAnswer
                val optionSelected = (submitted ?: candidate) == option.id
                val containerColor = when {
                    submitted == null && optionSelected -> MaterialTheme.colorScheme.primaryContainer
                    submitted == null -> MaterialTheme.colorScheme.surfaceContainerLow
                    option.id == question.correct_option_id -> MaterialTheme.colorScheme.tertiaryContainer
                    option.id == submitted -> MaterialTheme.colorScheme.errorContainer
                    else -> MaterialTheme.colorScheme.surfaceContainerLow
                }
                Card(
                    modifier = Modifier.fillMaxWidth().clickable(
                        enabled = submitted == null && !submitting && !deferring,
                    ) { candidate = option.id },
                    colors = CardDefaults.cardColors(containerColor = containerColor),
                ) {
                    Row(Modifier.padding(12.dp)) {
                        RadioButton(selected = optionSelected, onClick = null)
                        Text("${option.id}. ${humanizeMathText(option.text)}", Modifier.weight(1f).padding(start = 8.dp))
                    }
                }
            }
            item { Spacer(Modifier.height(8.dp)) }
        }
        val isCorrect = submittedAnswer == question.correct_option_id
        Surface(
            tonalElevation = 4.dp,
            color = when {
                submittedAnswer == null -> MaterialTheme.colorScheme.surface
                isCorrect -> MaterialTheme.colorScheme.tertiaryContainer
                else -> MaterialTheme.colorScheme.errorContainer
            },
        ) {
            Column(
                Modifier.fillMaxWidth().padding(horizontal = 16.dp, vertical = 12.dp),
                verticalArrangement = Arrangement.spacedBy(6.dp),
            ) {
                submitError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                deferError?.let { Text(it, color = MaterialTheme.colorScheme.error) }
                if (submittedAnswer == null) {
                    Button(
                        onClick = ::submit,
                        enabled = candidate != null && !submitting && !deferring,
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Text(if (submitting) "ĐANG LƯU…" else if (submitError != null) "THỬ LẠI" else "KIỂM TRA")
                    }
                } else {
                    val correctOption = question.options.first { it.id == question.correct_option_id }
                    Text(
                        if (isCorrect) "★ TUYỆT VỜI!" else "CHƯA ĐÚNG · ĐÁP ÁN ${correctOption.id}",
                        style = MaterialTheme.typography.titleLarge,
                        fontWeight = FontWeight.Bold,
                    )
                    Text(
                        if (isCorrect) {
                            "Bạn đã vượt qua câu này."
                        } else {
                            "Câu này sẽ quay lại ở lần mở khóa sau."
                        },
                    )
                    TextButton(
                        onClick = { explanationExpanded = true },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Xem giải thích") }
                    Button(onClick = onClose, modifier = Modifier.fillMaxWidth()) { Text("TIẾP TỤC") }
                }
            }
        }
    }
}
