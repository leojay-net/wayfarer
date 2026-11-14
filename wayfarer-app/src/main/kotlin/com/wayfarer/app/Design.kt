package com.wayfarer.app

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.LinearEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.Spring
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.animateFloatAsState
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.spring
import androidx.compose.animation.core.tween
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.Image
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.interaction.MutableInteractionSource
import androidx.compose.foundation.interaction.collectIsPressedAsState
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.aspectRatio
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.BasicText
import androidx.compose.foundation.text.BasicTextField
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.draw.clip
import androidx.compose.ui.draw.drawBehind
import androidx.compose.ui.draw.scale
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.graphicsLayer
import androidx.compose.animation.AnimatedVisibility
import androidx.compose.animation.core.Animatable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.composed
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.focus.onFocusChanged
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.geometry.Size
import androidx.compose.ui.graphics.Brush
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.ImageBitmap
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.SolidColor
import androidx.compose.ui.graphics.StrokeCap
import androidx.compose.ui.graphics.StrokeJoin
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.Dp
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

/**
 * Wayfarer design system.
 *
 * One dark, near-black canvas; one accent (signal mint) used only for the primary action, live state and success;
 * everything else is a ladder of neutral surfaces separated by hairlines rather than shadows. Type is a single
 * family with a strict scale: large figures are light and tight, labels are small, spaced capitals.
 */
object Wf {
    val Bg = Color(0xFF050608)
    val Surface = Color(0xFF0E1116)
    val Raised = Color(0xFF161A21)
    val Line = Color(0xFF232935)
    val Teal = Color(0xFF12B58C)
    val Glass = Color(0xFF131820)
    val Text = Color(0xFFF5F6F8)
    val Dim = Color(0xFF8A93A3)
    val Faint = Color(0xFF596070)
    val Mint = Color(0xFF4BF0B0)
    val MintInk = Color(0xFF06231A)
    val MintWash = Color(0xFF0E2A22)
    val Amber = Color(0xFFFFB84D)
    val AmberWash = Color(0xFF2B2111)
    val Red = Color(0xFFFF6B6B)
    val RedWash = Color(0xFF2B1517)

    val Radius = 28.dp
    val RadiusSmall = 18.dp
}

enum class Tone { Neutral, Live, Warn, Bad }

/** Single text primitive so the type scale lives in one place. */
@Composable
fun T(
    text: String,
    size: Int = 15,
    weight: FontWeight = FontWeight.Normal,
    color: Color = Wf.Text,
    spacing: Double = 0.0,
    mono: Boolean = false,
    align: TextAlign = TextAlign.Start,
    modifier: Modifier = Modifier,
    lineHeight: Int = 0,
) = BasicText(
    text,
    modifier,
    style = TextStyle(
        color = color,
        fontSize = size.sp,
        fontWeight = weight,
        letterSpacing = spacing.sp,
        fontFamily = if (mono) FontFamily.Monospace else FontFamily.Default,
        textAlign = align,
        lineHeight = if (lineHeight > 0) lineHeight.sp else TextStyle.Default.lineHeight,
    ),
)

@Composable
fun Eyebrow(text: String, color: Color = Wf.Dim) = T(text.uppercase(), 11, FontWeight.SemiBold, color, 1.4)

/** The page: near-black canvas with two slow-drifting mint and teal auroras behind everything. */
@Composable
fun Page(modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    val t = rememberInfiniteTransition(label = "aurora")
    val a by t.animateFloat(0f, 1f, infiniteRepeatable(tween(9000, easing = LinearEasing), RepeatMode.Reverse), label = "a")
    val b by t.animateFloat(0f, 1f, infiniteRepeatable(tween(13000, easing = LinearEasing), RepeatMode.Reverse), label = "b")
    Box(
        modifier
            .fillMaxSize()
            .background(Wf.Bg)
            .drawBehind {
                drawRect(
                    Brush.radialGradient(
                        listOf(Wf.Mint.copy(alpha = 0.16f), Color.Transparent),
                        center = Offset(size.width * (0.15f + 0.5f * a), -size.height * 0.02f + size.height * 0.05f * b),
                        radius = size.width * 1.0f,
                    ),
                )
                drawRect(
                    Brush.radialGradient(
                        listOf(Wf.Teal.copy(alpha = 0.10f), Color.Transparent),
                        center = Offset(size.width * (0.95f - 0.5f * b), size.height * (0.82f + 0.06f * a)),
                        radius = size.width * 0.9f,
                    ),
                )
            },
    ) { content() }
}

