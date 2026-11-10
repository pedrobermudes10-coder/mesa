#!/usr/bin/env python3
# Liga o Equalizer APO num dispositivo de áudio sem abrir o Device Selector dele (precisa de administrador).
# Faz no registro exatamente o que o DeviceAPOInfo::install() do Equalizer APO 1.4.2 faz com as opções
# padrão (pré-mix; pós-mix só em saídas; APO original da placa mantido como "filho"), reinicia o serviço de
# áudio e testa como o Device Selector: abre o dispositivo e espera o EqualizerAPO.dll avisar pelo pipe
# \\.\pipe\EqualizerAPODeviceTest que carregou; se não carregar, tenta o próximo modo de instalação
# (SFX/EFX → SFX/MFX → LFX/GFX). No laptop os alto-falantes Realtek só funcionaram em SFX/MFX.
#   python mesa_apo.py instalar [--entrada ID|padrao] [--saida ID|padrao]   (ID = id do MMDevice ou {guid})
#   python mesa_apo.py simular {guid}       mostra o que seria escrito (compara com a instalação atual)
# Desligar (desinstalação da Mesa): DeviceSelector.exe /u, do próprio Equalizer APO.
import re, subprocess, sys, winreg

MMDEV = r'SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices\Audio'
CHILD = r'SOFTWARE\EqualizerAPO\Child APOs'
LFX, GFX, SFX, MFX, EFX = (f'{{d04e05a6-594b-4fb6-a80d-01af5eed7d1d}},{n}' for n in (1, 2, 5, 6, 7))
MULTI = [f'{{d04e05a6-594b-4fb6-a80d-01af5eed7d1d}},{n}' for n in (13, 14, 15)]
ALL = [LFX, GFX, SFX, MFX, EFX]
FX_TITLE = '{b725f130-47ef-101a-a5f1-02608c9eebac},10'
MODES = {SFX: '{d3993a3f-99c2-4402-b5ec-a92a0367664b},5', MFX: '{d3993a3f-99c2-4402-b5ec-a92a0367664b},6',
         EFX: '{d3993a3f-99c2-4402-b5ec-a92a0367664b},7'}
DEFAULT_MODE = '{C18E2F7E-933D-4965-B7D1-1EEF228D2AF3}'
DISABLE_ENH = '{1da5d803-d492-4edd-8c23-e0c0ffee7f0e},5'
COMBINED = '{b3f8fa53-0004-438e-9003-51a46e139bfc},41'
PRE, POST = '{EACD2258-FCAC-4FF4-B36D-419E924A6D79}', '{EC1CC9CE-FAED-4822-828A-82A81A6F018F}'
NOKEY, NOVALUE = '!KEY', '!VALUE'
W64 = winreg.KEY_WOW64_64KEY
# nas chaves dos dispositivos o administrador só pode ler e gravar valores (o resto é do SISTEMA)
RW = winreg.KEY_READ | winreg.KEY_SET_VALUE

def key(path, write=False):
    return winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path, 0,
                          (RW if write else winreg.KEY_READ) | W64)

def exists(path):
    try:
        key(path).Close()
        return True
    except OSError:
        return False

def values(path):
    out = {}
    try:
        with key(path) as k:
            i = 0
            while True:
                try:
                    n, v, t = winreg.EnumValue(k, i)
                except OSError:
                    return out
                out[n] = v
                i += 1
    except OSError:
        return None

def guid_of(dev):
    m = re.search(r'\{[0-9a-fA-F-]{36}\}$', dev or '')
    return m[0].lower() if m else None

def device_path(guid):
    for flow, capture in (('Render', False), ('Capture', True)):
        if exists(rf'{MMDEV}\{flow}\{guid}'):
            return rf'{MMDEV}\{flow}\{guid}', capture
    raise SystemExit(f'dispositivo {guid} não existe')

def is_installed(fx):
    return bool(fx) and any(str(fx.get(n, '')).upper() in (PRE, POST) for n in ALL)

