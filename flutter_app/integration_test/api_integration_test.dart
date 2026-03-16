import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'package:flutter_test/flutter_test.dart';
import 'package:integration_test/integration_test.dart';
import 'package:http/http.dart' as http;
import 'package:path_provider/path_provider.dart';
import 'package:snooker_ball_detector/main.dart';

String _baseUrl() {
  if (Platform.isAndroid) {
    return 'http://10.0.2.2:5000';
  }
  return 'http://127.0.0.1:5000';
}

Future<File> _createTempPng() async {
  const tinyPngBase64 =
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/woAAgMBAp0d0y8AAAAASUVORK5CYII=';
  final bytes = base64Decode(tinyPngBase64);
  final dir = await getTemporaryDirectory();
  final file = File('${dir.path}/test_image.png');
  await file.writeAsBytes(Uint8List.fromList(bytes), flush: true);
  return file;
}

void main() {
  IntegrationTestWidgetsFlutterBinding.ensureInitialized();

  testWidgets('API responds to /stats with JSON', (tester) async {
    await tester.pumpWidget(const MyApp());
    await tester.pumpAndSettle();

    final file = await _createTempPng();
    final request = http.MultipartRequest('POST', Uri.parse('${_baseUrl()}/stats'));
    request.files.add(await http.MultipartFile.fromPath('file', file.path));

    final response = await request.send();
    expect(
      response.statusCode,
      200,
      reason: 'Backend not reachable. Start server.py before running this test.',
    );

    final body = await response.stream.bytesToString();
    final jsonData = jsonDecode(body) as Map<String, dynamic>;

    expect(jsonData.containsKey('red-ball'), isTrue);
    expect(jsonData.containsKey('visible_score'), isTrue);
    expect(jsonData.containsKey('potential_score'), isTrue);
  });
}
