#!/usr/bin/env python3
# Mesa — mesa de mixagem e cadeia de efeitos própria sobre o PipeWire (sem Easy Effects).
# Servidor HTTP local (só 127.0.0.1) + janela com a interface em ui.html: Chromium em modo app
# (medidores no fps do monitor) ou, sem Chromium / com --gtk, uma janela GTK/WebKit (60 fps).
#   Efeitos: plugins LADSPA da própria Mesa (lib/mesa_fx.so: filtro, EQ, RNNoise, compressor, Crystalizer)
#   rodando num filter-chain do PipeWire, num serviço do usuário (mesa-cadeia.service) que continua
#   funcionando com a janela fechada. Ajustes vão ao vivo (pw-cli set-param); mudar a ordem, adicionar,
#   remover ou trocar o dispositivo reinicia o serviço (~0,5 s) e a Mesa devolve as gravações à fonte dela.
#   Volumes: pactl (JSON).  Medidores: um nó de gravação (pw-record) com 2 canais por ponto, ligado com
#   pw-link; os do meio da cadeia vêm dos próprios plugins (/dev/shm/mesa-niveis-<uid>).
import array, hashlib, json, math, mmap, os, re, shutil, signal, socket, struct, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

VERSION = '2.2'
HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get('MESA_PORT', 47123))
C_ENV = {**os.environ, 'LC_ALL': 'C'}   # com acentos o pactl gera JSON invalido
HOME = os.path.expanduser('~')
CONF = os.path.join(HOME, '.config/mesa')
STORE_FILE = os.path.join(CONF, 'cadeia.json')
PW_CONF = os.path.join(CONF, 'pipewire-cadeia.conf')
DATA = os.path.join(HOME, '.local/share/mesa')
LIB = os.path.join(DATA, 'lib/mesa_fx.so')               # cópia instalada (a AppImage some quando fecha)
LIB_SRC = next((p for p in (os.path.join(HERE, 'lib/mesa_fx.so'), os.path.join(HERE, '../plugins/mesa_fx.so'))
                if os.path.exists(p)), None)
UNIT_NAME = 'mesa-cadeia.service'
UNIT = os.path.join(HOME, '.config/systemd/user', UNIT_NAME)
LEVELS_FILE = f'/dev/shm/mesa-niveis-{os.getuid()}' if hasattr(os, 'getuid') else None   # Windows: mesa_win.py
RETORNO_SINK = os.path.join(CONF, 'retorno-saida')        # saída escolhida para o retorno do mic
EE_DB = os.path.join(HOME, '.var/app/com.github.wwmm.easyeffects/config/easyeffects/db')

# nós de cada cadeia: (entrada do filtro, saída do filtro); a entrada recebe os ajustes ao vivo
NODES = {'input': ('mesa_entrada', 'mesa_source'), 'output': ('mesa_sink', 'mesa_saida')}
SOURCE, SINK = 'mesa_source', 'mesa_sink'

# ---------------------------------------------------------------- efeitos

COMMON = {'bypass': False, 'inputGain': 0.0, 'outputGain': 0.0}
EQ_FREQS = [22.4, 27.8, 34.51, 42.82, 53.14, 65.95, 81.83, 101.55, 126, 156.38, 194.06, 240.81, 298.834, 370.834,
            460.182, 571.057, 708.647, 879.387, 1091.26, 1354.19, 1680.47, 2085.35, 2587.79, 3211.29, 3985.01,
            4945.15, 6136.63, 7615.17, 9449.96, 11726.8, 14552.2, 18058.4]
# equalizador: o plugin processa as 32 bandas de 1/3 de oitava (Q 3, largura corrigida perto de fs/2). As 4 bandas "macro"
# da interface — shelf grave 150 Hz, bells 800 Hz e 4 kHz, shelf agudo 8 kHz (tipos do LSP: 1 bell, 3 shelf
# agudo, 5 shelf grave) — não existem no plugin: mexer nelas move as 32 pela matemática (eq_macro).
EQ_MACRO = ((150, 5), (800, 1), (4000, 1), (8000, 3))
EQ_FINE = len(EQ_FREQS)
EQ_SUF = ('Type', 'Frequency', 'Gain', 'Q', 'Slope')
# Q das 32 finas: 3 (um pouco mais largo que 1/3 de oitava). Com a largura corrigida no plugin, a soma das 32
# fica a ±0,2 dB do alvo de 100 Hz a 18 kHz; com Q 4,36 sem correção caía até 5 dB acima de 7 kHz.
EQ_FINE_Q = 3.0
EQ_DESIGN = 2     # 1 = Q 4,36 sem correção (2.1 inicial); 2 = Q 3 com correção

def eq_defaults():
    d = {'numBands': EQ_FINE}
    for b in range(EQ_FINE):
        d.update({f'band{b}Type': 1, f'band{b}Frequency': EQ_FREQS[b], f'band{b}Gain': 0.0, f'band{b}Q': EQ_FINE_Q,
                  f'band{b}Slope': 0})
    return d

def default_macro():
    return [{'type': t, 'freq': float(f), 'gain': 0.0, 'q': 0.7} for f, t in EQ_MACRO]

# rótulo LADSPA e valores padrão (os mesmos do Easy Effects, para a migração sair igual)
FX = {
    'filter': ('mesa_filter', {'type': 0, 'frequency': 2000.0, 'gain': 0.0, 'quality': 0.0, 'slope': 0}),
    'rnnoise': ('mesa_rnnoise', {'wet': 0.0, 'vadThres': 50.0, 'release': 20.0, 'enableVad': False}),
    'equalizer': ('mesa_eq', eq_defaults()),
    'compressor': ('mesa_compressor', {'mode': 0, 'threshold': -12.0, 'ratio': 4.0, 'knee': -6.0, 'makeup': 0.0,
                                       'attack': 20.0, 'release': 100.0, 'boostThreshold': -72.0,
                                       'boostAmount': 6.0, 'hpfMode': 0, 'hpfFrequency': 10.0, 'lpfMode': 0,
                                       'lpfFrequency': 20000.0, 'sidechainMode': 0, 'sidechainReactivity': 10.0}),
    'crystalizer': ('mesa_crystalizer', {**{f'intensityBand{b}': 0.0 for b in range(13)},
                                         **{f'muteBand{b}': False for b in range(13)},
                                         **{f'bypassBand{b}': False for b in range(13)},
                                         'adaptiveIntensity': True, 'oversampling': True, 'transitionBand': 120.0}),
}
CATALOG = [('filter', 'Filtro'), ('rnnoise', 'RNNoise'), ('equalizer', 'Equalizador'),
           ('crystalizer', 'Crystalizer'), ('compressor', 'Compressor')]
TITLES = dict(CATALOG)
PLUGIN_RE = re.compile(r'([a-z_]+)#(\d+)')

# Parâmetros mostrados em cada efeito: (prop, rótulo, min, max, unidade, escala)
KNOBS = {
    'filter':      [('frequency', 'FREQ', 10, 20000, 'Hz', 'log')],
    'rnnoise':     [('wet', 'WET', -40, 20, 'dB', 'lin'), ('vadThres', 'VAD', 0, 100, '%', 'lin'),
                    ('release', 'REL', 0, 1000, 'ms', 'lin')],
    'compressor':  [('threshold', 'THRESH', -60, 0, 'dB', 'lin'), ('ratio', 'RATIO', 1, 30, ':1', 'log'),
                    ('knee', 'KNEE', -24, 0, 'dB', 'lin'), ('makeup', 'MAKEUP', -12, 12, 'dB', 'lin'),
                    ('attack', 'ATK', 0, 500, 'ms', 'lin'), ('release', 'REL', 0, 2000, 'ms', 'lin')],
}
TOGGLES = {'rnnoise': [('enableVad', 'VAD')]}
SELECTS = {'filter': [('type', 'TIPO', ['Passa-baixa', 'Passa-alta', 'Shelf grave', 'Shelf agudo', 'Bell',
                                        'Passa-banda', 'Notch', 'Ressonância', 'Ladder passa', 'Ladder rejeita',
                                        'All-pass']),
                      # cada passo põe mais um filtro de 2ª ordem em cascata (12 dB por oitava)
                      ('slope', 'dB/OITAVA', ['12', '24', '36', '48', '72', '96', '144', '192'])]}
FILTER_GAIN_TYPES = (2, 3, 4, 7)       # shelf, bell e ressonância têm ganho
FILTER_Q_TYPES = (4, 5, 6, 7, 8, 9)    # os que têm largura (Q)

def defaults(kind):
    return {**COMMON, **FX[kind][1]}

# cadeia padrão da entrada (primeira vez sem Easy Effects): passa-alta, de-noiser, equalizador e compressor
DEFAULT_INPUT = ('filter#0', 'rnnoise#0', 'equalizer#0', 'compressor#0')

def new_params(kind):
    """Valores de um efeito recém-adicionado: o filtro começa como passa-alta de 80 Hz (24 dB/oitava),
    o uso normal num microfone (o equalizador já nasce com o macro de 4 bandas, ver eq_defaults)."""
    d = defaults(kind)
    if kind == 'filter':
        d.update(type=1, frequency=80.0, slope=1)
    return d

# ---------------------------------------------------------------- estado salvo (cadeia.json)

STORE_LOCK = threading.RLock()
STORE = None      # cadeia.json carregado (start_engine)

