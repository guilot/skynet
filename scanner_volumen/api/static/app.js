const ORDEN_ESTADOS = { NORMAL: 0, WATCH: 1, HOT: 2, SIGNAL: 3, EXTREME: 4 };
const estadosPrevios = new Map();
let seleccionado = null;
let ultimoEstado = null;

const num = (v, d = 2) => (v === null || v === undefined ? "—" : v.toFixed(d));
const pct = (v) => {
  if (v === null || v === undefined) return '<span>—</span>';
  const clase = v >= 0 ? "pos" : "neg";
  return `<span class="${clase}">${v >= 0 ? "+" : ""}${v.toFixed(2)}%</span>`;
};

function avisar() {
  if (!document.getElementById("sonido").checked) return;
  const ctx = new (window.AudioContext || window.webkitAudioContext)();
  const osc = ctx.createOscillator();
  const gan = ctx.createGain();
  osc.connect(gan); gan.connect(ctx.destination);
  osc.frequency.value = 880; gan.gain.value = 0.08;
  osc.start(); osc.stop(ctx.currentTime + 0.18);
}

function pintar(datos) {
  ultimoEstado = datos;
  const conexion = document.getElementById("conexion");
  conexion.textContent = datos.connected ? "conectado" : "sin conexión";
  conexion.className = "pastilla " + (datos.connected ? "ok" : "mal");

  // I1: badge independiente para el WebSocket -única fuente de velas-, que
  // antes no tenía ninguna representación en el estado: un WS muerto con el
  // refresco de universo aún sano dejaba el badge de arriba en verde.
  const wsConexion = document.getElementById("ws-conexion");
  wsConexion.textContent = datos.ws_connected ? "velas: conectado" : "velas: caído";
  wsConexion.className = "pastilla " + (datos.ws_connected ? "ok" : "mal");

  const b = datos.bootstrap;
  document.getElementById("bootstrap").textContent =
    `perfil: ${b.done}/${b.total || "—"}`;

  const dir = document.getElementById("filtro-direccion").value;
  const minEstado = parseInt(document.getElementById("filtro-estado").value, 10);

  const filas = datos.rows.filter(
    (f) => (!dir || f.direction === dir) && ORDEN_ESTADOS[f.state] >= minEstado
  );

  document.getElementById("filas").innerHTML = filas
    .map((f, i) => {
      const previo = estadosPrevios.get(f.symbol);
      if (previo !== undefined && ORDEN_ESTADOS[f.state] > previo &&
          ORDEN_ESTADOS[f.state] >= ORDEN_ESTADOS.SIGNAL) {
        avisar();
      }
      estadosPrevios.set(f.symbol, ORDEN_ESTADOS[f.state]);
      const dudoso = f.profile_confidence === "low" ? "baja-confianza" : "";
      // I1/I-2(a): fila marcada como obsoleta si no se actualiza desde hace
      // más del umbral configurado (`dashboard.stale_after_seconds`), señal
      // de que ese símbolo dejó de recibir velas aunque el badge general
      // siga verde. Se compara contra `datos.now_ms` -el reloj del
      // EXCHANGE que manda el propio servidor-, nunca contra `Date.now()`
      // (el reloj del NAVEGADOR): un visitante con el reloj desfasado no
      // debe poder activar o desactivar este marcador él solo.
      const obsoleta = datos.now_ms - f.updated_ms > datos.stale_after_ms ? "obsoleta" : "";
      return `<tr class="${f.state} ${obsoleta}" data-symbol="${f.symbol}">
        <td>${i + 1}</td>
        <td class="${dudoso}">${f.symbol}</td>
        <td>${f.direction}</td>
        <td>${num(f.price, 4)}</td>
        <td>${pct(f.ret_24h)}</td>
        <td>${pct(f.ret_5m)}</td>
        <td>${num(f.rvol_1m)}x</td>
        <td>${num(f.rvol_5m)}x</td>
        <td>${num(f.demand_burst)}x</td>
        <td>${pct(f.vwap_distance)}</td>
        <td><strong>${num(f.score, 0)}</strong></td>
        <td>${f.state}</td>
      </tr>`;
    })
    .join("");

  if (seleccionado) pintarDetalle(seleccionado);
}

