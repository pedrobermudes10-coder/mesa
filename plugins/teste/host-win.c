/* Bancada do MesaFX.dll no Windows: faz o papel do Equalizer APO (carrega o VST, cadeia de entrada) e,
 * como a Mesa faria, escreve na memória compartilhada uma cadeia de um efeito com os controles pedidos.
 *   host-win.exe MesaFX.dll entrada.raw saida.raw mesa_compressor threshold=-30 ratio=4 ...
 * entrada/saída: float32 estéreo intercalado, 48 kHz, blocos de 480 (como o Linux com BLOCO=480).
 * Os controles não passados ficam no padrão do mesmo jeito que a bancada do Linux (bancada.c). */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <windows.h>
#include "../ladspa.h"
#include "../vst2.h"

#define MAXCTL 192
#define MAXFX 32
typedef struct { int32_t kind; float c[MAXCTL]; } FxSlot;
typedef struct { int32_t n, pad; FxSlot fx[MAXFX]; } ChainData;
typedef struct {
  uint32_t magic, version;
  volatile LONG gen[2], bank[2], ack[2], beat[2];
  float rate[2], block[2], us[2], chans[2];
  float pipe_ms[2][4];
} ShmHead;

static intptr_t cb(AEffect *e, int32_t op, int32_t i, intptr_t v, void *p, float o) {
  return op == audioMasterVersion ? 2400 : 0;
}

int main(int argc, char **argv) {
  if (argc < 5) { fprintf(stderr, "uso: host-win dll entrada saida rotulo [ctl=valor...]\n"); return 2; }
  HMODULE m = LoadLibraryA(argv[1]);
  if (!m) { fprintf(stderr, "LoadLibrary falhou (%lu)\n", GetLastError()); return 1; }
  AEffect *(*vmain)(audioMasterCallback) = (void *)GetProcAddress(m, "VSTPluginMain");
  const LADSPA_Descriptor *(*get)(unsigned long) = (void *)GetProcAddress(m, "ladspa_descriptor");
  AEffect *e = vmain(cb);
  if (!e || e->magic != VST_MAGIC) { fprintf(stderr, "VSTPluginMain falhou\n"); return 1; }

  const char *nm = "Global\\MesaFX";
  HANDLE h = OpenFileMappingA(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, nm);
  if (!h) { nm = "Local\\MesaFX"; h = OpenFileMappingA(FILE_MAP_READ | FILE_MAP_WRITE, FALSE, nm); }
  if (!h) { fprintf(stderr, "memória compartilhada não existe\n"); return 1; }
  char *base = MapViewOfFile(h, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, 0);
  ShmHead *hd = (ShmHead *)base;
  ChainData *chains = (ChainData *)(base + 256);
  fprintf(stderr, "memória: %s, magic %08x\n", nm, hd->magic);

  /* a cadeia de teste vai no banco livre, como a Mesa faz */
  int kind = -1;
  const LADSPA_Descriptor *d = NULL;
  for (unsigned long i = 0; (d = get(i)); i++) if (!strcmp(d->Label, argv[4])) { kind = (int)i; break; }
  if (kind < 0) { fprintf(stderr, "sem %s\n", argv[4]); return 1; }
  int b = 1 - (hd->bank[0] & 1);
  ChainData *cd = &chains[0 * 2 + b];
  memset(cd, 0, sizeof *cd);
  cd->n = 1;
  cd->fx[0].kind = kind;
  for (unsigned long p = 4; p < d->PortCount; p++) {
    const LADSPA_PortRangeHint *r = &d->PortRangeHints[p];
    int dm = r->HintDescriptor & 0x3c0;
    float v = dm == 0x240 ? 1 : dm == 0x100 ? (r->LowerBound + r->UpperBound) / 2 : 0;
    for (int a = 5; a < argc; a++) {
      size_t L = strlen(d->PortNames[p]);
      if (!strncmp(argv[a], d->PortNames[p], L) && argv[a][L] == '=') v = (float)atof(argv[a] + L + 1);
    }
    cd->fx[0].c[p - 4] = v;
  }
  cd->fx[0].c[3] = 0; /* slot */
  MemoryBarrier();
  hd->bank[0] = b;
  InterlockedIncrement(&hd->gen[0]);

  e->setParameter(e, 0, 0.f);  /* Pipe 0 = entrada */
  e->dispatcher(e, effSetSampleRate, 0, 0, NULL, 48000.f);
  e->dispatcher(e, effSetBlockSize, 0, 480, NULL, 0);
  e->dispatcher(e, effMainsChanged, 0, 1, NULL, 0);
  e->dispatcher(e, effStartProcess, 0, 0, NULL, 0);
  if (hd->ack[0] != hd->gen[0]) { fprintf(stderr, "a cadeia não foi montada (ack %ld gen %ld)\n", hd->ack[0], hd->gen[0]); return 1; }

  FILE *fi = fopen(argv[2], "rb"), *fo = fopen(argv[3], "wb");
  if (!fi || !fo) { fprintf(stderr, "arquivos\n"); return 1; }
  enum { B = 480 };
  float buf[2 * B], inL[B], inR[B], outL[B], outR[B];
  float *in[2] = {inL, inR}, *out[2] = {outL, outR};
  LARGE_INTEGER f, t0, t1;
  QueryPerformanceFrequency(&f);
  double t = 0;
  long frames = 0;
  size_t got;
  while ((got = fread(buf, 8, B, fi)) > 0) {
    for (size_t i = 0; i < got; i++) { inL[i] = buf[2 * i]; inR[i] = buf[2 * i + 1]; }
    QueryPerformanceCounter(&t0);
    e->processReplacing(e, in, out, (int32_t)got);
    QueryPerformanceCounter(&t1);
    t += (double)(t1.QuadPart - t0.QuadPart) / f.QuadPart;
    for (size_t i = 0; i < got; i++) { buf[2 * i] = outL[i]; buf[2 * i + 1] = outR[i]; }
    fwrite(buf, 8, got, fo);
    frames += (long)got;
  }
  fclose(fi); fclose(fo);
  fprintf(stderr, "%s: %.1f s de áudio em %.3f s de CPU (%.2f%% de um núcleo); beat %ld, us/bloco %.1f\n", argv[4],
          frames / 48000.0, t, 100 * t / (frames / 48000.0), hd->beat[0], hd->us[0]);
  e->dispatcher(e, effMainsChanged, 0, 0, NULL, 0);
  e->dispatcher(e, effClose, 0, 0, NULL, 0);
  return 0;
}
