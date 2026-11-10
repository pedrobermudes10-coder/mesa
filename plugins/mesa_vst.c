/*
 * MesaFX.dll — a cadeia de efeitos da Mesa como um plugin VST 2.4 para o Equalizer APO (Windows).
 *
 * O Equalizer APO carrega o plugin dentro do serviço de áudio (audiodg.exe), um por dispositivo, com o
 * parâmetro Pipe dizendo qual cadeia ele roda (0 = entrada/microfone, 1 = saída):
 *     Stage: capture
 *     VSTPlugin: Library "C:\Program Files\Mesa\MesaFX.dll" Pipe 0
 * Os efeitos são os mesmos do Linux (mesa_fx.c, incluído aqui). A cadeia vem da memória compartilhada
 * Global\MesaFX, que a Mesa (a interface) edita:
 *   - ajustes ao vivo: a Mesa escreve o valor direto no controle; os plugins leem dali a cada bloco;
 *   - mudar a ordem, adicionar ou remover: a Mesa escreve a cadeia nova no outro banco, troca bank[] e
 *     soma gen[]; uma thread do plugin monta os efeitos novos e o áudio troca no bloco seguinte (ack[]).
 * Quem cria a memória (o primeiro plugin a carregar) preenche ela com %ProgramData%\Mesa\cadeia.bin, que
 * a Mesa grava a cada mudança — assim os efeitos funcionam desde o boot, com ou sem a janela aberta.
 *
 * GPL-3.0-or-later (inclui o Crystalizer portado do Easy Effects).
 */
#include "mesa_fx.c"
#include <sddl.h>
#include "vst2.h"

#define MAXFX 32
#define SHM_MAGIC 0x3158464d /* "MFX1" */
#define FILE_MAGIC 0x4443414d /* "MCAD" */

typedef struct { int32_t kind; float c[MAXCTL]; } FxSlot;     /* kind = índice em ladspa_descriptor() */
typedef struct { int32_t n, pad; FxSlot fx[MAXFX]; } ChainData;
typedef struct {
  uint32_t magic, version;
  volatile LONG gen[2], bank[2], ack[2], beat[2];   /* por cadeia: 0 = entrada, 1 = saída */
  float rate[2], block[2], us[2], chans[2];
  float pipe_ms[2][4];                              /* nível (média quadrática ~10 ms): entrada L R, saída L R */
} ShmHead;
typedef struct {
  union { ShmHead h; char pad[256]; };
  ChainData chain[2][2];                            /* [cadeia][banco] */
  Levels lv;
} MesaShm;
/* deslocamentos usados pela Mesa (mesa_win.py) */
_Static_assert(sizeof(FxSlot) == 4 + 4 * MAXCTL, "FxSlot");
_Static_assert(sizeof(ChainData) == 8 + MAXFX * sizeof(FxSlot), "ChainData");
_Static_assert(__builtin_offsetof(MesaShm, chain) == 256, "chain");

static MesaShm *shm;
static HANDLE shm_handle;
static volatile LONG shm_lock, shm_refs;

static Levels *win_levels(void) { return shm ? &shm->lv : NULL; }

static void load_file(MesaShm *m) {
  wchar_t dir[MAX_PATH], path[MAX_PATH + 32];
  if (!GetEnvironmentVariableW(L"ProgramData", dir, MAX_PATH)) wcscpy(dir, L"C:\\ProgramData");
  swprintf(path, sizeof path / sizeof *path, L"%ls\\Mesa\\cadeia.bin", dir);
  HANDLE f = CreateFileW(path, GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL,
                         OPEN_EXISTING, 0, NULL);
  if (f == INVALID_HANDLE_VALUE) return;
  struct { uint32_t magic, version, maxctl, maxfx; } hd;
  DWORD got = 0;
  if (ReadFile(f, &hd, sizeof hd, &got, NULL) && got == sizeof hd && hd.magic == FILE_MAGIC && hd.version == 1 &&
      hd.maxctl == MAXCTL && hd.maxfx == MAXFX) {
    for (int p = 0; p < 2; p++)
      if (!ReadFile(f, &m->chain[p][0], sizeof(ChainData), &got, NULL) || got != sizeof(ChainData)) {
        memset(&m->chain[p][0], 0, sizeof(ChainData));
        break;
      }
  }
  CloseHandle(f);
}

