package com.example.deeplock.app

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.activity.compose.BackHandler
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.ArrowBack
import androidx.compose.material.icons.filled.Settings
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Switch
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.TopAppBarDefaults
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.hilt.navigation.compose.hiltViewModel
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.example.deeplock.lockmode.LockNotificationController
import com.example.deeplock.ui.availableLockScreenThemes

data class DeepLinkRequest(
    val destination: String,
    val packId: String?,
    val atomId: String?,
    val questionId: String?,
    val unlockSessionId: String?,
    val nonce: Long,
)

internal enum class MainDestination { DASHBOARD, SETTINGS }

/** Old lesson/quiz/widget links can open the app, but can never recreate an in-app study surface. */
internal fun resolveMainDestination(destination: String?): MainDestination =
    if (destination == LockNotificationController.DEST_SETTINGS) {
        MainDestination.SETTINGS
    } else {
        MainDestination.DASHBOARD
    }

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DeepLockApp(
    deepLink: DeepLinkRequest? = null,
    viewModel: MainViewModel = hiltViewModel(),
) {
    val state by viewModel.uiState.collectAsStateWithLifecycle()
    var destination by rememberSaveable { mutableStateOf(MainDestination.DASHBOARD) }

    LaunchedEffect(deepLink?.nonce) {
        destination = resolveMainDestination(deepLink?.destination)
    }
    BackHandler(destination == MainDestination.SETTINGS) {
        destination = MainDestination.DASHBOARD
    }

    Scaffold(
        containerColor = MaterialTheme.colorScheme.background,
        topBar = {
            TopAppBar(
                title = {
                    Text(if (destination == MainDestination.SETTINGS) "Cài đặt" else "DeepLock")
                },
                navigationIcon = {
                    if (destination == MainDestination.SETTINGS) {
                        IconButton(onClick = { destination = MainDestination.DASHBOARD }) {
                            Icon(Icons.Default.ArrowBack, contentDescription = "Quay lại thống kê")
                        }
                    }
                },
                actions = {
                    if (destination == MainDestination.DASHBOARD) {
                        IconButton(onClick = { destination = MainDestination.SETTINGS }) {
                            Icon(Icons.Default.Settings, contentDescription = "Mở cài đặt")
                        }
                    }
                },
                colors = TopAppBarDefaults.topAppBarColors(
                    containerColor = MaterialTheme.colorScheme.background,
                    scrolledContainerColor = MaterialTheme.colorScheme.surface,
                ),
            )
        },
    ) { padding ->
        Box(Modifier.fillMaxSize().padding(padding)) {
            when (destination) {
                MainDestination.DASHBOARD -> StatisticsDashboard(
                    state = state,
                    onThemeSelected = viewModel::setLockThemeId,
                    onTestLockCard = viewModel::showTestLockCard,
                )
                MainDestination.SETTINGS -> SettingsScreen(state, viewModel)
            }
        }
    }
}

