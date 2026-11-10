/*
 * mesa_fx — efeitos da Mesa em LADSPA, carregados pelo filter-chain do PipeWire.
 *
 *   mesa_filter       filtro (passa-alta/baixa em cascata, shelf, bell, ressonância, notch, passa-banda, all-pass)
 *   mesa_eq           equalizador paramétrico de até 32 bandas
 *   mesa_rnnoise      de-noiser RNNoise (xiph), com VAD, wet e release iguais aos do Easy Effects
 *   mesa_compressor   compressor para baixo / para cima / boost (curva igual à desenhada na Mesa)
 *   mesa_crystalizer  Crystalizer portado do Easy Effects (13 bandas FIR, realce pela 2ª derivada,
 *                     intensidade adaptativa por curtose, fator de crista e fluxo, sobreamostragem 2x)
 *
 * Os nomes dos controles são os mesmos das propriedades do Easy Effects (threshold, intensityBand3…).
 * Todos têm bypass, inputGain e outputGain (dB) e slot: o nível RMS de entrada e saída de cada efeito
 * vai para /dev/shm/mesa-niveis-<uid> (slot = linha), de onde a Mesa lê os medidores do meio da cadeia.
 * No Windows este arquivo é incluído por mesa_vst.c (plugin VST para o Equalizer APO) e os níveis ficam na
 * memória compartilhada de lá.
 *
 * O algoritmo do Crystalizer vem do Easy Effects (GPL-3.0-or-later, © Wellington Wallace); este arquivo
 * também é GPL-3.0-or-later.
 */
#define _GNU_SOURCE
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#ifdef _WIN32
#include <windows.h>
#else
#include <sys/mman.h>
#include <unistd.h>
#endif
#if defined(__x86_64__) || defined(__i386__)
#include <xmmintrin.h>
#endif

#include "ladspa.h"
#include "rnnoise.h"

#ifdef _WIN32
#define EXPORT __declspec(dllexport)
#else
#define EXPORT __attribute__((visibility("default")))
#endif
#define MAXCTL 192

/* ------------------------------------------------------------------ níveis compartilhados */

#define NSLOT 64
typedef struct {
  uint32_t magic, version;
  float ms[NSLOT][4];      /* média quadrática ~10 ms: entrada L, R, saída L, R */
  float extra[NSLOT][16];  /* compressor: ganho (dB); rnnoise: VAD; crystalizer: intensidades (dB) */
} Levels;

