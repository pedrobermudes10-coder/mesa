#!/usr/bin/env python3
# Desenha o ícone da Mesa (o mesmo de mesa.svg) em mesa.ico para o Windows. Uso: python3 icone.py saida.ico
import sys
from PIL import Image, ImageDraw

S = 4  # desenha em 1024 px e reduz (bordas suaves)
im = Image.new('RGBA', (256 * S, 256 * S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([8 * S, 8 * S, 248 * S, 248 * S], 44 * S, fill='#16181d', outline='#2c313a', width=4 * S)
for x, top in ((64, 140), (128, 96), (192, 164)):
    d.line([x * S, 52 * S, x * S, 204 * S], fill='#3a404b', width=10 * S)
    d.line([x * S, top * S, x * S, 204 * S], fill='#3fc8ff', width=10 * S)
    for y in (52, 204):
        d.ellipse([(x - 5) * S, (y - 5) * S, (x + 5) * S, (y + 5) * S], fill='#3fc8ff' if y == 204 else '#3a404b')
for x, y in ((40, 124), (104, 80), (168, 148)):
    d.rounded_rectangle([x * S, y * S, (x + 48) * S, (y + 30) * S], 7 * S, fill='#e8eaed', outline='#16181d', width=4 * S)
    d.line([(x + 10) * S, (y + 15) * S, (x + 38) * S, (y + 15) * S], fill='#16181d', width=4 * S)
im = im.resize((256, 256), Image.LANCZOS)
im.save(sys.argv[1], sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
