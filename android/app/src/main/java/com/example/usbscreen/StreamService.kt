package com.example.usbscreen

import android.accessibilityservice.AccessibilityService
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.Bitmap
import android.graphics.PixelFormat
import android.graphics.Point
import android.hardware.display.DisplayManager
import android.hardware.display.VirtualDisplay
import android.media.Image
import android.media.ImageReader
import android.media.projection.MediaProjection
import android.media.projection.MediaProjectionManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.view.Display
import java.io.ByteArrayOutputStream
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.nio.ByteBuffer
import java.security.SecureRandom
import kotlin.math.min
import kotlin.math.roundToInt

/**
 * Слушает localhost:PORT (ПК пробрасывает порт через `adb forward`), проверяет PIN,
 * снимает экран (MediaProjection) и шлёт JPEG-кадры [1 байт тип=1][4 байта длина][jpeg].
 * От ПК принимает текстовые команды (tap/swipe/back/home/recents/notif/t/bs/enter/q).
 */
class StreamService : Service() {
    companion object {
        const val PORT = 38517
        @Volatile var running = false
        @Volatile var pin = ""
    }

    private var projection: MediaProjection? = null
    private var vd: VirtualDisplay? = null
    private var reader: ImageReader? = null
    private var server: ServerSocket? = null
    private var client: Socket? = null
    private var stopped = false
    private var failures = 0

    @Volatile private var pendingPreset = 2
    @Volatile private var jpegQ = 65
    @Volatile private var realW = 1080
    @Volatile private var realH = 1920

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val code = intent?.getIntExtra("code", 0) ?: 0
        @Suppress("DEPRECATION")
        val data = intent?.getParcelableExtra<Intent>("data")
        if (data == null) {
            stopSelf()
            return START_NOT_STICKY
        }
        if (running) return START_NOT_STICKY
        running = true
        newPin()

        startFg()
        val mpm = getSystemService(MediaProjectionManager::class.java)
        val proj = mpm.getMediaProjection(code, data)
        proj.registerCallback(object : MediaProjection.Callback() {
            override fun onStop() { stopAll() }
        }, Handler(Looper.getMainLooper()))
        projection = proj

