import json
import mimetypes
import re
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone


# Cap remote product photos so a bad CDN response cannot fill disk/memory.
MAX_PHOTO_BYTES = 5 * 1024 * 1024
PHOTO_DOWNLOAD_TIMEOUT_SECONDS = 15
FX_DOWNLOAD_TIMEOUT_SECONDS = 10

# Estimated prices are stored in CAD. UPCitemdb's empty currency means USD.
TARGET_PRICE_CURRENCY = "CAD"
DEFAULT_SOURCE_CURRENCY = "USD"

# Bank of Canada Valet series: CAD per 1 unit of foreign currency.
# Matches the currencies UPCitemdb documents on `currency`.
BOC_FX_SERIES_TO_CAD = {
    "USD": "FXUSDCAD",
    "EUR": "FXEURCAD",
    "GBP": "FXGBPCAD",
    "SEK": "FXSEKCAD",
}
BOC_VALET_OBSERVATIONS_URL = (
    "https://www.bankofcanada.ca/valet/observations/{series}/json?recent=1"
)

# In-process cache: currency code -> (calendar day fetched, CAD-per-unit rate).
_fx_rate_cache = {}


class ProductUpdater:
    """Pulls external product details and stores them on a Product.

    Field mapping lives on the Product hierarchy (`_apply_updater_data`); this
    class only talks to providers and delegates application.
    """

    def __init__(self, product):
        self.product = product.specific

    def fetch_lookup_data(self, barcode):
        """Retrieve provider JSON for this product and store it on lookup_data."""
        raise NotImplementedError

    def update_product(self, save=True):
        """Fetch provider data, map it onto the product, and optionally persist.

        Pass save=False when filling an unsaved create draft. The once-per-day
        guard only applies when persisting an update to an existing product.
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

    @staticmethod
    def _normalize_key(key):
        """Lowercase a JSON key and strip non-alphanumerics so Title / product_name match."""
        return re.sub(r"[^a-z0-9]", "", str(key).lower())

    @staticmethod
    def _walk_key_values(data, path=""):
        """Yield (dotted_path, key, value) for every object entry in nested JSON."""
        if isinstance(data, dict):
            for key, value in data.items():
                child_path = f"{path}.{key}" if path else str(key)
                yield child_path, key, value
                yield from ProductUpdater._walk_key_values(value, child_path)
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
                    yield child_path, item[0], item[1]
                else:
                    yield from ProductUpdater._walk_key_values(
                        item, f"{path}[{index}]"
                    )

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


    @staticmethod
    def _find_by_preferred_keys(data, preferred_keys):
        """Return the first value whose key matches the preferred list, in preference order.

        Scans the whole tree once, then picks the best preferred key that appeared
        anywhere (shallow paths win ties so top-level title beats nested noise).
        """
        preferred = [ProductUpdater._normalize_key(k) for k in preferred_keys]
        # normalized_key -> (path_depth, value)
        found = {}
        for path, key, value in ProductUpdater._walk_key_values(data):
            if ProductUpdater._is_blank(value) or isinstance(value, (dict, list)):
                continue
            norm = ProductUpdater._normalize_key(key)
            if norm not in preferred:
                continue
            depth = path.count(".") + path.count("[")
            prior = found.get(norm)
            if prior is None or depth < prior[0]:
                found[norm] = (depth, value)

        for norm in preferred:
            if norm in found:
                return found[norm][1]
        return None

    @staticmethod
    def _find_price_and_currency(data, preferred_price_keys, currency_keys=("currency",)):
        """Return ``(price_value, currency_code)`` for the best preferred price key.

        Currency is taken from a sibling key on the same object when present
        (UPCitemdb puts ``currency`` next to ``lowest_recorded_price`` / offer
        prices). Missing or blank currency is left as ``None`` so callers can
        apply the UPCitemdb default (USD).
        """
        preferred = [ProductUpdater._normalize_key(k) for k in preferred_price_keys]
        currency_norms = {ProductUpdater._normalize_key(k) for k in currency_keys}
        # normalized_key -> (depth, value, currency_or_none)
        found = {}

        def walk(node, depth=0):
            if isinstance(node, dict):
                local_currency = None
                for key, value in node.items():
                    if ProductUpdater._normalize_key(key) not in currency_norms:
                        continue
                    if ProductUpdater._is_blank(value) or isinstance(value, (dict, list)):
                        continue
                    text = str(value).strip()
                    if text:
                        local_currency = text
                        break
                for key, value in node.items():
                    if ProductUpdater._is_blank(value) or isinstance(value, (dict, list)):
                        continue
                    norm = ProductUpdater._normalize_key(key)
                    if norm not in preferred:
                        continue
                    prior = found.get(norm)
                    if prior is None or depth < prior[0]:
                        found[norm] = (depth, value, local_currency)
                for value in node.values():
                    if isinstance(value, (dict, list)):
                        walk(value, depth + 1)
            elif isinstance(node, list):
                for item in node:
                    walk(item, depth + 1)

        walk(data)
        for norm in preferred:
            if norm in found:
                _, value, currency = found[norm]
                return value, currency
        return None, None

    @classmethod
    def _fetch_boc_cad_rate(cls, currency):
        """Return CAD-per-unit rate for ``currency`` from Bank of Canada Valet."""
        code = (currency or "").strip().upper()
        if not code or code == TARGET_PRICE_CURRENCY:
            return Decimal("1")

        series = BOC_FX_SERIES_TO_CAD.get(code)
        if not series:
            raise RuntimeError(
                f"No Bank of Canada FX series mapped for currency {code!r}"
            )

        today = date.today()
        cached = _fx_rate_cache.get(code)
        if cached and cached[0] == today:
            return cached[1]

        url = BOC_VALET_OBSERVATIONS_URL.format(series=series)
        headers = {
            "Accept": "application/json",
            "User-Agent": "HHInventory-ProductUpdater/1.0",
        }
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=FX_DOWNLOAD_TIMEOUT_SECONDS) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Bank of Canada FX lookup failed for {code}: "
                f"HTTP {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"Bank of Canada FX lookup failed for {code}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(
                f"Bank of Canada FX lookup timed out for {code}"
            ) from exc

        payload = json.loads(body)
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

        _fx_rate_cache[code] = (today, rate)
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

        code = (currency or "").strip().upper() or DEFAULT_SOURCE_CURRENCY
        if code == TARGET_PRICE_CURRENCY:
            return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)

        rate = cls._fetch_boc_cad_rate(code)
        return (amount * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _coerce_photo_url(value):
        """Return the first http(s) URL from a string or list of strings, else None."""
        if isinstance(value, str):
            text = value.strip()
            if text.startswith(("http://", "https://")):
                return text
            return None
        if isinstance(value, (list, tuple)):
            for item in value:
                url = ProductUpdater._coerce_photo_url(item)
                if url:
                    return url
        return None

    @staticmethod
    def _find_photo_url(data, preferred_keys):
        """Like `_find_by_preferred_keys`, but accepts URL strings or lists of URLs.

        UPCitemdb uses `images: ["https://..."]`; Go-UPC uses nested `imageUrl`.
        """
        preferred = [ProductUpdater._normalize_key(k) for k in preferred_keys]
        found = {}
        for path, key, value in ProductUpdater._walk_key_values(data):
            norm = ProductUpdater._normalize_key(key)
            if norm not in preferred:
                continue
            url = ProductUpdater._coerce_photo_url(value)
            if not url:
                continue
            depth = path.count(".") + path.count("[")
            prior = found.get(norm)
            if prior is None or depth < prior[0]:
                found[norm] = (depth, url)

        for norm in preferred:
            if norm in found:
                return found[norm][1]
        return None

    def _photo_filename(self, url, content_type=None):
        """Build a safe upload name from the product barcode and URL / Content-Type."""
        path = urlparse(url).path
        basename = unquote(path.rsplit("/", 1)[-1]) if path else ""
        _, ext = basename.rsplit(".", 1) if "." in basename else ("", "")
        if ext and len(ext) <= 4 and ext.isalnum():
            ext = f".{ext.lower()}"
        else:
            ext = ""
        if not ext and content_type:
            guessed = mimetypes.guess_extension(
                content_type.split(";", 1)[0].strip(), strict=False
            )
            if guessed == ".jpe":
                guessed = ".jpg"
            ext = guessed or ""
        if not ext:
            ext = ".jpg"
        stem = re.sub(r"[^a-zA-Z0-9_-]", "", str(self.product.barcode or "product"))[:40]
        return f"{stem or 'product'}{ext}"

    def download_photo(self, url):
        """Fetch a remote image into a ContentFile, or raise RuntimeError on failure."""
        headers = {
            "Accept": "image/*,*/*;q=0.8",
            "User-Agent": "HHInventory-ProductUpdater/1.0",
        }
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=PHOTO_DOWNLOAD_TIMEOUT_SECONDS) as response:
                content_type = response.headers.get("Content-Type", "")
                # Prefer Content-Length when present; still stream with a hard cap.
                chunks = []
                total = 0
                while True:
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_PHOTO_BYTES:
                        raise RuntimeError(
                            f"Photo at {url} exceeds {MAX_PHOTO_BYTES} byte limit"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
        except HTTPError as exc:
            raise RuntimeError(
                f"Photo download failed for {url}: HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"Photo download failed for {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise RuntimeError(f"Photo download timed out for {url}") from exc

        if not body:
            raise RuntimeError(f"Photo download returned empty body for {url}")

        filename = self._photo_filename(url, content_type=content_type)
        return ContentFile(body, name=filename)

    @staticmethod
    def _is_blank(value):
        if value is None:
            return True
        # Unsaved / empty ImageField files have name=None; treat them as blank.
        # FieldFile.__bool__ is False when name is missing or empty.
        from django.db.models.fields.files import FieldFile

        if isinstance(value, FieldFile):
            return not value
        if isinstance(value, str) and not value.strip():
            return True
        if isinstance(value, (list, tuple, set)) and len(value) == 0:
            return True
        return False

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

        request = Request(url, headers=headers)
        try:
            with urlopen(request) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"UPCitemdb lookup failed for barcode {barcode}: "
                f"HTTP {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"UPCitemdb lookup failed for barcode {barcode}: {exc.reason}"
            ) from exc

        self.lookup_data = json.loads(body)
        if "items" in self.lookup_data and len(self.lookup_data["items"]) > 0:
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

        url = self.API_URL_TEMPLATE.format(code=barcode)
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        request = Request(url, headers=headers)
        try:
            with urlopen(request) as response:
                body = response.read().decode("utf-8")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Go-UPC lookup failed for barcode {barcode}: "
                f"HTTP {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"Go-UPC lookup failed for barcode {barcode}: {exc.reason}"
            ) from exc

        self.lookup_data = json.loads(body)
        return self.lookup_data


# Tried in order by Product.update_from_lookup until one stores data successfully.
PRODUCT_UPDATERS = (
    UPC_Item_DB_Product_Updater,
    GO_UPC_Product_Updater,
)
