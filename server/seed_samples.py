"""Store one simulated session as a labelled sample candidate and score it.
usage: python server/seed_samples.py <strong|borderline|weak> "<fictional name>" [session_stem]   (default stem: the newest engine session)
Sample candidates are labelled 'Sample candidate (simulated)' in the data itself (interviews.label / is_sample / sample_kind)."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server.config as cfg  # noqa: E402
from server import db, ingest  # noqa: E402

kind, name = sys.argv[1], sys.argv[2]
assert kind in ("strong", "borderline", "weak")
stem = sys.argv[3] if len(sys.argv) > 3 else max(cfg.ENGINE_OUT.glob("26_interview_*.json"), key=lambda p: p.stat().st_mtime).stem
iid = ingest.ingest(stem, candidate_name=name, label=cfg.SAMPLE_LABEL, is_sample=True, sample_kind=kind)
ok = asyncio.run(ingest.score(iid))
sc = db.one("SELECT * FROM scorecards WHERE interview_id=?", iid)
print(f"{kind}: {name} <- {stem}: scorecard {sc['status']} {sc['model']} score={sc['overall_score']} rec={sc['recommendation']} in {sc['generated_in_s']}s "
      f"{('error=' + sc['error']) if sc['error'] else ''}")
sys.exit(0 if ok else 1)
