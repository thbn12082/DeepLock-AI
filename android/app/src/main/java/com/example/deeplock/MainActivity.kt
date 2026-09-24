package com.example.deeplock

import android.content.Intent
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.mutableStateOf
import androidx.lifecycle.lifecycleScope
import com.example.deeplock.app.DeepLinkRequest
import com.example.deeplock.app.DeepLockApp
import com.example.deeplock.data.StudyCoordinator
import com.example.deeplock.lockmode.LockNotificationController
import com.example.deeplock.ui.DashboardTheme
import dagger.hilt.android.AndroidEntryPoint
import javax.inject.Inject
import kotlinx.coroutines.launch

@AndroidEntryPoint
class MainActivity : ComponentActivity() {
    @Inject lateinit var coordinator: StudyCoordinator
    @Inject lateinit var notifications: LockNotificationController
    private val deepLink = mutableStateOf<DeepLinkRequest?>(null)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        acceptIntent(intent)
        setContent {
            DashboardTheme {
                Surface(color = MaterialTheme.colorScheme.background) {
                    DeepLockApp(deepLink = deepLink.value)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        acceptIntent(intent)
    }

    private fun acceptIntent(intent: Intent?) {
        val destination = intent?.getStringExtra(LockNotificationController.EXTRA_DESTINATION)
        val packId = intent?.getStringExtra(LockNotificationController.EXTRA_PACK_ID)
        val atomId = intent?.getStringExtra(LockNotificationController.EXTRA_ATOM_ID)
        val questionId = intent?.getStringExtra(LockNotificationController.EXTRA_QUESTION_ID)
        val sessionId = intent?.getStringExtra(LockNotificationController.EXTRA_UNLOCK_SESSION_ID)
        deepLink.value = if (destination != null || atomId != null) {
            DeepLinkRequest(destination.orEmpty(), packId, atomId, questionId, sessionId, System.nanoTime())
        } else null
        if (sessionId != null) {
            notifications.cancelLearning()
            lifecycleScope.launch { coordinator.markNotificationOpened(sessionId) }
        }
    }
}
