import tomllib
d = tomllib.load(open("uv.lock", "rb"))
pk = [p for p in d["package"] if p["name"] != "spike"]
open("constraints-uvlock.txt", "w").write("".join(f"{p['name']}=={p['version']}\n" for p in pk))
print(len(pk), "pins")
for n in ("litellm", "brotli", "brotlicffi", "pipecat-ai", "onnxruntime", "aiortc", "av"):
    print(n, [p["version"] for p in pk if p["name"] == n])
