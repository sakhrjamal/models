# -*- coding: utf-8 -*-
"""مولّد باركود Code 128 (المجموعة B) كـ SVG بلا مكتبات خارجية — يُقرأ بأي ماسح."""
PATTERNS = ("212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 221312 231212 112232 122132 122231 113222 "
            "123122 123221 223211 221132 221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 212123 212321 "
            "232121 111323 131123 131321 112313 132113 132311 211313 231113 231311 112133 112331 132131 113123 113321 133121 "
            "313121 211331 231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 314111 221411 431111 111224 "
            "111422 121124 121421 141122 141221 112214 112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
            "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 214121 412121 111143 111341 131141 114113 "
            "114311 411113 411311 113141 114131 311141 411131 211412 211214 211232 2331112").split()
assert len(PATTERNS) == 107


def encode(text):
    """قائمة قيم Code128-B للنص (يقبل ASCII الطباعي فقط؛ غيره يُستبدل بـ ?)."""
    vals = [104]
    for ch in str(text):
        o = ord(ch)
        vals.append(o - 32 if 32 <= o < 127 else ord('?') - 32)
    chk = (vals[0] + sum(i * v for i, v in enumerate(vals[1:], 1))) % 103
    return vals + [chk, 106]


def svg(text, height=46, module=1.6, quiet=10, show_text=True):
    """SVG للباركود مع النص أسفله."""
    vals = encode(text)
    x, bars = quiet * module, []
    for v in vals:
        pat = PATTERNS[v]
        for i, w in enumerate(pat):
            w = int(w) * module
            if i % 2 == 0:
                bars.append(f'<rect x="{x:.2f}" y="0" width="{w:.2f}" height="{height}"/>')
            x += w
    width = x + quiet * module
    h = height + (13 if show_text else 0)
    txt = (f'<text x="{width / 2:.1f}" y="{height + 11}" font-size="11" text-anchor="middle" font-family="monospace">{_esc(text)}</text>'
           if show_text else '')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width:.1f} {h}" width="{width:.0f}" height="{h}" '
            f'preserveAspectRatio="xMidYMid meet"><rect width="100%" height="100%" fill="#fff"/><g fill="#000">{"".join(bars)}</g>{txt}</svg>')


def _esc(s):
    return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