/** Staggered entrance: fades and rises into place. Give each block a growing [index]. */
fun Modifier.appear(index: Int = 0, key: Any? = Unit): Modifier = composed {
    val p = remember(key) { Animatable(0f) }
    LaunchedEffect(key) { p.animateTo(1f, tween(650, delayMillis = 70 * index, easing = FastOutSlowInEasing)) }
    graphicsLayer {
        alpha = p.value
        translationY = (1f - p.value) * 36.dp.toPx()
    }
}

/** Mint glow under an element: colored shadow, visible on API 28+. */
fun Modifier.glow(radius: Dp = 22.dp, shape: androidx.compose.ui.graphics.Shape = RoundedCornerShape(Wf.Radius), color: Color = Wf.Mint, alpha: Float = 0.55f): Modifier =
    shadow(radius, shape, clip = false, ambientColor = color.copy(alpha = alpha), spotColor = color.copy(alpha = alpha))

/** The mark: a W drawn as a route with a waypoint above its middle peak. Same geometry as the launcher icon. */
@Composable
fun Logo(size: Dp, tint: Color = Wf.Mint, dot: Color = Wf.Text) {
    Canvas(Modifier.size(size)) {
        val u = this.size.minDimension / 54f // the mark lives in a 54-unit box
        val p = Path().apply {
            moveTo(2f * u, 14f * u)
            lineTo(13f * u, 41f * u)
            lineTo(25f * u, 22f * u)
            lineTo(37f * u, 41f * u)
            lineTo(48f * u, 14f * u)
        }
        drawPath(p, tint, style = Stroke(6.5f * u, cap = StrokeCap.Round, join = StrokeJoin.Round))
        drawCircle(dot, 4f * u, Offset(25f * u, 4f * u))
    }
}

@Composable
fun Wordmark() {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(10.dp)) {
        Logo(26.dp)
        T("Wayfarer", 19, FontWeight.SemiBold, spacing = -0.3)
    }
}

@Composable
fun Pill(text: String, tone: Tone = Tone.Neutral) {
    val (fg, bg) = when (tone) {
        Tone.Neutral -> Wf.Dim to Wf.Raised
        Tone.Live -> Wf.Mint to Wf.MintWash
        Tone.Warn -> Wf.Amber to Wf.AmberWash
        Tone.Bad -> Wf.Red to Wf.RedWash
    }
    Row(
        Modifier.clip(CircleShape).background(bg).border(1.dp, fg.copy(alpha = 0.25f), CircleShape).padding(horizontal = 14.dp, vertical = 7.dp),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(7.dp),
    ) {
        if (tone != Tone.Neutral) Box(Modifier.size(6.dp).clip(CircleShape).background(fg))
        T(text, 12, FontWeight.Medium, fg, 0.2)
    }
}

/** A grouped surface: glass gradient, light-catching top edge, deep soft shadow. */
@Composable
fun Card(modifier: Modifier = Modifier, pad: Dp = 20.dp, content: @Composable () -> Unit) {
    val shape = RoundedCornerShape(Wf.Radius)
    Column(
        modifier
            .fillMaxWidth()
            .shadow(18.dp, shape, clip = false, ambientColor = Color.Black, spotColor = Color.Black)
            .clip(shape)
            .background(Brush.verticalGradient(listOf(Wf.Glass, Wf.Surface)))
            .border(1.dp, Brush.verticalGradient(listOf(Color.White.copy(alpha = 0.14f), Color.White.copy(alpha = 0.03f))), shape)
            .padding(pad),
    ) { content() }
}

