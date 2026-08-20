import json
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import ContentFile
from django.db.models.fields.files import FieldFile
from django.utils import timezone


class ProductUpdater:
    """Pulls external product details and stores them on a Product.

    Field mapping lives on the Product hierarchy (`_apply_updater_data`); this
    class only talks to providers and delegates application.
    """

    # currency code -> (calendar day fetched, CAD-per-unit rate)
    _fx_rate_cache = {}

    def __init__(self, product):
        self.product = product.specific

    def fetch_lookup_data(self, barcode):
        """Retrieve provider JSON for this product and store it on lookup_data."""
        raise NotImplementedError

    def update_product(self, save=True, blank_before_apply=False):
        """Fetch provider data, map it onto the product, and optionally persist.

        Pass save=False when filling an unsaved create draft. The once-per-day
        guard only applies when persisting an update to an existing product.

        When blank_before_apply is True, fillable product fields are cleared only
        after this updater successfully returns data, so a failed lookup leaves
        existing details intact.
        """
        if (
            save
            and self.product.pk
            and self.product.updater_last_updated
            and self.product.updater_last_updated > timezone.now() - timedelta(days=1)
        ):
            raise RuntimeError(
                f"Product {self.product.barcode} has already been updated today automatically. "
                "It can be updated manually if needed."
            )
        self.product.updater_data = self.fetch_lookup_data(self.product.barcode)
        self.product.updater_class = self.__class__.__name__
        self.product.updater_last_updated = timezone.now()
        if blank_before_apply:
            self.product.blank_for_lookup_rescan()
        applied = self.apply_updater_data(save=False)
        if save:
            self.product.save()
        return applied

    def apply_updater_data(self, save=True):
        data = self.product.updater_data
        if not data:
            return {}

        applied = {}
        self.product._apply_updater_fields(data, applied, self)
        if save and applied:
            self.product.save()
        return applied

    # --- HTTP ---

    @staticmethod
    def _http_get_body(url, headers, *, timeout=None, error_prefix, include_error_body=True, max_bytes=None, too_large_error=None, timeout_error=None):
        """Return ``(body_bytes, headers)`` from ``url``, or raise RuntimeError."""
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                response_headers = response.headers
                if max_bytes is None:
                    return response.read(), response_headers
                chunks = []
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise RuntimeError(
                            too_large_error
                            or f"{error_prefix}: exceeds {max_bytes} byte limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), response_headers
        except HTTPError as exc:
            if include_error_body:
                detail = exc.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"{error_prefix}: HTTP {exc.code} {detail}"
                ) from exc
            raise RuntimeError(f"{error_prefix}: HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError(f"{error_prefix}: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError(
                timeout_error or f"{error_prefix}: timed out"
            ) from exc

    @classmethod
    def _http_get_json(cls, url, headers, *, timeout=None, error_prefix, timeout_error=None):
        """Fetch ``url`` and parse the body as JSON."""
        body, _headers = cls._http_get_body(
            url, headers, timeout=timeout, error_prefix=error_prefix, timeout_error=timeout_error
        )
        return json.loads(body.decode("utf-8"))

    # --- JSON extraction ---

    @staticmethod
    def _is_blank(value):
        if value is None:
            return True
        # Unsaved / empty ImageField files have name=None; treat them as blank.
        # FieldFile.__bool__ is False when name is missing or empty.
        if isinstance(value, FieldFile):
            return not value
        if isinstance(value, str) and not value.strip():
            return True
        if isinstance(value, (list, tuple, set)) and len(value) == 0:
            return True
        return False

    @staticmethod
    def _normalize_key(key):
        """Lowercase a JSON key and strip non-alphanumerics so Title / product_name match."""
        return re.sub(r"[^a-z0-9]", "", str(key).lower())

    @classmethod
    def _walk_key_values(cls, data, path=""):
        """Yield ``(dotted_path, key, value, parent)`` for every object entry in nested JSON."""
        if isinstance(data, dict):
            for key, value in data.items():
                child_path = f"{path}.{key}" if path else str(key)
                yield child_path, key, value, data
                yield from cls._walk_key_values(value, child_path)
        elif isinstance(data, list):
            for index, item in enumerate(data):
                # Go-UPC specs look like ["Weight", "5 lbs"] — treat as a named pair.
                if (
                    isinstance(item, (list, tuple))
                    and len(item) == 2
                    and isinstance(item[0], str)
                    and not isinstance(item[1], (dict, list))
                ):
                    child_path = f"{path}[{index}]"
                    yield child_path, item[0], item[1], data
                else:
                    yield from cls._walk_key_values(item, f"{path}[{index}]")

    @classmethod
    def _first_text_for_keys(cls, mapping, key_norms):
        """Return the first non-blank scalar on ``mapping`` whose key is in ``key_norms``."""
        if not isinstance(mapping, dict):
            return None
        for key, value in mapping.items():
            if cls._normalize_key(key) not in key_norms:
                continue
            if cls._is_blank(value) or isinstance(value, (dict, list)):
                continue
            text = str(value).strip()
            if text:
                return text
        return None

    @classmethod
    def _coerce_photo_url(cls, value, _parent=None):
        """Return the first http(s) URL from a string or list of strings, else None."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("http://", "https://")):
                return text
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                url = cls._coerce_photo_url(item)
                if url:
                    return url
        return None

    @classmethod
    def _best_preferred_match(cls, data, preferred_keys, coerce=None):
        """Return the shallowest value for the first preferred key that appears.

        Scans the whole tree once, then picks the best preferred key that appeared
        anywhere (shallow paths win ties so top-level title beats nested noise).

        If ``coerce`` is given, it receives ``(value, parent)`` and should return the
        stored value, or ``None`` to skip that entry. Without ``coerce``, blank
        values and nested dict/list nodes are skipped.
        """
        preferred = [cls._normalize_key(k) for k in preferred_keys]
        found = {}
        for path, key, value, parent in cls._walk_key_values(data):
            norm = cls._normalize_key(key)
            if norm not in preferred:
                continue
            if coerce is None:
                if cls._is_blank(value) or isinstance(value, (dict, list)):
                    continue
                stored = value
            else:
                stored = coerce(value, parent)
                if stored is None:
                    continue
            depth = path.count(".") + path.count("[")
            prior = found.get(norm)
            if prior is None or depth < prior[0]:
                found[norm] = (depth, stored)

        for norm in preferred:
            if norm in found:
                return found[norm][1]
        return None

    @classmethod
    def _find_price_and_currency(cls, data, preferred_price_keys, currency_keys=("currency",)):
        """Return ``(price_value, currency_code)`` for the best preferred price key.

        Currency is taken from a sibling key on the same object when present
        (UPCitemdb puts ``currency`` next to ``lowest_recorded_price`` / offer
        prices). Missing or blank currency is left as ``None`` so callers can
        apply the UPCitemdb default (USD).
        """
        currency_norms = {cls._normalize_key(k) for k in currency_keys}

        def coerce(value, parent):
            if cls._is_blank(value) or isinstance(value, (dict, list)):
                return None
            return (value, cls._first_text_for_keys(parent, currency_norms))

        match = cls._best_preferred_match(data, preferred_price_keys, coerce=coerce)
        if match is None:
            return None, None
        return match

    @classmethod
    def _find_photo_url(cls, data, preferred_keys):
        """Like `_best_preferred_match`, but accepts URL strings or lists of URLs.

        UPCitemdb uses `images: ["https://..."]`; Go-UPC uses nested `imageUrl`.
        """
        return cls._best_preferred_match(data, preferred_keys, coerce=cls._coerce_photo_url)

    @staticmethod
    def _collect_text_blobs(data):
        """Flatten every string-ish leaf in the JSON into one searchable corpus."""
        blobs = []
        if isinstance(data, dict):
            for key, value in data.items():
                blobs.append(str(key))
                blobs.extend(ProductUpdater._collect_text_blobs(value))
        elif isinstance(data, list):
            for item in data:
                blobs.extend(ProductUpdater._collect_text_blobs(item))
        elif data is None:
            pass
        else:
            blobs.append(str(data))
        return blobs

    @staticmethod
    def _nearest_token_distance(corpus, center, tokens):
        """Character distance from center to the nearest whole-word token, or None."""
        best = None
        for token in tokens:
            pattern = re.compile(rf"(?<!\w){re.escape(token)}(?!\w)")
            for match in pattern.finditer(corpus):
                distance = abs((match.start() + match.end()) // 2 - center)
                if best is None or distance < best:
                    best = distance
        return best

    @staticmethod
    def _find_choice_labels_in_data(
        data,
        choices,
        aliases=None,
        include_labels=True,
        require_near=None,
        reject_near=None,
    ):
        """Return choice values whose labels (or aliases) appear in the JSON text.

        `aliases` maps a choice value to extra phrases defined on the product class,
        e.g. FISH → ("Fish", "Seafood") beside the rigid label "Fish (Unspecified)".

        Longer phrases are tried first so "Whitefish" / "White Fish" win over "Fish".
        Matching is case-insensitive with word edges.

        Optional light context check (useful for ambiguous size words):
        - `require_near`: match only if one of these tokens is nearer than any reject token
          (and present at all when reject tokens are absent).
        - `reject_near`: discard a match when a reject token is closer than any require token
          (e.g. "Large" next to "Breed" vs "Large" next to "Kibble").
        """
        corpus = " ".join(ProductUpdater._collect_text_blobs(data)).lower()
        aliases = aliases or {}
        require_near = tuple(t.lower() for t in (require_near or ()))
        reject_near = tuple(t.lower() for t in (reject_near or ()))

        phrases = []
        for value, label in choices:
            if include_labels:
                phrases.append((str(value), str(label)))
            for alias in aliases.get(value, ()):
                phrases.append((str(value), str(alias)))

        phrases.sort(key=lambda item: len(item[1]), reverse=True)
        matched = []
        matched_values = set()
        claimed_spans = []

        for value, phrase in phrases:
            if value in matched_values:
                continue
            pattern = re.compile(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)")
            hit = pattern.search(corpus)
            if not hit:
                continue
            start, end = hit.span()
            # Skip if this span already claimed by a longer phrase (e.g. Fish inside Whitefish).
            if any(start < claimed_end and end > claimed_start for claimed_start, claimed_end in claimed_spans):
                continue

            if require_near or reject_near:
                center = (start + end) // 2
                require_distance = ProductUpdater._nearest_token_distance(
                    corpus, center, require_near
                )
                reject_distance = ProductUpdater._nearest_token_distance(
                    corpus, center, reject_near
                )
                # Dog-size wording wins when it sits closer than kibble wording.
                if reject_distance is not None and (
                    require_distance is None or reject_distance < require_distance
                ):
                    continue
                # Bare size words need a nearby kibble cue when require_near is set.
                if require_near and require_distance is None:
                    continue

            claimed_spans.append((start, end))
            matched_values.add(value)
            matched.append(value)

        return matched

    @classmethod
    def _find_best_choice_label_in_data(
        cls,
        data,
        choices,
        aliases=None,
        include_labels=True,
        require_near=None,
        reject_near=None,
    ):
        """Like `_find_choice_labels_in_data`, but for single-select fields.

        Returns the best-matching choice value (longest phrase wins), or None.
        """
        matched = cls._find_choice_labels_in_data(
            data,
            choices,
            aliases,
            include_labels=include_labels,
            require_near=require_near,
            reject_near=reject_near,
        )
        return matched[0] if matched else None

    @classmethod
    def _fetch_boc_cad_rate(cls, currency):
        """Return CAD-per-unit rate for ``currency`` from Bank of Canada Valet."""
        code = (currency or "").strip().upper()
        if not code or code == "CAD":
            return Decimal("1")

        # Bank of Canada Valet series: CAD per 1 unit of foreign currency.
        # Matches the currencies UPCitemdb documents on `currency`.
        series = {
            "USD": "FXUSDCAD",
            "EUR": "FXEURCAD",
            "GBP": "FXGBPCAD",
            "SEK": "FXSEKCAD",
        }.get(code)
        if not series:
            raise RuntimeError(
                f"No Bank of Canada FX series mapped for currency {code!r}"
            )

        today = date.today()
        cached = cls._fx_rate_cache.get(code)
        if cached and cached[0] == today:
            return cached[1]

        payload = cls._http_get_json(
            f"https://www.bankofcanada.ca/valet/observations/{series}/json?recent=1",
            {"Accept": "application/json", "User-Agent": "HHInventory-ProductUpdater/1.0"},
            timeout=10,
            error_prefix=f"Bank of Canada FX lookup failed for {code}",
            timeout_error=f"Bank of Canada FX lookup timed out for {code}",
        )
        observations = payload.get("observations") or []
        if not observations:
            raise RuntimeError(
                f"Bank of Canada FX lookup returned no observations for {code}"
            )
        raw_rate = observations[-1].get(series, {}).get("v")
        try:
            rate = Decimal(str(raw_rate))
        except (InvalidOperation, TypeError) as exc:
            raise RuntimeError(
                f"Bank of Canada FX lookup returned invalid rate for {code}: {raw_rate!r}"
            ) from exc
        if rate <= 0:
            raise RuntimeError(
                f"Bank of Canada FX lookup returned non-positive rate for {code}: {rate}"
            )

        cls._fx_rate_cache[code] = (today, rate)
        return rate

    @classmethod
    def convert_price_to_cad(cls, amount, currency=None):
        """Convert ``amount`` into CAD using Bank of Canada daily rates.

        UPCitemdb documents an empty ``currency`` as USD, so blank/missing codes
        default to USD. Results are rounded to the nearest dollar.
        """
        if amount is None:
            return None
        if not isinstance(amount, Decimal):
            amount = Decimal(str(amount))

        code = (currency or "").strip().upper() or "USD"
        if code == "CAD":
            return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        rate = cls._fetch_boc_cad_rate(code)
        return (amount * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    def download_photo(self, url):
        """Fetch a remote image into a ContentFile, or raise RuntimeError on failure."""
        max_bytes = settings.DATA_UPLOAD_MAX_MEMORY_SIZE
        body, response_headers = self._http_get_body(
            url,
            {"Accept": "image/*,*/*;q=0.8", "User-Agent": "HHInventory-ProductUpdater/1.0"},
            timeout=15,
            error_prefix=f"Photo download failed for {url}",
            include_error_body=False,
            max_bytes=max_bytes,
            too_large_error=f"Photo at {url} exceeds {max_bytes} byte limit",
            timeout_error=f"Photo download timed out for {url}",
        )
        if not body:
            raise RuntimeError(f"Photo download returned empty body for {url}")

        filename = self.product.photo_upload_filename(
            response_headers.get("Content-Type", "")
        )
        return ContentFile(body, name=filename)


class UPC_Item_DB_Product_Updater(ProductUpdater):
    """Looks up a product barcode against the UPCitemdb lookup API."""

    API_URL = "https://api.upcitemdb.com/prod/v1/lookup"
    API_URL_TRIAL = "https://api.upcitemdb.com/prod/trial/lookup"
    TRIAL = True

    def __init__(self, product, user_key=None):
        super().__init__(product)
        if user_key is None:
            user_key = getattr(settings, "UPCITEMDB_USER_KEY", "") or ""
        self.user_key = user_key

    def fetch_lookup_data(self, barcode):
        if self.TRIAL:
            url = self.API_URL_TRIAL
        else:
            url = self.API_URL
        params = urlencode({"upc": barcode})
        url = f"{url}?{params}"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        if not self.TRIAL:
            # Paid /prod/v1 calls require these headers; the free /prod/trial path does not.
            headers["user_key"] = self.user_key
            headers["key_type"] = "3scale"

        self.lookup_data = self._http_get_json(
            url,
            headers,
            error_prefix=f"UPCitemdb lookup failed for barcode {barcode}",
        )
        if "items" in self.lookup_data and len(self.lookup_data["items"]) > 0:
            if "offers" in self.lookup_data["items"][0]:
                del self.lookup_data["items"][0]["offers"]
            return self.lookup_data["items"][0]
        raise RuntimeError(
            f"UPCitemdb lookup failed for barcode {barcode}: No items found"
        )


class GO_UPC_Product_Updater(ProductUpdater):
    """Looks up a product barcode against the Go-UPC product API.

    Go-UPC has no unauthenticated free endpoint. A key is required (paid plan or
    manually approved free trial). Scraping their website is disallowed by their
    terms, so this updater only uses the official JSON API.
    """

    API_URL_TEMPLATE = "https://go-upc.com/api/v1/code/{code}"

    def __init__(self, product, api_key=None):
        super().__init__(product)
        if api_key is None:
            api_key = getattr(settings, "GO_UPC_API_KEY", "") or ""
        self.api_key = api_key

    def fetch_lookup_data(self, barcode):
        if not self.api_key:
            raise RuntimeError(
                "Go-UPC lookup requires an API key. Set GO_UPC_API_KEY in the "
                "environment, or request a free trial key at "
                "https://go-upc.com/plans/api/trial"
            )

        self.lookup_data = self._http_get_json(
            self.API_URL_TEMPLATE.format(code=barcode),
            {
                "Accept": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            error_prefix=f"Go-UPC lookup failed for barcode {barcode}",
        )
        return self.lookup_data


# Tried in order by Product.update_from_lookup until one stores data successfully.
PRODUCT_UPDATERS = (
    UPC_Item_DB_Product_Updater,
    GO_UPC_Product_Updater,
)
