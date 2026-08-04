import io

from PIL import Image

from download_sentinel2_truecolor import fraction_black


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_fully_black_image_is_all_black():
    img = Image.new("RGB", (10, 10), (0, 0, 0))
    assert fraction_black(_png_bytes(img)) == 1.0


def test_fully_white_image_has_no_black():
    img = Image.new("RGB", (10, 10), (255, 255, 255))
    assert fraction_black(_png_bytes(img)) == 0.0


def test_half_black_half_white_is_roughly_half():
    img = Image.new("RGB", (10, 10), (255, 255, 255))
    for y in range(10):
        for x in range(5):
            img.putpixel((x, y), (0, 0, 0))
    assert fraction_black(_png_bytes(img)) == 0.5


def test_dark_but_nonzero_pixels_below_threshold_count_as_black():
    # threshold por default es 8 de luminancia: un par de unidades de ruido
    # sobre negro puro (típico en compresión PNG) no debería escapar la
    # detección de nodata.
    img = Image.new("RGB", (4, 4), (2, 2, 2))
    assert fraction_black(_png_bytes(img)) == 1.0


def test_realistic_terrain_color_is_not_flagged_as_black():
    # Un verde/marrón típico de vegetación o suelo con gain=2.5 no debería
    # caer bajo el umbral de luminancia, aunque sea una imagen "oscura".
    img = Image.new("RGB", (4, 4), (60, 80, 40))
    assert fraction_black(_png_bytes(img)) == 0.0


def test_custom_threshold_is_respected():
    img = Image.new("RGB", (4, 4), (20, 20, 20))
    assert fraction_black(_png_bytes(img), threshold=8) == 0.0
    assert fraction_black(_png_bytes(img), threshold=25) == 1.0
