# -*- coding: utf-8 -*-
"""把 scf_index.py + Pillow 打成腾讯云 SCF 代码包（build/scf_code.zip）。

为什么要带 Pillow：新闻配图需要判断「这张图是不是文字截图」，用的是 Pillow 解码 +
灰度直方图；SCF 的 Python 3.7 运行时不带 Pillow，所以要随代码包一起上传。

首次使用先下载 cp37 wheel 并解压到 build/pilpkg：
    python -m pip download "Pillow==9.5.0" --only-binary=:all: \
        --platform manylinux2014_x86_64 --python-version 37 \
        --implementation cp --abi cp37m -d build/wheel
    cd build/wheel && unzip Pillow-9.5.0-*.whl -d ../pilpkg

之后每次改完代码只需：
    python scripts/build_zip.py
"""
import os
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(ROOT, "build")
PKG = os.path.join(BUILD, "pilpkg")
SRC = os.path.join(HERE, "scf_index.py")
OUT = os.path.join(BUILD, "scf_code.zip")

# Tk/Qt 相关用不上，删掉能省一点体积
SKIP_PREFIX = ("Pillow-9.5.0.dist-info", "PIL/ImageTk", "PIL/ImageQt")
SKIP_ANY = ("_imagingtk",)


def main():
    if not os.path.exists(SRC):
        raise SystemExit("缺少 %s" % SRC)
    if not os.path.isdir(PKG):
        raise SystemExit("缺少 %s，先按文件头注释下载并解压 Pillow wheel" % PKG)
    os.makedirs(BUILD, exist_ok=True)
    n = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(SRC, "index.py")   # 入口 index.main_handler
        n += 1
        for root, _dirs, files in os.walk(PKG):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, PKG).replace("\\", "/")
                if rel.startswith(SKIP_PREFIX) or any(k in rel for k in SKIP_ANY):
                    continue
                z.write(full, rel)
                n += 1
    size = os.path.getsize(OUT) / 1048576.0
    print("已生成 %s（%d 个文件，%.2f MB）" % (OUT, n, size))
    print("上传时 FunctionName=daily-push, Handler=index.main_handler")


if __name__ == "__main__":
    main()
