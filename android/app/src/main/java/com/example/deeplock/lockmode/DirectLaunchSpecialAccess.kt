package com.example.deeplock.lockmode

import android.content.Context
import android.provider.Settings
import dagger.hilt.android.qualifiers.ApplicationContext
import javax.inject.Inject
import javax.inject.Singleton

/**
 * Single injectable boundary around Android's user-granted "appear on top"
 * access. Keeping this check outside the launcher makes the delivery policy
 * independently testable without an Android Context.
 */
@Singleton
class DirectLaunchSpecialAccess @Inject constructor(
    @ApplicationContext private val context: Context,
) {
    fun isGranted(): Boolean = Settings.canDrawOverlays(context)
}
