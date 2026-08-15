import asyncio

from scanner_volumen.bitget.rate_limit import TokenBucket


async def test_permite_rafaga_inicial_hasta_la_capacidad():
    bucket = TokenBucket(rate_per_second=10, now=lambda: 0.0)
    inicio = asyncio.get_running_loop().time()
    for _ in range(10):
        await bucket.acquire()
    assert asyncio.get_running_loop().time() - inicio < 0.05


async def test_calcula_la_espera_cuando_se_agotan_los_tokens():
    """Con reloj inyectado no hay espera real: se comprueba el cálculo."""
    reloj = {"t": 0.0}
    esperas = []

    async def dormir_falso(segundos):
        esperas.append(segundos)
        reloj["t"] += segundos

    bucket = TokenBucket(
        rate_per_second=10, now=lambda: reloj["t"], sleep=dormir_falso
    )
    for _ in range(12):
        await bucket.acquire()

    assert len(esperas) == 2
    # cada token extra cuesta 1/10 de segundo
    assert all(abs(e - 0.1) < 1e-6 for e in esperas)


async def test_los_tokens_se_reponen_con_el_paso_del_tiempo():
    reloj = {"t": 0.0}
    bucket = TokenBucket(rate_per_second=10, now=lambda: reloj["t"])
    for _ in range(10):
        await bucket.acquire()
    reloj["t"] = 1.0  # ha pasado un segundo: se reponen 10 tokens
    esperas = []
    bucket._sleep = lambda s: esperas.append(s)  # no debería llamarse
    for _ in range(10):
        await bucket.acquire()
    assert esperas == []
