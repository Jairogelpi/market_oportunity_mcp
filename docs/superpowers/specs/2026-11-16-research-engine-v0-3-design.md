# Research Engine v0.3 — diseño

Fecha: 2026-11-16. Estado: implementado (pasos 0–7 completados; 18 conectores verificados en vivo).

## Objetivo

Convertir el MCP de MarketScope (v0.2: registro de evidencias y runs) en un motor de investigación de tres piezas:

1. **Planner**: (study, pregunta del catálogo) → plan ejecutable y versionado.
2. **Executor**: ejecuta pasos de conector contra APIs abiertas permitidas, con cliente HTTP endurecido; registra resultados, descartes, bloqueos, errores y presupuesto.
3. **Evaluator**: métricas deterministas sobre las evidencias y veredicto máximo permitido; el agente redacta la conclusión y el MCP la valida.

Se mantiene `AGENTS.md`: nada se marca `verified` automáticamente; todo entra como `candidate`.

## Decisiones

| Tema | Decisión |
|---|---|
| Quién ejecuta | Híbrido: Planner/Evaluator deterministas en Python; conectores propios solo para APIs abiertas; búsqueda web abierta y navegador siguen en el agente (registrado vía MCP). |
| Conclusión | Reglas deterministas fijan `allowed_verdict`; el agente redacta; `submit_conclusion` valida. |
| Contexto de mercado | Entidad `study` (mercado, geografía, segmento, idioma); runs, evidencias y conclusiones pertenecen a un study. |
| Conectores | OpenAlex → Stack Exchange → GitHub → Eurostat + TED, uno a uno con tests. |
| Licencias | Con `license_review=pending`, las evidencias de conectores llevan `source_access_status=needs_review`; nunca `permitted`. |
| Estructura | Módulos separados: `studies.py`, `planner.py`, `http_client.py`, `connectors/`, `executor.py`, `evaluator.py`; `core.py` conserva validación y persistencia. |

## Modelo de datos

- `studies(study_id, name UNIQUE, market_definition, geography, segment, language, created_at)`.
  - `geography`: código ISO 3166-1 alfa-2 (`ES`, `US`…), `EU`, `GLOBAL` o `unknown`.
  - `language`: ISO 639-1 (`es`, `en`…) o `unknown`.
  - `segment`: texto libre o `unknown`.
- `research_runs.study_id` y `evidence.study_id` (NULL = registro legacy v0.2).
- `research_plans(plan_id, run_id, version, plan_json, created_at)` — paso 1.
- `source_hits(hit_id, run_id, step_id, source_id, url, title, published_at, snippet, content_sha256, retrieved_at, metadata_json)` — paso 2.
- `research_events` amplía `event_type`: `connector_call`, `connector_error`, `blocked`, `budget_exhausted`, `step_completed`, `replanned`; columnas `http_status`, `content_sha256` — paso 2.
- `conclusions(conclusion_id, study_id, question_id, metrics_json, metrics_hash, allowed_verdict, submitted_verdict, cited_evidence_ids, uncertainty_json, text, status, created_at)` — paso 6.
- `evidence_id` = sha256(question_id|url|claim) en legacy; con study: sha256(study_id|question_id|url|claim).
- Versión de esquema en `PRAGMA user_version` (v0.3 = 3). `init_db` migra de forma idempotente; `open_db` falla si la versión es antigua.

## Planner (paso 1)

1. Fuentes candidatas: `sources.csv` cuyo `dimension_ids` contiene la dimensión de la pregunta, filtradas por geografía del study (fuentes UE solo con `EU` o país UE; España solo con `ES`; EE. UU. solo con `US`; globales siempre).
2. Ruta por fuente: `connector` (hay conector implementado), `agent` (acceso público sin conector), `excluded` (comercial, acuerdo o acceso a verificar; con motivo).
3. Elementos no web del texto libre `candidate_sources_unverified` (entrevistas, encuestas, contabilidad propia…) → `gaps_out_of_scope`.
4. Consultas por plantilla: `market_definition` + `segment` + términos del `primary_indicator`.
5. Reparto del `query_budget` priorizando rutas `connector`.
6. `min_independent_groups` se extrae de `operational_definition` (patrón `>=N … independientes`); si no, 2.
7. Plan inmutable; replanificar crea versión nueva y evento `replanned`.

