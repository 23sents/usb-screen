package com.example.usbscreen

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.text.SpannableString
import android.text.Spanned
import android.text.style.ForegroundColorSpan
import android.view.View
import android.view.ViewGroup
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {
    private val reqProjection = 10
    private var controlView: TextView? = null
    private var streamView: TextView? = null
    private var pinView: TextView? = null
    private val ticker = object : Runnable {
        override fun run() {
            updateStatus()
            pinView?.postDelayed(this, 2000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        buildUi()
        if (Build.VERSION.SDK_INT >= 33 &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED
        ) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 1)
    }

    override fun onResume() {
        super.onResume()
        ticker.run()
    }

    override fun onPause() {
        pinView?.removeCallbacks(ticker)
        super.onPause()
    }

    private fun dotText(color: Int, label: String): CharSequence {
        val s = SpannableString("●  $label")
        s.setSpan(ForegroundColorSpan(color), 0, 1, Spanned.SPAN_EXCLUSIVE_EXCLUSIVE)
        return s
    }

    private fun updateStatus() {
        val green = Color.rgb(48, 209, 88)
        val red = Color.rgb(255, 69, 58)
        val gray = Color.rgb(142, 142, 147)
        val ctl = RemoteControlService.instance != null
        val on = StreamService.running
        controlView?.text = if (ctl) dotText(green, "Управление включено") else dotText(red, "Управление выключено")
        streamView?.text = if (on) dotText(green, "Трансляция идёт") else dotText(gray, "Трансляция остановлена")
        pinView?.text = if (on && StreamService.pin.length == 6)
            StreamService.pin.substring(0, 3) + "  " + StreamService.pin.substring(3) else "—  —  —"
    }

    private fun label(text: String, sp: Float, alpha: Int = 255, bold: Boolean = false): TextView =
        TextView(this).apply {
            this.text = text
            textSize = sp
            setTextColor(Color.argb(alpha, 255, 255, 255))
            typeface = Typeface.create(if (bold) "sans-serif-medium" else "sans-serif", Typeface.NORMAL)
        }

    private fun lp(top: Int = 0) = LinearLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
    ).apply { topMargin = dp(top.toFloat()) }

    private fun buildUi() {
        window.statusBarColor = Color.rgb(8, 12, 28)
        window.navigationBarColor = Color.rgb(24, 14, 48)

        val frame = FrameLayout(this)
        frame.addView(BackgroundView(this), FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))

        val col = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20f), dp(28f), dp(20f), dp(28f))
        }

        col.addView(label("USB Screen", 34f, bold = true), lp())
        col.addView(label("Экран телефона на ПК по кабелю", 15f, alpha = 170), lp(2))

        // карточка статуса
        val card = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            background = Glass.drawable(this@MainActivity, 28f)
            setPadding(dp(22f), dp(20f), dp(22f), dp(22f))
        }
        controlView = label("", 16f, bold = true).also { card.addView(it, lp()) }
        streamView = label("", 16f, bold = true).also { card.addView(it, lp(8)) }
        card.addView(View(this).apply { setBackgroundColor(Color.argb(40, 255, 255, 255)) },
            LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(1f)).apply {
                topMargin = dp(16f); bottomMargin = dp(14f)
            })
        card.addView(label("PIN ДЛЯ ПК", 12f, alpha = 150).apply { letterSpacing = 0.15f }, lp())
        pinView = label("", 42f, bold = true).apply { letterSpacing = 0.08f }.also { card.addView(it, lp(2)) }
        col.addView(card, lp(24))

        // кнопки
        col.addView(Glass.button(this, "1.  Включить управление", 0x24FFFFFF, 0x40FFFFFF) {
            startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
        }, lp(20))
        col.addView(Glass.button(this, "2.  Начать трансляцию", 0x553A7BFF, 0x883A7BFF) {
            val mpm = getSystemService(MediaProjectionManager::class.java)
            @Suppress("DEPRECATION")
            startActivityForResult(mpm.createScreenCaptureIntent(), reqProjection)
        }, lp(12))
        col.addView(Glass.button(this, "Остановить", 0x33FF453A, 0x66FF453A) {
            stopService(Intent(this, StreamService::class.java))
            updateStatus()
        }, lp(12))

        // подсказка
        val hint = label(
            "3.  Подключите телефон кабелем, включите «Отладка по USB» " +
                "(Настройки → Для разработчиков), разрешите этот компьютер, " +
                "запустите на ПК viewer.py и введите PIN.", 14f, alpha = 190
        ).apply {
            background = Glass.drawable(this@MainActivity, 24f, fill = 0x14FFFFFF, stroke = 0x38FFFFFF)
            setPadding(dp(20f), dp(16f), dp(20f), dp(18f))
            setLineSpacing(0f, 1.15f)
        }
        col.addView(hint, lp(24))

        val scroll = ScrollView(this).apply { isFillViewport = true; addView(col) }
        frame.addView(scroll, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        setContentView(frame)
        updateStatus()
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != reqProjection || resultCode != RESULT_OK || data == null) {
            Toast.makeText(this, "Трансляция не запущена", Toast.LENGTH_SHORT).show()
            return
        }
        val i = Intent(this, StreamService::class.java)
            .putExtra("code", resultCode)
            .putExtra("data", data)
        if (Build.VERSION.SDK_INT >= 26) startForegroundService(i) else startService(i)
        pinView?.postDelayed({ updateStatus() }, 800)
    }
}
