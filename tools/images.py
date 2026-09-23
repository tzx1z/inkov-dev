#!/usr/bin/env python3
"""Подготовка изображений для сайта: AVIF, WebP и JPEG заданных ширин.

Запуск из корня репозитория:

    python3 tools/images.py SRC PREFIX WIDTH... [--crop l,t,r,b]
        [--q-avif 55] [--q-webp 78] [--q-jpeg 80]

Портрет в hero:

    python3 tools/images.py _src/img/avatar.jpg \\
        assets/img/photos/evgenii-inkov-portrait 256 400 600 --crop 250,30,850,630

Скриншоты проекта:

    python3 tools/images.py _src/img/projects/<slug>/1.png \\
        assets/img/projects/<slug>/<slug>-1 640 1280 --q-avif 70 --q-webp 85

Результат: PREFIX-<ширина>.avif, PREFIX-<ширина>.webp, PREFIX-<ширина>.jpg.
Ориентация берется из EXIF, цвета переводятся из встроенного ICC-профиля
в sRGB, прозрачные области заливаются белым, EXIF, XMP и ICC удаляются.
Изображение только уменьшается (Lanczos), увеличение не выполняется.
При ошибке скрипт выводит сообщение в stderr и завершается с кодом 1.
"""

import argparse
import io
import sys
from pathlib import Path

from PIL import Image, ImageCms, ImageOps, features

# Параметры кодеков
AVIF_SPEED = 4
WEBP_METHOD = 6


class Parser(argparse.ArgumentParser):
    """argparse с кодом выхода 1 вместо 2 при неверных аргументах."""

    def error(self, message):
        self.print_usage(sys.stderr)
        fail(message)


def fail(message):
    # sys.exit со строкой печатает ее в stderr и завершает процесс с кодом 1
    sys.exit(f"images.py: ошибка: {message}")


def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается целое число, получено {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"ширина должна быть больше 0, получено {number}")
    return number


def quality(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"ожидается целое число 0-100, получено {value!r}") from None
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError(f"качество должно быть в диапазоне 0-100, получено {number}")
    return number


def crop_box(value):
    try:
        box = tuple(int(part) for part in value.split(","))
    except ValueError:
        box = ()
    if len(box) != 4:
        raise argparse.ArgumentTypeError(f"ожидаются четыре целых числа l,t,r,b, получено {value!r}")
    left, top, right, bottom = box
    if left < 0 or top < 0 or right <= left or bottom <= top:
        raise argparse.ArgumentTypeError(f"нужно 0 <= l < r и 0 <= t < b, получено {value!r}")
    return box


def main():
    parser = Parser(
        prog="images.py",
        description="Готовит AVIF, WebP и JPEG заданных ширин без метаданных, в sRGB.",
    )
    parser.add_argument("src", type=Path, metavar="SRC", help="исходное изображение (JPEG, PNG и т.п.)")
    parser.add_argument("prefix", metavar="PREFIX", help="префикс выходных файлов, например assets/img/photos/name")
    parser.add_argument("widths", type=positive_int, nargs="+", metavar="WIDTH", help="ширины результата в пикселях")
    parser.add_argument("--crop", type=crop_box, metavar="l,t,r,b",
                        help="кадр в пикселях исходника после поворота по EXIF: left,top,right,bottom")
    parser.add_argument("--q-avif", type=quality, default=55, metavar="N", help="качество AVIF (по умолчанию 55)")
    parser.add_argument("--q-webp", type=quality, default=78, metavar="N", help="качество WebP (по умолчанию 78)")
    parser.add_argument("--q-jpeg", type=quality, default=80, metavar="N", help="качество JPEG (по умолчанию 80)")
    args = parser.parse_args()

    if len(set(args.widths)) != len(args.widths):
        parser.error(f"ширины повторяются: {' '.join(map(str, args.widths))}")
    if not Path(args.prefix).name or args.prefix.endswith(("/", "\\")):
        parser.error(f"PREFIX должен заканчиваться именем файла, получено {args.prefix!r}")

    missing = [name for name in ("avif", "webp") if not features.check(name)]
    if missing:
        fail(f"Pillow собран без поддержки {', '.join(missing)}, обновите Pillow")

    if not args.src.is_file():
        fail(f"файл не найден: {args.src}")
    try:
        with Image.open(args.src) as src:
            # Поворот по EXIF Orientation; ICC-профиль остается в im.info
            im = ImageOps.exif_transpose(src)
    except (OSError, ValueError, Image.DecompressionBombError) as e:
        fail(f"не удалось прочитать {args.src}: {e}")

    # Геометрию проверяем до обработки, чтобы при ошибке не тратить время
    # и не оставлять часть выходных файлов
    width, height = im.size
    if args.crop and (args.crop[2] > width or args.crop[3] > height):
        fail(f"--crop {','.join(map(str, args.crop))} выходит за границы изображения {width}x{height}")
    frame_width = args.crop[2] - args.crop[0] if args.crop else width
    too_wide = [w for w in args.widths if w > frame_width]
    if too_wide:
        fail(f"ширина {', '.join(map(str, too_wide))} больше ширины кадра {frame_width}px, увеличение не выполняется")

    try:
        # Режимы с альфой и палитрой (RGBA, LA, P, PA и др.) приводятся к RGBA,
        # альфа сохраняется отдельно. RGB и L с прозрачным цветом (tRNS) тоже:
        # иначе прозрачные пиксели останутся цветом ключа
        alpha = None
        if im.mode not in ("RGB", "L") or "transparency" in im.info:
            im = im.convert("RGBA")
            alpha = im.getchannel("A")

        # Перевод встроенного профиля (например, Display P3) в sRGB до его удаления
        icc = im.info.get("icc_profile")
        if icc:
            try:
                im = ImageCms.profileToProfile(
                    im,
                    ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                    ImageCms.createProfile("sRGB"),
                    outputMode="RGB",
                )
            except ImageCms.PyCMSError as e:
                fail(f"не удалось перевести ICC-профиль в sRGB: {e}")
        else:
            im = im.convert("RGB")

        # Наложение на белый фон: без этого прозрачные области становятся черными
        if alpha is not None:
            bg = Image.new("RGB", im.size, "white")
            bg.paste(im, mask=alpha)
            im = bg

        if args.crop:
            im = im.crop(args.crop)

        # Удаление всех метаданных: AVIF-кодер Pillow берет icc_profile, exif и xmp
        # из im.info, если они не переданы явно
        im.info.clear()
    except (OSError, ValueError) as e:
        fail(f"не удалось обработать {args.src}: {e}")

    out_dir = Path(args.prefix).parent
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        for w in args.widths:
            h = max(1, round(im.height * w / im.width))
            resized = im if w == im.width else im.resize((w, h), Image.Resampling.LANCZOS)
            base = f"{args.prefix}-{w}"
            resized.save(f"{base}.avif", quality=args.q_avif, speed=AVIF_SPEED)
            resized.save(f"{base}.webp", quality=args.q_webp, method=WEBP_METHOD)
            resized.save(f"{base}.jpg", quality=args.q_jpeg, optimize=True, progressive=True)
            for ext in ("avif", "webp", "jpg"):
                path = Path(f"{base}.{ext}")
                print(f"{path}  {w}x{h}  {path.stat().st_size} B")
    except OSError as e:
        fail(f"не удалось записать результат: {e}")


if __name__ == "__main__":
    main()
