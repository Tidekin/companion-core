import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:companion_chat/ui/app.dart';

void main() {
  test('login problems are explained instead of left silent', () {
    expect(loginProblemText('需要处理：CONSOLE_LOGIN_REQUIRED'), contains('凭据不正确'));
    expect(loginProblemText('需要处理：CONSOLE_LOGIN_RATE_LIMIT'), contains('稍后'));
    expect(
      loginProblemText('需要处理：OWNER_MODEL_CARRY_CHANGED'),
      contains('设备公钥'),
    );
    expect(loginProblemText('需要处理：X'), contains('X'));
  });

  testWidgets('offline registration shows the copyable device public key', (
    tester,
  ) async {
    const key =
        'ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12ab12';
    await tester.pumpWidget(
      MaterialApp(
        home: Builder(
          builder: (context) => TextButton(
            onPressed: () => showLoginProblem(
              context,
              '需要处理：OWNER_MODEL_CARRY_CHANGED',
              () async => key,
            ),
            child: const Text('open'),
          ),
        ),
      ),
    );
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('device_public_key')), findsOneWidget);
    expect(find.text(key), findsOneWidget);
    expect(find.byKey(const ValueKey('copy_device_key')), findsOneWidget);
  });

  testWidgets('a wrong credential shows no key', (tester) async {
    var asked = false;
    await tester.pumpWidget(
      MaterialApp(
        home: Builder(
          builder: (context) => TextButton(
            onPressed: () => showLoginProblem(
              context,
              '需要处理：CONSOLE_LOGIN_REQUIRED',
              () async {
                asked = true;
                return '';
              },
            ),
            child: const Text('open'),
          ),
        ),
      ),
    );
    await tester.tap(find.text('open'));
    await tester.pumpAndSettle();
    expect(find.textContaining('凭据不正确'), findsOneWidget);
    expect(find.byKey(const ValueKey('device_public_key')), findsNothing);
    expect(asked, isFalse);
  });
}
