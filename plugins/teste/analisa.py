import numpy as np, subprocess, sys
FS = 48000
def run(label, x, *ctl, bloco=1024):
    r = subprocess.run(['./bancada', label, *ctl], input=x.astype(np.float32).tobytes(), capture_output=True,
                       env={'BLOCO': str(bloco)})
    sys.stderr.write(r.stderr.decode())
    return np.frombuffer(r.stdout, np.float32).reshape(-1, 2)
def stereo(m): return np.stack([m, m], 1)
# 1) impulso pelo crystalizer com intensidade mínima: deve ser ~ atraso puro
x = np.zeros(48000 * 2); x[24000] = 1
low = [f'intensityBand{b}=-40' for b in range(13)]
for over in (1, 0):
    y = run('mesa_crystalizer', stereo(x), *low, f'oversampling={over}', 'adaptiveIntensity=0')[:, 0]
    k = int(np.argmax(np.abs(y))); d = k - 24000
    ir = y[k - 4000:k + 4000]
    H = np.abs(np.fft.rfft(ir, 1 << 16)); f = np.fft.rfftfreq(1 << 16, 1 / FS)
    sel = lambda a, b: 20 * np.log10(H[(f >= a) & (f <= b)])
    print(f'over={over}: atraso {d} amostras ({d / 48:.1f} ms); resposta 100-15k: {sel(100, 15000).min():+.2f}..{sel(100, 15000).max():+.2f} dB;'
          f' 30 Hz {sel(29, 31).mean():+.1f} dB; 20 kHz {sel(19900, 20100).mean():+.1f} dB')
# 2) crystalizer padrão (0 dB, adaptativo) em ruído rosa + senos: deve realçar agudos, sem explodir
rng = np.random.default_rng(1)
w = np.fft.irfft(np.fft.rfft(rng.standard_normal(FS * 10)) / np.sqrt(np.maximum(np.arange(FS * 5 + 1), 1)))
w = 0.1 * w / np.std(w)
y = run('mesa_crystalizer', stereo(w))
for lo, hi in ((100, 500), (1000, 3000), (5000, 10000), (10000, 16000)):
    def band(s):
        S = np.abs(np.fft.rfft(s[FS:])); f = np.fft.rfftfreq(len(s) - FS, 1 / FS)
        return 10 * np.log10(np.mean(S[(f >= lo) & (f < hi)] ** 2))
    print(f'  padrão {lo}-{hi} Hz: {band(y[:, 0]) - band(w):+.1f} dB   (finito: {np.isfinite(y).all()})')
for lab, ctl in (('mesa_filter', ['type=1', 'frequency=80', 'slope=2']), ('mesa_eq', ['numBands=4']),
                 ('mesa_compressor', ['mode=1', 'threshold=-24', 'ratio=4.4', 'knee=-7']), ('mesa_rnnoise', [])):
    y = run(lab, stereo(w), *ctl)
    print(f'  {lab}: rms entrada {20*np.log10(np.std(w)):.1f} dB saída {20*np.log10(np.std(y[FS:,0])):.1f} dB')
