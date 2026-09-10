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
// --- historico de trades del bot ---

// Los rangos que guarda `bot_posiciones.max_rank` son los del estado del
// escaner (NORMAL=0 ... EXTREME=4). Se traducen aqui porque saber HASTA
// DONDE llego una posicion es la mitad de la historia de un trade: dos
// operaciones con el mismo PnL no significan lo mismo si una toco EXTREME
// y la otra murio en WATCH.
const NOMBRE_RANGO = ["NORMAL", "WATCH", "HOT", "SIGNAL", "EXTREME"];

// Los precios del universo van de 0.00001 a 100000, asi que un numero fijo
// de decimales queda mal en los dos extremos: "140.200000" sobra por la
// derecha y "0.001000" pierde la informacion por ella. Se ajusta a la
// magnitud, que es como lo muestra cualquier exchange.
function precio(v) {
  if (v === null || v === undefined) return "—";
  const a = Math.abs(v);
  if (a >= 1000) return num(v, 2);
  if (a >= 1) return num(v, 4);
  if (a >= 0.01) return num(v, 5);
  return num(v, 8);
}

// Duracion en la unidad que se lee de un vistazo, no en segundos crudos.
function duracion(desdeMs, hastaMs) {
  if (!desdeMs || !hastaMs) return "—";
  const min = Math.round((hastaMs - desdeMs) / 60000);
  if (min < 60) return `${min}m`;
  const h = Math.floor(min / 60);
  return `${h}h ${String(min % 60).padStart(2, "0")}m`;
}

function horaCorta(ms) {
  if (!ms) return "—";
  // Hora LOCAL del navegador, que es la que el operador tiene en su reloj.
  // El resto del panel usa el reloj del exchange para decidir cosas; esto
  // es solo presentacion de un instante ya ocurrido.
  const d = new Date(ms);
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")} `
       + `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// Etiquetas de los motivos de salida. Los valores crudos (`SCALE_HOT`) son
// los de `ExitReason` y no se traducen en la base -son datos-, solo aqui.
// Que trades tiene el usuario desplegados, y el desglose ya descargado de
// cada uno. Vive fuera de `pintarHistorico` porque esa funcion se ejecuta en
// CADA sondeo (5s) y no puede ser ella quien recuerde esto.
//
// El fallo que esto arregla: el panel repintaba el `tbody` entero cada 5
// segundos, asi que un desglose abierto se cerraba solo a los pocos
// segundos de abrirlo. Se cierra cuando el usuario lo dice, no cuando toca
// sondear.
const desplegados = new Map();   // id -> detalle ya descargado (o null si aun carga)
let firmaHistorico = null;

// Firma de la lista de cerrados. Si no cambia, no hay nada que repintar: el
// historico solo se mueve cuando se cierra un trade nuevo. Evita ademas el
// parpadeo de reconstruir 20 filas cada 5 segundos para dejarlas igual.
function firmaDe(cerradas) {
  return cerradas.map((t) => `${t.id}:${t.pnl}:${t.fases}`).join("|");
}

const MOTIVO = {
  SCALE_HOT: "parcial en HOT",
  SCALE_SIGNAL: "parcial en SIGNAL",
  EXTREME: "cierre tras EXTREME",
  STOP: "stop",
  STALE_BE: "estancada (break-even)",
  END_OF_DATA: "fin de datos",
};

async function alternarDetalle(fila) {
  const id = fila.dataset.id;
  if (desplegados.has(id)) {          // ya abierto -> el usuario lo cierra
    desplegados.delete(id);
    const sig = fila.nextElementSibling;
    if (sig && sig.classList.contains("detalle")) sig.remove();
    fila.classList.remove("abierto");
    return;
  }
  desplegados.set(id, null);
  await dibujarDetalle(fila);
}

// Separado de `alternarDetalle` porque tambien se usa al repintar, para
// restaurar lo que estaba abierto sin volver a pedirlo a la red.
async function dibujarDetalle(fila) {
  const id = fila.dataset.id;
  fila.classList.add("abierto");
  const tr = document.createElement("tr");
  tr.className = "detalle";
  tr.innerHTML = `<td colspan="11" class="apagado">cargando…</td>`;
  fila.after(tr);

  let d = desplegados.get(id);
  if (!d) {
    try {
      const r = await fetch(`/api/bot/trade/${id}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      d = await r.json();
    } catch (e) {
      tr.innerHTML = `<td colspan="11" class="perdida">no se pudo cargar el desglose: ${e.message}</td>`;
      return;
    }
    // Un trade cerrado no cambia nunca, asi que su desglose se cachea y el
    // repintado no vuelve a pedirlo.
    if (desplegados.has(id)) desplegados.set(id, d);
  }

  const filas = d.fases
    .map((f) => {
      const c = f.pnl_neto > 0 ? "ganancia" : (f.pnl_neto < 0 ? "perdida" : "");
      const sg = f.pnl_neto >= 0 ? "+" : "";
      // El desvio compara lo que la REGLA pedia con lo que el mercado dio.
      // Puede no existir: una salida a mercado por temporizador no promete
      // ningun nivel, y ahi un "0 bps" seria una medicion inventada.
      const desv = f.desvio_bps === null || f.desvio_bps === undefined
        ? "—" : `${f.desvio_bps >= 0 ? "+" : ""}${num(f.desvio_bps, 0)} bps`;
      return `<tr>
        <td>${MOTIVO[f.reason] ?? f.reason}</td>
        <td>${num(100 * f.fraction, 0)}%</td>
        <td>${precio(f.precio)}</td>
        <td class="apagado">${desv}</td>
        <td class="apagado">${num(f.comision)}</td>
        <td class="${c}">${sg}${num(f.pnl_neto)}</td>
      </tr>`;
    })
    .join("");

  // El funding NO se muestra porque el bot no lo registra, y poner un 0
  // seria afirmar que no se pago nada. En paper es literalmente cierto -no
  // hay exchange que lo cobre-; en real aparece solo agregado, como la
  // diferencia entre el saldo real y el equity calculado que ya ensena el
  // informe. Ademas estos trades duran minutos y el funding se cobra en
  // ventanas de 8h, asi que casi nunca llegan a cruzar una.
  tr.innerHTML = `<td colspan="11">
      <table class="fases">
        <thead><tr><th>FASE</th><th>PARTE</th><th>PRECIO</th>
                   <th>DESVIO</th><th>FEE</th><th>PNL</th></tr></thead>
        <tbody>${filas}</tbody>
      </table>
      <p class="nota-fases">
        Entrada ${precio(d.entry_price)} · tamano ${num(d.size, 4)} ·
        margen ${num(d.margin)} · fee de entrada ${num(d.fee_entrada)} ·
        <strong>fees totales ${num(d.fees_total)}</strong> ·
        PnL ${d.pnl >= 0 ? "+" : ""}${num(d.pnl)} USDT.
        El funding no se registra por trade (ver nota).
      </p>
    </td>`;
}

