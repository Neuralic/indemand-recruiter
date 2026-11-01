"""Re-score live jargon runs POOLED (position-independent).

Server-side turn detection merged some of the 28 sentences into one turn, so scoring transcript[i] against
sentence[i] misaligns. Instead: join all transcripts, and for each term count occurrences (compact form) - each
term was spoken exactly twice, so hits = min(count, 2). Over-counting is capped; a term mentioned inside another
term's sentence could inflate, so we report the raw counts too.
"""

import glob
import json
import re
import sys
from collections import Counter

sys.path.insert(0, ".")
import importlib.util

spec = importlib.util.spec_from_file_location("jargon", "04_jargon.py")
j = importlib.util.module_from_spec(spec)
spec.loader.exec_module(j)

TERMS = list(dict.fromkeys(t for t, _, _ in j.ITEMS))
ALTS = {t: a for t, _, a in j.ITEMS}
for path in sorted(glob.glob("out/04_jargon_live_*.json")):
    d = json.load(open(path, encoding="utf-8"))
    pooled = j.compact(" ".join(d["transcripts"]))
    counts = Counter({t: pooled.count(j.compact(t)) for t in TERMS})
    hits = sum(min(counts[t], 2) for t in TERMS)
    wrong = [f"{t}({counts[t]}/2)" for t in TERMS if counts[t] < 2]
    print(f"{path[8:-5]:<62} pooled {hits}/28 ({100*hits/28:.0f}%)  short: {wrong}")
