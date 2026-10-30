import 'dart:convert';
import 'dart:io';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:permission_handler/permission_handler.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:webview_flutter/webview_flutter.dart';
import 'package:webview_flutter_android/webview_flutter_android.dart';

const defaultServerUrl =
    'https://ebf-tecnico.onrender.com';
const serverUrl = String.fromEnvironment(
  'EBF_SERVER_URL',
  defaultValue: defaultServerUrl,
);

bool isInternalUri(Uri uri, Uri trustedOrigin) =>
    (uri.scheme == 'http' || uri.scheme == 'https') &&
    uri.host == trustedOrigin.host &&
    uri.port == trustedOrigin.port;

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await SystemChrome.setPreferredOrientations([DeviceOrientation.portraitUp]);
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Color(0xFF080B0F),
      statusBarIconBrightness: Brightness.light,
      systemNavigationBarColor: Color(0xFF080B0F),
      systemNavigationBarIconBrightness: Brightness.light,
    ),
  );
  runApp(const EbfTecnicoApp());
}

class EbfTecnicoApp extends StatelessWidget {
  const EbfTecnicoApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'EBF Técnico',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      colorScheme: ColorScheme.fromSeed(
        seedColor: const Color(0xFFF15A2B),
        brightness: Brightness.light,
      ),
      scaffoldBackgroundColor: const Color(0xFFF4F6F8),
      useMaterial3: true,
    ),
    home: const EbfWebApp(),
  );
}

class EbfWebApp extends StatefulWidget {
  const EbfWebApp({super.key});

  @override
  State<EbfWebApp> createState() => _EbfWebAppState();
}

class _EbfWebAppState extends State<EbfWebApp> {
  static const _popupChannel = MethodChannel('ebf_tecnico/popup');
  final _notifications = FlutterLocalNotificationsPlugin();
  late final WebViewController _controller;
  final _trustedOrigin = Uri.parse(serverUrl);
  double _progress = 0;
  String? _mainFrameError;
  bool _popupEnabled = false;
  bool _inPopup = false;

  @override
  void initState() {
    super.initState();
    _popupChannel.setMethodCallHandler((call) async {
      if (call.method == 'pipChanged' && mounted) {
        final values = Map<String, dynamic>.from(call.arguments as Map);
        setState(() => _inPopup = values['active'] == true);
      }
    });
    _configureNotifications();
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(const Color(0xFFF4F6F8))
      ..addJavaScriptChannel(
        'EbfNative',
        onMessageReceived: _handleNativeMessage,
      )
      ..setNavigationDelegate(
        NavigationDelegate(
          onProgress: (value) => setState(() => _progress = value / 100),
          onPageStarted: (_) => setState(() {
            _progress = 0;
            _mainFrameError = null;
          }),
          onPageFinished: (_) async {
            await _controller.runJavaScript('''
            try {
              if (!window.Notification) {
                window.Notification = { permission: 'granted', requestPermission: async () => 'granted' };
              } else if (window.EbfNative) {
                Object.defineProperty(window.Notification, 'permission', { get: () => 'granted' });
              }
            } catch (_) {}
          ''');
            if (mounted) setState(() => _progress = 1);
          },
          onWebResourceError: (error) {
            if (error.isForMainFrame == true) {
              setState(() => _mainFrameError = error.description);
            }
          },
          onNavigationRequest: _handleNavigation,
        ),
      );
    if (Platform.isAndroid &&
        _controller.platform is AndroidWebViewController) {
      (_controller.platform as AndroidWebViewController)
          .setOnPlatformPermissionRequest((request) async {
            final statuses = await [
              Permission.camera,
              Permission.locationWhenInUse,
            ].request();
            if (statuses.values.every((status) => status.isGranted)) {
              request.grant();
            } else {
              request.deny();
            }
          });
    }
    _controller.loadRequest(_trustedOrigin);
  }

  Future<void> _configureNotifications() async {
    const settings = InitializationSettings(
      android: AndroidInitializationSettings('@mipmap/ic_launcher'),
      iOS: DarwinInitializationSettings(),
    );
    await _notifications.initialize(
      settings: settings,
      onDidReceiveNotificationResponse: (response) {
        final target = response.payload;
        if (target != null && target.isNotEmpty) {
          _openInternalTarget(target);
        }
      },
    );
  }

  Future<void> _requestNotificationPermission() async {
    await _notifications
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >()
        ?.requestNotificationsPermission();
    await _notifications
        .resolvePlatformSpecificImplementation<
          IOSFlutterLocalNotificationsPlugin
        >()
        ?.requestPermissions(alert: true, badge: true, sound: true);
  }

