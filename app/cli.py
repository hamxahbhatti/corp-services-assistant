"""Terminal demo:  python -m app.cli --persona sara "ما حالة الفاتورة 5100-2291؟ جهّز رسالة متابعة" [--approve|--reject]"""
import argparse
import asyncio
import json
import logging

logging.basicConfig(level=logging.WARNING)
for _n in ("agent_framework", "httpx", "mcp"):
    logging.getLogger(_n).setLevel(logging.WARNING)

from .telemetry import spans_for
from .workflow.build import ask, decide, new_session


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--persona", default="sara")
    ap.add_argument("--approve", action="store_true")
    ap.add_argument("--reject", action="store_true")
    ap.add_argument("--trace", action="store_true")
    a = ap.parse_args()
    s = new_session(a.persona)
    r = await ask(s, a.text)
    for o in r["outputs"]:
        print(json.dumps({k: v for k, v in o.items() if k not in ("route",)}, ensure_ascii=False, indent=2)[:4000])
    if r["pending_approval"]:
        print("\n--- DRAFT awaiting approval ---")
        print(json.dumps(r["pending_approval"]["draft"], ensure_ascii=False, indent=2))
        if a.approve or a.reject:
            r2 = await decide(s, approved=a.approve)
            for o in r2["outputs"]:
                print(json.dumps(o, ensure_ascii=False, indent=2))
    if a.trace:
        for sp in spans_for(s.trace_ids):
            print(f"{(sp['end_ns'] - sp['start_ns']) / 1e6:8.1f} ms  {sp['name']}")


if __name__ == "__main__":
    asyncio.run(main())