def plan(guid, fx, props, capture, mode=None):
    """O que gravar: (valores do Child APOs, valores a escrever no FxProperties, nomes a apagar dele)."""
    # modo (DeviceAPOInfo::load, Windows 8.1+): LFX/GFX só se o driver só tiver esses
    if mode:
        pass
    elif (fx is not None and (LFX in fx or GFX in fx) and not any(n in fx for n in (SFX, MFX, EFX, *MULTI))):
        mode = 'LFX_GFX'
    elif COMBINED in (props or {}):        # bluetooth combinado no Windows 11: EFX não funciona
        mode = 'SFX_MFX'
    else:
        mode = 'SFX_EFX'
    if fx is None:
        orig = {n: NOKEY for n in ALL}
    else:
        orig = {n: (NOVALUE if str(fx[n]).upper() in (PRE, POST) else fx[n]) if n in fx else NOVALUE for n in ALL}

    def original(pre):
        a, b = (LFX, GFX) if mode == 'LFX_GFX' else (SFX, MFX) if mode == 'SFX_MFX' else (SFX, EFX)
        g = orig[a if pre else b]
        if mode == 'LFX_GFX':
            if orig[LFX] == NOVALUE and orig[GFX] == NOVALUE:
                g = orig[SFX if pre else MFX]
        elif orig[a] == NOVALUE and orig[b] == NOVALUE:
            g = orig[LFX if pre else GFX]
        return '' if g in (NOKEY, NOVALUE) else g

    child = {n: (NOKEY if fx is None else fx.get(n, NOVALUE)) for n in ALL}
    child.update({'PreMixChild': original(True), 'PostMixChild': '' if capture else original(False),
                  'AllowSilentBufferModification': 'false', 'Version': '2'})
    write, delete = {}, [DISABLE_ENH]
    if fx is None:
        write[FX_TITLE] = 'Equalizer APO'
    if mode == 'LFX_GFX':
        write[LFX] = PRE
        if not capture:
            write[GFX] = POST
        delete += [SFX, MFX, EFX]
    else:
        delete += [LFX, GFX]
        write[SFX] = PRE
        if not (fx or {}).get(MODES[SFX]):
            write[MODES[SFX]] = [DEFAULT_MODE]
        if not capture:
            post = MFX if mode == 'SFX_MFX' else EFX
            write[post] = POST
            if not (fx or {}).get(MODES[post]):
                write[MODES[post]] = [DEFAULT_MODE]
    return mode, child, write, [d for d in delete if fx is not None and d in fx and d not in write]

def uninstall(guid):
    """DeviceAPOInfo::uninstall(): devolve ao FxProperties os APOs originais guardados no Child APOs."""
    path, capture = device_path(guid)
    child = values(rf'{CHILD}\{guid}') or {}
    if child.get(LFX) == NOKEY:
        subprocess.run(['reg', 'delete', rf'HKLM\{path}\FxProperties', '/f', '/reg:64'], capture_output=True)
    else:
        with key(path + r'\FxProperties', True) as k:
            for n in ALL:
                o = child.get(n, '')
                if o == NOVALUE:
                    try:
                        winreg.DeleteValue(k, n)
                    except OSError:
                        pass
                elif o:
                    winreg.SetValueEx(k, n, 0, winreg.REG_SZ, o)
    subprocess.run(['reg', 'delete', rf'HKLM\{CHILD}\{guid}', '/f', '/reg:64'], capture_output=True)

def install(dev, mode=None):
    guid = guid_of(dev)
    path, capture = device_path(guid)
    fx = values(path + r'\FxProperties')
    if is_installed(fx):
        if not mode:
            print(f'{guid}: já tem o Equalizer APO')
            return None
        uninstall(guid)     # trocar de modo = desinstalar e instalar de novo, como o reinstall() dele
        fx = values(path + r'\FxProperties')
    mode, child, write, delete = plan(guid, fx, values(path + r'\Properties'), capture, mode)
    winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, CHILD, 0, winreg.KEY_ALL_ACCESS | W64).Close()
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, rf'{CHILD}\{guid}', 0, winreg.KEY_ALL_ACCESS | W64) as k:
        for n, v in child.items():
            winreg.SetValueEx(k, n, 0, winreg.REG_SZ, v)
        try:
            winreg.DeleteValue(k, 'DisableAutomaticAdjustment')
        except OSError:
            pass
    try:
        k = key(path + r'\FxProperties', True) if fx is not None else \
            winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, path + r'\FxProperties', 0, RW | W64)
    except PermissionError:
        raise SystemExit(f'{guid}: sem permissão no registro do dispositivo — use o Device Selector do Equalizer APO')
    with k:
        for n, v in write.items():
            winreg.SetValueEx(k, n, 0, winreg.REG_MULTI_SZ if isinstance(v, list) else winreg.REG_SZ, v)
        for n in delete:
            winreg.DeleteValue(k, n)
    print(f'{guid}: Equalizer APO ligado ({"entrada" if capture else "saída"}, modo {mode})', flush=True)
    return mode