#ifdef _WIN32
static Levels *win_levels(void);   /* em mesa_vst.c */
static Levels *levels_map(void) { return win_levels(); }
#else
static Levels *levels_map(void) {
  char path[64];
  snprintf(path, sizeof path, "/dev/shm/mesa-niveis-%u", (unsigned)getuid());
  int fd = open(path, O_RDWR | O_CREAT, 0600);
  if (fd < 0) return NULL;
  if (ftruncate(fd, sizeof(Levels)) != 0) { close(fd); return NULL; }
  void *p = mmap(NULL, sizeof(Levels), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
  close(fd);
  if (p == MAP_FAILED) return NULL;
  Levels *l = p;
  l->magic = 0x4d455341; l->version = 1;
  return l;
}
#endif

static inline double now_us(void) {
#ifdef _WIN32
  static double k;
  LARGE_INTEGER c;
  if (!k) { LARGE_INTEGER f; QueryPerformanceFrequency(&f); k = 1e6 / (double)f.QuadPart; }
  QueryPerformanceCounter(&c);
  return (double)c.QuadPart * k;
#else
  struct timespec t;
  clock_gettime(CLOCK_MONOTONIC, &t);
  return t.tv_sec * 1e6 + t.tv_nsec * 1e-3;
#endif
}

/* ------------------------------------------------------------------ estrutura comum */

typedef struct { const char *name; float lo, hi, def; int flags; } Ctl;
enum { F_INT = 1, F_TOGGLE = 2 };
/* controles comuns, sempre os primeiros */
enum { C_BYPASS, C_INGAIN, C_OUTGAIN, C_SLOT, C_COMMON };
#define COMMON_CTLS \
  {"bypass", 0, 1, 0, F_TOGGLE}, {"inputGain", -36, 36, 0, 0}, {"outputGain", -36, 36, 0, 0}, {"slot", -1, 63, -1, F_INT}

typedef struct Inst Inst;
typedef struct {
  unsigned long id;
  const char *label, *name;
  const Ctl *ctls;
  int nctl;
  int (*init)(Inst *);
  void (*process)(Inst *, float *l, float *r, int n);   /* no lugar, já com o ganho de entrada */
  void (*done)(Inst *);
  /* gerado */
  LADSPA_Descriptor d;
} Def;

struct Inst {
  const Def *def;
  double rate;
  float *in[2], *out[2], *c[MAXCTL];
  Levels *lv;
  float gin, gout, gk;      /* ganhos suavizados */
  float ms[4], msk;
  float *wl, *wr;           /* buffers de trabalho */
  int wcap;
  void *st;
};

#define P(I, k) (*(I)->c[k])
static inline float db2lin(float d) { return powf(10.f, d / 20.f); }

/* ------------------------------------------------------------------ biquads (RBJ, iguais ao gráfico da Mesa) */

typedef struct { double b0, b1, b2, a1, a2; } BqC;
typedef struct { double z1, z2; } BqS;
static inline float bq_run(const BqC *c, BqS *s, float xf) {
  double x = xf, y = c->b0 * x + s->z1;
  s->z1 = c->b1 * x - c->a1 * y + s->z2;
  s->z2 = c->b2 * x - c->a2 * y;
  return (float)y;
}
enum { K_NONE, K_LP, K_HP, K_BP, K_NOTCH, K_LS, K_HS, K_BELL, K_AP };
static int bq_design(BqC *o, int kind, double rate, double f, double g, double q) {
  if (f > rate * .49) f = rate * .49;
  if (f < 1) f = 1;
  double w = 2 * M_PI * f / rate, cs = cos(w), sn = sin(w), A = pow(10, g / 40);
  double al = sn / (2 * fmax(q > 0 ? q : .707, .05));
  double b0, b1, b2, a0, a1, a2;
  switch (kind) {
    case K_LP: b0 = (1 - cs) / 2; b1 = 1 - cs; b2 = b0; a0 = 1 + al; a1 = -2 * cs; a2 = 1 - al; break;
    case K_HP: b0 = (1 + cs) / 2; b1 = -(1 + cs); b2 = b0; a0 = 1 + al; a1 = -2 * cs; a2 = 1 - al; break;
    case K_BP: b0 = al; b1 = 0; b2 = -al; a0 = 1 + al; a1 = -2 * cs; a2 = 1 - al; break;
    case K_NOTCH: b0 = 1; b1 = -2 * cs; b2 = 1; a0 = 1 + al; a1 = -2 * cs; a2 = 1 - al; break;
    case K_LS: {
      double s2 = 2 * sqrt(A) * al;
      b0 = A * ((A + 1) - (A - 1) * cs + s2); b1 = 2 * A * ((A - 1) - (A + 1) * cs); b2 = A * ((A + 1) - (A - 1) * cs - s2);
      a0 = (A + 1) + (A - 1) * cs + s2; a1 = -2 * ((A - 1) + (A + 1) * cs); a2 = (A + 1) + (A - 1) * cs - s2;
      break;
    }
    case K_HS: {
      double s2 = 2 * sqrt(A) * al;
      b0 = A * ((A + 1) + (A - 1) * cs + s2); b1 = -2 * A * ((A - 1) + (A + 1) * cs); b2 = A * ((A + 1) + (A - 1) * cs - s2);
      a0 = (A + 1) - (A - 1) * cs + s2; a1 = 2 * ((A - 1) - (A + 1) * cs); a2 = (A + 1) - (A - 1) * cs - s2;
      break;
    }
    case K_BELL: b0 = 1 + al * A; b1 = -2 * cs; b2 = 1 - al * A; a0 = 1 + al / A; a1 = -2 * cs; a2 = 1 - al / A; break;
    case K_AP: b0 = 1 - al; b1 = -2 * cs; b2 = 1 + al; a0 = 1 + al; a1 = -2 * cs; a2 = 1 - al; break;
    default: return 0;
  }
  o->b0 = b0 / a0; o->b1 = b1 / a0; o->b2 = b2 / a0; o->a1 = a1 / a0; o->a2 = a2 / a0;
  return 1;
}
/* passo do dB/oitava (índice do Easy Effects) -> quantas seções de 2ª ordem em cascata */
static const int SLOPE_N[8] = {1, 2, 3, 4, 6, 8, 12, 16};
static int slope_n(float v) { int i = (int)lrintf(v); return SLOPE_N[i < 0 ? 0 : i > 7 ? 7 : i]; }

/* ------------------------------------------------------------------ filtro */

/* tipos do LSP Filter usados pelo Easy Effects */
static const int FILT_KIND[11] = {K_LP, K_HP, K_LS, K_HS, K_BELL, K_BP, K_NOTCH, K_BELL, K_LP, K_HP, K_AP};
enum { FI_TYPE = C_COMMON, FI_FREQ, FI_GAIN, FI_Q, FI_SLOPE, FI_N };
static const Ctl filter_ctls[] = {COMMON_CTLS, {"type", 0, 10, 0, F_INT}, {"frequency", 10, 20000, 1000, 0},
                                  {"gain", -36, 36, 0, 0}, {"quality", 0, 100, 0.707f, 0}, {"slope", 0, 7, 0, F_INT}};
typedef struct {
  float key[5];
  BqC c;
  int n, on;
  BqS s[2][16];
} Filter;

static int filter_init(Inst *I) { I->st = calloc(1, sizeof(Filter)); ((Filter *)I->st)->key[0] = -1; return I->st != NULL; }
static void filter_process(Inst *I, float *l, float *r, int n) {
  Filter *F = I->st;
  float key[5] = {P(I, FI_TYPE), P(I, FI_FREQ), P(I, FI_GAIN), P(I, FI_Q), P(I, FI_SLOPE)};
  if (memcmp(key, F->key, sizeof key)) {
    memcpy(F->key, key, sizeof key);
    int t = (int)lrintf(key[0]);
    t = t < 0 ? 0 : t > 10 ? 10 : t;
    int pass = t == 0 || t == 1 || t == 8 || t == 9;
    int nn = pass ? slope_n(key[4]) : 1;
    if (nn != F->n) memset(F->s, 0, sizeof F->s);
    F->n = nn;
    F->on = bq_design(&F->c, FILT_KIND[t], I->rate, key[1], key[2], key[3]);
  }
  if (!F->on) return;
  for (int k = 0; k < F->n; k++)
    for (int i = 0; i < n; i++) { l[i] = bq_run(&F->c, &F->s[0][k], l[i]); r[i] = bq_run(&F->c, &F->s[1][k], r[i]); }
}

/* ------------------------------------------------------------------ equalizador */

/* 32 bandas de 1/3 de oitava (as 4 bandas macro da interface só movem estas, não existem aqui) */
#define EQ_BANDS 32
/* tipos de banda do LSP Para EQ: 0 desligada, 1 bell, 2 passa-alta, 3 shelf agudo, 4 passa-baixa, 5 shelf grave,
   6 notch, 7 ressonância, 8 all-pass, 9 passa-banda, 10 ladder passa, 11 ladder rejeita */
static const int BAND_KIND[12] = {K_NONE, K_BELL, K_HP, K_HS, K_LP, K_LS, K_NOTCH, K_BELL, K_AP, K_BP, K_LP, K_HP};
enum { EQ_NUM = C_COMMON, EQ_B0 };
enum { B_TYPE, B_FREQ, B_GAIN, B_Q, B_SLOPE, B_N };
static Ctl eq_ctls[C_COMMON + 1 + EQ_BANDS * B_N] = {COMMON_CTLS, {"numBands", 0, EQ_BANDS, EQ_BANDS, F_INT}};
static char eq_names[EQ_BANDS * B_N][24];
static void eq_make_ctls(void) {
  static const char *suf[B_N] = {"Type", "Frequency", "Gain", "Q", "Slope"};
  for (int b = 0; b < EQ_BANDS; b++)
    for (int k = 0; k < B_N; k++) {
      char *nm = eq_names[b * B_N + k];
      snprintf(nm, 24, "band%d%s", b, suf[k]);
      Ctl c = {nm, 0, 0, 0, 0};
      switch (k) {
        case B_TYPE: c.hi = 11; c.def = 1; c.flags = F_INT; break;
        case B_FREQ: c.lo = 10; c.hi = 20000; c.def = 1000; break;
        case B_GAIN: c.lo = -36; c.hi = 36; break;
        case B_Q: c.hi = 100; c.def = 0.707f; break;
        case B_SLOPE: c.hi = 7; c.flags = F_INT; break;
      }
      eq_ctls[EQ_B0 + b * B_N + k] = c;
    }
}
typedef struct {
  float key[EQ_BANDS][B_N];
  BqC c[EQ_BANDS];
  int n[EQ_BANDS], on[EQ_BANDS];
  BqS s[EQ_BANDS][2][16];
} Eq;
static int eq_init(Inst *I) {
  Eq *E = calloc(1, sizeof(Eq));
  if (!E) return 0;
  for (int b = 0; b < EQ_BANDS; b++) E->key[b][0] = -1;
  I->st = E;
  return 1;
}
static void eq_process(Inst *I, float *l, float *r, int n) {
  Eq *E = I->st;
  int nb = (int)lrintf(P(I, EQ_NUM));
  nb = nb < 0 ? 0 : nb > EQ_BANDS ? EQ_BANDS : nb;
  for (int b = 0; b < nb; b++) {
    float key[B_N];
    for (int k = 0; k < B_N; k++) key[k] = P(I, EQ_B0 + b * B_N + k);
    if (memcmp(key, E->key[b], sizeof key)) {
      memcpy(E->key[b], key, sizeof key);
      int t = (int)lrintf(key[B_TYPE]);
      t = t < 0 ? 0 : t > 11 ? 11 : t;
      int nn = slope_n(key[B_SLOPE]);
      if (nn != E->n[b]) memset(E->s[b], 0, sizeof E->s[b]);
      E->n[b] = nn;
      int kind = BAND_KIND[t];
      double q = key[B_Q];
      if (kind == K_BELL) {
        /* Q digital corrigido: a transformação bilinear estreita os bells perto de fs/2 (a 48 kHz, acima de
           ~7 kHz abriam buracos de até 5 dB entre bandas vizinhas). Mantém a largura em oitavas do Q pedido
           (fórmula de largura do RBJ com w0/sin(w0)). */
        double f0 = key[B_FREQ] < I->rate * .49 ? key[B_FREQ] : I->rate * .49, w = 2 * M_PI * f0 / I->rate;
        if (q > 0 && w > 1e-6) q = 1 / (2 * sinh(asinh(1 / (2 * q)) * w / sin(w)));
      }
      E->on[b] = bq_design(&E->c[b], kind, I->rate, key[B_FREQ], key[B_GAIN], q);
      /* bell e shelf com ganho zero são transparentes: não gasta processamento com eles */
      if ((kind == K_BELL || kind == K_LS || kind == K_HS) && fabsf(key[B_GAIN]) < .01f) E->on[b] = 0;
    }
    if (!E->on[b]) continue;
    for (int k = 0; k < E->n[b]; k++)
      for (int i = 0; i < n; i++) {
        l[i] = bq_run(&E->c[b], &E->s[b][0][k], l[i]);
        r[i] = bq_run(&E->c[b], &E->s[b][1][k], r[i]);
      }
  }
}

/* ------------------------------------------------------------------ FIFO simples (usado pelos que trabalham em blocos) */

typedef struct { float *d; int cap, len; } Fifo;
static int fifo_alloc(Fifo *f, int cap) { f->d = calloc(cap, sizeof(float)); f->cap = cap; f->len = 0; return f->d != NULL; }
static void fifo_push(Fifo *f, const float *x, int n) {
  if (f->len + n > f->cap) f->len = 0;   /* não deveria acontecer; descarta em vez de estourar */
  memcpy(f->d + f->len, x, n * sizeof(float));
  f->len += n;
}
static void fifo_pop(Fifo *f, float *x, int n) {
  if (x) memcpy(x, f->d, n * sizeof(float));
  memmove(f->d, f->d + n, (f->len - n) * sizeof(float));
  f->len -= n;
}
/* saída como no Easy Effects: se ainda não há amostras suficientes, completa o começo com zeros */
static void fifo_out(Fifo *fl, Fifo *fr, float *l, float *r, int n) {
  if (fl->len >= n) { fifo_pop(fl, l, n); fifo_pop(fr, r, n); return; }
  int off = n - fl->len;
  memset(l, 0, off * sizeof(float)); memset(r, 0, off * sizeof(float));
  memcpy(l + off, fl->d, fl->len * sizeof(float)); memcpy(r + off, fr->d, fr->len * sizeof(float));
  fl->len = fr->len = 0;
}

/* ------------------------------------------------------------------ RNNoise */

enum { RN_WET = C_COMMON, RN_VAD, RN_REL, RN_ENVAD, RN_N };
static const Ctl rn_ctls[] = {COMMON_CTLS, {"wet", -100, 20, 0, 0}, {"vadThres", 0, 100, 50, 0},
                              {"release", 0, 1000, 20, 0}, {"enableVad", 0, 1, 0, F_TOGGLE}};
#define RN_FRAME 480
typedef struct {
  DenoiseState *st[2];
  float frame[2][RN_FRAME], dry[RN_FRAME];
  int cnt;
  int grace[2];
  float last_release;
  Fifo out[2];
} Rn;
static int rn_init(Inst *I) {
  Rn *R = calloc(1, sizeof(Rn));
  if (!R) return 0;
  I->st = R;
  for (int c = 0; c < 2; c++) {
    R->st[c] = rnnoise_create(NULL);
    if (!R->st[c] || !fifo_alloc(&R->out[c], 65536)) return 0;
  }
  R->last_release = -1;
  return 1;
}
static void rn_done(Inst *I) {
  Rn *R = I->st;
  if (!R) return;
  for (int c = 0; c < 2; c++) { if (R->st[c]) rnnoise_destroy(R->st[c]); free(R->out[c].d); }
}
static void rn_process(Inst *I, float *l, float *r, int n) {
  Rn *R = I->st;
  if (I->rate != 48000) return;          /* o RNNoise só trabalha a 48 kHz; em outra taxa passa direto */
  int empty = 1;
  for (int i = 0; i < n && empty; i++) empty = fabsf(l[i]) <= 1e-6f && fabsf(r[i]) <= 1e-6f;
  if (empty) { memset(l, 0, n * sizeof(float)); memset(r, 0, n * sizeof(float)); return; }
  float wetdb = P(I, RN_WET), wet = wetdb <= -99 ? 0 : db2lin(wetdb);
  int release = (int)lrint(48000.0 * P(I, RN_REL) / 1000.0 / RN_FRAME);
  if (P(I, RN_REL) != R->last_release) { R->last_release = P(I, RN_REL); R->grace[0] = R->grace[1] = release; }
  int vad_on = P(I, RN_ENVAD) > .5f;
  float thr = P(I, RN_VAD) * 0.01f, vadmax = 0;
  for (int i = 0; i < n; i++) {
    R->frame[0][R->cnt] = l[i] * 32768.f;
    R->frame[1][R->cnt] = r[i] * 32768.f;
    if (++R->cnt < RN_FRAME) continue;
    R->cnt = 0;
    for (int c = 0; c < 2; c++) {
      float *d = R->frame[c];
      memcpy(R->dry, d, sizeof R->dry);
      float prob = rnnoise_process_frame(R->st[c], d, d);
      if (prob > vadmax) vadmax = prob;
      int keep = 1;
      if (vad_on) {
        if (prob >= thr) R->grace[c] = release;
        if (R->grace[c] >= 0) R->grace[c]--;
        else keep = 0;
      }
      if (keep) for (int k = 0; k < RN_FRAME; k++) d[k] = (d[k] * wet + R->dry[k] * (1 - wet)) / 32768.f;
      else memset(d, 0, sizeof R->frame[c]);
      fifo_push(&R->out[c], d, RN_FRAME);
    }
  }
  fifo_out(&R->out[0], &R->out[1], l, r, n);
  if (I->lv && P(I, C_SLOT) >= 0) I->lv->extra[(int)P(I, C_SLOT)][0] = vadmax;
}

/* ------------------------------------------------------------------ compressor */

enum { CO_MODE = C_COMMON, CO_THR, CO_RATIO, CO_KNEE, CO_MAKEUP, CO_ATK, CO_REL, CO_BTHR, CO_BAMT,
       CO_HPFM, CO_HPFF, CO_LPFM, CO_LPFF, CO_SCMODE, CO_SCREACT, CO_N };
static const Ctl co_ctls[] = {COMMON_CTLS, {"mode", 0, 2, 0, F_INT}, {"threshold", -60, 0, -20, 0},
                              {"ratio", 1, 100, 4, 0}, {"knee", -24, 0, -6, 0}, {"makeup", -36, 36, 0, 0},
                              {"attack", 0, 2000, 20, 0}, {"release", 0, 5000, 100, 0},
                              {"boostThreshold", -84, 0, -60, 0}, {"boostAmount", 0, 36, 6, 0},
                              {"hpfMode", 0, 3, 0, F_INT}, {"hpfFrequency", 10, 20000, 10, 0},
                              {"lpfMode", 0, 3, 0, F_INT}, {"lpfFrequency", 10, 20000, 20000, 0},
                              {"sidechainMode", 0, 3, 0, F_INT}, {"sidechainReactivity", 0, 250, 10, 0}};
typedef struct {
  double ms, env;
  float key[4];
  BqC hpc, lpc;
  BqS hps[3], lps[3];
  int hpn, lpn;
} Comp;
static int co_init(Inst *I) { I->st = calloc(1, sizeof(Comp)); ((Comp *)I->st)->key[0] = -1; return I->st != NULL; }
/* curva estática em dB, a mesma de compCurve() da interface */
static double co_curve(double x, int mode, double T, double R, double K, double bt, double ba, double mk) {
  if (R < 1) R = 1;
  double y;
#define DOWN(v) (K > 0 && fabs((v) - T) <= K ? (v) + (1 / R - 1) * ((v) - T + K) * ((v) - T + K) / (4 * K) : (v) > T ? T + ((v) - T) / R : (v))
#define UP(v) (K > 0 && fabs((v) - T) <= K ? (v) + (1 - 1 / R) * ((v) - T - K) * ((v) - T - K) / (4 * K) : (v) < T ? T + ((v) - T) / R : (v))
  if (mode == 1) { double xe = fmax(x, bt); y = x + UP(xe) - xe; }
  else if (mode == 2) y = x + fmin(ba, UP(x) - x);
  else y = DOWN(x);
#undef DOWN
#undef UP
  return y + mk;
}
static double lsp_tau(double ms, double rate) {
  /* como no LSP: tempo para chegar a 1/√2 do caminho */
  return ms <= 0 ? 1 : 1 - exp(log(1 - M_SQRT1_2) / (ms * 0.001 * rate));
}
static void co_process(Inst *I, float *l, float *r, int n) {
  Comp *C = I->st;
  float key[4] = {P(I, CO_HPFM), P(I, CO_HPFF), P(I, CO_LPFM), P(I, CO_LPFF)};
  if (memcmp(key, C->key, sizeof key)) {
    memcpy(C->key, key, sizeof key);
    C->hpn = (int)lrintf(key[0]); C->lpn = (int)lrintf(key[2]);
    C->hpn = C->hpn < 0 ? 0 : C->hpn > 3 ? 3 : C->hpn;
    C->lpn = C->lpn < 0 ? 0 : C->lpn > 3 ? 3 : C->lpn;
    bq_design(&C->hpc, K_HP, I->rate, key[1], 0, .707);
    bq_design(&C->lpc, K_LP, I->rate, key[3], 0, .707);
    memset(C->hps, 0, sizeof C->hps); memset(C->lps, 0, sizeof C->lps);
  }
  int mode = (int)lrintf(P(I, CO_MODE));
  double T = P(I, CO_THR), Ra = P(I, CO_RATIO), K = fabs(P(I, CO_KNEE)), mk = P(I, CO_MAKEUP);
  double bt = P(I, CO_BTHR), ba = P(I, CO_BAMT);
  double ta = lsp_tau(P(I, CO_ATK), I->rate), tr = lsp_tau(P(I, CO_REL), I->rate);
  /* sidechain como no Easy Effects: pico (padrão) ou RMS com a reatividade dada (10 ms) */
  int peak = (int)lrintf(P(I, CO_SCMODE)) == 0;
  double react = fmax(P(I, CO_SCREACT), 0.1) * 0.001;
  double a_rms = 1 - exp(-1 / (react * I->rate));
  double g = 0;
  for (int i = 0; i < n; i++) {
    float s = 0.5f * (l[i] + r[i]);
    for (int k = 0; k < C->hpn; k++) s = bq_run(&C->hpc, &C->hps[k], s);
    for (int k = 0; k < C->lpn; k++) s = bq_run(&C->lpc, &C->lps[k], s);
    double lvl;
    if (peak) lvl = fabsf(s);
    else { C->ms += a_rms * (s * s - C->ms); lvl = sqrt(C->ms); }
    C->env += (lvl > C->env ? ta : tr) * (lvl - C->env);
    double x = 20 * log10(C->env + 1e-10);
    if (x < -150) x = -150;
    g = co_curve(x, mode, T, Ra, K, bt, ba, mk) - x;
    float lin = (float)pow(10, g / 20);
    l[i] *= lin; r[i] *= lin;
  }
  if (I->lv && P(I, C_SLOT) >= 0) I->lv->extra[(int)P(I, C_SLOT)][0] = (float)g;
}

/* ------------------------------------------------------------------ Crystalizer (porte do Easy Effects) */

#define CR_NB 13
enum { CR_INT0 = C_COMMON, CR_MUTE0 = CR_INT0 + CR_NB, CR_BYP0 = CR_MUTE0 + CR_NB, CR_ADAPT = CR_BYP0 + CR_NB,
       CR_OVER, CR_TB, CR_N };
static Ctl cr_ctls[CR_N] = {COMMON_CTLS};
static char cr_names[3 * CR_NB][24];
static void cr_make_ctls(void) {
  for (int b = 0; b < CR_NB; b++) {
    snprintf(cr_names[b], 24, "intensityBand%d", b);
    snprintf(cr_names[CR_NB + b], 24, "muteBand%d", b);
    snprintf(cr_names[2 * CR_NB + b], 24, "bypassBand%d", b);
    cr_ctls[CR_INT0 + b] = (Ctl){cr_names[b], -40, 32, 0, 0};
    cr_ctls[CR_MUTE0 + b] = (Ctl){cr_names[CR_NB + b], 0, 1, 0, F_TOGGLE};
    cr_ctls[CR_BYP0 + b] = (Ctl){cr_names[2 * CR_NB + b], 0, 1, 0, F_TOGGLE};
  }
  cr_ctls[CR_ADAPT] = (Ctl){"adaptiveIntensity", 0, 1, 1, F_TOGGLE};
  cr_ctls[CR_OVER] = (Ctl){"oversampling", 0, 1, 1, F_TOGGLE};
  cr_ctls[CR_TB] = (Ctl){"transitionBand", 60, 1000, 120, 0};
}

/* laços quentes ganham uma versão AVX2/FMA, escolhida sozinha na hora de carregar (processadores sem AVX2
   usam a versão comum) */
#if defined(__x86_64__) && defined(__GNUC__) && !defined(__clang__) && !defined(_WIN32)   /* sem ifunc no Windows */
#define HOT __attribute__((target_clones("arch=x86-64-v3", "default")))
#else
#define HOT
#endif

/* FFT complexa radix-2 em vetores separados (real/imag), com os fatores de giro de cada estágio contíguos */
typedef struct { int n; float *wr, *wi; int *rev; } FFT;
static int fft_alloc(FFT *f, int nmax) {
  f->wr = malloc(nmax * sizeof(float)); f->wi = malloc(nmax * sizeof(float)); f->rev = malloc(nmax * sizeof(int));
  f->n = 0;
  return f->wr && f->wi && f->rev;
}
static void fft_setup(FFT *f, int n) {
  f->n = n;
  int lg = 0;
  while ((1 << lg) < n) lg++;
  /* estágio de tamanho len usa wr[len/2 .. len-1] */
  for (int len = 2; len <= n; len <<= 1)
    for (int j = 0; j < len / 2; j++) {
      double a = -2 * M_PI * j / len;
      f->wr[len / 2 + j] = (float)cos(a); f->wi[len / 2 + j] = (float)sin(a);
    }
  for (int i = 0; i < n; i++) {
    int r = 0;
    for (int b = 0; b < lg; b++) if (i & (1 << b)) r |= 1 << (lg - 1 - b);
    f->rev[i] = r;
  }
}
HOT static void fft_run(const FFT *f, float *restrict xr, float *restrict xi, int inv) {
  int n = f->n;
  for (int i = 0; i < n; i++) {
    int j = f->rev[i];
    if (j > i) { float t = xr[i]; xr[i] = xr[j]; xr[j] = t; t = xi[i]; xi[i] = xi[j]; xi[j] = t; }
  }
  for (int i = 0; i < n; i += 2) {   /* estágio 2: só somas */
    float ar = xr[i], ai = xi[i], br = xr[i + 1], bi = xi[i + 1];
    xr[i] = ar + br; xi[i] = ai + bi; xr[i + 1] = ar - br; xi[i + 1] = ai - bi;
  }
  float s = inv ? -1.f : 1.f;
  for (int len = 4; len <= n; len <<= 1) {
    int half = len >> 1;
    const float *wr = f->wr + half, *wi = f->wi + half;
    for (int i = 0; i < n; i += len) {
      float *ur = xr + i, *ui = xi + i, *vr = xr + i + half, *vi = xi + i + half;
      for (int j = 0; j < half; j++) {
        float w_r = wr[j], w_i = s * wi[j];
        float tr = vr[j] * w_r - vi[j] * w_i, ti = vr[j] * w_i + vi[j] * w_r;
        float a = ur[j], b = ui[j];
        ur[j] = a + tr; ui[j] = b + ti; vr[j] = a - tr; vi[j] = b - ti;
      }
    }
  }
}
HOT static void cmac(float *restrict ar, float *restrict ai, const float *restrict xr, const float *restrict xi,
                     const float *restrict hr, const float *restrict hi, int n) {
  for (int k = 0; k < n; k++) {
    ar[k] += xr[k] * hr[k] - xi[k] * hi[k];
    ai[k] += xr[k] * hi[k] + xi[k] * hr[k];
  }
}

/* passa-baixa para sobreamostrar 2x (o Easy Effects usa o resampler do Speex, qualidade 5) */
#define HB_TAPS 96
static float hb_coef[HB_TAPS], hb_even[HB_TAPS / 2], hb_odd[HB_TAPS / 2];
static void hb_design(void) {
  double fc = 0.2275;   /* da taxa alta: ~21,8 kHz a 96 kHz */
  double sum = 0;
  for (int k = 0; k < HB_TAPS; k++) {
    double m = k - (HB_TAPS - 1) / 2.0;
    double sv = m == 0 ? 2 * fc : sin(2 * M_PI * fc * m) / (M_PI * m);
    double w = 0.42 - 0.5 * cos(2 * M_PI * k / (HB_TAPS - 1)) + 0.08 * cos(4 * M_PI * k / (HB_TAPS - 1));
    hb_coef[k] = (float)(sv * w);
    sum += hb_coef[k];
  }
  for (int k = 0; k < HB_TAPS; k++) hb_coef[k] /= (float)sum;
  /* polifásico: na subida, as amostras pares usam os coeficientes pares e as ímpares, os ímpares */
  for (int j = 0; j < HB_TAPS / 2; j++) { hb_even[j] = 2 * hb_coef[2 * j]; hb_odd[j] = 2 * hb_coef[2 * j + 1]; }
}
/* histórico duplicado: os últimos L valores ficam contíguos em h[pos .. pos+L-1] (mais velho primeiro) */
typedef struct { float h[2 * HB_TAPS]; int pos; } Hist;
static inline void hist_push(Hist *H, float x, int L) {
  H->h[H->pos] = H->h[H->pos + L] = x;
  H->pos = H->pos + 1 == L ? 0 : H->pos + 1;
}
HOT static float dotrev(const float *restrict c, const float *restrict x, int L) {   /* Σ c[k]·x[L-1-k] */
  float acc = 0;
  for (int k = 0; k < L; k++) acc += c[k] * x[L - 1 - k];
  return acc;
}

typedef struct {
  /* configuração atual */
  int B, N, P, Bin, klen, over;
  double irate;
  float tb;
  int ready;
  /* núcleos FIR (no tempo) e seus espectros por partição */
  int kmax, bmax;
  float *kern[CR_NB];
  float *Hr, *Hi;          /* [CR_NB][P][N] */
  float *Xr, *Xi;          /* linha de atraso no domínio da frequência [P][N] (L + iR) */
  float *xr, *xi, *ar, *ai;
  int xpos;
  FFT fft;
  /* blocos */
  float *dl, *dr, *band[CR_NB][2], *d2[2], *bprevd2[CR_NB][2], *gd2[2], *gprev[2];
  float band_prev[CR_NB][2], band_next[CR_NB][2];
  float env_k[CR_NB][2], env_c[CR_NB][2], env_f[CR_NB][2];
  float g_crest[2], g_kurt[2], g_flux[2], g_prev[2];
  float att, rel;
  int first;
  Fifo fin[2], fout[2];
  Hist up[2], down[2];
  float *tmp;
} Crys;

static void cr_edges(float *e) {
  float gamma = 0.413f, r = 20000.f / 20.f;
  for (int i = 0; i <= CR_NB; i++) e[i] = 20.f * powf(r, powf((float)i / CR_NB, gamma));
}
static void lowpass_kernel(float *out, int len, double rate, float cutoff) {
  /* create_lowpass_kernel() do Easy Effects: sinc janelado (Blackman), normalizado para soma 1 */
  int M = len - 1;
  double fc = cutoff / rate, sum = 0;
  for (int n = 0; n < len; n++) {
    int off = n - M / 2;
    double v = off == 0 ? 2 * M_PI * fc : sin(2 * M_PI * fc * off) / off;
    double w = 0.42 - 0.5 * cos(2 * M_PI * n / M) + 0.08 * cos(4 * M_PI * n / M);
    out[n] = (float)(v * w);
    sum += out[n];
  }
  for (int n = 0; n < len; n++) out[n] /= (float)sum;
}
static int cr_klen(double irate, float tb) {
  double b = tb / irate;
  int M = (int)ceil(4.0 / b);
  if (M % 2) M++;
  return M + 1;
}
static void cr_kernels(Crys *C) {
  float e[CR_NB + 1];
  cr_edges(e);
  float *hp = C->tmp;
  for (int b = 0; b < CR_NB; b++) {
    float *k = C->kern[b];
    lowpass_kernel(k, C->klen, C->irate, e[b + 1]);
    lowpass_kernel(hp, C->klen, C->irate, e[b]);
    int mid = (C->klen - 1) / 2;
    for (int n = 0; n < C->klen; n++) hp[n] = -hp[n];
    hp[mid] += 1;
    for (int n = 0; n < C->klen; n++) k[n] = -(k[n] + hp[n]);
    k[mid] += 1;
  }
}
static int cr_init(Inst *I) {
  Crys *C = calloc(1, sizeof(Crys));
  if (!C) return 0;
  I->st = C;
  double imax = 2 * I->rate;
  C->kmax = cr_klen(imax, 60) + 2;
  C->bmax = 8192;
  int nmax = 2 * C->bmax;
  size_t spec = 2 * (size_t)C->kmax + 2 * (size_t)nmax;   /* ≥ P*N para qualquer B */
  int ok = fft_alloc(&C->fft, nmax);
  C->Hr = calloc(CR_NB * spec, sizeof(float)); C->Hi = calloc(CR_NB * spec, sizeof(float));
  C->Xr = calloc(spec, sizeof(float)); C->Xi = calloc(spec, sizeof(float));
  C->xr = calloc(nmax, sizeof(float)); C->xi = calloc(nmax, sizeof(float));
  C->ar = calloc(nmax, sizeof(float)); C->ai = calloc(nmax, sizeof(float));
  C->tmp = calloc(C->kmax > nmax ? C->kmax : nmax, sizeof(float));
  C->dl = calloc(C->bmax, sizeof(float)); C->dr = calloc(C->bmax, sizeof(float));
  ok = ok && C->Hr && C->Hi && C->Xr && C->Xi && C->xr && C->xi && C->ar && C->ai && C->tmp && C->dl && C->dr;
  for (int b = 0; b < CR_NB && ok; b++) {
    C->kern[b] = calloc(C->kmax, sizeof(float));
    ok = ok && C->kern[b];
    for (int c = 0; c < 2; c++) {
      C->band[b][c] = calloc(C->bmax, sizeof(float));
      C->bprevd2[b][c] = calloc(C->bmax, sizeof(float));
      ok = ok && C->band[b][c] && C->bprevd2[b][c];
    }
  }
  for (int c = 0; c < 2 && ok; c++) {
    C->d2[c] = calloc(C->bmax, sizeof(float));
    C->gd2[c] = calloc(C->bmax, sizeof(float));
    C->gprev[c] = calloc(C->bmax, sizeof(float));
    ok = C->d2[c] && C->gd2[c] && C->gprev[c] && fifo_alloc(&C->fin[c], 4 * nmax) && fifo_alloc(&C->fout[c], 4 * nmax);
  }
  return ok;
}
static void cr_done(Inst *I) {
  Crys *C = I->st;
  if (!C) return;
  free(C->fft.wr); free(C->fft.wi); free(C->fft.rev);
  free(C->Hr); free(C->Hi); free(C->Xr); free(C->Xi); free(C->xr); free(C->xi); free(C->ar); free(C->ai);
  free(C->tmp); free(C->dl); free(C->dr);
  for (int b = 0; b < CR_NB; b++) {
    free(C->kern[b]);
    for (int c = 0; c < 2; c++) { free(C->band[b][c]); free(C->bprevd2[b][c]); }
  }
  for (int c = 0; c < 2; c++) { free(C->d2[c]); free(C->gd2[c]); free(C->gprev[c]); free(C->fin[c].d); free(C->fout[c].d); }
}
/* refaz núcleos/espectros/estado: na primeira vez e quando o tamanho do bloco, a sobreamostragem ou a banda de
   transição mudam (como o setup() do Easy Effects) */
static void cr_setup(Inst *I, int n) {
  Crys *C = I->st;
  int over = P(I, CR_OVER) > .5f;
  float tb = P(I, CR_TB);
  tb = tb < 60 ? 60 : tb > 1000 ? 1000 : tb;
  int bin = 1;
  while (bin * 2 <= n) bin *= 2;
  bin = bin < 32 ? 32 : bin > 4096 ? 4096 : bin;
  int B = over ? 2 * bin : bin;
  if (B < 64) B = 64;
  double irate = over ? 2 * I->rate : I->rate;
  int kchange = !C->ready || irate != C->irate || tb != C->tb;
  C->Bin = bin; C->B = B; C->N = 2 * B; C->over = over;
  if (kchange) { C->irate = irate; C->tb = tb; C->klen = cr_klen(irate, tb); cr_kernels(C); }
  C->P = (C->klen + B - 1) / B;
  fft_setup(&C->fft, C->N);
  int N = C->N;
  for (int b = 0; b < CR_NB; b++)
    for (int p = 0; p < C->P; p++) {
      float *hr = C->Hr + ((size_t)b * C->P + p) * N, *hi = C->Hi + ((size_t)b * C->P + p) * N;
      memset(hr, 0, N * sizeof(float)); memset(hi, 0, N * sizeof(float));
      for (int k = 0; k < B && p * B + k < C->klen; k++) hr[k] = C->kern[b][p * B + k];
      fft_run(&C->fft, hr, hi, 0);
    }
  memset(C->Xr, 0, (size_t)C->P * N * sizeof(float)); memset(C->Xi, 0, (size_t)C->P * N * sizeof(float));
  memset(C->xr, 0, N * sizeof(float)); memset(C->xi, 0, N * sizeof(float));
  C->xpos = 0;
  /* block_time do Easy Effects: quantum de entrada / taxa de entrada */
  double bt = (double)bin / I->rate;
  C->att = (float)exp(-bt / 0.4); C->rel = (float)exp(-bt / 3.0);
  for (int b = 0; b < CR_NB; b++)
    for (int c = 0; c < 2; c++) {
      C->band_prev[b][c] = 0; C->env_k[b][c] = 3; C->env_c[b][c] = 1; C->env_f[b][c] = 1;
      memset(C->bprevd2[b][c], 0, C->bmax * sizeof(float));
    }
  for (int c = 0; c < 2; c++) {
    C->g_crest[c] = C->g_kurt[c] = C->g_flux[c] = 1; C->g_prev[c] = 0;
    memset(C->gprev[c], 0, C->bmax * sizeof(float));
    C->fin[c].len = C->fout[c].len = 0;
    memset(&C->up[c], 0, sizeof(Hist)); memset(&C->down[c], 0, sizeof(Hist));
  }
  C->first = 1;
  C->ready = 1;
}
HOT static float cr_kurtosis(const float *d, int n) {
  float mean = 0, m2 = 0, m4 = 0;
  for (int i = 0; i < n; i++) mean += d[i];
  mean /= n;
  for (int i = 0; i < n; i++) { float v = d[i] - mean, v2 = v * v; m2 += v2; m4 += v2 * v2; }
  m2 /= n; m4 /= n;
  return m2 > 1e-6f ? m4 / (m2 * m2) : 3.f;
}
HOT static float cr_crest(const float *d, int n) {
  float rms = 0, peak = 0;
  for (int i = 0; i < n; i++) { float v = fabsf(d[i]); rms += v * v; peak = v > peak ? v : peak; }
  rms = sqrtf(rms / n);
  return rms > 1e-6f ? peak / rms : 1.f;
}
HOT static float cr_flux(const float *restrict d, float *restrict prev, int n) {
  float f = 0;
  for (int i = 0; i < n; i++) f += fabsf(d[i] - prev[i]);
  memcpy(prev, d, n * sizeof(float));
  f /= n;
  return f > 1e-6f ? f : 1.f;
}
static inline void cr_env(float *env, float v, float att, float rel) {
  float a = v > *env ? att : rel;
  *env = a * *env + (1 - a) * v;
}
HOT static void second_derivative(float *restrict d2, const float *restrict x, int n, float prev, float next) {
  d2[0] = x[1] - 2 * x[0] + prev;
  for (int m = 1; m < n - 1; m++) d2[m] = x[m + 1] - 2 * x[m] + x[m - 1];
  d2[n - 1] = next - 2 * x[n - 1] + x[n - 2];
}
HOT static void axpy_neg(float *restrict x, const float *restrict d2, float it, int n) {
  for (int m = 0; m < n; m++) x[m] -= it * d2[m];
}
HOT static void add_to(float *restrict y, const float *restrict x, int n) {
  for (int m = 0; m < n; m++) y[m] += x[m];
}
/* enhance_peaks() do Easy Effects, num bloco de B amostras (na taxa interna) */
static void cr_block(Inst *I, float *dl, float *dr) {
  Crys *C = I->st;
  int B = C->B, N = C->N;
  float *data[2] = {dl, dr};
  /* divisão em bandas: convolução particionada (overlap-save), L e R juntos num FFT complexo */
  memmove(C->xr, C->xr + B, B * sizeof(float));
  memmove(C->xi, C->xi + B, B * sizeof(float));
  memcpy(C->xr + B, dl, B * sizeof(float));
  memcpy(C->xi + B, dr, B * sizeof(float));
  float *kr = C->Xr + (size_t)C->xpos * N, *ki = C->Xi + (size_t)C->xpos * N;
  memcpy(kr, C->xr, N * sizeof(float));
  memcpy(ki, C->xi, N * sizeof(float));
  fft_run(&C->fft, kr, ki, 0);
  float inv = 1.f / N;
  for (int b = 0; b < CR_NB; b++) {
    memset(C->ar, 0, N * sizeof(float)); memset(C->ai, 0, N * sizeof(float));
    for (int p = 0; p < C->P; p++) {
      size_t xo = (size_t)((C->xpos - p + C->P) % C->P) * N, ho = ((size_t)b * C->P + p) * N;
      cmac(C->ar, C->ai, C->Xr + xo, C->Xi + xo, C->Hr + ho, C->Hi + ho, N);
    }
    fft_run(&C->fft, C->ar, C->ai, 1);
    float *bl = C->band[b][0], *br = C->band[b][1];
    for (int i = 0; i < B; i++) { bl[i] = C->ar[B + i] * inv; br[i] = C->ai[B + i] * inv; }
    for (int c = 0; c < 2; c++) {
      float *x = C->band[b][c];
      C->band_next[b][c] = 2 * x[B - 1] - x[B - 2];
      if (C->first) C->band_prev[b][c] = x[0];
    }
  }
  C->xpos = (C->xpos + 1) % C->P;

  int adaptive = P(I, CR_ADAPT) > .5f;
  if (adaptive) {
    for (int c = 0; c < 2; c++) {
      float *x = data[c];
      if (C->first) C->g_prev[c] = x[0];
      second_derivative(C->gd2[c], x, B, C->g_prev[c], 2 * x[B - 1] - x[B - 2]);
      C->g_prev[c] = x[B - 1];
      cr_env(&C->g_crest[c], cr_crest(C->gd2[c], B), C->att, C->rel);
      cr_env(&C->g_kurt[c], cr_kurtosis(C->gd2[c], B), C->att, C->rel);
      cr_env(&C->g_flux[c], cr_flux(C->gd2[c], C->gprev[c], B), C->att, C->rel);
    }
  }
  int slot = (int)P(I, C_SLOT);
  for (int b = 0; b < CR_NB; b++) {
    if (P(I, CR_BYP0 + b) <= .5f) {
      float base = db2lin(P(I, CR_INT0 + b)), it[2];
      for (int c = 0; c < 2; c++) {
        float *x = C->band[b][c], *d2 = C->d2[c];
        second_derivative(d2, x, B, C->band_prev[b][c], C->band_next[b][c]);
        it[c] = base;
        if (adaptive) {
          cr_env(&C->env_k[b][c], cr_kurtosis(d2, B), C->att, C->rel);
          cr_env(&C->env_c[b][c], cr_crest(d2, B), C->att, C->rel);
          cr_env(&C->env_f[b][c], cr_flux(d2, C->bprevd2[b][c], B), C->att, C->rel);
          float kr2 = C->g_kurt[c] / C->env_k[b][c], cr = C->g_crest[c] / C->env_c[b][c], fr = C->g_flux[c] / C->env_f[b][c];
          it[c] = base * cbrtf(cr * kr2 * fr);
        }
        axpy_neg(x, d2, it[c], B);
      }
      if (adaptive && I->lv && slot >= 0 && slot < NSLOT) I->lv->extra[slot][b] = 20 * log10f(0.5f * (it[0] + it[1]) + 1e-9f);
    }
    for (int c = 0; c < 2; c++) C->band_prev[b][c] = C->band[b][c][B - 1];
  }
  memset(dl, 0, B * sizeof(float));
  memset(dr, 0, B * sizeof(float));
  for (int b = 0; b < CR_NB; b++) {
    if (P(I, CR_MUTE0 + b) > .5f) continue;
    add_to(dl, C->band[b][0], B);
    add_to(dr, C->band[b][1], B);
  }
  C->first = 0;
}
static void cr_process(Inst *I, float *l, float *r, int n) {
  Crys *C = I->st;
  if (n > C->bmax) return;             /* quantum acima de 8192: passa direto */
  int over = P(I, CR_OVER) > .5f;
  float tb = P(I, CR_TB);
  tb = tb < 60 ? 60 : tb > 1000 ? 1000 : tb;
  int bin = 1;
  while (bin * 2 <= n) bin *= 2;
  bin = bin < 32 ? 32 : bin > 4096 ? 4096 : bin;
  if (!C->ready || over != C->over || tb != C->tb || bin != C->Bin) cr_setup(I, n);
  float *ch[2] = {l, r};
  float *tmp = C->tmp;
  const int L2 = HB_TAPS / 2;
  for (int c = 0; c < 2; c++) {
    if (!C->over) { fifo_push(&C->fin[c], ch[c], n); continue; }
    Hist *H = &C->up[c];
    for (int i = 0; i < n; i++) {   /* 2x polifásico (= zero entre amostras + passa-baixa com ganho 2) */
      hist_push(H, ch[c][i], L2);
      const float *x = H->h + H->pos;
      tmp[2 * i] = dotrev(hb_even, x, L2);
      tmp[2 * i + 1] = dotrev(hb_odd, x, L2);
    }
    fifo_push(&C->fin[c], tmp, 2 * n);
  }
  while (C->fin[0].len >= C->B) {
    fifo_pop(&C->fin[0], C->dl, C->B);
    fifo_pop(&C->fin[1], C->dr, C->B);
    cr_block(I, C->dl, C->dr);
    float *d[2] = {C->dl, C->dr};
    for (int c = 0; c < 2; c++) {
      if (!C->over) { fifo_push(&C->fout[c], d[c], C->B); continue; }
      int h = C->B / 2;
      Hist *H = &C->down[c];
      for (int i = 0; i < h; i++) {   /* passa-baixa e fica com uma de cada duas */
        hist_push(H, d[c][2 * i], HB_TAPS);
        tmp[i] = dotrev(hb_coef, H->h + H->pos, HB_TAPS);
        hist_push(H, d[c][2 * i + 1], HB_TAPS);
      }
      fifo_push(&C->fout[c], tmp, h);
    }
  }
  fifo_out(&C->fout[0], &C->fout[1], l, r, n);
}

/* ------------------------------------------------------------------ LADSPA */

static void level_acc(Inst *I, int which, const float *l, const float *r, int n) {
  double sl = 0, sr = 0;
  for (int i = 0; i < n; i++) { sl += l[i] * l[i]; sr += r[i] * r[i]; }
  float a = 1 - expf(-(float)n / (0.010f * (float)I->rate));
  I->ms[which] += a * ((float)(sl / n) - I->ms[which]);
  I->ms[which + 1] += a * ((float)(sr / n) - I->ms[which + 1]);
}

static LADSPA_Handle instantiate(const LADSPA_Descriptor *d, unsigned long rate) {
  const Def *def = d->ImplementationData;
  Inst *I = calloc(1, sizeof(Inst));
  if (!I) return NULL;
  I->def = def;
  I->rate = rate;
  I->lv = levels_map();
  I->gin = I->gout = 1;
  I->gk = 1 - expf(-1.f / (0.005f * rate));
  if (!def->init(I)) {
    if (def->done) def->done(I);
    free(I->st); free(I);
    return NULL;
  }
  return I;
}
static void connect_port(LADSPA_Handle h, unsigned long port, LADSPA_Data *p) {
  Inst *I = h;
  if (port < 2) I->in[port] = p;
  else if (port < 4) I->out[port - 2] = p;
  else if (port - 4 < MAXCTL) I->c[port - 4] = p;
}
static void run_body(Inst *I, int n);
static void run(LADSPA_Handle h, unsigned long count) {
  Inst *I = h;
  int n = (int)count;
  if (n <= 0) return;
#if defined(__x86_64__) || defined(__i386__)
  /* números denormais (filtros decaindo no silêncio) deixam tudo até 15x mais lento: zera eles */
  unsigned csr = _mm_getcsr();
  _mm_setcsr(csr | 0x8040);
  double a = now_us();
  run_body(I, n);
  float us = (float)(now_us() - a);
  _mm_setcsr(csr);
  /* tempo gasto (µs, média móvel) e tamanho do último bloco, para diagnóstico */
  int slot = (int)P(I, C_SLOT);
  if (I->lv && slot >= 0 && slot < NSLOT) {
    I->lv->extra[slot][14] += 0.05f * (us - I->lv->extra[slot][14]);
    I->lv->extra[slot][15] = (float)n;
  }
#else
  run_body(I, n);
#endif
}
static void run_body(Inst *I, int n) {
  if (n > I->wcap) {   /* só acontece se o quantum crescer; o PipeWire chama com no máximo o quantum máximo */
    free(I->wl); free(I->wr);
    I->wl = malloc(n * sizeof(float)); I->wr = malloc(n * sizeof(float));
    I->wcap = I->wl && I->wr ? n : 0;
    if (!I->wcap) return;
  }
  const float *il = I->in[0], *ir = I->in[1] ? I->in[1] : I->in[0];
  float *ol = I->out[0], *orr = I->out[1];
  if (!il || !ol) return;
  level_acc(I, 0, il, ir, n);
  if (P(I, C_BYPASS) > .5f) {
    memmove(ol, il, n * sizeof(float));
    if (orr) memmove(orr, ir, n * sizeof(float));
  } else {
    float gi = db2lin(P(I, C_INGAIN)), go = db2lin(P(I, C_OUTGAIN));
    for (int i = 0; i < n; i++) {
      I->gin += I->gk * (gi - I->gin);
      I->wl[i] = il[i] * I->gin; I->wr[i] = ir[i] * I->gin;
    }
    I->def->process(I, I->wl, I->wr, n);
    for (int i = 0; i < n; i++) {
      I->gout += I->gk * (go - I->gout);
      ol[i] = I->wl[i] * I->gout;
      if (orr) orr[i] = I->wr[i] * I->gout;
    }
  }
  level_acc(I, 2, ol, orr ? orr : ol, n);
  int slot = (int)P(I, C_SLOT);
  if (I->lv && slot >= 0 && slot < NSLOT) memcpy(I->lv->ms[slot], I->ms, sizeof I->ms);
}
static void cleanup(LADSPA_Handle h) {
  Inst *I = h;
  if (I->def->done) I->def->done(I);
  free(I->st); free(I->wl); free(I->wr);
#ifndef _WIN32
  if (I->lv) munmap(I->lv, sizeof(Levels));
#endif
  free(I);
}

static Def defs[] = {
  {0x4d455301, "mesa_filter", "Mesa Filtro", filter_ctls, FI_N, filter_init, filter_process, NULL},
  {0x4d455302, "mesa_eq", "Mesa Equalizador", eq_ctls, C_COMMON + 1 + EQ_BANDS * B_N, eq_init, eq_process, NULL},
  {0x4d455303, "mesa_rnnoise", "Mesa De-noiser (RNNoise)", rn_ctls, RN_N, rn_init, rn_process, rn_done},
  {0x4d455304, "mesa_compressor", "Mesa Compressor", co_ctls, CO_N, co_init, co_process, NULL},
  {0x4d455305, "mesa_crystalizer", "Mesa Crystalizer", cr_ctls, CR_N, cr_init, cr_process, cr_done},
};
#define NDEFS (int)(sizeof defs / sizeof defs[0])

static void build(Def *def) {
  int np = 4 + def->nctl;
  LADSPA_PortDescriptor *pd = calloc(np, sizeof *pd);
  const char **pn = calloc(np, sizeof *pn);
  LADSPA_PortRangeHint *ph = calloc(np, sizeof *ph);
  static const char *audio[4] = {"inL", "inR", "outL", "outR"};
  for (int i = 0; i < 4; i++) {
    pd[i] = LADSPA_PORT_AUDIO | (i < 2 ? LADSPA_PORT_INPUT : LADSPA_PORT_OUTPUT);
    pn[i] = audio[i];
  }
  for (int k = 0; k < def->nctl; k++) {
    const Ctl *c = &def->ctls[k];
    pd[4 + k] = LADSPA_PORT_CONTROL | LADSPA_PORT_INPUT;
    pn[4 + k] = c->name;
    int hint = LADSPA_HINT_BOUNDED_BELOW | LADSPA_HINT_BOUNDED_ABOVE;
    if (c->flags & F_INT) hint |= LADSPA_HINT_INTEGER;
    if (c->flags & F_TOGGLE) hint = LADSPA_HINT_TOGGLED;
    hint |= c->def == 0 ? LADSPA_HINT_DEFAULT_0 : c->def == 1 ? LADSPA_HINT_DEFAULT_1 : LADSPA_HINT_DEFAULT_MIDDLE;
    ph[4 + k] = (LADSPA_PortRangeHint){hint, c->lo, c->hi};
  }
  def->d = (LADSPA_Descriptor){def->id, def->label, LADSPA_PROPERTY_HARD_RT_CAPABLE, def->name, "Mesa",
                               "GPL-3.0-or-later", (unsigned long)np, pd, pn, ph, def,
                               instantiate, connect_port, NULL, run, NULL, NULL, NULL, cleanup};
}

__attribute__((constructor)) static void mesa_fx_init(void) {
  eq_make_ctls();
  cr_make_ctls();
  hb_design();
  for (int i = 0; i < NDEFS; i++) build(&defs[i]);
}

EXPORT const LADSPA_Descriptor *ladspa_descriptor(unsigned long index) {
  return index < (unsigned long)NDEFS ? &defs[index].d : NULL;
}
