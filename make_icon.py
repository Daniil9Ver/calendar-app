"""Generates app.ico (a simple calendar glyph) for the window and the .exe."""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
S = 256
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((16, 24, 240, 240), radius=40, fill=(255, 255, 255, 255), outline=(210, 214, 220, 255), width=6)
d.rounded_rectangle((16, 24, 240, 92), radius=40, fill=(10, 132, 255, 255))
d.rectangle((16, 60, 240, 92), fill=(10, 132, 255, 255))
for x in (76, 180):
    d.rounded_rectangle((x - 9, 6, x + 9, 50), radius=9, fill=(60, 64, 72, 255))
try:
    font = ImageFont.truetype("seguisb.ttf", 112)
except OSError:
    font = ImageFont.load_default()
d.text((128, 166), "31", font=font, fill=(31, 35, 40, 255), anchor="mm")
img.save(os.path.join(HERE, "app.ico"), sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("app.ico written")
