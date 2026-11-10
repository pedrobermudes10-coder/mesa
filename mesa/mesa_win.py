#!/usr/bin/env python3
# Mesa para Windows — a mesma interface (ui.html) e o mesmo servidor (mesa.py), com os efeitos rodando no
# Equalizer APO: o MesaFX.dll (VST, plugins/mesa_vst.c) roda dentro do serviço de áudio do Windows.
#   Cadeia: memória compartilhada Global\MesaFX. Ajustes vão direto no controle (ao vivo); mudar a ordem,
#     adicionar ou remover escreve a cadeia no outro banco e o plugin troca no bloco seguinte.
#     %ProgramData%\Mesa\cadeia.bin guarda a cadeia para o plugin achar no boot, com a janela fechada.
#   Dispositivo: <config do Equalizer APO>\mesa.txt (Stage / Device / VSTPlugin), incluído no config.txt.
#     Mudar o dispositivo faz o Equalizer APO recarregar (o som falha por um instante).
#   Volumes, apps e medidores: API de áudio do Windows (MMDevice / WASAPI) via ctypes.
import ctypes, json, math, os, re, socket, struct, subprocess, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mesa as M  # noqa: E402

WIN = sys.platform == 'win32'
if WIN:
    import winreg
    from ctypes import wintypes as W

HERE = M.HERE
APPDATA = os.environ.get('APPDATA') or os.path.expanduser('~/.config')
LOCAL = os.environ.get('LOCALAPPDATA') or APPDATA
PROGDATA = os.path.join(os.environ.get('ProgramData') or r'C:\ProgramData', 'Mesa')
BIN_FILE = os.path.join(PROGDATA, 'cadeia.bin')
DLL = os.path.join(HERE, 'MesaFX.dll')
LOG = os.path.join(LOCAL, 'Mesa', 'mesa.log')

M.CONF = os.path.join(APPDATA, 'Mesa')
M.STORE_FILE = os.path.join(M.CONF, 'cadeia.json')
M.NAMES_FILE = os.path.join(M.CONF, 'nomes.json')
M.NAMES = M.names_load()
M.LIB_SRC = None
M.RETORNO_SINK = os.path.join(M.CONF, 'retorno-saida')     # saída escolhida para o retorno do mic
RETORNO_EXE = os.path.join(HERE, 'mesa_retorno.exe')

# ---------------------------------------------------------------- formato da memória (igual a mesa_vst.c)

MAXCTL, MAXFX = 192, 32
FX_SIZE = 4 + 4 * MAXCTL
CHAIN_SIZE = 8 + MAXFX * FX_SIZE
CHAIN_OFF = 256
LEVELS_OFF = CHAIN_OFF + 4 * CHAIN_SIZE
SHM_SIZE = LEVELS_OFF + 8 + 64 * 16 + 64 * 64
SHM_MAGIC, FILE_MAGIC = 0x3158464d, 0x4443414d
# cabeçalho: magic, version, gen[2], bank[2], ack[2], beat[2], rate[2], block[2], us[2], chans[2], pipe_ms[2][4]
H_GEN, H_BANK, H_ACK, H_BEAT, H_RATE, H_US, H_CHANS, H_PMS = 8, 16, 24, 32, 40, 56, 64, 72
PIPE_I = {'input': 0, 'output': 1}

def chain_off(pi, bank):
    return CHAIN_OFF + (pi * 2 + bank) * CHAIN_SIZE

# ---------------------------------------------------------------- controles de cada efeito (do próprio DLL)

class _Desc(ctypes.Structure):
    _fields_ = [('id', ctypes.c_ulong), ('label', ctypes.c_char_p), ('props', ctypes.c_int),
                ('name', ctypes.c_char_p), ('maker', ctypes.c_char_p), ('copy', ctypes.c_char_p),
                ('nports', ctypes.c_ulong), ('pdesc', ctypes.c_void_p), ('pnames', ctypes.POINTER(ctypes.c_char_p))]

def load_ctls():
    """rótulo LADSPA -> (índice do tipo, [nomes dos controles na ordem das portas])"""
    lib = ctypes.CDLL(DLL if WIN else os.path.join(HERE, '../plugins/mesa_fx.so'))
    lib.ladspa_descriptor.restype = ctypes.POINTER(_Desc)
    lib.ladspa_descriptor.argtypes = [ctypes.c_ulong]
    out, i = {}, 0
    while True:
        p = lib.ladspa_descriptor(i)
        if not p:
            return out
        d = p.contents
        out[d.label.decode()] = (i, [d.pnames[k].decode() for k in range(4, d.nports)])
        i += 1

CTLS = {}

def chain_bytes(pipe):
    with M.STORE_LOCK:
        p = M.STORE[pipe]
        plugins = list(p['plugins'])[:MAXFX]
        params = {pid: dict(p['params'][pid]) for pid in plugins}
    buf = bytearray(CHAIN_SIZE)
    n = 0
    for i, pid in enumerate(plugins):
        label = M.FX[pid.split('#')[0]][0]
        if label not in CTLS:
            continue
        kind, names = CTLS[label]
        vals = {**params[pid], 'slot': M.slot_of(pipe, i)}
        struct.pack_into(f'<i{MAXCTL}f', buf, 8 + n * FX_SIZE, kind,
                         *[float(vals.get(nm, 0)) for nm in names], *[0.0] * (MAXCTL - len(names)))
        n += 1
    struct.pack_into('<ii', buf, 0, n, 0)
    return bytes(buf)

