# Componentes de terceiros

A Mesa é distribuída sob a GPL-3.0-or-later (ver `LICENSE`). Os pacotes incluem ou derivam de:

| Componente | Onde entra | Licença |
|---|---|---|
| [RNNoise 0.2](https://github.com/xiph/rnnoise) (Xiph.Org, Mozilla, Amazon, Jean-Marc Valin) | compilado dentro de `mesa_fx.so` (Linux) e `MesaFX.dll` (Windows): de-noiser | BSD-3-Clause, texto em `RNNoise-COPYING.txt` |
| Algoritmo do Crystalizer do [Easy Effects](https://github.com/wwmm/easyeffects) (© Wellington Wallace) | portado para `plugins/mesa_fx.c` | GPL-3.0-or-later |
| [Equalizer APO 1.4.2](https://sourceforge.net/projects/equalizerapo/) (© Jonas Thedering) | só no instalador do Windows, sem alterações: hospeda o `MesaFX.dll` no serviço de áudio | GPL-2.0-or-later. Código-fonte: `EqualizerAPO-src-1.4.2.zip`, publicado junto com cada versão da Mesa e em [sourceforge.net/projects/equalizerapo/files/1.4.2](https://sourceforge.net/projects/equalizerapo/files/1.4.2/) |
| [Python 3.14 embeddable](https://www.python.org/downloads/windows/) (Python Software Foundation) | só no instalador do Windows: roda a interface | PSF License, texto em `python\LICENSE.txt` na pasta instalada |

`plugins/ladspa.h` e `plugins/vst2.h` são definições mínimas das interfaces binárias LADSPA 1.1 e VST 2.4,
escritas para a Mesa: não vêm do `ladspa.h` oficial nem do SDK da Steinberg.
