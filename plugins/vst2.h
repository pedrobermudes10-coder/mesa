/* Definições mínimas da interface binária VST 2.4 (só o que o plugin da Mesa usa), escritas a partir do
 * formato público da estrutura AEffect. Não vem do SDK da Steinberg. */
#ifndef MESA_VST2_H
#define MESA_VST2_H
#include <stdint.h>

#define VST_MAGIC 0x56737450 /* 'VstP' */

typedef struct AEffect AEffect;
typedef intptr_t (*audioMasterCallback)(AEffect *, int32_t opcode, int32_t index, intptr_t value, void *ptr, float opt);
typedef intptr_t (*VstDispatcher)(AEffect *, int32_t opcode, int32_t index, intptr_t value, void *ptr, float opt);
typedef void (*VstProcess)(AEffect *, float **in, float **out, int32_t frames);
typedef void (*VstProcessDouble)(AEffect *, double **in, double **out, int32_t frames);
typedef void (*VstSetParameter)(AEffect *, int32_t index, float value);
typedef float (*VstGetParameter)(AEffect *, int32_t index);

struct AEffect {
  int32_t magic;
  VstDispatcher dispatcher;
  VstProcess process; /* obsoleto */
  VstSetParameter setParameter;
  VstGetParameter getParameter;
  int32_t numPrograms, numParams, numInputs, numOutputs, flags;
  intptr_t resvd1, resvd2;
  int32_t initialDelay, realQualities, offQualities;
  float ioRatio;
  void *object, *user;
  int32_t uniqueID, version;
  VstProcess processReplacing;
  VstProcessDouble processDoubleReplacing;
  char future[56];
};

enum { effFlagsHasEditor = 1, effFlagsCanReplacing = 1 << 4, effFlagsProgramChunks = 1 << 5, effFlagsNoSoundInStop = 1 << 9 };

enum {
  effOpen = 0, effClose = 1, effSetProgram = 2, effGetProgram = 3, effGetParamLabel = 6, effGetParamDisplay = 7,
  effGetParamName = 8, effSetSampleRate = 10, effSetBlockSize = 11, effMainsChanged = 12, effGetPlugCategory = 35,
  effGetEffectName = 45, effGetVendorString = 47, effGetProductString = 48, effGetVendorVersion = 49, effCanDo = 51,
  effGetTailSize = 52, effGetVstVersion = 58, effStartProcess = 71, effStopProcess = 72
};
enum { audioMasterVersion = 1, audioMasterPinConnected = 13 };
enum { kPlugCategEffect = 1 };

#endif
