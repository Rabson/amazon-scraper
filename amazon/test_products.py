"""Offline tests for how the product endpoints classify the page Amazon
served — no network (`fetch.page` is replaced by captured pages).

Captured 2026-11-13 from amazon.in (home egress):
  in_dp_B0F2FR3QZS_hollow   HTTP 200, ~320 KB: the storefront shell (nav,
                            footer, the product name in <title>) with an
                            EMPTY #dp-container — no #productTitle, no
                            buybox. Served for a real, listed laptop on
                            about 1 in 3 fresh sessions; the same ASIN
                            renders in full on the next try.
  in_dp_B000000000_404      Amazon's real answer for an ASIN that does not
                            exist: HTTP 404, 2 KB "Page Not Found" — the
                            fetch layer raises AmazonNotFound on the status
                            before any parser runs (every try, 8 of 8).

    python -m pytest amazon/test_products.py -q
"""
import gzip
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from amazon import fetch, products  # noqa: E402

FX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def raw(name):
    with gzip.open(os.path.join(FX, name + ".gz"), "rt", encoding="utf-8") as f:
        return f.read()


HOLLOW = raw("in_dp_B0F2FR3QZS_hollow.html")
# a full product page without inline reviews (a "T1" session)
FULL_NO_REVIEWS = raw("in_dp_B0GWQHQB4T.html").replace('data-hook="review"', 'data-hook="other"')


@pytest.fixture
def served(monkeypatch):
    """Queue the pages fetch.page hands out, in order (the last repeats)."""
    pages = []

    def page(country, path, params=None, **kwargs):
        return pages.pop(0) if len(pages) > 1 else pages[0]
    monkeypatch.setattr(fetch, "page", page)
    monkeypatch.setattr(fetch, "drop_session", lambda *a, **k: None)
    return pages


@pytest.mark.parametrize("call", [
    lambda: products.details("B0F2FR3QZS", "IN"),
    lambda: products.variations("B0F2FR3QZS", "IN"),
    lambda: products.reviews("B0F2FR3QZS", "IN"),
])
def test_hollow_page_is_blocked_not_missing(served, call):
    served.append(HOLLOW)
    with pytest.raises(fetch.AmazonBlocked) as err:
        call()
    assert not isinstance(err.value, fetch.AmazonNotFound)


def test_bulk_reports_hollow_page_as_blocked(served):
    served.append(HOLLOW)
    rows = products.bulk(["B0F2FR3QZS"], "IN")["products"]
    assert "blocked" in rows[0]["error"] and "not found" not in rows[0]["error"]


def test_reviews_refetch_keeps_the_full_page_when_the_retry_is_hollow(served):
    # the first page has no inline reviews, so details re-fetches; a hollow
    # re-fetch must not throw away the product it already has
    served.extend([FULL_NO_REVIEWS, HOLLOW])
    data = products.details("B0GWQHQB4T", "IN")
    assert data["title"]


def test_missing_asin_stays_not_found(monkeypatch):
    # Amazon's real "no such ASIN" answer is HTTP 404: the fetch layer
    # raises AmazonNotFound before any page reaches the parser
    assert "Page Not Found" in raw("in_dp_B000000000_404.html")

    def page(*a, **k):
        raise fetch.AmazonNotFound("product B000000000: not found")
    monkeypatch.setattr(fetch, "page", page)
    with pytest.raises(fetch.AmazonNotFound):
        products.details("B000000000", "IN")


def test_reviews_refetch_drops_the_session_after_a_hollow_page(served, monkeypatch):
    dropped = []
    monkeypatch.setattr(fetch, "drop_session", lambda *a, **k: dropped.append(a))
    served.extend([HOLLOW, FULL_NO_REVIEWS])
    data = products.details("B0GWQHQB4T", "IN")
    assert data["title"] and dropped
