# Arquitectura — MarketScope Research Engine v0.3

## Alcance

- El agente investigador corre en Claude Code o Codex. El servidor MCP local planifica, ejecuta conectores a APIs oficiales, valida, persiste y evalúa.
- La búsqueda web abierta y la lectura de páginas siguen en manos del agente (pasos `agent`), registradas en el MCP.
- Nada se marca `verified` automáticamente: evidencias y conclusiones entran como `candidate`. La verificación es humana e independiente.

Diseño detallado y decisiones: `docs/superpowers/specs/2026-11-16-research-engine-v0-3-design.md`.

## Módulos

| Módulo | Responsabilidad |
|---|---|
| `core.py` | validación de evidencias, SQLite (migraciones idempotentes, `PRAGMA user_version` = 7), catálogo, runs y eventos de auditoría |
| `studies.py` | contexto de mercado (geografía, segmento, idioma, `keywords_en`) |
| `planner.py` | plan determinista por (study, pregunta): fuentes por dimensión, filtro geográfico, exclusión por acceso, reparto de presupuesto, carencias, avisos de cobertura y población |
| `http_client.py` | cliente HTTP endurecido para conectores |
| `connectors/` | un módulo por fuente (18), con interfaz común en `base.py` |
| `executor.py` | ejecución de un paso, almacenamiento de hits y promoción a evidencia con extracto comprobado |
| `evaluator.py` | métricas de evidencia, veredicto permitido y validación de conclusiones |
| `bronze.py` | respuestas brutas inmutables, direccionadas por hash, solo de fuentes `approved` |
| `silver.py` | extractores deterministas y versionados: observaciones y documentos normalizados |
| `gold.py` | métricas declarativas (`catalog/metrics.json`) con linaje y promoción a evidencia derivada |
| `attribution.py` | citas exigidas por las licencias de cada conclusión |
| `maintenance.py` | retirada de evidencias borradas en origen (Bluesky, Mastodon) |
| `mcp_server.py`, `cli.py` | adaptadores |

## Flujo

```
create_study → begin_research → plan_research
  → next_step → execute_step (connector) | WebSearch/WebFetch + record_research_event + complete_agent_step (agent)
  → list_hits / get_hit → submit_evidence_from_hit | submit_evidence
  → evaluate_question → submit_conclusion → revisión humana
```

## Datos

SQLite (`PRAGMA user_version` = 8): `studies`, `research_runs`, `research_plans` (versiones inmutables), `research_events` (auditoría), `source_hits`, `evidence`, `conclusions`, `intake_events`, y las capas de datos:

| Capa | Dónde | Contenido |
|---|---|---|
| Bronze | `data/bronze/<fuente>/<fecha>/<sha256>.json.gz` + `bronze_objects` | respuesta bruta inmutable (solo fuentes `approved`; las `restricted` solo quedan indexadas) |
| Silver | `silver_observations`, `silver_documents` | valores normalizados (geografía ISO-2, periodos, unidad, multiplicador) y textos con grupo de autor y marca de borrado |
| Gold | `gold_metrics` + `catalog/metrics.json` | métricas calculadas con hash de la fórmula y los identificadores de todas las entradas |

Diseño de las capas: `docs/superpowers/specs/2026-11-16-data-model-bronze-silver-gold-design.md`.

## Seguridad del cliente HTTP

- Lista cerrada de hosts por conector; solo HTTPS y puerto 443; IP resuelta pública; sin redirecciones; tiempo máximo y tamaño máximo (5 MB).
- Límites de peticiones por fuente, cuotas diarias persistidas, `Retry-After` y `backoff`. Esperas de más de 30 s fallan en lugar de bloquear.
- 401/403, CAPTCHA o cuota agotada → `blocked` (sin reintento ni evasión). 429 o error de red → `failed`, reintentable solo de forma explícita.
- Las cabeceras y los cuerpos de petición nunca se registran. Los parámetros secretos se eliminan de las URL registradas. El contacto del User-Agent solo se envía a las APIs cuya política lo exige (`Policy.contact_env`).

## Calidad de la evidencia

- Hits ≠ evidencias: el agente promueve solo lo relevante, y el extracto debe estar literalmente en el fragmento guardado.
- Grupo de independencia por autor o entidad (usuario, comprador, empresa, dataset); varias piezas del mismo grupo cuentan como una.
- No se guardan nombres de usuario de plataformas sociales (ids numéricos, hashes o DID).
- Tendencias: solo periodos completos y normalizadas por la actividad de la plataforma.
- Conclusiones: veredicto acotado por reglas; las cifras deben aparecer en los extractos citados.

## Amenazas y limitaciones

- El contenido de páginas y APIs es no confiable: nunca se ejecutan sus instrucciones. Las consultas SPARQL usan plantillas fijas con entradas validadas.
- Validar estructura no valida veracidad. La revisión humana es obligatoria antes de usar una conclusión.
- La independencia se mide por fuente, no por linaje real (una ficha de Wikidata puede derivar de la web del propio competidor).
- El veredicto valora las afirmaciones citadas, no si la pregunta queda respondida por completo.
- Licencias del catálogo pendientes de revisión (`license_review: pending`): las evidencias de conectores se marcan `needs_review`; se guardan solo fragmentos cortos.
- Fuentes sin vía autorizada (Reddit con su política actual, Google sin acceso a la API de Ads, plataformas de reseñas comerciales) no se conectan.

## Evolución prioritaria

1. Flujo de revisión humana con historial inmutable de cambios de estado (`candidate` → `verified` / `rejected`), incluida la revisión de las definiciones de métricas (`reviewed`).
2. ETL reproducible: reprocesar Silver desde Bronze, comparar versiones de extractores y exportar a Parquet (fase 3).
3. Detección de linaje común entre fuentes y deduplicación semántica de evidencias.
4. Benchmark etiquetado de precisión de citas y cobertura.