@Composable
private fun pressScale(source: MutableInteractionSource): Float {
    val pressed by source.collectIsPressedAsState()
    val s by animateFloatAsState(
        if (pressed) 0.97f else 1f,
        spring(dampingRatio = Spring.DampingRatioMediumBouncy, stiffness = Spring.StiffnessMedium),
        label = "press",
    )
    return s
}

@Composable
fun PrimaryButton(text: String, enabled: Boolean = true, onClick: () -> Unit) {
    val source = remember { MutableInteractionSource() }
    val shape = RoundedCornerShape(Wf.RadiusSmall + 6.dp)
    val t = rememberInfiniteTransition(label = "shimmer")
    val sweep by t.animateFloat(-0.4f, 1.4f, infiniteRepeatable(tween(2800, easing = LinearEasing), RepeatMode.Restart), label = "sweep")
    Box(
        Modifier
            .fillMaxWidth()
            .scale(pressScale(source))
            .then(if (enabled) Modifier.glow(20.dp, shape) else Modifier)
            .height(60.dp)
            .clip(shape)
            .background(if (enabled) Brush.horizontalGradient(listOf(Wf.Mint, Color(0xFF2FE0B4), Wf.Teal)) else SolidColor(Wf.Raised))
            .drawBehind {
                if (enabled) {
                    val x = size.width * sweep
                    drawRect(
                        Brush.horizontalGradient(
                            listOf(Color.Transparent, Color.White.copy(alpha = 0.28f), Color.Transparent),
                            startX = x - size.width * 0.18f, endX = x + size.width * 0.18f,
                        ),
                    )
                }
            }
            .clickable(interactionSource = source, indication = null, enabled = enabled, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) { T(text, 17, FontWeight.SemiBold, if (enabled) Wf.MintInk else Wf.Faint, -0.1) }
}

@Composable
fun SecondaryButton(text: String, onClick: () -> Unit) {
    val source = remember { MutableInteractionSource() }
    val shape = RoundedCornerShape(Wf.RadiusSmall + 6.dp)
    Box(
        Modifier
            .fillMaxWidth()
            .scale(pressScale(source))
            .shadow(10.dp, shape, clip = false, ambientColor = Color.Black, spotColor = Color.Black)
            .height(56.dp)
            .clip(shape)
            .background(Brush.verticalGradient(listOf(Wf.Raised, Wf.Surface)))
            .border(1.dp, Brush.verticalGradient(listOf(Color.White.copy(alpha = 0.16f), Color.White.copy(alpha = 0.04f))), shape)
            .clickable(interactionSource = source, indication = null, onClick = onClick),
        contentAlignment = Alignment.Center,
    ) { T(text, 16, FontWeight.Medium) }
}

@Composable
fun TextAction(text: String, color: Color = Wf.Dim, onClick: () -> Unit) {
    Box(Modifier.clickable(indication = null, interactionSource = remember { MutableInteractionSource() }, onClick = onClick).padding(vertical = 10.dp, horizontal = 4.dp)) {
        T(text, 14, FontWeight.Medium, color)
    }
}

@Composable
fun TopBar(title: String, onBack: (() -> Unit)?, trailing: @Composable () -> Unit = {}) {
    Row(Modifier.fillMaxWidth().height(56.dp), verticalAlignment = Alignment.CenterVertically) {
        Box(Modifier.width(76.dp), contentAlignment = Alignment.CenterStart) {
            if (onBack != null) {
                Box(
                    Modifier.size(40.dp).clip(CircleShape).background(Wf.Surface).border(1.dp, Wf.Line, CircleShape)
                        .clickable(indication = null, interactionSource = remember { MutableInteractionSource() }, onClick = onBack),
                    contentAlignment = Alignment.Center,
                ) {
                    Canvas(Modifier.size(16.dp)) {
                        val p = Path().apply {
                            moveTo(size.width * 0.68f, size.height * 0.1f)
                            lineTo(size.width * 0.28f, size.height * 0.5f)
                            lineTo(size.width * 0.68f, size.height * 0.9f)
                        }
                        drawPath(p, Wf.Text, style = Stroke(2.2.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round))
                    }
                }
            }
        }
        Box(Modifier.weight(1f), contentAlignment = Alignment.Center) { T(title, 16, FontWeight.SemiBold, spacing = -0.1) }
        Box(Modifier.width(76.dp), contentAlignment = Alignment.CenterEnd) { trailing() }
    }
}

@Composable
fun Field(label: String, value: String, mono: Boolean = false, keyboard: KeyboardType = KeyboardType.Text, onChange: (String) -> Unit) {
    var focused by remember { mutableStateOf(false) }
    Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
        Eyebrow(label)
        Box(
            Modifier
                .fillMaxWidth()
                .clip(RoundedCornerShape(Wf.RadiusSmall))
                .background(Wf.Raised)
                .border(1.dp, if (focused) Wf.Mint.copy(alpha = 0.7f) else Wf.Line, RoundedCornerShape(Wf.RadiusSmall))
                .padding(horizontal = 16.dp, vertical = 15.dp),
        ) {
            BasicTextField(
                value,
                onChange,
                modifier = Modifier.fillMaxWidth().onFocusChanged { focused = it.isFocused },
                textStyle = TextStyle(
                    color = Wf.Text,
                    fontSize = 16.sp,
                    fontFamily = if (mono) FontFamily.Monospace else FontFamily.Default,
                ),
                cursorBrush = SolidColor(Wf.Mint),
                singleLine = true,
                keyboardOptions = KeyboardOptions(keyboardType = keyboard),
            )
        }
    }
}

