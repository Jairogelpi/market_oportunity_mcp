# Auditoría de licencias, acceso y costes — fuentes conectadas

Fecha de revisión: 2026-11-16. Alcance: las 18 fuentes con conector. Datos estructurados en `catalog/license_audit.csv`.

**Decisión:** el responsable del proyecto confirmó el 2026-11-16 las clasificaciones de esta auditoría para **uso no comercial**: 12 `approved` y 6 `restricted`. Están aplicadas en `license_review` de `catalog/sources.csv`. Las 30 fuentes sin conector siguen `pending`. Si el uso pasa a ser comercial, hay que repetir la revisión. No es asesoramiento jurídico.

En una segunda pasada se verificaron, con los términos oficiales, Mastodon (términos vigentes de mastodon.social, en vigor desde el 31-8-2026, leídos de la API de la instancia), Bluesky (pautas para desarrolladores, del repositorio oficial de su documentación) y Wikimedia (política de acceso de la Analytics API). Siguen sin leerse directamente Stack Exchange, OECD, EUR-Lex e Internet Archive. No se usaron copias archivadas para leer páginas que devolvían 403, porque sería rodear un bloqueo (regla 6).

## Resumen

| Propuesta | Fuentes | Condición |
|---|---|---|
| **Aprobable** (12) | Eurostat, TED, SEC EDGAR, World Bank, OECD, US Census, OpenAlex, Wikidata, Wikimedia Pageviews, GDELT, EUR-Lex, Federal Register | Datos abiertos o públicos, también para uso comercial. Casi todas exigen **citar la fuente**. |
| **Restringida** (6) | GitHub, Stack Exchange, Hacker News, Internet Archive, Mastodon, Bluesky | Contenido de terceros o de personas: uso limitado a investigación o a fines no comerciales, con obligaciones de privacidad o atribución. |

**Coste:** 0 en las 18. Todas son gratuitas; los límites son de cuota o de velocidad, no de pago.

**Confianza de la revisión:** 11 fuentes con los términos leídos directamente (`verified_from_terms`), 6 con fuentes secundarias porque la página oficial no se dejó leer (`secondary_source`), y 1 sin verificar (`unverified`): Stack Exchange, cuyo dominio bloquea la lectura automática. **Prioridad de revisión humana: las 7 de confianza menor.**

## Fuentes aprobables

| Fuente | Licencia | Atribución obligatoria | Matiz |
|---|---|---|---|
| Eurostat | Reutilización autorizada citando la fuente (Decisión 2011/833/UE) | «Source: [DOI del dataset], [fecha de acceso]» | Los datos de fuentes ajenas a Eurostat o de países fuera de la UE, la EFTA y los países candidatos «may not be reused for commercial purposes» |
| TED | Anuncios de reutilización libre; contenido editorial CC BY 4.0 | crédito en el contenido editorial | Los anuncios incluyen datos de contacto personales |
| SEC EDGAR | «considered public information and may be copied or further distributed» | recomendada | Máximo 10 peticiones/s; bloquean IPs que lo excedan |
| World Bank | CC BY 4.0, licencia por defecto de sus datasets | crédito e indicar cambios | Algunos indicadores tienen otra licencia: comprobar los metadatos |
| OECD | CC BY 4.0 desde el 1-7-2024 | crédito | *Fuente secundaria*: la página de términos devolvió 403 |
| US Census | Datos públicos, con Términos de servicio de la API | aviso: «This product uses the Census Bureau Data API but is not endorsed or certified by the Census Bureau.» | Prohibido alterar los datos o insinuar aval del Census |
| OpenAlex | CC0 | no | — |
| Wikidata | CC0 | no | Exige contacto en el User-Agent |
| Wikimedia Pageviews | CC0 | no | *Fuente secundaria*; exige contacto en el User-Agent |
| GDELT | «unlimited and unrestricted use» | cita a «the GDELT Project» con enlace | — |
| EUR-Lex | Decisión 2011/833/UE | crédito | *Fuente secundaria*; solo el Diario Oficial es auténtico |
| Federal Register | Dominio público (obra federal) | no; no usar logotipos | *Fuente secundaria*: el dominio público se deduce de la ley |

## Fuentes restringidas

| Fuente | Qué dicen sus términos | Qué implica para MarketScope |
|---|---|---|
| **GitHub** | «Researchers may use public, non-personal information… only if any publications resulting from that research are open access» | **Solo investigación, y las publicaciones derivadas deben ser de acceso abierto.** No extraer datos personales. |
| **Stack Exchange** | *Sin verificar.* El contenido es CC BY-SA, que exige **nombrar al autor** y compartir derivados con la misma licencia; según fuentes secundarias, prohíbe usarlo para entrenar IA sin permiso escrito | **Conflicto con el diseño actual:** el sistema no guarda nombres de autor, pero CC BY-SA los exige al citar. Hay que añadir la atribución al publicar. |
| **Hacker News** | Y Combinator prohíbe «data mining, robots, scraping or similar data gathering» salvo autorización expresa, y el uso comercial del contenido | La búsqueda de Algolia es la que enlaza HN, pero **no consta una autorización expresa**. Conviene pedir aclaración antes de usar sus resultados en informes. |
| **Internet Archive** | El uso debe ser «noncommercial» y limitado a «noninfringing or fair use» (según el blog del propio IA) | **Solo uso no comercial**, con extractos breves a modo de cita. Las páginas de precios son obras de los competidores. |
| **Mastodon** | Términos por instancia; mastodon.social prohibió en 2025 usar su contenido para entrenar modelos; un estudio de 2020 se retiró por críticas de privacidad y RGPD | Hace falta una **revisión de privacidad y ética** antes de usarlo en informes. |
| **Bluesky** | Bluesky notifica los borrados de cuentas a las apps del protocolo; las pautas para desarrolladores no se pudieron leer | **Respetar los borrados**; datos personales sujetos al RGPD. |

## Obligaciones que se derivan para el sistema

1. **Atribución al publicar.** Los informes y conclusiones deben incluir la cita que exige cada fuente: DOI y fecha en Eurostat, cita a GDELT con enlace, aviso de no-aval del Census, crédito CC BY 4.0, y autor y enlace en Stack Exchange. *Hoy el sistema guarda la URL, pero no genera el bloque de atribución.*
2. **Uso no comercial** de Internet Archive y Hacker News, y **solo investigación con publicación en abierto** para GitHub. Si MarketScope pasa a uso comercial, estas fuentes quedan fuera.
3. **Respetar los borrados** en Bluesky y Mastodon, y el futuro Reddit: comprobar periódicamente que las publicaciones citadas siguen existiendo.
4. **No usar los datos para entrenar modelos** (Stack Exchange, Mastodon).
5. **Distinguir datos de terceros** dentro de Eurostat, World Bank y OECD, que pueden tener otra licencia.

## Pendiente

- **Implementar** el borrado de contenido eliminado en origen (Bluesky, Mastodon) y la atribución al publicar (con autor en Stack Exchange). Hasta entonces, esas fuentes se usan con cautela.
- **Hacker News:** pedir aclaración a Y Combinator o a Algolia sobre el uso de la API de búsqueda; mientras tanto, no usar sus resultados en informes.
- Opcional, para subir la confianza: leer en el navegador los términos de Stack Exchange, OECD, EUR-Lex e Internet Archive.
