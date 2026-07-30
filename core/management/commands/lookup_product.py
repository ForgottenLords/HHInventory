"""Parse barcode product pages into HHInventory-friendly product data.

Usage:
  python manage.py lookup_product https://www.barcodelookup.com/030111179302
  python manage.py lookup_product 030111179302 --json
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand, CommandError

from core.product_lookup import ProductLookupError, lookup_product


class Command(BaseCommand):
    help = "Look up product details from a barcode or barcodelookup.com URL"

    def add_arguments(self, parser):
        parser.add_argument(
            "url_or_barcode",
            help="Barcode number or product URL (e.g. https://www.barcodelookup.com/030111179302)",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit the normalized product payload as JSON",
        )

    def handle(self, *args, **options):
        url_or_barcode = options["url_or_barcode"]
        try:
            product = lookup_product(url_or_barcode)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        except ProductLookupError as exc:
            raise CommandError(str(exc)) from exc

        if options["json"]:
            self.stdout.write(json.dumps(product.to_dict(), indent=2))
            return

        self.stdout.write(self.style.SUCCESS(f"Found via {product.source}"))
        self.stdout.write(f"Barcode:      {product.barcode}")
        self.stdout.write(f"Name:         {product.name}")
        self.stdout.write(f"Brand:        {product.brand}")
        self.stdout.write(f"Type:         {product.product_type}")
        self.stdout.write(f"Weight (lb):  {product.weight}")
        self.stdout.write(f"Can size:     {product.can_size}")
        self.stdout.write(f"Price:        {product.estimated_price}")
        self.stdout.write(f"Life stage:   {product.life_stages}")
        self.stdout.write(f"Proteins:     {', '.join(product.proteins) or '-'}")
        self.stdout.write(f"Grain-free:   {product.grain_free}")
        self.stdout.write(f"Category:     {product.category}")
        self.stdout.write(f"Source URL:   {product.source_url}")
        if product.image_urls:
            self.stdout.write(f"Image:        {product.image_urls[0]}")
        if product.description:
            self.stdout.write("")
            self.stdout.write(product.description[:500])