def migrate_ee():
    """Primeira vez: copia cadeias, dispositivos e ajustes do Easy Effects (Flatpak), se existir."""
    rc = os.path.join(EE_DB, 'easyeffectsrc')
    if not os.path.exists(rc):
        return None
    sect_of = {'[StreamInputs]': 'input', '[StreamOutputs]': 'output'}
    cfg = {p: {'plugins': [], 'device': None} for p in NODES}
    sect = None
    for ln in open(rc, encoding='utf-8'):
        ln = ln.strip()
        if ln.startswith('['):
            sect = sect_of.get(ln)
        elif sect and '=' in ln:
            k, v = ln.split('=', 1)
            if k == 'plugins':
                cfg[sect]['plugins'] = [p for p in v.split(',') if PLUGIN_RE.fullmatch(p) and p.split('#')[0] in FX]
            elif k in ('inputDevice', 'outputDevice'):
                cfg[sect]['device'] = v
    # ajustes: [sie][Compressor#0] / [sie][Equalizer#0#left] em <plugin>rc
    vals = {}
    for f in os.listdir(EE_DB):
        if not f.endswith('rc') or f == 'easyeffectsrc':
            continue
        cur = None
        for ln in open(os.path.join(EE_DB, f), encoding='utf-8'):
            ln = ln.strip()
            m = re.fullmatch(r'\[(sie|soe)\]\[([A-Za-z]+)#(\d+)(#left|#right)?\]', ln)
            if m:
                pipe = 'input' if m[1] == 'sie' else 'output'
                kind = {'Filter': 'filter', 'RNNoise': 'rnnoise', 'Equalizer': 'equalizer',
                        'Crystalizer': 'crystalizer', 'Compressor': 'compressor'}.get(m[2], '-')
                cur = None if m[4] == '#right' else (pipe, f'{kind}#{m[3]}')
            elif cur and '=' in ln:
                k, v = ln.split('=', 1)
                vals.setdefault(cur, {})[k] = v
    out = {}
    for pipe, c in cfg.items():
        params = {}
        for pid in c['plugins']:
            d = defaults(pid.split('#')[0])
            for k, v in vals.get((pipe, pid), {}).items():
                if k in d:
                    d[k] = v == 'true' if isinstance(d[k], bool) else type(d[k])(float(v))
            params[pid] = d
        out[pipe] = {'device': c['device'], 'plugins': c['plugins'], 'params': params}
    return out

def default_store():
    st = migrate_ee()
    if st:
        return st
    return {'input': {'device': None, 'plugins': list(DEFAULT_INPUT),
                      'params': {pid: new_params(pid.split('#')[0]) for pid in DEFAULT_INPUT}},
            'output': {'device': None, 'plugins': [], 'params': {}}}

def store_load():
    try:
        with open(STORE_FILE, encoding='utf-8') as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = default_store()
    st.pop('eq_layouts', None)   # resquício da 2.1 (layouts 4/32 separados)
    for pipe in NODES:
        p = st.setdefault(pipe, {})
        p.setdefault('device', None)
        p['plugins'] = [x for x in p.get('plugins', []) if PLUGIN_RE.fullmatch(x) and x.split('#')[0] in FX]
        raw, macros, pending = p.get('params', {}), p.get('macro') or {}, {}
        for pid, prm in list(raw.items()):
            if pid.startswith('equalizer#') and 'band35Frequency' in prm:
                # layout intermediário da 2.1 (4 macro + 32 finas no plugin): macro sai do plugin, finas voltam a 0..31
                macros[pid] = [{'type': int(prm[f'band{b}Type']), 'freq': float(prm[f'band{b}Frequency']),
                                'gain': float(prm[f'band{b}Gain']), 'q': float(prm[f'band{b}Q'])} for b in range(4)]
                fine = {k: v for k, v in prm.items() if not k.startswith('band')}
                for b in range(EQ_FINE):
                    for k in EQ_SUF:
                        fine[f'band{b}{k}'] = prm[f'band{b + 4}{k}']
                raw[pid] = fine
                pending[pid] = True     # o macro ainda não estava aplicado nas finas
        p['params'] = {pid: {**defaults(pid.split('#')[0]), **raw.get(pid, {})} for pid in p['plugins']}
        p['macro'] = {pid: m for pid, m in macros.items() if pid in p['params']}
        for pid, prm in p['params'].items():
            if not pid.startswith('equalizer#'):
                continue
            prm['numBands'] = EQ_FINE   # bandas com ganho zero não custam nada; o macro precisa de todas
            if all(prm[f'band{b}Gain'] == 0 for b in range(EQ_FINE)):
                grid = eq_defaults()    # sem ganho nenhum: garante a grade de 1/3 de oitava (arquivos antigos)
                prm.update({k: v for k, v in grid.items() if k.startswith('band')})
            if pending.get(pid):
                for b, g in enumerate(fine_gains(prm, macro_curve(p['macro'][pid], eq_freqs(prm)))):
                    prm[f'band{b}Gain'] = clamp_gain(prm[f'band{b}Gain'] + g)
            if p.get('eq_design', 1) < EQ_DESIGN:
                # bells antigos (Q 4,36, sem correção): mede a resposta deles nas 32 centrais e acha os ganhos do
                # desenho novo que dão a mesma resposta ali (o som nas centrais não muda; entre elas melhora)
                freqs = eq_freqs(prm)
                old = [bq_coef('bell', freqs[k], prm[f'band{k}Gain'], prm[f'band{k}Q'] or 4.36)
                       if prm[f'band{k}Gain'] else None for k in range(EQ_FINE)]
                resp = [sum(bq_mag_db(c, f) for c in old if c) for f in freqs]
                for b in range(EQ_FINE):
                    prm[f'band{b}Q'] = EQ_FINE_Q
                # com macro: as 32 saem do macro (descarta desvios antigos das pontas, que puxavam o shelf agudo
                # acima de 15 kHz); sem macro (ex.: vindo do Easy Effects): mantém a resposta que havia
                target = macro_curve(p['macro'][pid], freqs) if p['macro'].get(pid) else resp
                if any(target):
                    for b, g in enumerate(fine_gains(prm, target)):
                        prm[f'band{b}Gain'] = clamp_gain(g)
        p['eq_design'] = EQ_DESIGN
    return st

def store_save():
    os.makedirs(CONF, exist_ok=True)
    with STORE_LOCK:
        txt = json.dumps(STORE, ensure_ascii=False, indent=1)
    tmp = STORE_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(txt)
    os.replace(tmp, STORE_FILE)

# ---------------------------------------------------------------- configuração do PipeWire

def num(v):
    v = float(v)
    return str(int(v)) if v == int(v) and abs(v) < 1e9 else repr(round(v, 6))

