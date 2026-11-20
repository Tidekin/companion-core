package dev.tidekin.companion_chat

import android.content.Intent
import com.google.firebase.messaging.FirebaseMessagingService

class CompanionMessagingService : FirebaseMessagingService() {
    override fun onNewToken(token: String) {
        // A disconnected app will read the current token when its authenticated session resumes.
        sendBroadcast(Intent(TOKEN_CHANGED).setPackage(packageName))
    }

    companion object {
        const val TOKEN_CHANGED = "dev.tidekin.companion_chat.PUSH_TOKEN_CHANGED"
    }
}
