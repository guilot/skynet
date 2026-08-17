# scanner_volumen/app/outcomes.py
"""Registro del resultado posterior de cada señal.

Sin estos datos no hay forma de calibrar los pesos del score con evidencia. Se
recogen desde V1 porque el flujo de precios ya está disponible: si no se graban
ahora, dentro de tres meses simplemente no existen.
"""
from __future__ import annotations

from scanner_volumen.models import Candle
from scanner_volumen.storage.repos import CandleRepo, SignalRepo

MINUTO_MS = 60_000
HORIZONTES = (1, 5, 15, 30, 60)


def compute_outcome(
    entry_price: float, candles: list[Candle]
) -> tuple[float, float, float, float] | None:
    """Devuelve (precio_final, retorno_pct, mfe_pct, mae_pct).

    MFE es la máxima excursión favorable y MAE la máxima adversa, ambas
    medidas sobre los extremos de las velas (high/low), no sobre los
    cierres: son lo que una posición habría experimentado realmente.
    Devuelve None ante entrada vacía o precio de entrada no positivo, nunca
    lanza.
    """
    if not candles or entry_price <= 0:
        return None
    final = candles[-1].close
    maximo = max(c.high for c in candles)
    minimo = min(c.low for c in candles)
    return (
        final,
        (final / entry_price - 1) * 100,
        (maximo / entry_price - 1) * 100,
        (minimo / entry_price - 1) * 100,
    )


class OutcomeTracker:
    """Rellena `signal_outcomes` para los horizontes ya vencidos.

    Solo escribe un horizonte cuando el stream de velas progresó más allá
    del límite de la ventana; si aún no llegó tan lejos, se salta en
    silencio y se reintenta en la siguiente pasada. Grabar con una ventana
    parcial corrompería el propio dataset que este componente existe para
    producir.
    """

    def __init__(
        self,
        signal_repo: SignalRepo,
        candle_repo: CandleRepo,
        horizons: tuple[int, ...] = HORIZONTES,
    ) -> None:
        self._signals = signal_repo
        self._candles = candle_repo
        self._horizons = horizons

    def run_once(self, now_ms: int) -> int:
        escritos = 0
        for signal_id, symbol, precio, horizonte, ts in self._signals.pending_outcomes(
            now_ms, self._horizons
        ):
            # `ts` es el instante exacto de evaluación (el orquestador corre
            # cada segundo), no el arranque de una vela. Se redondea hacia
            # abajo al minuto para incluir la vela que estaba abierta en el
            # momento de la señal (la de entrada) y para que `fin` quede
            # alineado a un límite de vela alcanzable.
            inicio = ts - (ts % MINUTO_MS)
            fin = inicio + horizonte * MINUTO_MS
            # Ventana [inicio, fin] inclusive en ambos extremos.
            velas = [
                c for c in self._candles.load(symbol, since_ms=inicio) if c.ts <= fin
            ]
            ultimo_ts = self._candles.latest_ts(symbol)
            if not velas or ultimo_ts is None or ultimo_ts < fin:
                # El stream de velas todavía no progresó más allá del límite
                # de la ventana: horizonte vencido según el reloj, pero datos
                # incompletos. Se salta en silencio, nunca se graba con lo
                # que haya. Exigir progreso (en vez de la vela exacta en
                # `fin`) evita quedarse pendiente para siempre si un símbolo
                # poco líquido nunca publica esa vela concreta.
                continue
            resultado = compute_outcome(precio, velas)
            if resultado is None:
                continue
            final, ret, mfe, mae = resultado
            # Progreso más allá de `fin` no garantiza que la propia ventana
            # esté completa: puede haber un hueco interno (p. ej. una caída
            # de WS ya cubierta por `refill_gap`, o un símbolo poco líquido
            # que no publica todos los minutos) y `velas` simplemente omite
            # los minutos ausentes. Se registra cuántas velas se vieron
            # frente a cuántas debería haber (inicio y fin inclusive, cada
            # minuto) para que V3 pueda descartar o ponderar filas
            # calculadas sobre un hueco en vez de tratarlas como idénticas a
            # una ventana completa.
            velas_esperadas = (fin - inicio) // MINUTO_MS + 1
            self._signals.save_outcome(
                signal_id, horizonte, final, ret, mfe, mae,
                candles_seen=len(velas), candles_expected=velas_esperadas,
            )
            escritos += 1
        return escritos
