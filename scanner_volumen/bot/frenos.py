"""Los frenos manuales de la Fase 3: los límites que un humano puede
accionar para cortar entradas nuevas sin tocar el gobierno de lo ya abierto.

**Los dos frenos cortan ENTRADAS nuevas, nunca la gestión de las posiciones
abiertas.** Dejar una posición apalancada sin gobierno -sin que se le sigan
moviendo el stop, sin que se cierre cuando toca- sería peor que el problema
que estos frenos existen para evitar. `BotRunner.on_tick` sigue avanzando el
bucle de las abiertas exactamente igual, freno activo o no; lo único que
cambia es que no se evalúan entradas nuevas.

- **Pérdida diaria máxima**: si el equity actual (`BotRepo.equity`, el saldo
  vivo del bot) ha caído más de `perdida_diaria_max` desde el saldo de
  referencia del día, no se abre nada más hasta que cambie el día UTC. La
  referencia se PERSISTE en `bot_meta` (nunca en un atributo de esta clase):
  bajo `Restart=always`, un reinicio en pleno frenazo que recalculara la
  referencia en memoria la fijaría sobre el saldo YA castigado -justo el día
  en que hace falta que no se mueva- y el bot seguiría operando.
- **Parada de emergencia**: si existe `fichero_parada` en disco, no se abre
  nada. Se comprueba con `os.stat()` en cada llamada -una consulta barata
  al sistema de ficheros-, lo que permite cortar desde SSH creando el
  fichero, y reanudar borrándolo, sin reiniciar el proceso. Si la propia
  comprobación no puede determinar si el fichero existe (p. ej. un
  `PermissionError` en algún directorio de la ruta), se frena igualmente
  -por diseño, no por accidente: ver `_parada_de_emergencia`.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from scanner_volumen.bot.repo import BotRepo
from scanner_volumen.config import BotConfig

log = logging.getLogger(__name__)

# Nombres que devuelve `puede_abrir`: se persisten tal cual como clave de
# `bot_contadores` (ver `BotRunner.on_tick`), así que también son las
# etiquetas que aparecen en el informe -deben coincidir con las que se
# añaden a `ETIQUETAS_DESCARTE` en `bot/model.py`.
MOTIVO_PERDIDA_DIARIA = "perdida diaria"
MOTIVO_PARADA_EMERGENCIA = "parada de emergencia"


def _dia_utc(ahora: int) -> str:
    """`ahora` (epoch ms) -> `"AAAA-MM-DD"` en UTC.

    Nunca en el reloj local: todo el proyecto ancla el tiempo en el reloj
    del exchange -el `ahora` que recibe `on_tick`-, no en el de la máquina
    donde corre el proceso."""
    return datetime.fromtimestamp(ahora / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


class Frenos:
    """Los dos frenos manuales. `puede_abrir` es la única consulta que
    necesita el runner antes de evaluar entradas nuevas en cada tick."""

    def __init__(self, cfg_bot: BotConfig, repo: BotRepo, modo: str) -> None:
        self._cfg = cfg_bot
        self._repo = repo
        self._modo = modo

    def puede_abrir(self, ahora: int) -> str | None:
        """El nombre del freno que impide abrir ahora mismo, o `None` si
        ninguno está activo.

        La parada de emergencia se comprueba primero: es la más barata (un
        `os.stat()` sin tocar la base de datos) y la que un humano puede
        querer que gane siempre, sin depender de en qué estado ande la
        pérdida diaria.

        CONTRATO DE ORDEN con `registrar_saldo_del_dia`: la primera vez que
        se llama en un día UTC nuevo -desde este método o desde ese otro,
        el que llegue primero-, la referencia del día queda fijada. Si nadie
        llamó antes a `registrar_saldo_del_dia`, ESTE método la fija con el
        equity derivado del bot (`BotRepo.equity`). Si el cableador quiere
        anclar la referencia a otro saldo -p. ej. el saldo real del
        exchange en modo real, más fiable que la contabilidad reconstruida
        cuando hay dinero de verdad en juego-, tiene que llamar a
        `registrar_saldo_del_dia` ANTES de la primera llamada a este método
        en ese día: si llega después, ya no tiene ningún efecto (ver el
        docstring de `registrar_saldo_del_dia`)."""
        if self._parada_de_emergencia():
            return MOTIVO_PARADA_EMERGENCIA
        if self._perdida_diaria_superada(ahora):
            return MOTIVO_PERDIDA_DIARIA
        return None

    def registrar_saldo_del_dia(self, ahora: int, saldo: float) -> None:
        """Fija el saldo de referencia de HOY (UTC) a `saldo`, si todavía no
        hay uno persistido para este día -y modo-.

        No lo sobrescribe si ya existe: la referencia se fija una sola vez
        por día y se respeta después, sin importar cuántas veces se vuelva
        a llamar ni con qué `saldo` -incluido tras un reinicio, que es
        justo el caso que garantiza que esto sea seguro con dinero real.

        ORDEN OBLIGATORIO: esta llamada debe llegar ANTES que la primera
        llamada a `puede_abrir` del día. Si `puede_abrir` corre primero, ya
        fija la referencia por su cuenta -con el equity derivado del bot,
        no con el `saldo` que se le pase aquí después-, y esta llamada,
        aunque traiga un valor distinto (el saldo real del exchange, por
        ejemplo), NO TIENE NINGÚN EFECTO: llega tarde a una referencia que
        ya quedó fijada. Ninguno de los dos métodos puede detectar ese
        orden incorrecto desde dentro de esta clase -comparten la misma
        regla de "quien pregunta primero, fija"-, así que respetar el orden
        es responsabilidad de quien cablea el bucle del bot."""
        self._referencia_del_dia(ahora, saldo)

    def _parada_de_emergencia(self) -> bool:
        """True si hay que frenar por el fichero de parada.

        Deliberadamente NO usa `Path.exists()`: en CPython (comprobado en
        3.14, y así desde que `pathlib` delega en `os.path.exists`) esa
        llamada atrapa `(OSError, ValueError)` puertas adentro y devuelve
        `False` para CUALQUIER fallo, incluido un directorio de la ruta
        vuelto ilegible (`PermissionError`) -exactamente el caso que un
        freno de emergencia no puede permitirse leer como "no hay
        parada". `exists()` colapsa "el fichero no está" y "no puedo saber
        si está" en el mismo `False`, y ese colapso es el agujero: con
        `Path.exists()`, el único escenario de permisos que puede darse de
        verdad en producción hace que el freno falle ABIERTO -el bot sigue
        operando- justo cuando debería fallar cerrado.

        Por eso se usa `os.stat()` directamente y se distinguen los tres
        casos por separado:

        - `FileNotFoundError`: el fichero está ausente de verdad. Sin freno.
        - Cualquier otro `OSError` (`PermissionError` incluido): no se puede
          determinar si el fichero existe. Freno activo, con log -ante la
          duda, un freno de EMERGENCIA tiene que asumir que la respuesta es
          sí, nunca dejar el bot operando porque no pudo ni preguntar.
        - Sin excepción: el fichero está. Freno activo."""
        try:
            os.stat(self._cfg.fichero_parada)
        except FileNotFoundError:
            return False
        except OSError:
            log.exception(
                "bot: no se pudo determinar si existe el fichero de parada "
                "de emergencia (%r); se frena por precaucion",
                self._cfg.fichero_parada,
            )
            return True
        return True

    def _perdida_diaria_superada(self, ahora: int) -> bool:
        saldo_actual = self._repo.equity(self._modo)
        referencia = self._referencia_del_dia(ahora, saldo_actual)
        if referencia <= 0:
            # sin saldo de referencia positivo no hay sobre qué medir una
            # fracción de pérdida con sentido.
            return False
        perdida = (referencia - saldo_actual) / referencia
        return perdida >= self._cfg.perdida_diaria_max

    def _referencia_del_dia(self, ahora: int, saldo_por_defecto: float) -> float:
        """El saldo de referencia de hoy (UTC), persistido en `bot_meta` por
        día y modo (`BotRepo.saldo_dia` / `fijar_saldo_dia`).

        La primera vez que se consulta un día -desde `puede_abrir` o desde
        `registrar_saldo_del_dia`, da igual cuál llegue primero- se fija a
        `saldo_por_defecto` y esa queda como la referencia del día;
        cualquier consulta posterior, de esta instancia o de una creada
        después de un reinicio, respeta el valor ya guardado."""
        dia = _dia_utc(ahora)
        referencia = self._repo.saldo_dia(self._modo, dia)
        if referencia is None:
            self._repo.fijar_saldo_dia(self._modo, dia, saldo_por_defecto)
            return saldo_por_defecto
        return referencia
