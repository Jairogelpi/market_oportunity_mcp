# Modelo de datos Bronze/Silver/Gold — diseño

Fecha: 2026-11-16. Estado: implementado y probado de punta a punta con datos reales. Fase 2 de la hoja de ruta (después de la auditoría de licencias).

Cambios durante la implementación:
- `ratio` se retiró de los tipos de fórmula: ninguna métrica lo usa y no se implementó. Se añadirá con su primera métrica.
- Las métricas Gold usan solo datos Silver **del mismo study** (vía hit → run → study), para que el linaje sea el de esa investigación.
- Wikidata usa como entidad el dominio de su web oficial cuando lo tiene, así un producto y su página de precios archivada cuentan como una sola alternativa.
- La evidencia derivada guarda `derived_from_urls` (las URL de las observaciones de origen), que usa el bloque de atribución.
- Unidad de respaldo: si la fuente no informa la unidad, se usa la de la definición de la métrica.
- `check-catalog` valida también `catalog/metrics.json` (fórmulas, relación, métrica y preguntas del catálogo, `reviewed` booleano, ids `G…`).
- Prueba real: OECD (empresas TIC de 10 a 249 empleados) DE 16452 + FR 7701 = 24153 en 2024; Eurostat, adopción de IA en ES = 20.27 %. La evaluación quedó en INSUFFICIENT_EVIDENCE porque la suma procede de un solo grupo (OECD), que es lo correcto.

## Decisiones

| Tema | Decisión |
|---|---|
| Almacenamiento | SQLite para Silver y Gold; Bronze como ficheros comprimidos e inmutables con índice en SQLite |
| Métricas | Registro declarativo `catalog/metrics.json`, con tipos de fórmula fijos y motor determinista |
| Licencias | Bronze guarda la respuesta completa solo de fuentes `approved`; de las `restricted`, solo metadatos y fragmento corto |
| Cifras en conclusiones | Una métrica entra como evidencia derivada con extracto determinista; no crea independencia nueva |

## Bronze

- Ficheros `data/bronze/<source_id>/<AAAA-MM-DD>/<sha256>.json.gz` (direccionados por contenido, nunca sobrescritos; hash comprobado al leer). `data/bronze/` excluido de git.
- Tabla `bronze_objects(sha256, source_id, logged_url, retrieved_at, content_type, bytes, path, stored, reason, run_id, step_id)`. `stored=0` con motivo para fuentes no `approved`.
- Se escribe en el executor, solo para respuestas 2xx, con la licencia leída de `catalog/sources.csv` en ese momento.

## Silver

- `silver_observations(obs_id, source_id, dataset, measure, dims_json, geo, entity, period_label, period_start, period_end, unit, unit_mult, value_raw, value_scaled, status_flag, hit_id, bronze_sha256, extractor_version, extracted_at)`.
- `silver_documents(doc_id, source_id, url, title, published_at, language, excerpt, author_group, kind, entity, metadata_json, hit_id, bronze_sha256, deleted_at, extractor_version, extracted_at)`.
- Extractores deterministas y versionados por fuente que parten de los hits (metadatos estructurados de cada conector). Se ejecutan automáticamente tras cada paso y en una carga inicial (`build_silver`) para los hits anteriores. Identificador estable = hash de los campos que definen la fila (deduplicación por `INSERT OR IGNORE`).
- Normalización: geografía ISO 3166 alfa-2 (`EL`→`GR`, `UK`→`GB`, ISO alfa-3→alfa-2), agregados `EU27`, `WORLD`; periodos `AAAA`, `AAAA-Qn`, `AAAA-MM`, `AAAA-MM-DD` convertidos a inicio y fin (fin exclusivo) en ISO; `value_scaled = value_raw × 10^unit_mult`.
- Observaciones: Eurostat, OECD, World Bank, US Census (una por variable numérica), SEC EDGAR (entidad = CIK), series de tendencia (GDELT y modo `trend`) y Wikimedia Pageviews (un punto por mes). Documentos: el resto.

## Borrado en origen

- `recheck_deleted()` vuelve a consultar las publicaciones de Bluesky y Mastodon citadas como evidencia (Mastodon: `GET /api/v1/statuses/<id>`; Bluesky: `app.bsky.feed.getPosts` con sesión).
- Publicación ausente → `silver_documents.deleted_at`, evidencia `rejected` con motivo «borrada en origen» y evento en `intake_events`. Las conclusiones que la citaban quedan `stale`.
- Peticiones sin presupuesto de run, con las mismas comprobaciones de host, IP y límites.

## Gold

- `catalog/metrics.json`: `metric_id` propio (`G…`), `catalog_metric` y `relation` (`component` | `proxy`) con la métrica del catálogo, `question_ids`, `formula`, `inputs` (fuentes, tabla, medida, filtros de dimensiones), `unit`, `caveats`, `reviewed`.
- Tipos de fórmula: `latest`, `sum`, `ratio`, `growth` (variación relativa y, con periodos anuales, CAGR), `count_distinct`.
- El motor rechaza (no adivina): unidades o multiplicadores distintos, mezcla de fuentes en una suma, periodos distintos, geografías solapadas (`EU27` con un Estado miembro; `WORLD` con cualquier país) y series ambiguas.
- `gold_metrics(gold_id, metric_id, study_id, params_json, value, unit, formula_hash, input_ids, input_groups, computed_at, engine_version)`. Un cambio de definición cambia `formula_hash` y deja obsoletos los valores anteriores.
- Primeras métricas, todas con `reviewed: false`:
  - `G06_01_UNIVERSE` (componente de M06_01): suma de empresas.
  - `G06_01_ADOPTION` (proxy de la fracción elegible de M06_01): último % de adopción.
  - `G07_01_INTEREST_GROWTH` (proxy de M07_01): crecimiento de una proporción de atención.
  - `G04_01_ALTERNATIVES_FOUND` (componente de M04_01): alternativas distintas identificadas.

## Métricas como evidencia

- `promote_metric(gold_id, claim)` → evidencia `method=derived_metric`, `evidence_kind=estimate`, extracto generado por el motor con la cifra literal, `derived_from_groups` = grupos de independencia de las entradas.
- Evaluator: una evidencia derivada aporta los grupos de sus entradas, no uno nuevo. Las derivadas de métricas no revisadas no cumplen el requisito de evidencia, y una conclusión que las cite no puede pasar de `WEAK_SUPPORT`.

## Atribución

- `attribution(conclusion_id)`: citas exigidas por cada fuente usada (incluidas las de origen de las métricas derivadas), tomadas de `catalog/license_audit.csv`. Stack Exchange: enlace y aviso de añadir el autor antes de publicar. `list_conclusions` incluye el bloque.

## Fuera de alcance (fase 3)

Orquestación de extracciones repetibles desde Bronze, comparación de versiones y exportación a Parquet.