Formato `plan_json`: `plan_format, run_id, study_id, question_id, objective, population_defined, evidence_required, abstention_condition, independence_check, min_independent_groups, steps[{step_id, route, source_id, coverage(full|partial), priority_tier, risk_notes, query_terms{market, focus, language, segment?, geography?}, budget, status(pending|deferred)}], excluded_sources[{source_id, reason}], gaps_out_of_scope[], source_hints[], bias_notes`. `plan_version` viene de la fila en `research_plans`.

Notas de implementación (paso 1):
- `query_terms` en lugar de una consulta literal: cada conector traduce los términos a la sintaxis de su API y el agente los usa como pista; la consulta real queda registrada en el evento.
- Exclusión por acceso: `access_method` con `comercial|acuerdo|a verificar|con permisos|autorizado`, o `authentication` con `contrato|cuenta`, o fuente `rejected`.
- Geografía `unknown`: solo fuentes globales.
- `step_id` = `source_id` (un paso por fuente), para que los cierres sigan ligados a la misma fuente tras replanificar.
- Pasos sin presupuesto quedan `deferred`; `next_step` los omite. El estado de cada paso se deriva de eventos con `step_id` (`step_completed`, `blocked`, `failed`); el plan es inmutable.
- `source_hints`: pistas del texto libre que no son fuentes catalogadas ni fuentes no web (p. ej. «webs oficiales», «repositorios»).
- Esquema v4: tabla `research_plans`, columna `research_events.step_id`.

## Executor (pasos 2–5)

`execute_step(run_id, step_id)` ejecuta un único paso `connector`; sin bucle automático.

Cliente HTTP (`httpx`):
- Allowlist de hosts por conector (`api.openalex.org`, `api.stackexchange.com`, `api.github.com`, `ec.europa.eu`, `api.ted.europa.eu`).
- Solo HTTPS; redirecciones desactivadas (redirección = error); IP resuelta debe ser pública.
- Timeout 15 s; respuesta máxima 5 MB.
- Rate limit por fuente según documentación; cuotas diarias persistidas en SQLite; respeta `backoff` (Stack Exchange), `Retry-After` y `X-RateLimit-*` (GitHub).
- 401/403/429, HTML con CAPTCHA/login donde se esperaba JSON, o cuota agotada → evento `blocked`, paso `blocked`; sin reintento ni evasión.
- `User-Agent` identificable; contacto opcional vía `MARKETSCOPE_CONTACT`.
- robots.txt no aplica a APIs documentadas; obligatorio si se añade navegación con Playwright (fuera de alcance v0.3).
- Cada petición HTTP (incluida paginación) descuenta 1 del `query_budget`; agotado → `budget_exhausted`.
- `GITHUB_TOKEN` solo desde entorno; nunca en eventos, hits ni logs.

Resultados como `source_hits` (metadatos + fragmento corto, no la respuesta completa, por licencias pendientes). El agente promueve con `submit_evidence_from_hit(hit_id, claim, excerpt, ...)`:
- procedencia autocompletada; `method=official_api`; `source_access_status=needs_review`;
- `excerpt` debe aparecer literalmente en el `snippet` del hit;
- `evidence_kind` por fuente: Stack Exchange/GitHub `opinion`, OpenAlex `secondary`, Eurostat/TED `primary`;
- `source_independence_group` derivado (`stackexchange:<site>:<owner>`, `github:<repo>`, `openalex:<venue|autor>`…).

La ruta `agent` sigue con `submit_evidence` (`method=agent_research`), sin comprobación de fragmento.

Notas de implementación (paso 2):
- Esquema v5: tabla `source_hits`; columnas `http_status` y `content_sha256` en `research_events`. `connector_call` consume presupuesto igual que `search`.
- Comprobaciones previas (host, esquema, puerto, IP pública, cuota, backoff largo) fallan sin enviar petición ni gastar presupuesto. Esperas de rate limit > 30 s fallan en lugar de bloquear el MCP.
- `connector_error` cierra el paso para `next_step`; `execute_step(retry=true)` lo reabre explícitamente si queda presupuesto del paso.
- `execute_step(query=...)` permite al agente sustituir la consulta de plantilla (queda registrada).
- `submit_evidence_from_hit` solo admite en `extra`: `limitations, contradictions, geography, language, sample_frame, sampling_method`; la comparación del extracto ignora mayúsculas y espacios.
- OpenAlex: sin clave se permiten consultas básicas; paginar más allá de la primera página exige clave → una petición por paso (25 resultados). Clave opcional `MARKETSCOPE_OPENALEX_API_KEY` en cabecera `Authorization`. Cuota diaria conservadora: 100 sin clave, 1000 con clave. Hit: URL `https://openalex.org/W…`, snippet = título + abstract reconstruido (≤1500 caracteres), grupo de independencia por primer autor > revista > obra.
- Smoke test en vivo: `MARKETSCOPE_LIVE_TESTS=1 python -m unittest tests.test_live` (carga `.env` como el MCP).
- Secretos locales en `.env` (excluido en `.gitignore`), cargado por `start_mcp.py`; el entorno tiene prioridad.