@Composable
fun DetailRow(label: String, value: String, mono: Boolean = false) {
    Row(Modifier.fillMaxWidth().padding(vertical = 11.dp), horizontalArrangement = Arrangement.SpaceBetween) {
        T(label, 14, color = Wf.Dim)
        Spacer(Modifier.width(16.dp))
        T(value, 14, FontWeight.Medium, mono = mono, align = TextAlign.End, modifier = Modifier.weight(1f, fill = false))
    }
}

@Composable
fun Hairline() = Box(Modifier.fillMaxWidth().height(1.dp).background(Wf.Line))

/** Naira (or other fiat) figure: large, light, tight; the symbol sits smaller and dimmer on the baseline. */
@Composable
fun Amount(symbol: String, figure: String, size: Int = 56, dim: Boolean = false) {
    Row(verticalAlignment = Alignment.Bottom, horizontalArrangement = Arrangement.Center) {
        T(symbol, (size * 0.5).toInt(), FontWeight.Light, Wf.Dim, modifier = Modifier.padding(bottom = (size * 0.1).dp, end = 6.dp))
        T(figure, size, FontWeight.Light, if (dim) Wf.Faint else Wf.Text, -(size * 0.03), lineHeight = (size * 1.1).toInt())
    }
}

/** POS keypad. Digits, a decimal point and backspace, drawn rather than bitmap icons. */
@Composable
fun Keypad(onKey: (String) -> Unit) {
    val rows = listOf(listOf("1", "2", "3"), listOf("4", "5", "6"), listOf("7", "8", "9"), listOf(".", "0", "<"))
    Column(verticalArrangement = Arrangement.spacedBy(10.dp)) {
        rows.forEach { row ->
            Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
                row.forEach { key ->
                    val source = remember { MutableInteractionSource() }
                    val pressed by source.collectIsPressedAsState()
                    val keyScale by animateFloatAsState(if (pressed) 0.92f else 1f, spring(Spring.DampingRatioMediumBouncy, Spring.StiffnessMedium), label = "key")
                    val keyShape = RoundedCornerShape(Wf.RadiusSmall)
                    Box(
                        Modifier
                            .weight(1f)
                            .scale(keyScale)
                            .height(60.dp)
                            .clip(keyShape)
                            .background(if (pressed) Brush.verticalGradient(listOf(Wf.MintWash, Wf.Surface)) else Brush.verticalGradient(listOf(Wf.Glass, Wf.Surface)))
                            .border(1.dp, if (pressed) Wf.Mint.copy(alpha = 0.5f) else Color.White.copy(alpha = 0.07f), keyShape)
                            .clickable(interactionSource = source, indication = null) { onKey(key) },
                        contentAlignment = Alignment.Center,
                    ) {
                        if (key == "<") {
                            Canvas(Modifier.size(26.dp, 20.dp)) {
                                val w = size.width
                                val h = size.height
                                val outline = Path().apply {
                                    moveTo(w * 0.34f, 0f); lineTo(w, 0f); lineTo(w, h); lineTo(w * 0.34f, h); lineTo(0f, h / 2); close()
                                }
                                drawPath(outline, Wf.Dim, style = Stroke(1.8.dp.toPx(), join = StrokeJoin.Round))
                                drawLine(Wf.Dim, Offset(w * 0.52f, h * 0.3f), Offset(w * 0.78f, h * 0.7f), 1.8.dp.toPx(), StrokeCap.Round)
                                drawLine(Wf.Dim, Offset(w * 0.78f, h * 0.3f), Offset(w * 0.52f, h * 0.7f), 1.8.dp.toPx(), StrokeCap.Round)
                            }
                        } else {
                            T(key, 28, FontWeight.Light)
                        }
                    }
                }
            }
        }
    }
}