def q(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"') + '"'

def slot_of(pipe, i):
    return i + (32 if pipe == 'output' else 0)

def graph_text(pipe, st):
    plugins = st['plugins']
    if not plugins:   # sem efeitos: só passa o som
        return ('{ nodes = [ { type = builtin name = cL label = copy } { type = builtin name = cR label = copy } ]\n'
                '        inputs = [ "cL:In" "cR:In" ] outputs = [ "cL:Out" "cR:Out" ] }')
    nodes, links = [], []
    for i, pid in enumerate(plugins):
        kind = pid.split('#')[0]
        ctl = {**st['params'].get(pid, {}), 'slot': slot_of(pipe, i)}
        c = ' '.join(f'{q(k)} {num(v)}' for k, v in ctl.items())
        nodes.append(f'          {{ type = ladspa name = fx{i} plugin = {q(LIB)} label = {FX[kind][0]}\n'
                     f'            control = {{ {c} }} }}')
        if i:
            links += [f'{{ output = "fx{i - 1}:outL" input = "fx{i}:inL" }}',
                      f'{{ output = "fx{i - 1}:outR" input = "fx{i}:inR" }}']
    last = len(plugins) - 1
    return ('{\n        nodes = [\n' + '\n'.join(nodes) + '\n        ]\n'
            f'        links = [ {" ".join(links)} ]\n'
            '        inputs = [ "fx0:inL" "fx0:inR" ]\n'
            f'        outputs = [ "fx{last}:outL" "fx{last}:outR" ]\n      }}')

def resolve_device(pipe, dev):
    """Dispositivo da ponta física; sem escolha, o padrão do sistema (nunca a própria Mesa)."""
    if dev:
        return dev
    kind = 'source' if pipe == 'input' else 'sink'
    d = default(kind)
    if d and not d.startswith('mesa_'):
        return d
    items = pactl_json('sources' if pipe == 'input' else 'sinks')
    pre = 'alsa_input' if pipe == 'input' else 'alsa_output'
    return next((s['name'] for s in items if s['name'].startswith(pre)), None)

def conf_text(st):
    mods = []
    for pipe in NODES:
        p = st[pipe]
        if pipe == 'output' and not p['plugins']:
            continue      # sem efeitos de saída não cria a saída virtual
        dev = resolve_device(pipe, p['device'])
        tgt = f' target.object = {q(dev)}' if dev else ''
        a, b = NODES[pipe]
        if pipe == 'input':
            # node.dont-move: ninguém (nem outro programa de efeitos) muda a entrada dos efeitos de lugar.
            # dont-fallback + linger: se o dispositivo ainda não existe (boot, USB desligado), o nó fica
            # esperando por ele; com dont-reconnect o WirePlumber destruía o nó e a cadeia sumia até um
            # reinício manual (era o "reset" depois de desligar o computador).
            cap = (f'node.name = {a} node.description = "Mesa (entrada dos efeitos)" node.passive = true '
                   f'node.dont-move = true node.dont-fallback = true node.linger = true{tgt}')
            play = f'node.name = {b} node.description = "Mesa (microfone)" media.class = Audio/Source'
            desc = 'Mesa — efeitos de entrada'
        else:
            cap = f'node.name = {a} node.description = "Mesa (efeitos de saída)" media.class = Audio/Sink'
            play = (f'node.name = {b} node.description = "Mesa (saída dos efeitos)" node.passive = true '
                    f'node.dont-move = true node.dont-fallback = true node.linger = true{tgt}')
            desc = 'Mesa — efeitos de saída'
        mods.append(f'''  {{ name = libpipewire-module-filter-chain
    args = {{
      node.description = {q(desc)}
      media.name = {q(desc)}
      filter.graph = {graph_text(pipe, p)}
      audio.channels = 2
      audio.position = [ FL FR ]
      capture.props = {{ {cap} }}
      playback.props = {{ {play} }}
    }}
  }}''')
    return f'''# Gerado pela Mesa {VERSION} — não edite: é refeito a cada mudança (o estado fica em cadeia.json).
context.properties = {{ log.level = 2 }}
context.spa-libs = {{
  audio.convert.* = audioconvert/libspa-audioconvert
  support.* = support/libspa-support
}}
context.modules = [
  {{ name = libpipewire-module-rt args = {{ }} flags = [ ifexists nofail ] }}
  {{ name = libpipewire-module-protocol-native }}
  {{ name = libpipewire-module-client-node }}
  {{ name = libpipewire-module-adapter }}
{chr(10).join(mods)}
]
'''

def write_conf():
    """Grava a configuração; devolve True se mudou."""
    with STORE_LOCK:
        txt = conf_text(STORE)
    try:
        if open(PW_CONF, encoding='utf-8').read() == txt:
            return False
    except OSError:
        pass
    os.makedirs(CONF, exist_ok=True)
    with open(PW_CONF + '.tmp', 'w', encoding='utf-8') as f:
        f.write(txt)
    os.replace(PW_CONF + '.tmp', PW_CONF)
    return True

def perf_cores():
    """Processadores híbridos (Intel 12ª geração em diante): só os núcleos de desempenho. Nos de eficiência
    o processamento roda em clock baixo e leva de 3 a 6 vezes mais tempo (picos e falhas no som)."""
    try:
        cpus = open('/sys/devices/cpu_core/cpus').read().strip()
        return f'CPUAffinity={cpus.replace(",", " ")}\n' if re.fullmatch(r'[0-9,-]+', cpus) else ''
    except OSError:
        return ''

UNIT_TEXT = f'''[Unit]
Description=Mesa — cadeia de efeitos (filter-chain do PipeWire)
After=pipewire.service wireplumber.service
BindsTo=pipewire.service

[Service]
ExecStart={shutil.which('pipewire') or '/usr/bin/pipewire'} -c {PW_CONF}
Restart=on-failure
RestartSec=1
{perf_cores()}
[Install]
WantedBy=pipewire.service
'''

def systemctl(*a):
    return subprocess.run(['systemctl', '--user', *a], capture_output=True, text=True, timeout=15)

def engine_active():
    return systemctl('is-active', '--quiet', UNIT_NAME).returncode == 0

def file_hash(p):
    try:
        with open(p, 'rb') as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None

def install_engine():
    """Copia os plugins, cria o serviço e liga a cadeia. Devolve True se o serviço precisa reiniciar."""
    changed = False
    if LIB_SRC and file_hash(LIB_SRC) != file_hash(LIB):
        os.makedirs(os.path.dirname(LIB), exist_ok=True)
        shutil.copyfile(LIB_SRC, LIB + '.tmp')
        os.replace(LIB + '.tmp', LIB)
        changed = True
    try:
        same = open(UNIT, encoding='utf-8').read() == UNIT_TEXT
    except OSError:
        same = False
    if not same:
        os.makedirs(os.path.dirname(UNIT), exist_ok=True)
        with open(UNIT, 'w', encoding='utf-8') as f:
            f.write(UNIT_TEXT)
        systemctl('daemon-reload')
        systemctl('enable', UNIT_NAME)
        changed = True
    if not os.path.exists(STORE_FILE):
        store_save()
    changed |= write_conf()
    # serviço ativo mas sem a fonte virtual (a cadeia caiu dentro do PipeWire): reinicia também
    return changed or not engine_active() or not source_present()

def source_present():
    return any(s.get('name') == SOURCE for s in pactl_json('sources'))

# ---------------------------------------------------------------- aplicar (reinicia a cadeia)

APPLY = {'busy': False, 'msg': '', 'error': ''}
STATS = {'fps': 0, 'seen': 0.0}   # seen: último contato da janela
APPLY_LOCK = threading.Lock()

def streams_on_ours():
    """Gravações ligadas na fonte da Mesa e apps tocando na saída dela: voltam para lá depois do reinício."""
    out = []
    for what, ours, field in (('source-outputs', SOURCE, 'source'), ('sink-inputs', SINK, 'sink')):
        idx = {s['index'] for s in pactl_json('sources' if field == 'source' else 'sinks') if s['name'] == ours}
        for s in pactl_json(what):
            if s.get(field) in idx and not s.get('properties', {}).get('node.name', '').startswith('mesa_'):
                out.append((what, s['index'], ours))
    return out

def wait_nodes(timeout=6):
    need = {SOURCE} | ({SINK} if STORE['output']['plugins'] else set())
    end = time.time() + timeout
    while time.time() < end:
        names = {s['name'] for s in pactl_json('sources')} | {s['name'] for s in pactl_json('sinks')}
        if need <= names:
            return True
        time.sleep(0.1)
    return False

def restart_engine():
    keep = streams_on_ours()
    had_ret = bool(retorno_pids())
    if had_ret:
        retorno_stop()
    r = systemctl('restart', UNIT_NAME)
    ok = r.returncode == 0 and wait_nodes()
    for what, i, target in keep:
        cmd = 'move-source-output' if what == 'source-outputs' else 'move-sink-input'
        subprocess.run(['pactl', cmd, str(i), target], capture_output=True, timeout=3)
    if had_ret:
        retorno_start()
    NODE_IDS.clear()
    if not ok:
        raise RuntimeError('A cadeia de efeitos não voltou: ' + (r.stderr.strip() or 'veja journalctl --user -u ' + UNIT_NAME))

def apply_change(desc, edit):
    if not APPLY_LOCK.acquire(blocking=False):
        return False
    def job():
        try:
            with STORE_LOCK:
                edit()
            store_save()
            write_conf()
            restart_engine()
        except Exception as e:  # noqa: BLE001 — qualquer falha vira aviso na tela
            APPLY['error'] = str(e)
        finally:
            APPLY.update(busy=False)
            APPLY_LOCK.release()
    APPLY.update(busy=True, msg=desc, error='')   # já ocupado antes da resposta, para a janela não perder o início
    threading.Thread(target=job, daemon=True).start()
    return True

# ---------------------------------------------------------------- ajustes ao vivo

LIVE = {}                 # pipe -> {"fxN:prop": valor}
LIVE_LOCK = threading.Lock()
LIVE_EV = threading.Event()
SAVE_AT = [0.0]
NODE_IDS = {}

def live_loop():
    """Junta os ajustes que chegam (um botão sendo girado manda dezenas por segundo) e manda de uma vez."""
    while True:
        LIVE_EV.wait(0.5)
        if LIVE_EV.is_set():
            time.sleep(0.015)
            LIVE_EV.clear()
            with LIVE_LOCK:
                batch = dict(LIVE)
                LIVE.clear()
            for pipe, ctls in batch.items():
                params = ' '.join(f'{q(k)} {num(v)}' for k, v in ctls.items())
                subprocess.run(['pw-cli', 'set-param', NODES[pipe][0], 'Props', f'{{ params = [ {params} ] }}'],
                               capture_output=True, timeout=3)
            SAVE_AT[0] = time.time() + 1
        if SAVE_AT[0] and time.time() > SAVE_AT[0]:   # grava 1 s depois do último ajuste
            SAVE_AT[0] = 0
            try:
                store_save()
                write_conf()
            except OSError:
                pass

def set_param(pipe, pid, prop, value):
    with STORE_LOCK:
        p = STORE[pipe]
        if pid not in p['plugins']:
            return False
        params = p['params'][pid]
        if prop not in params:
            return False
        cur = params[prop]
        if isinstance(cur, bool):
            value = bool(value)
        elif isinstance(cur, int):
            value = int(round(float(value)))
        else:
            value = float(value)
        params[prop] = value
        i = p['plugins'].index(pid)
    with LIVE_LOCK:
        LIVE.setdefault(pipe, {})[f'fx{i}:{prop}'] = value
    LIVE_EV.set()
    return True

# ---------------------------------------------------------------- retorno do mic

def retorno_pids():
    pids = []
    for d in os.listdir('/proc'):
        if d.isdigit():
            try:
                with open(f'/proc/{d}/cmdline', 'rb') as f:
                    c = f.read()
            except OSError:
                continue
            if c.startswith(b'pw-loopback') and b'mesa_retorno' in c:
                pids.append(int(d))
    return pids

def retorno_sink():
    for p in (RETORNO_SINK, os.path.join(HOME, '.config/retorno-mic/saida')):
        try:
            s = open(p).read().strip()
            if s:
                return s
        except OSError:
            pass
    return None

def retorno_start():
    sink = retorno_sink() or default('sink')
    subprocess.Popen(['pw-loopback', '--latency=20',
                      f'--capture-props=node.name=mesa_retorno_in node.description="Retorno do mic" '
                      f'target.object={SOURCE} node.dont-reconnect=true node.dont-move=true',
                      f'--playback-props=node.name=mesa_retorno node.description="Retorno do mic" '
                      f'media.name="Retorno do mic" target.object={sink} node.dont-reconnect=true node.dont-move=true'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    # os dois canais da fonte (L e R) saem somados nos dois lados
    for _ in range(30):
        links = subprocess.run(['pw-link', '-l'], capture_output=True, text=True).stdout
        if f'{SOURCE}:capture_FR\n  |-> mesa_retorno_in:input_FR' in links:
            break
        time.sleep(0.1)
    subprocess.run(['pw-link', f'{SOURCE}:capture_FL', 'mesa_retorno_in:input_FR'], capture_output=True)
    subprocess.run(['pw-link', f'{SOURCE}:capture_FR', 'mesa_retorno_in:input_FL'], capture_output=True)

def retorno_stop():
    for p in retorno_pids():
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass
    time.sleep(0.2)

# ---------------------------------------------------------------- equalizador: macro (4 bandas) -> finas (32)

EQ_RATE = 48000.0
EQ_KIND = {1: 'bell', 2: 'hp', 3: 'hs', 4: 'lp', 5: 'ls', 6: 'notch', 7: 'bell', 9: 'bp'}   # tipos do LSP

def bq_coef(kind, f0, g, q):
    """Biquad RBJ (igual ao do plugin e ao do gráfico): (b0, b1, b2, a1, a2) normalizados, ou None."""
    w = 2 * math.pi * min(f0, EQ_RATE * .49) / EQ_RATE
    cs, sn, A = math.cos(w), math.sin(w), 10 ** (g / 40)
    al = sn / (2 * max(q or .707, .05))
    if kind == 'lp':
        b0 = b2 = (1 - cs) / 2; b1 = 1 - cs; a0, a1, a2 = 1 + al, -2 * cs, 1 - al
    elif kind == 'hp':
        b0 = b2 = (1 + cs) / 2; b1 = -(1 + cs); a0, a1, a2 = 1 + al, -2 * cs, 1 - al
    elif kind == 'bp':
        b0, b1, b2, a0, a1, a2 = al, 0, -al, 1 + al, -2 * cs, 1 - al
    elif kind == 'notch':
        b0, b1, b2, a0, a1, a2 = 1, -2 * cs, 1, 1 + al, -2 * cs, 1 - al
    elif kind == 'ls':
        s2 = 2 * math.sqrt(A) * al
        b0, b1, b2 = A * ((A + 1) - (A - 1) * cs + s2), 2 * A * ((A - 1) - (A + 1) * cs), A * ((A + 1) - (A - 1) * cs - s2)
        a0, a1, a2 = (A + 1) + (A - 1) * cs + s2, -2 * ((A - 1) + (A + 1) * cs), (A + 1) + (A - 1) * cs - s2
    elif kind == 'hs':
        s2 = 2 * math.sqrt(A) * al
        b0, b1, b2 = A * ((A + 1) + (A - 1) * cs + s2), -2 * A * ((A - 1) + (A + 1) * cs), A * ((A + 1) + (A - 1) * cs - s2)
        a0, a1, a2 = (A + 1) - (A - 1) * cs + s2, 2 * ((A - 1) - (A + 1) * cs), (A + 1) - (A - 1) * cs - s2
    elif kind == 'bell':
        b0, b1, b2, a0, a1, a2 = 1 + al * A, -2 * cs, 1 - al * A, 1 + al / A, -2 * cs, 1 - al / A
    else:
        return None
    return b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

def warp_q(f0, q):
    """Q que o plugin usa de fato nos bells do equalizador (largura corrigida perto de fs/2; ver mesa_fx.c)."""
    w = 2 * math.pi * min(f0, EQ_RATE * .49) / EQ_RATE
    return 1 / (2 * math.sinh(math.asinh(1 / (2 * q)) * w / math.sin(w))) if q > 0 and w > 1e-6 else q

def bq_mag_db(c, f):
    w = 2 * math.pi * f / EQ_RATE
    z = complex(math.cos(w), -math.sin(w))
    h = (c[0] + c[1] * z + c[2] * z * z) / (1 + c[3] * z + c[4] * z * z)
    return 20 * math.log10(max(abs(h), 1e-9))

def eq_freqs(prm):
    return [float(prm[f'band{b}Frequency']) for b in range(EQ_FINE)]

def macro_curve(macro, freqs):
    """Resposta somada das bandas macro (dB) nas frequências centrais das finas."""
    out = [0.0] * len(freqs)
    for m in macro:
        c = bq_coef(EQ_KIND.get(int(m['type']), 'bell'), m['freq'], m['gain'], m['q']) if m['gain'] else None
        if c:
            for i, f in enumerate(freqs):
                out[i] += bq_mag_db(c, f)
    return out

def solve(A, b):
    """Resolve A x = b (eliminação de Gauss com pivô), n pequeno."""
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        M[col], M[piv] = M[piv], M[col]
        if abs(M[col][col]) < 1e-12:
            continue
        for r in range(n):
            if r != col and M[r][col]:
                k = M[r][col] / M[col][col]
                M[r] = [a - k * c for a, c in zip(M[r], M[col])]
    return [M[i][n] / M[i][i] if abs(M[i][i]) > 1e-12 else 0.0 for i in range(n)]

def fine_gains(prm, target):
    """Ganhos (dB) das 32 bandas finas cuja soma reproduz `target` nas frequências centrais. Bells vizinhos de
    1/3 de oitava se sobrepõem, então não basta copiar a curva: resolve o sistema com a resposta de cada um a +1 dB."""
    freqs = eq_freqs(prm)
    cols = [bq_coef('bell', freqs[k], 1.0, warp_q(freqs[k], float(prm[f'band{k}Q']) or EQ_FINE_Q)) for k in range(EQ_FINE)]
    A = [[bq_mag_db(cols[k], freqs[j]) for k in range(EQ_FINE)] for j in range(EQ_FINE)]
    return solve(A, target)

def clamp_gain(g):
    return round(max(-12.0, min(12.0, g)), 2)

def macro_fine(prm, macro):
    """Parte das 32 finas que é do macro, já no limite de ±12 dB. O resto (fina − isto) é ajuste feito à mão.
    Comparar com a parte limitada evita que o macro escorregue quando uma fina bate no limite."""
    return [clamp_gain(g) for g in fine_gains(prm, macro_curve(macro, eq_freqs(prm)))]

def eq_macro(pipe, pid, band, prop, value):
    """Muda uma banda macro e move as finas pela diferença entre a curva de antes e a de depois (ajustes feitos
    direto nas finas são preservados). Devolve os ganhos finos novos."""
    if not pid.startswith('equalizer#') or prop not in ('gain', 'q', 'freq', 'type'):
        return None
    with STORE_LOCK:
        p = STORE[pipe]
        prm = p['params'].get(pid)
        if not prm:
            return None
        macro = p.setdefault('macro', {}).setdefault(pid, default_macro())
        if not 0 <= band < len(macro):
            return None
        old = [dict(m) for m in macro]
        m = macro[band]
        if prop == 'gain':
            m['gain'] = clamp_gain(float(value))
        elif prop == 'q':
            m['q'] = round(max(.1, min(20.0, float(value))), 2)
        elif prop == 'freq':
            m['freq'] = round(max(10.0, min(20000.0, float(value))), 1)
        else:
            m['type'] = int(value) if int(value) in EQ_KIND else 1
        d_old, d_new = macro_fine(prm, old), macro_fine(prm, macro)
        gains = [clamp_gain(prm[f'band{b}Gain'] + d_new[b] - d_old[b]) for b in range(EQ_FINE)]
        changed = [(b, g) for b, g in enumerate(gains) if g != prm[f'band{b}Gain']]
    for b, g in changed:
        set_param(pipe, pid, f'band{b}Gain', g)
    SAVE_AT[0] = time.time() + 1      # o macro mudou mesmo sem mexer em fina nenhuma (ex.: Q com ganho 0)
    return gains

def eq_reset(pipe, pid, what):
    """Zera o equalizador. 'macro': as 4 bandas voltam ao padrão e as finas perdem só o que o macro tinha posto
    (ajustes feitos direto nas finas ficam). 'all': tudo plano, finas e macro, com o Q de fábrica."""
    if not pid.startswith('equalizer#') or what not in ('macro', 'all'):
        return False
    with STORE_LOCK:
        p = STORE[pipe]
        prm = p['params'].get(pid)
        if not prm:
            return False
        macro = p.setdefault('macro', {}).setdefault(pid, default_macro())
        old = [dict(m) for m in macro]
        macro[:] = default_macro()
        if what == 'all':
            new = {**{f'band{b}Gain': 0.0 for b in range(EQ_FINE)}, **{f'band{b}Q': EQ_FINE_Q for b in range(EQ_FINE)}}
        else:
            d_old = macro_fine(prm, old)
            new = {f'band{b}Gain': clamp_gain(prm[f'band{b}Gain'] - d_old[b]) for b in range(EQ_FINE)}
        changed = [(k, v) for k, v in new.items() if prm[k] != v]
    for k, v in changed:
        set_param(pipe, pid, k, v)
    SAVE_AT[0] = time.time() + 1
    return True

# ---------------------------------------------------------------- estado de um efeito para a interface

def plugin_state(pipe, pid, i):
    name, idx = pid.split('#')
    with STORE_LOCK:
        P = dict(STORE[pipe]['params'].get(pid) or new_params(name))
    st = {'id': pid, 'plugin': name, 'index': int(idx), 'title': TITLES.get(name, name),
          'bypass': P['bypass'], 'inputGain': P['inputGain'], 'outputGain': P['outputGain'],
          'knobs': [], 'toggles': [], 'bands': [], 'selects': []}
    for prop, label, lo, hi, unit, scale in KNOBS.get(name, []):
        st['knobs'].append({'prop': prop, 'label': label, 'min': lo, 'max': hi, 'unit': unit, 'scale': scale,
                            'value': P[prop]})
    for prop, label, opts in SELECTS.get(name, []):
        st['selects'].append({'prop': prop, 'label': label, 'options': opts, 'value': int(P[prop])})
    for prop, label in TOGGLES.get(name, []):
        st['toggles'].append({'prop': prop, 'label': label, 'value': P[prop]})
    slot = slot_of(pipe, i)
    if name == 'rnnoise':
        # quanto o de-noiser está cortando: nível antes e depois dele
        st['taps'] = {'in': f'lvl:{slot}:in', 'out': f'lvl:{slot}:out'}
    if name == 'filter':
        t = int(P['type'])
        # para o gráfico de resposta (curva do filtro somada à do equalizador)
        st['resp'] = {'type': t, 'frequency': P['frequency'], 'slope': int(P['slope']), 'gain': P['gain'],
                      'quality': P['quality']}
        if t in FILTER_GAIN_TYPES:
            st['knobs'].append({'prop': 'gain', 'label': 'GANHO', 'min': -12, 'max': 12, 'unit': 'dB',
                                'scale': 'lin', 'value': P['gain']})
        if t in FILTER_Q_TYPES:
            st['knobs'].append({'prop': 'quality', 'label': 'Q', 'min': 0.1, 'max': 20, 'unit': '',
                                'scale': 'log', 'value': P['quality']})
    if name == 'equalizer':
        for b in range(int(P['numBands'])):   # as 32 finas: o que o plugin processa (Q fixo, 1/3 de oitava)
            st['bands'].append({'prop': f'band{b}Gain', 'ch': 'left', 'both': True, 'min': -12, 'max': 12,
                                'value': P[f'band{b}Gain'], 'freq': P[f'band{b}Frequency'],
                                'type': int(P[f'band{b}Type']), 'q': P[f'band{b}Q'] or EQ_FINE_Q, 'fine': True,
                                'slope': int(P[f'band{b}Slope']), 'label': fmt_hz(P[f'band{b}Frequency'])})
        with STORE_LOCK:
            macro = (STORE[pipe].get('macro') or {}).get(pid) or default_macro()
        st['macro'] = [{'band': i, 'type': int(m['type']), 'freq': m['freq'], 'gain': m['gain'], 'q': m['q'],
                        'label': fmt_hz(m['freq'])} for i, m in enumerate(macro)]
    elif name == 'compressor':
        # para o gráfico: modo (0 = para baixo, 1 = para cima, 2 = boost) e onde medir antes/depois
        st['comp'] = {'mode': int(P['mode']), 'boostThreshold': P['boostThreshold'],
                      'boostAmount': P['boostAmount'], 'tapIn': f'lvl:{slot}:in', 'tapOut': f'lvl:{slot}:out'}
    return st

def fmt_hz(f):
    if f is None:
        return '?'
    return f'{f / 1000:.1f}k' if f >= 1000 else f'{f:.0f}'

# ---------------------------------------------------------------- PipeWire / pactl

def pactl_json(what):
    try:
        return json.loads(subprocess.run(['pactl', '-f', 'json', 'list', what], capture_output=True,
                                         text=True, timeout=3, env=C_ENV).stdout or '[]')
    except (subprocess.SubprocessError, json.JSONDecodeError):
        return []

def vol_pct(v):
    vals = [int(c['value_percent'].rstrip('%')) for c in (v or {}).values() if 'value_percent' in c]
    return max(vals) if vals else 0

def app_name(props):
    return props.get('application.name') or props.get('media.name') or props.get('node.name') or '?'

HIDDEN_APPS = ('parec', 'Mesa', 'speech-dispatcher')

def default(kind):
    try:
        return subprocess.run(['pactl', f'get-default-{kind}'], capture_output=True, text=True,
                              timeout=2).stdout.strip()
    except subprocess.SubprocessError:
        return None

DEV_NAMES = [('hdmi', 'HDMI (monitor)'),
             ('mesa_sink', 'Mesa'), ('mesa_source', 'Mesa'), ('easyeffects_sink', 'Easy Effects'),
             ('easyeffects_source', 'Easy Effects')]

# Nomes dos canais: entradas recebem nomes genéricos (Entrada 1, 2…), numerados na ordem em que aparecem
# pela primeira vez e lembrados; qualquer canal pode ser renomeado na Mesa (duplo clique no nome).
NAMES_FILE = os.path.expanduser('~/.config/mesa/nomes.json')
NAMES_LOCK = threading.Lock()

def names_load():
    try:
        with open(NAMES_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault('nomes', {})
    d.setdefault('numeros', {})
    return d

def names_save(d):
    os.makedirs(os.path.dirname(NAMES_FILE), exist_ok=True)
    tmp = NAMES_FILE + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, NAMES_FILE)

NAMES = names_load()

def custom_name(key):
    return NAMES['nomes'].get(key)

def dev_label(dev):
    name = dev['name']
    if custom_name(name):
        return custom_name(name)
    if name.startswith('alsa_input'):
        return f'Entrada {number(name, "alsa_input")}'
    for k, v in DEV_NAMES:
        if k in name:
            return v
    desc = dev.get('description')
    if desc and desc != '(null)':          # com LC_ALL=C o pactl troca descrições com acento por "(null)"
        return short_dev(desc)
    return f'Saída {number(name, "alsa_output")}' if name.startswith('alsa_output') else name

def number(name, prefix):
    """Número fixo do dispositivo entre os do mesmo tipo (entradas ou saídas), na ordem em que apareceram."""
    with NAMES_LOCK:
        n = NAMES['numeros'].get(name)
        if n is None:
            n = NAMES['numeros'][name] = max((v for k, v in NAMES['numeros'].items() if k.startswith(prefix)),
                                             default=0) + 1
            names_save(NAMES)
    return n

def short_dev(desc):
    d = re.sub(r'\s*(Analog Stereo|Mono|Digital Stereo \(HDMI\))\s*', ' ', desc or '')
    return d.strip() or desc

def mixer_state():
    sinks = pactl_json('sinks')
    sources = pactl_json('sources')
    sink_inputs = pactl_json('sink-inputs')
    source_outputs = pactl_json('source-outputs')
    sink_by_idx = {s['index']: s for s in sinks}
    src_by_idx = {s['index']: s for s in sources}
    apps, by_name, loop_sinks = [], {}, {}
    for si in sink_inputs:
        p = si.get('properties', {})
        nm = app_name(p)
        node = p.get('node.name', '')
        if node == 'mesa_retorno':                                    # retorno: para onde ele toca
            loop_sinks[node] = sink_by_idx.get(si.get('sink'), {}).get('name')
        if any(h.lower() in nm.lower() for h in HIDDEN_APPS) or node.startswith('mesa_'):
            continue
        sink = sink_by_idx.get(si.get('sink'), {}).get('name', '')
        STREAM_SINK[si['index']] = sink
        a = by_name.get(nm.lower())
        if a:      # mesmo app com vários fluxos (ex.: Vesktop voz + sons): um canal só
            a['ids'].append(si['index'])
            a['volume'] = max(a['volume'], vol_pct(si.get('volume')))
            a['mute'] = a['mute'] and si.get('mute', False)
            continue
        a = by_name[nm.lower()] = {'kind': 'sink-input', 'ids': [si['index']], 'name': custom_name('app:' + nm.lower()) or nm, 'rkey': 'app:' + nm.lower(),
                                   'volume': vol_pct(si.get('volume')), 'mute': si.get('mute', False), 'target': sink}
        apps.append(a)
    for a in apps:
        a['id'] = '+'.join(map(str, a.pop('ids')))
        a['meter'] = 'app:' + a['id']
    masters = [{'kind': 'sink', 'id': s['name'], 'name': dev_label(s), 'rkey': s['name'],
                'volume': vol_pct(s.get('volume')), 'mute': s.get('mute', False), 'meter': f'sink:{s["name"]}'}
               for s in sinks if s['name'].startswith('alsa_output')]
    mics = [{'kind': 'source', 'id': s['name'], 'name': dev_label(s), 'rkey': s['name'],
             'volume': vol_pct(s.get('volume')), 'mute': s.get('mute', False), 'meter': f'src:{s["name"]}'}
            for s in sources if s['name'].startswith('alsa_input')]
    ee_src = next((s for s in sources if s['name'] == SOURCE), None)
    recorders = []
    for so in source_outputs:
        p = so.get('properties', {})
        nm = app_name(p)
        node = p.get('node.name', '')
        if nm == 'parec' or 'Mesa' in nm or (node.startswith('mesa_') and node != 'mesa_retorno_in'):
            continue
        to = None
        if node == 'mesa_retorno_in':
            nm, to = 'Retorno do mic', loop_sinks.get('mesa_retorno')
        recorders.append({'name': nm, 'source': src_by_idx.get(so.get('source'), {}).get('name', ''), 'to': to})
    return {'apps': apps, 'masters': masters, 'mics': mics, 'recorders': recorders,
            'ee_source': ee_src and {'kind': 'source', 'id': SOURCE, 'name': custom_name('fxout') or 'FX OUT', 'rkey': 'fxout',
                                     'volume': vol_pct(ee_src.get('volume')), 'mute': ee_src.get('mute', False),
                                     'meter': f'src:{SOURCE}'},
            'sink_names': {s['name']: dev_label(s) for s in sinks},
            'source_names': {s['name']: dev_label(s) for s in sources},
            'default_sink': default('sink'), 'default_source': default('source')}

# ---------------------------------------------------------------- medidores

class MeterHub(threading.Thread):
    """Todos os medidores num só nó de gravação do PipeWire (pw-record sem conexão automática, 2 canais
    por medidor). Antes era um parec/pw-record por ponto, e cada um aparecia como um app gravando no
    Easy Effects. Os pontos são ligados à mão (pw-link, pelos ids das portas do pw-dump) e religados
    se a cadeia mudar, um app abrir/fechar ou a cadeia reiniciar.
      src:<fonte>     portas capture_* da fonte          sink:<saída>   portas monitor_* da saída
      app:<serial>    portas output_* do fluxo do app    tapout:<nó>    portas output_* do nó de efeito
      tapin:<nó>      o que chega nas entradas do nó de efeito (a saída do efeito anterior)
    Medidores comuns guardam o maior pico desde a última leitura; os do meio da cadeia (tap*), RMS (~10 ms),
    porque o compressor reage ao RMS e o gráfico dele mede igual."""
    SLOTS = 24
    # RMS com constante de ~10 ms, atualizado a cada bloco de 4 ms
    A = 1 - math.exp(-4 / 10)

    def __init__(self):
        super().__init__(daemon=True)
        self.name = f'mesa_medidores_{os.getpid()}'
        self.lock = threading.Lock()
        self.slot = {}                       # chave -> número do par de canais
        self.seen = {}                       # chave -> último pedido da interface
        self.peak = [[0, 0] for _ in range(self.SLOTS)]
        self.ms = [[0.0, 0.0] for _ in range(self.SLOTS)]
        self.proc = None
        self.alive = True
        self.wake = threading.Event()
        threading.Thread(target=self.keep_linked, daemon=True).start()

    # ---- leitura
    def run(self):
        n = 2 * self.SLOTS
        cmap = ','.join(f'AUX{i}' for i in range(n))
        while self.alive:
            try:
                self.proc = subprocess.Popen(
                    ['pw-record', '-P', f'{{ node.autoconnect=false node.name={self.name} application.name=Mesa '
                     f'media.name=Medidores node.description=Mesa }}', '--latency', '8ms', '--rate', '16000',
                     '--channels', str(n), '--channel-map', cmap, '--format', 's16', '--raw', '-'],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                self.wake.set()
                block = 64 * n * 2                    # 4 ms
                while self.alive:
                    b = self.proc.stdout.read(block)
                    if not b:
                        break
                    a = array.array('h', b[:len(b) // (2 * n) * (2 * n)])
                    if not a:
                        continue
                    with self.lock:
                        active = list(self.slot.items())
                    for key, i in active:
                        l, r = a[2 * i::n], a[2 * i + 1::n]
                        if key.startswith('tap'):
                            m = self.ms[i]
                            m[0] += self.A * (sum(v * v for v in l) / len(l) - m[0])
                            m[1] += self.A * (sum(v * v for v in r) / len(r) - m[1])
                        else:
                            p = self.peak[i]
                            p[0] = max(p[0], max(l), -min(l))
                            p[1] = max(p[1], max(r), -min(r))
            except OSError:
                pass
            finally:
                self.stop_proc()
            if self.alive:
                time.sleep(1)

    def take(self, key):
        i = self.slot.get(key)
        if i is None:
            return None
        if key.startswith('tap'):
            return [round(10 * math.log10(m / 32768 ** 2), 1) if m > 1 else -90.0 for m in self.ms[i]]
        a, self.peak[i] = self.peak[i], [0, 0]
        return [round(20 * math.log10(min(p, 32767) / 32768), 1) if p else -90.0 for p in a]

    # ---- quais pontos medir
    def want(self, keys):
        """Reserva um par de canais para cada chave pedida; libera as que ninguém pede há 5 s."""
        now, changed = time.time(), False
        with self.lock:
            for k in keys:
                self.seen[k] = now
                if k not in self.slot:
                    free = sorted(set(range(self.SLOTS)) - set(self.slot.values()))
                    if free:
                        self.slot[k] = free[0]
                        self.peak[free[0]], self.ms[free[0]] = [0, 0], [0.0, 0.0]
                        changed = True
            for k in [k for k, t in self.seen.items() if now - t > 5]:
                self.seen.pop(k)
                self.slot.pop(k, None)
                changed = True
        if changed:
            self.wake.set()

    # ---- ligações
    def keep_linked(self):
        while self.alive:
            self.wake.wait(1.5)
            self.wake.clear()
            if self.alive:
                try:
                    self.link()
                except Exception:
                    pass

    def link(self):
        try:
            dump = json.loads(subprocess.run(['pw-dump'], capture_output=True, text=True, timeout=3).stdout or '[]')
        except (subprocess.SubprocessError, json.JSONDecodeError):
            return
        nodes, ports, links = {}, {}, []
        for o in dump:
            t, info = o.get('type', ''), o.get('info') or {}
            if t.endswith('Node'):
                nodes[o['id']] = info.get('props', {})
            elif t.endswith('Port'):
                p = info.get('props', {})
                ports.setdefault(p.get('node.id'), []).append(
                    (o['id'], info.get('direction') or p.get('port.direction'), p.get('port.name', ''), p.get('audio.channel', '')))
            elif t.endswith('Link'):
                links.append((o['id'], info.get('output-port-id'), info.get('input-port-id'), info.get('input-node-id')))
        me = next((i for i, p in nodes.items() if p.get('node.name') == self.name), None)
        if me is None:
            return
        mine = {name: pid for pid, d, name, ch in ports.get(me, []) if d in ('input', 'in')}
        by_name = {}
        for i, p in nodes.items():
            by_name.setdefault(p.get('node.name'), i)
        feeding = {}                                   # porta de entrada -> portas de saída ligadas nela
        for lid, op, ip, inode in links:
            feeding.setdefault(ip, []).append(op)

        def outs(node, prefix):
            return [(pid, ch, name) for pid, d, name, ch in ports.get(node, [])
                    if d in ('output', 'out') and name.startswith(prefix)]

        def lr(lst):
            """Portas (esquerda, direita) de uma lista [(id, canal, nome)]; mono vai nos dois lados."""
            if not lst:
                return [], []
            ch = {c: [p] for p, c, _ in lst}
            if 'FL' in ch or 'FR' in ch:
                return ch.get('FL', []), ch.get('FR', ch.get('FL', []))
            if len(lst) == 1:
                return [lst[0][0]], [lst[0][0]]
            return [lst[0][0]], [lst[1][0]]

        with self.lock:
            active = list(self.slot.items())
        want = set()
        for key, i in active:
            kind, _, arg = key.partition(':')
            if kind == 'src':
                L, R = lr(outs(by_name.get(arg), 'capture_'))
            elif kind == 'sink':
                L, R = lr(outs(by_name.get(arg), 'monitor_'))
            elif kind == 'app':
                node = next((n for n, p in nodes.items() if str(p.get('object.serial')) == arg), None)
                L, R = lr(outs(node, 'output_'))
            elif kind == 'tapout':
                L, R = lr(outs(by_name.get(arg), 'output_'))
            elif kind == 'tapin':
                node = by_name.get(arg)
                ins = {ch: pid for pid, d, name, ch in ports.get(node, []) if d in ('input', 'in')}
                own = set(mine.values())
                src = lambda ch: [op for op in feeding.get(ins.get(ch), []) if op not in own]
                L, R = src('FL'), src('FR')
            else:
                continue
            for side, lst in ((0, L), (1, R)):
                dst = mine.get(f'input_AUX{2 * i + side}')
                for op in lst:
                    if dst is not None:
                        want.add((op, dst))
        have = {(op, ip): lid for lid, op, ip, inode in links if inode == me}
        for pair, lid in have.items():
            if pair not in want:
                subprocess.run(['pw-link', '-d', str(lid)], capture_output=True, timeout=2)
        for op, ip in want - set(have):
            subprocess.run(['pw-link', str(op), str(ip)], capture_output=True, timeout=2)

    def stop_proc(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def stop(self):
        self.alive = False
        self.wake.set()
        self.stop_proc()


class Levels:
    """Níveis do meio da cadeia, escritos pelos próprios plugins (RMS ~10 ms) em /dev/shm."""
    NSLOT = 64

    def __init__(self):
        self.mm = None

    def take(self, key):
        m = re.fullmatch(r'lvl:(\d+):(in|out)', key)
        if not m:
            return None
        if self.mm is None:
            try:
                with open(LEVELS_FILE, 'rb') as f:
                    self.mm = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)
            except (OSError, ValueError):
                return None
        slot, off = int(m[1]), 0 if m[2] == 'in' else 2
        if slot >= self.NSLOT:
            return None
        try:
            ms = struct.unpack_from('4f', self.mm, 8 + 16 * slot)
        except (struct.error, ValueError):
            self.mm = None
            return None
        return [round(10 * math.log10(v), 1) if v > 1e-9 else -90.0 for v in ms[off:off + 2]]

HUB = None
LEVELS = Levels()
METER_LOCK = threading.Lock()
STREAM_SINK = {}   # índice do fluxo de app -> sink onde ele toca

def sub_keys(k):
    """'app:12+15' mede cada fluxo separado; o canal mostra o maior."""
    return [f'app:{i}' for i in k[4:].split('+')] if k.startswith('app:') else [k]

def ensure_meters(keys):
    """Garante um par de canais do nó de medidores para cada ponto pedido pela interface."""
    global HUB
    with METER_LOCK:
        if HUB is None:
            HUB = MeterHub()
            HUB.start()
        HUB.want([sk for key in keys for sk in sub_keys(key) if sk.split(':')[0] in ('src', 'sink', 'app')])

def stop_meters():
    global HUB
    with METER_LOCK:
        if HUB:
            HUB.stop()
            HUB = None

def meter_value(sk):
    if sk.startswith('lvl:'):
        return LEVELS.take(sk)
    return HUB.take(sk) if HUB else None

# ---------------------------------------------------------------- estado completo

LAST_MIX = {}

def full_state(pipe):
    global LAST_MIX
    mix = mixer_state()
    LAST_MIX = mix
    names = {s['id'] for s in mix['mics']} | {s['id'] for s in mix['masters']}
    with STORE_LOCK:
        cfg = {p: {'plugins': list(STORE[p]['plugins']), 'device': STORE[p]['device']} for p in NODES}
    src_up = mix['ee_source'] is not None
    up = src_up and not APPLY['busy']
    # a cadeia salva aparece mesmo com o serviço parado ou reiniciando: antes a janela a via vazia e o
    # primeiro efeito adicionado apagava os outros (/api/apply manda a lista inteira)
    chain = [plugin_state(pipe, p, i) for i, p in enumerate(cfg[pipe]['plugins'])]
    devs = {'input': cfg['input']['device'] or mix['default_source'],
            'output': cfg['output']['device'] or mix['default_sink']}
    dev = devs[pipe]
    if pipe == 'input':
        devices = [{'id': m['id'], 'name': m['name']} for m in mix['mics']]
        flow = {'from': [{'name': mix['source_names'].get(dev, dev or '?'), 'kind': 'mic', 'device': dev}],
                'to': [{'name': r['name'], 'kind': 'app', 'to': r['to']} for r in mix['recorders']
                       if r['source'] == SOURCE],
                'bus': 'Mesa (microfone)'}
    else:
        devices = [{'id': m['id'], 'name': m['name']} for m in mix['masters']]
        flow = {'from': [{'name': a['name'], 'kind': 'app'} for a in mix['apps'] if a['target'] == SINK],
                'to': [{'name': mix['sink_names'].get(dev, dev or '?'), 'kind': 'speaker', 'device': dev}],
                'bus': 'Mesa (efeitos de saída)'}
    # dispositivos das duas pontas da cadeia (para colorir as rotas)
    mix['ee_devices'] = devs
    return {'pipe': pipe, 'ee_running': up, 'chain': chain, 'flow': flow, 'mix': mix,
            'device': dev, 'devices': devices, 'catalog': CATALOG, 'apply': APPLY, 'missing': dev not in names,
            'monitor': bool(retorno_pids()), 'monitor_sink': retorno_sink() or default('sink'), 'has_retorno': True,
            'fps': STATS['fps'], 'version': VERSION, 'ee_alive': ee_alive()}

def ee_alive():
    """O Easy Effects ainda aberto: os dois juntos processariam o mic duas vezes."""
    return 'easyeffects_source' in LAST_MIX.get('source_names', {})

# ---------------------------------------------------------------- HTTP

class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def send(self, code, body, ctype='application/json'):
        b = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(b)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        STATS['seen'] = time.time()
        path, _, qs = self.path.partition('?')
        q = dict(p.split('=', 1) for p in qs.split('&') if '=' in p)
        if path == '/':
            with open(os.path.join(HERE, 'ui.html'), 'rb') as f:
                return self.send(200, f.read(), 'text/html; charset=utf-8')
        if path == '/api/state':
            return self.send(200, full_state('output' if q.get('pipe') == 'output' else 'input'))
        if path == '/api/meters':
            return self.meter_stream([unquote(k) for k in q.get('keys', '').split(',') if k])
        self.send(404, {'erro': 'nao encontrado'})

    def meter_stream(self, keys):
        """Server-Sent Events: pico desde o último envio, a cada ~4 ms (a tela desenha no fps do monitor)."""
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        last_keep = 0
        try:
            while True:
                now = time.time()
                if now - last_keep > 1:
                    ensure_meters(keys)
                    last_keep = now
                data = {}
                with METER_LOCK:
                    for k in keys:
                        vals = [v for v in (meter_value(sk) for sk in sub_keys(k)) if v]
                        if vals:
                            data[k] = [max(v[0] for v in vals), max(v[1] for v in vals)]
                self.wfile.write(b'data: ' + json.dumps(data, separators=(',', ':')).encode() + b'\n\n')
                STATS['seen'] = time.time()
                self.wfile.flush()
                time.sleep(0.004)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        n = int(self.headers.get('Content-Length', 0))
        try:
            d = json.loads(self.rfile.read(n) or b'{}')
        except json.JSONDecodeError:
            return self.send(400, {'erro': 'json'})
        pipe = 'output' if d.get('pipe') == 'output' else 'input'
        if self.path == '/api/ee':
            name, idx, prop = str(d['plugin']), int(d.get('index', 0)), str(d['prop'])
            val = d['value']
            if not isinstance(val, bool):
                val = float(val)
                if not math.isfinite(val) or abs(val) > 1e6:
                    return self.send(400, {'erro': 'valor'})
            ok = set_param(pipe, f'{name}#{idx}', prop, val)
            return self.send(200 if ok else 400, {'ok': ok})
        if self.path == '/api/eq_macro':   # {pipe, plugin, index, band, prop: gain|q|freq|type, value}
            pid = f"{d.get('plugin', 'equalizer')}#{int(d.get('index', 0))}"
            try:
                val = float(d['value'])
                if not math.isfinite(val):
                    raise ValueError
                gains = eq_macro(pipe, pid, int(d.get('band', -1)), str(d.get('prop')), val)
            except (KeyError, ValueError, TypeError):
                return self.send(400, {'erro': 'valor'})
            return self.send(200 if gains is not None else 400, {'ok': gains is not None, 'fine': gains})
        if self.path == '/api/eq_reset':   # {pipe, plugin, index, what: macro|all}
            ok = eq_reset(pipe, f"{d.get('plugin', 'equalizer')}#{int(d.get('index', 0))}", str(d.get('what')))
            return self.send(200 if ok else 400, {'ok': ok})
        if self.path == '/api/apply':
            # cadeia e/ou dispositivo de uma vez: um reinício só
            plugins, dev = d.get('plugins'), d.get('device')
            if plugins is not None:
                if (not isinstance(plugins, list) or len(plugins) > 30 or len(set(plugins)) != len(plugins)
                        or not all(isinstance(p, str) and PLUGIN_RE.fullmatch(p) and p.split('#')[0] in FX
                                   for p in plugins)):
                    return self.send(400, {'erro': 'cadeia invalida'})
            if dev:
                dev = str(dev)
                if dev not in {m['id'] for m in LAST_MIX.get('mics' if pipe == 'input' else 'masters', [])}:
                    return self.send(400, {'erro': 'dispositivo'})
            if plugins is None and not dev:
                return self.send(400, {'erro': 'nada a aplicar'})

            def edit():
                p = STORE[pipe]
                if plugins is not None:
                    p['params'] = {x: p['params'].get(x) or new_params(x.split('#')[0]) for x in plugins}
                    p['plugins'] = list(plugins)
                if dev:
                    p['device'] = dev
            ok = apply_change('Aplicando as mudanças', edit)
            return self.send(200 if ok else 409, {'ok': ok})
        if self.path == '/api/vol':
            kind, ident = d.get('kind'), str(d.get('id'))
            cmd = {'sink': 'sink', 'source': 'source', 'sink-input': 'sink-input'}.get(kind)
            if not cmd or not re.fullmatch(r'[A-Za-z0-9_.:+-]+', ident):
                return self.send(400, {'erro': 'parametro'})
            for one in (ident.split('+') if kind == 'sink-input' else [ident]):
                if 'volume' in d:
                    v = max(0, min(150, int(d['volume'])))
                    subprocess.run(['pactl', f'set-{cmd}-volume', one, f'{v}%'], timeout=3)
                if 'mute' in d:
                    subprocess.run(['pactl', f'set-{cmd}-mute', one, '1' if d['mute'] else '0'], timeout=3)
            return self.send(200, {'ok': True})
        if self.path == '/api/fps':      # taxa de desenho dos medidores informada pela janela
            STATS['fps'] = int(d.get('fps', 0))
            return self.send(200, {'ok': True})
        if self.path == '/api/rename':     # {key, name}; nome vazio volta ao padrão
            key, nm = str(d.get('key', ''))[:200], str(d.get('name', '')).strip()[:40]
            if not key:
                return self.send(400, {'erro': 'key'})
            with NAMES_LOCK:
                if nm:
                    NAMES['nomes'][key] = nm
                else:
                    NAMES['nomes'].pop(key, None)
                names_save(NAMES)
            return self.send(200, {'ok': True})
        if self.path == '/api/monitor':
            sink = d.get('sink')
            if sink is not None:
                # trocar a saída: grava a escolha e, se o retorno estava ligado, religa nela
                if sink not in {m['id'] for m in LAST_MIX.get('masters', [])}:
                    return self.send(400, {'erro': 'saida invalida'})
                os.makedirs(CONF, exist_ok=True)
                with open(RETORNO_SINK, 'w') as f:
                    f.write(sink + '\n')
                if retorno_pids():
                    retorno_stop()
                    retorno_start()
                return self.send(200, {'ok': True})
            if retorno_pids():
                retorno_stop()
            else:
                retorno_start()
            return self.send(200, {'ok': True})
        self.send(404, {'erro': 'nao encontrado'})

# ---------------------------------------------------------------- janela

def find_chromium():
    return next((c for c in ('/snap/bin/chromium', '/usr/bin/chromium', '/usr/bin/chromium-browser',
                             '/usr/bin/google-chrome') if os.access(c, os.X_OK)), None)

def chromium_args(chromium, url):
    perfil = os.path.expanduser('~/snap/chromium/common/mesa-perfil' if chromium.startswith('/snap/')
                                else '~/.local/share/mesa/perfil-chromium')
    return [chromium, f'--app={url}', f'--user-data-dir={perfil}', '--ozone-platform-hint=auto',
            '--no-first-run', '--no-default-browser-check', '--start-maximized', '--class=mesa']

def open_chromium(chromium, url):
    subprocess.Popen(chromium_args(chromium, url),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

CHROME_APP_ID = 'chrome-127.0.0.1__-Default'   # app_id da janela do Chromium em modo app (Wayland)
SOCKET_UNIT = 'mesa-janela.socket'

def write_if_changed(path, txt):
    try:
        if open(path, encoding='utf-8').read() == txt:
            return False
    except OSError:
        pass
    with open(path, 'w', encoding='utf-8') as f:
        f.write(txt)
    return True

def systemd_user(*args):
    try:
        return subprocess.run(['systemctl', '--user', *args], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False

def desktop_entry():
    """Rodando da AppImage: põe a Mesa no menu (e, com Chromium, o socket que liga o servidor).
    Devolve True quando o socket do systemd está escutando: aí basta abrir a janela."""
    img = os.environ.get('APPIMAGE')
    if not img:
        return False
    apps = os.path.join(HOME, '.local/share/applications')
    units = os.path.join(HOME, '.config/systemd/user')
    icon_src = os.path.join(HERE, 'mesa.svg')
    icon = os.path.join(DATA, 'mesa.svg')
    chromium = find_chromium()
    # Ícone: o Chromium em modo app se identifica no Wayland como chrome-127.0.0.1__-Default (o --class só vale
    # no X11), daí o StartupWMClass. Mas o Ubuntu só liga uma janela do Chromium snap a um atalho cujo comando
    # seja o /snap/bin/chromium (patch chromium-snap-pwa do gnome-shell); com outro comando ele mostra o ícone
    # do Chromium. Por isso o atalho abre o Chromium direto, e o servidor nasce sozinho pela ativação por socket
    # do systemd quando a janela se conecta (mesa-janela.socket → "Mesa.AppImage --servico").
    ativo = False
    try:
        os.makedirs(apps, exist_ok=True)
        if os.path.exists(icon_src) and file_hash(icon_src) != file_hash(icon):
            os.makedirs(DATA, exist_ok=True)
            shutil.copyfile(icon_src, icon)
        if chromium and shutil.which('systemctl'):
            os.makedirs(units, exist_ok=True)
            mudou = write_if_changed(os.path.join(units, SOCKET_UNIT),
                                     f'[Unit]\nDescription=Mesa: liga o servidor da interface quando a janela abre\n\n'
                                     f'[Socket]\nListenStream=127.0.0.1:{PORT}\n\n[Install]\nWantedBy=sockets.target\n')
            mudou |= write_if_changed(os.path.join(units, 'mesa-janela.service'),
                                      f'[Unit]\nDescription=Mesa: servidor da interface\n\n'
                                      f'[Service]\nExecStart="{img}" --servico\n')
            if mudou:
                systemd_user('daemon-reload')
            ativo = systemd_user('enable', '--now', SOCKET_UNIT)
        if ativo:
            cmd = ' '.join(f'"{a}"' if ' ' in a else a for a in chromium_args(chromium, f'http://127.0.0.1:{PORT}/'))
        else:
            cmd = f'"{img}"'
        txt = (f'[Desktop Entry]\nType=Application\nName=Mesa\nComment=Mesa de mixagem e efeitos do PipeWire\n'
               f'Exec={cmd}\nIcon={icon}\nTerminal=false\nCategories=AudioVideo;Audio;Mixer;\n'
               f'StartupWMClass={CHROME_APP_ID}\n')
        q = os.path.join(apps, 'chromium_chromium-mesa.desktop')     # nome usado numa tentativa anterior
        try:
            if 'Name=Mesa\n' in open(q, encoding='utf-8').read():
                os.remove(q)
        except OSError:
            pass
        write_if_changed(os.path.join(apps, 'mesa.desktop'), txt)
    except OSError:
        pass
    return ativo

def serve_activated():
    """--servico: servidor da interface ligado pelo mesa-janela.socket (o systemd passa o socket no fd 3).
    Sai quando a janela some (8 s sem contato); a próxima conexão liga de novo."""
    if os.environ.get('LISTEN_FDS') != '1':
        sys.exit('--servico: rode pelo mesa-janela.socket do systemd')
    start_engine()
    sock = socket.socket(fileno=3)
    srv = ThreadingHTTPServer(sock.getsockname(), H, bind_and_activate=False)
    srv.socket.close()
    srv.socket = sock
    srv.server_name, srv.server_port = '127.0.0.1', PORT
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    STATS['seen'] = time.time() + 30
    try:
        while time.time() - STATS['seen'] < 8:
            time.sleep(1)
    finally:
        stop_meters()
        srv.shutdown()

def start_engine():
    global STORE
    STORE = store_load()
    try:
        store_save()      # grava já as conversões feitas na carga (ex.: desenho novo do equalizador)
    except OSError:
        pass
    try:
        if install_engine():
            restart_engine()
    except Exception as e:  # noqa: BLE001
        APPLY['error'] = f'Não deu para ligar a cadeia de efeitos: {e}'
    threading.Thread(target=live_loop, daemon=True).start()

def main():
    start_engine()
    if not find_chromium():
        desktop_entry()
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{PORT}/'
    if '--sem-janela' in sys.argv:
        print(url, flush=True)
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        try:
            signal.pause()
        finally:
            stop_meters()
        return
    chromium = find_chromium()
    if chromium and '--gtk' not in sys.argv:
        # Chromium em modo app: os medidores acompanham o monitor (o WebKitGTK fica preso em 60 fps
        # porque o driver NVIDIA nega o drmWaitVBlank). Perfil próprio, separado do navegador.
        # O Chromium do snap não aceita sinais de fora, então a Mesa fecha quando a janela some:
        # sem nenhum contato por 8 s (a janela consulta o estado a cada 1 s e recebe os medidores sem parar).
        open_chromium(chromium, url)
        signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
        STATS['seen'] = time.time() + 30          # tempo para a janela abrir
        try:
            while time.time() - STATS['seen'] < 8:
                time.sleep(1)
        finally:
            stop_meters()
            srv.shutdown()
        return
    import gi
    gi.require_version('Gtk', '3.0')
    gi.require_version('WebKit2', '4.1')
    from gi.repository import Gtk, WebKit2, GLib
    GLib.set_prgname(CHROME_APP_ID)    # mesmo app_id da janela do Chromium: casa com o StartupWMClass do atalho
    win = Gtk.Window(title='Mesa')
    win.set_default_size(1600, 940)
    win.set_icon_name('audio-volume-high')
    view = WebKit2.WebView()
    # medidores no fps do monitor: o WebKit vem limitado a ~60 fps por padrão
    settings = view.get_settings()
    feats = WebKit2.Settings.get_all_features()
    for i in range(feats.get_length()):
        if feats.get(i).get_identifier() == 'PreferPageRenderingUpdatesNear60FPS':
            settings.set_feature_enabled(feats.get(i), False)
    view.load_uri(url)
    win.add(view)

    def bye(*a):
        stop_meters()
        srv.shutdown()
        Gtk.main_quit()
    win.connect('destroy', bye)
    try:
        gi.require_version('GLibUnix', '2.0')
        from gi.repository import GLibUnix
        GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, bye)
    except (ValueError, ImportError):
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, bye)
    win.maximize()
    win.show_all()
    Gtk.main()

if __name__ == '__main__':
    if '--servico' in sys.argv:
        serve_activated()
        sys.exit(0)
    # com o socket do systemd escutando, abrir a janela basta: a conexão dela liga o servidor
    if find_chromium() and '--gtk' not in sys.argv and '--sem-janela' not in sys.argv and desktop_entry():
        open_chromium(find_chromium(), f'http://127.0.0.1:{PORT}/')
        sys.exit(0)
    # uma instância só: se a porta já está em uso, a mesa já está aberta
    try:
        socket.create_connection(('127.0.0.1', PORT), timeout=0.3).close()
        if find_chromium() and '--gtk' not in sys.argv and '--sem-janela' not in sys.argv:
            open_chromium(find_chromium(), f'http://127.0.0.1:{PORT}/')
            sys.exit(0)
        sys.exit('A Mesa já está aberta.')
    except OSError:
        pass
    main()