# ---- teste (DeviceTestThread do Device Selector)

def _pipe_server(name, got, stop):
    import ctypes
    from ctypes import wintypes as W
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.CreateNamedPipeW.restype = W.HANDLE
    adv = ctypes.WinDLL('advapi32')
    sd = ctypes.create_string_buffer(64)
    adv.InitializeSecurityDescriptor(sd, 1)
    adv.SetSecurityDescriptorDacl(sd, True, None, False)          # qualquer um escreve (o audiodg)

    class SA(ctypes.Structure):
        _fields_ = [('n', W.DWORD), ('sd', ctypes.c_void_p), ('inh', W.BOOL)]
    sa = SA(ctypes.sizeof(SA), ctypes.addressof(sd), False)
    while not stop.is_set():
        h = k32.CreateNamedPipeW(rf'\\.\pipe\{name}', 1, 4 | 2 | 8, 255, 0, 1024, 0, ctypes.byref(sa))
        if h in (None, W.HANDLE(-1).value):
            return
        if k32.ConnectNamedPipe(W.HANDLE(h), None) or ctypes.get_last_error() == 535:
            buf = ctypes.create_string_buffer(1024)
            n = W.DWORD()
            if k32.ReadFile(W.HANDLE(h), buf, 1024, ctypes.byref(n), None) and n.value:
                msg = buf.raw[:n.value].decode('utf-8', 'replace')
                if msg != 'stop':
                    got.append(msg)
            k32.DisconnectNamedPipe(W.HANDLE(h))
        k32.CloseHandle(W.HANDLE(h))

def _open_stream(guid, capture):
    """testAPOInstallation(): só inicializar um fluxo faz o serviço de áudio carregar os APOs."""
    import ctypes
    from mesa_win import AUDIO, com, out_ptr, coinit, IID_Enum, CLSID_Enum, release
    from ctypes import wintypes as W
    coinit()
    dev = out_ptr()
    did = ('{0.0.1.00000000}.' if capture else '{0.0.0.00000000}.') + guid
    com(AUDIO._enum(), 5, ctypes.c_wchar_p(did), ctypes.byref(dev), types=(ctypes.c_wchar_p, ctypes.c_void_p))
    from mesa_win import G
    client = AUDIO._activate(dev.value, G('{1CB9AD4C-DBFA-4c32-B178-C2F568A703B2}'))
    fmt = ctypes.c_void_p()
    com(client, 8, ctypes.byref(fmt), types=(ctypes.c_void_p,))                       # GetMixFormat
    try:
        com(client, 3, 0, 0, ctypes.c_longlong(1000000), ctypes.c_longlong(0), fmt, None,   # Initialize 100 ms
            types=(ctypes.c_int, W.DWORD, ctypes.c_longlong, ctypes.c_longlong, ctypes.c_void_p, ctypes.c_void_p))
    finally:
        ctypes.windll.ole32.CoTaskMemFree(fmt)
    return client, dev.value

