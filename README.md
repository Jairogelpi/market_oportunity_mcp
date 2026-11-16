# MarketScope Research Engine — v0.3

Motor de investigación de mercado con evidencia trazable, expuesto como servidor MCP para Claude Code o Codex:

- **Planner**: convierte una pregunta del catálogo (Nivel 2) y un study (mercado, geografía, segmento) en un plan determinista.
- **Executor**: consulta 18 fuentes oficiales o abiertas con un cliente HTTP endurecido y audita cada petición.
- **Evaluator**: mide la evidencia, decide el veredicto máximo permitido (o la abstención) y valida las conclusiones.

- **Capas de datos**: Bronze (respuestas brutas inmutables), Silver (observaciones y documentos normalizados) y Gold (métricas declarativas con linaje).

Todo entra como `candidate`; la verificación es humana. Las instrucciones para el agente están en `AGENTS.md` y la arquitectura en `docs/architecture.md`.

## Requisitos e instalación

Python >= 3.10.

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt   # mcp, httpx
.venv\Scripts\python -m marketscope.cli check-catalog
```

## Configuración (`.env`, excluido de git)

`start_mcp.py` carga `.env` al arrancar; las variables de entorno ya definidas tienen prioridad. Ningún secreto se guarda en la base de datos ni en los registros.

| Variable | Uso | ¿Obligatoria? |
|---|---|---|
| `MARKETSCOPE_DB` | ruta de SQLite (por defecto `data/marketscope.sqlite`) | no |
| `MARKETSCOPE_OPENALEX_API_KEY` | OpenAlex: más cuota | no |
| `MARKETSCOPE_STACKEXCHANGE_KEY` | Stack Exchange: 10.000 peticiones al día en lugar de 300 | no |
| `MARKETSCOPE_GITHUB_TOKEN` | GitHub: token fine-grained de solo lectura de repos públicos | no |
| `MARKETSCOPE_CONTACT` | email de contacto en el User-Agent, **solo para SEC** | sí, para SEC EDGAR |
| `MARKETSCOPE_WIKIMEDIA_CONTACT` | contacto exigido por Wikimedia, **solo para Wikimedia y Wikidata** | sí, para esas dos |
| `MARKETSCOPE_CENSUS_KEY` | clave gratuita del US Census | sí, para US Census |
| `MARKETSCOPE_BLUESKY_HANDLE`, `MARKETSCOPE_BLUESKY_APP_PASSWORD` | sesión de Bluesky con contraseña de aplicación | sí, para Bluesky |

Sin la variable necesaria, el conector se niega **antes** de enviar ninguna petición y explica qué falta.

## Fuentes conectadas

| Tipo | Fuentes |
|---|---|
| Testimonios y problemas | GitHub, Stack Exchange, Hacker News, Mastodon, Bluesky |
| Competencia y precios | Internet Archive (páginas de precios archivadas), Wikidata, SEC EDGAR |
| Tamaño de mercado | Eurostat, OECD, US Census, World Bank |
| Demanda e interés | Wikimedia Pageviews, TED |
| Regulación | EUR-Lex, Federal Register |
| Tendencias | GDELT, OpenAlex; modo tendencia en Hacker News, Stack Exchange y OpenAlex |

Sin conector, por falta de vía autorizada: Reddit (exige aprobación previa), Google (Keyword Planner requiere credenciales OAuth de Google Ads), plataformas de reseñas y bases comerciales.

## Conectar a Claude Code o Codex

`.mcp.json` apunta al intérprete de `.venv` y a `start_mcp.py` con rutas absolutas de esta máquina; si se mueve el proyecto, hay que regenerarlas. Tras cambiar el código, reconecta el servidor (`/mcp` → reconnect en Claude Code). El arranque migra la base automáticamente.

## CLI

```bash
python -m marketscope.cli init | check-catalog | summary | export --file out.json
python -m marketscope.cli ingest --file examples/evidence.json      # ejemplo sintético, no evidencia real
python -m marketscope.cli create-study --name X --market "..." --geography GLOBAL --segment "..." --language en [--keywords-en "..."]
python -m marketscope.cli list-studies
python -m marketscope.cli plan --run <run_id>
python -m marketscope.cli evaluate --study <study_id> --question Q01.01
python -m marketscope.cli conclusions --study <study_id>
python -m marketscope.cli build-silver          # reconstruye Silver desde los hits guardados (idempotente)
python -m marketscope.cli metrics               # definiciones Gold (catalog/metrics.json)
python -m marketscope.cli recheck-deleted       # retira evidencias de Bluesky/Mastodon borradas en origen
```

Bronze se guarda en `data/bronze/` (excluido de git) junto a la base de datos; haz copia de seguridad de ambos.

La ejecución de pasos solo está en el MCP, para que siempre quede auditada.

## Tests

```bash
.venv\Scripts\python -m unittest discover -s tests -v                                  # sin red
set MARKETSCOPE_LIVE_TESTS=1 && .venv\Scripts\python -m unittest tests.test_live -v      # una petición real por fuente
```

Los tests en vivo nunca modifican la base del proyecto ni el campo `live_test` del catálogo (esa revisión es humana). Se omiten, sin darse por superados, cuando faltan credenciales o el proveedor limita la conexión.

## Estado

- Probado: los 18 conectores en vivo, el flujo de punta a punta (plan → ejecución → evidencia → evaluación → conclusión) y el arranque real del MCP.
- Licencias auditadas y decididas para uso no comercial (`docs/license_audit.md`): 12 `approved`, 6 `restricted`.
- Capas Bronze/Silver/Gold probadas de punta a punta con datos reales (OECD y Eurostat).
- Pendiente: flujo de revisión humana (`verified` y revisión de métricas), ETL reproducible desde Bronze (fase 3), detección de linaje común entre fuentes.
- Datos de prueba: la base contiene un study etiquetado «PRUEBA herramientas v0.3 (no usar)».
