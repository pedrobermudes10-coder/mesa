#!/bin/bash
# Compila MesaFX.dll (plugin VST 2.4 para o Equalizer APO, Windows x64) com o RNNoise 0.2 embutido,
# e mesa_retorno.exe (retorno do mic).
# Precisa do mingw-w64 (sudo apt install gcc-mingw-w64-x86-64) ou do mesmo pacote extraído em
# ../.ferramentas/mingw (ver build-windows.sh). Uso: ./build-win.sh   → gera ./MesaFX.dll
set -euo pipefail
cd "$(dirname "$0")"
MINGW="$(cd .. && pwd)/.ferramentas/mingw"
if ! command -v x86_64-w64-mingw32-gcc >/dev/null && [ -d "$MINGW/usr/bin" ]; then
    export PATH="$MINGW/usr/bin:$PATH"
fi
CC=$(command -v x86_64-w64-mingw32-gcc || command -v x86_64-w64-mingw32-gcc-win32)
RN=rnnoise-0.2
RNW=$RN-win
if [ ! -f "$RNW/.libs/librnnoise.a" ]; then
    rm -rf "$RNW"; mkdir "$RNW"
    curl -fL "https://github.com/xiph/rnnoise/releases/download/v0.2/$RN.tar.gz" | tar xz -C "$RNW" --strip-components=1
    (cd "$RNW" && ./configure --host=x86_64-w64-mingw32 CC="$CC" AR=x86_64-w64-mingw32-ar RANLIB=x86_64-w64-mingw32-ranlib --disable-shared --enable-static \
        --disable-examples --disable-doc --enable-x86-rtcd >/dev/null && make -j"$(nproc)" >/dev/null)
fi
"$CC" -O3 -fno-math-errno -fno-trapping-math -fassociative-math -fno-signed-zeros -shared \
    -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers \
    -I"$RNW/include" -o MesaFX.dll mesa_vst.c "$RNW/.libs/librnnoise.a" -lm \
    -static -static-libgcc -Wl,--exclude-libs,ALL -s
# retorno do mic (botão RETORNO): WASAPI, sem janela
"$CC" -O2 -Wall -mwindows -o mesa_retorno.exe mesa_retorno.c -lole32 -lavrt -static -s
echo "ok: $(pwd)/MesaFX.dll $(pwd)/mesa_retorno.exe"
