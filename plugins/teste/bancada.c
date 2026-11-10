/* Bancada de teste offline dos plugins da Mesa: ./bancada <label> [controle=valor ...] < entrada.f32 > saida.f32
   Áudio estéreo float32 intercalado a 48 kHz; processa em blocos de $BLOCO amostras (padrão 1024). */
#include <dlfcn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include "../ladspa.h"
int main(int argc, char **argv) {
  void *so = dlopen(getenv("SO") && *getenv("SO") ? getenv("SO") : "../mesa_fx.so", RTLD_NOW);
  if (!so) { fprintf(stderr, "%s\n", dlerror()); return 1; }
  const LADSPA_Descriptor *(*get)(unsigned long) = dlsym(so, "ladspa_descriptor");
  const LADSPA_Descriptor *d = NULL;
  for (unsigned long i = 0; (d = get(i)); i++) if (!strcmp(d->Label, argv[1])) break;
  if (!d) { fprintf(stderr, "sem %s\n", argv[1]); return 1; }
  int B = getenv("BLOCO") ? atoi(getenv("BLOCO")) : 1024;
  LADSPA_Handle h = d->instantiate(d, 48000);
  float *ctl = calloc(d->PortCount, sizeof(float));
  float *in[2] = {malloc(B * 4), malloc(B * 4)}, *out[2] = {malloc(B * 4), malloc(B * 4)};
  for (unsigned long p = 0; p < d->PortCount; p++) {
    const LADSPA_PortRangeHint *r = &d->PortRangeHints[p];
    if (d->PortDescriptors[p] & LADSPA_PORT_CONTROL) {
      int dm = r->HintDescriptor & 0x3c0;
      ctl[p] = dm == 0x240 ? 1 : dm == 0x100 ? (r->LowerBound + r->UpperBound) / 2 : 0;
      for (int a = 2; a < argc; a++) {
        size_t L = strlen(d->PortNames[p]);
        if (!strncmp(argv[a], d->PortNames[p], L) && argv[a][L] == '=') ctl[p] = atof(argv[a] + L + 1);
      }
      d->connect_port(h, p, &ctl[p]);
    } else d->connect_port(h, p, p == 0 ? in[0] : p == 1 ? in[1] : p == 2 ? out[0] : out[1]);
  }
  float *buf = malloc(B * 8);
  double t = 0; long frames = 0;
  size_t got;
  while ((got = fread(buf, 8, B, stdin)) > 0) {
    for (size_t i = 0; i < got; i++) { in[0][i] = buf[2 * i]; in[1][i] = buf[2 * i + 1]; }
    struct timespec a, b; clock_gettime(CLOCK_MONOTONIC, &a);
    d->run(h, got);
    clock_gettime(CLOCK_MONOTONIC, &b);
    t += (b.tv_sec - a.tv_sec) + (b.tv_nsec - a.tv_nsec) * 1e-9;
    for (size_t i = 0; i < got; i++) { buf[2 * i] = out[0][i]; buf[2 * i + 1] = out[1][i]; }
    fwrite(buf, 8, got, stdout);
    frames += got;
  }
  fprintf(stderr, "%s: %.1f s de áudio em %.3f s de CPU (%.2f%% de um núcleo)\n", argv[1], frames / 48000.0, t,
          100 * t / (frames / 48000.0));
  d->cleanup(h);
  return 0;
}