Notas de implementación (paso 3):
- **Cambio de diseño:** HTTP 429 ya no bloquea; es `connector_error` reintentable con `retry=true` tras respetar `Retry-After` (motivo: OpenAlex devuelve 429 temporal por carga del clúster). 401/403 siguen bloqueando. Un 4xx con JSON que menciona `throttle`/`quota` (p. ej. `throttle_violation` de Stack Exchange) bloquea.
- Conectores declaran `OPTIONS` validadas por regex; `execute_step(options_json=...)`. OpenAlex no admite opciones.
- `fetch.defer(segundos)` permite al conector aplicar el `backoff` del servidor.
- Stack Exchange: `/2.3/search/advanced`, `filter=withbody`, 30 resultados, sitio por defecto `stackoverflow`; opciones `site` y `tagged`. Sin clave 300/día por IP (cuota interna 250); clave opcional `MARKETSCOPE_STACKEXCHANGE_KEY` (parámetro `key`, redactado en logs; cuota interna 9000). `quota_remaining=0` sin resultados bloquea. No se guarda el nombre del autor, solo `user_id` para el grupo de independencia (`stackexchange:<site>:user:<id>`).

Notas de implementación (paso 4):
- GitHub: `/search/issues`, 30 resultados, siempre `is:issue` (los PR se descartan). Opciones `repo` (→ `repo:`), `state` (→ `state:`), `sort` (`comments|reactions|created|updated`). `sort` ordena por popularidad, no por relevancia: para investigar, preferir el orden por defecto y frases entre comillas.
- Token opcional `MARKETSCOPE_GITHUB_TOKEN` (fine-grained, solo lectura de repos públicos) en cabecera `Authorization`; intervalo mínimo 2 s con token y 6 s sin él (búsqueda ~30/min y ~10/min). `x-ratelimit-remaining=0` aplaza la fuente hasta `x-ratelimit-reset`.
- **Cambio en el cliente:** un 403 cuyo JSON menciona `rate limit` es límite temporal (reintentable, respeta `Retry-After`); el resto de 401/403 bloquea.
- **Cambio respecto al diseño (paso 4):** grupo de independencia por autor (`github:user:<id>`), no por repositorio: issues de distintos usuarios en el mismo repo son testimonios distintos; el repo queda en `metadata.repo`. No se guarda el login.

Notas de implementación (paso 5):
- Cliente: `get_json(..., json_body=...)` hace POST (TED). Errores JSON de Eurostat (`error[].label`) y TED (`error.type`) se incluyen en el motivo.
- Conectores: `validate_query()` (antes de cualquier petición) y `QUERY_HELP` (mensaje cuando falta consulta); el executor pasa `query_terms` del paso como `connector.terms`.
- Eurostat (`/eurostat/api/dissemination/statistics/1.0/data/{dataset}`, JSON-stat 2.0): la consulta es el **código de dataset** (no hay búsqueda libre; el agente lo localiza en el Data Browser). Opción `filters` = `dim=v1,v2;dim2=v` (incluye `lastTimePeriod`, `sinceTimePeriod`, `untilTimePeriod`, `geoLevel`). Sin filtro `geo` se usa la geografía del study (`GR→EL`, `GB→UK`, `EU→EU27_2020`; `GLOBAL` sin filtro). Cada observación = un hit (máx. 60, `truncated` si hay más) con URL que reproduce exactamente la observación y snippet con etiquetas, códigos y `valor: N [estado]`. Grupo de independencia único `eurostat` (un solo linaje estadístico). Selección vacía → paso completado con 0 hits.
- TED (`POST https://api.ted.europa.eu/v3/notices/search`, sin autenticación): consulta libre → `FT ~ (...)`; consulta experta (`~`, `=`, `IN (`, `SORT BY`) se respeta. Cláusulas: país comprador (ISO alfa-3; por defecto desde la geografía del study), `cpv` (un código de 8 dígitos), `since` (AAAAMMDD), `lang` (ISO 639-2 del título; por defecto `eng`). Orden por defecto `SORT BY publication-date DESC`; 25 anuncios. El evento registra la consulta experta exacta. Grupo de independencia por comprador (`ted:buyer:<nombre>`, organización pública). Metadato `caveat`: gasto licitado ≠ adjudicado. `timedOut` → fallo.
- Solo se planifican para studies UE o de países UE (filtro geográfico del Planner).

