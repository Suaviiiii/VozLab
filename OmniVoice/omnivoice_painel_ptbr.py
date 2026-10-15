#!/usr/bin/env python3
"""
Painel completo do OmniVoice em português (Missão OmniVoice, Fase OV-6).

É montado pelo serviço (omnivoice_service.py) em http://127.0.0.1:17495/painel e gera pela MESMA fila e pelo MESMO
modelo do serviço (função `gerar` recebida do serviço). O painel original (omnivoice/cli/demo.py) continua intacto.

O que tem:
- Clonar voz, com três origens: biblioteca do VozLab (só leitura), catálogo do OmniVoice (pasta catalogo_vozes)
  ou um arquivo enviado na hora.
- Criar voz (Design), com os atributos do modelo em português.
- TODOS os ajustes nativos do modelo, recolhidos, cada um com uma explicação curta.
O catálogo e a biblioteca mostram a voz lendo o áudio direto (sem depender do ffprobe, que não existe nesta máquina).
"""

import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np
import soundfile as sf

BASE_DIR = Path(__file__).resolve().parent
CATALOGO_DIR = BASE_DIR / "catalogo_vozes"
DADOS_VOZLAB = Path(os.path.expandvars(os.environ.get("VOZLAB_DADOS", r"%APPDATA%\sh.voicebox.app")))

AUTO = "Automático"

# Idiomas mais usados primeiro, com nome em português (o valor é o nome que o modelo entende)
IDIOMAS_PT = [
    ("Português", "Portuguese"), ("Inglês", "English"), ("Espanhol", "Spanish"), ("Francês", "French"),
    ("Alemão", "German"), ("Italiano", "Italian"), ("Japonês", "Japanese"), ("Chinês", "Chinese"),
    ("Coreano", "Korean"), ("Russo", "Russian"), ("Árabe", "Arabic"), ("Hindi", "Hindi"),
    ("Holandês", "Dutch"), ("Polonês", "Polish"), ("Turco", "Turkish"), ("Sueco", "Swedish"),
    ("Ucraniano", "Ukrainian"), ("Grego", "Greek"), ("Tcheco", "Czech"), ("Hebraico", "Hebrew"),
    ("Indonésio", "Indonesian"), ("Vietnamita", "Vietnamese"), ("Tailandês", "Thai"), ("Persa", "Persian"),
    ("Romeno", "Romanian"),
]
# Código do VozLab -> nome em português (mostrado na lista da biblioteca)
NOMES_IDIOMA_VOZLAB = {
    "pt": "Português", "en": "Inglês", "es": "Espanhol", "fr": "Francês", "de": "Alemão", "it": "Italiano",
    "ja": "Japonês", "zh": "Chinês", "ko": "Coreano", "ru": "Russo", "ar": "Árabe", "hi": "Hindi",
    "nl": "Holandês", "pl": "Polonês", "tr": "Turco", "sv": "Sueco", "uk": "Ucraniano", "el": "Grego",
    "cs": "Tcheco", "he": "Hebraico", "id": "Indonésio", "vi": "Vietnamita", "th": "Tailandês",
}

