package dev.tidekin.companion_chat

import android.Manifest
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import androidx.core.app.NotificationManagerCompat
import androidx.core.content.ContextCompat
import com.google.firebase.FirebaseApp
import com.google.firebase.messaging.FirebaseMessaging
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private var channel: MethodChannel? = null
    private var permissionRequest: MethodChannel.Result? = null
    private var pendingRoute: String? = null
    private val tokenChanges = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action == CompanionMessagingService.TOKEN_CHANGED && notificationsAllowed()) {
                channel?.invokeMethod("tokenChanged", null)
            }
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        pendingRoute = notificationRoute(intent) ?: savedInstanceState?.getString("pendingPushRoute")
        super.onCreate(savedInstanceState)
        ContextCompat.registerReceiver(this, tokenChanges,
            IntentFilter(CompanionMessagingService.TOKEN_CHANGED), ContextCompat.RECEIVER_NOT_EXPORTED)
    }

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        channel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, "dev.tidekin.companion_chat/push")
        channel?.setMethodCallHandler { call, result ->
            when (call.method) {
                "enable" -> enable(result)
                "getToken" -> token(result, interactive = false)
                "takeRoute" -> {
                    val route = pendingRoute
                    pendingRoute = null
                    result.success(route)
                }
                else -> result.notImplemented()
            }
        }
    }

    private fun messaging(): FirebaseMessaging? =
        if (FirebaseApp.getApps(this).any { it.name == FirebaseApp.DEFAULT_APP_NAME })
            FirebaseMessaging.getInstance() else null

    private fun enable(result: MethodChannel.Result) {
        if (messaging() == null) {
            result.error("PUSH_NOT_CONFIGURED", "此安装包尚未配置 Firebase。", null)
            return
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(NotificationChannel(
                getString(R.string.proactive_channel_id), getString(R.string.proactive_channel_name),
                NotificationManager.IMPORTANCE_DEFAULT))
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            if (permissionRequest != null) {
                result.error("PUSH_REQUEST_IN_PROGRESS", "系统正在请求通知权限。", null)
                return
            }
            permissionRequest = result
            requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), NOTIFICATION_PERMISSION)
            return
        }
        token(result, interactive = true)
    }

    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == NOTIFICATION_PERMISSION) {
            val result = permissionRequest ?: return
            permissionRequest = null
            if (grantResults.firstOrNull() == PackageManager.PERMISSION_GRANTED) token(result, interactive = true)
            else result.success(null)
        }
    }

    private fun notificationsAllowed(): Boolean {
        if (!NotificationManagerCompat.from(this).areNotificationsEnabled()) return false
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return true
        val channel = getSystemService(NotificationManager::class.java)
            .getNotificationChannel(getString(R.string.proactive_channel_id))
        return channel != null && channel.importance != NotificationManager.IMPORTANCE_NONE
    }

    private fun token(result: MethodChannel.Result, interactive: Boolean) {
        val messaging = messaging()
        if (messaging == null || !notificationsAllowed() || (!interactive && !messaging.isAutoInitEnabled)) {
            result.success(null)
            return
        }
        if (interactive) messaging.isAutoInitEnabled = true
        messaging.token.addOnCompleteListener { task ->
            if (task.isSuccessful) result.success(task.result)
            else if (interactive) result.error("PUSH_TOKEN_UNAVAILABLE", "暂时无法向 Google Play 服务登记。", null)
            else result.success(null)
        }
    }

    private fun notificationRoute(intent: Intent?): String? {
        val route = intent?.getStringExtra("route") ?: return null
        // FlutterActivity also uses "route" as an initial-route extra.
        intent.removeExtra("route")
        if ((intent.flags and Intent.FLAG_ACTIVITY_LAUNCHED_FROM_HISTORY) != 0) return null
        return route.takeIf { ROUTE.matches(it) }
    }

    override fun onNewIntent(intent: Intent) {
        val route = notificationRoute(intent)
        super.onNewIntent(intent)
        setIntent(intent)
        if (route != null) {
            pendingRoute = route
            channel?.invokeMethod("routeOpened", null)
        }
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putString("pendingPushRoute", pendingRoute)
    }

    override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
        channel?.setMethodCallHandler(null)
        channel = null
        permissionRequest?.error("PUSH_REQUEST_CANCELLED", "通知权限请求已中断。", null)
        permissionRequest = null
        super.cleanUpFlutterEngine(flutterEngine)
    }

    override fun onDestroy() {
        unregisterReceiver(tokenChanges)
        super.onDestroy()
    }

    private companion object {
        const val NOTIFICATION_PERMISSION = 7015
        val ROUTE = Regex("[a-f0-9]{32}")
    }
}
