"""Punto de entrada: demo del flujo de delegación (genera la traza) o chat interactivo.

Uso:
    python main.py                       # demo guionada → traces/
    python main.py --chat --thread-id t1  # chat con memoria por thread_id
    python main.py --reindexar            # vuelve a indexar data/docs en Chroma
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from langchain_chroma import Chroma
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphRecursionError

from agents import es_sin_saldo
from agents.supervisor import MAX_DELEGACIONES, MAX_POR_ESPECIALISTA, MAX_RECHAZOS_VALIDACION
from graph import (
    RECURSION_LIMIT,
    ROLES,
    Modelos,
    abrir_checkpointer,
    build_graph,
    modelo_llm,
    proveedor_llm,
    verificar_configuracion,
)
from state import Contribucion
from tools.custom_tools import ingestar

DB_PATH = "checkpoints.db"
TRACES_DIR = Path("traces")

log = logging.getLogger("orquestador")


def config_de(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


def _linea(c: Contribucion) -> str:
    fuentes = f" [fuentes: {', '.join(c.fuentes)}]" if c.fuentes else ""
    return f"  [paso {c.paso}] {c.agente} → {c.accion}: {c.detalle}{fuentes} ({c.duracion_s}s)"


async def ejecutar_turno(graph: Any, thread_id: str, pregunta: str) -> dict[str, Any]:
    """Corre un turno y devuelve su traza: la bitácora de contribuciones nodo por nodo."""
    log.info("\n[%s] Usuario: %s", thread_id, pregunta)
    pasos: list[dict[str, Any]] = []
    try:
        async for update in graph.astream(
            {"messages": [HumanMessage(pregunta)]}, config_de(thread_id), stream_mode="updates"
        ):
            for nodo, salida in update.items():
                for c in (salida or {}).get("contribuciones", []):
                    log.info(_linea(c))
                    pasos.append({"nodo": nodo, **c.model_dump()})
    except GraphRecursionError:
        # Red de seguridad: con los topes del supervisor no debería ocurrir.
        log.warning("  ✖ Se alcanzó recursion_limit=%d.", RECURSION_LIMIT)
        pasos.append({"nodo": "-", "accion": f"corte por recursion_limit={RECURSION_LIMIT}"})

    estado = (await graph.aget_state(config_de(thread_id))).values
    turno = estado.get("turno")
    respuesta = estado.get("respuesta_final", "")
    log.info("\nRespuesta: %s", respuesta)
    return {
        "thread_id": thread_id,
        "turno": turno,
        "usuario": pregunta,
        "delegaciones": estado.get("delegaciones", {}),
        "rechazos_validacion": estado.get("rechazos_validacion", 0),
        "pasos": pasos,
        # Lo que cada especialista dejó en su parcela del estado en este turno.
        "investigaciones": [r.model_dump() for r in estado.get("investigaciones", []) if r.turno == turno],
        "analisis": [r.model_dump() for r in estado.get("analisis", []) if r.turno == turno],
        "respuesta_final": respuesta,
    }


DEMO: list[tuple[str, str]] = [
    # 1) Investigación + análisis: umbral y límite (docs) → porcentaje, margen y comparación (cálculo).
    (
        "sesion-pool",
        "Hoy PgBouncer tiene 87 conexiones activas sostenidas hace 3 minutos. ¿Ya debería haber saltado "
        "la alerta del pool de PostgreSQL? ¿A qué porcentaje del límite estamos, cuánto margen queda y "
        "qué indica el runbook que hagamos?",
    ),
    # 2) Seguimiento en el mismo thread: "esas conexiones" y "el límite temporal" salen de la memoria.
    (
        "sesion-pool",
        "Si aplicamos la resolución del runbook y subimos temporalmente el límite, ¿a qué porcentaje "
        "quedarían esas mismas conexiones y cuántas más podríamos sumar antes de volver a disparar la alerta?",
    ),
    # 3) Dos documentos distintos y dos umbrales a comparar.
    (
        "sesion-canario",
        "Durante un canario medimos 1.6% de errores 5xx durante 6 minutos seguidos. ¿Se revierte "
        "automáticamente el despliegue? ¿Dispara la alerta de 5xx de monitoreo? Compará los dos umbrales.",
    ),
    # 4) Fuera de dominio: el investigador no encuentra nada → "no lo sé", sin inventar.
    ("sesion-fuera", "¿Cuál es la cotización del dólar hoy y cuánto costarían 3 servidores nuevos?"),
]


async def demo(
    modelos: Modelos | None = None, db_path: str = DB_PATH, vectorstore: Chroma | None = None
) -> list[dict[str, Any]]:
    turnos: list[dict[str, Any]] = []
    async with abrir_checkpointer(db_path) as saver:
        # La demo arranca siempre de cero: borra sus threads de corridas anteriores.
        for thread_id in {t for t, _ in DEMO}:
            await saver.adelete_thread(thread_id)
        graph = build_graph(saver, modelos, vectorstore)
        for thread_id, pregunta in DEMO:
            turnos.append(await ejecutar_turno(graph, thread_id, pregunta))
    return turnos


async def chat(thread_id: str) -> None:
    async with abrir_checkpointer(DB_PATH) as saver:
        graph = build_graph(saver)
        print(f"Chat con el equipo (thread_id={thread_id}). 'salir' para terminar.")
        while (pregunta := input("\nVos: ").strip()).lower() not in {"salir", "exit"}:
            if pregunta:
                await ejecutar_turno(graph, thread_id, pregunta)


def _configurar_logs() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])
    for ruidoso in ("httpx", "anthropic", "openai", "chromadb", "sentence_transformers", "huggingface_hub"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


def _quitar_handler(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()  # en Windows no se puede reemplazar el archivo si sigue abierto


async def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Orquestador multi-agente (supervisor + especialistas)")
    parser.add_argument("--chat", action="store_true", help="modo interactivo")
    parser.add_argument("--thread-id", default="sesion-1")
    parser.add_argument("--reindexar", action="store_true", help="vuelve a indexar data/docs y sale")
    args = parser.parse_args()
    _configurar_logs()

    # Ingesta idempotente (pre-entrega 3): solo indexa si la colección está vacía.
    await asyncio.to_thread(ingestar, forzar=args.reindexar)
    if args.reindexar:
        return
    verificar_configuracion()
    if args.chat:
        await chat(args.thread_id)
        return

    # El log se escribe en un .tmp y solo reemplaza la traza anterior si la demo termina bien.
    TRACES_DIR.mkdir(exist_ok=True)
    log_final, log_tmp = TRACES_DIR / "traza_delegacion.log", TRACES_DIR / "traza_delegacion.log.tmp"
    archivo = logging.FileHandler(log_tmp, mode="w", encoding="utf-8")
    logging.getLogger().addHandler(archivo)
    modelos = {rol: modelo_llm(rol) for rol in ROLES}
    log.info("Proveedor: %s · modelos: %s · recursion_limit=%d", proveedor_llm(), modelos, RECURSION_LIMIT)
    try:
        turnos = await demo()
    except BaseException:
        _quitar_handler(archivo)
        log_tmp.unlink(missing_ok=True)
        raise
    _quitar_handler(archivo)
    log_tmp.replace(log_final)
    salida = {
        "generado": datetime.now().isoformat(timespec="seconds"),
        "proveedor": proveedor_llm(),
        "modelos": modelos,
        "limites": {
            "max_delegaciones": MAX_DELEGACIONES,
            "max_por_especialista": MAX_POR_ESPECIALISTA,
            "max_rechazos_validacion": MAX_RECHAZOS_VALIDACION,
            "recursion_limit": RECURSION_LIMIT,
        },
        "turnos": turnos,
    }
    (TRACES_DIR / "traza_delegacion.json").write_text(
        json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log.info("\nTraza guardada en %s/", TRACES_DIR)


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit("\nInterrumpido.")
    except Exception as e:
        # Errores del proveedor (API key inválida, sin crédito, sin red...): mensaje corto.
        if type(e).__module__.split(".")[0] in {"anthropic", "openai"}:
            if es_sin_saldo(e):
                sys.exit(f"✖ La cuenta de {proveedor_llm()} no tiene saldo: cargá crédito en Plans & Billing y reintentá.")
            sys.exit(f"✖ Error del proveedor de LLM ({proveedor_llm()}): {e}\n  Revisá la API key en el .env.")
        raise


if __name__ == "__main__":
    run()
