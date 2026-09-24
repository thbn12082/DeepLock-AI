package com.example.deeplock.widget

import android.content.Context
import androidx.glance.appwidget.updateAll
import androidx.room.withTransaction
import com.example.deeplock.data.SettingsRepository
import com.example.deeplock.data.content.ContentPackRepository
import com.example.deeplock.data.local.AtomProgressEntity
import com.example.deeplock.data.local.DeepLockDatabase
import com.example.deeplock.data.local.WidgetSnapshotEntity
import com.example.deeplock.lockmode.LockNotificationController
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

internal data class WidgetAtomState(
    val atomId: String,
    val title: String,
    val order: Int,
    val status: String,
    val mastery: Float,
    val dueAt: Long,
    val lastStudiedAt: Long?,
)

internal data class WidgetTarget(val destination: String, val atomId: String?)

internal data class WidgetPresentation(
    val dueCount: Int,
    val weakLine: String,
    val continueLine: String?,
    val learnTarget: WidgetTarget,
    val reviewTarget: WidgetTarget,
) {
    companion object {
        fun empty() = WidgetPresentation(
            dueCount = 0,
            weakLine = "Kiến thức yếu: chưa có",
            continueLine = null,
            learnTarget = WidgetTarget(LockNotificationController.DEST_HOME, null),
            reviewTarget = WidgetTarget(LockNotificationController.DEST_HOME, null),
        )
    }
}

internal object WidgetSnapshotCalculator {
    fun calculate(atoms: List<WidgetAtomState>, now: Long): WidgetSnapshotEntity {
        val ordered = atoms
            .map { atom ->
                atom.copy(
                    mastery = atom.mastery.takeIf { it.isFinite() }?.coerceIn(0f, 1f) ?: 0f,
                    dueAt = atom.dueAt.coerceAtLeast(0L),
                    lastStudiedAt = atom.lastStudiedAt?.takeIf { it >= 0L },
                )
            }
            .sortedWith(compareBy<WidgetAtomState>({ it.order }, { it.atomId }))
        val due = ordered.count { it.dueAt in 1..now }
        val weak = ordered
            .filter { it.status != "NEW" || it.lastStudiedAt != null || it.dueAt > 0L || it.mastery > 0f }
            .minWithOrNull(compareBy<WidgetAtomState>({ it.mastery }, { it.dueAt.takeIf { dueAt -> dueAt > 0 } ?: Long.MAX_VALUE }, { it.order }, { it.atomId }))
        val continuing = ordered
            .filter { it.status != "MASTERED" && it.lastStudiedAt != null }
            .maxWithOrNull(
                compareBy<WidgetAtomState> { it.lastStudiedAt ?: Long.MIN_VALUE }
                    .thenByDescending { it.order }
                    .thenByDescending { it.atomId },
            )
            ?: ordered.firstOrNull { it.status != "MASTERED" }
            ?: ordered.firstOrNull { it.dueAt in 1..now }
            ?: weak
        return WidgetSnapshotEntity(
            dueCount = due,
            weakAtomId = weak?.atomId,
            weakAtomTitle = weak?.title,
            continueAtomId = continuing?.atomId,
            continueAtomTitle = continuing?.title,
            updatedAt = now,
        )
    }

    fun present(snapshot: WidgetSnapshotEntity, showTitles: Boolean, validAtomIds: Set<String>): WidgetPresentation {
        val weakId = snapshot.weakAtomId?.takeIf(validAtomIds::contains)
        val continueId = snapshot.continueAtomId?.takeIf(validAtomIds::contains)
        val weakLine = when {
            !showTitles -> "Kiến thức yếu: đã ẩn"
            weakId == null -> "Kiến thức yếu: chưa có"
            else -> "Yếu nhất: ${snapshot.weakAtomTitle.orEmpty().ifBlank { weakId }}"
        }
        val continueLine = if (showTitles && continueId != null) {
            snapshot.continueAtomTitle?.takeIf { it.isNotBlank() }
        } else null
        return WidgetPresentation(
            dueCount = snapshot.dueCount.coerceAtLeast(0),
            weakLine = weakLine,
            continueLine = continueLine,
            // Learning is lock-screen-only. The widget is another statistics
            // surface and can never route into a lesson or quiz inside the app.
            learnTarget = WidgetTarget(LockNotificationController.DEST_HOME, null),
            reviewTarget = WidgetTarget(LockNotificationController.DEST_HOME, null),
        )
    }
}

@Singleton
class WidgetSnapshotRepository @Inject constructor(
    @ApplicationContext private val context: Context,
    private val database: DeepLockDatabase,
    private val content: ContentPackRepository,
    private val settings: SettingsRepository,
) {
    internal suspend fun presentationForWidget(): WidgetPresentation {
        val snapshot = database.widgetSnapshotDao().get() ?: return WidgetPresentation.empty()
        val catalog = content.catalog()
        val showTitles = settings.settingsValue().widgetShowTitles
        return WidgetSnapshotCalculator.present(snapshot, showTitles, catalog.atoms.mapTo(mutableSetOf()) { it.atom_id })
    }

    /** Called after a learning transaction; no polling, worker, service or network is introduced. */
    suspend fun refreshAndNotify(now: Long = System.currentTimeMillis()) {
        materialize(now)
        DeepLockWidget().updateAll(context)
    }

    private suspend fun materialize(now: Long): WidgetSnapshotEntity {
        val catalog = content.catalog()
        val progress = database.studyDao().progressNow().associateBy(AtomProgressEntity::atomId)
        val atoms = catalog.orderedAtoms().map { atom ->
            val value = progress[atom.atom_id]
            WidgetAtomState(
                atomId = atom.atom_id,
                title = atom.title,
                order = atom.order_index,
                status = value?.status ?: "NEW",
                mastery = value?.mastery ?: 0f,
                dueAt = value?.dueAt ?: 0L,
                lastStudiedAt = value?.lastStudiedAt,
            )
        }
        val snapshot = WidgetSnapshotCalculator.calculate(atoms, now)
        database.withTransaction { database.widgetSnapshotDao().put(snapshot) }
        return snapshot
    }
}
