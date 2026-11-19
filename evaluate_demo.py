"""对已有合成库跑开发基准：只读业务函数，不调用模型，不生成预期。"""
from datetime import datetime
import argparse
import hashlib
import json
from pathlib import Path
import time

from agent_runtime import search_policies
from database import get_engine
from merchant_service import check_shop_order, summary_shop_orders
from refund_service import check_order, summary_orders

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "local/evaluation.json")
    args = parser.parse_args()
    baseline = json.loads((ROOT / "evaluation/cases.json").read_text())
    engine = get_engine()
    results = []
    try:
        for case in baseline["cases"]:
            params = {k: datetime.fromisoformat(v) if k in {"paid_from", "paid_before", "refund_before"} else v
                      for k, v in case["input"].items()}
            started = time.monotonic()
            try:
                if case["tool"] == "search_policies":
                    output = search_policies(**params)
                elif case["tool"] == "check_order":
                    fn = check_shop_order if case["role"] == "merchant" else check_order
                    output = fn(engine, params["order_id"], case["identity"], params["refund_before"])
                else:
                    fn = summary_shop_orders if case["role"] == "merchant" else summary_orders
                    output = fn(engine, case["identity"], **params)
                passed = all(output.get(k) == v for k, v in case["expected"].items()) and not any(k in output for k in case.get("forbidden_fields", []))
                error = None
            except Exception as exc:
                output, passed, error = None, False, type(exc).__name__
            results.append({"case_id": case["id"], "passed": passed, "result": output, "error": error,
                            "elapsed_seconds": round(time.monotonic() - started, 4)})
    finally:
        engine.dispose()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({"baseline": baseline["version"], "scope": "真实本机业务工具只读；非真实模型任务全量基准",
        "source_sha256": hashlib.sha256((ROOT / "evaluation/cases.json").read_bytes()).hexdigest(), "results": results}, ensure_ascii=False, indent=2))
    print(f"开发基准通过 {sum(r['passed'] for r in results)}/{len(results)}；本次未调用模型。证据：{args.output}")
    return int(not all(r["passed"] for r in results))


if __name__ == "__main__":
    raise SystemExit(main())