/** Concentric rings that breathe outward: the phone is listening for a tap. */
@Composable
fun Radar(size: Dp = 220.dp) {
    val transition = rememberInfiniteTransition(label = "radar")
    val phase by transition.animateFloat(
        0f, 1f, infiniteRepeatable(tween(2600, easing = LinearEasing), RepeatMode.Restart), label = "phase",
    )
    Box(Modifier.size(size), contentAlignment = Alignment.Center) {
        Canvas(Modifier.fillMaxSize()) {
            val max = this.size.minDimension / 2
            for (i in 0 until 3) {
                val t = (phase + i / 3f) % 1f
                val r = max * (0.34f + 0.66f * FastOutSlowInEasing.transform(t))
                drawCircle(Wf.Mint.copy(alpha = (1f - t) * 0.38f), r, style = Stroke(1.5.dp.toPx()))
            }
            drawCircle(Wf.MintWash, max * 0.34f)
            drawCircle(Wf.Mint.copy(alpha = 0.6f), max * 0.34f, style = Stroke(1.5.dp.toPx()))
        }
        Logo(34.dp)
    }
}

/** Success mark: a ring that draws itself, then the check. */
@Composable
fun SuccessMark(size: Dp = 96.dp, tone: Color = Wf.Mint) {
    val progress by animateFloatAsState(1f, tween(700, easing = FastOutSlowInEasing), label = "success")
    Canvas(Modifier.size(size).graphicsLayer { scaleX = 0.8f + 0.2f * progress; scaleY = 0.8f + 0.2f * progress }.glow(36.dp, CircleShape, tone, 0.5f * progress)) {
        val stroke = 3.dp.toPx()
        val inset = stroke
        drawCircle(tone.copy(alpha = 0.12f), this.size.minDimension / 2)
        drawArc(
            tone, -90f, 360f * progress, false,
            Offset(inset, inset), Size(this.size.width - inset * 2, this.size.height - inset * 2),
            style = Stroke(stroke, cap = StrokeCap.Round),
        )
        val w = this.size.width
        val check = Path().apply {
            moveTo(w * 0.31f, w * 0.52f)
            lineTo(w * 0.45f, w * 0.65f)
            lineTo(w * 0.70f, w * 0.37f)
        }
        val checkAlpha = ((progress - 0.55f) / 0.45f).coerceIn(0f, 1f)
        drawPath(check, tone.copy(alpha = checkAlpha), style = Stroke(stroke * 1.4f, cap = StrokeCap.Round, join = StrokeJoin.Round))
    }
}

