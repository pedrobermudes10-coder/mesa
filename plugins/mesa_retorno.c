/*
 * mesa_retorno.exe — o botão RETORNO da Mesa no Windows: toca o microfone (já com os efeitos do Equalizer
 * APO, que rodam no próprio dispositivo) numa saída, com L+R somados nos dois lados, como no Linux.
 *   mesa_retorno.exe <id do microfone | -> <id da saída | - | 0> [evento] [pid]   ("-" = o padrão do Windows)
 * Saída "0" = só escuta: mantém o microfone aberto e descarta o som. É o que a Mesa usa enquanto a janela
 * está aberta: no Windows os efeitos de captura (o APO) só rodam com alguém gravando; assim a cadeia e os
 * medidores ficam vivos como no Linux. [evento]: nome do evento de parar (padrão Local\MesaRetornoParar);
 * [pid]: termina sozinho quando esse processo (a Mesa) fecha.
 * WASAPI compartilhado: captura por evento em blocos de 10 ms; a saída recebe no formato da captura e o
 * Windows converte a taxa (AUTOCONVERTPCM). Se a saída acumular mais de ~30 ms, o excesso é descartado
 * (latência fixa). Para parar: a Mesa sinaliza o evento Local\MesaRetornoParar (o mesmo evento diz que já
 * tem um retorno rodando). Continua rodando com a janela da Mesa fechada, como o pw-loopback no Linux.
 */
#define COBJMACROS
#include <initguid.h>
#include <windows.h>
#include <mmdeviceapi.h>
#include <audioclient.h>
#include <avrt.h>
#include <stdlib.h>
#include <string.h>

DEFINE_GUID(SUB_FLOAT, 0x00000003, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);
DEFINE_GUID(SUB_PCM, 0x00000001, 0x0000, 0x0010, 0x80, 0x00, 0x00, 0xaa, 0x00, 0x38, 0x9b, 0x71);
#define AUTOCONVERT (0x80000000 | 0x08000000) /* AUTOCONVERTPCM | SRC_DEFAULT_QUALITY */

static IMMDevice *device(IMMDeviceEnumerator *en, const char *id, EDataFlow flow) {
  IMMDevice *d = NULL;
  if (!strcmp(id, "-")) {
    IMMDeviceEnumerator_GetDefaultAudioEndpoint(en, flow, eConsole, &d);
  } else {
    wchar_t w[512];
    MultiByteToWideChar(CP_UTF8, 0, id, -1, w, 512);
    IMMDeviceEnumerator_GetDevice(en, w, &d);
  }
  return d;
}

/* uma amostra do formato da captura em float */
static float sample(const BYTE *p, int bits, int isfloat) {
  if (isfloat) return *(const float *)p;
  switch (bits) {
    case 16: return *(const short *)p / 32768.f;
    case 24: return (float)((int)((unsigned)p[0] << 8 | (unsigned)p[1] << 16 | (unsigned)p[2] << 24) >> 8) / 8388608.f;
    case 32: return *(const int *)p / 2147483648.f;
  }
  return 0;
}