Correcciones tras la revisión de punta a punta (2026-11-16):
- **Consultas de plantilla:** sin palabras repetidas. Fuentes mayoritariamente en inglés (`ENGLISH_FIRST`: OpenAlex, Stack Exchange, GitHub) usan `studies.keywords_en`; si el study no está en inglés y no hay `keywords_en`, no se envía nada y se pide consulta explícita (sin gastar presupuesto). `keywords_en` es el único campo mutable del study (`set_study_keywords`); el executor lee el valor vigente al ejecutar. Esquema v6.
- **Pasos vacíos:** 0 resultados → evento `step_empty` (no terminal), estado `empty`; el paso sigue abierto para otra consulta mientras quede presupuesto del paso; `next_step` lo anota.
- **TED:** las palabras de una consulta libre se unen con `AND` explícito (verificado en vivo: separar por espacios no es ni AND ni OR).
- **`list_hits`:** compacto y paginado (`offset`, `limit` ≤ 50, `step_id`), con vista previa de 200 caracteres, metadatos clave, `independent_groups` y `largest_groups`; `get_hit` devuelve el hit completo.
- **Cobertura:** `global_unfiltered` para fuentes globales en studies no globales (no se restringen a la geografía); `coverage_notes` en el plan.
- **Población (cont.):** ver más abajo.

Ampliación de conectores (foco mundial, 2026-11-16): SEC EDGAR y World Bank. BOE e INE (solo España) quedan pospuestos.
- Cliente: `Policy.timeout` por fuente (World Bank 90 s: latencia medida en vivo de 0,5 a 55 s), `Policy.send_contact` (el email de `MARKETSCOPE_CONTACT` solo se añade al User-Agent de SEC; el valor se lee en cada petición) y `NotFound` (404) para que el conector pruebe alternativas.
- SEC EDGAR: consulta = ticker (resuelto con `company_tickers.json`, una petición) o CIK. `companyconcept` XBRL; por defecto `Revenues` y, si 404, `RevenueFromContractWithCustomerExcludingAssessedTax`; opciones `concept`, `taxonomy` (`us-gaap|ifrs-full|dei|srt`). Un valor por año: solo 10-K/10-K/A/20-F/40-F con `frame` anual `CYaaaa` (descarta reexpresiones y trimestres); últimos 10 años. Importes en dígitos sin separadores. Grupo `sec:cik:<cik>`; metadatos con `accn` y `filing_url`. Exige `MARKETSCOPE_CONTACT`. Planner: fuente de datos de empresa (`COMPANY_DATA_SOURCES`) planificada en cualquier geografía como `partial` con `scope_note` (competidores, no mercado local).
- World Bank: consulta = código de indicador; países por defecto desde el study (`GLOBAL→WLD`, `EU→EUU`); opciones `countries`, `date`, `mrv`. Errores llegan como HTTP 200 con `message` → fallo. Observaciones con valor = hits (máx. 60). Evidencia `secondary` (compila estadísticas nacionales), grupo único `worldbank`.

