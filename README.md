# Mesa

Mesa de mixagem e cadeia de efeitos para o microfone, com efeitos próprios e interface no navegador.
Roda no **Linux** (PipeWire) e no **Windows** (Equalizer APO).

*A mixer and microphone effects chain with its own effects (high-pass/low-pass filter, RNNoise
de-noiser, 4→32-band EQ, compressor, Crystalizer) for Linux/PipeWire and Windows/Equalizer APO.
The interface is in Brazilian Portuguese.*

## O que tem

- **Cadeia de efeitos da entrada (microfone)**: filtro, de-noiser (RNNoise), equalizador, compressor e
  Crystalizer, em qualquer ordem. Por padrão ela já vem com filtro passa-alta de 80 Hz, de-noiser,
  equalizador e compressor.
- **Equalizador**: 4 bandas de controle (shelf grave em 150 Hz, 800 Hz, 4 kHz e shelf agudo em 8 kHz, cada
  uma com Q ajustável) que movem as 32 bandas de 1/3 de oitava do equalizador de verdade. Também dá para
  mexer nas 32 bandas direto. O botão **↺** volta tudo para plano.
- **Mixer**: volume e mudo de entradas, aplicativos e saídas, com medidores no fps do monitor.
- **Retorno do microfone**: escute o próprio microfone, já com os efeitos, num fone.
- Os ajustes ficam salvos e voltam ao ligar o computador.

## Instalar

Baixe na página de [versões](../../releases).

### Linux

`Mesa-<versão>-linux-x86_64.tar.gz` traz a `Mesa-x86_64.AppImage`:

```bash
tar xzf Mesa-2.2-linux-x86_64.tar.gz
cd Mesa-2.2-linux-x86_64
./Mesa-x86_64.AppImage
```

Na primeira vez ela se coloca no menu de aplicativos ("Mesa"). Precisa de:

- PipeWire com `pipewire-pulse` (comando `pactl`) e WirePlumber;
- `python3`;
- para a janela: Chromium ou Google Chrome (os medidores acompanham o monitor). Sem eles, ela usa GTK 3 +
  WebKit2GTK 4.1 (limitada a 60 fps);
- systemd de usuário, com que a janela liga o servidor da interface sozinha.

Os efeitos rodam como o serviço de usuário `mesa-cadeia` e continuam ativos com a janela fechada. A Mesa
cria o microfone virtual **Mesa (microfone)**: escolha ele como microfone nos programas.

### Windows

`Mesa-<versão>-windows-x64.zip` traz o `Mesa-<versão>-Setup.exe`, para Windows 10/11 de 64 bits. O instalador
já inclui o [Equalizer APO](https://sourceforge.net/projects/equalizerapo/) (que leva os efeitos para dentro
do serviço de áudio do Windows) e o Python que roda a interface. Não é preciso instalar mais nada.

## Desinstalar

**Windows:** "Adicionar ou remover programas" → Mesa.

**Linux:**

```bash
systemctl --user disable --now mesa-cadeia mesa-janela.socket
rm ~/.config/systemd/user/mesa-cadeia.service ~/.config/systemd/user/mesa-janela.socket ~/.config/systemd/user/mesa-janela.service
rm -r ~/.config/mesa ~/.local/share/mesa ~/.local/share/applications/mesa.desktop
rm -r ~/snap/chromium/common/mesa-perfil    # perfil da janela, se o Chromium for o do snap
```

## Compilar

As duas versões são geradas no Linux:

```bash
./build-appimage.sh    # dist/Mesa-x86_64.AppImage (gcc, make, curl, python3)
./build-windows.sh     # dist/Mesa-<versão>-Setup.exe (mingw-w64, NSIS, 7-Zip, python3 com Pillow)
```

Na primeira vez, os scripts baixam o RNNoise 0.2, o appimagetool, o Python embutido e o Equalizer APO.

| Pasta | Conteúdo |
|---|---|
| `mesa/` | interface (`ui.html`) e servidor (`mesa.py`); `mesa_win.py` e `mesa_apo.py` adaptam para o Windows |
| `plugins/` | efeitos em C: `mesa_fx.c` (LADSPA para o filter-chain do PipeWire), `mesa_vst.c` (VST 2.4 para o Equalizer APO), `mesa_retorno.c` (retorno do mic no Windows) |
| `plugins/teste/` | bancada para medir os efeitos fora do PipeWire |
| `windows/` | script do instalador (NSIS) |

## Licença

GPL-3.0-or-later (ver [LICENSE](LICENSE)). Componentes de terceiros em [TERCEIROS.md](TERCEIROS.md).
