"""tests/test_v910.py — C-cart logo: site branding and icon files (v9.10)."""
import os
import struct

_F = os.path.join(os.path.dirname(__file__), "..", "frontend")
ARC = 'd="M46 17.5A18 18 0 1 0 46 42.5"'


def test_mark_in_header_and_sign_in():
    html = open(os.path.join(_F, "index.html"), encoding="utf-8").read()
    assert html.count(ARC) == 2
    assert '<div class="tb-brand"><svg class="brand-mark"' in html
    assert '<div class="auth-hd"><svg class="brand-mark auth-mark"' in html


def test_logo_svg_is_the_same_mark():
    svg = open(os.path.join(_F, "cartwise_logo.svg"), encoding="utf-8").read()
    assert ARC in svg and 'fill="#1a6b3c"' in svg and 'fill="#f5a524"' in svg


def test_icon_files():
    ico = open(os.path.join(_F, "favicon.ico"), "rb").read()
    reserved, kind, count = struct.unpack("<HHH", ico[:6])
    assert (reserved, kind) == (0, 1) and count == 3                     # 16, 32, 48
    for name, size in (("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)):
        png = open(os.path.join(_F, name), "rb").read()
        assert png[:8] == b"\x89PNG\r\n\x1a\n" and struct.unpack(">II", png[16:24]) == (size, size)