def write_bin():
    data = struct.pack('<4I', FILE_MAGIC, 1, MAXCTL, MAXFX) + chain_bytes('input') + chain_bytes('output')
    try:
        if open(BIN_FILE, 'rb').read() == data:
            return
    except OSError:
        pass
    os.makedirs(PROGDATA, exist_ok=True)
    tmp = BIN_FILE + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, BIN_FILE)

# ---------------------------------------------------------------- memória compartilhada

class Shm:
    """Abre a memória que o plugin criou (a Mesa não cria: sem o plugin carregado ela não existe)."""
    def __init__(self):
        self.buf = None
        self.tried = 0
        self.beats = [(0, 0.0), (0, 0.0)]

    def get(self):
        if self.buf is None and WIN and time.time() - self.tried > 2:
            self.tried = time.time()
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.OpenFileMappingW.restype = W.HANDLE
            k32.OpenFileMappingW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
            k32.MapViewOfFile.restype = ctypes.c_void_p
            k32.MapViewOfFile.argtypes = [W.HANDLE, W.DWORD, W.DWORD, W.DWORD, ctypes.c_size_t]
            for nm in ('Global\\MesaFX', 'Local\\MesaFX'):
                h = k32.OpenFileMappingW(0x0002 | 0x0004, False, nm)
                if h:
                    addr = k32.MapViewOfFile(h, 0x0002 | 0x0004, 0, 0, SHM_SIZE)
                    if addr:
                        buf = (ctypes.c_char * SHM_SIZE).from_address(addr)
                        if struct.unpack_from('<I', buf, 0)[0] == SHM_MAGIC:
                            self.buf, self.name = buf, nm
                            break
        return self.buf

    def i32(self, off, i=0):
        return struct.unpack_from('<i', self.buf, off + 4 * i)[0]

    def set_i32(self, off, i, v):
        struct.pack_into('<i', self.buf, off + 4 * i, v)

    def alive(self, pi):
        """O plugin dessa cadeia processou áudio no último segundo."""
        if not self.get():
            return False
        b = self.i32(H_BEAT, pi)
        last, t = self.beats[pi]
        now = time.time()
        if b != last:            # mudou desde a última olhada (na primeira não dá para saber)
            self.beats[pi] = (b, now)
            return t > 0
        return t > 0 and now - t < 1

    def push_layout(self, pipe):
        """Cadeia nova no banco livre; o plugin troca no bloco seguinte e confirma em ack[]."""
        if not self.get():
            return True
        pi = PIPE_I[pipe]
        data = chain_bytes(pipe)
        cur = chain_off(pi, self.i32(H_BANK, pi) & 1)
        if bytes(self.buf[cur:cur + CHAIN_SIZE]) == data:
            return True          # esta cadeia não mudou: nada de remontar (os efeitos perderiam o estado)
        live = self.alive(pi)
        end = time.time() + (1 if live else 0)
        while self.i32(H_ACK, pi) != self.i32(H_GEN, pi) and time.time() < end:
            time.sleep(0.01)
        b = 1 - (self.i32(H_BANK, pi) & 1)
        off = chain_off(pi, b)
        self.buf[off:off + CHAIN_SIZE] = data
        self.set_i32(H_BANK, pi, b)
        g = self.i32(H_GEN, pi) + 1
        self.set_i32(H_GEN, pi, g)
        if not live:
            return True
        end = time.time() + 3
        while time.time() < end:
            if self.i32(H_ACK, pi) == g:
                return True
            time.sleep(0.01)
        return False

    def set_ctl(self, pipe, i, prop, value):
        if not self.get():
            return
        pi = PIPE_I[pipe]
        b = self.i32(H_BANK, pi) & 1
        off = chain_off(pi, b)
        if i >= self.i32(off):
            return
        kind = self.i32(off + 8 + i * FX_SIZE)
        names = next((n for k, n in CTLS.values() if k == kind), [])
        if prop in names:
            struct.pack_into('<f', self.buf, off + 8 + i * FX_SIZE + 4 + 4 * names.index(prop), float(value))

    def level(self, slot, which):
        if not self.get() or not 0 <= slot < 64:
            return None
        ms = struct.unpack_from('4f', self.buf, LEVELS_OFF + 8 + 16 * slot)
        o = 0 if which == 'in' else 2
        return [round(10 * math.log10(v), 1) if v > 1e-9 else -90.0 for v in ms[o:o + 2]]

    def pipe_level(self, pi, which):
        if not self.get():
            return None
        ms = struct.unpack_from('4f', self.buf, H_PMS + 16 * pi)
        o = 0 if which == 'in' else 2
        return [round(10 * math.log10(v), 1) if v > 1e-9 else -90.0 for v in ms[o:o + 2]]

    def info(self, pi):
        if not self.get():
            return {}
        return {'rate': struct.unpack_from('<f', self.buf, H_RATE + 4 * pi)[0],
                'us': struct.unpack_from('<f', self.buf, H_US + 4 * pi)[0],
                'chans': struct.unpack_from('<f', self.buf, H_CHANS + 4 * pi)[0]}

SHM = Shm()

# ---------------------------------------------------------------- Equalizer APO

