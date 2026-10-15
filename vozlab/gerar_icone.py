"""
Gera o ícone do VozLab (barras de onda sonora douradas em fundo escuro).

Saídas:
    vozlab/VozLab.ico            (16 a 256 px, usado no VozLab.exe e nos atalhos)
    frontend/novo/icon.png       (ícone da janela/aba; o anterior é preservado como icon_voicebox.png)

Uso:
    venv_rocm\\Scripts\\python.exe vozlab\\gerar_icone.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

RAIZ = Path(__file__).resolve().parent.parent
FUNDO = (23, 25, 31, 255)      # --surface da interface
BORDA = (59, 62, 74, 255)      # --border
OURO = (240, 179, 77, 255)     # --accent
ALTURAS = (0.34, 0.62, 0.86, 0.62, 0.34)  # forma de onda simétrica


def desenhar(lado: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    m = lado * 0.04
    d.rounded_rectangle((m, m, lado - m, lado - m), radius=lado * 0.22, fill=FUNDO,
                        outline=BORDA, width=max(1, int(lado * 0.02)))
    n = len(ALTURAS)
    larg = lado * 0.105
    vao = lado * 0.055
    total = n * larg + (n - 1) * vao
    x = (lado - total) / 2
    for h in ALTURAS:
        alt = lado * 0.62 * h
        y0 = (lado - alt) / 2
        d.rounded_rectangle((x, y0, x + larg, y0 + alt), radius=larg / 2, fill=OURO)
        x += larg + vao
    return img


def main() -> None:
    base = desenhar()
    ico = RAIZ / "vozlab" / "VozLab.ico"
    base.save(ico, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    png = RAIZ / "frontend" / "novo" / "icon.png"
    antigo = png.with_name("icon_voicebox.png")
    if png.exists() and not antigo.exists():
        png.rename(antigo)  # preserva o ícone anterior (nada é apagado)
    base.resize((256, 256), Image.LANCZOS).save(png)
    print("gerado:", ico, ico.stat().st_size, "bytes")
    print("gerado:", png, png.stat().st_size, "bytes | anterior preservado em", antigo.name)


if __name__ == "__main__":
    main()
