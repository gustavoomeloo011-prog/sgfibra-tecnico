# EBF Técnico — Flutter

Cliente móvel Android/iOS para o painel operacional EBF Telecom.

## Servidor

O aplicativo não incorpora credenciais do SGP. Ele acessa o backend EBF por HTTPS. O endereço é definido na compilação:

```powershell
flutter build apk --release --dart-define=EBF_SERVER_URL=https://seu-dominio.example
```

Sem `--dart-define`, o projeto usa o túnel temporário registrado em `defaultServerUrl` apenas para o beta.

## Android

O APK de teste usa a chave de desenvolvimento do Flutter. Para distribuição definitiva, crie uma chave privada da empresa e configure a assinatura release no Gradle.

## iPhone

Abra `ios/Runner.xcworkspace` em um Mac com Xcode, escolha a equipe Apple Developer e execute `flutter build ipa --release` usando o mesmo `--dart-define`. Câmera, galeria e localização já estão declaradas no `Info.plist`.