function pintarHistorico(cerradas) {
  const cuerpo = document.querySelector("#bot-cerradas tbody");
  const vacio = document.getElementById("bot-sin-trades");
  const tabla = document.getElementById("bot-cerradas");
  const resumen = document.getElementById("bot-resumen");

  if (!cerradas || cerradas.length === 0) {
    cuerpo.innerHTML = "";
    tabla.hidden = true;
    vacio.hidden = false;
    resumen.textContent = "";
    firmaHistorico = "";
    desplegados.clear();
    return;
  }
  tabla.hidden = false;
  vacio.hidden = true;

  // Si la lista no ha cambiado no se toca el DOM. El historico solo se mueve
  // cuando se cierra un trade, asi que en la inmensa mayoria de los sondeos
  // esto sale por aqui -y un desglose abierto ni se entera de que hubo
  // sondeo.
  const firma = firmaDe(cerradas);
  if (firma === firmaHistorico) return;

  // El resumen acompaña SIEMPRE al win rate con el total y el PnL: un win
  // rate alto con PnL negativo es perfectamente posible en esta estrategia
  // -las salidas escalonadas dejan cerrar muchos trades en verde recortando
  // los que se iban lejos-, asi que mostrarlo solo seria enganoso.
  const ganadores = cerradas.filter((t) => t.pnl > 0).length;
  const total = cerradas.reduce((a, t) => a + t.pnl, 0);
  const signo = total >= 0 ? "+" : "";
  resumen.textContent =
    `${cerradas.length} trades · ${ganadores} en verde `
    + `(${(100 * ganadores / cerradas.length).toFixed(0)}%) · `
    + `PnL ${signo}${num(total)} USDT`;
  resumen.className = total >= 0 ? "ganancia" : "perdida";

  cuerpo.innerHTML = cerradas
    .map((t) => {
      const clase = t.pnl > 0 ? "ganancia" : (t.pnl < 0 ? "perdida" : "");
      const s = t.pnl >= 0 ? "+" : "";
      // Sobre el MARGEN, que es lo que de verdad se arriesgo en ese trade,
      // no sobre el nocional apalancado ni sobre el equity total.
      const pct = t.margin ? (100 * t.pnl / t.margin) : null;
      return `<tr class="trade ${t.degradada ? "degradada" : ""}" data-id="${t.id}">
        <td>${t.symbol}</td>
        <td>${t.direction}</td>
        <td>${precio(t.entry_price)}</td>
        <td>${precio(t.exit_price)}</td>
        <td class="apagado">${horaCorta(t.entry_ts)}</td>
        <td class="apagado">${horaCorta(t.close_ts)}</td>
        <td class="apagado">${duracion(t.entry_ts, t.close_ts)}</td>
        <td class="apagado">${NOMBRE_RANGO[t.max_rank] ?? "—"}</td>
        <td class="apagado">${num(t.fees)}</td>
        <td class="${clase}">${s}${num(t.pnl)}</td>
        <td class="${clase}">${pct === null ? "—" : s + num(pct, 1) + "%"}</td>
      </tr>`;
    })
    .join("");

  // Delegacion sobre las filas ya pintadas: se re-crean en cada sondeo, asi
  // que enganchar aqui es mas simple que mantener un listener en el tbody
  // con estado de que fila estaba abierta.
  cuerpo.querySelectorAll("tr.trade").forEach((fila) => {
    fila.addEventListener("click", () => alternarDetalle(fila));
  });

  // Cuando SI hubo que repintar (se cerro un trade nuevo), se restaura lo
  // que el usuario tenia abierto. Sin red: el desglose de un trade cerrado
  // no cambia nunca y ya esta cacheado.
  if (desplegados.size > 0) {
    cuerpo.querySelectorAll("tr.trade").forEach((fila) => {
      if (desplegados.has(fila.dataset.id)) dibujarDetalle(fila);
    });
  }

  // La firma se marca AL FINAL, solo si se ha pintado de verdad. Marcarla
  // antes -como hacia la primera version- convertia cualquier excepcion
  // posterior en un fallo PERMANENTE: la tabla se quedaba vacia y los
  // sondeos siguientes salian por el atajo de "no ha cambiado nada" sin
  // volver a intentarlo nunca. Y como `refrescarBot` se traga la excepcion
  // con un `console.warn`, el sintoma era una tabla vacia sin ninguna pista.
  firmaHistorico = firma;
}

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
        <td>${precio(p.entry_price)}</td>
        <td>${num(p.margin)}</td>
      </tr>`)
    .join("");

  pintarHistorico(datos.cerradas);
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
