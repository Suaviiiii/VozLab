(() => {
  'use strict';

  const isOvDisabled = localStorage.getItem('voicebox_disable_omnivoice') === 'true' || location.search.includes('no_omnivoice');
  const pages = new Set(['generate', 'voices', ...(isOvDisabled ? [] : ['omnivoice']), 'importar', 'models', 'settings', 'guia']);
  window.toggleOmniVoice = (enabled) => {
    localStorage.setItem('voicebox_disable_omnivoice', enabled ? 'false' : 'true');
    location.reload();
  };
  window.addEventListener('DOMContentLoaded', () => {
    if (isOvDisabled) {
      const ovBtn = document.querySelector('.nav-item-omnivoice');
      if (ovBtn) ovBtn.style.display = 'none';
    }
  });
  const labels = {
    generate: 'Generate', voices: 'Voices', omnivoice: 'OmniVoice 🌍', importar: 'Importar vozes',
    models: 'Models', settings: 'Settings', guia: 'Guia'
  };
  const $ = (id) => document.getElementById(id);
  let profiles = [];
  // Favoritas (15/10/2026): ficam no topo da Generate e das outras listas. Guardadas em config/favoritos.json.
  let favoritas = new Set();
  const ehFavorita = (id) => favoritas.has(id);
  async function alternarFavorita(profile) {
    const marcar = !favoritas.has(profile.id);
    if (marcar) favoritas.add(profile.id); else favoritas.delete(profile.id);
    atualizarTelasFavoritas();
    try {
      const r = await fetch('/favoritos/' + encodeURIComponent(profile.id), {method: marcar ? 'PUT' : 'DELETE'});
      if (!r.ok) throw new Error('HTTP ' + r.status);
      favoritas = new Set((await r.json()).ids || []);
    } catch {
      if (marcar) favoritas.delete(profile.id); else favoritas.add(profile.id);  // desfaz se não gravou
    }
    atualizarTelasFavoritas();
  }
  function atualizarTelasFavoritas() {
    renderProfiles();
    try { renderLibrary(); } catch { /* aba Vozes ainda não montada */ }
    if (selected) marcarEstrela($('selected-fav'), selected);
    if (librarySelected) marcarEstrela($('library-fav'), librarySelected);
    window.dispatchEvent(new CustomEvent('vb-favoritos'));
  }
  function marcarEstrela(botao, profile) {
    if (!botao || !profile) return;
    const fav = ehFavorita(profile.id);
    botao.hidden = false;
    botao.textContent = fav ? '★' : '☆';
    botao.setAttribute('aria-pressed', String(fav));
    botao.title = fav ? 'Tirar das favoritas' : 'Marcar como favorita (fica no topo da lista)';
    botao.setAttribute('aria-label', (fav ? 'Tirar das favoritas: ' : 'Favoritar: ') + displayName(profile));
  }
  function botaoEstrela(profile) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'fav-toggle';
    marcarEstrela(b, profile);
    b.addEventListener('click', (e) => { e.stopPropagation(); alternarFavorita(profile); });
    return b;
  }
  function tituloLista(texto) {
    const t = document.createElement('p');
    t.className = 'lista-titulo';
    t.textContent = texto;
    return t;
  }
  const favoritasPrimeiro = (lista) => [...lista.filter((p) => ehFavorita(p.id)), ...lista.filter((p) => !ehFavorita(p.id))];
  let voiceAliases = {};
  let selected = null;
  let librarySelected = null;
  let libraryLimit = 60;
  let generating = false;
  const languageNames = {
    ar:'Árabe', da:'Dinamarquês', de:'Alemão', el:'Grego', en:'Inglês', es:'Espanhol',
    fi:'Finlandês', fr:'Francês', he:'Hebraico', hi:'Hindi', it:'Italiano', ja:'Japonês',
    ko:'Coreano', ms:'Malaio', nl:'Holandês', no:'Norueguês', pl:'Polonês', pt:'Português',
    ru:'Russo', sv:'Sueco', sw:'Suaíli', tr:'Turco', zh:'Chinês'
  };
  const sourceName = (profile) => {
    const found = String(profile.description || '').match(/Fonte:\s*([^.(]+)/i);
    return found ? found[1].trim() : 'Não informada';
  };
  const genderName = (personality) => ({FEMALE:'Feminina', MALE:'Masculina'})[personality] || 'Não informado';
  const hasCatalogName = (profile) => !/^\[[^\]]+\]\s+(Feminino|Masculino)\s+-\s+/i.test(profile.name || '');
  // Item 3 do usuário (OV-7): a sigla do idioma entre colchetes no nome da voz vira o nome em português
  // ([DE] e [ALE] -> [Alemão], [EN] -> [Inglês]...). Só a exibição muda: o banco e os apelidos ficam iguais.
  const SIGLAS_IDIOMA = {
    DE: 'Alemão', ALE: 'Alemão', EN: 'Inglês', ING: 'Inglês', ES: 'Espanhol', ESP: 'Espanhol', FR: 'Francês',
    JA: 'Japonês', JP: 'Japonês', IT: 'Italiano', RU: 'Russo', NO: 'Norueguês', PL: 'Polonês', PT: 'Português',
    BR: 'Português BR', ZH: 'Chinês', KO: 'Coreano', AR: 'Árabe', HI: 'Hindi', NL: 'Holandês', TR: 'Turco',
    SV: 'Sueco', DA: 'Dinamarquês', FI: 'Finlandês', EL: 'Grego', HE: 'Hebraico', MS: 'Malaio', SW: 'Suaíli'
  };
  const rotuloNome = (nome) => String(nome || '').replace(/\[([A-Za-z]{2,3})\]/g,
    (m, sigla) => (SIGLAS_IDIOMA[sigla.toUpperCase()] ? '[' + SIGLAS_IDIOMA[sigla.toUpperCase()] + ']' : m));
  const displayName = (profile) => rotuloNome(voiceAliases[profile.id] || profile.name);
  window.VBVozlab = {rotuloNome, displayName, languageNames, ehFavorita: (id) => favoritas.has(id)};

  function showPage(id) {
    const page = pages.has(id) ? id : 'generate';
    if (page !== 'voices') $('library-player').pause();
    if (page !== 'generate') $('reference-player').pause();
    if (page !== 'omnivoice') {
      const op = $('ov-ref-player'); if (op) op.pause();
      const rp = $('ov-result-player'); if (rp) rp.pause();
    } else {
      initOmniVoice();
    }
    document.querySelectorAll('.nav-item').forEach((button) => {
      const active = button.dataset.view === page;
      button.classList.toggle('active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
    document.querySelectorAll('.view').forEach((view) => { view.hidden = view.id !== 'view-' + page; });
    if (page === 'models') loadModels();
    if (page === 'settings') loadSettings();
    $('page-title').textContent = labels[page];
    document.title = 'VozLab · ' + labels[page];
  }

  document.querySelectorAll('.nav-item').forEach((button) => button.addEventListener('click', () => {
    location.hash = button.dataset.view;
    showPage(button.dataset.view);
    $('page').scrollTop = 0;
    $('page').focus({preventScroll: true});
  }));
  window.addEventListener('hashchange', () => showPage(location.hash.slice(1)));

  // OV-2: aviso discreto de quem está com a placa de vídeo (VozLab ou OmniVoice). Só aparece quando importa.
  async function atualizarAvisoGpu() {
    const pagina = location.hash.slice(1) || 'generate';
    const avGen = $('gpu-aviso-generate');
    const avOv = $('gpu-aviso-ov');
    if (document.hidden || !['generate', 'omnivoice'].includes(pagina)) return;
    let e = null;
    try { e = await getJson('/omnivoice/gpu/estado'); } catch (_) { e = null; }
    const ov = e && e.omnivoice;
    let msgGen = '';
    let msgOv = '';
    if (e) {
      if (e.vozlab && e.vozlab.aguardando_omnivoice) msgGen = '⏳ Aguardando o OmniVoice terminar para usar a placa de vídeo.';
      else if (ov && ov.gerando) msgGen = 'O OmniVoice está gerando na placa de vídeo. Se você gerar aqui, o VozLab espera ele terminar.';
      else if (ov && ov.modelo_carregado) {
        const min = ov.descarrega_em_s != null ? Math.ceil(ov.descarrega_em_s / 60) : null;
        msgGen = 'O OmniVoice está com a placa de vídeo' + (min != null ? ' (libera sozinho em ' + min + ' min)' : '') + '. Ao gerar aqui, ele cede a placa.';
      }
      if (ov && ov.aguardando === 'vozlab') msgOv = '⏳ Aguardando o VozLab terminar para usar a placa de vídeo.';
      else if (e.vozlab_gerando) msgOv = 'O VozLab está gerando na placa de vídeo. O OmniVoice gera quando ele terminar.';
    }
    if (avGen) { avGen.textContent = msgGen; avGen.hidden = !msgGen; }
    if (avOv) { avOv.textContent = msgOv; avOv.hidden = !msgOv; }
  }
  setInterval(atualizarAvisoGpu, 5000);
  window.addEventListener('hashchange', atualizarAvisoGpu);
  $('rail-toggle').addEventListener('click', () => {
    const expanded = $('app').classList.toggle('nav-expanded');
    $('rail-toggle').setAttribute('aria-expanded', String(expanded));
    $('rail-toggle').setAttribute('aria-label', expanded ? 'Recolher menu' : 'Expandir menu');
    $('rail-toggle').title = expanded ? 'Recolher menu' : 'Expandir menu';
  });
  $('filters-toggle').addEventListener('click', () => {
    const expanded = $('voice-filters').hidden;
    $('voice-filters').hidden = !expanded;
    $('filters-toggle').setAttribute('aria-expanded', String(expanded));
  });

  async function getJson(path) {
    const response = await fetch(path, {cache: 'no-store'});
    if (!response.ok) throw new Error(path + ': HTTP ' + response.status);
    return response.json();
  }

  const serverDown = 'Não foi possível falar com o servidor. Confira se o VozLab está aberto.';
  const fmtGB = (mb) => (mb / 1024).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' GB';

  async function loadModels() {
    const list = $('models-list');
    $('models-status').textContent = 'Carregando modelos…';
    try {
      const data = await getJson('/models/status');
      // Downloads (15/10/2026): mostra também os que faltam, com tamanho aproximado e botão Baixar.
      const models = [...(data.models || []).filter((m) => m.downloaded || m.loaded),
        ...(data.models || []).filter((m) => !(m.downloaded || m.loaded))];
      const instalados = models.filter((m) => m.downloaded || m.loaded).length;
      list.replaceChildren();
      $('models-status').textContent = instalados + ' de ' + models.length + ' modelos neste PC · ' +
        models.filter((m) => m.loaded).length + ' na memória agora.';
      models.forEach((m) => {
        if (!(m.downloaded || m.loaded)) { list.append(linhaParaBaixar(m)); return; }
        const row = document.createElement('li');
        const name = document.createElement('strong');
        name.textContent = m.display_name || m.model_name;
        const meta = document.createElement('span');
        meta.className = 'field-help';
        const sp = situacaoModelo(m.model_name);
        meta.textContent = [m.loaded ? 'Na memória' : 'Instalado, fora da memória',
          Number.isFinite(m.size_mb) ? fmtGB(m.size_mb) + ' no disco' : '',
          sp && sp.situacao !== 'ok' ? '⚠ ' + (sp.motivo || sp.situacao) : ''].filter(Boolean).join(' · ');
        const text = document.createElement('div');
        text.append(name, meta);
        row.append(text);
        row.dataset.loaded = String(Boolean(m.loaded));
        if (m.loaded) {
          const unload = document.createElement('button');
          unload.type = 'button';
          unload.className = 'secondary-button';
          unload.textContent = 'Tirar da memória';
          unload.addEventListener('click', async () => {
            unload.disabled = true;
            $('models-status').textContent = 'Tirando ' + name.textContent + ' da memória…';
            try {
              const r = await fetch('/models/' + encodeURIComponent(m.model_name) + '/unload', {method: 'POST'});
              if (!r.ok) throw new Error('O servidor respondeu com erro ' + r.status + '.');
              await loadModels();
              const h = await getJson('/health').catch(() => null);
              if (h && typeof h.vram_used_mb === 'number') $('vram-label').textContent = fmtGB(h.vram_used_mb) + ' alocados pelo VozLab.';
            } catch (error) {
              $('models-status').textContent = error instanceof TypeError ? serverDown : error.message;
              unload.disabled = false;
            }
          });
          row.append(unload);
        }
        list.append(row);
      });
    } catch (error) {
      list.replaceChildren();
      $('models-status').textContent = error instanceof TypeError ? serverDown : 'Não foi possível listar os modelos.';
    }
  }

  // Tamanho aproximado do download (medido em 15/10/2026; os que não estavam no PC são estimativa).
  const TAMANHO_DOWNLOAD_MB = {
    'qwen-tts-1.7B': 4330, 'qwen-tts-0.6B': 2400, 'qwen-custom-voice-1.7B': 4310, 'qwen-custom-voice-0.6B': 2380,
    'luxtts': 300, 'chatterbox-tts': 3060, 'chatterbox-turbo': 3860, 'f5-tts': 2570, 'kokoro': 315,
    'whisper-base': 280, 'whisper-small': 925, 'whisper-medium': 2920, 'whisper-large': 2950, 'whisper-turbo': 1550,
    'qwen3-0.6b': 1450, 'qwen3-1.7b': 3890, 'qwen3-4b': 7690
  };
  function linhaParaBaixar(m) {
    const row = document.createElement('li');
    row.dataset.loaded = 'false';
    row.className = 'modelo-faltando';
    const name = document.createElement('strong');
    name.textContent = m.display_name || m.model_name;
    const meta = document.createElement('span');
    meta.className = 'field-help';
    const tam = TAMANHO_DOWNLOAD_MB[m.model_name];
    meta.textContent = 'Não baixado' + (tam ? ' · ~' + fmtGB(tam) + ' para baixar' : '');
    const text = document.createElement('div');
    text.append(name, meta);
    if (m.hf_repo_id) {
      const link = document.createElement('a');
      link.href = 'https://huggingface.co/' + m.hf_repo_id;
      link.target = '_blank';
      link.rel = 'noopener';
      link.className = 'field-help';
      link.textContent = 'Página do modelo (Hugging Face): ' + m.hf_repo_id;
      text.append(link);
    }
    const baixar = document.createElement('button');
    baixar.type = 'button';
    baixar.className = 'secondary-button';
    baixar.textContent = m.downloading ? 'Baixando…' : 'Baixar';
    baixar.disabled = Boolean(m.downloading);
    baixar.addEventListener('click', async () => {
      baixar.disabled = true;
      baixar.textContent = 'Baixando…';
      try {
        const r = await fetch('/models/' + encodeURIComponent(m.model_name) + '/baixar', {method: 'POST'});
        const d = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(typeof d.detail === 'string' ? d.detail : 'O servidor respondeu com erro ' + r.status + '.');
        acompanharDownload(m, meta, baixar);
      } catch (error) {
        meta.textContent = error instanceof TypeError ? serverDown : error.message;
        baixar.disabled = false;
        baixar.textContent = 'Tentar de novo';
      }
    });
    if (m.downloading) acompanharDownload(m, meta, baixar);
    row.append(text, baixar);
    return row;
  }
  function acompanharDownload(m, meta, botao) {
    const t0 = Date.now();
    const timer = setInterval(async () => {
      let p = {};
      try { p = await getJson('/models/' + encodeURIComponent(m.model_name) + '/progresso'); } catch { return; }
      const seg = Math.round((Date.now() - t0) / 1000);
      if (p.status === 'complete') {
        clearInterval(timer);
        meta.textContent = 'Baixado e carregado na placa.';
        loadModels();
      } else if (p.status === 'error') {
        clearInterval(timer);
        const falta = /No module named '([^']+)'/.exec(p.error || '');
        meta.textContent = falta
          ? 'Este modelo precisa de um componente do Python que não está instalado (' + falta[1] + '). Nada foi apagado; ele só funciona depois de instalar esse componente.'
          : 'O download falhou: ' + (p.error || p.filename || 'erro desconhecido') + '. Nada foi apagado; pode tentar de novo.';
        botao.disabled = false;
        botao.textContent = 'Tentar de novo';
      } else {
        const pct = p.total > 0 ? ' ' + Math.round(p.progress) + '% (' + fmtGB(p.current / 1048576) + ' de ' + fmtGB(p.total / 1048576) + ')' : '';
        meta.textContent = 'Baixando…' + pct + ' · ' + seg + ' s · depois ele é carregado na placa';
      }
    }, 2000);
  }

  async function loadSettings() {
    const dl = $('settings-server');
    const row = (label, value) => {
      const dt = document.createElement('dt'); dt.textContent = label;
      const dd = document.createElement('dd'); dd.textContent = value;
      return [dt, dd];
    };
    const [health, fs, cache] = await Promise.allSettled([
      getJson('/health'), getJson('/health/filesystem'), getJson('/models/cache-dir')
    ]);
    dl.replaceChildren();
    $('connection-dot').className = 'connection-dot ' + (health.status === 'fulfilled' ? 'online' : 'offline');
    $('connection-label').textContent = health.status === 'fulfilled' ? 'Servidor conectado' : 'Servidor indisponível';
    if (health.status === 'fulfilled') {
      const h = health.value;
      const mb = h.vram_used_mb;
      dl.append(...row('Estado', h.status === 'healthy' ? 'Conectado e respondendo' : 'Respondendo (' + h.status + ')'),
        ...row('Placa de vídeo', h.gpu_available ? (h.gpu_type || 'disponível') : 'não detectada (usando o processador)'),
        ...row('Pacote do servidor', h.backend_variant || h.backend_type || 'não informado'),
        ...row('VRAM em uso pelo VozLab', typeof mb === 'number' ? fmtGB(mb) : 'indisponível'),
        ...row('Modelo principal carregado', h.model_loaded ? 'sim' + (h.model_size ? ' (' + h.model_size + ')' : '') : 'não'));
      if (h.gpu_compatibility_warning) dl.append(...row('Aviso da placa', h.gpu_compatibility_warning));
    } else {
      dl.append(...row('Estado', health.reason instanceof TypeError ? serverDown : 'Servidor fora do ar ou com erro.'));
    }
    if (fs.status === 'fulfilled') {
      const f = fs.value;
      const bad = (f.directories || []).filter((d) => !d.exists || !d.writable);
      $('settings-disk').textContent = (Number.isFinite(f.disk_free_mb) ? 'Espaço livre no disco dos dados: ' + fmtGB(f.disk_free_mb) + '. ' : '') +
        (bad.length ? 'Pastas com problema: ' + bad.map((d) => d.path).join(', ') : 'Pastas de dados acessíveis.');
    } else {
      $('settings-disk').textContent = 'Não foi possível verificar o disco.';
    }
    $('settings-cache').textContent = cache.status === 'fulfilled' ? 'Pasta dos modelos: ' + cache.value.path : '';
    if (window.VBTextoLongo) window.VBTextoLongo.loadStorage($('settings-jobs'));
    $('settings-omnivoice').textContent = isOvDisabled ? 'O painel OmniVoice está escondido do menu.' : 'O painel OmniVoice está visível no menu.';
    $('settings-omnivoice-toggle').textContent = isOvDisabled ? 'Mostrar painel OmniVoice' : 'Esconder painel OmniVoice';
  }
  // Depois de importar um pacote, recarrega a biblioteca nas telas Generate e Voices.
  window.addEventListener('vozlab:vozes-alteradas', () => loadSummary());
  $('models-refresh').addEventListener('click', loadModels);
  $('settings-refresh').addEventListener('click', loadSettings);
  $('settings-omnivoice-toggle').addEventListener('click', () => window.toggleOmniVoice(isOvDisabled));

  function normalized(value) {
    return String(value || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase('pt-BR');
  }

  function renderProfiles(resetScroll = false) {
    const container = $('voice-options');
    const previousScroll = resetScroll ? 0 : container.scrollTop;
    container.replaceChildren();
    const search = normalized($('voice-search').value.trim());
    const language = $('filter-language').value;
    const gender = $('filter-gender').value;
    const source = $('filter-source').value;
    const matches = profiles.filter((profile) =>
      (!language || profile.language === language) &&
      (!gender || profile.personality === gender) &&
      (!source || sourceName(profile) === source) &&
      normalized([displayName(profile), profile.name, profile.language, languageNames[profile.language], profile.description].join(' ')).includes(search)
    );
    const aliased = matches.filter((profile) => Boolean(voiceAliases[profile.id])).length;
    $('profile-count').textContent = matches.length.toLocaleString('pt-BR') + ' de ' +
      profiles.length.toLocaleString('pt-BR') + ' vozes · ' + aliased.toLocaleString('pt-BR') +
      (aliased === 1 ? ' apelido de catálogo' : ' apelidos de catálogo');
    if (!matches.length) {
      const empty = document.createElement('p');
      empty.className = 'secondary';
      empty.textContent = 'Nenhuma voz encontrada.';
      container.append(empty);
      container.scrollTop = 0;
      return;
    }
    const fragment = document.createDocumentFragment();
    const ordenadas = favoritasPrimeiro(matches);
    const nFav = ordenadas.filter((p) => ehFavorita(p.id)).length;
    ordenadas.forEach((profile, i) => {
      if (nFav && i === 0) fragment.append(tituloLista('★ Favoritas (' + nFav + ')'));
      if (nFav && i === nFav) fragment.append(tituloLista('Todas as vozes'));
      const row = document.createElement('div');
      row.className = 'voice-row';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'voice-option';
      button.setAttribute('role', 'option');
      button.setAttribute('aria-selected', String(selected?.id === profile.id));
      const name = document.createElement('strong');
      name.textContent = displayName(profile);
      const meta = document.createElement('span');
      meta.textContent = [languageNames[profile.language] || profile.language?.toUpperCase(), genderName(profile.personality)].filter(Boolean).join(' · ');
      button.append(name, meta);
      button.addEventListener('click', () => selectProfile(profile));
      const preview = document.createElement('button');
      preview.type = 'button';
      preview.className = 'preview-inline';
      preview.setAttribute('aria-label', 'Ouvir amostra de ' + displayName(profile));
      preview.title = 'Ouvir amostra';
      preview.textContent = '▶';
      preview.addEventListener('click', () => { selectProfile(profile); previewProfile(profile); });
      row.append(button, botaoEstrela(profile), preview);
      fragment.append(row);
    });
    container.append(fragment);
    container.scrollTop = previousScroll;
  }

  function selectProfile(profile) {
    selected = profile;
    $('voice-selected').hidden = false;
    $('selected-name').textContent = displayName(profile);
    marcarEstrela($('selected-fav'), profile);
    $('selected-meta').replaceChildren();
    [languageNames[profile.language] || profile.language?.toUpperCase(), genderName(profile.personality), sourceName(profile),
      voiceAliases[profile.id] ? 'Apelido de catálogo' : null].filter(Boolean).forEach((value) => {
      const tag = document.createElement('span');
      tag.textContent = value;
      $('selected-meta').append(tag);
    });
    $('selected-description').textContent = profile.description || 'Sem descrição cadastrada.';
    try { updateQuality(); } catch { /* tela ainda carregando */ }
    $('selected-extra').textContent = (voiceAliases[profile.id] ? 'Identificador original: ' + profile.name + ' · ' : '') +
      (profile.sample_count || 0) + ' amostra(s) · Modelo padrão: ' + (profile.default_engine || 'não informado');
    $('composer-voice').textContent = 'Voz selecionada: ' + displayName(profile);
    $('preview-status').textContent = '';
    $('reference-player').pause();
    $('reference-player').removeAttribute('src');
    $('reference-player').hidden = true;
    $('generation-status').textContent = 'Voz selecionada. Escreva o texto e gere o áudio.';
    $('generate-button').disabled = generating;
    const models = $('gen-model');
    const engine = profile.default_engine;
    const value = engine === 'qwen' || engine === 'qwen_custom_voice' ? engine + ':1.7B' : engine;
    if ([...models.options].some((option) => option.value === value)) models.value = value;
    updateModelWarning();
    atualizarOmniVoiceGerar();
    renderProfiles();
  }

  function renderLibrary(resetScroll = false) {
    const container = $('library-list');
    const previousScroll = resetScroll ? 0 : $('page').scrollTop;
    container.replaceChildren();
    const search = normalized($('library-search').value.trim());
    const language = $('library-language').value;
    const gender = $('library-gender').value;
    const source = $('library-source').value;
    const matches = profiles.filter((profile) =>
      (!language || profile.language === language) &&
      (!gender || profile.personality === gender) &&
      (!source || sourceName(profile) === source) &&
      normalized([displayName(profile), profile.name, profile.language, languageNames[profile.language], profile.description].join(' ')).includes(search)
    );
    if (librarySelected && !matches.some((profile) => profile.id === librarySelected.id)) clearLibrarySelection();
    const visible = favoritasPrimeiro(matches).slice(0, libraryLimit);
    $('library-count').textContent = matches.length
      ? 'Mostrando ' + visible.length.toLocaleString('pt-BR') + ' de ' + matches.length.toLocaleString('pt-BR') + ' vozes encontradas.'
      : 'Nenhuma voz encontrada.';
    $('library-more').hidden = visible.length >= matches.length;
    const fragment = document.createDocumentFragment();
    visible.forEach((profile) => {
      const item = document.createElement('div');
      item.className = 'library-item';
      const choice = document.createElement('button');
      choice.type = 'button';
      choice.className = 'library-choice';
      choice.dataset.profileId = profile.id;
      choice.setAttribute('role', 'option');
      choice.setAttribute('aria-selected', String(librarySelected?.id === profile.id));
      const name = document.createElement('strong');
      name.textContent = displayName(profile);
      const detail = document.createElement('span');
      detail.textContent = [languageNames[profile.language] || profile.language?.toUpperCase(), genderName(profile.personality), sourceName(profile)].filter(Boolean).join(' · ');
      choice.append(name, detail);
      choice.addEventListener('click', () => selectLibraryProfile(profile));
      const estrelaLib = botaoEstrela(profile);
      const preview = document.createElement('button');
      preview.type = 'button';
      preview.className = 'preview-inline';
      preview.setAttribute('aria-label', 'Ouvir amostra de ' + displayName(profile));
      preview.title = 'Ouvir amostra';
      preview.textContent = '▶';
      preview.addEventListener('click', () => { selectLibraryProfile(profile); previewLibraryProfile(profile); });
      item.append(choice, estrelaLib, preview);
      fragment.append(item);
    });
    container.append(fragment);
    $('page').scrollTop = previousScroll;
  }

  function selectLibraryProfile(profile) {
    librarySelected = profile;
    $('library-name').textContent = displayName(profile);
    marcarEstrela($('library-fav'), profile);
    $('library-meta').replaceChildren();
    [languageNames[profile.language] || profile.language?.toUpperCase(), genderName(profile.personality), sourceName(profile),
      voiceAliases[profile.id] ? 'Apelido de catálogo' : null].filter(Boolean).forEach((value) => {
      const tag = document.createElement('span');
      tag.textContent = value;
      $('library-meta').append(tag);
    });
    $('library-description').textContent = profile.description || 'Sem descrição cadastrada.';
    $('library-original').textContent = voiceAliases[profile.id] ? 'Identificador original: ' + profile.name : '';
    $('library-engine').textContent = 'Modelo padrão: ' + (profile.default_engine || 'não informado') +
      ' · ' + (profile.sample_count || 0) + (profile.sample_count === 1 ? ' amostra' : ' amostras');
    $('library-preview').disabled = false;
    $('library-use').disabled = false;
    $('library-use-ov').disabled = !(profile.sample_count > 0);
    $('library-preview-status').textContent = '';
    $('library-player').pause();
    $('library-player').removeAttribute('src');
    $('library-player').hidden = true;
    document.querySelectorAll('#library-list .library-choice').forEach((button) => {
      button.setAttribute('aria-selected', String(button.dataset.profileId === profile.id));
    });
    if (window.matchMedia('(max-width:720px)').matches) $('library-detail').scrollIntoView({behavior: 'smooth', block: 'start'});
  }

  function clearLibrarySelection() {
    librarySelected = null;
    $('library-name').textContent = 'Selecione uma voz';
    if ($('library-fav')) $('library-fav').hidden = true;
    $('library-meta').replaceChildren();
    $('library-description').textContent = 'Escolha uma voz na lista para ver a origem, o modelo e ouvir a amostra.';
    $('library-original').textContent = '';
    $('library-engine').textContent = '';
    $('library-preview').disabled = true;
    $('library-use').disabled = true;
    $('library-use-ov').disabled = true;
    $('library-preview-status').textContent = '';
    $('library-player').pause();
    $('library-player').removeAttribute('src');
    $('library-player').hidden = true;
  }

  async function previewLibraryProfile(profile) {
    if (!profile) return;
    const player = $('library-player');
    player.src = '/profiles/' + encodeURIComponent(profile.id) + '/sample-audio';
    player.hidden = false;
    $('library-preview-status').textContent = 'Carregando amostra…';
    try {
      await player.play();
      $('library-preview-status').textContent = '';
    } catch {
      $('library-preview-status').textContent = 'Não foi possível reproduzir a amostra desta voz.';
    }
  }

  $('library-search').addEventListener('input', () => { libraryLimit = 60; renderLibrary(true); });
  ['library-language', 'library-gender', 'library-source'].forEach((id) => $(id).addEventListener('change', () => {
    libraryLimit = 60;
    renderLibrary(true);
  }));
  $('library-more').addEventListener('click', () => { libraryLimit += 60; renderLibrary(); });
  $('library-preview').addEventListener('click', () => previewLibraryProfile(librarySelected));
  $('library-player').addEventListener('error', () => {
    $('library-preview-status').textContent = 'A amostra desta voz não está disponível.';
  });
  $('library-use-ov').addEventListener('click', () => {
    if (!librarySelected || !window.VBOmniVoice) return;
    window.VBOmniVoice.usarVoz(librarySelected.id);
    location.hash = 'omnivoice';
    showPage('omnivoice');
  });
  $('library-use').addEventListener('click', () => {
    if (!librarySelected) return;
    $('voice-search').value = displayName(librarySelected);
    $('filter-language').value = '';
    $('filter-gender').value = '';
    $('filter-source').value = '';
    selectProfile(librarySelected);
    location.hash = 'generate';
    showPage('generate');
    $('page').scrollTop = 0;
  });

  $('voice-search').addEventListener('input', () => renderProfiles(true));
  ['filter-language', 'filter-gender', 'filter-source'].forEach((id) => $(id).addEventListener('change', () => renderProfiles(true)));
  async function previewProfile(profile) {
    if (!profile) return;
    const player = $('reference-player');
    player.src = '/profiles/' + encodeURIComponent(profile.id) + '/sample-audio';
    player.hidden = false;
    $('preview-status').textContent = 'Carregando amostra…';
    try {
      await player.play();
      $('preview-status').textContent = '';
    } catch {
      $('preview-status').textContent = 'Não foi possível reproduzir a amostra desta voz.';
    }
  }
  $('preview-voice').addEventListener('click', () => previewProfile(selected));
  $('reference-player').addEventListener('error', () => {
    $('preview-status').textContent = 'A amostra desta voz não está disponível.';
  });

  async function loadSummary() {
    $('retry-data').hidden = true;
    $('connection-dot').className = 'connection-dot';
    $('connection-label').textContent = 'Verificando servidor…';
    const [health, profileResult, aliasesResult, favResult] = await Promise.allSettled([
      getJson('/health'), getJson('/profiles'), getJson('/novo/voice-aliases.json'), getJson('/favoritos')
    ]);
    favoritas = new Set(favResult.status === 'fulfilled' ? favResult.value.ids || [] : []);
    voiceAliases = aliasesResult.status === 'fulfilled' && aliasesResult.value?.kind === 'display_only_catalog_aliases'
      ? aliasesResult.value.aliases || {} : {};
    if (health.status === 'fulfilled') {
      $('connection-dot').className = 'connection-dot online';
      $('connection-label').textContent = 'Servidor conectado';
      const mb = health.value.vram_used_mb;
      $('vram-label').textContent = typeof mb === 'number' && Number.isFinite(mb)
        ? (mb / 1024).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' GB alocados pelo VozLab.'
        : 'Memória do VozLab indisponível.';
    } else {
      $('connection-dot').className = 'connection-dot offline';
      $('connection-label').textContent = 'Servidor indisponível';
      $('vram-label').textContent = 'Não foi possível consultar /health.';
      $('retry-data').hidden = false;
    }
    if (profileResult.status === 'fulfilled' && Array.isArray(profileResult.value)) {
      profiles = profileResult.value.sort((a, b) =>
        Number(hasCatalogName(b)) - Number(hasCatalogName(a)) ||
        displayName(a).localeCompare(displayName(b), 'pt-BR')
      );
      const languageFilter = $('filter-language');
      const currentLanguage = languageFilter.value;
      languageFilter.replaceChildren(new Option('Todos', ''));
      [...new Set(profiles.map((p) => p.language).filter(Boolean))].sort((a, b) =>
        (languageNames[a] || a).localeCompare(languageNames[b] || b, 'pt-BR')
      ).forEach((code) => languageFilter.add(new Option(languageNames[code] || code.toUpperCase(), code)));
      languageFilter.value = currentLanguage;
      const libraryLanguage = $('library-language');
      const currentLibraryLanguage = libraryLanguage.value;
      libraryLanguage.replaceChildren(new Option('Todos', ''));
      [...languageFilter.options].slice(1).forEach((option) => libraryLanguage.add(new Option(option.textContent, option.value)));
      libraryLanguage.value = currentLibraryLanguage;
      const sourceFilter = $('filter-source');
      const currentSource = sourceFilter.value;
      sourceFilter.replaceChildren(new Option('Todas', ''));
      [...new Set(profiles.map(sourceName))].sort((a, b) => a.localeCompare(b, 'pt-BR'))
        .forEach((source) => sourceFilter.add(new Option(source, source)));
      sourceFilter.value = currentSource;
      const librarySource = $('library-source');
      const currentLibrarySource = librarySource.value;
      librarySource.replaceChildren(new Option('Todas', ''));
      [...sourceFilter.options].slice(1).forEach((option) => librarySource.add(new Option(option.textContent, option.value)));
      librarySource.value = currentLibrarySource;
      const count = profiles.length.toLocaleString('pt-BR') + ' vozes';
      $('voices-count').textContent = count + ' disponíveis.';
      if (selected) selected = profiles.find((p) => p.id === selected.id) || null;
      if (librarySelected) librarySelected = profiles.find((p) => p.id === librarySelected.id) || null;
      renderProfiles();
      renderLibrary();
      if (selected) selectProfile(selected);
      if (librarySelected) selectLibraryProfile(librarySelected);
      loadHistory();
    } else {
      $('profile-count').textContent = 'Biblioteca indisponível';
      $('voices-count').textContent = 'Não foi possível carregar as vozes.';
      $('retry-data').hidden = false;
    }
  }

  function statusText(status) {
    return {
      pending: 'Na fila', loading_model: 'Carregando modelo',
      generating: 'Gerando', completed: 'Concluído', failed: 'Falhou'
    }[status] || status;
  }

  async function loadHistory() {
    const container = $('history-list');
    try {
      const data = await getJson('/history?limit=8');
      container.replaceChildren();
      historyTotal = data.total || 0;
      $('history-clear-all').hidden = !data.items?.length;
      if (!data.items?.length) {
        const empty = document.createElement('p');
        empty.className = 'secondary';
        empty.textContent = 'Nenhum áudio gerado ainda.';
        container.append(empty);
        return;
      }
      data.items.forEach((item) => {
        const card = document.createElement('div');
        card.className = 'history-item';
        const name = document.createElement('strong');
        const historyProfile = profiles.find((profile) => profile.id === item.profile_id);
        name.textContent = historyProfile ? displayName(historyProfile)
          : (voiceAliases[item.profile_id] || item.profile_name || 'Voz').replace(/\[DE\]/g, '[ALE]');
        const detail = document.createElement('p');
        detail.textContent = statusText(item.status) + ' · ' + (item.text || '').slice(0, 85);
        card.append(name, detail);
        if (item.status === 'completed') {
          const player = document.createElement('audio');
          player.controls = true;
          player.preload = 'none';
          player.src = '/audio/' + encodeURIComponent(item.id);
          player.className = 'audio-player';
          const download = document.createElement('a');
          download.href = '/history/' + encodeURIComponent(item.id) + '/export-audio';
          download.textContent = 'Baixar WAV';
          download.className = 'small-link';
          card.append(player, download);
        } else if (item.status === 'failed' && item.error) {
          const error = document.createElement('p');
          error.className = 'error-text';
          error.textContent = item.error;
          card.append(error);
        }
        if (!['pending', 'loading_model', 'generating'].includes(item.status)) {
          const remove = document.createElement('button');
          remove.type = 'button';
          remove.className = 'history-delete';
          remove.textContent = '✕';
          remove.title = 'Excluir esta geração (apaga o arquivo)';
          remove.setAttribute('aria-label', 'Excluir a geração de ' + name.textContent);
          remove.addEventListener('click', () => askDeleteHistory(item, name.textContent));
          card.append(remove);
        }
        container.append(card);
        // Trabalho em blocos: botão para abrir o painel (some se não for).
        if (window.VBTextoLongo && item.status !== 'pending') {
          window.VBTextoLongo.probe(item.id).then((isLong) => {
            if (!isLong || !card.isConnected) return;
            const open = document.createElement('button');
            open.type = 'button';
            open.className = 'secondary-button lj-open';
            open.textContent = item.status === 'failed' ? '▶ Ver blocos e retomar' : '☰ Ver blocos';
            open.addEventListener('click', async () => {
              $('generation-result').hidden = true;
              await window.VBTextoLongo.open(item.id);
              if (item.status === 'completed') showResult(item.id);
              $('long-job').scrollIntoView({behavior: 'smooth', block: 'nearest'});
            });
            card.append(open);
          });
        }
      });
    } catch {
      container.textContent = 'Não foi possível carregar o histórico.';
    }
  }

  // Excluir do histórico: apaga o áudio do disco (e versões e blocos de texto longo), sempre com confirmação.
  let historyTotal = 0;
  let historyDeleteTarget = null; // item, ou 'all'
  function askDeleteHistory(target, label) {
    historyDeleteTarget = target;
    const all = target === 'all';
    $('hist-confirm-title').textContent = all ? 'Excluir todas as gerações?' : 'Excluir esta geração?';
    $('hist-confirm-text').textContent = all
      ? (historyTotal === 1 ? 'Será excluída 1 geração do histórico.'
        : 'Serão excluídas as ' + historyTotal.toLocaleString('pt-BR') + ' gerações do histórico (não só as que aparecem na lista).') +
        ' As que estiverem gerando agora ficam.'
      : label + ': “' + (target.text || '').slice(0, 80) + '”';
    $('hist-confirm-type').hidden = !all;
    $('hist-confirm-input').value = '';
    $('hist-confirm-go').disabled = all;
    $('hist-confirm-go').textContent = all ? 'Excluir todas' : 'Excluir';
    $('hist-confirm-status').textContent = '';
    $('hist-confirm').showModal();
    (all ? $('hist-confirm-input') : $('hist-confirm-cancel')).focus();
  }
  async function confirmDeleteHistory(event) {
    event.preventDefault();
    const target = historyDeleteTarget;
    if (!target) return;
    const all = target === 'all';
    $('hist-confirm-go').disabled = true;
    $('hist-confirm-status').textContent = 'Excluindo…';
    try {
      const url = all ? '/history/all?confirm=true' : '/history/' + encodeURIComponent(target.id) + '?incluir_blocos=true';
      const response = await fetch(url, {method: 'DELETE'});
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || ('HTTP ' + response.status));
      if (window.VBTextoLongo && (all || window.VBTextoLongo.isOpen(target.id))) window.VBTextoLongo.close();
      $('generation-result').hidden = true;
      $('result-player').pause();
      $('hist-confirm').close();
      $('generation-status').textContent = all
        ? body.deleted + ' geração(ões) excluída(s)' + (body.skipped_active ? '; ' + body.skipped_active + ' em andamento ficaram.' : '.')
        : 'Geração excluída.';
      historyDeleteTarget = null;
      await loadHistory();
    } catch (error) {
      $('hist-confirm-status').textContent = 'Não foi possível excluir: ' + (error.message || error);
      $('hist-confirm-go').disabled = false;
    }
  }
  $('history-clear-all').addEventListener('click', () => askDeleteHistory('all'));
  $('hist-confirm-input').addEventListener('input', (e) => {
    $('hist-confirm-go').disabled = e.target.value.trim().toUpperCase() !== 'EXCLUIR';
  });
  $('hist-confirm-cancel').addEventListener('click', () => { historyDeleteTarget = null; $('hist-confirm').close(); });
  $('hist-confirm-form').addEventListener('submit', confirmDeleteHistory);

  function showResult(id) {
    $('generation-result').hidden = false;
    $('result-player').src = '/audio/' + encodeURIComponent(id) + '?t=' + Date.now();
    $('download-wav').href = '/history/' + encodeURIComponent(id) + '/export-audio';
  }

  async function pollGeneration(id) {
    const started = Date.now();
    let longJob = false;
    // Trabalho em blocos pode levar horas: sem limite de 10 min enquanto o painel acompanha.
    while (longJob || Date.now() - started < 600000) {
      await new Promise((resolve) => setTimeout(resolve, 2000));
      if (!longJob && window.VBTextoLongo) longJob = await window.VBTextoLongo.open(id, {quiet: true});
      let item;
      try {
        item = await getJson('/history/' + encodeURIComponent(id));
      } catch {
        $('generation-status').textContent = 'Aguardando resposta do servidor…';
        continue;
      }
      if (item.status === 'completed') {
        $('generation-status').textContent = 'Áudio pronto.';
        showResult(id);
        await loadHistory();
        return;
      }
      if (item.status === 'failed') {
        await loadHistory();
        if (longJob) throw new Error('A geração parou. Veja o painel do trabalho abaixo: dá para retomar de onde parou.');
        throw new Error(item.error || 'A geração falhou.');
      }
      $('generation-status').textContent = statusText(item.status) + '… ' + Math.floor((Date.now() - started) / 1000) + ' s';
    }
    throw new Error('A geração continua no servidor. Consulte o histórico daqui a pouco.');
  }

  if (window.VBTextoLongo) {
    window.VBTextoLongo.setOnFinished(async (id, dbStatus) => {
      if (dbStatus === 'completed') {
        $('generation-status').textContent = 'Áudio pronto.';
        showResult(id);
      }
      await loadHistory();
    });
  }

  // "Padrão" não manda nada: vale o texto_longo.json (140/300/775). Os outros só mudam a montagem.
  const PAUSE_PRESETS = {
    curtas: {pause_oracao_ms: 90, pause_frase_ms: 180, pause_paragrafo_ms: 450},
    longas: {pause_oracao_ms: 160, pause_frase_ms: 350, pause_paragrafo_ms: 900}
  };
  function pausePresetValues() {
    const r = document.querySelector('input[name="pause-preset"]:checked');
    return (r && PAUSE_PRESETS[r.value]) || {};
  }

  // Fase 7: modo qualidade. Nada é mandado com as chaves desligadas (servidor age como antes).
  const QUALITY = {revisar_whisper: 'q-revisar', refazer_sozinho: 'q-refazer', voz_com_sotaque: 'q-sotaque', nivelador_volume: 'q-nivelador'};
  function qualityValues() {
    const out = {};
    const review = $('q-revisar').checked;
    for (const [key, id] of Object.entries(QUALITY)) {
      if (!$(id).checked) continue;
      if ((key === 'refazer_sozinho' || key === 'voz_com_sotaque') && !review) continue;
      out[key] = true;
    }
    return out;
  }
  function updateQuality() {
    const review = $('q-revisar').checked;
    $('q-refazer').disabled = !review || $('q-sotaque').checked;
    $('q-sotaque').disabled = !review;
    const v = qualityValues();
    const parts = [];
    if (v.revisar_whisper) parts.push(v.voz_com_sotaque ? 'revisão (voz com sotaque)' : v.refazer_sozinho ? 'revisão e refação' : 'revisão');
    if (v.nivelador_volume) parts.push('nivelador');
    // Sugestão: voz de um idioma lendo texto de outro costuma ter sotaque, e o sotaque engana o Whisper.
    const textLang = $('gen-language').value;
    const mismatch = selected && selected.language && textLang !== 'auto' && selected.language !== textLang;
    const suggest = review && mismatch && !$('q-sotaque').checked;
    $('quality-hint').hidden = !suggest;
    if (suggest) {
      const nome = (code) => (languageNames[code] || code).toLowerCase();
      $('quality-hint').textContent = 'A voz é de ' + nome(selected.language) + ' e o texto está em ' + nome(textLang) +
        '. Se ela falar com sotaque, ligue “Voz com sotaque”: o sotaque engana o Whisper.';
    }
    $('quality-summary').textContent = (parts.join(' · ') || 'desligado') + (suggest ? ' · ⚠ veja a sugestão' : '');
    $('quality-summary').classList.toggle('on', parts.length > 0);
  }

  // GPU 2 (Missão GPU por famílias): perfil da placa (/gpu/perfil). Só informa; nunca bloqueia.
  let perfilPlaca = null;
  const ROTULO_SITUACAO = {aviso: ' ⚠', nao_testado: ' (não testado nesta placa)', nao_utilizavel: ' — não utilizável nesta placa'};
  const nomeDaOpcao = (valor) => valor === 'omnivoice' ? 'omnivoice' : MODELO_POR_OPCAO[valor];
  const situacaoModelo = (nome) => (perfilPlaca && nome && perfilPlaca.modelos && perfilPlaca.modelos[nome]) || null;
  function marcarOpcoesPerfil() {
    for (const opt of $('gen-model').options) {
      if (!opt.dataset.rotulo) opt.dataset.rotulo = opt.textContent;
      const s = situacaoModelo(nomeDaOpcao(opt.value));
      const rotulo = perfilPlaca && perfilPlaca.familia === 'sem_placa' ? ' — precisa de placa de vídeo' : (s && ROTULO_SITUACAO[s.situacao]);
      opt.textContent = opt.dataset.rotulo + (rotulo || '');
    }
  }
  function mostrarPerfil() {
    const p = perfilPlaca;
    if (!p) { $('placa-perfil').textContent = ''; return; }
    $('placa-perfil').textContent = p.familia === 'sem_placa' ? '⚠ ' + (p.avisos[0] || 'Sem placa de vídeo compatível.')
      : 'Sua placa: ' + p.placa + ' · família ' + p.familia_nome + (p.vram_total_mb ? ' · ' + fmtGB(p.vram_total_mb) : '') +
        (p.avisos.length ? ' · ⚠ ' + p.avisos.join(' ') : '');
  }
  async function loadPerfilPlaca() {
    try { perfilPlaca = await getJson('/gpu/perfil'); } catch { perfilPlaca = null; /* servidor antigo: sem perfil */ }
    marcarOpcoesPerfil();
    mostrarPerfil();
    updateModelWarning();
  }

  // Fase 8: avisos por modelo e idioma (avisos_modelo_idioma.json). Só informam; nunca bloqueiam.
  let modelWarnings = [];
  function updateModelWarning() {
    const box = $('model-warning');
    const model = $('gen-model').value;
    const choice = $('gen-language').value;
    const language = choice === 'auto' ? (selected && selected.language) : choice;
    const sp = situacaoModelo(nomeDaOpcao(model));
    const hits = [...(sp && sp.situacao !== 'ok' && sp.motivo ? [{aviso: sp.motivo}] : []),
      ...modelWarnings.filter((w) =>
        Array.isArray(w.modelos) && w.modelos.includes(model) &&
        Array.isArray(w.idiomas) && w.idiomas.includes(language) && w.aviso)];
    box.replaceChildren(...hits.map((w) => {
      const line = document.createElement('p');
      line.textContent = '⚠ ' + w.aviso;
      if (w.detalhe) line.title = w.detalhe;
      return line;
    }));
    box.hidden = hits.length === 0;
  }
  async function loadModelWarnings() {
    try {
      const data = await getJson('/novo/avisos_modelo_idioma.json');
      modelWarnings = data?.kind === 'avisos_modelo_idioma' && Array.isArray(data.avisos) ? data.avisos : [];
    } catch { modelWarnings = []; /* sem o arquivo: nenhum aviso */ }
    updateModelWarning();
  }

  async function generate() {
    if (generating) return;
    if (!selected) { $('generation-status').textContent = 'Selecione uma voz.'; return; }
    const text = $('gen-text').value.trim();
    if (!text) { $('generation-status').textContent = 'Escreva um texto para gerar.'; $('gen-text').focus(); return; }
    const chars = [...text].length;
    if (chars > 50000) {
      $('generation-status').textContent = 'O texto tem ' + chars.toLocaleString('pt-BR') +
        ' caracteres; o limite é 50.000. Divida em duas gerações (nada foi cortado da caixa).';
      return;
    }
    const [engine, modelSize] = $('gen-model').value.split(':');
    const languageChoice = $('gen-language').value;
    const language = languageChoice === 'auto' ? (selected.language || 'pt') : languageChoice;
    generating = true;
    $('generate-button').disabled = true;
    $('generation-result').hidden = true;
    if (window.VBTextoLongo) window.VBTextoLongo.close();
    $('generation-status').textContent = 'Enviando geração…';
    try {
      const response = await fetch('/generate', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({profile_id: selected.id, text, language, engine, model_size: modelSize || null,
          ...pausePresetValues(), ...qualityValues()})
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        const detail = typeof error.detail === 'string' ? error.detail : 'HTTP ' + response.status;
        throw new Error(detail);
      }
      const job = await response.json();
      $('generation-status').textContent = 'Geração iniciada…';
      await loadHistory();
      await pollGeneration(job.id);
    } catch (error) {
      $('generation-status').textContent = error instanceof TypeError
        ? 'Não foi possível falar com o servidor. Confira se o VozLab está aberto.'
        : (error.message || 'Erro ao gerar o áudio.');
    } finally {
      generating = false;
      $('generate-button').disabled = !selected;
    }
  }

  // ============================================================
  // PAINEL OMNIVOICE (OV-6+7): mora em omnivoice-painel.js
  // ============================================================
  function initOmniVoice() {
    if (window.VBOmniVoice) window.VBOmniVoice.init();
    else window.__vbOmniVoicePendente = true;  // o arquivo do painel ainda não carregou
  }

  $('generate-button').addEventListener('click', generate);

  // Contador de caracteres. O servidor aceita até 50.000 (conta como o Python: por caractere,
  // depois de tirar espaços das pontas). Sem maxlength: o texto colado nunca é cortado em silêncio.
  const MAX_TEXT = 50000;
  const textLength = () => [...$('gen-text').value.trim()].length;
  function updateCount() {
    const n = textLength();
    const el = $('gen-count');
    el.textContent = n.toLocaleString('pt-BR') + ' / ' + MAX_TEXT.toLocaleString('pt-BR') + ' caracteres' +
      (n > MAX_TEXT ? ' · passou ' + (n - MAX_TEXT).toLocaleString('pt-BR') + ': divida o texto em duas gerações' : '');
    el.className = 'gen-count' + (n > MAX_TEXT ? ' over' : n > MAX_TEXT * 0.9 ? ' near' : '');
  }
  $('gen-text').addEventListener('input', updateCount);
  updateCount();

  // Modo qualidade: lembra as chaves neste computador.
  try {
    const savedQ = JSON.parse(localStorage.getItem('vozlab_qualidade') || '{}');
    for (const id of Object.values(QUALITY)) $(id).checked = Boolean(savedQ[id]);
  } catch { /* sem armazenamento: tudo desligado */ }
  for (const id of Object.values(QUALITY)) $(id).addEventListener('change', () => {
    updateQuality();
    try {
      localStorage.setItem('vozlab_qualidade', JSON.stringify(Object.fromEntries(Object.values(QUALITY).map((k) => [k, $(k).checked]))));
    } catch { /* ignora */ }
  });
  $('gen-language').addEventListener('change', updateQuality);
  updateQuality();
  $('gen-language').addEventListener('change', updateModelWarning);
  $('gen-model').addEventListener('change', updateModelWarning);

  // "Carregar na placa agora" (15/10/2026): mostra o que está na placa e troca na hora (um modelo de cada vez).
  const MODELO_POR_OPCAO = {
    'qwen:1.7B': 'qwen-tts-1.7B', 'qwen:0.6B': 'qwen-tts-0.6B', 'f5_tts': 'f5-tts', 'luxtts': 'luxtts',
    'chatterbox': 'chatterbox-tts', 'chatterbox_turbo': 'chatterbox-turbo', 'kokoro': 'kokoro',
    'qwen_custom_voice:1.7B': 'qwen-custom-voice-1.7B', 'qwen_custom_voice:0.6B': 'qwen-custom-voice-0.6B'
  };
  const TAMANHO_ESTIMADO_MB = {'chatterbox-turbo': 3860, 'luxtts': 300, 'omnivoice': 3117};
  let placaOcupada = false;
  let statusModelos = [];
  const gb = (mb) => (mb / 1024).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' GB';
  async function atualizarPlaca() {
    let modelos = [];
    let ov = {};
    try { modelos = (await getJson('/models/status')).models || []; } catch { modelos = []; }
    try { ov = await getJson('/omnivoice/status'); } catch { ov = {}; }
    statusModelos = modelos;
    const naPlaca = modelos.filter((m) => m.loaded && !/^whisper|^qwen3-/.test(m.model_name)).map((m) => m.display_name);
    if (ov.status === 'online' && ov.model_loaded) naPlaca.push('OmniVoice (serviço)');
    if (!placaOcupada) $('placa-agora').textContent = 'Na placa de vídeo: ' + (naPlaca.length ? naPlaca.join(', ') : 'nenhum modelo de voz');
    // Aviso de download: o modelo escolhido ainda não está no PC?
    const nome = MODELO_POR_OPCAO[$('gen-model').value];
    const m = modelos.find((x) => x.model_name === nome);
    const aviso = $('placa-download');
    if (m && !m.downloaded) {
      const tam = TAMANHO_ESTIMADO_MB[nome];
      aviso.textContent = m.downloading ? 'Baixando ' + m.display_name + '… (veja o progresso em Models)'
        : m.display_name + ' ainda não está neste PC. Ele é baixado do Hugging Face na 1ª geração ou em "Carregar na placa agora"' +
          (tam ? ' (~' + gb(tam) + ')' : '') + '. Também dá para baixar pela tela Models.';
      aviso.hidden = false;
    } else {
      aviso.hidden = true;
    }
  }
  async function carregarNaPlaca() {
    const opcao = $('gen-model').value;
    const botao = $('placa-carregar');
    botao.disabled = true;
    placaOcupada = true;
    const t0 = Date.now();
    const relogio = setInterval(() => {
      $('placa-agora').textContent = 'Carregando na placa… ' + Math.round((Date.now() - t0) / 1000) + ' s (se o modelo não estiver no PC, ele é baixado antes)';
    }, 1000);
    try {
      let r;
      if (opcao === 'omnivoice') {
        let st = {};
        try { st = await getJson('/omnivoice/status'); } catch { st = {}; }
        if (st.status !== 'online') {
          await fetch('/omnivoice/control', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: 'start'})});
          for (let i = 0; i < 45; i++) {
            await new Promise((ok) => setTimeout(ok, 1000));
            try { if ((await getJson('/omnivoice/status')).status === 'online') break; } catch { /* ainda ligando */ }
          }
        }
        r = await fetch('/omnivoice/control', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({action: 'load'})});
      } else {
        r = await fetch('/models/' + encodeURIComponent(MODELO_POR_OPCAO[opcao]) + '/carregar', {method: 'POST'});
      }
      const d = await r.json().catch(() => ({}));
      if (!r.ok || (opcao === 'omnivoice' && d.status !== 'loaded')) throw new Error(typeof d.detail === 'string' ? d.detail : (d.message || 'HTTP ' + r.status));
      clearInterval(relogio);
      placaOcupada = false;
      await atualizarPlaca();
      $('placa-agora').textContent += ' · pronto em ' + Math.round((Date.now() - t0) / 1000) + ' s';
    } catch (err) {
      clearInterval(relogio);
      placaOcupada = false;
      $('placa-agora').textContent = 'Não consegui carregar: ' + (err.message || err);
    } finally {
      botao.disabled = false;
    }
  }
  $('placa-carregar').addEventListener('click', carregarNaPlaca);
  $('gen-model').addEventListener('change', atualizarPlaca);
  setInterval(() => { if (!document.hidden && (location.hash.slice(1) || 'generate') === 'generate' && !placaOcupada) atualizarPlaca(); }, 15000);
  atualizarPlaca();

  // OV-4: OmniVoice no seletor "Modelo": ajustes recolhidos e aviso de serviço desligado (com botão para ligar).
  let ovAjustesTimer = null;
  function ovGerarMostrarAjustes(a) {
    $('ov-gerar-passos').value = a.num_step;
    $('ov-gerar-cfg').value = a.guidance_scale;
    $('ov-gerar-passos-val').textContent = a.num_step;
    $('ov-gerar-cfg-val').textContent = Number(a.guidance_scale).toFixed(1);
    const padrao = Number(a.num_step) === 16 && Number(a.guidance_scale) === 2;
    $('ov-gerar-resumo').textContent = padrao ? '(padrão)' : '(' + a.num_step + ' passos · CFG ' + Number(a.guidance_scale).toFixed(1) + ')';
  }
  function ovGerarSalvar() {
    const a = {num_step: Number($('ov-gerar-passos').value), guidance_scale: Number($('ov-gerar-cfg').value)};
    ovGerarMostrarAjustes(a);
    clearTimeout(ovAjustesTimer);
    ovAjustesTimer = setTimeout(() => fetch('/omnivoice/ajustes', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(a)
    }).catch(() => {}), 400);
  }
  async function atualizarOmniVoiceGerar() {
    const box = $('ov-gerar-box');
    const ativo = $('gen-model').value === 'omnivoice';
    box.hidden = !ativo;
    if (!ativo) return;
    try { ovGerarMostrarAjustes(await getJson('/omnivoice/ajustes')); } catch (_) { /* sem a rota: ajustes ficam no padrão */ }
    let online = false;
    try { online = (await getJson('/omnivoice/status')).status === 'online'; } catch (_) { online = false; }
    $('ov-gerar-servico').textContent = online
      ? 'Serviço OmniVoice ligado.'
      : 'O serviço OmniVoice está desligado. Ele liga sozinho ao gerar (~15 s), ou ligue agora.';
    $('ov-gerar-ligar').hidden = online;
  }
  $('gen-model').addEventListener('change', atualizarOmniVoiceGerar);
  $('ov-gerar-passos').addEventListener('input', ovGerarSalvar);
  $('ov-gerar-cfg').addEventListener('input', ovGerarSalvar);
  $('ov-gerar-padrao').addEventListener('click', () => {
    $('ov-gerar-passos').value = 16; $('ov-gerar-cfg').value = 2.0; ovGerarSalvar();
  });
  $('ov-gerar-ligar').addEventListener('click', async () => {
    $('ov-gerar-ligar').disabled = true;
    $('ov-gerar-servico').textContent = 'Ligando o serviço OmniVoice…';
    try {
      await fetch('/omnivoice/control', {method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({action: 'start'})});
      for (let i = 1; i <= 45; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        let st = {};
        try { st = await getJson('/omnivoice/status'); } catch (_) { st = {}; }
        if (st.status === 'online') break;
        $('ov-gerar-servico').textContent = 'Ligando o serviço OmniVoice… (' + i + ' s)';
      }
    } catch (_) { /* o atualizar abaixo mostra o estado real */ }
    $('ov-gerar-ligar').disabled = false;
    atualizarOmniVoiceGerar();
  });
  loadModelWarnings();
  loadPerfilPlaca();

  // MP3 junto (15/10/2026): com o interruptor ligado, todo "Baixar WAV" baixa também o .mp3 (o servidor converte).
  // A preferência fica salva neste navegador; vale para Generate, Histórico e a aba OmniVoice.
  const CHAVE_MP3 = 'vozlab.mp3Junto';
  const lerMp3 = () => { try { return localStorage.getItem(CHAVE_MP3) !== '0'; } catch { return true; } };
  function sincronizarMp3() { document.querySelectorAll('.mp3-junto').forEach((c) => { c.checked = lerMp3(); }); }
  document.addEventListener('change', (ev) => {
    if (!ev.target.classList || !ev.target.classList.contains('mp3-junto')) return;
    try { localStorage.setItem(CHAVE_MP3, ev.target.checked ? '1' : '0'); } catch { /* sem armazenamento: só nesta tela */ }
    sincronizarMp3();
  });
  document.addEventListener('click', (ev) => {
    const a = ev.target.closest && ev.target.closest('a[href]');
    if (!a || !lerMp3()) return;
    const href = a.getAttribute('href') || '';
    const ehAudio = /\/history\/[^/]+\/export-audio/.test(href) || /\/omnivoice\/painel\/[^/]+\/audio/.test(href);
    if (!ehAudio || /format=mp3/.test(href)) return;
    setTimeout(() => {
      const mp3 = document.createElement('a');
      mp3.href = href + (href.includes('?') ? '&' : '?') + 'format=mp3';
      mp3.download = '';
      mp3.hidden = true;
      document.body.append(mp3);
      mp3.click();
      mp3.remove();
    }, 400);
  });
  sincronizarMp3();

  // Pausas entre frases: lembra a escolha neste computador.
  try {
    const saved = localStorage.getItem('vozlab_pausas');
    const radio = saved && document.querySelector('input[name="pause-preset"][value="' + saved + '"]');
    if (radio) radio.checked = true;
  } catch { /* sem armazenamento: fica no Padrão */ }
  document.querySelectorAll('input[name="pause-preset"]').forEach((r) => r.addEventListener('change', () => {
    try { localStorage.setItem('vozlab_pausas', r.value); } catch { /* ignora */ }
  }));
  $('gen-text').addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); generate(); }
  });
  $('retry-data').addEventListener('click', () => { loadSummary(); loadHistory(); });
  showPage(location.hash.slice(1));
  loadSummary();
  loadHistory();
})();
