#!/bin/zsh
# Arquiva e envia um build do iOS para a App Store Connect, com trava.
#
#   ./mobile/subir_build.sh
#
# A trava do meio existe porque build com sonda de teste dentro ja foi enviado
# duas vezes: uma numa copia duplicada pelo iCloud (builds 6 a 10), outra no
# proprio index.html, porque o arquivamento rodou enquanto uma sonda do
# simulador estava instalada. O xcodebuild le a pasta no momento em que roda,
# nao no momento em que e chamado. Conferir DEPOIS de arquivar e ANTES de
# enviar e o unico ponto que pega os dois casos.
#
# Precisa da chave de API da App Store Connect com papel Admin, em
# ~/Credenciais/Recibo-Taxi/apple/. Ver mobile/CAPACITOR.md.
set -e
RAIZ="$(cd "$(dirname "$0")/.." && pwd)"
SAIDA="${TMPDIR:-/tmp}/recibo-taxi-build"
CHAVE="$HOME/Credenciais/Recibo-Taxi/apple/AuthKey_Z53W4BVFVV.p8"
ISSUER=1a48f074-d9f7-4c4a-b9c9-7f172ac0d5a5

if pgrep -f xcodebuild > /dev/null; then
  echo "ABORTADO: ja existe xcodebuild rodando. Dois no mesmo projeto se atrapalham."
  exit 1
fi

cd "$RAIZ/mobile" && npx --no-install cap sync ios > /dev/null 2>&1
cd "$RAIZ/mobile/ios/App"
rm -rf "$SAIDA" && mkdir -p "$SAIDA"

echo "== archive $(date '+%H:%M:%S')  (build $(grep -m1 -o 'CURRENT_PROJECT_VERSION = [0-9]*' App.xcodeproj/project.pbxproj | grep -o '[0-9]*$'))"
xcodebuild -project App.xcodeproj -scheme App -configuration Release \
  -destination 'generic/platform=iOS' -archivePath "$SAIDA/ReciboTaxi.xcarchive" \
  -allowProvisioningUpdates -authenticationKeyPath "$CHAVE" \
  -authenticationKeyID Z53W4BVFVV -authenticationKeyIssuerID "$ISSUER" archive 2>&1 | tail -2

PUB="$SAIDA/ReciboTaxi.xcarchive/Products/Applications/App.app/public"
SONDAS=$(grep -rl "SONDA" "$PUB" 2>/dev/null | wc -l | tr -d ' ')
DUPES=$(find "$PUB" -name "* [0-9].*" 2>/dev/null | wc -l | tr -d ' ')
echo "-- arquivos: $(ls "$PUB" | tr '\n' ' ')"
echo "-- sondas: $SONDAS   duplicados: $DUPES"
if [ "$SONDAS" != "0" ] || [ "$DUPES" != "0" ]; then
  echo "ABORTADO: o pacote tem sonda ou copia duplicada. NAO enviado."
  grep -rl "SONDA" "$PUB" 2>/dev/null; find "$PUB" -name "* [0-9].*" 2>/dev/null
  exit 1
fi

echo "== upload $(date '+%H:%M:%S')"
xcodebuild -exportArchive -archivePath "$SAIDA/ReciboTaxi.xcarchive" \
  -exportOptionsPlist "$RAIZ/mobile/ios/ExportOptions.plist" -exportPath "$SAIDA/upload" \
  -allowProvisioningUpdates -authenticationKeyPath "$CHAVE" \
  -authenticationKeyID Z53W4BVFVV -authenticationKeyIssuerID "$ISSUER" 2>&1 | tail -3
echo "== fim $(date '+%H:%M:%S')"
