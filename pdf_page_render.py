# -*- coding: utf-8 -*-
"""由 redirect_addon 子进程调用：渲染 PDF 单页或统计总页数。

1) 统计页数：python pdf_page_render.py --count <pdf路径>
   向 stdout 打印整数页数。

2) 渲染 PNG：python pdf_page_render.py <pdf路径> <页码0起> <缩放浮点>
   向 stdout 输出 PNG 二进制。

依赖：pip install pymupdf（装在运行本脚本的 Python 中，可与 mitmproxy 安装方式无关）。
"""
from __future__ import annotations

import sys


def _count(path: str) -> int:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("当前 Python 未安装 pymupdf，请执行: pip install pymupdf", file=sys.stderr)
        raise SystemExit(3)
    try:
        doc = fitz.open(path)
        n = len(doc)
        doc.close()
    except Exception as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(4)
    return n


def _render(path: str, page_i: int, scale: float) -> bytes:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        print("当前 Python 未安装 pymupdf，请执行: pip install pymupdf", file=sys.stderr)
        raise SystemExit(3)
    try:
        doc = fitz.open(path)
        if page_i >= len(doc):
            page_i = max(0, len(doc) - 1)
        page = doc.load_page(page_i)
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        png = pix.tobytes("png")
        doc.close()
    except Exception as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(4)
    return png


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--count":
        n = _count(sys.argv[2])
        sys.stdout.write(str(n))
        return 0
    if len(sys.argv) != 4:
        print(
            "用法:\n  pdf_page_render.py --count <pdf>\n"
            "  pdf_page_render.py <pdf> <page0> <scale>",
            file=sys.stderr,
        )
        return 2
    path, page_s, scale_s = sys.argv[1], sys.argv[2], sys.argv[3]
    try:
        page_i = max(0, int(page_s))
        scale = float(scale_s)
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2
    png = _render(path, page_i, scale)
    sys.stdout.buffer.write(png)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
