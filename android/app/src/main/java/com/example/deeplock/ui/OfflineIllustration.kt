package com.example.deeplock.ui

import android.graphics.BitmapFactory
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.gestures.detectTransformGestures
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.rounded.Close
import androidx.compose.material3.Card
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.produceState
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clipToBounds
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.asImageBitmap
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.layout.ContentScale
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.semantics.Role
import androidx.compose.ui.semantics.role
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.IntSize
import androidx.compose.ui.unit.dp
import androidx.compose.ui.window.Dialog
import androidx.compose.ui.window.DialogProperties
import com.example.deeplock.data.content.Illustration
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlin.math.min

private data class DecodeState(val complete: Boolean = false, val image: ImageBitmap? = null)

@Composable
fun OfflineIllustration(
    illustration: Illustration,
    bytes: ByteArray?,
    modifier: Modifier = Modifier,
) {
    val decoded by produceState(DecodeState(), illustration.illustration_id, bytes) {
        value = withContext(Dispatchers.Default) {
            val image = try {
                bytes?.let { decodeBounded(it) }
            } catch (_: RuntimeException) {
                null
            }
            DecodeState(complete = true, image = image)
        }
    }
    var showZoom by remember(illustration.illustration_id) { mutableStateOf(false) }

    Card(modifier.fillMaxWidth()) {
        Column {
            when {
                !decoded.complete -> Text(
                    "Đang mở hình minh họa…",
                    modifier = Modifier.padding(16.dp),
                    style = MaterialTheme.typography.bodySmall,
                )
                decoded.image != null -> Image(
                    bitmap = requireNotNull(decoded.image),
                    contentDescription = illustration.alt_text,
                    modifier = Modifier
                        .fillMaxWidth()
                        .aspectRatio((illustration.width.toFloat() / illustration.height).coerceIn(0.5f, 2.5f))
                        .semantics { role = Role.Button }
                        .clickable { showZoom = true },
                    contentScale = ContentScale.Fit,
                )
                else -> Text(
                    "Không thể hiển thị hình. ${illustration.alt_text}",
                    modifier = Modifier.padding(16.dp),
                )
            }
            Text(
                illustration.caption,
                modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 12.dp),
                style = MaterialTheme.typography.bodySmall,
            )
            if (decoded.image != null) {
                Text(
                    "Chạm hình để phóng to",
                    modifier = Modifier.padding(start = 16.dp, end = 16.dp, top = 4.dp, bottom = 12.dp),
                    color = MaterialTheme.colorScheme.primary,
                    style = MaterialTheme.typography.labelMedium,
                )
            } else {
                Box(Modifier.padding(bottom = 12.dp))
            }
        }
    }

    if (showZoom && decoded.image != null) {
        IllustrationZoomDialog(
            image = requireNotNull(decoded.image),
            altText = illustration.alt_text,
            caption = illustration.caption,
            onDismiss = { showZoom = false },
        )
    }
}

