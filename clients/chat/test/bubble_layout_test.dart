import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/chat/models.dart';
import 'package:companion_chat/ui/app.dart';
import 'package:companion_chat/ui/providers.dart';

final bot = BotProfile(
  id: 'a',
  name: 'Core A',
  endpoint: Uri.parse('https://a.test'),
);

Widget host(Widget child) => ProviderScope(
  overrides: [profileProvider.overrideWith((ref, _) => Stream.value(null))],
  child: MaterialApp(home: Scaffold(body: child)),
);

void main() {
  testWidgets('a paragraph break does not leave a blank line in the bubble', (
    tester,
  ) async {
    await tester.pumpWidget(host(Bubble(text: '第一句话。\n\n', bot: bot)));
    expect(find.text('第一句话。'), findsOneWidget);
  });

  testWidgets('follow-up bubbles line up with the first one', (tester) async {
    await tester.pumpWidget(
      host(
        Column(
          children: [
            Bubble(key: const ValueKey('first'), text: '第一段', bot: bot),
            const Bubble(key: ValueKey('second'), text: '第二段'),
          ],
        ),
      ),
    );
    await tester.pumpAndSettle();
    final first = tester.getTopLeft(find.text('第一段')).dx;
    final second = tester.getTopLeft(find.text('第二段')).dx;
    expect(second, first);
    expect(find.byKey(const ValueKey('bubble_avatar_gap')), findsOneWidget);
  });
}
