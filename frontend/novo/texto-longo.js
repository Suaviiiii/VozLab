/* Painel de texto longo (Fase 5): progresso, cancelar, retomar, blocos,
 * regerar bloco, remontar e limpar temporários.
 * Só usa rotas que já existem no servidor. Uma geração é "longa" quando
 * GET /generate/{id}/progress responde 200; com 404 o painel fica escondido
 * e a tela se comporta exatamente como antes. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const ACTIVE_DB = new Set(['pending', 'loading_model', 'generating']);
  const STATUS = {
    pendente: 'Na fila',
    em_andamento: 'Gerando',
    interrompido: 'Interrompido · pode ser retomado',
    cancelado: 'Cancelado · pode ser retomado',
    falhou: 'Falhou · pode ser retomado',
    concluido: 'Concluído',
    possivelmente_travado: 'Possivelmente travado',
    sem_progresso: 'Sem progresso recente'
  };
  const BLOCK_STATUS = {ok: 'Pronto', pendente: 'Aguardando', em_andamento: 'Gerando', falhou: 'Falhou'};
  const BOUNDARY = {oracao: 'oração', frase: 'frase', paragrafo: 'parágrafo', fim: 'fim'};
  // Mensagens do servidor que ainda vêm em inglês.
  const SERVER_EN = {
    'Generation not found': 'Geração não encontrada.',
    'Only active generations can be cancelled': 'Só dá para cancelar uma geração que está gerando agora.',
    'Generation cancelled': 'Geração cancelada.',
    'Orphaned generation cleared': 'A geração tinha parado sozinha e foi marcada como falha.'
  };

  let current = null;       // id do trabalho mostrado
  let timer = null;
  let lastProgress = null;
  let lastBlocks = null;
  let blocksKey = '';
  let blocksDirty = false;
  let busy = false;
  let onFinished = () => {};
  const known = new Map();  // id -> true/false (é trabalho longo?)

  function friendlyError(error) {
    if (error instanceof TypeError) return 'Não foi possível falar com o servidor. Confira se o VozLab está aberto.';
    return error?.message || 'Erro inesperado.';
  }

  async function request(path, options = {}) {
    const response = await fetch(path, {cache: 'no-store', ...options});
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      let detail = body && body.detail;
      if (Array.isArray(detail)) detail = 'Valor fora da faixa permitida.';
      if (typeof detail !== 'string') detail = 'O servidor respondeu com erro ' + response.status + '.';
      const error = new Error(SERVER_EN[detail] || detail);
      error.status = response.status;
      throw error;
    }
    if (body && typeof body.message === 'string' && SERVER_EN[body.message]) body.message = SERVER_EN[body.message];
    return body;
  }

  const enc = encodeURIComponent;
  const fmtMB = (bytes) => (bytes / 1048576).toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' MB';
  function fmtDuration(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '';
    if (seconds < 60) return Math.max(1, Math.round(seconds)) + ' s';
    const min = Math.round(seconds / 60);
    if (min < 60) return min + ' min';
    return Math.floor(min / 60) + ' h ' + String(min % 60).padStart(2, '0') + ' min';
  }

  /** Responde se a geração tem trabalho em blocos (cacheia só o "sim"). */
  async function probe(id) {
    if (known.get(id)) return true;
    try {
      await request('/generate/' + enc(id) + '/progress');
      known.set(id, true);
      return true;
    } catch (error) {
      return false;
    }
  }

  function setNote(text, kind = '') {
    const note = $('lj-note');
    note.textContent = text || '';
    note.hidden = !text;
    note.className = 'lj-note' + (kind ? ' ' + kind : '');
  }

  function estimate(progress, blocks) {
    if (!blocks || progress.effective_status !== 'em_andamento') return '';
    const done = blocks.filter((b) => b.status === 'ok' && Number.isFinite(b.gen_time_s) && b.chars > 0);
    if (!done.length) return 'calculando o tempo restante…';
    const secPerChar = done.reduce((s, b) => s + b.gen_time_s, 0) / done.reduce((s, b) => s + b.chars, 0);
    const remainingChars = blocks.filter((b) => b.status !== 'ok').reduce((s, b) => s + (b.chars || 0), 0);
    return 'faltam cerca de ' + fmtDuration(secPerChar * remainingChars);
  }

  function renderProgress(progress, dbStatus) {
    const eff = progress.effective_status;
    const running = progress.is_locked || ACTIVE_DB.has(dbStatus);
    const label = running && eff !== 'em_andamento' && ACTIVE_DB.has(dbStatus) ? 'Gerando' : (STATUS[eff] || eff);
    const chip = $('lj-status');
    chip.textContent = label;
    chip.dataset.state = running ? 'em_andamento' : eff;

    const pct = Math.max(0, Math.min(100, Number(progress.percent) || 0));
    $('lj-bar-fill').style.width = pct + '%';
    $('lj-bar').setAttribute('aria-valuenow', String(Math.round(pct)));
    const parts = [progress.completed_chunks + ' de ' + progress.total_chunks + ' blocos',
      pct.toLocaleString('pt-BR', {maximumFractionDigits: 1}) + '%'];
    if (progress.failed_chunks) parts.push(progress.failed_chunks + (progress.failed_chunks === 1 ? ' bloco falhou' : ' blocos falharam'));
    const eta = estimate(progress, lastBlocks);
    if (eta) parts.push(eta);
    $('lj-progress-text').textContent = parts.join(' · ');

    $('lj-cancel').hidden = !running;
    $('lj-resume').hidden = running || !progress.can_resume;
    const concluded = eff === 'concluido' && !running;
    $('lj-reassemble').hidden = !concluded;
    $('lj-cleanup').hidden = !concluded;

    if (progress.warning === 'sem_progresso') setNote('O trabalho não avança há algum tempo. Se continuar assim, cancele e retome.', 'warn');
    else if (progress.warning === 'montagem_final_ausente') setNote('Todos os blocos estão prontos, mas o áudio final não foi montado. Clique em Retomar: ele só junta os blocos (nada é gerado de novo).', 'warn');
    else if (progress.warning) setNote('Aviso do servidor: ' + progress.warning, 'warn');
    else if (!running && eff === 'cancelado') setNote('Cancelado. Os blocos prontos estão salvos; Retomar gera só os que faltam.');
    else if (!running && progress.error && eff !== 'concluido') setNote(progress.error, 'error');    else if (!running && progress.can_resume) setNote('Os blocos prontos estão salvos. Retomar gera só os que faltam.');
    // Gerando: mantém o aviso da última ação (ex.: "cancelamento pedido").
  }

  function blockRow(block, concluded, running) {
    const row = document.createElement('li');
    row.className = 'lj-block';
    row.dataset.state = block.status;
    const head = document.createElement('div');
    head.className = 'lj-block-head';
    const title = document.createElement('strong');
    title.textContent = 'Bloco ' + block.idx;
    const state = document.createElement('span');
    state.className = 'lj-block-state';
    let stateText = BLOCK_STATUS[block.status] || block.status;
    if (running && lastProgress && lastProgress.current_chunk === block.idx && block.status !== 'ok') stateText = 'Gerando agora';
    state.textContent = stateText;
    const meta = document.createElement('span');
    meta.className = 'lj-block-meta';
    const metaParts = [BOUNDARY[block.boundary_type] ? 'termina em ' + BOUNDARY[block.boundary_type] : '', (block.chars || 0) + ' caracteres'];
    if (Number.isFinite(block.audio_dur_s)) metaParts.push(block.audio_dur_s.toLocaleString('pt-BR', {maximumFractionDigits: 1}) + ' s de áudio');
    const takes = (block.attempts || []).length;
    if (takes > 1) metaParts.push(takes + ' tentativas');
    const rev = block.revisao;
    if (rev && rev.refeito) metaParts.push('refeito pelo revisor (' + rev.tentativas.length + ' versões, ficou a melhor)');
    meta.textContent = metaParts.filter(Boolean).join(' · ');
    head.append(title);
    // Fase 7: o Whisper marcou este bloco. Só aponta; quem decide é quem ouve.
    if (rev && rev.ouvir) {
      const flag = document.createElement('span');
      flag.className = 'lj-flag';
      flag.textContent = '🎧 Ouvir';
      flag.title = 'O revisor marcou este bloco: ' + (rev.motivo_ouvir || 'suspeito') + '. Ouça e, se precisar, regere.';
      head.append(flag);
      row.dataset.ouvir = '1';
    }
    head.append(state);
    const text = document.createElement('p');
    text.className = 'lj-block-text';
    text.textContent = block.text || '';
    row.append(head, meta, text);
    if (block.status === 'falhou' && block.error_message) {
      const err = document.createElement('p');
      err.className = 'error-text';
      err.textContent = block.error_message;
      row.append(err);
    }
    const actions = document.createElement('div');
    actions.className = 'lj-block-actions';
    if (block.status === 'ok') {
      const play = document.createElement('button');
      play.type = 'button';
      play.className = 'secondary-button';
      play.textContent = '▶ Ouvir bloco';
      play.addEventListener('click', () => {
        let player = row.querySelector('audio');
        if (!player) {
          player = document.createElement('audio');
          player.controls = true;
          player.preload = 'none';
          player.className = 'audio-player';
          player.addEventListener('error', () => setNote('Não foi possível tocar o bloco ' + block.idx + '.', 'error'));
          row.append(player);
        }
        player.src = '/generate/' + enc(current) + '/blocks/' + block.idx + '/audio?t=' + Date.now();
        player.play().catch(() => {});
      });
      actions.append(play);
    }
    if (concluded) {
      const regen = document.createElement('button');
      regen.type = 'button';
      regen.className = 'secondary-button';
      regen.textContent = '↻ Regerar este bloco';
      regen.title = 'Gera de novo só este bloco, com outra semente. O áudio anterior fica guardado.';
      regen.addEventListener('click', () => regenerateBlock(block.idx));
      actions.append(regen);
    }
    if (actions.childElementCount) row.append(actions);
    return row;
  }

  function renderBlocks(blocks, progress, dbStatus) {
    const list = $('lj-blocks');
    const running = progress.is_locked || ACTIVE_DB.has(dbStatus);
    const concluded = progress.effective_status === 'concluido' && !running;
    list.replaceChildren();
    if (!blocks.length) {
      const empty = document.createElement('li');
      empty.className = 'secondary';
      empty.textContent = 'Este trabalho ainda não tem blocos.';
      list.append(empty);
      return;
    }
    blocks.forEach((block) => {
      const row = blockRow(block, concluded, running);
      row.dataset.idx = String(block.idx);
      list.append(row);
    });
    const marcados = blocks.filter((b) => b.revisao && b.revisao.ouvir).length;
    $('lj-blocks-count').textContent = blocks.length + (blocks.length === 1 ? ' bloco' : ' blocos') +
      (marcados ? ' · ' + marcados + ' para ouvir' : '');
    const bytes = blocks.reduce((s, b) => s + (b.file_bytes || 0), 0);
    $('lj-cleanup-size').textContent = bytes ? 'Os blocos deste trabalho ocupam cerca de ' + fmtMB(bytes) + ' (sem contar versões antigas regeradas).' : '';
  }

  async function refresh() {
    if (!current) return;
    const id = current;
    let progress;
    let dbStatus = '';
    try {
      const [p, row] = await Promise.all([
        request('/generate/' + enc(id) + '/progress'),
        request('/history/' + enc(id)).catch(() => null)
      ]);
      progress = p;
      dbStatus = row?.status || '';
    } catch (error) {
      if (id !== current) return;
      if (error.status === 404) { setNote('Os arquivos deste trabalho não existem mais.', 'error'); stop(); return; }
      setNote(friendlyError(error), 'error');
      schedule(4000);
      return;
    }
    if (id !== current) return;
    const key = [progress.completed_chunks, progress.failed_chunks, progress.effective_status, progress.is_locked, dbStatus, progress.current_chunk].join('|');
    if (key !== blocksKey || !lastBlocks) {
      try {
        lastBlocks = (await request('/generate/' + enc(id) + '/blocks')).blocks || [];
        blocksKey = key;
        blocksDirty = true;
      } catch (error) {
        setNote(friendlyError(error), 'error');
      }
    }
    if (id !== current) return;
    const wasRunning = lastProgress && (lastProgress.is_locked || ACTIVE_DB.has(lastProgress._db));
    lastProgress = {...progress, _db: dbStatus};
    $('lj-loading').hidden = true;
    $('lj-body').hidden = false;
    renderProgress(progress, dbStatus);
    // Redesenha a lista só quando algo mudou e nenhum bloco está tocando.
    const playing = [...$('lj-blocks').querySelectorAll('audio')].some((a) => !a.paused);
    if (lastBlocks && blocksDirty && !playing) {
      renderBlocks(lastBlocks, progress, dbStatus);
      blocksDirty = false;
    }
    const running = progress.is_locked || ACTIVE_DB.has(dbStatus);
    if (running) schedule(2000);
    else if (wasRunning) {
      if (dbStatus === 'completed') setNote('Áudio final pronto.', 'ok');
      onFinished(id, dbStatus);
    }
  }

  function schedule(ms) {
    clearTimeout(timer);
    timer = setTimeout(refresh, ms);
  }

  function stop() { clearTimeout(timer); timer = null; }

  /** Mostra o painel do trabalho `id`. Retorna false se não for trabalho longo. */
  async function open(id, {quiet = false} = {}) {
    if (!(await probe(id))) {
      if (!quiet) setNote('');
      return false;
    }
    if (current !== id) {
      current = id;
      lastProgress = null;
      lastBlocks = null;
      blocksKey = '';
      $('lj-blocks').replaceChildren();
      $('lj-loading').hidden = false;
      $('lj-body').hidden = true;
      setNote('');
      $('lj-reassemble-status').textContent = '';
    }
    $('long-job').hidden = false;
    stop();
    await refresh();
    return true;
  }

  function close() {
    stop();
    current = null;
    $('long-job').hidden = true;
  }

  async function action(fn, pending) {
    if (busy || !current) return;
    busy = true;
    document.querySelectorAll('#long-job button').forEach((b) => { b.disabled = true; });
    if (pending) setNote(pending);
    try {
      await fn();
    } catch (error) {
      setNote(friendlyError(error), 'error');
    } finally {
      busy = false;
      document.querySelectorAll('#long-job button').forEach((b) => { b.disabled = false; });
      blocksKey = '';
      await refresh();
    }
  }

  function cancelJob() {
    return action(async () => {
      const result = await request('/generate/' + enc(current) + '/cancel', {method: 'POST'});
      setNote((result && result.message && SERVER_EN[result.message]) || 'Cancelamento pedido. O trabalho para depois do bloco atual; os blocos prontos ficam salvos.');
    }, 'Pedindo para cancelar…');
  }

  function resumeJob() {
    return action(async () => {
      await request('/generate/' + enc(current) + '/resume', {method: 'POST'});
      setNote('Retomando: só os blocos que faltam serão gerados.');
    }, 'Pedindo para retomar…');
  }

  function regenerateBlock(idx) {
    return action(async () => {
      await request('/generate/' + enc(current) + '/blocks/' + idx + '/regenerate', {method: 'POST'});
      setNote('Regerando o bloco ' + idx + '. O áudio final será remontado no fim.');
    }, 'Pedindo para regerar o bloco ' + idx + '…');
  }

  function reassemble(event) {
    event.preventDefault();
    const body = {};
    const fields = {pause_oracao_ms: 'lj-pause-oracao', pause_frase_ms: 'lj-pause-frase', pause_paragrafo_ms: 'lj-pause-paragrafo', target_lufs: 'lj-lufs'};
    for (const [key, inputId] of Object.entries(fields)) {
      const raw = $(inputId).value.trim().replace(',', '.');
      if (raw === '') continue;
      const value = Number(raw);
      if (!Number.isFinite(value)) { $('lj-reassemble-status').textContent = 'Use só números nos campos.'; return; }
      body[key] = key === 'target_lufs' ? value : Math.round(value);
    }
    if ($('lj-nivelador').value !== '') body.nivelador_volume = $('lj-nivelador').value === '1';
    $('lj-reassemble-status').textContent = 'Remontando (só processador, sem regerar voz)…';
    return action(async () => {
      try {
        await request('/generate/' + enc(current) + '/reassemble', {
          method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
        });
      } catch (error) {
        if (error.status === 422 && /emendas_v2/.test(error.message)) {
          error.message = 'Pausas, volume e nivelador diferentes do padrão só funcionam com a opção "emendas_v2" ligada. Com ela desligada, deixe os campos como estão.';
        }
        $('lj-reassemble-status').textContent = '';
        throw error;
      }
      $('lj-reassemble-status').textContent = 'Áudio final remontado.';
      setNote('Áudio final remontado. O player acima já toca a versão nova.', 'ok');
      onFinished(current, 'completed');
    }, 'Remontando o áudio final…');
  }

  function openCleanup() {
    $('lj-confirm-input').value = '';
    $('lj-confirm-go').disabled = true;
    $('lj-confirm-text').textContent = $('lj-cleanup-size').textContent ||
      'Os blocos deste trabalho serão apagados.';
    $('lj-confirm').showModal();
    $('lj-confirm-input').focus();
  }

  function confirmCleanup(event) {
    event.preventDefault();
    if ($('lj-confirm-input').value.trim().toUpperCase() !== 'LIMPAR') return;
    $('lj-confirm').close();
    const id = current;
    return action(async () => {
      const result = await request('/generate/' + enc(id) + '/job-files?confirm=true', {method: 'DELETE'});
      known.delete(id);
      setNote('Temporários apagados: ' + fmtMB(result.freed_bytes || 0) + ' liberados. O áudio final continua no histórico.', 'ok');
      stop();
      current = null;
      $('lj-body').hidden = true;
      $('lj-reassemble').hidden = true;
      $('lj-cleanup').hidden = true;
    }, 'Apagando os blocos…');
  }

  /** Resumo do espaço da pasta de trabalhos (tela Settings). */
  async function loadStorage(target) {
    if (!target) return;
    target.textContent = 'Calculando espaço…';
    try {
      const s = await request('/system/jobs/storage');
      const jobs = s.jobs || 0;
      const parts = [fmtMB(s.total_bytes || 0) + ' em ' + jobs + (jobs === 1 ? ' trabalho' : ' trabalhos')];
      if (s.archived_dirs) parts.push(s.archived_dirs + ' pasta(s) arquivada(s): ' + (s.archived_mb || 0).toLocaleString('pt-BR') + ' MB');
      target.textContent = 'Temporários de texto longo: ' + parts.join(' · ') + '.' + (s.jobs_dir ? ' Pasta: ' + s.jobs_dir : '');
    } catch (error) {
      target.textContent = 'Espaço dos temporários: ' + friendlyError(error);
    }
  }

  window.addEventListener('DOMContentLoaded', () => {
    if (!$('long-job')) return;
    $('lj-cancel-button').addEventListener('click', cancelJob);
    $('lj-resume-button').addEventListener('click', resumeJob);
    $('lj-close').addEventListener('click', close);
    $('lj-reassemble-form').addEventListener('submit', reassemble);
    $('lj-cleanup-button').addEventListener('click', openCleanup);
    $('lj-confirm-input').addEventListener('input', () => {
      $('lj-confirm-go').disabled = $('lj-confirm-input').value.trim().toUpperCase() !== 'LIMPAR';
    });
    $('lj-confirm-form').addEventListener('submit', confirmCleanup);
    $('lj-confirm-cancel').addEventListener('click', () => $('lj-confirm').close());
  });

  window.VBTextoLongo = {
    probe, open, close, loadStorage,
    isOpen: (id) => current === id && !$('long-job').hidden,
    setOnFinished(fn) { onFinished = typeof fn === 'function' ? fn : () => {}; }
  };
})();