@Composable
fun QrCard(image: ImageBitmap, caption: String) {
    Column(horizontalAlignment = Alignment.CenterHorizontally, verticalArrangement = Arrangement.spacedBy(14.dp)) {
        Box(
            Modifier
                .fillMaxWidth(0.82f)
                .aspectRatio(1f)
                .glow(34.dp, RoundedCornerShape(32.dp), alpha = 0.45f)
                .clip(RoundedCornerShape(32.dp))
                .background(Color.White)
                .padding(18.dp),
        ) { Image(image, "QR code", Modifier.fillMaxSize()) }
        T(caption, 13, color = Wf.Dim, align = TextAlign.Center)
    }
}

/** Slim banner for transient messages. Tap to dismiss. */
@Composable
fun Banner(text: String, tone: Tone = Tone.Neutral, onDismiss: () -> Unit) {
    val (fg, bg) = when (tone) {
        Tone.Bad -> Wf.Red to Wf.RedWash
        Tone.Warn -> Wf.Amber to Wf.AmberWash
        Tone.Live -> Wf.Mint to Wf.MintWash
        Tone.Neutral -> Wf.Text to Wf.Raised
    }
    Row(
        Modifier
            .fillMaxWidth()
            .clip(RoundedCornerShape(Wf.RadiusSmall))
            .background(bg)
            .border(1.dp, fg.copy(alpha = 0.25f), RoundedCornerShape(Wf.RadiusSmall))
            .clickable(indication = null, interactionSource = remember { MutableInteractionSource() }, onClick = onDismiss)
            .padding(horizontal = 16.dp, vertical = 14.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) { T(text, 14, color = fg, lineHeight = 20, modifier = Modifier.weight(1f)) }
}

/** Honest status strip shown on every screen: this is a testnet prototype. */
@Composable
fun TestnetStrip() {
    Row(
        Modifier.fillMaxWidth().alpha(0.9f),
        horizontalArrangement = Arrangement.Center,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Box(Modifier.size(5.dp).clip(CircleShape).background(Wf.Amber))
        Spacer(Modifier.width(8.dp))
        T("Testnet prototype  ·  unaudited  ·  demo rate", 11, FontWeight.Medium, Wf.Faint, 0.4)
    }
}

/** The hero: a vault card with a drifting route line, the account it settles from and the per-tap bound. */
@Composable
fun VaultCard(account: String, limit: String, modifier: Modifier = Modifier) {
    val shape = RoundedCornerShape(30.dp)
    val t = rememberInfiniteTransition(label = "vault")
    val drift by t.animateFloat(0f, 1f, infiniteRepeatable(tween(6000, easing = LinearEasing), RepeatMode.Reverse), label = "drift")
    Box(
        modifier
            .fillMaxWidth()
            .glow(30.dp, shape, alpha = 0.30f)
            .clip(shape)
            .background(Brush.linearGradient(listOf(Color(0xFF0F3B30), Color(0xFF0A1C1A), Color(0xFF0B0F13))))
            .border(1.dp, Brush.linearGradient(listOf(Wf.Mint.copy(alpha = 0.55f), Color.White.copy(alpha = 0.04f))), shape)
            .drawBehind {
                // drifting highlight
                drawCircle(
                    Brush.radialGradient(listOf(Wf.Mint.copy(alpha = 0.30f), Color.Transparent), center = Offset(size.width * (0.75f + 0.15f * drift), size.height * 0.1f), radius = size.width * 0.55f),
                    radius = size.width * 0.55f, center = Offset(size.width * (0.75f + 0.15f * drift), size.height * 0.1f),
                )
                // route line with waypoints, echoing the logo
                val path = Path().apply {
                    moveTo(size.width * 0.66f, size.height * 0.80f)
                    cubicTo(size.width * 0.76f, size.height * 0.52f, size.width * 0.86f, size.height * 0.85f, size.width * 0.94f, size.height * 0.50f)
                }
                drawPath(path, Wf.Mint.copy(alpha = 0.28f), style = Stroke(2.dp.toPx(), cap = StrokeCap.Round))
                drawCircle(Wf.Mint.copy(alpha = 0.8f), 4.dp.toPx(), Offset(size.width * 0.94f, size.height * 0.50f))
            }
            .padding(24.dp),
    ) {
        Column {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Logo(24.dp)
                Spacer(Modifier.width(10.dp))
                T("Wayfarer Vault", 14, FontWeight.SemiBold, Wf.Text, 0.2)
                Spacer(Modifier.weight(1f))
                Pill("Testnet", Tone.Live)
            }
            Spacer(Modifier.height(30.dp))
            Eyebrow("Pays up to", Wf.Mint.copy(alpha = 0.8f))
            Spacer(Modifier.height(6.dp))
            Row(verticalAlignment = Alignment.Bottom) {
                T(limit, 44, FontWeight.Light, Wf.Text, -1.2, lineHeight = 48)
                Spacer(Modifier.width(8.dp))
                T("NEAR per tap", 15, FontWeight.Medium, Wf.Dim, modifier = Modifier.padding(bottom = 8.dp))
            }
            Spacer(Modifier.height(18.dp))
            T(account.ifEmpty { "Vault not linked yet" }, 12, FontWeight.Medium, Wf.Dim, 0.3, mono = true)
        }
    }
}