int WINAPI WinMain(HINSTANCE hi, HINSTANCE hp, LPSTR cmd, int show) {
  if (__argc < 3) return 2;
  wchar_t evname[128] = L"Local\\MesaRetornoParar";
  if (__argc > 3) MultiByteToWideChar(CP_UTF8, 0, __argv[3], -1, evname, 128);
  HANDLE parent = __argc > 4 ? OpenProcess(SYNCHRONIZE, FALSE, (DWORD)atoi(__argv[4])) : NULL;
  int render = strcmp(__argv[2], "0") != 0;
  HANDLE stop = CreateEventW(NULL, TRUE, FALSE, evname);
  if (!stop || GetLastError() == ERROR_ALREADY_EXISTS) return 3;   /* já tem um rodando */
  CoInitializeEx(NULL, COINIT_MULTITHREADED);
  IMMDeviceEnumerator *en = NULL;
  if (FAILED(CoCreateInstance(&CLSID_MMDeviceEnumerator, NULL, CLSCTX_ALL, &IID_IMMDeviceEnumerator, (void **)&en))) return 4;
  IMMDevice *dc = device(en, __argv[1], eCapture), *dr = render ? device(en, __argv[2], eRender) : NULL;
  if (!dc || (render && !dr)) return 5;

  IAudioClient *ac = NULL, *ar = NULL;
  IMMDevice_Activate(dc, &IID_IAudioClient, CLSCTX_ALL, NULL, (void **)&ac);
  if (render) IMMDevice_Activate(dr, &IID_IAudioClient, CLSCTX_ALL, NULL, (void **)&ar);
  if (!ac || (render && !ar)) return 6;
  WAVEFORMATEX *cf = NULL;
  IAudioClient_GetMixFormat(ac, &cf);
  int ch = cf->nChannels, bits = cf->wBitsPerSample, bpf = cf->nBlockAlign;
  int isfloat = cf->wFormatTag == WAVE_FORMAT_IEEE_FLOAT ||
                (cf->wFormatTag == WAVE_FORMAT_EXTENSIBLE && IsEqualGUID(&((WAVEFORMATEXTENSIBLE *)cf)->SubFormat, &SUB_FLOAT));
  HANDLE ev = CreateEventW(NULL, FALSE, FALSE, NULL);
  if (FAILED(IAudioClient_Initialize(ac, AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_EVENTCALLBACK, 100000, 0, cf, NULL)))
    return 7;
  IAudioClient_SetEventHandle(ac, ev);

  /* saída: float estéreo na taxa da captura; o Windows converte para a da saída */
  WAVEFORMATEXTENSIBLE rf = {0};
  rf.Format.wFormatTag = WAVE_FORMAT_EXTENSIBLE;
  rf.Format.nChannels = 2;
  rf.Format.nSamplesPerSec = cf->nSamplesPerSec;
  rf.Format.wBitsPerSample = 32;
  rf.Format.nBlockAlign = 8;
  rf.Format.nAvgBytesPerSec = cf->nSamplesPerSec * 8;
  rf.Format.cbSize = 22;
  rf.Samples.wValidBitsPerSample = 32;
  rf.dwChannelMask = SPEAKER_FRONT_LEFT | SPEAKER_FRONT_RIGHT;
  rf.SubFormat = SUB_FLOAT;
  if (render && FAILED(IAudioClient_Initialize(ar, AUDCLNT_SHAREMODE_SHARED, AUTOCONVERT, 400000, 0, &rf.Format, NULL)))
    return 8;
  UINT32 rbuf = 0;
  if (render) IAudioClient_GetBufferSize(ar, &rbuf);
  IAudioCaptureClient *cap = NULL;
  IAudioRenderClient *ren = NULL;
  IAudioClient_GetService(ac, &IID_IAudioCaptureClient, (void **)&cap);
  if (render) IAudioClient_GetService(ar, &IID_IAudioRenderClient, (void **)&ren);
  if (!cap || (render && !ren)) return 9;
  UINT32 rate = cf->nSamplesPerSec, keep = rate * 30 / 1000;
  if (keep > rbuf) keep = rbuf;

  BYTE *z;
  if (render && SUCCEEDED(IAudioRenderClient_GetBuffer(ren, rate / 100, &z))) {   /* 10 ms de silêncio para começar */
    IAudioRenderClient_ReleaseBuffer(ren, rate / 100, AUDCLNT_BUFFERFLAGS_SILENT);
  }
  DWORD task = 0;
  HANDLE av = AvSetMmThreadCharacteristicsW(L"Pro Audio", &task);
  IAudioClient_Start(ac);
  if (render) IAudioClient_Start(ar);
  HANDLE waits[3] = {stop, ev, parent};
  int code = 0;
  for (;;) {
    DWORD w = WaitForMultipleObjects(parent ? 3 : 2, waits, FALSE, 500);
    if (w == WAIT_OBJECT_0 || w == WAIT_OBJECT_0 + 2) break;   /* mandaram parar / a Mesa fechou */
    BYTE *data;
    UINT32 n;
    DWORD flags;
    HRESULT hr;
    while ((hr = IAudioCaptureClient_GetBuffer(cap, &data, &n, &flags, NULL, NULL)) == S_OK && n) {
      if (!render) { IAudioCaptureClient_ReleaseBuffer(cap, n); continue; }   /* só escuta: descarta */
      UINT32 pad = 0;
      if (FAILED(IAudioClient_GetCurrentPadding(ar, &pad))) { code = 10; goto fim; }
      UINT32 room = pad < keep ? keep - pad : 0;     /* passou de ~30 ms: descarta (latência fixa) */
      UINT32 m = n < room ? n : room;
      BYTE *out;
      if (m && SUCCEEDED(IAudioRenderClient_GetBuffer(ren, m, &out))) {
        float *o = (float *)out;
        for (UINT32 i = 0; i < m; i++) {   /* L+R nos dois lados (mono: o mesmo canal) */
          float v = 0;
          if (!(flags & AUDCLNT_BUFFERFLAGS_SILENT))
            for (int c = 0; c < ch && c < 2; c++) v += sample(data + i * bpf + c * (bpf / ch), bits, isfloat);
          o[2 * i] = o[2 * i + 1] = v;
        }
        IAudioRenderClient_ReleaseBuffer(ren, m, 0);
      }
      IAudioCaptureClient_ReleaseBuffer(cap, n);
    }
    if (FAILED(hr)) { code = 11; break; }   /* dispositivo sumiu */
  }
fim:
  IAudioClient_Stop(ac);
  if (render) IAudioClient_Stop(ar);
  if (av) AvRevertMmThreadCharacteristics(av);
  return code;
}
