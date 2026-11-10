#!/bin/bash
# Compila mesa_fx.so (efeitos LADSPA da Mesa) com o RNNoise 0.2 embutido (estático).
# Uso: ./build.sh   → gera ./mesa_fx.so
set -euo pipefail
cd "$(dirname "$0")"
RN=rnnoise-0.2
if [ ! -f "$RN/.libs/librnnoise.a" ]; then
    [ -d "$RN" ] || curl -fL "https://github.com/xiph/rnnoise/releases/download/v0.2/$RN.tar.gz" | tar xz
    (cd "$RN" && ./configure --disable-shared --enable-static --with-pic --disable-examples --disable-doc \
        --enable-x86-rtcd >/dev/null && make -j"$(nproc)" >/dev/null)
fi
gcc -O3 -fno-math-errno -fno-trapping-math -fassociative-math -fno-signed-zeros -fPIC -shared -fvisibility=hidden -Wall -Wextra -Wno-unused-parameter -Wno-missing-field-initializers \
    -I"$RN/include" -o mesa_fx.so mesa_fx.c "$RN/.libs/librnnoise.a" -lm -Wl,--exclude-libs,ALL -Wl,-z,defs
echo "ok: $(pwd)/mesa_fx.so"
