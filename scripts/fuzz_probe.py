"""Ad-hoc fuzz probe: report internal errors that the api guard would wrap."""
import collections, random, sys, time, traceback
sys.path[:0] = ["tests", "src", "oracle"]
import test_fuzz as t, msdoc
from msdoc import cfb
from msdoc.errors import MsDocError

seed = sys.argv[1] if len(sys.argv) > 1 else "x"
n = int(sys.argv[2]) if len(sys.argv) > 2 else 200
c = collections.Counter(); slow = []
O_OPEN, O_EX = cfb.CompoundFile.open_stream, cfb.CompoundFile.exists
for fid, p in t.FILES:
    cfb.CompoundFile.open_stream, cfb.CompoundFile.exists = O_OPEN, O_EX
    raw = p.read_bytes(); st = t._streams(raw)
    rng = random.Random(f"{seed}{fid}")
    for i in range(n):
        m = t.MODES[i % 7]; mu = t._mutate(rng, st, m)
        cfb.CompoundFile.open_stream = lambda self, nm, _m=mu: _m[nm] if nm in _m else (_ for _ in ()).throw(MsDocError("nf"))
        cfb.CompoundFile.exists = lambda self, nm, _m=mu: nm in _m
        t0 = time.monotonic()
        try:
            msdoc.extract(raw); c[('ok', m)] += 1
        except MsDocError as e:
            c[(type(e).__name__, m)] += 1
            if "internal error" in str(e):
                tb = traceback.extract_tb(e.__cause__.__traceback__)[-1]
                c[(type(e.__cause__).__name__, f"{tb.filename.split('/')[-1]}:{tb.lineno}")] += 1
        except BaseException as e:
            tb = traceback.extract_tb(e.__traceback__)[-1]
            c[("ESCAPED " + type(e).__name__, f"{tb.filename.split('/')[-1]}:{tb.lineno}")] += 1
        if time.monotonic() - t0 > 1: slow.append((fid, m, i, time.monotonic() - t0))
for k, v in c.most_common(): print(v, k)
print("slow", slow[:10])