Segunda ampliación (huecos DIM04, DIM08, DIM10): EUR-Lex, Federal Register, Wikimedia Pageviews y Wikidata.
- Catálogo: añadidas `wikimedia_pageviews`, `wikidata` y `federal_register` (`candidate`, `license_review: pending`); 43 fuentes.
- Cliente: `Policy.contact_env` sustituye a `send_contact`: cada fuente declara qué variable de contacto exige su política y solo a ella se envía (SEC: `MARKETSCOPE_CONTACT`; Wikimedia/Wikidata: `MARKETSCOPE_WIKIMEDIA_CONTACT`, consentimiento separado). User-Agent `MarketScopeResearch/0.3 (<contacto>; research prototype; …)` (formato Wikimedia; SEC lo acepta, verificado en vivo).
- Federal Register: `/api/v1/documents.json`, `order=relevance` (con `newest` los primeros resultados no tienen relación, verificado), 20 documentos; opciones `type`, `since`, `agency`; `ENGLISH_FIRST`; grupo por agencia.
- EUR-Lex: endpoint SPARQL abierto de Cellar (el servicio SOAP de EUR-Lex exige registro). Plantilla fija; 1-6 palabras validadas que deben aparecer en el título; idioma del título desde el study (`lang` EU de 3 letras); opciones `sector` (dígito CELEX) y `since`. Sin plantilla automática (palabras vacías nunca coinciden). ~45-60 s por consulta (timeout 90 s). Grupo único `eurlex`.
- Wikimedia Pageviews: 1-5 artículos separados por `|` (una petición cada uno), proyecto desde el idioma del study, últimos 24 meses completos por defecto; un hit por artículo con la serie literal. 404 → artículo ausente (si todos faltan, fallo). Evidencia `secondary`; interés ≠ demanda.
- Wikidata: SPARQL fijo; búsqueda por nombre (EntitySearch) o `class` (QID, instancias directas) con filtro opcional por palabras; texto validado (sin comillas ni llaves). Evidencia `secondary`, grupo único `wikidata`.
- **Bloqueo verificado en vivo:** Wikimedia y Wikidata devuelven 403 sin contacto en el User-Agent (política de robots). Sin `MARKETSCOPE_WIKIMEDIA_CONTACT` los conectores se niegan antes de enviar nada; sus smoke tests se omiten. El usuario autorizó usar el mismo email que para SEC; ambos verificados en vivo.
- **Corrección de datos (Wikimedia):** la fecha final de la API es inclusiva; terminar el día 1 del mes en curso añadía un «mes» de un solo día (128 visitas frente a ~13.000). La ventana por defecto termina el último día del mes anterior: 24 meses completos.

Tercera ampliación (precios, compradores potenciales): Internet Archive, OECD y US Census.
- Catálogo: añadidas `internet_archive` (P0) y `us_census`; `oecd` gana DIM02 (uso de TIC por empresas); 45 fuentes.
- Cliente: `get_json(..., expect='text')` para páginas archivadas (sin heurística de login/CAPTCHA, que daría falsos positivos en páginas de precios). Conectores: `REQUIRED_OPTIONS` validadas antes de cualquier petición.
- Internet Archive: consulta = URL sin esquema (validada; rechaza hosts locales o privados). CDX con una captura por año (`collapse=timestamp:4`, solo 200/HTML) y hasta `snapshots` (1-6, por defecto 3: primera, intermedia, última) descargadas en crudo (`id_`). Se eliminan script/style; se extraen patrones de precio con contexto (máx. 20). Una captura fallida no hace fallar el paso (`failed_captures`). Grupo `web:<host>` (afirmación del propio competidor), evidencia `primary`, con la advertencia de que el precio de lista no es el precio pagado.
- OECD: consulta = `"<AGENCIA,DATAFLOW,VERSIÓN>/<clave>"` (la clave es obligatoria, porque un dataset completo supera 5 MB). 404 `NoResultsFound` = selección vacía. `UNIT_MULT` siempre explícito (`valor: N × 10^m`). **Sin `lastNObservations`**: el endpoint responde 413 (verificado). Evidencia `secondary`, grupo `oecd`.
- US Census: **la clave gratuita es obligatoria** (verificado: sin clave redirige a `missing_key.html`); `MARKETSCOPE_CENSUS_KEY`, redactada en los registros. Consulta = dataset (`2022/cbp`); `get` es obligatorio, con `for`, `in` y `filters` opcionales. Las etiquetas de las variables se piden en una petición pequeña por variable. Evidencia `primary`, grupo `us_census`.