function pintarDetalle(symbol) {
  const f = ultimoEstado?.rows.find((r) => r.symbol === symbol);
  const panel = document.getElementById("detalle");
  if (!f) { panel.hidden = true; return; }
  panel.hidden = false;
  const comps = Object.entries(f.components)
    .map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  panel.innerHTML = `<strong>${f.symbol}</strong> — ${f.state} (${f.direction})
    <dl>
      <dt>score</dt><dd>${num(f.score, 1)}</dd>
      <dt>momentum</dt><dd>${num(f.score_momentum, 1)} / 40</dd>
      <dt>demand</dt><dd>${num(f.score_demand, 1)} / 40</dd>
      <dt>structure</dt><dd>${num(f.score_structure, 1)} / 20</dd>
      <dt>rvol 1m vivo</dt><dd>${num(f.rvol_1m_live)}x</dd>
      <dt>rvol sesión</dt><dd>${num(f.rvol_session)}x</dd>
      <dt>z-score</dt><dd>${num(f.z_return)}</dd>
      <dt>vwap</dt><dd>${num(f.vwap, 4)}</dd>
      <dt>market cap</dt><dd>${f.market_cap ? (f.market_cap / 1e6).toFixed(0) + "M" : "—"}</dd>
      <dt>perfil</dt><dd>${f.profile_confidence}</dd>
      ${comps}
    </dl>`;
}

document.getElementById("filas").addEventListener("click", (e) => {
  const fila = e.target.closest("tr");
  if (!fila) return;
  seleccionado = seleccionado === fila.dataset.symbol ? null : fila.dataset.symbol;
  pintarDetalle(seleccionado);
});
for (const id of ["filtro-direccion", "filtro-estado"]) {
  document.getElementById(id).addEventListener("change", () => {
    if (ultimoEstado) pintar(ultimoEstado);
  });
}

function conectar() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (ev) => pintar(JSON.parse(ev.data));
  ws.onclose = () => {
    document.getElementById("conexion").className = "pastilla mal";
    setTimeout(conectar, 2000);
  };
}
conectar();

// El bot vive en su propia tabla (bot_posiciones), no en ScannerState: no
// viaja en el payload del `/ws` de arriba. Un fetch con su propio
// setInterval -en vez de forzarlo dentro del WebSocket del estado- deja las
// dos fuentes de datos separadas, igual que ya lo están en el backend.
function pintarBot(datos) {
  const seccion = document.getElementById("bot");
  if (!datos.activo) { seccion.hidden = true; return; }
  seccion.hidden = false;
  // Task 11: dinero real en juego solo cuando el modo efectivo es "real"
  // (a diferencia de "real_lectura", que conecta con Bitget pero no manda
  // ordenes) -distinto de un vistazo es la mitigacion mas barata contra
  // confundir una sesion de pruebas con una que mueve dinero de verdad.
  const dineroReal = datos.modo === "real";
  seccion.classList.toggle("dinero-real", dineroReal);
  const modoEl = document.getElementById("bot-modo");
  modoEl.textContent = dineroReal ? `${datos.modo} — DINERO REAL` : datos.modo;
  modoEl.className = "pastilla" + (dineroReal ? " real" : "");
  // Ronda de arreglo: con dinero real de verdad, mostrar el SALDO REAL
  // (persistido por el proceso en vivo, `BotRepo.set_saldo_real`) en vez
  // del equity contable -el mismo numero que el aviso "DINERO REAL" de
  // arriba estaria contradiciendo si siguiera mostrando el contable. Si el
  // proceso aun no lo ha persistido (`null`), se cae al contable con la
  // misma honestidad que ya usa el informe (nunca fingir un dato que no
  // se tiene).
  const equityEl = document.getElementById("bot-equity");
  if (dineroReal && datos.saldo_real !== null && datos.saldo_real !== undefined) {
    equityEl.textContent = `${num(datos.saldo_real)} (contable: ${num(datos.equity)})`;
  } else {
    equityEl.textContent = num(datos.equity);
  }
  document.querySelector("#bot-abiertas tbody").innerHTML = datos.abiertas
    .map((p) => `<tr>
        <td>${p.symbol}</td>
        <td>${p.direction}</td>
        <td>${num(p.entry_price, 6)}</td>
        <td>${num(p.margin)}</td>
      </tr>`)
    .join("");
}

async function refrescarBot() {
  try {
    const r = await fetch("/api/bot");
    pintarBot(await r.json());
  } catch (exc) {
    // un fallo puntual del fetch no debe tumbar el resto del dashboard;
    // el próximo setInterval lo reintenta solo.
    console.warn("no se pudo refrescar el bot:", exc);
  }
}
setInterval(refrescarBot, 5000);
refrescarBot();