# Atributos do Design: (rótulo em português, valor original "English / 中文" que o modelo entende)
CATEGORIAS = {
    "Gênero": [("Masculino", "Male / 男"), ("Feminino", "Female / 女")],
    "Idade": [("Criança", "Child / 儿童"), ("Adolescente", "Teenager / 少年"), ("Jovem adulto", "Young Adult / 青年"),
              ("Meia-idade", "Middle-aged / 中年"), ("Idoso", "Elderly / 老年")],
    "Tom (altura da voz)": [("Muito grave", "Very Low Pitch / 极低音调"), ("Grave", "Low Pitch / 低音调"),
                            ("Médio", "Moderate Pitch / 中音调"), ("Agudo", "High Pitch / 高音调"),
                            ("Muito agudo", "Very High Pitch / 极高音调")],
    "Estilo": [("Sussurro", "Whisper / 耳语")],
    "Sotaque (só para fala em inglês)": [
        ("Americano", "American Accent / 美式口音"), ("Australiano", "Australian Accent / 澳大利亚口音"),
        ("Britânico", "British Accent / 英国口音"), ("Chinês", "Chinese Accent / 中国口音"),
        ("Canadense", "Canadian Accent / 加拿大口音"), ("Indiano", "Indian Accent / 印度口音"),
        ("Coreano", "Korean Accent / 韩国口音"), ("Português", "Portuguese Accent / 葡萄牙口音"),
        ("Russo", "Russian Accent / 俄罗斯口音"), ("Japonês", "Japanese Accent / 日本口音")],
    "Dialeto (só para fala em chinês)": [
        ("Henan", "Henan Dialect / 河南话"), ("Shaanxi", "Shaanxi Dialect / 陕西话"), ("Sichuan", "Sichuan Dialect / 四川话"),
        ("Guizhou", "Guizhou Dialect / 贵州话"), ("Yunnan", "Yunnan Dialect / 云南话"), ("Guilin", "Guilin Dialect / 桂林话"),
        ("Jinan", "Jinan Dialect / 济南话"), ("Shijiazhuang", "Shijiazhuang Dialect / 石家庄话"),
        ("Gansu", "Gansu Dialect / 甘肃话"), ("Ningxia", "Ningxia Dialect / 宁夏话"), ("Qingdao", "Qingdao Dialect / 青岛话"),
        ("Nordeste", "Northeast Dialect / 东北话")],
}

ORIGEM_BIBLIOTECA = "Biblioteca do VozLab"
ORIGEM_CATALOGO = "Catálogo do OmniVoice"
ORIGEM_ARQUIVO = "Enviar um arquivo"


# ----------------------------------------------------------------------------------------------- vozes
def vozes_catalogo() -> Dict[str, Tuple[str, str]]:
    """Nome -> (caminho do áudio, texto de referência do .txt ao lado, se houver)."""
    CATALOGO_DIR.mkdir(exist_ok=True)
    vozes = {}
    for f in sorted(CATALOGO_DIR.iterdir()):
        if f.suffix.lower() in (".wav", ".mp3", ".flac", ".ogg", ".m4a"):
            txt = f.with_suffix(".txt")
            ref = txt.read_text(encoding="utf-8").strip() if txt.exists() else ""
            vozes[f.stem] = (str(f), ref)
    return vozes


def vozes_biblioteca() -> Dict[str, Tuple[str, str]]:
    """Vozes clonadas da biblioteca do VozLab, SÓ LEITURA (banco aberto em modo leitura; nada é copiado).
    Rótulo "Nome · Idioma" -> (caminho do áudio da 1ª amostra, texto de referência)."""
    db = DADOS_VOZLAB / "voicebox.db"
    if not db.exists():
        return {}
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        linhas = con.execute(
            "select p.name, p.language, s.audio_path, s.reference_text from profiles p "
            "join profile_samples s on s.profile_id = p.id order by p.name, s.id").fetchall()
    finally:
        con.close()
    vozes: Dict[str, Tuple[str, str]] = {}
    for nome, lang, caminho, ref in linhas:
        rotulo = f"{nome} · {NOMES_IDIOMA_VOZLAB.get((lang or '').lower(), (lang or '?').upper())}"
        if rotulo in vozes:  # perfis com várias amostras: usa a 1ª
            continue
        arq = (DADOS_VOZLAB / caminho) if not os.path.isabs(caminho) else Path(caminho)
        if arq.exists():
            vozes[rotulo] = (str(arq.resolve()), ref or "")
    return vozes


def _ler_para_ouvir(caminho: str):
    """Lê o áudio e devolve (taxa, amostras) para o player: sem ffprobe."""
    onda, sr = sf.read(caminho, dtype="float32", always_2d=False)
    if onda.ndim > 1:
        onda = onda.mean(axis=1)
    return sr, (np.clip(onda, -1, 1) * 32767).astype(np.int16)


