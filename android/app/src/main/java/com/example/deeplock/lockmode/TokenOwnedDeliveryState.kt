package com.example.deeplock.lockmode

/**
 * Keeps an unconfirmed direct-delivery request separate from the request that
 * UI callbacks are allowed to act on. A fresh token always hides the previous
 * card until that exact token is accepted by the persisted-session handshake.
 */
internal data class TokenOwnedDeliveryState<T>(
    val pending: TokenOwnedValue<T>? = null,
    val visible: TokenOwnedValue<T>? = null,
) {
    fun receive(
        token: String,
        value: T,
        alreadyAccepted: Boolean,
    ): TokenOwnedDeliveryState<T>? {
        val currentPending = pending
        if (currentPending != null &&
            currentPending.token == token &&
            currentPending.value != value
        ) return null
        val incoming = TokenOwnedValue(token, value)
        return TokenOwnedDeliveryState(
            pending = incoming,
            visible = incoming.takeIf { alreadyAccepted },
        )
    }

    fun accept(token: String, value: T): TokenOwnedDeliveryState<T> {
        val current = pending?.takeIf { it.token == token && it.value == value } ?: return this
        return copy(visible = current)
    }

    fun ownsPending(token: String, value: T): Boolean =
        pending?.let { it.token == token && it.value == value } == true

    fun ownsVisible(token: String, value: T): Boolean {
        val currentPending = pending ?: return false
        val currentVisible = visible ?: return false
        return currentPending.token == token &&
            currentPending.value == value &&
            currentVisible.token == token &&
            currentVisible.value == value
    }
}

internal data class TokenOwnedValue<T>(
    val token: String,
    val value: T,
)
