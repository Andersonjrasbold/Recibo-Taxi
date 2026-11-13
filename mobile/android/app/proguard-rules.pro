# Regras do R8 deste app (minifyEnabled no build.gradle, desde o build 24).
#
# O Capacitor 8 ja mantem, pelas suas consumer rules, as classes anotadas com
# @CapacitorPlugin e os metodos @PluginMethod; o RevenueCat traz as dele. As
# regras abaixo sao a rede de seguranca: os pacotes dos plugins ficam inteiros
# e com o nome, porque nao ha aparelho de teste aqui para provar cada caminho
# nativo (compra, avaliacao, GPS, compartilhar). O ganho que o Play mede vem do
# resto: bibliotecas (Kotlin, OkHttp, Play Billing) e o proprio WebView bridge.

# Capacitor: bridge, plugins oficiais e da comunidade.
-keep class com.getcapacitor.** { *; }
-keep class com.capacitorjs.plugins.** { *; }
-keep class com.getcapacitor.community.** { *; }

# RevenueCat (compra na loja) e Play Billing.
-keep class com.revenuecat.purchases.** { *; }
-keep class com.android.billingclient.** { *; }

# Google Play In-App Review (plugin da comunidade usa a Play Core).
-keep class com.google.android.play.core.** { *; }

# O proprio app (MainActivity e o que o Capacitor registra por nome).
-keep class br.com.recibotaxi.app.** { *; }

# JavaScript interface do WebView: metodos chamados pelo nome a partir do JS.
-keepclassmembers class * {
    @android.webkit.JavascriptInterface <methods>;
}

# Atributos que o Capacitor e o Play Billing leem em tempo de execucao.
-keepattributes *Annotation*, Signature, InnerClasses, EnclosingMethod

# Sem avisos de classes opcionais que o RevenueCat referencia (Amazon).
-dontwarn com.amazon.**
