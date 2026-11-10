/* Definições mínimas da interface LADSPA 1.1 (a mesma ABI do ladspa.h oficial), só o que a Mesa usa. */
#ifndef MESA_LADSPA_H
#define MESA_LADSPA_H

typedef float LADSPA_Data;
typedef int LADSPA_Properties;
typedef int LADSPA_PortDescriptor;
typedef int LADSPA_PortRangeHintDescriptor;
typedef void *LADSPA_Handle;

#define LADSPA_PROPERTY_HARD_RT_CAPABLE 0x4

#define LADSPA_PORT_INPUT   0x1
#define LADSPA_PORT_OUTPUT  0x2
#define LADSPA_PORT_CONTROL 0x4
#define LADSPA_PORT_AUDIO   0x8

#define LADSPA_HINT_BOUNDED_BELOW 0x1
#define LADSPA_HINT_BOUNDED_ABOVE 0x2
#define LADSPA_HINT_TOGGLED       0x4
#define LADSPA_HINT_INTEGER       0x20
#define LADSPA_HINT_DEFAULT_MIDDLE 0x100
#define LADSPA_HINT_DEFAULT_0     0x200
#define LADSPA_HINT_DEFAULT_1     0x240

typedef struct {
  LADSPA_PortRangeHintDescriptor HintDescriptor;
  LADSPA_Data LowerBound;
  LADSPA_Data UpperBound;
} LADSPA_PortRangeHint;

typedef struct _LADSPA_Descriptor {
  unsigned long UniqueID;
  const char *Label;
  LADSPA_Properties Properties;
  const char *Name;
  const char *Maker;
  const char *Copyright;
  unsigned long PortCount;
  const LADSPA_PortDescriptor *PortDescriptors;
  const char *const *PortNames;
  const LADSPA_PortRangeHint *PortRangeHints;
  void *ImplementationData;
  LADSPA_Handle (*instantiate)(const struct _LADSPA_Descriptor *Descriptor, unsigned long SampleRate);
  void (*connect_port)(LADSPA_Handle Instance, unsigned long Port, LADSPA_Data *DataLocation);
  void (*activate)(LADSPA_Handle Instance);
  void (*run)(LADSPA_Handle Instance, unsigned long SampleCount);
  void (*run_adding)(LADSPA_Handle Instance, unsigned long SampleCount);
  void (*set_run_adding_gain)(LADSPA_Handle Instance, LADSPA_Data Gain);
  void (*deactivate)(LADSPA_Handle Instance);
  void (*cleanup)(LADSPA_Handle Instance);
} LADSPA_Descriptor;

#endif
