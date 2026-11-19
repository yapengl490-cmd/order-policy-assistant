"""部署后只读核新账号→网页API→真实库/MCP；不建账号、不改数据、不调用模型。"""
import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prepare_public_demo import ACCOUNT_ID, SHOP_ID, SCOPE_KEY, _private_json, _validate_accounts
from mcp_order_client import OrderProtocolClient


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("证据已存在，未覆盖；请用新文件名")
    report = {"executor": "assistant", "kind": "live_read_only_api_and_mcp_no_model", "checks": {}}
    stage = "read_local_credentials"
    try:
        access, _ = _private_json(args.root / "local/access.json")
        account = _validate_accounts(access)
        assert account is not None
        legacy = next(row for row in access["accounts"] if any(
            role.get("role") == "merchant" and role.get("shop_id") == "S001" for role in row.get("roles", [])))
        expected = json.loads((args.root / "samples/uci/uci_expected.json").read_text())
        comparison = json.loads((args.root / "samples/uci/uci_demo_comparison_expected.json").read_text())
        opener = build_opener(ProxyHandler({}), NoRedirect())

        def request(path, credential=account, mode="merchant", body=None, status=200):
            req = Request("http://127.0.0.1:18080" + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={"Authorization": "Bearer " + credential["token"], "X-Access-Mode": mode,
                         "Content-Type": "application/json"})
            try:
                response = opener.open(req, timeout=15)
            except HTTPError as error:
                response = error
            with response:
                assert response.status == status
                raw = response.read(1024 * 1024 + 1)
            assert len(raw) <= 1024 * 1024
            return json.loads(raw)

        stage = "new_account_scope"
        identity = request("/api/me")
        assert identity["account_id"] == ACCOUNT_ID and identity["shop_id"] == SHOP_ID
        report["checks"][stage] = identity
        request("/api/me", mode="buyer", status=401)
        report["checks"]["buyer_role_denied"] = True
        stage = "all_orders_and_provenance"
        listed = request("/api/orders?limit=20")
        assert {row["order_id"] for row in listed["orders"]} == {row["order_id"] for row in expected["orders"]}
        details = []
        for row in expected["orders"]:
            got = request("/api/orders/" + row["order_id"] + "?" + urlencode({"refund_before": expected["cutoff"]}))
            assert got["status"] == "ok"
            assert (got["paid_amount_fen"], got["refund_amount_fen"], got["net_amount_fen"]) == (
                row["paid_fen"], row["refund_fen"], row["net_fen"])
            assert got["provenance"]["status"] == "recorded"
            assert all("合成" in item["source"] for item in got["provenance"]["batches"])
            details.append(got)
        report["checks"][stage] = details
        stage = "scope_boundary"
        cross_old = request("/api/orders/O001")
        cross_new = request("/api/orders/" + expected["orders"][0]["order_id"], credential=legacy)
        assert cross_old["status"] == cross_new["status"] == "not_found"
        assert "paid_amount_fen" not in cross_old and "paid_amount_fen" not in cross_new
        report["checks"][stage] = {"new_cannot_read_S001": True, "old_S001_cannot_read_new": True}
        stage = "two_period_channel_comparison"
        actual = request("/api/analysis/compare", body=comparison["conditions"])
        for key in ("first", "second", "delta", "reason_status"):
            assert actual[key] == comparison[key]
        assert {row["channel"]: row["contribution_net_fen"] for row in actual["channels"]} == comparison["channel_net_delta_fen"]
        assert actual["provenance"]["status"] == "recorded"
        report["checks"][stage] = actual
        stage = "real_mcp_new_scope"
        client = OrderProtocolClient(SCOPE_KEY, root=args.root)
        row = expected["orders"][0]
        normal = client.check_order(order_id=row["order_id"], refund_before=expected["cutoff"])
        assert normal["status"] == "ok" and normal["net_amount_fen"] == row["net_fen"]
        report["checks"][stage] = {"result": normal, "trace": dict(client.last_trace)}
        denied = client.check_order(order_id="O001", refund_before=expected["cutoff"])
        assert denied["status"] == "not_found" and "paid_amount_fen" not in denied
        report["checks"]["real_mcp_cross_scope"] = {"result": denied, "trace": dict(client.last_trace)}
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", stage=stage, error_type=type(error).__name__)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({"status": report["status"], "stage": stage, "evidence": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