Cuarta ampliación (testimonios sociales, sustituto legítimo de Reddit): Hacker News, Mastodon y Bluesky.
- Reddit: crear una app exige aprobación previa (Responsible Builder Policy); sin aprobación no se conecta ni se usan scrapers (regla 6).
- Catálogo: `hackernews` corregido (búsqueda Algolia; la API Firebase no busca); añadidos `bluesky` y `mastodon`; 47 fuentes.
- Hacker News (`hn.algolia.com/api/v1/search` o `search_by_date`): sin clave; opciones `tags`, `since`, `sort`; autor convertido en hash dentro del grupo de independencia (no se guarda el nombre).
- Mastodon: la búsqueda de texto sin sesión no devuelve nada (verificado); se usan timelines públicos por hashtag (1-3 por consulta) en una lista fija de instancias grandes (`instance`). Solo publicaciones públicas originales; grupo por id numérico de cuenta local. La URL de la publicación (necesaria para citar) contiene el alias; no se guarda en otro sitio.
- Bluesky: la búsqueda anónima da 403 (verificado); sesión oficial con `MARKETSCOPE_BLUESKY_HANDLE` y `MARKETSCOPE_BLUESKY_APP_PASSWORD` (contraseña de aplicación). Contraseña y token solo en cuerpo/cabecera, nunca registrados. URLs con DID, no con alias. Sin credenciales se niega antes de enviar nada.
- Planner: `SELF_SELECTED_SOCIAL` (Bluesky, Mastodon) siempre lleva aviso de muestra autoseleccionada, sea cual sea el segmento.
- Tests: los tests de flujo usan `Q08.01` (tiene pasos agent) y ya no dependen del número de fuentes del catálogo.

Señales de tendencia (Google descartado por el usuario): GDELT y modo tendencia.
- `period_ranges`: solo periodos **completos** (año, trimestre o mes; máx. 12); el periodo en curso se excluye. `trend_hit`: un hit con la serie literal; **normalizado por defecto** (recuento ÷ total de la plataforma en cada periodo), porque el recuento bruto crece o cae con la propia plataforma. Verificado en vivo: las preguntas sobre «invoice» en Stack Overflow caen un 88 % (2022-2025) mientras su proporción sube del 0,163 % al 0,233 %. Proporciones con tres cifras significativas: con tres decimales fijos, todos los trimestres de GDELT salían 0,001 %.
- Modo tendencia (`options.mode=trend`, `granularity`, `periods`, `normalize`): Hacker News (`nbHits` con `hitsPerPage=0`; 1-2 peticiones por periodo; recuentos aproximados por tolerancia a erratas), Stack Exchange (`filter=total` en `/search/advanced` y `/questions`; 1-2 por periodo, sujeto a cuota) y OpenAlex (`group_by=publication_year`; 1-2 peticiones; solo anual).
- GDELT DOC 2.0 (`mode=TimelineVolRaw`): una petición con `startdatetime`/`enddatetime`, puntos sumados en periodos completos; los periodos sin datos se listan en `missing_periods` (nunca se cuentan como cero). Opciones `lang` (sourcelang) y `country` (sourcecountry). Intervalo mínimo de 6 s: GDELT respondió 429 de forma persistente durante un rato (bloqueo temporal de la conexión) y después funcionó. El smoke test se omite, no se da por superado, ante un 429. Catálogo: 48 fuentes.

- **Población:** `population_warning` por paso y `population_warnings` en el plan cuando una fuente de testimonios técnica (GitHub, Stack Exchange, Hacker News, Reddit, Product Hunt, Indie Hackers) no coincide con el segmento del study. El Evaluator deberá usarlo.

## Evaluator (paso 6)

`evaluate_question(study_id, question_id)`, solo lectura. Métricas: `n_evidence` (por método y tipo), `independent_groups` (total y con fragmento comprobado), `primary_or_secondary`, `declared_contradictions`, `population_defined`, `gaps`, `age_range` (informativa).

Escalera (primera regla que aplica):
1. `INSUFFICIENT_EVIDENCE`: población no definida; o grupos < mínimo; o pregunta cuantitativa (DIM02, DIM05, DIM06, DIM11) con solo opinión.
2. `DEFER`: contradicción declarada sin resolver.
3. `WEAK_SUPPORT`: grupos mínimos, pero sin `primary_or_verified_secondary`.
4. `MODERATE_SUPPORT`: grupos mínimos, ≥1 grupo con fragmento comprobado y `evidence_required` cumplido.

`SUPPORTED` requiere evidencias `verified` (revisión humana) y es inalcanzable en v0.3. Salida: métricas, `allowed_verdict`, razones, `metrics_hash`.

