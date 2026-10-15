/* Tela "Importar vozes" (VozLab): prévia do pacote .zip e importação só das vozes novas. */
(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const LANG = {pt: 'Português', en: 'Inglês', es: 'Espanhol', fr: 'Francês', de: 'Alemão', it: 'Italiano',
    ru: 'Russo', no: 'Norueguês', ja: 'Japonês', zh: 'Chinês', ko: 'Coreano'};
  const STATUS = {novo: 'Nova', duplicado: 'Já existe', invalido: 'Não dá para importar'};
  let pack = null;
  let busy = false;

  function serverDown() { return 'Não foi possível falar com o servidor. Confira se o VozLab está aberto.'; }

  function setStatus(text, kind = '') {
    $('imp-status').textContent = text;
    $('imp-status').className = 'field-help' + (kind ? ' imp-' + kind : '');
  }

  function detail(xhrOrBody, status) {
    const d = xhrOrBody && xhrOrBody.detail;
    if (typeof d === 'string') return d;
    return 'O servidor respondeu com erro ' + status + '.';
  }

  function upload(file, lang) {
    return new Promise((resolve, reject) => {
      const form = new FormData();
      form.append('file', file);
      form.append('default_language', lang);
      const xhr = new XMLHttpRequest();
      xhr.open('POST', '/voice-packs/preview');
      xhr.responseType = 'json';
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const pct = Math.round(100 * e.loaded / e.total);
        $('imp-progress').value = pct;
        setStatus(pct < 100 ? 'Enviando o pacote… ' + pct + '%' : 'Lendo e conferindo as vozes… (pacotes grandes levam alguns minutos)');
      };
      xhr.onload = () => (xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response)
        : reject(new Error(detail(xhr.response, xhr.status))));
      xhr.onerror = () => reject(new Error(serverDown()));
      xhr.send(form);
    });
  }

  function selectedIdx() {
    return [...document.querySelectorAll('#imp-list input[type=checkbox]:checked')].map((c) => Number(c.value));
  }

  function updateGo() {
    const n = selectedIdx().length;
    $('imp-go').disabled = busy || n === 0;
    $('imp-go').textContent = n ? 'Importar ' + n + (n === 1 ? ' voz' : ' vozes') : 'Nada para importar';
  }

  function renderPreview(data) {
    pack = data;
    const c = data.counts;
    $('imp-summary').textContent = data.total + (data.total === 1 ? ' voz encontrada' : ' vozes encontradas') +
      ' · ' + c.novo + ' novas · ' + c.duplicado + ' já existem · ' + c.invalido + ' com problema';
    const list = $('imp-list');
    list.replaceChildren();
    if (!data.voices.length) {
      const li = document.createElement('li');
      li.className = 'secondary';
      li.textContent = 'Nenhuma voz encontrada neste pacote. Veja "Que tipo de pacote funciona?" acima.';
      list.append(li);
    }
    data.voices.forEach((v) => {
      const li = document.createElement('li');
      li.className = 'imp-item';
      li.dataset.state = v.status;
      const box = document.createElement('input');
      box.type = 'checkbox';
      box.value = String(v.idx);
      box.checked = v.status === 'novo';
      box.disabled = v.status !== 'novo';
      box.id = 'imp-v' + v.idx;
      box.addEventListener('change', updateGo);
      const label = document.createElement('label');
      label.htmlFor = box.id;
      const name = document.createElement('strong');
      name.textContent = v.name;
      const meta = document.createElement('span');
      meta.className = 'field-help';
      meta.textContent = [
        v.language ? (LANG[v.language] || v.language.toUpperCase()) : '',
        v.gender === 'FEMALE' ? 'Feminina' : v.gender === 'MALE' ? 'Masculina' : '',
        v.kind === 'exportada' ? 'voz exportada · ' + v.samples + (v.samples === 1 ? ' amostra' : ' amostras') : '',
        Number.isFinite(v.duration_s) ? v.duration_s.toLocaleString('pt-BR') + ' s' : '',
        v.kind === 'audio' ? (v.has_transcript ? 'com transcrição' : 'sem transcrição') : '',
        v.source
      ].filter(Boolean).join(' · ');
      label.append(name, meta);
      if (v.motivo) {
        const why = document.createElement('span');
        why.className = 'imp-why';
        why.textContent = v.motivo;
        label.append(why);
      }
      const chip = document.createElement('span');
      chip.className = 'imp-chip';
      chip.textContent = STATUS[v.status] || v.status;
      li.append(box, label, chip);
      list.append(li);
    });
    $('imp-preview').hidden = false;
    updateGo();
  }

  async function readPack(event) {
    event.preventDefault();
    if (busy) return;
    const file = $('imp-file').files[0];
    if (!file) { setStatus('Escolha um arquivo .zip.', 'error'); return; }
    if (!/\.zip$/i.test(file.name)) { setStatus('O arquivo precisa ser .zip.', 'error'); return; }
    busy = true;
    $('imp-read').disabled = true;
    $('imp-preview').hidden = true;
    $('imp-progress').hidden = false;
    $('imp-progress').value = 0;
    setStatus('Enviando o pacote…');
    try {
      const data = await upload(file, $('imp-lang').value);
      setStatus('Confira a lista abaixo e clique em Importar. Nada foi gravado ainda.');
      renderPreview(data);
    } catch (error) {
      setStatus(error.message, 'error');
    } finally {
      busy = false;
      $('imp-read').disabled = false;
      $('imp-progress').hidden = true;
      updateGo();
    }
  }

  async function doImport() {
    if (!pack || busy) return;
    const indices = selectedIdx();
    if (!indices.length) return;
    busy = true;
    updateGo();
    setStatus('Importando ' + indices.length + (indices.length === 1 ? ' voz…' : ' vozes…'));
    try {
      const r = await fetch('/voice-packs/import', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: pack.token, indices, default_language: $('imp-lang').value, pack_name: pack.pack_name})
      });
      const body = await r.json().catch(() => null);
      if (!r.ok) throw new Error(detail(body, r.status));
      const c = body.counts;
      const fails = body.results.filter((x) => x.resultado === 'falhou');
      setStatus(c.importada + (c.importada === 1 ? ' voz importada' : ' vozes importadas') +
        (fails.length ? ' · ' + fails.length + ' falharam: ' + fails.map((f) => f.name + ' (' + f.motivo + ')').join('; ') : '') +
        '. Elas já aparecem em Voices e na tela Generate.', fails.length ? 'error' : 'ok');
      $('imp-preview').hidden = true;
      $('imp-form').reset();
      pack = null;
      window.dispatchEvent(new CustomEvent('vozlab:vozes-alteradas'));
    } catch (error) {
      setStatus(error instanceof TypeError ? serverDown() : error.message, 'error');
    } finally {
      busy = false;
      updateGo();
    }
  }

  window.addEventListener('DOMContentLoaded', () => {
    if (!$('imp-form')) return;
    $('imp-form').addEventListener('submit', readPack);
    $('imp-go').addEventListener('click', doImport);
    $('imp-cancel').addEventListener('click', () => {
      pack = null;
      $('imp-preview').hidden = true;
      setStatus('Importação cancelada. Nada foi gravado.');
    });
    $('imp-all').addEventListener('click', () => {
      document.querySelectorAll('#imp-list input[type=checkbox]:not(:disabled)').forEach((c) => { c.checked = true; });
      updateGo();
    });
    $('imp-none').addEventListener('click', () => {
      document.querySelectorAll('#imp-list input[type=checkbox]').forEach((c) => { c.checked = false; });
      updateGo();
    });
  });
})();
