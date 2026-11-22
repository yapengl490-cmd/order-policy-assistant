"""仅离线检查开发店元信息连接；所有凭据和响应均为测试替身。"""

import contextlib
import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

import httpx

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import shopify_connection as connection
import setup_shopify


SHOP = "offline-demo.myshopify.com"
CLIENT = "offline_client_id_1234"
SECRET = "offline_secret_DO_NOT_OUTPUT_1234"
TOKEN = "offline_access_token_DO_NOT_OUTPUT_1234"


def settings():
    return connection.ShopifySettings(SHOP, CLIENT, SECRET)


def successful_data():
    return {"data": {
        "shop": {"myshopifyDomain": SHOP, "currencyCode": "CNY", "ianaTimezone": "Asia/Shanghai",
                 "plan": {"partnerDevelopment": True}},
        "currentAppInstallation": {"accessScopes": [
            {"handle": scope} for scope in sorted(connection.REQUIRED_SCOPES)]},
    }}


class ShopifyConnectionTests(unittest.TestCase):
    def transport(self, *, data=None, token_status=200, query_status=200,
                  token_body=None, query_headers=None, token_headers=None):
        requests = []
        body = successful_data() if data is None else data

        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.scheme, "https")
            self.assertEqual(request.url.host, SHOP)
            if request.url.path == "/admin/oauth/access_token":
                return httpx.Response(token_status, headers=token_headers,
                                      json=token_body if token_body is not None else {"access_token": TOKEN})
            self.assertEqual(request.url.path, f"/admin/api/{connection.API_VERSION}/graphql.json")
            return httpx.Response(query_status, json=body, headers=query_headers)

        return httpx.MockTransport(handler), requests

    def assert_safe_error(self, callback):
        with self.assertRaises(connection.ShopifyConnectionError) as context:
            callback()
        text = str(context.exception)
        for sensitive in (SECRET, TOKEN, CLIENT):
            self.assertNotIn(sensitive, text)
        return text

    def test_domain_accepts_only_normalized_shop_hostname(self):
        for raw in (SHOP, SHOP.upper(), " https://" + SHOP + "/ "):
            with self.subTest(raw=raw):
                self.assertEqual(connection.normalize_shop_domain(raw), SHOP)

    def test_domain_rejects_other_hosts_paths_ports_and_userinfo(self):
        for domain in ("http://" + SHOP, "https://evil.example", SHOP + ".evil.example",
                       "evil.example/" + SHOP, SHOP + "@evil.example", "x@" + SHOP,
                       SHOP + ":443", SHOP + "/admin", SHOP + "?token=" + SECRET,
                       SHOP + "#fragment", "127.0.0.1", "[::1]", "myshopify.com",
                       "-bad.myshopify.com", "bad-.myshopify.com", "a..myshopify.com",
                       "a" * 64 + ".myshopify.com", "測試.myshopify.com", "a\n.myshopify.com",
                       "https://" + SHOP + "//", None):
            with self.subTest(domain=domain):
                self.assert_safe_error(lambda: connection.ShopifySettings(domain, CLIENT, SECRET))

    def test_settings_reject_invalid_credentials_without_echo(self):
        for value in ("", "short", "contains spaces", "x" * 513, "secret\nvalue", None):
            with self.subTest(value=repr(value)):
                self.assert_safe_error(lambda: connection.ShopifySettings(SHOP, CLIENT, value))
        self.assertNotIn(SECRET, repr(settings()))
        self.assertNotIn(CLIENT, repr(settings()))

    def test_two_requests_only_and_fixed_non_customer_query(self):
        transport, requests = self.transport()
        result = connection.check_connection(settings(), transport=transport)
        self.assertEqual(len(requests), 2)
        self.assertEqual(parse_qs(requests[0].content.decode()), {
            "grant_type": ["client_credentials"], "client_id": [CLIENT], "client_secret": [SECRET]})
        self.assertEqual(json.loads(requests[1].content), {"query": connection.CONNECTION_QUERY})
        self.assertEqual(requests[1].headers["X-Shopify-Access-Token"], TOKEN)
        self.assertEqual(result["status"], "connection_verified")
        self.assertTrue(result["development_store"])
        self.assertEqual((result["currency"], result["timezone"]), ("CNY", "Asia/Shanghai"))
        self.assertEqual(result["scopes"], sorted(connection.REQUIRED_SCOPES))
        self.assertFalse(result["orders_read"])
        self.assertFalse(result["customer_data_read"])
        self.assertFalse(result["model_called"])
        for sensitive in (CLIENT, SECRET, TOKEN):
            self.assertNotIn(sensitive, json.dumps(result))
        self.assertNotIn("orders {", connection.CONNECTION_QUERY)
        self.assertNotIn("customer", connection.CONNECTION_QUERY.lower())

    def test_token_redirect_is_not_followed(self):
        transport, requests = self.transport(token_status=302,
                                            token_headers={"Location": "https://evil.example/collect"})
        self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))
        self.assertEqual(len(requests), 1)

    def test_graphql_redirect_is_not_followed(self):
        transport, requests = self.transport(query_status=307,
                                            query_headers={"Location": "https://evil.example/collect"})
        self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))
        self.assertEqual(len(requests), 2)

    def test_http_200_graphql_errors_rejects_partial_success(self):
        body = successful_data()
        body["errors"] = [{"message": SECRET + TOKEN, "extensions": {"code": "ACCESS_DENIED"}}]
        transport, _ = self.transport(data=body)
        self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))

    def test_non_development_or_truthy_string_rejected(self):
        for value in (False, None, "true", 1):
            with self.subTest(value=value):
                body = successful_data()
                body["data"]["shop"]["plan"]["partnerDevelopment"] = value
                transport, _ = self.transport(data=body)
                self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))

    def test_different_shop_rejected(self):
        body = successful_data()
        body["data"]["shop"]["myshopifyDomain"] = "other.myshopify.com"
        transport, _ = self.transport(data=body)
        self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))

    def test_each_required_scope_and_empty_scope_rejected(self):
        for scopes in ([], ["read_orders"], ["read_legal_policies"]):
            with self.subTest(scopes=scopes):
                body = successful_data()
                body["data"]["currentAppInstallation"]["accessScopes"] = [{"handle": s} for s in scopes]
                transport, _ = self.transport(data=body)
                self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))

    def test_extra_scope_rejected(self):
        body = successful_data()
        body["data"]["currentAppInstallation"]["accessScopes"].append({"handle": "write_orders"})
        transport, _ = self.transport(data=body)
        self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))

    def test_malformed_shop_installation_or_scope_is_safe_error(self):
        mutations = [
            lambda b: b.update(data=None),
            lambda b: b["data"].update(shop=None),
            lambda b: b["data"].update(currentAppInstallation=None),
            lambda b: b["data"]["shop"].update(currencyCode={"secret": SECRET}),
            lambda b: b["data"]["shop"].update(ianaTimezone=None),
            lambda b: b["data"]["currentAppInstallation"].update(accessScopes="not-a-list"),
            lambda b: b["data"]["currentAppInstallation"].update(accessScopes=[{"handle": [SECRET]}]),
            lambda b: b["data"]["currentAppInstallation"].update(accessScopes=[None]),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                body = successful_data()
                mutate(body)
                transport, _ = self.transport(data=body)
                self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))

    def test_wrong_api_version_rejected(self):
        transport, _ = self.transport(query_headers={"X-Shopify-API-Version": "2026-07"})
        self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))

    def test_http_failures_hide_raw_body_and_do_not_retry(self):
        for stage in ("token", "query"):
            for status in (401, 403, 429, 500):
                with self.subTest(stage=stage, status=status):
                    kwargs = ({"token_status": status, "token_body": {"error": SECRET + TOKEN}}
                              if stage == "token" else {"query_status": status, "data": {"error": SECRET + TOKEN}})
                    transport, requests = self.transport(**kwargs)
                    self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))
                    self.assertEqual(len(requests), 1 if stage == "token" else 2)

    def test_missing_or_invalid_token_stops_before_graphql(self):
        for value in (None, "", SECRET + "\n", {}, "x" * 2049):
            with self.subTest(value=type(value).__name__):
                transport, requests = self.transport(token_body={"access_token": value})
                self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))
                self.assertEqual(len(requests), 1)

    def test_network_exception_message_not_disclosed(self):
        for exc in (httpx.ReadTimeout, httpx.ConnectError):
            count = []

            def handler(request):
                count.append(request)
                raise exc(SECRET + TOKEN, request=request)

            with self.subTest(exception=exc.__name__):
                self.assert_safe_error(lambda: connection.check_connection(settings(), transport=httpx.MockTransport(handler)))
                self.assertEqual(len(count), 1)

    def test_invalid_json_and_overlarge_response_are_safe_errors(self):
        for content in (SECRET.encode(), json.dumps([SECRET]).encode(), b"x" * 262145):
            with self.subTest(size=len(content)):
                transport = httpx.MockTransport(lambda request: httpx.Response(200, content=content))
                self.assert_safe_error(lambda: connection.check_connection(settings(), transport=transport))


class ShopifyConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "local" / "shopify.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_save_roundtrip_private_mode_and_no_token(self):
        settings().save(self.path)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(connection.ShopifySettings.load(self.path), settings())
        self.assertNotIn("access_token", json.loads(self.path.read_text()))
        self.assertEqual(list(self.path.parent.glob(".shopify-*.tmp")), [])

    def test_invalid_settings_cannot_replace_existing_config(self):
        settings().save(self.path)
        original = self.path.read_bytes()
        with self.assertRaises(connection.ShopifyConnectionError):
            connection.ShopifySettings(SHOP, CLIENT, "bad secret").save(self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_failed_atomic_replace_preserves_existing_file_and_cleans_temp(self):
        settings().save(self.path)
        original = self.path.read_bytes()
        with patch.object(connection.os, "replace", side_effect=PermissionError("denied")):
            with self.assertRaises(connection.ShopifyConnectionError):
                settings().save(self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob(".shopify-*.tmp")), [])

    def test_symlink_file_and_parent_are_rejected(self):
        settings().save(self.path)
        linked = self.root / "linked.json"
        linked.symlink_to(self.path)
        with self.assertRaises(connection.ShopifyConnectionError):
            settings().save(linked)
        with self.assertRaises(connection.ShopifyConnectionError):
            connection.ShopifySettings.load(linked)
        linked_parent = self.root / "linked-directory"
        linked_parent.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaises(connection.ShopifyConnectionError):
            settings().save(linked_parent / "new.json")

    def test_load_rejects_broad_permissions(self):
        settings().save(self.path)
        self.path.chmod(0o644)
        with self.assertRaises(connection.ShopifyConnectionError):
            connection.ShopifySettings.load(self.path)

    def test_load_rejects_extra_fields_wrong_mode_version_and_malformed_json(self):
        settings().save(self.path)
        original = json.loads(self.path.read_text())
        variants = [[], {**original, "access_token": TOKEN}, {**original, "mode": "production"},
                    {**original, "api_version": "2026-07"}, {**original, "client_secret": "invalid space"}]
        for body in variants:
            with self.subTest(body_type=type(body).__name__):
                self.path.write_text(json.dumps(body))
                with self.assertRaises(connection.ShopifyConnectionError) as context:
                    connection.ShopifySettings.load(self.path)
                self.assertNotIn(TOKEN, str(context.exception))
                self.assertNotIn(SECRET, str(context.exception))
        self.path.write_text(SECRET)
        with self.assertRaises(connection.ShopifyConnectionError):
            connection.ShopifySettings.load(self.path)

    def run_main(self, argv, *, input_values=None, secrets=None):
        output = io.StringIO()
        with patch.object(setup_shopify, "ROOT", self.root), patch.object(sys, "argv", ["setup_shopify.py", *argv]), \
                patch.object(sys.stdin, "isatty", return_value=True), \
                patch("builtins.input", side_effect=input_values), \
                patch.object(setup_shopify.getpass, "getpass", side_effect=secrets), \
                contextlib.redirect_stdout(output):
            code = setup_shopify.main()
        return code, output.getvalue()

    def test_setup_save_does_not_connect_or_echo_credentials(self):
        with patch.object(setup_shopify, "check_connection", side_effect=AssertionError("unexpected network")):
            code, output = self.run_main([], input_values=[SHOP], secrets=[CLIENT, SECRET])
        self.assertEqual(code, 0)
        self.assertEqual(connection.ShopifySettings.load(self.path), settings())
        for sensitive in (CLIENT, SECRET, TOKEN):
            self.assertNotIn(sensitive, output)

    def test_setup_declined_replacement_preserves_old_bytes(self):
        settings().save(self.path)
        original = self.path.read_bytes()
        code, _ = self.run_main([], input_values=["no"])
        self.assertEqual(code, 0)
        self.assertEqual(self.path.read_bytes(), original)

    def test_setup_invalid_replacement_preserves_old_bytes(self):
        settings().save(self.path)
        original = self.path.read_bytes()
        code, output = self.run_main([], input_values=["REPLACE", SHOP], secrets=[CLIENT, "invalid space"])
        self.assertEqual(code, 1)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertNotIn("invalid space", output)

    def test_check_failure_keeps_credentials_and_updates_latest(self):
        settings().save(self.path)
        evidence = self.path.parent / "shopify_connection_check.json"
        evidence.write_text("previous-safe-evidence")
        original = self.path.read_bytes()
        with patch.object(setup_shopify, "check_connection", side_effect=connection.ShopifyConnectionError("连接失败")):
            code, _ = self.run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(evidence.read_text())["status"], "connection_failed")
        self.assertEqual(self.path.read_bytes(), original)

    def test_success_then_failure_keeps_both_reports_and_latest_failed(self):
        settings().save(self.path)
        transport, _ = ShopifyConnectionTests().transport()
        success = connection.check_connection(settings(), transport=transport)
        with patch.object(setup_shopify, "check_connection", return_value=success):
            code, _ = self.run_main(["--check"])
        self.assertEqual(code, 0)
        with patch.object(setup_shopify, "check_connection", side_effect=connection.ShopifyConnectionError("连接失败")):
            code, _ = self.run_main(["--check"])
        self.assertEqual(code, 1)
        reports = list((self.path.parent / "shopify_checks").glob("*.json"))
        self.assertEqual(len(reports), 2)
        self.assertEqual({json.loads(p.read_text())["status"] for p in reports},
                         {"connection_verified", "connection_failed"})
        latest = self.path.parent / "shopify_connection_check.json"
        self.assertEqual(json.loads(latest.read_text())["status"], "connection_failed")
        for report in [*reports, latest]:
            self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
            for sensitive in (CLIENT, SECRET, TOKEN):
                self.assertNotIn(sensitive, report.read_text())

    def test_load_failure_records_failure_without_network_or_secret_echo(self):
        self.path.parent.mkdir()
        self.path.write_text(SECRET)
        self.path.chmod(0o600)
        with patch.object(setup_shopify, "check_connection", side_effect=AssertionError("unexpected network")):
            code, output = self.run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertNotIn(SECRET, output)
        latest = json.loads((self.path.parent / "shopify_connection_check.json").read_text())
        self.assertEqual(latest["status"], "connection_failed")
        self.assertNotIn(SECRET, json.dumps(latest))


if __name__ == "__main__":
    unittest.main()
