package com.example.deeplock.data

const val DEFAULT_DIRECT_PRIMARY_ENABLED = true
const val DEFAULT_PUBLIC_LOCK_CONTENT = true

/**
 * Direct delivery is now the only learning surface. Ignore a legacy false value
 * so an in-place upgrade from notification-first builds immediately shows the
 * requested board without deleting any study data.
 */
fun resolveDirectPrimaryEnabled(@Suppress("UNUSED_PARAMETER") persistedValue: Boolean?): Boolean =
    DEFAULT_DIRECT_PRIMARY_ENABLED

/** Legacy privacy toggles must not turn the requested study board into a placeholder after upgrade. */
fun resolvePublicLockContent(@Suppress("UNUSED_PARAMETER") persistedValue: Boolean?): Boolean =
    DEFAULT_PUBLIC_LOCK_CONTENT
