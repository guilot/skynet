from scanner_volumen.models import Direction, State
from scanner_volumen.strategy.model import (
    ExitReason, Fill, ResumenOperativa, TradeResumen,
)
from scanner_volumen.strategy.report import format_resumen

MIN = 60_000


def _trade(symbol="X", pnl=10.0, max_rank=State.SIGNAL.rank, reason=ExitReason.EXTREME):
    fill = Fill(ts=MIN, price=110.0, fraction=1.0, reason=reason)
    return TradeResumen(
        symbol=symbol, direction=Direction.LONG, entry_ts=0, entry_price=100.0,
        close_ts=MIN, fills=(fill,), fill_pnls=(pnl,), margin=20.0,
        pnl=pnl, fees=0.5, max_rank=max_rank,
    )


def _resumen(trades, descartes=None):
    return ResumenOperativa(
        titulo="Prueba", trades=tuple(trades),
        descartes=descartes or {"NEUTRAL": 0},
        equity_inicial=1000.0, equity_final=1000.0 + sum(t.pnl for t in trades),
        ts_min=0, ts_max=MIN, total_transiciones=7, max_concurrentes_alcanzado=2,
    )


def test_cabecera_y_ventana():
    salida = format_resumen(_resumen([_trade()]))
    assert salida.startswith("== Prueba ==")
    assert "7 transiciones" in salida


def test_descartes_se_alinean_a_28_caracteres():
    # el informe del backtest alinea a mano: "descartes <etiqueta>:" queda
    # rellenado a 28 caracteres, de modo que "tope concurrencia" pega con su
    # numero. El golden master compara caracter a caracter.
    salida = format_resumen(_resumen([], descartes={
        "NEUTRAL": 0, "tope concurrencia": 3,
    }))
    assert "  descartes NEUTRAL:          0" in salida
    assert "  descartes tope concurrencia:3" in salida


def test_equity_win_rate_y_medias():
    salida = format_resumen(_resumen([_trade(pnl=10.0), _trade(pnl=-4.0)]))
    assert "Equity: 1000.00 -> 1006.00 (+0.60%)" in salida
    assert "Win rate: 50.0%  (1/2)" in salida
    assert "Media ganancia: +10.00   Media perdida: -4.00" in salida


def test_separa_runners_de_arrastre():
    salida = format_resumen(_resumen([
        _trade(symbol="A", pnl=20.0, max_rank=State.SIGNAL.rank),
        _trade(symbol="B", pnl=-5.0, max_rank=State.HOT.rank),
    ]))
    assert "Runners (alcanzan SIGNAL+): 1 trades, PnL +20.00" in salida
    assert "Arrastre (no pasan de HOT): 1 trades, PnL -5.00" in salida


def test_pnl_por_motivo_lista_todos_los_motivos():
    salida = format_resumen(_resumen([_trade(reason=ExitReason.STOP, pnl=-3.0)]))
    assert "  STOP         -3.00" in salida
    assert "  SCALE_HOT    +0.00" in salida  # motivos sin uso salen a cero


def test_cierres_por_fin_de_datos_se_separan():
    salida = format_resumen(_resumen([_trade(reason=ExitReason.END_OF_DATA, pnl=2.0)]))
    assert "Cerrados por fin de datos (no son salidas reales): 1 (PnL +2.00)" in salida
