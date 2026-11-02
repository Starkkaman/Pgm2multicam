"""Stand-in for the pgm2mc engine in tests/test_resolve_lua.py: records argv + env, prints CLI-style output.

FAKE_ENGINE_MODE: "" (success), "fail" (ERROR + exit 1), "ss" (one SuperSource layout + analysis JSON).
"""
import json
import os
import sys

args = sys.argv[1:]
if "--help" in args:
    print("usage: pgm2mc PGM ISO [ISO ...] -o OUTDIR")
    sys.exit(0)
mode = os.environ.get("FAKE_ENGINE_MODE", "")
out = args[args.index("-o") + 1]
name = args[args.index("--name") + 1]
with open(os.path.join(out, "argv.json"), "w", encoding="utf-8") as fh:
    json.dump({"argv": args, "env": {k: os.environ.get(k) for k in ("PYTHONPATH", "PYTHONUNBUFFERED", "PYTHONHOME")}},
              fh)
for stage in ("probe", "sync", "frames", "match", "write"):
    sys.stderr.write("PROGRESS %s 50\nPROGRESS %s 100\n" % (stage, stage))
sys.stderr.write("WARN fake warning é\n")
sys.stderr.flush()
if mode == "fail":
    sys.stderr.write("ERROR no overlap between PGM and ISOs\n")
    sys.exit(1)
res = {"warnings": ["fake warning é"], "segments": 4, "transitions": 12, "stills": 3,
       "supersources": 1 if mode == "ss" else 0}
sdir = os.path.join(out, name + "_stills")
os.makedirs(sdir, exist_ok=True)
for s in ("s1.png", "s2.png"):
    open(os.path.join(sdir, s), "wb").close()
res["stills_dir"] = sdir
for kind in ("multicam", "conform", "stacked"):
    p = os.path.join(out, "%s_%s.fcpxml" % (name, kind))
    with open(p, "w") as fh:
        fh.write("<fcpxml/>")
    res[kind] = p
res["analysis"] = os.path.join(out, name + "_analysis.json")
if mode == "ss":
    iso_paths = [a for a in args[1:args.index("-o")]]
    analysis = {
        "version": 2,
        "program": {"path": args[0], "width": 1920, "height": 1080},
        "isos": [{"id": "a%d" % (i + 1), "name": os.path.splitext(os.path.basename(p))[0], "path": p,
                  "width": 3840, "height": 2160} for i, p in enumerate(iso_paths)],
        "transitions": [{"edit": 300, "duration": 10, "kind": "dissolve", "color": None}],
        "supersources": [{"id": "ss1", "background": "s2", "occurrences": [[300, 900]],
                          "boxes": [{"angle": "a1", "layer": 1, "rect": [0.07, 0.275, 0.43, 0.725],
                                     "crop": [0.1, 0, 0.1, 0]}]}],
    }
    with open(res["analysis"], "w", encoding="utf-8") as fh:
        json.dump(analysis, fh)
print("some log line")
print("RESULT " + json.dumps(res))