/* Global\ deixa a memória visível para a Mesa na sessão do usuário (o audiodg roda na sessão 0, como
   LOCAL SERVICE). Rótulo de integridade baixo: senão a Mesa (integridade média) não poderia escrever. */
static void shm_open(void) {
  while (InterlockedCompareExchange(&shm_lock, 1, 0)) Sleep(0);
  if (shm_refs++ == 0) {
    PSECURITY_DESCRIPTOR sd = NULL;
    SECURITY_ATTRIBUTES sa = {sizeof sa, NULL, FALSE};
    if (ConvertStringSecurityDescriptorToSecurityDescriptorW(L"D:(A;;GA;;;WD)S:(ML;;NW;;;LW)", SDDL_REVISION_1, &sd, NULL))
      sa.lpSecurityDescriptor = sd;
    static const wchar_t *names[] = {L"Global\\MesaFX", L"Local\\MesaFX"};
    for (int i = 0; i < 2 && !shm_handle; i++)
      shm_handle = CreateFileMappingW(INVALID_HANDLE_VALUE, &sa, PAGE_READWRITE, 0, sizeof(MesaShm), names[i]);
    if (sd) LocalFree(sd);
    if (shm_handle) {
      shm = MapViewOfFile(shm_handle, FILE_MAP_READ | FILE_MAP_WRITE, 0, 0, sizeof(MesaShm));
      if (shm && shm->h.magic != SHM_MAGIC) {   /* primeiro a abrir: a cadeia salva */
        memset(shm, 0, sizeof *shm);
        load_file(shm);
        shm->h.version = 1;
        shm->h.gen[0] = shm->h.gen[1] = 1;
        shm->lv.magic = 0x4d455341; shm->lv.version = 1;
        MemoryBarrier();
        shm->h.magic = SHM_MAGIC;
      }
    }
  }
  InterlockedExchange(&shm_lock, 0);
}
static void shm_close(void) {
  while (InterlockedCompareExchange(&shm_lock, 1, 0)) Sleep(0);
  if (--shm_refs == 0) {
    if (shm) UnmapViewOfFile(shm);
    if (shm_handle) CloseHandle(shm_handle);
    shm = NULL; shm_handle = NULL;
  }
  InterlockedExchange(&shm_lock, 0);
}

/* ------------------------------------------------------------------ cadeia montada */

typedef struct {
  int n, pipe;
  LONG gen;
  double rate;
  const LADSPA_Descriptor *d[MAXFX];
  LADSPA_Handle h[MAXFX];
} Chain;

static Chain *chain_build(int pipe, LONG gen, int bank, double rate) {
  Chain *c = calloc(1, sizeof *c);
  if (!c) return NULL;
  c->pipe = pipe; c->gen = gen; c->rate = rate;
  if (!shm) return c;
  ChainData *cd = &shm->chain[pipe][bank];
  int n = cd->n < 0 ? 0 : cd->n > MAXFX ? MAXFX : cd->n;
  for (int i = 0; i < n; i++) {
    const LADSPA_Descriptor *d = cd->fx[i].kind >= 0 ? ladspa_descriptor((unsigned long)cd->fx[i].kind) : NULL;
    if (!d) continue;
    LADSPA_Handle h = d->instantiate(d, (unsigned long)rate);
    if (!h) continue;
    for (unsigned long p = 4; p < d->PortCount && p - 4 < MAXCTL; p++) d->connect_port(h, p, &cd->fx[i].c[p - 4]);
    c->d[c->n] = d; c->h[c->n++] = h;
  }
  return c;
}
static void chain_free(Chain *c) {
  if (!c) return;
  for (int i = 0; i < c->n; i++) c->d[i]->cleanup(c->h[i]);
  free(c);
}

/* ------------------------------------------------------------------ plugin */

