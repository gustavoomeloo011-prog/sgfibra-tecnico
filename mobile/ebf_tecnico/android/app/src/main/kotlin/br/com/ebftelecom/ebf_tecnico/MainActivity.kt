package br.com.ebftelecom.ebf_tecnico

import android.app.PictureInPictureParams
import android.content.res.Configuration
import android.os.Build
import android.util.Rational
import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine
import io.flutter.plugin.common.MethodChannel

class MainActivity : FlutterActivity() {
    private val channelName = "ebf_tecnico/popup"
    private var popupEnabled = false
    private var popupChannel: MethodChannel? = null

    override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
        super.configureFlutterEngine(flutterEngine)
        popupChannel = MethodChannel(flutterEngine.dartExecutor.binaryMessenger, channelName)
        popupChannel?.setMethodCallHandler { call, result ->
            when (call.method) {
                "setPopupEnabled" -> {
                    popupEnabled = call.argument<Boolean>("enabled") == true
                    result.success(true)
                }
                "openPopup" -> result.success(enterEbfPictureInPicture())
                else -> result.notImplemented()
            }
        }
    }

    private fun enterEbfPictureInPicture(): Boolean {
        if (!popupEnabled || Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return false
        val params = PictureInPictureParams.Builder()
            .setAspectRatio(Rational(16, 9))
            .build()
        return enterPictureInPictureMode(params)
    }

    override fun onUserLeaveHint() {
        super.onUserLeaveHint()
        enterEbfPictureInPicture()
    }

    override fun onPictureInPictureModeChanged(inPictureInPictureMode: Boolean, newConfig: Configuration) {
        super.onPictureInPictureModeChanged(inPictureInPictureMode, newConfig)
        popupChannel?.invokeMethod("pipChanged", mapOf("active" to inPictureInPictureMode))
    }
}
