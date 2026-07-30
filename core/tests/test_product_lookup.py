from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from django.test import SimpleTestCase

from core.product_lookup import (
    ProductLookupError,
    enrich_product_data,
    extract_barcode,
    lookup_product,
    parse_barcodelookup_html,
    parse_go_upc_html,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class ExtractBarcodeTests(SimpleTestCase):
    def test_from_barcodelookup_url(self):
        self.assertEqual(
            extract_barcode("https://www.barcodelookup.com/030111179302"),
            "030111179302",
        )

    def test_from_raw_barcode(self):
        self.assertEqual(extract_barcode("030111179302"), "030111179302")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            extract_barcode("https://example.com/product/abc")


class ParseBarcodeLookupHtmlTests(SimpleTestCase):
    def test_parses_fixture(self):
        html = (FIXTURES / "barcodelookup_030111179302.html").read_text()
        product = parse_barcodelookup_html(html, barcode="030111179302")
        self.assertEqual(product.barcode, "030111179302")
        self.assertIn("Royal Canin", product.name)
        self.assertEqual(product.brand, "Royal Canin")
        self.assertEqual(product.weight, 30.0)
        self.assertEqual(product.estimated_price, Decimal("74.99"))
        self.assertEqual(product.product_type, "kibble")
        self.assertEqual(product.life_stages, "ADL")
        self.assertIn("CHKN", product.proteins)
        self.assertTrue(product.image_urls)

    def test_rejects_challenge_page(self):
        html = (FIXTURES / "barcodelookup_challenge.html").read_text()
        with self.assertRaises(ProductLookupError):
            parse_barcodelookup_html(html, barcode="030111179302")


class ParseGoUpcHtmlTests(SimpleTestCase):
    def test_parses_live_fixture(self):
        html = (FIXTURES / "go_upc_030111179302.html").read_text()
        product = parse_go_upc_html(html, barcode="030111179302")
        self.assertEqual(product.barcode, "030111179302")
        self.assertIn("Royal Canin", product.name)
        self.assertEqual(product.brand, "Royal Canin")
        self.assertEqual(product.weight, 30.0)
        self.assertEqual(product.product_type, "kibble")
        self.assertEqual(product.life_stages, "ADL")
        self.assertTrue(product.description)


class EnrichmentTests(SimpleTestCase):
    def test_senior_and_grain_free(self):
        from core.product_lookup import ProductData

        product = enrich_product_data(
            ProductData(
                barcode="1",
                name="Grain-Free Senior Salmon Dry Dog Food 12 lb",
                description="Supports joint health",
            )
        )
        self.assertEqual(product.life_stages, "SNR")
        self.assertTrue(product.grain_free)
        self.assertEqual(product.proteins, ["SALM"])
        self.assertEqual(product.special_diet, "JONT")
        self.assertEqual(product.weight, 12.0)

    def test_ignores_fish_oil_as_protein(self):
        from core.product_lookup import ProductData

        product = enrich_product_data(
            ProductData(
                barcode="1",
                name="Adult Dry Dog Food 30 lb",
                description="Chicken by-product meal with fish oil for coat health",
            )
        )
        self.assertEqual(product.proteins, ["CHKN"])
        self.assertIsNone(product.special_diet)


class LookupFallbackTests(SimpleTestCase):
    def test_falls_back_when_barcodelookup_blocked(self):
        challenge = (FIXTURES / "barcodelookup_challenge.html").read_text()
        go_upc = (FIXTURES / "go_upc_030111179302.html").read_text()

        def fake_get(url, *args, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "barcodelookup.com" in url:
                response.text = challenge
                response.status_code = 200
            elif "go-upc.com" in url:
                response.text = go_upc
                response.status_code = 200
            else:
                raise AssertionError(f"unexpected url {url}")
            return response

        session = MagicMock()
        session.headers = {}
        session.get.side_effect = fake_get

        product = lookup_product(
            "https://www.barcodelookup.com/030111179302",
            session=session,
            api_key="",
        )
        self.assertEqual(product.source, "go_upc_html")
        self.assertEqual(product.barcode, "030111179302")
        self.assertIn("Royal Canin", product.name)

    def test_upcitemdb_fallback(self):
        challenge = (FIXTURES / "barcodelookup_challenge.html").read_text()
        payload = {
            "code": "OK",
            "items": [
                {
                    "upc": "030111179302",
                    "title": "Royal Canin Large Adult Dry Dog Food 30 lb bag",
                    "brand": "Royal Canin",
                    "description": "Adult dry dog food with chicken",
                    "lowest_recorded_price": 74.99,
                    "images": ["https://example.com/img.jpg"],
                    "offers": [],
                }
            ],
        }

        def fake_get(url, *args, **kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "barcodelookup.com" in url:
                response.text = challenge
            elif "go-upc.com" in url:
                response.text = "<html><body><p>No product found</p></body></html>"
            elif "upcitemdb.com" in url:
                response.json = MagicMock(return_value=payload)
            else:
                raise AssertionError(url)
            return response

        session = MagicMock()
        session.headers = {}
        session.get.side_effect = fake_get

        product = lookup_product("030111179302", session=session, api_key="")
        self.assertEqual(product.source, "upcitemdb")
        self.assertEqual(product.estimated_price, Decimal("74.99"))
        self.assertEqual(product.weight, 30.0)