@Composable
private fun SettingsScreen(state: AppUiState, viewModel: MainViewModel) {
    val context = LocalContext.current
    var confirmReset by remember { mutableStateOf(false) }
    var directSpecialAccessGranted by remember { mutableStateOf(Settings.canDrawOverlays(context)) }
    val notificationPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission(),
    ) { granted -> viewModel.setLockReview(granted) }
    val directSpecialAccessLauncher = rememberLauncherForActivityResult(
        ActivityResultContracts.StartActivityForResult(),
    ) {
        directSpecialAccessGranted = Settings.canDrawOverlays(context)
        viewModel.onDirectSpecialAccessResult(directSpecialAccessGranted)
    }
    val notificationGranted = Build.VERSION.SDK_INT < 33 ||
        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) ==
        PackageManager.PERMISSION_GRANTED
    val heartbeatFresh = state.settings.serviceHeartbeatAt > 0L &&
        System.currentTimeMillis() - state.settings.serviceHeartbeatAt < 120_000L
    val diagnostic = state.diagnostics

    fun openSystemSettings(action: String) {
        runCatching {
            val intent = Intent(action).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            if (action == Settings.ACTION_APP_NOTIFICATION_SETTINGS) {
                intent.putExtra(Settings.EXTRA_APP_PACKAGE, context.packageName)
            }
            context.startActivity(intent)
        }
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize().padding(horizontal = 18.dp),
        verticalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        item {
            SettingsGroup("Bảng học màn hình khóa") {
                Text(
                    if (directSpecialAccessGranted) {
                        "Quyền Hiện trên cùng đã sẵn sàng"
                    } else {
                        "Cần quyền Hiện trên cùng để One UI mở bảng học"
                    },
                    color = if (directSpecialAccessGranted) {
                        MaterialTheme.colorScheme.primary
                    } else {
                        MaterialTheme.colorScheme.error
                    },
                    fontWeight = FontWeight.SemiBold,
                )
                if (!directSpecialAccessGranted) {
                    Button(
                        onClick = {
                            directSpecialAccessLauncher.launch(
                                Intent(
                                    Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                                    Uri.parse("package:${context.packageName}"),
                                ),
                            )
                        },
                        modifier = Modifier.fillMaxWidth(),
                    ) { Text("Cấp quyền Hiện trên cùng") }
                }
                HorizontalDivider()
                SettingsSwitch(
                    title = "Lock Review",
                    subtitle = "Hiện lesson trước, rồi hỏi đủ quiz ở các lần mở màn hình sau.",
                    checked = state.settings.lockReviewEnabled,
                ) { enabled ->
                    if (
                        enabled && Build.VERSION.SDK_INT >= 33 &&
                        ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS) !=
                        PackageManager.PERMISSION_GRANTED
                    ) {
                        notificationPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
                    } else {
                        viewModel.setLockReview(enabled)
                    }
                }
                Text(
                    "Không giới hạn số thẻ mỗi ngày",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }

        item {
            SettingsGroup("Cách ôn") {
                Text("Giao diện màn hình khóa", fontWeight = FontWeight.SemiBold)
                LazyRow(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                    items(availableLockScreenThemes, key = { it.id }) { theme ->
                        ThemeChoiceCard(
                            title = theme.title,
                            subtitle = theme.subtitle,
                            selected = state.settings.lockThemeId == theme.id,
                            accent = theme.accent,
                            background = theme.background,
                            surfaceAlt = theme.surfaceAlt,
                            onClick = { viewModel.setLockThemeId(theme.id) },
                        )
                    }
                }
                HorizontalDivider()
                Text("Thời gian tự nhớ: ${state.settings.preRecallSeconds} giây", fontWeight = FontWeight.SemiBold)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    listOf(0, 3, 5).forEach { seconds ->
                        AssistChip(
                            onClick = { viewModel.setPreRecallSeconds(seconds) },
                            label = { Text("$seconds giây") },
                        )
                    }
                }
                HorizontalDivider()
                SettingsSwitch(
                    "Giải thích đáp án",
                    "Hiện lý do sau khi trả lời trên bảng khóa.",
                    state.settings.showExplanations,
                    viewModel::setShowExplanations,
                )
                SettingsSwitch(
                    "Tiêu đề trên widget",
                    "Có thể ẩn tên kiến thức khỏi widget.",
                    state.settings.widgetShowTitles,
                    viewModel::setWidgetShowTitles,
                )
            }
        }

        item {
            SettingsGroup("Thiết lập One UI · ${Build.MODEL}") {
                StatusLine("Hiện trên cùng", directSpecialAccessGranted)
                StatusLine("Notification trạng thái", notificationGranted)
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = { openSystemSettings(Settings.ACTION_APP_NOTIFICATION_SETTINGS) }) {
                        Text("Notification")
                    }
                    TextButton(onClick = { openSystemSettings(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS) }) {
                        Text("Battery")
                    }
                }
                Text(
                    "Cho phép chạy nền/không ngủ, sau đó khóa và mở máy để kiểm tra.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.bodySmall,
                )
                HorizontalDivider()
                SettingsSwitch(
                    "Đã thấy bảng học trực tiếp",
                    "Chỉ xác nhận sau khi lesson hoặc quiz thực sự xuất hiện.",
                    state.settings.testCardConfirmed,
                    viewModel::confirmTestCard,
                )
                SettingsSwitch(
                    "Đã kiểm tra One UI",
                    "Ghi nhận thiết lập trên A56 đã hoàn tất.",
                    state.settings.oneUiSetupConfirmed,
                    viewModel::confirmOneUiSetup,
                )
            }
        }

        item {
            SettingsGroup("Trạng thái hệ thống") {
                StatusLine("Lock Review", state.settings.lockReviewEnabled)
                Text("Runtime: ${state.settings.runtimeStatus}")
                Text("Service: ${if (heartbeatFresh) "đang hoạt động" else "cũ hoặc chưa chạy"}")
                Text("DB: ${diagnostic?.state?.mode ?: "—"}")
                Text("Chu kỳ hôm nay: ${diagnostic?.sessionsToday ?: 0}")
                TextButton(onClick = viewModel::refreshDiagnostics) { Text("Làm mới") }
            }
        }

        item {
            SettingsGroup("Dữ liệu trên máy") {
                Text(
                    "Ứng dụng không có quyền Internet. Nội dung và tiến độ chỉ được đọc, lưu cục bộ.",
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    style = MaterialTheme.typography.bodySmall,
                )
                TextButton(onClick = { confirmReset = true }) {
                    Text("Xóa tiến độ cục bộ", color = MaterialTheme.colorScheme.error)
                }
            }
        }
        item { Spacer(Modifier.height(12.dp)) }
    }

    if (confirmReset) {
        AlertDialog(
            onDismissRequest = { confirmReset = false },
            title = { Text("Xóa toàn bộ tiến độ?") },
            text = { Text("Lịch sử làm bài và sổ lỗi trên thiết bị sẽ bị xóa.") },
            confirmButton = {
                TextButton(onClick = {
                    viewModel.clearProgress()
                    confirmReset = false
                }) { Text("Xóa") }
            },
            dismissButton = {
                TextButton(onClick = { confirmReset = false }) { Text("Hủy") }
            },
        )
    }
}

