# -*- coding: utf-8 -*-
"""生成应用图标：installer/icon.png (256) + installer/icon.ico (多尺寸) + web favicon

用法: python installer/make_icon.py
"""
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
PNG = HERE / 'icon.png'
ICO = HERE / 'icon.ico'
FAVICON = HERE.parent / 'web' / 'public' / 'favicon.ico'


def main():
    img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((8, 8, 248, 248), radius=56, fill=(59, 130, 246, 255))
    font = None
    for name in ('arialbd.ttf', 'DejaVuSans-Bold.ttf'):
        try:
            font = ImageFont.truetype(name, 150)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    d.text((128, 128), 'R', font=font, fill='white', anchor='mm')

    img.save(PNG)
    # Windows .ico（exe 图标/安装向导/桌面快捷方式）：多尺寸合一
    img.save(ICO, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    # 浏览器标签栏 favicon（web/public 会被 Vite 拷进 dist 根）
    FAVICON.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ICO, FAVICON)
    print(f'written: {PNG}, {ICO}, {FAVICON}')


if __name__ == '__main__':
    main()
