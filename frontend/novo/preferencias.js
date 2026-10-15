// Preferências da tela guardadas também no VozLab (15/10/2026).
// No Edge o VozLab abre em InPrivate (para não entrar sozinho na conta Microsoft) e o InPrivate esquece o
// armazenamento do navegador ao fechar. Este arquivo roda antes do app.js: traz as escolhas guardadas no
// VozLab para o navegador e, a cada escolha nova, manda uma cópia para o VozLab.
(() => {
  'use strict';
  const nossa = (k) => typeof k === 'string' && (k.startsWith('vozlab_') || k.startsWith('voicebox_'));
  let guardadas = {};
  try {
    const x = new XMLHttpRequest();
    x.open('GET', '/preferencias-interface', false);  // síncrono de propósito: o app.js lê logo ao abrir
    x.send();
    if (x.status === 200) guardadas = JSON.parse(x.responseText) || {};
  } catch { return; }
  const enviar = (dados) => {
    try {
      fetch('/preferencias-interface', {method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(dados), keepalive: true}).catch(() => {});
    } catch { /* sem servidor: fica só nesta tela */ }
  };
  try {
    Object.entries(guardadas).forEach(([k, v]) => { if (nossa(k)) localStorage.setItem(k, v); });
    // Quem já usava o VozLab: leva para o VozLab o que só estava no navegador.
    const soAqui = {};
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i);
      if (nossa(k) && !(k in guardadas)) soAqui[k] = localStorage.getItem(k);
    }
    if (Object.keys(soAqui).length) enviar(soAqui);
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (k, v) {
      original.call(this, k, v);
      if (this === window.localStorage && nossa(k)) enviar({[k]: String(v)});
    };
  } catch { /* navegador sem armazenamento: o app.js já trata */ }
})();
