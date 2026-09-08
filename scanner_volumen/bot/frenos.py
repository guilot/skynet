"""Los frenos manuales de la Fase 3: los límites que un humano puede
accionar para cortar entradas nuevas sin tocar el gobierno de lo ya abierto.

**Los dos frenos cortan ENTRADAS nuevas, nunca la gestión de las posiciones
abiertas.** Dejar una posición apalancada sin gobierno -sin que se le sigan
moviendo el stop, sin que se cierre cuando toca- sería peor que el problema
que estos frenos existen para evitar. `BotRunner.on_tick` sigue avanzando el
bucle de las abiertas exactamente igual, freno activo o no; lo único que
cambia es que no se evalúan entradas nuevas.

- **Pérdida diaria máxima**: si el saldo actual ha caído más de
  `perdida_diaria_max` desde el saldo de referencia del día, no se abre nada
  más hasta que cambie el día UTC. La referencia se PERSISTE en `bot_meta`
  (nunca en un atributo de esta clase): bajo `Restart=always`, un reinicio
  en pleno frenazo que recalculara la referencia en memoria la fijaría sobre
  el saldo YA castigado -justo el día en que hace falta que no se mueva- y
  el bot seguiría operando.

  **De dónde sale "el saldo actual" (Task 11, corrección de un hallazgo de
  revisión):** `Frenos` acepta el mismo `proveedor_saldo` opcional que
  `LivePortfolio`. Sin él, tanto la referencia como la medida salen de
  `BotRepo.equity` (el saldo contable), igual que siempre. Con él, las DOS
  puntas -la referencia que se fija la primera vez que se consulta un día,
  y el saldo con el que se compara en cada llamada posterior- salen de la
  MISMA fuente. Esto no es cosmético: antes de este cambio, `_perdida_
  diaria_superada` medía siempre contra `BotRepo.equity`, así que un bot en
  modo real que dimensiona el margen sobre el saldo REAL del exchange
  (Step 1 de esta misma tarea) podía perder dinero de verdad por encima del
  tope configurado sin que el freno se enterase -el freno miraba una cifra
  y el dinero se regía por otra. Ver `_saldo_actual` y el docstring de
  `puede_abrir` para el efecto que esto tiene sobre el contrato de orden con
  `registrar_saldo_del_dia`.

  **Un `proveedor_saldo` puede fallar, y este freno no puede permitírselo**
  (segundo hallazgo de revisión, misma ronda): un valor no finito o no
  positivo (`nan`, `0.0`, negativo) NUNCA se usa para fijar ni persistir la
  referencia del día -ni desde `_saldo_actual` ni desde
  `registrar_saldo_del_dia`-, y en su lugar el freno se activa por
  precaución. La alternativa (dejar pasar un `nan` hasta el cálculo) es lo
  que se arregló: un `nan` colado en la primera consulta del día apagaba
  este freno hasta medianoche UTC, sobreviviendo incluso a un reinicio
  porque quedaba escrito en `bot_meta`.
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
import math
import os
from collections.abc import Callable
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


def _saldo_valido(saldo: float) -> bool:
    """Mismo criterio que `LivePortfolio.equity()` (Task 11): un saldo con
    el que se pueda medir algo tiene que ser un número finito y positivo.
    Compartido aquí porque `Frenos` tiene DOS puntos donde un `saldo`
    externo puede colarse -`_saldo_actual` (vía `proveedor_saldo`) y
    `registrar_saldo_del_dia` (vía su parámetro explícito)- y los dos deben
    fallar cerrado del mismo modo ante el mismo tipo de valor espurio."""
    return math.isfinite(saldo) and saldo > 0


class Frenos:
    """Los dos frenos manuales. `puede_abrir` es la única consulta que
    necesita el runner antes de evaluar entradas nuevas en cada tick."""

    def __init__(
        self, cfg_bot: BotConfig, repo: BotRepo, modo: str,
        proveedor_saldo: Callable[[], float] | None = None,
    ) -> None:
        """`proveedor_saldo` es el mismo tipo que recibe `LivePortfolio`
        (Task 11) y, en el cableado real, debe ser literalmente el MISMO
        callable inyectado ahí -no uno equivalente construido aparte-: es lo
        que garantiza que el freno y el tamaño de posición nunca lean cifras
        de fuentes distintas. Ver `_saldo_actual`."""
        self._cfg = cfg_bot
        self._repo = repo
        self._modo = modo
        self._proveedor_saldo = proveedor_saldo

    def puede_abrir(self, ahora: int) -> str | None:
        """El nombre del freno que impide abrir ahora mismo, o `None` si
        ninguno está activo.

        La parada de emergencia se comprueba primero: es la más barata (un
        `os.stat()` sin tocar la base de datos) y la que un humano puede
        querer que gane siempre, sin depender de en qué estado ande la
        pérdida diaria.

        SOBRE EL CONTRATO DE ORDEN con `registrar_saldo_del_dia` (matizado
        en la Task 11): la primera vez que se llama en un día UTC nuevo
        -desde este método o desde ese otro, el que llegue primero-, la
        referencia del día queda fijada con `_saldo_actual()` si nadie la
        fijó ya explícitamente. CON `proveedor_saldo` inyectado, ese valor
        por defecto es la MISMA fuente que usa `registrar_saldo_del_dia`
        cuando el cableador la invoca con el saldo real del exchange (que,
        para ser coherente, también debería salir de `proveedor_saldo`) -
        así que si `puede_abrir` corre primero un día, la referencia que
        fija por su cuenta ya no es una cifra distinta (el equity contable
        de antes), sino la misma que `registrar_saldo_del_dia` habría
        fijado. El orden dejó de poder producir el desajuste real
        (referencia de una fuente, medida de otra) que motivó este
        contrato en la Task 10 -sigue quedando la diferencia, sin
        importancia práctica, de que dos llamadas del mismo tick puedan leer
        el proveedor con un instante de por medio-. SIN `proveedor_saldo`
        (paper, o un cableado real que olvidó inyectarlo), el contrato
        ORIGINAL sigue aplicando tal cual: la referencia por defecto sale de
        `BotRepo.equity`, y `registrar_saldo_del_dia` sigue siendo la única
        vía para anclarla a otra cosa -y sigue teniendo que llegar antes."""
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

        ORDEN OBLIGATORIO (matizado, no eliminado, por la Task 11): esta
        llamada debería llegar ANTES que la primera llamada a `puede_abrir`
        del día. Si `puede_abrir` corre primero SIN que este freno tenga un
        `proveedor_saldo` inyectado, fija la referencia con el equity
        contable, y esta llamada -aunque traiga el saldo real del
        exchange- NO TIENE NINGÚN EFECTO. CON `proveedor_saldo` inyectado
        (y si `saldo` se saca de ese mismo proveedor, como debe), el orden
        deja de importar en la práctica: `puede_abrir` ya fijaría por su
        cuenta el mismo valor que esta llamada traería. La responsabilidad
        de que las dos puntas usen la misma fuente sigue siendo de quien
        cablea el bucle del bot -esta clase no puede verificar de dónde
        sale el `saldo` que se le pasa aquí.

        Mismo criterio de validez que `_saldo_actual` (ver el hallazgo de
        revisión ahí): un `saldo` no finito o no positivo NO se persiste
        como referencia -se ignora con un `log.error`, dejando la
        referencia del día sin fijar para que una llamada posterior (de
        aquí o de `puede_abrir`) con un valor válido pueda fijarla bien."""
        if not _saldo_valido(saldo):
            log.error(
                "bot: registrar_saldo_del_dia recibio un saldo invalido "
                "(%r); se ignora sin persistir ninguna referencia con ese "
                "valor", saldo,
            )
            return
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

    def _saldo_actual(self) -> float:
        """La cifra que gobierna el freno: la MISMA que `LivePortfolio.
        equity()` usaría en este instante, para que referencia y medida no
        puedan salir de fuentes distintas (ver el docstring de la clase).
        Sin `proveedor_saldo`, cae al equity contable, igual que siempre."""
        if self._proveedor_saldo is not None:
            return self._proveedor_saldo()
        return self._repo.equity(self._modo)

    def _perdida_diaria_superada(self, ahora: int) -> bool:
        saldo_actual = self._saldo_actual()
        if not _saldo_valido(saldo_actual):
            # Hallazgo de revisión: un valor espurio de `proveedor_saldo`
            # (`nan`, `0.0`, negativo -un caché sin inicializar en el
            # cableado, una lectura fallida que no se aisló antes de
            # llegar aquí-) NO puede fijar ni tocar la referencia del día.
            # Antes de esta guarda, un `nan` colado en la PRIMERA consulta
            # del día se persistía como referencia en `bot_meta`: a partir
            # de ahí `referencia <= 0` daba `False`, `perdida` salía `nan`,
            # y `nan >= tope` TAMBIÉN da `False` en Python -así que el
            # freno quedaba desactivado el resto del día UTC, y sobrevivía
            # a un reinicio porque la referencia envenenada ya estaba en
            # disco. Es un freno de emergencia: ante la duda, se frena (la
            # misma regla que ya rige `_parada_de_emergencia`), y frenar
            # aquí significa NO escribir nada en `bot_meta` con este valor
            # -ni siquiera como referencia por defecto-, para que la
            # próxima consulta con un saldo válido pueda fijarla bien.
            # El mensaje nombra la fuente REAL del valor: sin proveedor
            # inyectado (el caso de `paper`) el saldo sale del equity
            # contable, y decir "proveedor_saldo" mandaría a quien depura a
            # buscar un proveedor que no existe.
            origen = ("proveedor_saldo" if self._proveedor_saldo is not None
                      else "el equity contable de la base")
            log.error(
                "bot: %s devolvio un saldo invalido (%r) al medir la "
                "perdida diaria; se frena por precaucion sin fijar ni "
                "persistir ninguna referencia con ese valor",
                origen, saldo_actual,
            )
            return True
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
