#!/bin/bash
# Gera dist/Mesa-<versão>-Setup.exe (versão = VERSION do mesa.py): Mesa para Windows (efeitos no Equalizer APO + interface com Python embutido).
# Precisa: curl, python3 com Pillow (ícone). O mingw-w64 e o NSIS vêm dos pacotes do Ubuntu
# (sudo apt install gcc-mingw-w64-x86-64 nsis) ou, sem sudo, extraídos em .ferramentas/mingw
# (apt-get download dos pacotes + dpkg -x; ver AUDIO-SETUP.md). O Python embutido vem de python.org.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
PY=3.14.10
EAPO=1.4.2
T="$ROOT/.ferramentas"
if ! command -v makensis >/dev/null && [ -x "$T/mingw/usr/bin/makensis" ]; then
    export PATH="$T/mingw/usr/bin:$PATH"
    export NSISDIR="$T/mingw/usr/share/nsis"
fi
plugins/build-win.sh
ZIP="$T/python-$PY-embed-amd64.zip"
[ -f "$ZIP" ] || curl -fL -o "$ZIP" "https://www.python.org/ftp/python/$PY/python-$PY-embed-amd64.zip"
# Equalizer APO oficial (GPL-2.0-or-later), embutido: os arquivos saem do instalador dele com o 7-Zip
# (7zip do Ubuntu, extraído como o mingw); a instalação nos dispositivos é feita por mesa/mesa_apo.py
EXE="$T/EqualizerAPO-x64-$EAPO.exe"
[ -f "$EXE" ] || curl -fL -o "$EXE" "https://sourceforge.net/projects/equalizerapo/files/$EAPO/EqualizerAPO-x64-$EAPO.exe/download"
SEVENZ="$T/mingw/usr/lib/7zip/7z"   # o wrapper do pacote aponta para /usr/lib; sem ele: 7z do sistema
[ -x "$SEVENZ" ] || SEVENZ=7z

PKG=windows/pacote
rm -rf "$PKG" windows/eapo; mkdir -p "$PKG/python"
"$SEVENZ" x -y -owindows/eapo "$EXE" >/dev/null
rm -rf 'windows/eapo/$PLUGINSDIR' windows/eapo/Uninstall.exe windows/eapo/UpdateChecker.exe windows/eapo/config
printf 'Equalizer APO %s (c) Jonas Thedering - GPL-2.0-or-later.\r\nInstalado pela Mesa sem alteracoes.\r\nCodigo-fonte: https://sourceforge.net/projects/equalizerapo/files/%s/EqualizerAPO-src-%s.zip\r\n' \
    "$EAPO" "$EAPO" "$EAPO" > windows/eapo/LEIA-licenca-Equalizer-APO.txt
cp plugins/MesaFX.dll plugins/mesa_retorno.exe mesa/mesa_apo.py mesa/mesa.py mesa/mesa_win.py mesa/ui.html "$PKG/"
python3 mesa/icone.py "$PKG/mesa.ico"
cp LICENSE "$PKG/LICENSE.txt"; cp TERCEIROS.md "$PKG/"
cp plugins/rnnoise-0.2-win/COPYING "$PKG/RNNoise-COPYING.txt"
(cd "$PKG/python" && unzip -q "$ZIP")
mkdir -p dist
makensis -V2 -DVERSAO="$(sed -n "s/^VERSION = '\(.*\)'/\1/p" mesa/mesa.py)" windows/Mesa.nsi
rm -rf "$PKG" windows/eapo
ls -la "dist/Mesa-$(sed -n "s/^VERSION = '\(.*\)'/\1/p" mesa/mesa.py)-Setup.exe"