        Thread { serve() }.start()
        return START_NOT_STICKY
    }

    private fun newPin() {
        pin = (100000 + SecureRandom().nextInt(900000)).toString()
    }

    private fun startFg() {
        val nm = getSystemService(NotificationManager::class.java)
        val builder = if (Build.VERSION.SDK_INT >= 26) {
            nm.createNotificationChannel(
                NotificationChannel("stream", "Трансляция экрана", NotificationManager.IMPORTANCE_LOW))
            Notification.Builder(this, "stream")
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        val n = builder
            .setContentTitle("USB Screen")
            .setContentText("Экран доступен ПК по USB-кабелю")
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setOngoing(true)
            .build()
        if (Build.VERSION.SDK_INT >= 29) {
            startForeground(1, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION)
        } else {
            startForeground(1, n)
        }
    }

    // ---------------- приём подключений ----------------
    private fun serve() {
        try {
            val ss = ServerSocket()
            ss.reuseAddress = true
            ss.bind(InetSocketAddress(InetAddress.getByName("127.0.0.1"), PORT))
            server = ss
            while (running) {
                val c = ss.accept()
                try {
                    handleClient(c)
                } catch (e: Exception) {
                    // клиент ушёл или не прошёл проверку
                } finally {
                    try { c.close() } catch (e: Exception) {}
                    client = null
                }
            }
        } catch (e: Exception) {
            // сервер закрыт
        } finally {
            stopAll()
        }
    }

    private fun handleClient(c: Socket) {
        c.tcpNoDelay = true
        c.soTimeout = 5000
        val br = c.getInputStream().bufferedReader()
        val first = br.readLine() ?: return
        if (first.trim() != "auth $pin") {
            // защита от подбора: после 5 неудач PIN меняется
            failures++
            if (failures >= 5) { newPin(); failures = 0 }
            Thread.sleep(500)
            return
        }
        failures = 0
        c.soTimeout = 0
        client = c
        Thread {
            try { br.forEachLine { handle(it) } } catch (e: Exception) {}
            try { c.close() } catch (e: Exception) {}
        }.start()
        captureLoop(c)
    }

    // ---------------- захват экрана ----------------
    private fun realSize(): Point {
        val d = getSystemService(DisplayManager::class.java).getDisplay(Display.DEFAULT_DISPLAY)
        val p = Point()
        @Suppress("DEPRECATION")
        d.getRealSize(p)
        return p
    }

    private fun applyPreset(p: Int) {
        val rs = realSize()
        realW = rs.x; realH = rs.y
        val (shortSide, q) = when (p) {
            1 -> 480 to 50
            3 -> 1080 to 75
            else -> 720 to 65
        }
        jpegQ = q
        val sc = min(1f, shortSide.toFloat() / min(realW, realH))
        val w = (realW * sc).roundToInt() and 1.inv()
        val h = (realH * sc).roundToInt() and 1.inv()
        val dpi = resources.displayMetrics.densityDpi
        val old = reader
        val nr = ImageReader.newInstance(w, h, PixelFormat.RGBA_8888, 2)
        val v = vd
        if (v == null) {
            vd = projection!!.createVirtualDisplay(
                "usb-screen", w, h, dpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, nr.surface, null, null)
        } else {
            v.resize(w, h, dpi)
            v.surface = nr.surface
        }
        reader = nr
        old?.close()
    }

    private fun encode(img: Image): ByteArray {
        val pl = img.planes[0]
        val rowPx = pl.rowStride / pl.pixelStride
        val bmp = Bitmap.createBitmap(rowPx, img.height, Bitmap.Config.ARGB_8888)
        bmp.copyPixelsFromBuffer(pl.buffer)
        val crop = if (rowPx != img.width) Bitmap.createBitmap(bmp, 0, 0, img.width, img.height) else bmp
        val bos = ByteArrayOutputStream(128 * 1024)
        crop.compress(Bitmap.CompressFormat.JPEG, jpegQ, bos)
        if (crop !== bmp) crop.recycle()
        bmp.recycle()
        return bos.toByteArray()
    }

    private fun captureLoop(c: Socket) {
        val out = c.getOutputStream().buffered(64 * 1024)
        var applied = 0
        var checkAt = 0L
        while (running && !c.isClosed) {
            val now = System.currentTimeMillis()
            var rotated = false
            if (now >= checkAt) {
                checkAt = now + 1000
                val rs = realSize()
                rotated = rs.x != realW || rs.y != realH
            }
            if (pendingPreset != applied || rotated) {
                applied = pendingPreset
                applyPreset(applied)
            }
            val r = reader ?: break
            val img = try { r.acquireLatestImage() } catch (e: Exception) { null }
            if (img == null) {
                Thread.sleep(10)
                continue
            }
            val bytes = try { encode(img) } finally { img.close() }
            out.write(ByteBuffer.allocate(5).put(1.toByte()).putInt(bytes.size).array())
            out.write(bytes)
            out.flush()
        }
    }

    // ---------------- команды с ПК ----------------
    private fun handle(line: String) {
        val p = line.trim().split(' ')
        val svc = RemoteControlService.instance
        val w = realW.toFloat()
        val h = realH.toFloat()
        try {
            when (p[0]) {
                "q" -> pendingPreset = p[1].toInt().coerceIn(1, 3)
                "tap" -> {
                    val x = p[1].toFloat() * w
                    val y = p[2].toFloat() * h
                    svc?.swipe(x, y, x, y, p.getOrNull(3)?.toLong() ?: 50L)
                }
                "swipe" -> svc?.swipe(
                    p[1].toFloat() * w, p[2].toFloat() * h,
                    p[3].toFloat() * w, p[4].toFloat() * h,
                    p.getOrNull(5)?.toLong() ?: 250L)
                "back" -> svc?.global(AccessibilityService.GLOBAL_ACTION_BACK)
                "home" -> svc?.global(AccessibilityService.GLOBAL_ACTION_HOME)
                "recents" -> svc?.global(AccessibilityService.GLOBAL_ACTION_RECENTS)
                "notif" -> svc?.global(AccessibilityService.GLOBAL_ACTION_NOTIFICATIONS)
                "t" -> svc?.typeText(
                    p.drop(1).joinToString("") { String(Character.toChars(it.toInt(16))) })
                "bs" -> svc?.backspace()
                "enter" -> svc?.enter()
            }
        } catch (e: Exception) {
            // некорректная команда — игнорируем
        }
    }

    // ---------------- остановка ----------------
    @Synchronized
    private fun stopAll() {
        if (stopped) return
        stopped = true
        running = false
        pin = ""
        try { client?.close() } catch (e: Exception) {}
        try { server?.close() } catch (e: Exception) {}
        try { vd?.release() } catch (e: Exception) {}
        try { reader?.close() } catch (e: Exception) {}
        try { projection?.stop() } catch (e: Exception) {}
        client = null; server = null; vd = null; reader = null; projection = null
        try { stopForeground(true) } catch (e: Exception) {}
        stopSelf()
    }

    override fun onDestroy() {
        stopAll()
        super.onDestroy()
    }
}
