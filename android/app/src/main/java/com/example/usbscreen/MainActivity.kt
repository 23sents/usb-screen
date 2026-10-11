package com.example.usbscreen

import android.Manifest
import android.app.Activity
import android.content.Intent
import android.content.pm.PackageManager
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast

class MainActivity : Activity() {
    private val reqProjection = 10
    private var status: TextView? = null
    private val ticker = object : Runnable {
        override fun run() {
            updateStatus()
            status?.postDelayed(this, 2000)
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
        status?.removeCallbacks(ticker)
        super.onPause()
    }

    private fun updateStatus() {
        val ctl = if (RemoteControlService.instance != null) "включено ✔" else "ВЫКЛЮЧЕНО ✖"
        val pinLine = if (StreamService.running) "\n\nPIN для ПК:  ${StreamService.pin}" else ""
        val str = if (StreamService.running) "идёт" else "остановлена"
        status?.text = "Управление: $ctl\nТрансляция: $str$pinLine"
    }

    private fun buildUi() {
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(32, 64, 32, 32) }
        status = TextView(this).apply { textSize = 20f; setPadding(0, 0, 0, 32) }
        root.addView(status)
        root.addView(Button(this).apply {
            text = "1. Включить управление (Спец. возможности)"
            setOnClickListener { startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }
        })
        root.addView(Button(this).apply {
            text = "2. Начать трансляцию"
            setOnClickListener {
                val mpm = getSystemService(MediaProjectionManager::class.java)
                @Suppress("DEPRECATION")
                startActivityForResult(mpm.createScreenCaptureIntent(), reqProjection)
            }
        })
        root.addView(Button(this).apply {
            text = "Остановить"
            setOnClickListener {
                stopService(Intent(this@MainActivity, StreamService::class.java))
                updateStatus()
            }
        })
        root.addView(TextView(this).apply {
            textSize = 14f
            setPadding(0, 32, 0, 0)
            text = "3. Подключите телефон кабелем, включите «Отладка по USB» " +
                "(Настройки → Для разработчиков), разрешите этот компьютер и запустите на ПК viewer.py. " +
                "Введите PIN, который показан выше."
        })
        setContentView(root)
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
        status?.postDelayed({ updateStatus() }, 800)
    }
}
