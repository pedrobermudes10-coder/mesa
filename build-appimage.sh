#!/bin/bash
# Gera Mesa-x86_64.AppImage: interface + servidor (mesa/) + efeitos LADSPA compilados (plugins/).
# Precisa: gcc, make, curl, python3. Baixa o RNNoise 0.2 e o appimagetool 1.9.1 na primeira vez.
# No computador de destino a AppImage só precisa de PipeWire (com pipewire-pulse/pactl), python3 e,
# para a janela, Chromium/Chrome (ou GTK + WebKit2 4.1).
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(pwd)
plugins/build.sh

TOOL="$ROOT/.ferramentas/appimagetool-1.9.1-x86_64.AppImage"
if [ ! -x "$TOOL" ]; then
    mkdir -p "$(dirname "$TOOL")"
    curl -fL -o "$TOOL" https://github.com/AppImage/appimagetool/releases/download/1.9.1/appimagetool-x86_64.AppImage
    chmod +x "$TOOL"
fi

APPDIR=$(mktemp -d)/Mesa.AppDir
trap 'rm -rf "$(dirname "$APPDIR")"' EXIT
mkdir -p "$APPDIR/usr/share/mesa/lib"
cp mesa/mesa.py mesa/ui.html mesa/mesa.svg "$APPDIR/usr/share/mesa/"
cp plugins/mesa_fx.so "$APPDIR/usr/share/mesa/lib/"
cp LICENSE TERCEIROS.md "$APPDIR/usr/share/mesa/"
cp plugins/rnnoise-0.2/COPYING "$APPDIR/usr/share/mesa/RNNoise-COPYING.txt"
cp mesa/mesa.svg "$APPDIR/mesa.svg"
cp mesa/mesa.svg "$APPDIR/.DirIcon"
cat > "$APPDIR/mesa.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Mesa
Comment=Mesa de mixagem e efeitos do PipeWire
Exec=mesa
Icon=mesa
Terminal=false
Categories=AudioVideo;Audio;Mixer;
EOF
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec python3 "$HERE/usr/share/mesa/mesa.py" "$@"
EOF
chmod +x "$APPDIR/AppRun"

mkdir -p dist
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream "$APPDIR" dist/Mesa-x86_64.AppImage >/dev/null
ls -la dist/Mesa-x86_64.AppImage
