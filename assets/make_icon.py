"""生成 幕言 图标：深色圆角底 + 白色「幕」+ 一条字幕条，导出多尺寸 ico。"""
import sys

from PIL import Image, ImageDraw, ImageFont

SIZE = 512
BG_TOP = (26, 30, 38)
BG_BOT = (12, 14, 18)
FG = (245, 247, 250)
ACCENT = (86, 197, 255)
FONT = r"C:\Windows\Fonts\msyhbd.ttc"

img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 竖向渐变
for y in range(SIZE):
    t = y / (SIZE - 1)
    c = tuple(int(BG_TOP[i] * (1 - t) + BG_BOT[i] * t) for i in range(3))
    d.line([(0, y), (SIZE, y)], fill=c + (255,))

# 圆角遮罩
mask = Image.new("L", (SIZE, SIZE), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=96, fill=255)
img.putalpha(mask)

d = ImageDraw.Draw(img)
font = ImageFont.truetype(FONT, 300)
bbox = d.textbbox((0, 0), "幕", font=font)
w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
d.text(((SIZE - w) / 2 - bbox[0], (SIZE - h) / 2 - bbox[1] - 26), "幕", font=font, fill=FG)

# 字幕条：一条亮线 + 一条暗线
d.rounded_rectangle([96, 372, 300, 392], radius=10, fill=ACCENT + (255,))
d.rounded_rectangle([96, 408, 416, 428], radius=10, fill=(255, 255, 255, 90))

out = sys.argv[1] if len(sys.argv) > 1 else "幕言.ico"
img.save(out, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("saved", out)