APO_PRE, APO_POST = 'eacd2258-fcac-4ff4-b36d-419e924a6d79', 'ec1cc9ce-faed-4822-828a-82a81a6f018f'
MMDEV = r'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio'

def reg_read(path, name, root=None):
    try:
        with winreg.OpenKey(root or winreg.HKEY_LOCAL_MACHINE, path, 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None

def eapo_dir():
    if not WIN:
        return None
    p = reg_read(r'SOFTWARE\EqualizerAPO', 'ConfigPath')
    if p and os.path.isdir(p):
        return p
    p = os.path.join(os.environ.get('ProgramW6432') or r'C:\Program Files', 'EqualizerAPO', 'config')
    return p if os.path.isdir(p) else None

def dev_guid(dev_id):
    m = re.search(r'\{[0-9a-fA-F-]{36}\}$', dev_id or '')
    return m[0].lower() if m else None

def eapo_on(dev_id, flow):
    """O Equalizer APO está instalado neste dispositivo (escolhido no Configurator dele)."""
    g = dev_guid(dev_id)
    if not g or not WIN:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf'{MMDEV}\{"Capture" if flow else "Render"}\{g}\FxProperties',
                            0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            i = 0
            while True:
                try:
                    v = winreg.EnumValue(k, i)[1]
                except OSError:
                    return False
                if isinstance(v, str) and (APO_PRE in v.lower() or APO_POST in v.lower()):
                    return True
                i += 1
    except OSError:
        return False

def resolve_device(pipe):
    with M.STORE_LOCK:
        d = M.STORE[pipe]['device']
    return d or AUDIO.default(1 if pipe == 'input' else 0)

def conf_text():
    # só ASCII: o Equalizer APO lê cada linha como UTF-8 e, se falhar, na página de código do Windows
    lines = ['# Gerado pela Mesa - nao edite: e refeito a cada mudanca (o estado fica em %APPDATA%\\Mesa\\cadeia.json)']
    for pipe, stage in (('input', 'capture'), ('output', 'post-mix')):
        with M.STORE_LOCK:
            if pipe == 'output' and not M.STORE[pipe]['plugins']:
                continue
        g = dev_guid(resolve_device(pipe))
        if not g:
            continue
        lines += [f'Stage: {stage}', f'Device: {g}',
                  f'VSTPlugin: Library "{DLL}" Pipe {PIPE_I[pipe]}', 'Device: all']
    return '\n'.join(lines) + '\n'

CONF_ERR = ['']

def write_conf():
    """mesa.txt (dispositivos) e cadeia.bin; devolve True se o mesa.txt mudou (o Equalizer APO recarrega)."""
    try:
        write_bin()
    except OSError as e:
        CONF_ERR[0] = f'Não deu para gravar {BIN_FILE}: {e.strerror}'
        return False
    d = eapo_dir()
    if not d:
        CONF_ERR[0] = 'O Equalizer APO não está instalado.'
        return False
    path = os.path.join(d, 'mesa.txt')
    txt = conf_text()
    try:
        if open(path, encoding='utf-8').read() == txt:
            CONF_ERR[0] = ''
            return False
    except OSError:
        pass
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(txt)
        CONF_ERR[0] = ''
        return True
    except OSError as e:
        CONF_ERR[0] = f'Não deu para gravar {path}: {e.strerror} (reinstale a Mesa)'
        return False

def enable_apo(devs):
    """Liga o Equalizer APO nos dispositivos (mesa_apo.py como administrador: o Windows pede permissão)."""
    class SEI(ctypes.Structure):
        _fields_ = [('cb', W.DWORD), ('mask', ctypes.c_ulong), ('hwnd', W.HWND), ('verb', W.LPCWSTR),
                    ('file', W.LPCWSTR), ('params', W.LPCWSTR), ('dir', W.LPCWSTR), ('show', ctypes.c_int),
                    ('inst', W.HINSTANCE), ('idlist', ctypes.c_void_p), ('cls', W.LPCWSTR), ('hkey', W.HKEY),
                    ('hotkey', W.DWORD), ('icon', W.HANDLE), ('proc', W.HANDLE)]
    args = ' '.join(f'--{"entrada" if flow else "saida"} "{d}"' for d, flow in devs)
    py = os.path.join(os.path.dirname(sys.executable), 'python.exe')
    sei = SEI(ctypes.sizeof(SEI), 0x40, None, 'runas', py, f'"{os.path.join(HERE, "mesa_apo.py")}" instalar {args}',
              HERE, 0)
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.proc:
        raise RuntimeError('Sem permissão de administrador: o Equalizer APO não foi ligado nesse dispositivo.')
    ctypes.windll.kernel32.WaitForSingleObject(sei.proc, 120000)
    ctypes.windll.kernel32.CloseHandle(sei.proc)

def restart_engine():
    # escolheu um dispositivo sem o Equalizer APO: liga nele (só ao aplicar uma escolha, nunca sozinho)
    if WIN and M.APPLY['busy'] and eapo_dir():
        need = [(d, 1 if pipe == 'input' else 0) for pipe in PIPE_I
                for d in [resolve_device(pipe)] if d and (pipe == 'input' or M.STORE[pipe]['plugins'])
                and not eapo_on(d, 1 if pipe == 'input' else 0)]
        if need:
            M.APPLY['msg'] = 'Ligando o Equalizer APO no dispositivo (o Windows pede permissão)'
            enable_apo(need)
    if retorno_running() and RET['dev'] != (resolve_device('input') or '-'):   # trocou o microfone
        retorno_stop()
        retorno_start()
    if ESC['dev'] and ESC['dev'] != (resolve_device('input') or '-'):
        escuta_start()
    for pipe in PIPE_I:
        if not SHM.push_layout(pipe):
            raise RuntimeError('O plugin não trocou a cadeia (o Equalizer APO está processando este dispositivo?)')

def install_engine():
    if not os.path.exists(M.STORE_FILE):
        M.store_save()
    write_conf()
    return False

# ---------------------------------------------------------------- retorno do mic (mesa_retorno.exe)

RET = {'dev': None}
RET_EVENT = 'Local\\MesaRetornoParar'   # criado pelo mesa_retorno.exe enquanto ele roda
ESC_EVENT = 'Local\\MesaEscutaParar'    # o mesmo programa em modo "só escuta"
ESC = {'dev': None}

def _ret_event(access, name=RET_EVENT):
    if not WIN:
        return None
    k32 = ctypes.windll.kernel32
    k32.OpenEventW.restype = W.HANDLE
    k32.OpenEventW.argtypes = [W.DWORD, W.BOOL, W.LPCWSTR]
    return k32.OpenEventW(access, False, name)

def escuta_start():
    """Microfone aberto (som descartado) enquanto a Mesa está aberta: no Windows os efeitos de captura só
    rodam com alguém gravando; assim a cadeia e os medidores ficam vivos como no Linux. O programa fecha
    sozinho quando a Mesa fecha (vigia o PID dela)."""
    if not WIN or not os.path.exists(RETORNO_EXE):
        return
    escuta_stop()
    ESC['dev'] = resolve_device('input') or '-'
    subprocess.Popen([RETORNO_EXE, ESC['dev'], '0', ESC_EVENT, str(os.getpid())],
                     creationflags=0x08000000 | 0x00000008 | 0x00000200)

def escuta_stop():
    h = _ret_event(0x0002, ESC_EVENT)
    if h:
        ctypes.windll.kernel32.SetEvent(W.HANDLE(h))
        ctypes.windll.kernel32.CloseHandle(W.HANDLE(h))
        for _ in range(40):
            g = _ret_event(0x00100000, ESC_EVENT)
            if not g:
                break
            ctypes.windll.kernel32.CloseHandle(W.HANDLE(g))
            time.sleep(0.05)

def retorno_running():
    h = _ret_event(0x00100000)   # SYNCHRONIZE
    if h:
        ctypes.windll.kernel32.CloseHandle(W.HANDLE(h))
    return bool(h)

def retorno_sink():
    try:
        return open(M.RETORNO_SINK, encoding='utf-8').read().strip() or None
    except OSError:
        return None

def retorno_start():
    RET['dev'] = resolve_device('input') or '-'
    subprocess.Popen([RETORNO_EXE, RET['dev'], retorno_sink() or '-'],
                     creationflags=0x08000000 | 0x00000008 | 0x00000200)   # sem janela, independente da Mesa
    for _ in range(20):
        if retorno_running():
            return
        time.sleep(0.05)

def retorno_stop():
    h = _ret_event(0x0002)   # EVENT_MODIFY_STATE
    if not h:
        return
    ctypes.windll.kernel32.SetEvent(W.HANDLE(h))
    ctypes.windll.kernel32.CloseHandle(W.HANDLE(h))
    for _ in range(40):
        if not retorno_running():
            return
        time.sleep(0.05)

# ---------------------------------------------------------------- ajustes ao vivo

def live_loop():
    while True:
        M.LIVE_EV.wait(0.5)
        if M.LIVE_EV.is_set():
            M.LIVE_EV.clear()
            with M.LIVE_LOCK:
                batch = dict(M.LIVE)
                M.LIVE.clear()
            for pipe, ctls in batch.items():
                for key, v in ctls.items():
                    fx, prop = key.split(':', 1)
                    SHM.set_ctl(pipe, int(fx[2:]), prop, v)
            M.SAVE_AT[0] = time.time() + 1
        if M.SAVE_AT[0] and time.time() > M.SAVE_AT[0]:
            M.SAVE_AT[0] = 0
            try:
                M.store_save()
                write_bin()
            except OSError:
                pass

def watch_loop():
    """Sem dispositivo escolhido a cadeia segue o padrão do Windows: refaz o mesa.txt quando ele muda."""
    while True:
        time.sleep(2)
        if not M.APPLY['busy']:
            try:
                write_conf()
            except Exception:  # noqa: BLE001
                pass

# ---------------------------------------------------------------- áudio do Windows (COM via ctypes)

if WIN:
    class GUID(ctypes.Structure):
        _fields_ = [('a', ctypes.c_ulong), ('b', ctypes.c_ushort), ('c', ctypes.c_ushort), ('d', ctypes.c_ubyte * 8)]

    def G(s):
        g = GUID()
        ctypes.oledll.ole32.CLSIDFromString(ctypes.c_wchar_p(s), ctypes.byref(g))
        return g

    class PROPKEY(ctypes.Structure):
        _fields_ = [('fmtid', GUID), ('pid', ctypes.c_ulong)]

    class PROPVARIANT(ctypes.Structure):
        _fields_ = [('vt', ctypes.c_ushort), ('r1', ctypes.c_ushort), ('r2', ctypes.c_ushort), ('r3', ctypes.c_ushort),
                    ('ptr', ctypes.c_void_p), ('pad', ctypes.c_void_p)]

    CLSID_Enum = G('{BCDE0395-E52F-467C-8E3D-C4579291692E}')
    IID_Enum = G('{A95664D2-9614-4F35-A746-DE8DB63617E6}')
    IID_Vol = G('{5CDF2C82-841E-4546-9722-0CF74078229A}')
    IID_Meter = G('{C02216F6-8C67-4B5B-9D00-D008E73E0064}')
    IID_SessMgr2 = G('{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}')
    IID_Ctl2 = G('{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}')
    IID_Simple = G('{87CE5498-68D6-44E5-9215-6DA47EF883D8}')
    PKEY_Name = PROPKEY(G('{A45C254E-DF1C-4EFD-8020-67D146A850E0}'), 14)

def com(p, i, *args, types=(), res=None):
    """Chama o método i da tabela virtual do objeto COM p."""
    vt = ctypes.cast(p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    f = ctypes.WINFUNCTYPE(res or ctypes.HRESULT, ctypes.c_void_p, *types)(vt[i])
    return f(p, *args)

def release(p):
    if p:
        com(p, 2, res=ctypes.c_ulong)

def out_ptr():
    return ctypes.c_void_p()

_tls = threading.local()

def coinit():
    if not getattr(_tls, 'ok', False):
        ctypes.windll.ole32.CoInitializeEx(None, 0)   # MTA: os objetos servem em qualquer thread
        _tls.ok = True

def wstr_free(p):
    s = ctypes.wstring_at(p) if p else ''
    if p:
        ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(p))
    return s

class Audio:
    """Dispositivos (volume, mudo, pico), sessões de apps por dispositivo e quem grava de cada microfone."""
    def __init__(self):
        self.lock = threading.RLock()
        self.enum = None
        self.devs = {}        # id -> {'flow','name','dev','vol','meter'}
        self.sess = {}        # n -> {'key','name','dev','flow','simple','meter','pid'}
        self.sess_key = {}    # identificador da instância -> n
        self.next_n = 1
        self.t = 0

    def _enum(self):
        coinit()
        if self.enum is None:
            p = out_ptr()
            ctypes.oledll.ole32.CoCreateInstance(ctypes.byref(CLSID_Enum), None, 1 | 4, ctypes.byref(IID_Enum), ctypes.byref(p))
            self.enum = p.value
        return self.enum

    def _activate(self, dev, iid):
        p = out_ptr()
        com(dev, 3, ctypes.byref(iid), 1 | 4, None, ctypes.byref(p),
            types=(ctypes.c_void_p, W.DWORD, ctypes.c_void_p, ctypes.c_void_p))
        return p.value

    def _name(self, dev):
        store = out_ptr()
        com(dev, 4, 0, ctypes.byref(store), types=(W.DWORD, ctypes.c_void_p))
        pv = PROPVARIANT()
        try:
            com(store.value, 5, ctypes.byref(PKEY_Name), ctypes.byref(pv), types=(ctypes.c_void_p, ctypes.c_void_p))
            return ctypes.wstring_at(pv.ptr) if pv.vt == 31 and pv.ptr else '?'
        finally:
            ctypes.windll.ole32.PropVariantClear(ctypes.byref(pv))
            release(store.value)

    def refresh(self, force=False):
        if not WIN:
            return
        with self.lock:
            if not force and time.time() - self.t < 1:
                return
            self.t = time.time()
            e = self._enum()
            seen = set()
            for flow in (0, 1):
                col = out_ptr()
                com(e, 3, flow, 1, ctypes.byref(col), types=(ctypes.c_int, W.DWORD, ctypes.c_void_p))
                n = W.UINT()
                com(col.value, 3, ctypes.byref(n), types=(ctypes.c_void_p,))
                for i in range(n.value):
                    dev = out_ptr()
                    com(col.value, 4, i, ctypes.byref(dev), types=(W.UINT, ctypes.c_void_p))
                    pid = ctypes.c_void_p()
                    com(dev.value, 5, ctypes.byref(pid), types=(ctypes.c_void_p,))
                    did = wstr_free(pid.value)
                    seen.add(did)
                    if did in self.devs:
                        release(dev.value)
                        continue
                    try:
                        self.devs[did] = {'flow': flow, 'name': self._name(dev.value), 'dev': dev.value,
                                          'vol': self._activate(dev.value, IID_Vol),
                                          'meter': self._activate(dev.value, IID_Meter), 'mgr': None}
                        self.devs[did]['mgr'] = self._activate(dev.value, IID_SessMgr2)
                    except OSError:
                        pass
                release(col.value)
            for did in [d for d in self.devs if d not in seen]:
                d = self.devs.pop(did)
                for k in ('mgr', 'meter', 'vol', 'dev'):
                    release(d.get(k))
            self._sessions()

    def _sessions(self):
        k32 = ctypes.windll.kernel32
        alive = set()
        for did, d in self.devs.items():
            if not d.get('mgr'):
                continue
            en = out_ptr()
            try:
                com(d['mgr'], 5, ctypes.byref(en), types=(ctypes.c_void_p,))
            except OSError:
                continue
            cnt = ctypes.c_int()
            com(en.value, 3, ctypes.byref(cnt), types=(ctypes.c_void_p,))
            for i in range(cnt.value):
                ctl = out_ptr()
                com(en.value, 4, i, ctypes.byref(ctl), types=(ctypes.c_int, ctypes.c_void_p))
                c2 = out_ptr()
                try:
                    com(ctl.value, 0, ctypes.byref(IID_Ctl2), ctypes.byref(c2), types=(ctypes.c_void_p, ctypes.c_void_p))
                    state = ctypes.c_int()
                    com(c2.value, 3, ctypes.byref(state), types=(ctypes.c_void_p,))
                    if state.value == 2 or com(c2.value, 15) == 0:     # expirada / sons do sistema
                        continue
                    sid = ctypes.c_void_p()
                    com(c2.value, 13, ctypes.byref(sid), types=(ctypes.c_void_p,))
                    key = did + '|' + wstr_free(sid.value)
                    pid = W.DWORD()
                    com(c2.value, 14, ctypes.byref(pid), types=(ctypes.c_void_p,))
                    if d['flow'] == 1 and state.value != 1:   # quem grava: só quem está gravando agora
                        continue
                    alive.add(key)
                    if key in self.sess_key:
                        continue
                    simple, meter = out_ptr(), out_ptr()
                    com(ctl.value, 0, ctypes.byref(IID_Simple), ctypes.byref(simple), types=(ctypes.c_void_p, ctypes.c_void_p))
                    com(ctl.value, 0, ctypes.byref(IID_Meter), ctypes.byref(meter), types=(ctypes.c_void_p, ctypes.c_void_p))
                    n = self.next_n
                    self.next_n += 1
                    self.sess_key[key] = n
                    self.sess[n] = {'key': key, 'dev': did, 'flow': d['flow'], 'pid': pid.value,
                                    'name': proc_name(k32, pid.value), 'simple': simple.value, 'meter': meter.value}
                except OSError:
                    pass
                finally:
                    release(c2.value)
                    release(ctl.value)
            release(en.value)
        for key in [k for k in self.sess_key if k not in alive]:
            s = self.sess.pop(self.sess_key.pop(key))
            release(s['simple'])
            release(s['meter'])

    # ---- leitura
    def default(self, flow):
        if not WIN:
            return None
        with self.lock:
            dev = out_ptr()
            try:
                com(self._enum(), 4, flow, 0, ctypes.byref(dev), types=(ctypes.c_int, ctypes.c_int, ctypes.c_void_p))
            except OSError:
                return None
            pid = ctypes.c_void_p()
            com(dev.value, 5, ctypes.byref(pid), types=(ctypes.c_void_p,))
            release(dev.value)
            return wstr_free(pid.value)

    def volume(self, d):
        v, m = ctypes.c_float(), W.BOOL()
        try:
            com(d['vol'], 9, ctypes.byref(v), types=(ctypes.c_void_p,))
            com(d['vol'], 15, ctypes.byref(m), types=(ctypes.c_void_p,))
        except OSError:
            return 0, False
        return round(v.value * 100), bool(m.value)

    def sess_volume(self, s):
        v, m = ctypes.c_float(), W.BOOL()
        try:
            com(s['simple'], 4, ctypes.byref(v), types=(ctypes.c_void_p,))
            com(s['simple'], 6, ctypes.byref(m), types=(ctypes.c_void_p,))
        except OSError:
            return 0, False
        return round(v.value * 100), bool(m.value)

    def peak(self, meter):
        coinit()
        n = W.UINT()
        try:
            com(meter, 4, ctypes.byref(n), types=(ctypes.c_void_p,))
            k = max(1, min(2, n.value))
            arr = (ctypes.c_float * k)()
            com(meter, 5, k, arr, types=(W.UINT, ctypes.c_void_p))
        except OSError:
            return None
        vals = list(arr) * (2 // k)
        return [round(20 * math.log10(v), 1) if v > 3e-5 else -90.0 for v in vals[:2]]

    # ---- escrita
    def set_volume(self, kind, ident, volume=None, mute=None):
        coinit()
        with self.lock:
            if kind in ('sink', 'source'):
                d = self.devs.get(ident)
                if not d:
                    return False
                if volume is not None:
                    com(d['vol'], 7, ctypes.c_float(max(0, min(100, volume)) / 100), None,
                        types=(ctypes.c_float, ctypes.c_void_p))
                if mute is not None:
                    com(d['vol'], 14, bool(mute), None, types=(W.BOOL, ctypes.c_void_p))
                return True
            for n in ident.split('+'):
                s = self.sess.get(int(n)) if n.isdigit() else None
                if not s:
                    continue
                if volume is not None:
                    com(s['simple'], 3, ctypes.c_float(max(0, min(100, volume)) / 100), None,
                        types=(ctypes.c_float, ctypes.c_void_p))
                if mute is not None:
                    com(s['simple'], 5, bool(mute), None, types=(W.BOOL, ctypes.c_void_p))
            return True

def proc_name(k32, pid):
    if pid == 0:
        return 'Sistema'
    h = k32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return f'Processo {pid}'
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = W.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return os.path.splitext(os.path.basename(buf.value))[0]
        return f'Processo {pid}'
    finally:
        k32.CloseHandle(h)

AUDIO = Audio()

# ---------------------------------------------------------------- estado para a interface

def mixer_state():
    coinit() if WIN else None
    AUDIO.refresh()
    with AUDIO.lock:
        devs = dict(AUDIO.devs)
        sess = dict(AUDIO.sess)
        mics, masters = [], []
        for did, d in devs.items():
            vol, mute = AUDIO.volume(d)
            name = M.custom_name(did) or d['name']
            item = {'kind': 'source' if d['flow'] else 'sink', 'id': did, 'name': name, 'rkey': did,
                    'volume': vol, 'mute': mute, 'meter': f'{"src" if d["flow"] else "sink"}:{did}'}
            (mics if d['flow'] else masters).append(item)
        apps, by_name, recorders = [], {}, []
        ret = s_ret = None
        for n, s in sorted(sess.items()):
            if s['name'].lower() == 'mesa_retorno':     # o retorno aparece como quem grava, não como app tocando
                if s['flow'] == 1:
                    ret = s
                else:
                    s_ret = s['dev']
                continue
            if s['flow'] == 1:
                recorders.append({'name': s['name'], 'source': s['dev'], 'to': None})
                continue
            vol, mute = AUDIO.sess_volume(s)
            k = (s['name'].lower(), s['dev'])
            a = by_name.get(k)
            if a:
                a['ids'].append(n)
                a['volume'] = max(a['volume'], vol)
                a['mute'] = a['mute'] and mute
                continue
            a = by_name[k] = {'kind': 'sink-input', 'ids': [n], 'name': M.custom_name('app:' + s['name'].lower()) or s['name'],
                              'rkey': 'app:' + s['name'].lower(), 'volume': vol, 'mute': mute, 'target': s['dev']}
            apps.append(a)
    if ret:
        recorders.append({'name': 'Retorno do mic', 'source': ret['dev'], 'to': s_ret})
    for a in apps:
        a['id'] = '+'.join(map(str, a.pop('ids')))
        a['meter'] = 'app:' + a['id']
    return {'apps': apps, 'masters': masters, 'mics': mics, 'recorders': recorders, 'ee_source': None,
            'sink_names': {m['id']: m['name'] for m in masters}, 'source_names': {m['id']: m['name'] for m in mics},
            'default_sink': AUDIO.default(0), 'default_source': AUDIO.default(1)}

def full_state(pipe):
    mix = mixer_state()
    M.LAST_MIX = mix
    names = {s['id'] for s in mix['mics']} | {s['id'] for s in mix['masters']}
    with M.STORE_LOCK:
        cfg = {p: {'plugins': list(M.STORE[p]['plugins']), 'device': M.STORE[p]['device']} for p in PIPE_I}
    busy = M.APPLY['busy']
    # a cadeia salva aparece mesmo reiniciando (como no Linux): /api/apply manda a lista inteira
    chain = [M.plugin_state(pipe, p, i) for i, p in enumerate(cfg[pipe]['plugins'])]
    devs = {'input': cfg['input']['device'] or mix['default_source'],
            'output': cfg['output']['device'] or mix['default_sink']}
    dev = devs[pipe]
    flow_i = 1 if pipe == 'input' else 0
    lst = mix['mics'] if pipe == 'input' else mix['masters']
    devices = [{'id': m['id'], 'name': m['name'] + ('' if eapo_on(m['id'], flow_i) else ' (sem Equalizer APO)')}
               for m in lst]
    if pipe == 'input':
        flow = {'from': [{'name': mix['source_names'].get(dev, dev or '?'), 'kind': 'mic', 'device': dev}],
                'to': [{'name': r['name'], 'kind': 'app', 'to': r['to']} for r in mix['recorders'] if r['source'] == dev],
                'bus': 'Microfone com efeitos'}
    else:
        flow = {'from': [{'name': a['name'], 'kind': 'app'} for a in mix['apps'] if a['target'] == dev],
                'to': [{'name': mix['sink_names'].get(dev, dev or '?'), 'kind': 'speaker', 'device': dev}],
                'bus': 'Saída com efeitos'}
    mix['ee_devices'] = devs
    pi = PIPE_I[pipe]
    alive = SHM.alive(pi)
    on = eapo_on(dev, flow_i)
    if not eapo_dir():
        problem = 'O Equalizer APO não está instalado. Instale a Mesa de novo (o instalador traz ele).'
    elif dev and not on and (pipe == 'input' or cfg['output']['plugins']):
        problem = (f'O Equalizer APO não está instalado em “{mix["source_names" if pipe == "input" else "sink_names"].get(dev, dev)}”. '
                   'Feche e abra a Mesa, ou mude algo na cadeia, para ligar (o Windows pede permissão).')
    else:
        problem = CONF_ERR[0]
    if alive:
        chip = 'efeitos ligados'
    elif problem:
        chip = 'efeitos desligados'
    else:
        chip = 'efeitos prontos · aguardando áudio' if pipe == 'input' else 'efeitos prontos'
    apply = dict(M.APPLY)
    if problem and not apply.get('error'):
        apply['error'] = problem
    return {'pipe': pipe, 'ee_running': not busy, 'chain': chain, 'flow': flow, 'mix': mix,
            'device': dev, 'devices': devices, 'catalog': M.CATALOG, 'apply': apply, 'missing': dev not in names,
            'monitor': retorno_running(), 'monitor_sink': retorno_sink() or mix['default_sink'], 'has_retorno': True, 'fps': M.STATS['fps'],
            'version': M.VERSION, 'ee_alive': False, 'brand': 'EQUALIZER APO · EFEITOS PRÓPRIOS',
            'chip': chip, 'chip_on': alive, 'engine': SHM.info(pi), 'problem': problem,
            'engine_hint': 'Os efeitos rodam no Equalizer APO (Configurator → dispositivos).'}

def meter_value(sk):
    kind, _, arg = sk.partition(':')
    if kind == 'lvl':
        m = re.fullmatch(r'(\d+):(in|out)', arg)
        return SHM.level(int(m[1]), m[2]) if m else None
    if not WIN:
        return None
    with AUDIO.lock:     # a atualização da lista libera os objetos de quem sumiu
        if kind in ('src', 'sink'):
            d = AUDIO.devs.get(arg)
            return AUDIO.peak(d['meter']) if d and d.get('meter') else None
        if kind == 'app':
            s = AUDIO.sess.get(int(arg)) if arg.isdigit() else None
            return AUDIO.peak(s['meter']) if s and s.get('meter') else None
    return None

# ---------------------------------------------------------------- HTTP (o mesmo do Linux, com volume do Windows)

class WinH(M.H):
    def do_POST(self):
        if self.path != '/api/vol':     # /api/monitor é o do Linux, com o retorno trocado acima
            return super().do_POST()
        n = int(self.headers.get('Content-Length', 0))
        try:
            d = json.loads(self.rfile.read(n) or b'{}')
        except json.JSONDecodeError:
            return self.send(400, {'erro': 'json'})
        kind, ident = d.get('kind'), str(d.get('id'))
        if kind not in ('sink', 'source', 'sink-input'):
            return self.send(400, {'erro': 'parametro'})
        ok = AUDIO.set_volume(kind, ident, int(d['volume']) if 'volume' in d else None,
                              d['mute'] if 'mute' in d else None)
        return self.send(200 if ok else 400, {'ok': ok})

# ---------------------------------------------------------------- janela

def find_browser():
    roots = [os.environ.get(k) for k in ('ProgramFiles(x86)', 'ProgramW6432', 'ProgramFiles', 'LOCALAPPDATA')]
    for r in filter(None, roots):
        for rel in (r'Microsoft\Edge\Application\msedge.exe', r'Google\Chrome\Application\chrome.exe'):
            p = os.path.join(r, rel)
            if os.path.exists(p):
                return p
    return None

def open_window(url):
    b = find_browser()
    if not b:
        os.startfile(url)
        return
    perfil = os.path.join(LOCAL, 'Mesa', 'navegador')
    subprocess.Popen([b, f'--app={url}', f'--user-data-dir={perfil}', '--no-first-run', '--no-default-browser-check',
                      '--start-maximized'], creationflags=0x00000008 | 0x00000200)   # DETACHED, NEW_PROCESS_GROUP

def patch():
    """Troca no mesa.py só o que é do PipeWire; o resto (estado, efeitos, HTTP) é o mesmo."""
    for k, v in {'write_conf': write_conf, 'restart_engine': restart_engine, 'install_engine': install_engine,
                 'live_loop': live_loop, 'mixer_state': mixer_state, 'full_state': full_state,
                 'meter_value': meter_value, 'ensure_meters': lambda keys: None, 'stop_meters': lambda: None,
                 'retorno_pids': lambda: [1] if retorno_running() else [], 'retorno_sink': retorno_sink,
                 'retorno_start': retorno_start, 'retorno_stop': retorno_stop, 'ee_alive': lambda: False}.items():
        setattr(M, k, v)

def main():
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    if sys.stderr is None or not sys.stderr.isatty():
        sys.stderr = open(LOG, 'a', encoding='utf-8', buffering=1)
    patch()
    CTLS.update(load_ctls())
    M.STORE = M.store_load()
    try:
        M.store_save()    # grava já as conversões feitas na carga (ex.: desenho novo do equalizador)
    except OSError:
        pass
    try:
        install_engine()
        restart_engine()
    except Exception as e:  # noqa: BLE001
        M.APPLY['error'] = f'Não deu para ligar a cadeia de efeitos: {e}'
    threading.Thread(target=live_loop, daemon=True).start()
    threading.Thread(target=watch_loop, daemon=True).start()
    # dispositivo da cadeia sem o Equalizer APO (ex.: o padrão do Windows mudou): liga ao abrir a Mesa
    if WIN and eapo_dir() and any(not eapo_on(resolve_device(p), 1 if p == 'input' else 0)
                                  for p in PIPE_I if resolve_device(p) and (p == 'input' or M.STORE[p]['plugins'])):
        M.apply_change('Ligando o Equalizer APO no dispositivo', lambda: None)
    srv = M.ThreadingHTTPServer(('127.0.0.1', M.PORT), WinH)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f'http://127.0.0.1:{M.PORT}/'
    escuta_start()
    if '--sem-janela' in sys.argv:
        print(url, flush=True)
        while True:
            time.sleep(3600)
    open_window(url)
    # a Mesa fecha quando a janela some: sem contato por 8 s
    M.STATS['seen'] = time.time() + 30
    while time.time() - M.STATS['seen'] < 8:
        time.sleep(1)
    escuta_stop()
    srv.shutdown()

if __name__ == '__main__':
    try:
        socket.create_connection(('127.0.0.1', M.PORT), timeout=0.3).close()
        if '--sem-janela' not in sys.argv:
            open_window(f'http://127.0.0.1:{M.PORT}/')
        sys.exit(0)
    except OSError:
        pass
    main()
