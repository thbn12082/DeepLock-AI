package com.example.deeplock.widget

import android.content.Context
import android.content.Intent
import androidx.compose.runtime.Composable
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.glance.Button
import androidx.glance.GlanceId
import androidx.glance.GlanceModifier
import androidx.glance.LocalContext
import androidx.glance.appwidget.GlanceAppWidget
import androidx.glance.appwidget.GlanceAppWidgetReceiver
import androidx.glance.appwidget.action.actionStartActivity
import androidx.glance.appwidget.provideContent
import androidx.glance.layout.Alignment
import androidx.glance.layout.Column
import androidx.glance.layout.Spacer
import androidx.glance.layout.fillMaxSize
import androidx.glance.layout.height
import androidx.glance.layout.padding
import androidx.glance.text.FontWeight
import androidx.glance.text.Text
import androidx.glance.text.TextStyle
import com.example.deeplock.MainActivity
import com.example.deeplock.lockmode.LockNotificationController
import dagger.hilt.EntryPoint
import dagger.hilt.InstallIn
import dagger.hilt.android.EntryPointAccessors
import dagger.hilt.components.SingletonComponent

class DeepLockWidgetReceiver : GlanceAppWidgetReceiver() {
    override val glanceAppWidget: GlanceAppWidget = DeepLockWidget()
}

class DeepLockWidget : GlanceAppWidget() {
    override suspend fun provideGlance(context: Context, id: GlanceId) {
        val presentation = runCatching {
            EntryPointAccessors.fromApplication(
                context.applicationContext,
                DeepLockWidgetEntryPoint::class.java,
            ).widgetSnapshotRepository().presentationForWidget()
        }.getOrElse { WidgetPresentation.empty() }

        provideContent { WidgetContent(presentation) }
    }
}

@EntryPoint
@InstallIn(SingletonComponent::class)
internal interface DeepLockWidgetEntryPoint {
    fun widgetSnapshotRepository(): WidgetSnapshotRepository
}

@Composable
private fun WidgetContent(presentation: WidgetPresentation) {
    val context = LocalContext.current
    Column(
        modifier = GlanceModifier.fillMaxSize().padding(10.dp),
        verticalAlignment = Alignment.Vertical.Top,
        horizontalAlignment = Alignment.Horizontal.Start,
    ) {
        Text(
            text = "DeepLock AI",
            style = TextStyle(fontWeight = FontWeight.Bold, fontSize = 15.sp),
        )
        Text(
            text = "Bài đến hạn: ${presentation.dueCount}",
            style = TextStyle(fontWeight = FontWeight.Medium, fontSize = 13.sp),
        )
        Text(text = presentation.weakLine, style = TextStyle(fontSize = 12.sp))
        presentation.continueLine?.let { title ->
            Text(text = "Gần đây: $title", style = TextStyle(fontSize = 12.sp))
        }
        Spacer(GlanceModifier.height(6.dp))
        Button(
            text = "Xem thống kê",
            onClick = actionStartActivity(targetIntent(context, presentation.learnTarget, WIDGET_STATS_ACTION)),
            modifier = GlanceModifier.defaultWeight(),
        )
    }
}

private fun targetIntent(context: Context, target: WidgetTarget, action: String): Intent =
    Intent(context, MainActivity::class.java)
        .setAction(action)
        .addFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP or Intent.FLAG_ACTIVITY_SINGLE_TOP)
        .putExtra(LockNotificationController.EXTRA_DESTINATION, target.destination)
        .putExtra(LockNotificationController.EXTRA_ATOM_ID, target.atomId)

private const val WIDGET_STATS_ACTION = "com.example.deeplock.widget.action.STATS"