@Composable
private fun IllustrationZoomDialog(
    image: ImageBitmap,
    altText: String,
    caption: String,
    onDismiss: () -> Unit,
) {
    Dialog(
        onDismissRequest = onDismiss,
        properties = DialogProperties(usePlatformDefaultWidth = false),
    ) {
        Surface(
            modifier = Modifier.fillMaxSize(),
            color = Color.Black.copy(alpha = 0.96f),
            contentColor = Color.White,
        ) {
            var viewport by remember { mutableStateOf(IntSize.Zero) }
            var transform by remember(image) { mutableStateOf(IllustrationZoomTransform()) }

            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .background(Color.Black.copy(alpha = 0.96f)),
            ) {
                Image(
                    bitmap = image,
                    contentDescription = altText,
                    modifier = Modifier
                        .fillMaxSize()
                        .clipToBounds()
                        .onSizeChanged { size ->
                            viewport = size
                            transform = clampIllustrationZoomTransform(
                                requestedScale = transform.scale,
                                requestedOffsetX = transform.offsetX,
                                requestedOffsetY = transform.offsetY,
                                viewportWidth = size.width.toFloat(),
                                viewportHeight = size.height.toFloat(),
                                imageWidth = image.width.toFloat(),
                                imageHeight = image.height.toFloat(),
                            )
                        }
                        .pointerInput(image, viewport) {
                            detectTransformGestures { _, pan, zoom, _ ->
                                transform = clampIllustrationZoomTransform(
                                    requestedScale = transform.scale * zoom,
                                    requestedOffsetX = transform.offsetX + pan.x,
                                    requestedOffsetY = transform.offsetY + pan.y,
                                    viewportWidth = viewport.width.toFloat(),
                                    viewportHeight = viewport.height.toFloat(),
                                    imageWidth = image.width.toFloat(),
                                    imageHeight = image.height.toFloat(),
                                )
                            }
                        }
                        .graphicsLayer {
                            scaleX = transform.scale
                            scaleY = transform.scale
                            translationX = transform.offsetX
                            translationY = transform.offsetY
                        },
                    contentScale = ContentScale.Fit,
                )

                Row(
                    modifier = Modifier
                        .align(Alignment.TopCenter)
                        .fillMaxWidth()
                        .statusBarsPadding()
                        .padding(horizontal = 12.dp, vertical = 8.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                    verticalAlignment = Alignment.CenterVertically,
                ) {
                    Text(
                        if (transform.scale > MIN_ZOOM_SCALE) {
                            "${transform.scale.toInt()}× • Kéo để xem"
                        } else {
                            "Chụm hai ngón để phóng to"
                        },
                        style = MaterialTheme.typography.labelLarge,
                    )
                    IconButton(onClick = onDismiss) {
                        Icon(Icons.Rounded.Close, contentDescription = "Đóng hình minh họa")
                    }
                }

                Text(
                    caption,
                    modifier = Modifier
                        .align(Alignment.BottomCenter)
                        .fillMaxWidth()
                        .background(Color.Black.copy(alpha = 0.72f))
                        .padding(horizontal = 20.dp, vertical = 16.dp),
                    style = MaterialTheme.typography.bodySmall,
                )
            }
        }
    }
}

internal data class IllustrationZoomTransform(
    val scale: Float = MIN_ZOOM_SCALE,
    val offsetX: Float = 0f,
    val offsetY: Float = 0f,
)

internal fun clampIllustrationZoomTransform(
    requestedScale: Float,
    requestedOffsetX: Float,
    requestedOffsetY: Float,
    viewportWidth: Float,
    viewportHeight: Float,
    imageWidth: Float,
    imageHeight: Float,
): IllustrationZoomTransform {
    val scale = requestedScale
        .takeIf(Float::isFinite)
        ?.coerceIn(MIN_ZOOM_SCALE, MAX_ZOOM_SCALE)
        ?: MIN_ZOOM_SCALE
    if (
        viewportWidth <= 0f || viewportHeight <= 0f ||
        imageWidth <= 0f || imageHeight <= 0f
    ) {
        return IllustrationZoomTransform(scale = scale)
    }

    val fit = min(viewportWidth / imageWidth, viewportHeight / imageHeight)
    val maxOffsetX = ((imageWidth * fit * scale - viewportWidth) / 2f).coerceAtLeast(0f)
    val maxOffsetY = ((imageHeight * fit * scale - viewportHeight) / 2f).coerceAtLeast(0f)
    val safeOffsetX = requestedOffsetX.takeIf(Float::isFinite) ?: 0f
    val safeOffsetY = requestedOffsetY.takeIf(Float::isFinite) ?: 0f
    return IllustrationZoomTransform(
        scale = scale,
        offsetX = if (maxOffsetX == 0f) 0f else safeOffsetX.coerceIn(-maxOffsetX, maxOffsetX),
        offsetY = if (maxOffsetY == 0f) 0f else safeOffsetY.coerceIn(-maxOffsetY, maxOffsetY),
    )
}

private fun decodeBounded(bytes: ByteArray): ImageBitmap? {
    val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
    BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
    if (bounds.outWidth <= 0 || bounds.outHeight <= 0) return null
    var sampleSize = 1
    while (bounds.outWidth / sampleSize > MAX_DECODE_WIDTH || bounds.outHeight / sampleSize > MAX_DECODE_HEIGHT) {
        sampleSize *= 2
    }
    val options = BitmapFactory.Options().apply { inSampleSize = sampleSize }
    return BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options)?.asImageBitmap()
}

private const val MAX_DECODE_WIDTH = 1_600
private const val MAX_DECODE_HEIGHT = 2_000
private const val MIN_ZOOM_SCALE = 1f
private const val MAX_ZOOM_SCALE = 5f