def test(devs):
    """devs: {guid: capture}. Devolve {guid: True/False} (o APO carregou pré-mix e, em saídas, pós-mix)."""
    import json, threading, time
    name = 'EqualizerAPODeviceTest'
    got, stop = [], threading.Event()
    t = threading.Thread(target=_pipe_server, args=(name, got, stop), daemon=True)
    t.start()
    with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\EqualizerAPO', 0, winreg.KEY_ALL_ACCESS | W64) as k:
        winreg.SetValueEx(k, 'DeviceTestPipeName', 0, winreg.REG_SZ, name)
    keep = []
    try:
        for g, cap in devs.items():
            try:
                keep.append(_open_stream(g, cap))
            except OSError as e:
                print(f'{g}: não deu para abrir o dispositivo ({e})')
        ok = {g: False for g in devs}
        end = time.time() + 3
        while time.time() < end and not all(ok.values()):
            time.sleep(0.1)
            seen = {}
            for m in list(got):
                try:
                    j = json.loads(m)
                except ValueError:
                    continue
                if j.get('phase') == 'Initialize':
                    seen.setdefault(str(j.get('deviceGuid', '')).lower(), set()).add(j.get('stage'))
            for g, cap in devs.items():
                st = seen.get(g, set())
                ok[g] = 'PreMix' in st and (cap or 'PostMix' in st)
        return ok
    finally:
        with winreg.CreateKeyEx(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\EqualizerAPO', 0, winreg.KEY_ALL_ACCESS | W64) as k:
            try:
                winreg.DeleteValue(k, 'DeviceTestPipeName')
            except OSError:
                pass
        stop.set()
        import ctypes
        h = ctypes.windll.kernel32.CreateFileW(rf'\\.\pipe\{name}', 0x40000000, 0, None, 3, 0, None)
        if h not in (0, -1):
            ctypes.windll.kernel32.WriteFile(h, b'stop', 4, ctypes.byref(ctypes.c_ulong()), None)
            ctypes.windll.kernel32.CloseHandle(h)
        from mesa_win import release
        for client, dev in keep:
            release(client)
            release(dev)

def install_tested(devs):
    """Liga e testa; quem não carregar passa para o próximo modo (como o ajuste automático do Device Selector)."""
    todo = {}
    for dev in devs:
        mode = install(dev)
        if mode:
            g = guid_of(dev)
            todo[g] = (device_path(g)[1], [m for m in ('SFX_EFX', 'SFX_MFX', 'LFX_GFX') if m != mode])
    while todo:
        restart_audio()
        ok = test({g: cap for g, (cap, _) in todo.items()})
        for g in list(todo):
            cap, rest = todo[g]
            if ok.get(g):
                print(f'{g}: teste ok', flush=True)
                del todo[g]
            elif rest:
                print(f'{g}: não carregou; tentando {rest[0]}', flush=True)
                install(g, rest[0])
                todo[g] = (cap, rest[1:])
            else:
                print(f'{g}: não carregou em nenhum modo (fica no último)', flush=True)
                del todo[g]
    return bool(devs)

def default_device(flow):
    sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
    from mesa_win import AUDIO
    return AUDIO.default(flow)

def restart_audio():
    subprocess.run(['powershell', '-NoProfile', '-Command', 'Restart-Service Audiosrv -Force'],
                   capture_output=True, creationflags=0x08000000)

def simulate(guid):
    """Reconstrói o estado de antes pelo Child APOs e compara o plano com o que o Device Selector gravou."""
    guid = guid.lower()
    path, capture = device_path(guid)
    fx_now = values(path + r'\FxProperties') or {}
    child_now = values(rf'{CHILD}\{guid}')
    if not child_now:
        raise SystemExit('esse dispositivo não tem instalação do Equalizer APO para comparar')
    if all(child_now.get(n) == NOKEY for n in ALL):
        fx_before = None
    else:
        fx_before = {k: v for k, v in fx_now.items() if k not in ALL and k != FX_TITLE}
        fx_before.update({n: child_now[n] for n in ALL if child_now.get(n) not in (NOKEY, NOVALUE)})
    mode, child, write, delete = plan(guid, fx_before, values(path + r'\Properties'), capture)
    print(f'{guid} ({"entrada" if capture else "saída"}): modo calculado {mode}')
    for n, v in child.items():
        print(f'  Child {n} = {v!r}', '' if child_now.get(n, '') == v else f'  <- atual {child_now.get(n)!r}')
    for n, v in write.items():
        cur = fx_now.get(n)
        same = (cur == v) if not isinstance(v, list) else (cur and v[0] in cur)
        print(f'  Fx {n} = {v!r}', '' if same else f'  <- atual {cur!r}')
    for n in ALL:
        if n not in write and n in fx_now:
            print(f'  Fx {n} existe hoje = {fx_now[n]!r} (o plano não escreve)')

if __name__ == '__main__':
    a = sys.argv[1:]
    if a[:1] == ['simular'] and len(a) == 2:
        simulate(a[1])
    elif a[:1] == ['instalar']:
        opts = dict(zip(a[1::2], a[2::2]))
        devs = []
        for opt, flow in (('--entrada', 1), ('--saida', 0)):
            if opt in opts:
                dev = default_device(flow) if opts[opt] == 'padrao' else opts[opt]
                if dev:
                    devs.append(dev)
        install_tested(devs)
    else:
        raise SystemExit(__doc__ if __doc__ else 'uso: mesa_apo.py instalar [--entrada ID|padrao] [--saida ID|padrao]')
