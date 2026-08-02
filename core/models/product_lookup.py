import json
import re
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone




def normalize_key(key):
    """Lowercase a JSON key and strip non-alphanumerics so Title / product_name match."""
    return re.sub(r"[^a-z0-9]", "", str(key).lower())

def walk_key_values(data, path=""):
    """Yield (dotted_path, key, value) for every object entry in nested JSON."""
    if isinstance(data, dict):
        for key, value in data.items():
            child_path = f"{path}.{key}" if path else str(key)
            yield child_path, key, value
            yield from walk_key_values(value, child_path)
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
                yield from walk_key_values(item, f"{path}[{index}]")

def collect_text_blobs(data):
    """Flatten every string-ish leaf in the JSON into one searchable corpus."""
    blobs = []
    if isinstance(data, dict):
        for key, value in data.items():
            blobs.append(str(key))
            blobs.extend(collect_text_blobs(value))
    elif isinstance(data, list):
        for item in data:
            blobs.extend(collect_text_blobs(item))
    elif data is None:
        pass
    else:
        blobs.append(str(data))
    return blobs




class ProductUpdater:
    """Pulls external product details and stores them on a Product.

    Field mapping lives on the Product hierarchy (`_apply_updater_data`); this
    class only talks to providers and delegates application.
    """

    def __init__(self, product):
        self.product = product.specific

    def fetch_lookup_data(self):
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
        self.product.updater_data = self.fetch_lookup_data()
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
        corpus = " ".join(collect_text_blobs(data)).lower()
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
        preferred = [normalize_key(k) for k in preferred_keys]
        # normalized_key -> (path_depth, value)
        found = {}
        for path, key, value in walk_key_values(data):
            if ProductUpdater._is_blank(value) or isinstance(value, (dict, list)):
                continue
            norm = normalize_key(key)
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
    def _is_blank(value):
        if value is None:
            return True
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

    def fetch_lookup_data(self):
        if self.TRIAL:
            url = self.API_URL_TRIAL
        else:
            url = self.API_URL
        params = urlencode({"upc": self.product.barcode})
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
                f"UPCitemdb lookup failed for barcode {self.product.barcode}: "
                f"HTTP {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"UPCitemdb lookup failed for barcode {self.product.barcode}: {exc.reason}"
            ) from exc

        self.lookup_data = json.loads(body)
        if "items" in self.lookup_data and len(self.lookup_data["items"]) > 0:
            return self.lookup_data["items"][0]
        raise RuntimeError(
            f"UPCitemdb lookup failed for barcode {self.product.barcode}: No items found"
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

    def fetch_lookup_data(self):
        if not self.api_key:
            raise RuntimeError(
                "Go-UPC lookup requires an API key. Set GO_UPC_API_KEY in the "
                "environment, or request a free trial key at "
                "https://go-upc.com/plans/api/trial"
            )

        url = self.API_URL_TEMPLATE.format(code=self.product.barcode)
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
                f"Go-UPC lookup failed for barcode {self.product.barcode}: "
                f"HTTP {exc.code} {detail}"
            ) from exc
        except URLError as exc:
            raise RuntimeError(
                f"Go-UPC lookup failed for barcode {self.product.barcode}: {exc.reason}"
            ) from exc

        self.lookup_data = json.loads(body)
        return self.lookup_data


# Tried in order by Product.update_from_lookup until one stores data successfully.
PRODUCT_UPDATERS = (
    UPC_Item_DB_Product_Updater,
    GO_UPC_Product_Updater,
)