# ----------------------------------------------------------------------------------------------- painel
def construir_painel(gerar: Callable[[Dict[str, Any]], Dict[str, Any]], nomes_idiomas: List[str]):
    """`gerar(pedido)` é a função do serviço (mesma fila e mesmo modelo); devolve {"ok", "audio", "sampling_rate"}."""
    import gradio as gr

    escolhas_idioma = [(AUTO, AUTO)] + IDIOMAS_PT + [(n, n) for n in nomes_idiomas
                                                       if n not in {v for _, v in IDIOMAS_PT}]

    def seletor_idioma():
        return gr.Dropdown(label="Idioma do texto", choices=escolhas_idioma, value=AUTO, interactive=True,
                           info="Deixe em Automático para o modelo descobrir. Os mais usados estão no topo.")

    def ajustes():
        with gr.Accordion("Ajustes do modelo (opcional)", open=False):
            gr.Markdown("Tudo aqui já vem no valor recomendado. Mexa só se souber o que quer mudar.")
            with gr.Row():
                ns = gr.Slider(4, 64, value=32, step=1, label="Passos de difusão",
                               info="Mais passos: som mais polido e mais lento. 16 = rápido; 32 = padrão do painel.")
                gs = gr.Slider(0.0, 4.0, value=2.0, step=0.1, label="CFG (escala de orientação)",
                               info="O quanto segue a voz e o texto. Mais alto pode soar tenso. Padrão 2,0.")
            with gr.Row():
                sp = gr.Slider(0.5, 1.5, value=1.0, step=0.05, label="Velocidade",
                               info="1,0 = normal. Ignorada se a Duração for preenchida.")
                du = gr.Number(value=None, label="Duração (segundos)",
                               info="Vazio = usa a velocidade. Preenchido = força o áudio a durar isso.")
            with gr.Row():
                seed = gr.Number(value=None, precision=0, label="Semente",
                                 info="Vazio = sai diferente a cada vez. Um número = mesmo texto, mesmo áudio.")
                dn = gr.Checkbox(value=True, label="Reduzir ruído", info="Limpa ruído de fundo. Padrão: ligado.")
            with gr.Row():
                pp = gr.Checkbox(value=True, label="Preparar a referência",
                                 info="Tira silêncios do áudio de referência e põe ponto final no texto dele.")
                po = gr.Checkbox(value=True, label="Tirar silêncios longos do resultado")
            with gr.Accordion("Avançado (ajustes internos do modelo)", open=False):
                with gr.Row():
                    ts = gr.Slider(0.01, 1.0, value=0.1, step=0.01, label="t_shift (desvio dos passos)",
                                   info="Onde o modelo gasta mais passos. Padrão 0,1.")
                    pt = gr.Slider(0.0, 20.0, value=5.0, step=0.5, label="Temperatura da posição",
                                   info="Variedade na ordem em que o som é montado. Padrão 5.")
                with gr.Row():
                    ct = gr.Slider(0.0, 2.0, value=0.0, step=0.05, label="Temperatura do som",
                                   info="0 = sempre a escolha mais provável (estável). Mais alto = mais variado.")
                    lp = gr.Slider(0.0, 10.0, value=5.0, step=0.5, label="Penalidade por camada",
                                   info="Equilíbrio entre as camadas de som. Padrão 5.")
                with gr.Row():
                    cd = gr.Slider(5.0, 60.0, value=15.0, step=1.0, label="Divisão interna: tamanho (s)",
                                   info="Textos longos são gerados em pedaços deste tamanho. Padrão 15 s.")
                    cl = gr.Slider(10.0, 300.0, value=30.0, step=5.0, label="Divisão interna: a partir de (s)",
                                   info="Só divide se o áudio previsto passar disto. Padrão 30 s.")
        return [ns, gs, sp, du, seed, dn, pp, po, ts, pt, ct, lp, cd, cl]

    def montar_pedido(texto, idioma, aj):
        ns, gs, sp, du, seed, dn, pp, po, ts, pt, ct, lp, cd, cl = aj
        return {
            "text": texto, "language": None if (not idioma or idioma == AUTO) else idioma,
            "num_step": int(ns), "guidance_scale": float(gs), "speed": float(sp),
            "duration": float(du) if du else None, "seed": int(seed) if seed not in (None, "") else None,
            "denoise": bool(dn), "preprocess_prompt": bool(pp), "postprocess_output": bool(po),
            "t_shift": float(ts), "position_temperature": float(pt), "class_temperature": float(ct),
            "layer_penalty_factor": float(lp), "audio_chunk_duration": float(cd), "audio_chunk_threshold": float(cl),
            "usar_cache": True,
        }

    def resultado(r):
        if not r.get("ok"):
            return None, "Erro: " + str(r.get("erro"))
        onda = (np.clip(r["audio"], -1, 1) * 32767).astype(np.int16)
        dur = len(r["audio"]) / r["sampling_rate"]
        return (r["sampling_rate"], onda), f"Pronto: {dur:.1f} s de áudio em {r.get('segundos', 0):.1f} s."

    with gr.Blocks(title="Painel do OmniVoice") as painel:
        gr.Markdown(
            "# Painel completo do OmniVoice\n"
            "Modelo de voz em **mais de 600 idiomas**: clona uma voz a partir de um áudio ou cria uma voz nova a "
            "partir de uma descrição.\n\n"
            "Este painel usa **o mesmo modelo e a mesma fila** do VozLab: se o VozLab estiver gerando, o pedido "
            "espera ele terminar. O modelo carrega na placa de vídeo na 1ª geração (~15 s) e sai sozinho depois "
            "de 10 minutos parado."
        )
        with gr.Tabs():
            # =========================================================================== Clonar
            with gr.Tab("Clonar voz"):
                with gr.Row():
                    with gr.Column(scale=1):
                        cl_texto = gr.Textbox(label="Texto para falar", lines=5,
                                              placeholder="Escreva aqui o texto que a voz vai falar...")
                        cl_origem = gr.Radio([ORIGEM_BIBLIOTECA, ORIGEM_CATALOGO, ORIGEM_ARQUIVO],
                                             value=ORIGEM_BIBLIOTECA, label="De onde vem a voz")
                        bib_inicial = vozes_biblioteca()
                        cl_bib = gr.Dropdown(label=f"Voz da biblioteca do VozLab ({len(bib_inicial)} vozes, só leitura)",
                                             choices=sorted(bib_inicial), value=None, filterable=True,
                                             info="Digite para procurar. As vozes não são copiadas nem alteradas.")
                        cat_inicial = vozes_catalogo()
                        cl_cat = gr.Dropdown(label=f"Voz do catálogo (pasta catalogo_vozes, {len(cat_inicial)} vozes)",
                                             choices=sorted(cat_inicial), value=None, visible=False)
                        cl_arquivo = gr.Audio(label="Seu arquivo de áudio (3 a 10 s é o ideal)", type="filepath",
                                              sources=["upload"], visible=False)
                        cl_atualizar = gr.Button("Atualizar listas de vozes", size="sm")
                        cl_ouvir = gr.Audio(label="Ouvir a voz escolhida", type="numpy", interactive=False)
                        cl_ref = gr.Textbox(label="Texto da referência (opcional)", lines=2,
                                            placeholder="O que é falado no áudio de referência. Vazio: o Whisper "
                                                        "transcreve sozinho (usa mais ~1,6 GB da placa na 1ª vez).")
                        cl_idioma = seletor_idioma()
                        with gr.Accordion("Instrução extra (opcional)", open=False):
                            cl_instr = gr.Textbox(label="Instrução", lines=2,
                                                  info="Ex.: whisper (sussurro). Em inglês, como o modelo entende.")
                        cl_aj = ajustes()
                        cl_btn = gr.Button("Gerar", variant="primary")
                    with gr.Column(scale=1):
                        cl_saida = gr.Audio(label="Áudio gerado", type="numpy")
                        cl_status = gr.Textbox(label="Situação", lines=2)
                cl_caminho = gr.State(None)

                def trocar_origem(origem):
                    return (gr.update(visible=origem == ORIGEM_BIBLIOTECA), gr.update(visible=origem == ORIGEM_CATALOGO),
                            gr.update(visible=origem == ORIGEM_ARQUIVO), None, None, "")

                cl_origem.change(trocar_origem, [cl_origem], [cl_bib, cl_cat, cl_arquivo, cl_caminho, cl_ouvir, cl_ref])

                def escolher(nome, fonte):
                    vozes = vozes_biblioteca() if fonte == "bib" else vozes_catalogo()
                    if not nome or nome not in vozes:
                        return None, None, ""
                    caminho, ref = vozes[nome]
                    try:
                        audio = _ler_para_ouvir(caminho)
                    except Exception:
                        audio = None
                    return caminho, audio, ref

                cl_bib.change(lambda n: escolher(n, "bib"), [cl_bib], [cl_caminho, cl_ouvir, cl_ref])
                cl_cat.change(lambda n: escolher(n, "cat"), [cl_cat], [cl_caminho, cl_ouvir, cl_ref])
                cl_arquivo.change(lambda p: p, [cl_arquivo], [cl_caminho])

                def atualizar():
                    b, c = vozes_biblioteca(), vozes_catalogo()
                    return (gr.update(choices=sorted(b), label=f"Voz da biblioteca do VozLab ({len(b)} vozes, só leitura)"),
                            gr.update(choices=sorted(c), label=f"Voz do catálogo (pasta catalogo_vozes, {len(c)} vozes)"))

                cl_atualizar.click(atualizar, None, [cl_bib, cl_cat])

                def clonar(texto, caminho, ref, idioma, instr, *aj):
                    if not texto or not texto.strip():
                        return None, "Escreva o texto para falar."
                    if not caminho:
                        return None, "Escolha uma voz (biblioteca, catálogo ou arquivo)."
                    pedido = montar_pedido(texto, idioma, aj)
                    pedido.update(mode="clone", ref_audio_path=caminho, ref_text=(ref or "").strip() or None,
                                  instruct=(instr or "").strip() or None)
                    return resultado(gerar(pedido))

                cl_btn.click(clonar, [cl_texto, cl_caminho, cl_ref, cl_idioma, cl_instr] + cl_aj, [cl_saida, cl_status],
                             api_name="clonar")

            # =========================================================================== Design
            with gr.Tab("Criar voz (Design)"):
                with gr.Row():
                    with gr.Column(scale=1):
                        ds_texto = gr.Textbox(label="Texto para falar", lines=5,
                                              placeholder="Escreva aqui o texto que a voz vai falar...")
                        ds_idioma = seletor_idioma()
                        gr.Markdown("**Como é a voz?** Deixe em Automático o que não importar. Tudo em Automático "
                                    "= a voz neutra do próprio modelo.")
                        ds_attrs = [gr.Dropdown(label=cat, choices=[(AUTO, AUTO)] + ops, value=AUTO)
                                    for cat, ops in CATEGORIAS.items()]
                        ds_aj = ajustes()
                        ds_btn = gr.Button("Gerar", variant="primary")
                    with gr.Column(scale=1):
                        ds_saida = gr.Audio(label="Áudio gerado", type="numpy")
                        ds_status = gr.Textbox(label="Situação", lines=2)

                def instrucao(attrs):
                    partes = []
                    for v in attrs:
                        if not v or v == AUTO:
                            continue
                        en, zh = v.split(" / ", 1)
                        partes.append(zh.strip() if "Dialect" in en else en.strip())
                    return ", ".join(partes) or None

                def desenhar(texto, idioma, *resto):
                    if not texto or not texto.strip():
                        return None, "Escreva o texto para falar."
                    n = len(CATEGORIAS)
                    attrs, aj = resto[:n], resto[n:]
                    pedido = montar_pedido(texto, idioma, aj)
                    instr = instrucao(attrs)
                    pedido.update(mode="design" if instr else "auto", instruct=instr)
                    return resultado(gerar(pedido))

                ds_btn.click(desenhar, [ds_texto, ds_idioma] + ds_attrs + ds_aj, [ds_saida, ds_status],
                             api_name="desenhar")

        gr.Markdown(
            "<small>Painel original do OmniVoice (em inglês e chinês) guardado em `omnivoice/cli/demo.py`. "
            "Endereço deste painel: http://127.0.0.1:17495/painel</small>"
        )
    return painel
