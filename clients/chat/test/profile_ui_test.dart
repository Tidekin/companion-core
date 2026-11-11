import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/profile.dart';
import 'package:companion_chat/ui/providers.dart';
import 'package:companion_chat/ui/theme.dart';

final bot = BotProfile(
  id: 'a',
  name: 'Core A',
  endpoint: Uri.parse('https://a.test'),
);

CompanionProfile profile({String? alias, bool enabled = true}) =>
    CompanionProfile(
      companionId: 'cmp_a',
      revision: 2,
      displayName: '小满',
      autoName: true,
      autoAvatar: true,
      settingsRevision: 1,
      selfUpdateEnabled: enabled,
      viewerAlias: alias,
    );

final change = ProfileChange(
  changeId: 'pchg_2',
  oldRevision: 1,
  newRevision: 2,
  fields: 'name',
  oldName: '小肥鱼',
  newName: '小满',
  actorType: 'companion',
  createdAt: DateTime.utc(2026, 11, 11, 8),
);

Widget host(Widget child, CompanionProfile? value) => ProviderScope(
  overrides: [
    profileProvider.overrideWith((ref, _) => Stream.value(value)),
    profileChangesProvider.overrideWith((ref, _) async => [change]),
  ],
  child: MaterialApp(
    theme: warmTheme(),
    home: Scaffold(body: child),
  ),
);

void main() {
  test('alias is shown first without replacing the companion name', () {
    expect(profile().shownName, '小满');
    expect(profile(alias: '鱼鱼').shownName, '鱼鱼');
    expect(profile(alias: '鱼鱼').displayName, '小满');
    expect(change.actorLabel, '伙伴更新');
  });

  testWidgets('panel shows the committed name, alias and real switches', (
    tester,
  ) async {
    await tester.pumpWidget(host(ProfilePanel(bot: bot), profile(alias: '鱼鱼')));
    await tester.pumpAndSettle();
    expect(find.text('小满'), findsOneWidget);
    expect(find.text('鱼鱼'), findsOneWidget);
    expect(find.byKey(const ValueKey('profile_auto_name')), findsOneWidget);
    expect(find.text('Core 尚未开启伙伴自行改名。'), findsNothing);
  });

  testWidgets('without the Core capability the switch is not offered', (
    tester,
  ) async {
    await tester.pumpWidget(
      host(ProfilePanel(bot: bot), profile(enabled: false)),
    );
    await tester.pumpAndSettle();
    final toggle = tester.widget<Switch>(
      find.byKey(const ValueKey('profile_auto_name')),
    );
    expect(toggle.onChanged, isNull);
    expect(find.text('Core 尚未开启伙伴自行改名。'), findsOneWidget);
  });

  testWidgets('before login the panel claims nothing', (tester) async {
    await tester.pumpWidget(host(ProfilePanel(bot: bot), null));
    await tester.pumpAndSettle();
    expect(find.text('登录后可查看伙伴的名字和变化记录。'), findsOneWidget);
    expect(find.byKey(const ValueKey('profile_auto_name')), findsNothing);
  });

  testWidgets('timeline event names the real actor and opens the record', (
    tester,
  ) async {
    await tester.pumpWidget(
      host(ProfileEventRow(bot: bot, change: change), profile()),
    );
    await tester.pumpAndSettle();
    expect(find.text('小肥鱼 把名字改成了「小满」'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('view_change_pchg_2')));
    await tester.pumpAndSettle();
    expect(find.text('变化记录'), findsOneWidget);
    expect(find.text('小肥鱼  →  小满'), findsOneWidget);
  });
}