  Future<void> _handleNativeMessage(JavaScriptMessage message) async {
    try {
      final data = jsonDecode(message.message) as Map<String, dynamic>;
      if (data['action'] == 'requestNotifications') {
        await _requestNotificationPermission();
        return;
      }
      if (data['action'] == 'setPopupEnabled') {
        _popupEnabled = data['enabled'] == true;
        if (Platform.isAndroid) {
          await _popupChannel.invokeMethod('setPopupEnabled', {'enabled': _popupEnabled});
        }
        return;
      }
      if (data['action'] == 'openPopup') {
        if (Platform.isAndroid && _popupEnabled) {
          await _popupChannel.invokeMethod('openPopup');
        }
        return;
      }
      if (data['action'] != 'notify') return;
      final target = (data['url'] as String?) ?? '/#dashboard';
      const details = NotificationDetails(
        android: AndroidNotificationDetails(
          'ebf_operacoes',
          'EBF Operações',
          channelDescription: 'Alterações de rota e resumos de serviço',
          importance: Importance.high,
          priority: Priority.high,
          color: Color(0xFFF15A2B),
        ),
        iOS: DarwinNotificationDetails(),
      );
      await _notifications.show(
        id: (data['tag'] ?? DateTime.now().millisecondsSinceEpoch).hashCode,
        title: (data['title'] as String?) ?? 'EBF Técnico',
        body: (data['body'] as String?) ?? '',
        notificationDetails: details,
        payload: target,
      );
    } catch (_) {
      // Mensagens inválidas vindas da página são ignoradas com segurança.
    }
  }

  NavigationDecision _handleNavigation(NavigationRequest request) {
    final uri = Uri.tryParse(request.url);
    if (uri == null) return NavigationDecision.prevent;
    if (isInternalUri(uri, _trustedOrigin)) return NavigationDecision.navigate;
    launchUrl(uri, mode: LaunchMode.externalApplication);
    return NavigationDecision.prevent;
  }

  void _openInternalTarget(String target) {
    final uri = Uri.tryParse(target);
    final resolved = uri == null
        ? _trustedOrigin
        : _trustedOrigin.resolveUri(uri);
    if (isInternalUri(resolved, _trustedOrigin)) {
      _controller.loadRequest(resolved);
    }
  }

  Future<void> _goBack() async {
    if (await _controller.canGoBack()) {
      await _controller.goBack();
    } else if (mounted) {
      SystemNavigator.pop();
    }
  }

  @override
  Widget build(BuildContext context) => PopScope(
    canPop: false,
    onPopInvokedWithResult: (didPop, result) => _goBack(),
    child: Scaffold(
      body: SafeArea(
        bottom: false,
        child: Stack(
          children: [
            if (_inPopup)
              const _PopupCompactView()
            else if (_mainFrameError == null)
              WebViewWidget(controller: _controller)
            else
              _ConnectionError(
                message: _mainFrameError!,
                onRetry: () {
                  setState(() => _mainFrameError = null);
                  _controller.loadRequest(_trustedOrigin);
                },
              ),
            if (_progress < 1)
              Align(
                alignment: Alignment.topCenter,
                child: LinearProgressIndicator(
                  value: _progress == 0 ? null : _progress,
                  minHeight: 3,
                  color: const Color(0xFFF15A2B),
                  backgroundColor: const Color(0xFF151B23),
                ),
              ),
          ],
        ),
      ),
    ),
  );
}

class _PopupCompactView extends StatelessWidget {
  const _PopupCompactView();

  @override
  Widget build(BuildContext context) => Container(
    color: const Color(0xFF0D131B),
    padding: const EdgeInsets.symmetric(horizontal: 18, vertical: 10),
    child: Row(
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Image.asset('assets/ebf-telecom.png', width: 64),
        const SizedBox(width: 14),
        const Flexible(
          child: Column(
            mainAxisAlignment: MainAxisAlignment.center,
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Text('EBF Técnico', style: TextStyle(color: Colors.white, fontSize: 18, fontWeight: FontWeight.w900)),
              Text('Toque para abrir', style: TextStyle(color: Color(0xFFF15A2B), fontSize: 12, fontWeight: FontWeight.w700)),
            ],
          ),
        ),
      ],
    ),
  );
}

class _ConnectionError extends StatelessWidget {
  const _ConnectionError({required this.message, required this.onRetry});

  final String message;
  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) => Center(
    child: SingleChildScrollView(
      padding: const EdgeInsets.all(28),
      child: ConstrainedBox(
        constraints: const BoxConstraints(maxWidth: 420),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: [
            Image.asset('assets/ebf-telecom.png', width: 260),
            const SizedBox(height: 28),
            const Text(
              'Servidor indisponível',
              style: TextStyle(fontSize: 24, fontWeight: FontWeight.w900),
            ),
            const SizedBox(height: 10),
            const Text(
              'Verifique a internet do aparelho ou se o servidor EBF está ligado.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Color(0xFF687486), height: 1.45),
            ),
            const SizedBox(height: 8),
            Text(
              message,
              textAlign: TextAlign.center,
              style: const TextStyle(fontSize: 10, color: Color(0xFF98A1AD)),
            ),
            const SizedBox(height: 22),
            FilledButton.icon(
              onPressed: onRetry,
              icon: const Icon(Icons.refresh),
              label: const Text('Tentar novamente'),
            ),
          ],
        ),
      ),
    ),
  );
}
