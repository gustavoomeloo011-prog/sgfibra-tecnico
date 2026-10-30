import 'package:ebf_tecnico/main.dart';
import 'package:flutter_test/flutter_test.dart';

void main() {
  final trusted = Uri.parse('https://tecnico.ebftelecom.com.br');

  test('aceita somente o mesmo servidor EBF dentro do WebView', () {
    expect(
      isInternalUri(
        Uri.parse('https://tecnico.ebftelecom.com.br/#orders'),
        trusted,
      ),
      isTrue,
    );
    expect(
      isInternalUri(Uri.parse('https://evil.example/#orders'), trusted),
      isFalse,
    );
    expect(isInternalUri(Uri.parse('javascript:alert(1)'), trusted), isFalse);
  });
}
