import json, glob, os

# 找 codex 的 cards
fs = glob.glob('work/**/phase2/cards/*.json', recursive=True)
print("cards 文件数:", len(fs))
if not fs:
    print("没找到 cards，列 work 下 phase2 结构:")
    for r, d, f in os.walk('work'):
        if r.endswith('phase2'):
            print(" ", r, "->", d)
    raise SystemExit

# 挑一个 codex 的（路径含 codex）
codex_cards = [f for f in fs if 'codex' in f and '_zh' not in f]
sample = codex_cards[0] if codex_cards else fs[0]
d = json.load(open(sample))
print("样本 card:", sample)
print("顶层字段:", list(d.keys()))
print()
for k, v in d.items():
    if isinstance(v, str):
        print(f"  {k}: {v[:90]}")
    elif isinstance(v, list):
        print(f"  {k}: list[{len(v)}]")
        if v and isinstance(v[0], dict):
            print(f"     元素字段: {list(v[0].keys())}")
            print(f"     首元素: {json.dumps(v[0], ensure_ascii=False)[:300]}")
    else:
        print(f"  {k}: {type(v).__name__}")
