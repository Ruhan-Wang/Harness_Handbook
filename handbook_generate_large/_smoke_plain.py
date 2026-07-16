import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "shared")
sys.path.insert(0, "phase2")
sys.path.insert(0, "phase1")

import read_files
import nav_pack as nav
from api_client import Api

SRC = Path(os.environ.get("SRC", "/path/to/source/repo"))
lang = sys.argv[1] if len(sys.argv) > 1 else "zh"

graph = json.load(open(os.environ.get("SMOKE_GRAPH", "work/repo/phase1/graph.json")))
inv = read_files.build_inventory(graph)
navpack = nav.build_nav_pack(graph)
files = nav.all_file_descriptors(graph, navpack)

# pick two smallish files that have a few functions, for a representative sample
cand = [f for f in files if 2 <= f.get("n_functions", 0) <= 8]
sample = (cand or files)[:2]
print("SAMPLE:", [f["file"] for f in sample], "lang=", lang)

api = Api()
print("model:", api.model_marker, "| endpoint:", api.base_url)

res = read_files._describe_batch_safe(api, SRC, sample, 0, "deep", inv, 60000, lang)
for rel, entry in res.items():
    print("\n" + "=" * 80)
    print("FILE:", rel, "| role:", entry.get("role"), "| lifecycle:", entry.get("lifecycle"))
    print("PURPOSE:", entry.get("purpose"))
    print("DESCRIPTION:", entry.get("description"))
    for fn in (entry.get("functions") or [])[:2]:
        print("  FN", fn.get("qualname"))
        print("    purpose  :", fn.get("purpose"))
        print("    data_flow:", fn.get("data_flow"))
        print("    relations:", fn.get("relations"))