/** Drawn role icons so the app carries no bitmap assets. */
@Composable
fun RoleIcon(buyer: Boolean, size: Dp = 28.dp, tint: Color = Wf.Mint) {
    Canvas(Modifier.size(size)) {
        val w = this.size.width
        val st = Stroke(2.2.dp.toPx(), cap = StrokeCap.Round, join = StrokeJoin.Round)
        if (buyer) {
            // fingerprint-like concentric arcs
            for (i in 1..3) {
                val r = w * (0.14f + 0.12f * i)
                drawArc(tint.copy(alpha = 1f - 0.22f * (i - 1)), 200f, 220f, false, Offset(w / 2 - r, w / 2 - r), Size(r * 2, r * 2), style = st)
            }
            drawCircle(tint, 2.2.dp.toPx(), Offset(w / 2, w / 2))
        } else {
            // storefront: awning + door
            val awn = Path().apply { moveTo(w * 0.1f, w * 0.38f); lineTo(w * 0.2f, w * 0.14f); lineTo(w * 0.8f, w * 0.14f); lineTo(w * 0.9f, w * 0.38f); close() }
            drawPath(awn, tint, style = st)
            drawLine(tint, Offset(w * 0.18f, w * 0.38f), Offset(w * 0.18f, w * 0.86f), st.width, StrokeCap.Round)
            drawLine(tint, Offset(w * 0.82f, w * 0.38f), Offset(w * 0.82f, w * 0.86f), st.width, StrokeCap.Round)
            drawLine(tint, Offset(w * 0.18f, w * 0.86f), Offset(w * 0.82f, w * 0.86f), st.width, StrokeCap.Round)
            drawRoundRect(tint, Offset(w * 0.42f, w * 0.56f), Size(w * 0.16f, w * 0.3f), androidx.compose.ui.geometry.CornerRadius(3.dp.toPx()), style = st)
        }
    }
}

@Composable
fun IconTile(content: @Composable () -> Unit) {
    val shape = RoundedCornerShape(18.dp)
    Box(
        Modifier.size(56.dp).clip(shape)
            .background(Brush.verticalGradient(listOf(Wf.MintWash, Color(0xFF0A1A16))))
            .border(1.dp, Wf.Mint.copy(alpha = 0.30f), shape),
        contentAlignment = Alignment.Center,
    ) { content() }
}
