"""Optional MCP adapter. Install: pip install -r requirements.txt"""
import json
import sys
from .core import (export, summary, load_questions, load_sources, start_run, log_research_event, run_report,
                   ingest, init_db, open_db, resolve_db_path, check_catalog)
from . import studies, planner, executor, evaluator, silver, gold, maintenance, attribution

def main():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as ex:
        raise SystemExit(f"MCP no instalado en {sys.executable}: pip install -r requirements.txt") from ex
    if errors := check_catalog():
        raise SystemExit('Catálogo inválido:\n'+'\n'.join(errors))
    # Schema creation happens once, here; tools never create the DB.
    dbpath=init_db(resolve_db_path())
    print(f'marketscope: python={sys.executable} db={dbpath}',file=sys.stderr)
    server=FastMCP('marketscope-research')
    @server.tool()
    def server_info() -> str:
        """Return interpreter, database path and catalog sizes for diagnostics."""
        return json.dumps({'python':sys.executable,'db':str(dbpath),
                           'questions':len(load_questions()),'sources':len(load_sources())})
    @server.tool()
    def submit_evidence(record_json: str) -> str:
        """Submit one research observation in JSON; validates provenance and saves as candidate."""
        record=json.loads(record_json)
        # An agent cannot self-certify verification.
        record['status']='candidate'
        with open_db(dbpath,write=True) as db:
            evidence_id,state,_=ingest(db,record)
        return json.dumps({'evidence_id':evidence_id,'result':state})
    @server.tool()
    def evidence_summary() -> str:
        """Summarize the number and review status of stored evidence records."""
        with open_db(dbpath) as db: return json.dumps(summary(db))
    @server.tool()
    def list_evidence(question_id: str = '') -> str:
        """List stored evidence, optionally filtered by MarketScope question ID."""
        with open_db(dbpath) as db: data=export(db)
        if question_id: data=[e for e in data if e['question_id']==question_id]
        return json.dumps(data[:100],ensure_ascii=False)
    @server.tool()
    def catalog_questions() -> str:
        """Return canonical Level 2 questions, including identifiers and expected evidence."""
        return json.dumps(list(load_questions().values()),ensure_ascii=False)
    @server.tool()
    def catalog_sources() -> str:
        """Return source inventory with access and license review status; not proof of authorization."""
        return json.dumps(list(load_sources().values()),ensure_ascii=False)
    @server.tool()
    def create_study(name: str, market_definition: str, geography: str, segment: str, language: str, keywords_en: str = '') -> str:
        """Create a study (market context). geography: ISO alpha-2, EU, GLOBAL or unknown; language: ISO 639-1 or unknown; segment may be 'unknown'. keywords_en: English search keywords for mostly-English sources (GitHub, Stack Exchange, OpenAlex); without them those sources need an explicit query when language is not 'en'."""
        with open_db(dbpath,write=True) as db:
            return json.dumps(studies.create_study(db,name,market_definition,geography,segment,language,keywords_en),ensure_ascii=False)
    @server.tool()
    def set_study_keywords(study_id: str, keywords_en: str) -> str:
        """Set the English search keywords of a study (the only mutable study field)."""
        with open_db(dbpath,write=True) as db: return json.dumps(studies.set_study_keywords(db,study_id,keywords_en),ensure_ascii=False)
    @server.tool()
    def get_study(study_id: str) -> str:
        """Return one study by ID."""
        with open_db(dbpath) as db: return json.dumps(studies.get_study(db,study_id),ensure_ascii=False)
    @server.tool()
    def list_studies() -> str:
        """List all studies."""
        with open_db(dbpath) as db: return json.dumps(studies.list_studies(db),ensure_ascii=False)
    @server.tool()
    def begin_research(question_id: str, query_budget: int = 10, study_id: str = '') -> str:
        """Create a bounded research run for a catalogued question, optionally inside a study (recommended)."""
        with open_db(dbpath,write=True) as db: return json.dumps({'run_id':start_run(db,question_id,query_budget,study_id)})
    @server.tool()
    def record_research_event(run_id: str, event_type: str, query: str = '', source_url: str = '', reason: str = '', step_id: str = '') -> str:
        """Record search, discarded or considered source (optionally for a plan step); enforces logged search budget."""
        with open_db(dbpath,write=True) as db: return json.dumps(log_research_event(db,run_id,event_type,query,source_url,reason,step_id))
    @server.tool()
    def plan_research(run_id: str) -> str:
        """Create the deterministic research plan for a run (or return the current one). The run must belong to a study."""
        with open_db(dbpath,write=True) as db: return json.dumps(planner.plan_research(db,run_id),ensure_ascii=False)
    @server.tool()
    def replan_research(run_id: str, reason: str) -> str:
        """Regenerate the plan as a new version; the reason is logged."""
        with open_db(dbpath,write=True) as db: return json.dumps(planner.replan_research(db,run_id,reason),ensure_ascii=False)
    @server.tool()
    def next_step(run_id: str) -> str:
        """Return the next pending plan step and the remaining query budget."""
        with open_db(dbpath) as db: return json.dumps(planner.next_step(db,run_id),ensure_ascii=False)
    @server.tool()
    def execute_step(run_id: str, step_id: str, query: str = '', retry: bool = False, options_json: str = '') -> str:
        """Run one connector-route step against its official API (budgeted, audited). Optional query overrides the plan terms; retry=true only after a transient failure. options_json: connector options, e.g. stackexchange {"site":"softwarerecs","tagged":"crm"}."""
        options=json.loads(options_json) if options_json.strip() else None
        with open_db(dbpath,write=True) as db:
            return json.dumps(executor.execute_step(db,run_id,step_id,query,retry,options=options),ensure_ascii=False)
    @server.tool()
    def list_hits(run_id: str, offset: int = 0, limit: int = 20, step_id: str = '') -> str:
        """Compact, paginated connector results for a run (200-char preview, key metadata, spread across independence groups). Use get_hit for the full snippet. Hits are not evidence until promoted."""
        with open_db(dbpath) as db: return json.dumps(executor.hits_page(db,run_id,offset,limit,step_id),ensure_ascii=False)
    @server.tool()
    def get_hit(hit_id: str) -> str:
        """Full stored hit: snippet (the text an evidence excerpt must come from) and all metadata."""
        with open_db(dbpath) as db: return json.dumps(executor.get_hit(db,hit_id),ensure_ascii=False)
    @server.tool()
    def submit_evidence_from_hit(hit_id: str, claim: str, excerpt: str, extra_json: str = '') -> str:
        """Promote a hit to candidate evidence. excerpt must appear literally in the hit snippet; provenance is filled automatically. extra_json may hold limitations, contradictions, geography, language, sample_frame, sampling_method."""
        extra=json.loads(extra_json) if extra_json.strip() else None
        with open_db(dbpath,write=True) as db: return json.dumps(executor.submit_evidence_from_hit(db,hit_id,claim,excerpt,extra))
    @server.tool()
    def evaluate_question(study_id: str, question_id: str) -> str:
        """Deterministic metrics over the study's evidence for a question and the strongest verdict it allows (with reasons). Read-only."""
        with open_db(dbpath) as db: return json.dumps(evaluator.evaluate_question(db,study_id,question_id),ensure_ascii=False)
    @server.tool()
    def submit_conclusion(study_id: str, question_id: str, record_json: str) -> str:
        """Submit a conclusion: {"verdict","text","cited_evidence_ids":[...],"uncertainty":{"coverage_limits","biases","missing_evidence","contradictions"}}. Rejected if the verdict exceeds the allowed one, citations do not cover enough independent groups, plan gaps are not acknowledged, or the text contains numbers absent from the cited excerpts. Stored as candidate."""
        with open_db(dbpath,write=True) as db:
            return json.dumps(evaluator.submit_conclusion(db,study_id,question_id,json.loads(record_json)),ensure_ascii=False)
    @server.tool()
    def list_conclusions(study_id: str) -> str:
        """List a study's conclusions; stale=true when the evidence changed after the conclusion was written."""
        with open_db(dbpath) as db: return json.dumps(evaluator.list_conclusions(db,study_id),ensure_ascii=False)
    @server.tool()
    def build_silver() -> str:
        """Rebuild the Silver layer (normalized observations and documents) from every stored hit. Idempotent; new steps feed Silver automatically."""
        with open_db(dbpath,write=True) as db: return json.dumps(silver.build_all(db))
    @server.tool()
    def query_observations(source_id: str = '', measure: str = '', geo: str = '', dataset: str = '', limit: int = 50) -> str:
        """Compact list of Silver observations (normalized geo ISO-2, periods, unit, unit_mult, raw and scaled values) to choose metric parameters."""
        with open_db(dbpath) as db: return json.dumps(silver.query_observations(db,source_id,measure,geo,dataset,limit),ensure_ascii=False)
    @server.tool()
    def list_metrics() -> str:
        """Gold metric definitions (catalog/metrics.json): formula, inputs, required params, relation to the catalog metric, caveats, reviewed flag."""
        return json.dumps(list(gold.load_metrics().values()),ensure_ascii=False)
    @server.tool()
    def compute_metric(study_id: str, metric_id: str, params_json: str) -> str:
        """Compute a Gold metric from the study's Silver data with full lineage. Refuses mixed units, overlapping geographies or ambiguous series."""
        with open_db(dbpath,write=True) as db:
            return json.dumps(gold.compute_metric(db,study_id,metric_id,json.loads(params_json or '{}')),ensure_ascii=False)
    @server.tool()
    def promote_metric(gold_id: str, claim: str, question_id: str = '') -> str:
        """Turn a Gold value into derived candidate evidence; its excerpt carries the figure literally and it counts its inputs' groups, not a new one."""
        with open_db(dbpath,write=True) as db: return json.dumps(gold.promote_metric(db,gold_id,claim,question_id),ensure_ascii=False)
    @server.tool()
    def get_attribution(conclusion_id: str) -> str:
        """Citations each licence requires for the sources behind a conclusion (including inputs of derived metrics)."""
        with open_db(dbpath) as db: return json.dumps(attribution.attribution(db,conclusion_id),ensure_ascii=False)
    @server.tool()
    def recheck_deleted() -> str:
        """Re-check cited Bluesky/Mastodon posts and withdraw (reject) evidence whose post was deleted by its author."""
        with open_db(dbpath,write=True) as db: return json.dumps(maintenance.recheck_deleted(db),ensure_ascii=False)
    @server.tool()
    def complete_agent_step(run_id: str, step_id: str, notes: str) -> str:
        """Close an agent-route step with notes on what was searched and found; returns the next step."""
        with open_db(dbpath,write=True) as db: return json.dumps(planner.complete_agent_step(db,run_id,step_id,notes),ensure_ascii=False)
    @server.tool()
    def get_research_run(run_id: str) -> str:
        """Return the research run's audit log."""
        with open_db(dbpath) as db: return json.dumps(run_report(db,run_id),ensure_ascii=False)
    server.run(transport='stdio')

if __name__=='__main__': main()
