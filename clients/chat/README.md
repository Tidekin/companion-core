# Companion Chat

Flutter / Dart client for Android and Windows. Supports multiple independent Core private chats. Runtime and development MCP instructions: [native chat guide](../../docs/native-chat-mvp.md).

`lib/main.dart` is the production entry. `lib/main_debug.dart` contains synthetic in-memory Bot fixtures and official Flutter Driver support for development MCP only.

Dependencies are locked. From repository root: `scripts/install-flutter.ps1`, then `scripts/check-flutter.ps1 -WindowsBuild`. Do not distribute a debug target as the production client.