typedef struct {
  AEffect e;
  audioMasterCallback host;
  float pipe_param;
  double rate;
  int block, stereo;
  volatile LONG running, quit;
  Chain *cur;                         /* só o áudio troca; só a thread de montagem libera */
  Chain *volatile pending, *volatile retired;
  HANDLE thread, ev;
  CRITICAL_SECTION build;
  float ms[4];
} Plug;

static int pipe_of(Plug *pl) { return pl->pipe_param > .5f; }

static int needs_build(Plug *pl, LONG *g, int *b) {
  int pipe = pipe_of(pl);
  *g = shm ? shm->h.gen[pipe] : 0;
  MemoryBarrier();
  *b = shm ? shm->h.bank[pipe] & 1 : 0;
  Chain *c = pl->cur;
  return !c || c->gen != *g || c->pipe != pipe || c->rate != pl->rate;
}

static DWORD WINAPI builder(void *arg) {
  Plug *pl = arg;
  while (!pl->quit) {
    WaitForSingleObject(pl->ev, 50);
    chain_free(InterlockedExchangePointer((void *volatile *)&pl->retired, NULL));
    if (!pl->running || pl->pending || pl->retired) continue;
    EnterCriticalSection(&pl->build);
    LONG g; int b;
    if (pl->running && needs_build(pl, &g, &b))
      InterlockedExchangePointer((void *volatile *)&pl->pending, chain_build(pipe_of(pl), g, b, pl->rate));
    LeaveCriticalSection(&pl->build);
  }
  return 0;
}

static void levels(Plug *pl, int which, const float *l, const float *r, int n) {
  double sl = 0, sr = 0;
  for (int i = 0; i < n; i++) { sl += l[i] * l[i]; sr += r[i] * r[i]; }
  float a = 1 - expf(-(float)n / (0.010f * (float)pl->rate));
  pl->ms[which] += a * ((float)(sl / n) - pl->ms[which]);
  pl->ms[which + 1] += a * ((float)(sr / n) - pl->ms[which + 1]);
}

static void process(AEffect *e, float **in, float **out, int32_t n) {
  Plug *pl = e->object;
  float *L = out[0], *R = out[1];
  if (n <= 0) return;
  if (in[0] != L) memmove(L, in[0], n * sizeof(float));
  if (in[1] != R) memmove(R, in[1], n * sizeof(float));
  if (!pl->stereo) memcpy(R, L, n * sizeof(float));    /* microfone mono: os efeitos veem o mesmo nos dois lados */
  Chain *p = InterlockedExchangePointer((void *volatile *)&pl->pending, NULL);
  if (p) {
    InterlockedExchangePointer((void *volatile *)&pl->retired, pl->cur);
    pl->cur = p;
    if (shm) shm->h.ack[p->pipe] = p->gen;
    SetEvent(pl->ev);
  }
  int pipe = pipe_of(pl);
  double t0 = now_us();
  levels(pl, 0, L, R, n);
  Chain *c = pl->cur;
  if (c)
    for (int i = 0; i < c->n; i++) {
      for (unsigned long k = 0; k < 4; k++) c->d[i]->connect_port(c->h[i], k, k & 1 ? R : L);
      c->d[i]->run(c->h[i], (unsigned long)n);
    }
  levels(pl, 2, L, R, n);
  if (shm) {
    ShmHead *h = &shm->h;
    memcpy(h->pipe_ms[pipe], pl->ms, sizeof pl->ms);
    h->us[pipe] += 0.05f * ((float)(now_us() - t0) - h->us[pipe]);
    h->rate[pipe] = (float)pl->rate; h->block[pipe] = (float)n; h->chans[pipe] = pl->stereo ? 2 : 1;
    InterlockedIncrement(&h->beat[pipe]);
    LONG g; int b;
    if (!pl->pending && needs_build(pl, &g, &b)) SetEvent(pl->ev);
  }
}

