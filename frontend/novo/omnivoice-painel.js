/* Painel OmniVoice do VozLab (Missão OmniVoice, OV-6+7).
   Clonar voz (biblioteca do VozLab, catálogo do OmniVoice ou arquivo enviado), criar voz (Design) e voz automática;
   pausas entre frases (as mesmas da tela Generate), todos os ajustes nativos do modelo, espera sem limite com
   progresso por bloco e botão Cancelar. Gera pela rota /omnivoice/painel/* do VozLab (serviço OmniVoice por baixo). */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const LIMITE_PASSO = 80;
  const PAUSAS = {
    curtas: {pause_oracao_ms: 90, pause_frase_ms: 180, pause_paragrafo_ms: 450},
    padrao: {},
    longas: {pause_oracao_ms: 160, pause_frase_ms: 350, pause_paragrafo_ms: 900},
    modelo: null,
  };
  const IDIOMAS = [
    ['auto', 'Automático (descobrir)'], ['pt', 'Português'], ['en', 'Inglês'], ['es', 'Espanhol'], ['fr', 'Francês'],
    ['de', 'Alemão'], ['it', 'Italiano'], ['ja', 'Japonês'], ['zh', 'Chinês'], ['ko', 'Coreano'], ['ru', 'Russo'],
    ['ar', 'Árabe'], ['hi', 'Hindi'], ['nl', 'Holandês'], ['pl', 'Polonês'], ['tr', 'Turco'], ['sv', 'Sueco'],
    ['no', 'Norueguês'], ['da', 'Dinamarquês'], ['fi', 'Finlandês'], ['uk', 'Ucraniano'], ['el', 'Grego'],
    ['cs', 'Tcheco'], ['he', 'Hebraico'], ['id', 'Indonésio'], ['vi', 'Vietnamita'], ['th', 'Tailandês'],
    ['fa', 'Persa'], ['ro', 'Romeno'], ['ms', 'Malaio'], ['sw', 'Suaíli'],
  ];
  const NOME_IDIOMA = Object.fromEntries(IDIOMAS);
  const PADRAO = {passos: 16, cfg: 2.0, velocidade: 1.0, tshift: 0.1, tpos: 5, tsom: 0, camada: 5, divtam: 15, divlim: 30};

  const estado = {
    iniciado: false, modo: 'clone', origem: 'biblioteca', vozes: [], catalogo: [], limite: LIMITE_PASSO,
    escolhida: null, gerando: null, sessao: [], vozPendente: null, timerStatus: null,
  };

  const vozlab = () => window.VBVozlab || {};
  const rotulo = (nome) => (vozlab().rotuloNome ? vozlab().rotuloNome(nome) : nome);
  const normalizar = (s) => String(s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase();
  async function getJson(url) {
    const r = await fetch(url);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    return r.json();
  }

  // ------------------------------------------------------------------ serviço
  async function atualizarServico() {
    let st = {status: 'offline'};
    try { st = await getJson('/omnivoice/status'); } catch (_) { /* fica offline */ }
    const on = st.status === 'online';
    $('ov-badge-status').className = 'ov-status-badge ' + (on ? 'badge-online' : 'badge-offline');
    const dev = st.device && !String(st.device).startsWith('desconhecido') ? st.device : '';
    $('ov-status-text').textContent = on ? (dev ? 'Ligado (' + dev + ')' : 'Ligado') : 'Desligado';
    const mb = on ? Math.round(st.vram_used_mb || 0) : 0;
    $('ov-vram-text').textContent = 'Placa: ' + mb + ' MB' + (on && !st.model_loaded ? ' (modelo fora da placa)' : '');
    $('ov-btn-unload').hidden = !(on && st.model_loaded);
    $('ov-btn-carregar').hidden = on && st.model_loaded;
    $('ov-btn-start').hidden = on;
    return st;
  }

  async function ligar() {
    const b = $('ov-btn-start');
    b.disabled = true;
    $('ov-status-text').textContent = 'Ligando…';
    $('ov-badge-status').className = 'ov-status-badge badge-loading';
    try {
      await fetch('/omnivoice/control', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'start'})});
      for (let i = 1; i <= 180; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if ((await atualizarServico()).status === 'online') break;
        $('ov-status-text').textContent = 'Ligando… (' + i + ' s)';
        $('ov-badge-status').className = 'ov-status-badge badge-loading';
      }
    } finally { b.disabled = false; atualizarServico(); }
  }

  async function carregar() {
    // "Carregar na placa agora": liga o serviço se preciso e carrega o modelo (o VozLab cede a placa).
    const b = $('ov-btn-carregar');
    b.disabled = true;
    const t0 = Date.now();
    const relogio = setInterval(() => { $('ov-status-text').textContent = 'Carregando… ' + Math.round((Date.now() - t0) / 1000) + ' s'; }, 1000);
    try {
      if ((await atualizarServico()).status !== 'online') await ligar();
      const r = await fetch('/omnivoice/control', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'load'})});
      const d = await r.json().catch(() => ({}));
      if (!r.ok || d.status !== 'loaded') throw new Error(typeof d.detail === 'string' ? d.detail : (d.message || 'HTTP ' + r.status));
      $('ovp-situacao').textContent = 'OmniVoice na placa (' + Math.round((Date.now() - t0) / 1000) + ' s). A próxima geração já começa direto.';
    } catch (e) {
      $('ovp-situacao').textContent = 'Não consegui carregar o OmniVoice: ' + (e.message || e);
    } finally {
      clearInterval(relogio);
      b.disabled = false;
      atualizarServico();
    }
  }

  async function liberar() {
    $('ov-btn-unload').disabled = true;
    try {
      await fetch('/omnivoice/control', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'unload'})});
      $('ovp-situacao').textContent = 'Modelo fora da placa de vídeo. Ele volta sozinho na próxima geração.';
    } catch (_) { $('ovp-situacao').textContent = 'Não consegui liberar a placa.'; }
    $('ov-btn-unload').disabled = false;
    atualizarServico();
  }

  // ------------------------------------------------------------------ modo e origem
  function setModo(modo) {
    estado.modo = modo;
    document.querySelectorAll('.ovp-aba').forEach((a) => a.setAttribute('aria-selected', String(a.dataset.modo === modo)));
    ['clone', 'design', 'auto'].forEach((m) => { $('ovp-modo-' + m).hidden = m !== modo; });
    atualizarSituacao();
  }

  function setOrigem(origem) {
    estado.origem = origem;
    const r = document.querySelector('input[name="ovp-origem"][value="' + origem + '"]');
    if (r) r.checked = true;
    ['biblioteca', 'catalogo', 'arquivo'].forEach((o) => { $('ovp-origem-' + o).hidden = o !== origem; });
    if (!estado.escolhida || estado.escolhida.origem !== origem) mostrarEscolhida(null);
    atualizarSituacao();
  }

  // ------------------------------------------------------------------ biblioteca
  function filtradas() {
    const q = normalizar($('ovp-busca').value);
    const idioma = $('ovp-filtro-idioma').value;
    const fav = (id) => (vozlab().ehFavorita ? vozlab().ehFavorita(id) : false);
    const lista = estado.vozes.filter((v) => (!idioma || v.language === idioma) &&
      (!q || normalizar(v.rotulo + ' ' + (v.original_name || '') + ' ' + (NOME_IDIOMA[v.language] || '')).includes(q)));
    return [...lista.filter((v) => fav(v.id)), ...lista.filter((v) => !fav(v.id))];  // favoritas no topo
  }

  function itemVoz(texto, sub, ativo, aoClicar) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'ovp-item' + (ativo ? ' ativo' : '');
    b.setAttribute('role', 'option');
    b.setAttribute('aria-selected', String(ativo));
    const s = document.createElement('strong'); s.textContent = texto;
    const span = document.createElement('span'); span.textContent = sub;
    b.append(s, span);
    b.addEventListener('click', aoClicar);
    return b;
  }

  function renderBiblioteca() {
    const lista = filtradas();
    const mostrar = lista.slice(0, estado.limite);
    const fav = (id) => (vozlab().ehFavorita ? vozlab().ehFavorita(id) : false);
    $('ovp-lista').replaceChildren(...mostrar.map((v) => itemVoz((fav(v.id) ? '★ ' : '') + v.rotulo,
      (NOME_IDIOMA[v.language] || (v.language || '?').toUpperCase()) + (v.has_reference_audio ? '' : ' · sem amostra'),
      estado.escolhida && estado.escolhida.origem === 'biblioteca' && estado.escolhida.id === v.id,
      () => escolherBiblioteca(v))));
    $('ovp-contagem').textContent = lista.length + (lista.length === 1 ? ' voz' : ' vozes') +
      (lista.length > mostrar.length ? ' · mostrando ' + mostrar.length : '') +
      (lista.length !== estado.vozes.length ? ' (de ' + estado.vozes.length + ')' : '');
    $('ovp-mais').hidden = lista.length <= mostrar.length;
    $('ovp-mais').textContent = 'Mostrar mais ' + Math.min(LIMITE_PASSO, lista.length - mostrar.length) + ' vozes';
    $('ovp-busca').placeholder = 'Buscar entre ' + estado.vozes.length + ' vozes…';
  }

  function escolherBiblioteca(v) {
    if (!v.has_reference_audio) { $('ovp-situacao').textContent = 'Esta voz não tem amostra de áudio para clonar.'; return; }
    mostrarEscolhida({origem: 'biblioteca', id: v.id, nome: v.rotulo, idioma: v.language,
      ouvir: '/profiles/' + encodeURIComponent(v.id) + '/sample-audio', refTexto: v.reference_text || '',
      tags: [NOME_IDIOMA[v.language] || v.language, 'Biblioteca do VozLab']});
    // O idioma do texto é escolha de quem escreve: escolher a voz não o troca mais (21/10/2026: uma voz
    // alemã trocava "Português" por "Alemão" sem avisar, e o OmniVoice falava alemão). Só avisa a diferença.
    renderBiblioteca();
  }

  async function carregarBiblioteca() {
    try {
      const d = await getJson('/omnivoice/voices');
      estado.vozes = (d.voices || []).filter((v) => v.id !== 'ov_design')
        .map((v) => ({...v, rotulo: rotulo(v.name)}))
        .sort((a, b) => a.rotulo.localeCompare(b.rotulo, 'pt-BR'));
      const idiomas = [...new Set(estado.vozes.map((v) => v.language).filter(Boolean))]
        .sort((a, b) => (NOME_IDIOMA[a] || a).localeCompare(NOME_IDIOMA[b] || b, 'pt-BR'));
      $('ovp-filtro-idioma').replaceChildren(new Option('Todos os idiomas', ''),
        ...idiomas.map((c) => new Option((NOME_IDIOMA[c] || c.toUpperCase()) + ' (' +
          estado.vozes.filter((v) => v.language === c).length + ')', c)));
      renderBiblioteca();
      if (estado.vozPendente) { const id = estado.vozPendente; estado.vozPendente = null; usarVoz(id); }
    } catch (e) {
      $('ovp-contagem').textContent = 'Não consegui carregar as vozes da biblioteca.';
    }
  }

  // ------------------------------------------------------------------ catálogo e arquivo
  async function carregarCatalogo() {
    try {
      const d = await getJson('/omnivoice/catalogo');
      estado.catalogo = d.vozes || [];
    } catch (_) { estado.catalogo = []; }
    renderCatalogo();
  }

  function renderCatalogo() {
    const el = $('ovp-catalogo');
    if (!estado.catalogo.length) {
      const p = document.createElement('p'); p.className = 'field-help';
      p.textContent = 'Nenhuma voz no catálogo (pasta catalogo_vozes do OmniVoice).';
      el.replaceChildren(p);
      return;
    }
    el.replaceChildren(...estado.catalogo.map((v) => itemVoz(v.nome, v.arquivo,
      estado.escolhida && estado.escolhida.origem === 'catalogo' && estado.escolhida.id === v.nome,
      () => { mostrarEscolhida({origem: 'catalogo', id: v.nome, nome: v.nome,
        ouvir: '/omnivoice/catalogo/audio/' + encodeURIComponent(v.nome), refTexto: v.texto_referencia || '',
        tags: ['Catálogo do OmniVoice']}); renderCatalogo(); })));
  }

  async function enviarArquivo(file) {
    if (!file) return;
    const st = $('ovp-arquivo-status');
    st.textContent = 'Enviando ' + file.name + '…';
    const fd = new FormData();
    fd.append('arquivo', file);
    try {
      const r = await fetch('/omnivoice/referencia', {method: 'POST', body: fd});
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
      st.textContent = file.name + ' · ' + d.duracao_s + ' s' + (d.duracao_s > 12 ? ' (passa de 12 s: o OmniVoice usa os primeiros ~10 s)' : '');
      mostrarEscolhida({origem: 'arquivo', id: d.id, nome: file.name, ouvir: '/omnivoice/referencia/' + d.id + '/audio',
        refTexto: '', tags: ['Arquivo enviado', d.duracao_s + ' s']});
    } catch (e) {
      st.textContent = 'Não consegui usar este arquivo: ' + (e.message || e);
    }
  }

  function mostrarEscolhida(v) {
    estado.escolhida = v;
    const card = $('ovp-escolhida');
    const player = $('ov-ref-player');
    player.pause(); player.removeAttribute('src'); player.hidden = true;
    card.hidden = !v;
    if (v) {
      $('ovp-escolhida-nome').textContent = v.nome;
      $('ovp-escolhida-tags').replaceChildren(...(v.tags || []).filter(Boolean).map((t) => {
        const s = document.createElement('span'); s.textContent = t; return s;
      }));
      $('ovp-ref-texto').value = v.refTexto || '';
    }
    avisarIdioma();
    atualizarSituacao();
  }

  function avisarIdioma() {
    const voz = estado.escolhida && estado.escolhida.idioma;
    const texto = $('ovp-idioma').value;
    const diferente = Boolean(voz && texto && texto !== 'auto' && voz !== texto && NOME_IDIOMA[voz]);
    $('ovp-idioma-aviso').hidden = !diferente;
    if (diferente) {
      $('ovp-idioma-aviso').textContent = 'A voz é de ' + NOME_IDIOMA[voz].toLowerCase() + ' e o texto vai sair em ' +
        (NOME_IDIOMA[texto] || texto).toLowerCase() + '. Pode ficar com sotaque; troque o idioma do texto se quiser.';
    }
  }

  // ------------------------------------------------------------------ ajustes
  const ajustesIds = [['passos', 0], ['cfg', 1], ['velocidade', 2], ['tshift', 2], ['tpos', 1], ['tsom', 2], ['camada', 1], ['divtam', 0], ['divlim', 0]];
  function atualizarAjustesTexto() {
    ajustesIds.forEach(([id, casas]) => {
      const v = Number($('ovp-' + id).value);
      $('ovp-' + id + '-v').textContent = id === 'velocidade' ? v.toFixed(2) + 'x' : v.toFixed(casas);
    });
    const mudou = ajustesIds.some(([id]) => Number($('ovp-' + id).value) !== PADRAO[id]) ||
      $('ovp-duracao').value || $('ovp-semente').value ||
      !$('ovp-ruido').checked || !$('ovp-prepara').checked || !$('ovp-silencios').checked;
    $('ovp-ajustes-resumo').textContent = mudou ? '(alterados)' : '(padrão)';
    document.querySelectorAll('.ovp-pilula').forEach((p) => p.classList.toggle('ativo', Number(p.dataset.passos) === Number($('ovp-passos').value)));
  }
  function voltarPadrao() {
    ajustesIds.forEach(([id]) => { $('ovp-' + id).value = PADRAO[id]; });
    $('ovp-duracao').value = ''; $('ovp-semente').value = '';
    $('ovp-ruido').checked = true; $('ovp-prepara').checked = true; $('ovp-silencios').checked = true;
    atualizarAjustesTexto();
  }
  function ajustes() {
    const n = (id) => Number($('ovp-' + id).value);
    const semente = $('ovp-semente').value.trim();
    const duracao = $('ovp-duracao').value.trim();
    return {
      num_step: n('passos'), guidance_scale: n('cfg'), speed: n('velocidade'),
      duration: duracao ? Number(duracao) : null, seed: semente !== '' ? Math.trunc(Number(semente)) : null,
      denoise: $('ovp-ruido').checked, preprocess_prompt: $('ovp-prepara').checked, postprocess_output: $('ovp-silencios').checked,
      t_shift: n('tshift'), position_temperature: n('tpos'), class_temperature: n('tsom'), layer_penalty_factor: n('camada'),
      audio_chunk_duration: n('divtam'), audio_chunk_threshold: n('divlim'),
    };
  }

  // ------------------------------------------------------------------ gerar
  function atualizarSituacao() {
    if (estado.gerando) return;
    const texto = $('ovp-texto').value.trim();
    let msg = 'Pronto para gerar.';
    if (estado.modo === 'clone' && !estado.escolhida) msg = 'Escolha uma voz (biblioteca, catálogo ou seu arquivo).';
    else if (!texto) msg = 'Escreva o texto que a voz vai falar.';
    $('ovp-situacao').textContent = msg;
  }

  function instrucaoDesign() {
    return [$('ovp-genero').value, $('ovp-idade').value, $('ovp-tom').value, $('ovp-estilo').value, $('ovp-sotaque').value,
      $('ovp-instrucao').value.trim()].filter(Boolean).join(', ');
  }

  async function gerar() {
    if (estado.gerando) return;
    const texto = $('ovp-texto').value.trim();
    if (!texto) { $('ovp-situacao').textContent = 'Escreva o texto que a voz vai falar.'; $('ovp-texto').focus(); return; }
    if (estado.modo === 'clone' && !estado.escolhida) { $('ovp-situacao').textContent = 'Escolha uma voz para clonar.'; return; }
    const pausa = (document.querySelector('input[name="ovp-pausa"]:checked') || {}).value || 'padrao';
    const pedido = {modo: estado.modo, texto, idioma: $('ovp-idioma').value, ajustes: ajustes(), pausas: PAUSAS[pausa]};
    if (estado.modo === 'clone') {
      const v = estado.escolhida;
      pedido.origem = v.origem;
      if (v.origem === 'biblioteca') pedido.voz_id = v.id;
      if (v.origem === 'catalogo') pedido.catalogo_nome = v.id;
      if (v.origem === 'arquivo') pedido.referencia_id = v.id;
      pedido.texto_referencia = $('ovp-ref-texto').value.trim() || null;
    } else if (estado.modo === 'design') {
      pedido.instrucao = instrucaoDesign() || null;
      if (!pedido.instrucao) pedido.modo = 'auto';
    }
    $('ovp-gerar').disabled = true;
    $('ovp-cancelar').hidden = false;
    $('ovp-resultado').hidden = true;
    $('ovp-situacao').textContent = 'Enviando…';
    const t0 = Date.now();
    try {
      const r = await fetch('/omnivoice/painel/gerar', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(pedido)});
      const d = await r.json();
      if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'HTTP ' + r.status);
      estado.gerando = d.id;
      // Sem limite de tempo (antes a tela desistia em ~135 s): espera até pronto, falhou ou cancelado.
      for (;;) {
        await new Promise((res) => setTimeout(res, 1000));
        const e = await getJson('/omnivoice/painel/' + d.id);
        const s = Math.round((Date.now() - t0) / 1000);
        if (e.situacao === 'pronto') { mostrarResultado(e, pedido); break; }
        if (e.situacao === 'falhou') throw new Error(e.erro || 'falhou');
        if (e.situacao === 'cancelado') { $('ovp-situacao').textContent = 'Geração cancelada.'; break; }
        const blocos = e.blocos_total > 1 ? ' · bloco ' + Math.min(e.blocos_prontos + 1, e.blocos_total) + ' de ' + e.blocos_total : '';
        const fase = e.situacao === 'preparando' ? 'Preparando (liga o serviço e carrega o modelo se preciso)' : 'Gerando';
        $('ovp-situacao').textContent = fase + blocos + ' · ' + s + ' s';
      }
    } catch (err) {
      $('ovp-situacao').textContent = 'Erro: ' + (err.message || err);
    } finally {
      estado.gerando = null;
      $('ovp-gerar').disabled = false;
      $('ovp-cancelar').hidden = true;
      atualizarServico();
    }
  }

  async function cancelar() {
    if (!estado.gerando) return;
    $('ovp-cancelar').disabled = true;
    $('ovp-situacao').textContent = 'Cancelando…';
    try { await fetch('/omnivoice/painel/' + estado.gerando + '/cancelar', {method: 'POST'}); } catch (_) { /* a espera mostra */ }
    $('ovp-cancelar').disabled = false;
  }

  function mostrarResultado(e, pedido) {
    const url = '/omnivoice/painel/' + e.id + '/audio';
    const player = $('ov-result-player');
    player.src = url;
    player.play().catch(() => {});
    $('ovp-resultado').hidden = false;
    const blocos = e.blocos_total > 1 ? ' · ' + e.blocos_total + ' blocos' : '';
    $('ovp-resultado-info').textContent = e.duracao_s + ' s de áudio em ' + e.segundos + ' s' + blocos;
    $('ovp-baixar').href = url;
    $('ovp-baixar').download = 'omnivoice_' + e.id + '.wav';
    $('ovp-situacao').textContent = 'Pronto: ' + e.duracao_s + ' s de áudio.';
    const voz = pedido.modo === 'clone' ? (estado.escolhida && estado.escolhida.nome) : (pedido.modo === 'design' ? 'Voz criada' : 'Voz automática');
    estado.sessao.unshift({id: e.id, texto: pedido.texto, voz, duracao: e.duracao_s,
      hora: new Date().toLocaleTimeString('pt-BR', {hour: '2-digit', minute: '2-digit'}), url});
    renderSessao();
  }

  function renderSessao() {
    const el = $('ovp-historico');
    if (!estado.sessao.length) return;
    el.replaceChildren(...estado.sessao.map((it) => {
      const card = document.createElement('div'); card.className = 'ovp-sessao-item';
      const t = document.createElement('strong'); t.textContent = it.texto.length > 80 ? it.texto.slice(0, 80) + '…' : it.texto;
      const meta = document.createElement('span'); meta.className = 'field-help';
      meta.textContent = (it.voz || '') + ' · ' + it.duracao + ' s · ' + it.hora;
      const a = document.createElement('audio'); a.className = 'audio-player'; a.controls = true; a.preload = 'none'; a.src = it.url;
      const link = document.createElement('a'); link.className = 'small-link'; link.href = it.url;
      link.download = 'omnivoice_' + it.id + '.wav'; link.textContent = 'Baixar WAV';
      card.append(t, meta, a, link);
      return card;
    }));
  }

  // ------------------------------------------------------------------ voz vinda da aba Vozes
  function usarVoz(profileId) {
    if (!estado.vozes.length) { estado.vozPendente = profileId; return; }
    const v = estado.vozes.find((x) => x.id === profileId);
    if (!v) return;
    setModo('clone');
    setOrigem('biblioteca');
    $('ovp-busca').value = '';
    $('ovp-filtro-idioma').value = '';
    const pos = filtradas().findIndex((x) => x.id === profileId);
    estado.limite = Math.max(LIMITE_PASSO, pos + 1);
    escolherBiblioteca(v);
    const ativo = $('ovp-lista').querySelector('.ovp-item.ativo');
    if (ativo) ativo.scrollIntoView({block: 'nearest'});
    $('ovp-texto').focus({preventScroll: true});
  }

  // ------------------------------------------------------------------ início
  function init() {
    if (estado.iniciado) { atualizarServico(); return; }
    estado.iniciado = true;
    $('ovp-idioma').replaceChildren(...IDIOMAS.map(([c, n]) => new Option(n, c)));
    $('ovp-idioma').value = 'pt';
    document.querySelectorAll('.ovp-aba').forEach((a) => a.addEventListener('click', () => setModo(a.dataset.modo)));
    document.querySelectorAll('input[name="ovp-origem"]').forEach((r) => r.addEventListener('change', () => setOrigem(r.value)));
    $('ovp-busca').addEventListener('input', () => { estado.limite = LIMITE_PASSO; renderBiblioteca(); });
    $('ovp-filtro-idioma').addEventListener('change', () => { estado.limite = LIMITE_PASSO; renderBiblioteca(); });
    $('ovp-idioma').addEventListener('change', avisarIdioma);
    $('ovp-mais').addEventListener('click', () => { estado.limite += LIMITE_PASSO; renderBiblioteca(); });
    $('ovp-arquivo').addEventListener('change', (e) => enviarArquivo(e.target.files && e.target.files[0]));
    $('ovp-ouvir').addEventListener('click', () => {
      if (!estado.escolhida) return;
      const p = $('ov-ref-player'); p.src = estado.escolhida.ouvir; p.hidden = false; p.play().catch(() => {});
    });
    document.querySelectorAll('.ovp-chip').forEach((c) => c.addEventListener('click', () => {
      $('ovp-genero').value = c.dataset.genero || ''; $('ovp-idade').value = c.dataset.idade || '';
      $('ovp-tom').value = c.dataset.tom || ''; $('ovp-estilo').value = c.dataset.estilo || '';
    }));
    document.querySelectorAll('.ovp-exemplo').forEach((c) => c.addEventListener('click', () => {
      $('ovp-texto').value = c.dataset.texto; $('ovp-texto').dispatchEvent(new Event('input'));
    }));
    $('ovp-texto').addEventListener('input', () => {
      $('ovp-contador').textContent = [...$('ovp-texto').value.trim()].length.toLocaleString('pt-BR') + ' / 50.000 caracteres';
      atualizarSituacao();
    });
    ajustesIds.forEach(([id]) => $('ovp-' + id).addEventListener('input', atualizarAjustesTexto));
    ['ovp-duracao', 'ovp-semente', 'ovp-ruido', 'ovp-prepara', 'ovp-silencios'].forEach((id) => $(id).addEventListener('input', atualizarAjustesTexto));
    document.querySelectorAll('.ovp-pilula').forEach((p) => p.addEventListener('click', () => { $('ovp-passos').value = p.dataset.passos; atualizarAjustesTexto(); }));
    $('ovp-padrao').addEventListener('click', voltarPadrao);
    $('ovp-gerar').addEventListener('click', gerar);
    $('ovp-cancelar').addEventListener('click', cancelar);
    $('ov-btn-start').addEventListener('click', ligar);
    $('ov-btn-unload').addEventListener('click', liberar);
    $('ov-btn-carregar').addEventListener('click', carregar);
    atualizarAjustesTexto();
    atualizarServico();
    carregarBiblioteca();
    carregarCatalogo();
    window.addEventListener('vb-favoritos', () => { if (estado.vozes.length) renderBiblioteca(); });
    estado.timerStatus = setInterval(() => {
      if (!document.hidden && location.hash === '#omnivoice' && !estado.gerando) atualizarServico();
    }, 5000);
  }

  window.VBOmniVoice = {init, usarVoz};
  // Inicia ao abrir a aba por qualquer caminho (link, botão do menu, recarregar, ou pedido feito antes deste arquivo carregar)
  const talvezIniciar = () => { if (location.hash === '#omnivoice' || window.__vbOmniVoicePendente) init(); };
  window.addEventListener('hashchange', talvezIniciar);
  talvezIniciar();
})();