@Composable
private fun SettingsGroup(
    title: String,
    content: @Composable ColumnScope.() -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface),
    ) {
        Column(
            modifier = Modifier.fillMaxWidth().padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(11.dp),
        ) {
            Text(title, style = MaterialTheme.typography.titleMedium)
            content()
        }
    }
}

@Composable
private fun ThemeChoiceCard(
    title: String,
    subtitle: String,
    selected: Boolean,
    accent: Color,
    background: Color,
    surfaceAlt: Color,
    onClick: () -> Unit,
) {
    Card(
        modifier = Modifier
            .width(152.dp)
            .clickable(onClick = onClick),
        colors = CardDefaults.cardColors(containerColor = background),
    ) {
        Column(
            Modifier.fillMaxWidth().padding(12.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Row(horizontalArrangement = Arrangement.spacedBy(5.dp), verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(10.dp).clip(CircleShape).background(accent))
                Text(title, color = accent, fontWeight = FontWeight.Bold, maxLines = 1)
            }
            Text(
                subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                maxLines = 1,
            )
            Box(
                Modifier
                    .fillMaxWidth()
                    .height(34.dp)
                    .clip(MaterialTheme.shapes.medium)
                    .background(surfaceAlt),
            )
            Text(
                if (selected) "Đang dùng" else "Chọn",
                color = if (selected) accent else MaterialTheme.colorScheme.onSurfaceVariant,
                fontWeight = FontWeight.SemiBold,
            )
        }
    }
}

@Composable
private fun SettingsSwitch(
    title: String,
    subtitle: String,
    checked: Boolean,
    onChange: (Boolean) -> Unit,
) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Column(Modifier.weight(1f), verticalArrangement = Arrangement.spacedBy(2.dp)) {
            Text(title, fontWeight = FontWeight.SemiBold)
            Text(
                subtitle,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
                style = MaterialTheme.typography.bodySmall,
            )
        }
        Switch(checked = checked, onCheckedChange = onChange)
    }
}

@Composable
private fun StatusLine(label: String, ready: Boolean) {
    Row(Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.SpaceBetween) {
        Text(label)
        Text(
            if (ready) "Sẵn sàng" else "Chưa có",
            color = if (ready) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.error,
            fontWeight = FontWeight.SemiBold,
        )
    }
}