static void set_param(AEffect *e, int32_t i, float v) {
  Plug *pl = e->object;
  if (i == 0) pl->pipe_param = v < 0 ? 0 : v > 1 ? 1 : v;
}
static float get_param(AEffect *e, int32_t i) { return i == 0 ? ((Plug *)e->object)->pipe_param : 0; }

static void copy_str(void *dst, const char *s, size_t cap) {
  strncpy(dst, s, cap - 1);
  ((char *)dst)[cap - 1] = 0;
}

static void start(Plug *pl) {
  /* microfone mono: o Equalizer APO avisa que a segunda entrada não está ligada */
  pl->stereo = !pl->host || pl->host(&pl->e, audioMasterPinConnected, 1, 1, NULL, 0) == 0;
  EnterCriticalSection(&pl->build);
  LONG g; int b;
  if (needs_build(pl, &g, &b)) {   /* sem áudio rodando: monta e troca aqui mesmo */
    chain_free(InterlockedExchangePointer((void *volatile *)&pl->pending, NULL));
    chain_free(pl->cur);
    pl->cur = chain_build(pipe_of(pl), g, b, pl->rate);
    if (shm && pl->cur) shm->h.ack[pl->cur->pipe] = pl->cur->gen;
  }
  pl->running = 1;
  LeaveCriticalSection(&pl->build);
  if (!pl->thread) pl->thread = CreateThread(NULL, 0, builder, pl, 0, NULL);
}

static void destroy(Plug *pl) {
  pl->quit = 1;
  if (pl->thread) {
    SetEvent(pl->ev);
    WaitForSingleObject(pl->thread, INFINITE);
    CloseHandle(pl->thread);
  }
  chain_free(pl->cur);
  chain_free(pl->pending);
  chain_free(pl->retired);
  CloseHandle(pl->ev);
  DeleteCriticalSection(&pl->build);
  free(pl);
  shm_close();
}

static intptr_t dispatch(AEffect *e, int32_t op, int32_t idx, intptr_t val, void *ptr, float opt) {
  Plug *pl = e->object;
  switch (op) {
    case effClose: destroy(pl); return 1;
    case effGetParamName: if (idx == 0) copy_str(ptr, "Pipe", 8); return 0;
    case effGetParamLabel: copy_str(ptr, "", 8); return 0;
    case effGetParamDisplay: if (idx == 0) copy_str(ptr, pipe_of(pl) ? "saida" : "entrada", 8); return 0;
    case effSetSampleRate: if (opt > 0) pl->rate = opt; return 0;
    case effSetBlockSize: pl->block = (int)val; return 0;
    case effMainsChanged:
      if (val) start(pl);
      else pl->running = 0;
      return 0;
    case effGetPlugCategory: return kPlugCategEffect;
    case effGetEffectName: copy_str(ptr, "Mesa", 32); return 1;
    case effGetVendorString: copy_str(ptr, "Mesa", 64); return 1;
    case effGetProductString: copy_str(ptr, "Mesa (efeitos)", 64); return 1;
    case effGetVendorVersion: return 2000;
    case effGetVstVersion: return 2400;
    case effCanDo: return 0;
    case effGetTailSize: return 0;
    default: return 0;
  }
}

EXPORT AEffect *VSTPluginMain(audioMasterCallback host) {
  if (host && !host(NULL, audioMasterVersion, 0, 0, NULL, 0)) return NULL;
  Plug *pl = calloc(1, sizeof *pl);
  if (!pl) return NULL;
  shm_open();
  pl->host = host;
  pl->rate = 48000;
  pl->ev = CreateEventW(NULL, FALSE, FALSE, NULL);
  InitializeCriticalSection(&pl->build);
  AEffect *e = &pl->e;
  e->magic = VST_MAGIC;
  e->dispatcher = dispatch;
  e->setParameter = set_param;
  e->getParameter = get_param;
  e->numPrograms = 0;
  e->numParams = 1;
  e->numInputs = e->numOutputs = 2;
  e->flags = effFlagsCanReplacing;
  e->object = pl;
  e->uniqueID = 0x4d657346; /* 'MesF' */
  e->version = 2000;
  e->processReplacing = process;
  e->process = process;
  return e;
}