`submit_conclusion` rechaza si: veredicto > permitido (recalculado); citas inexistentes o de otro study/pregunta; citas con menos grupos que el mínimo en veredictos de apoyo; falta `uncertainty{coverage_limits, biases, missing_evidence, contradictions}` o `missing_evidence` no incluye los `gaps`; el texto contiene cifras ausentes de los extractos citados (exentos años de 4 dígitos e IDs `Qnn.nn`). Abstenciones deben explicar qué falta. Se guarda `candidate`; si cambia `metrics_hash` la conclusión se lista como `stale`.

Notas de implementación (paso 6):
- Esquema v7: tabla `conclusions`. Herramientas `evaluate_question`, `submit_conclusion`, `list_conclusions`; CLI `evaluate` y `conclusions`.
- Métricas sobre evidencias del study y la pregunta (no `rejected`): por método y tipo, grupos independientes (`source_independence_group`, o el host de la URL si falta), grupos con extracto comprobado (`hit_id`), `evidence_requirement_met` (primaria con extracto comprobado, o primaria o secundaria `verified`; una secundaria `candidate` no basta), grupos verificados, contradicciones declaradas, población definida, `only_population_warned_sources`, carencias del plan, fuentes bloqueadas y rango de fechas.
- Escalera aplicada: INSUFFICIENT_EVIDENCE (población, grupos, cuantitativa solo con opinión) → DEFER (contradicciones) → SUPPORTED (solo si los grupos verificados alcanzan el mínimo, es decir, tras revisión humana) → WEAK_SUPPORT (requisito no cumplido, sin extracto comprobado, o **todo el apoyo de fuentes con aviso de población**) → MODERATE_SUPPORT.
- Rangos: las abstenciones valen 0, y una conclusión siempre puede ser más prudente que lo permitido; los veredictos de apoyo siguen necesitando citas que cubran el mínimo de grupos.
- Cifras: se extraen los números del texto (salvo años de 4 dígitos, IDs `Qnn.nn` e IDs de evidencia) y deben aparecer literalmente en los extractos citados. Se aceptan variantes con separador de miles (`17,438` = `17438`), pero no coincidencias solo por dígitos (`1.5` ≠ `15`). Las cifras derivadas (crecimientos, sumas) se rechazan a propósito. Se admiten los recuentos de las propias métricas (número de citas o de grupos).
- Prueba real de punta a punta (Q04.01, FreshBooks): sin evidencias → INSUFFICIENT; 2 grupos (Internet Archive primaria + Wikidata) → MODERATE; SUPPORTED rechazado; cifra inventada rechazada. Corrección derivada: Internet Archive une cifras partidas por el HTML (`$ 8 .40` → `$8.40`) y reconoce el código de moneda tras el número.
- **Limitaciones conocidas:** (1) el veredicto valora las afirmaciones citadas, no si la pregunta queda respondida por completo (dos fuentes sobre FreshBooks no son un mapa de competidores); (2) la independencia se mide por grupo de fuente y no por linaje: la ficha de Wikidata puede proceder de la web del propio competidor. Ambas quedan para la revisión humana.

## Herramientas MCP nuevas

`create_study`, `get_study`, `list_studies`, `begin_research(+study_id)`, `plan_research`, `replan_research`, `next_step`, `execute_step`, `complete_agent_step`, `list_hits`, `submit_evidence_from_hit`, `evaluate_question`, `submit_conclusion`, `list_conclusions`. La CLI expone equivalentes salvo `execute_step`.

## Errores

`EvidenceError` en español, sin cambio de estado. Red/conector → `connector_error`, paso `failed`, reintento solo explícito. Bloqueos → `blocked`, definitivo para el run.

## Tests

`unittest`. Unitarios por regla (planner, escalera, `submit_conclusion`, fragmentos, migración). Conectores con `httpx.MockTransport` y fixtures JSON (allowlist, redirecciones, tamaño, 403/429, CAPTCHA, backoff, presupuesto, no fuga de token). Smoke en vivo opcional con `MARKETSCOPE_LIVE_TESTS=1`, sin modificar `live_test` del catálogo. Test de arranque MCP ampliado al flujo completo.

## Orden de implementación

0. Migración de esquema + entidad `study`.
1. Planner.
2. Cliente HTTP endurecido + OpenAlex.
3. Stack Exchange.
4. GitHub.
5. Eurostat + TED.
6. Evaluator + conclusiones.
7. Actualizar `AGENTS.md`, `README.md`, `docs/architecture.md`.
