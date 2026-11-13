"""/amazon/products/* — product page details, offers (aodAjaxMain), the
variation matrix, top reviews + AI summary, bulk lookups and the
scrape-by-url dispatcher.

Reviews: Amazon renders the product page's top reviews inline only for
sessions in its "C" treatment (about half of new sessions, sticky per
session); the others get an empty reviews medley, and every review ajax
route is login-gated. `_page_with_reviews` therefore drops the thread's
session and re-fetches until a page carries reviews (bounded by
REVIEW_ATTEMPTS), remembering sessions that never will.

Hollow pages: Amazon sometimes answers a product URL with HTTP 200 and the
storefront shell only — the product name in <title>, an empty
#dp-container, no #productTitle (measured on amazon.in 2026-11-13: about
1 in 3 fresh sessions for a listed ASIN). A product that does not exist is
a real HTTP 404, raised as AmazonNotFound by the fetch layer, so a 200
page the parser finds no title in is a block (retry later), never "not
found". A re-fetch for reviews never trades a full page for a hollow one.
"""
from amazon import fetch, parsers as P, refs, sites
from amazon.shared import context, fan_out, paged_result

REVIEW_ATTEMPTS_DETAILS = 2      # fetches spent looking for inline reviews on /products/details
REVIEW_ATTEMPTS_REVIEWS = 5      # … on /products/reviews (reviews are the whole point there)
OFFERS_PER_PAGE = 10
BULK_MAX = 10
CONDITIONS = {"all": None, "new": "new", "used": "usedAcceptable", "used_like_new": "usedLikeNew",
              "used_very_good": "usedVeryGood", "used_good": "usedGood", "used_acceptable": "usedAcceptable",
              "collectible": "collectible"}


def _has_product(html):
    return 'id="productTitle"' in html or 'id="title"' in html


def _product_page(asin, site, locale, postal_code=None, want_reviews=False, attempts=1):
    """The dp page HTML, re-fetched on a fresh session while it lacks
    inline reviews (when wanted). A hollow page rotates the session too; a
    hollow re-fetch keeps the page before it."""
    html = None
    for attempt in range(max(attempts, 1)):
        fetched = fetch.page(site["country"], f"/dp/{asin}", {"th": "1", "psc": "1"}, referer=f"{site['base']}/s?k={asin}",
                             postal_code=postal_code, language=locale, label=f"product {asin}")
        if html is None or _has_product(fetched) or not _has_product(html):
            html = fetched
        if not want_reviews or 'data-hook="review"' in html:
            break
        if (P.review_treatment(html) == "T1" or not _has_product(html)) and attempt < attempts - 1:
            fetch.drop_session(site["country"], "page", locale)
    return html


def _product_data(asin, html, site):
    """Parsed product page; a page without a product title is a block."""
    data = P.product_page(html, site)
    if not data.get("title"):
        raise fetch.AmazonBlocked(f"product {asin}: Amazon served a page without the product (blocked), retry later")
    return data


def details(product, country=None, language=None, postal_code=None):
    site, locale = context(country, language)
    html = _product_page(product, site, locale, postal_code, want_reviews=True, attempts=REVIEW_ATTEMPTS_DETAILS)
    data = _product_data(product, html, site)
    data["asin"] = data.get("asin") or product
    data["link"] = P.product_link(site, data["asin"])
    return data


def offers(product, country=None, language=None, page=1, condition="all", prime_only=None, postal_code=None):
    site, locale = context(country, language)
    params = {"asin": product, "pc": "dp", "experienceId": "aodAjaxMain"}
    filters = {}
    token = CONDITIONS.get(condition or "all")
    if token:
        filters[token] = True
    if prime_only:
        filters["primeEligible"] = True
    if filters:
        import json
        params["filters"] = json.dumps({"all": True, **filters}, separators=(",", ":"))
    if page > 1:
        params["pageno"] = str(page)
        params["isonlyrenderofferlist"] = "true"
    html = fetch.ajax(site["country"], "/gp/product/ajax/aodAjaxMain", params, referer=P.product_link(site, product),
                      locate=True, postal_code=postal_code, language=locale, label=f"offers {product}")
    data = P.offers_page(html, site)
    rows = data["offers"]
    total = data.get("other_offers_count")
    pinned = [o for o in rows if o["is_pinned"]]
    total_count = (total + len(pinned)) if total is not None else None
    out = paged_result("offers", rows, page, OFFERS_PER_PAGE, total=total_count if page == 1 else None,
                       has_more=data.get("has_more") if page == 1 else len([o for o in rows if not o["is_pinned"]]) >= OFFERS_PER_PAGE,
                       asin=product, title=data.get("title"), rating=data.get("rating"), condition=condition or "all",
                       country=site["country"], domain=site["host"])
    if total_count is not None:
        out["pagination"]["total_count"] = total_count
        out["pagination"]["total_pages"] = max((total + OFFERS_PER_PAGE - 1) // OFFERS_PER_PAGE, 1) if total else 1
    return out


def variations(product, country=None, language=None):
    site, locale = context(country, language)
    html = _product_page(product, site, locale)
    data = _product_data(product, html, site)
    block = data.get("variations") or {"parent_asin": None, "current_asin": data.get("asin"), "dimensions": [], "products": [], "count": 0}
    return {"asin": data.get("asin") or product, "title": data.get("title"), "link": data.get("link"), **block,
            "country": site["country"], "domain": site["host"]}


def reviews(product, country=None, language=None):
    site, locale = context(country, language)
    html = _product_page(product, site, locale, want_reviews=True, attempts=REVIEW_ATTEMPTS_REVIEWS)
    data = _product_data(product, html, site)
    rows = data.get("top_reviews") or []
    return {
        "asin": data.get("asin") or product,
        "title": data.get("title"),
        "link": data.get("link"),
        "rating": data.get("rating"),
        "customers_say": data.get("customers_say"),
        "reviews": rows,
        "count": len(rows),
        "is_partial": not rows and P.review_treatment(html) == "T1",
        "note": "Amazon shows the top reviews on the product page; full review pagination requires a signed-in customer.",
        "country": site["country"],
        "domain": site["host"],
    }


def _summary(data):
    images = data.get("images") or []
    return {
        "asin": data.get("asin"), "title": data.get("title"), "link": data.get("link"), "brand": (data.get("brand") or {}).get("name"),
        "price": data.get("price"), "availability": data.get("availability"), "buybox": data.get("buybox"),
        "is_prime": data.get("is_prime"), "rating": {k: v for k, v in (data.get("rating") or {}).items() if k != "histogram"} or None,
        "bought_past_month": data.get("bought_past_month"), "badges": data.get("badges"), "deal_badge": data.get("deal_badge"),
        "coupon": data.get("coupon"), "image": images[0]["link"] if images else None, "images_count": len(images),
        "best_sellers_rank": data.get("best_sellers_rank"), "categories": [c["name"] for c in data.get("categories") or []],
        "variations_count": (data.get("variations") or {}).get("count"), "parent_asin": (data.get("variations") or {}).get("parent_asin"),
    }


def bulk(products, country=None, language=None, postal_code=None):
    site, locale = context(country, language)
    asins = list(dict.fromkeys(products))[:BULK_MAX]

    def one(asin):
        html = _product_page(asin, site, locale, postal_code)
        data = _product_data(asin, html, site)
        data["asin"] = data.get("asin") or asin
        data["link"] = P.product_link(site, data["asin"])
        return _summary(data)
    results = fan_out(one, asins, workers=4)
    rows = []
    for asin, result in zip(asins, results):
        if isinstance(result, Exception):
            rows.append({"asin": asin, "error": str(result) or type(result).__name__})
        else:
            rows.append(result)
    return {"products": rows, "count": len(rows), "country": site["country"], "domain": site["host"]}


def scrape_url(url, language=None, postal_code=None):
    """Any Amazon link -> the matching endpoint's result (+ `page_type`)."""
    from amazon import influencers, rankings, search, sellers
    country = refs.country_of(url) or sites.DEFAULT_COUNTRY
    kind = refs.page_type(url)
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if kind == "product":
        out = details(refs.resolve_product(url), country, language, postal_code)
    elif kind == "seller":
        out = sellers.profile(refs.resolve_seller(url), country, language)
    elif kind == "influencer":
        out = influencers.profile(refs.resolve_influencer(url), country, language)
    elif kind == "influencer_post":
        post = refs.resolve_post(url)
        out = influencers.post_products(post, country, language)
    elif kind == "bestsellers":
        path = parsed.path.lower()
        list_type = "new_releases" if "new-releases" in path else "movers_and_shakers" if "movers" in path else \
            "most_wished_for" if "wished" in path else "most_gifted" if "gifted" in path else "best_sellers"
        out = rankings.bestsellers(refs.resolve_bestseller_category(url), list_type=list_type, country=country, language=language,
                                  page=int((query.get("pg") or ["1"])[0]) if (query.get("pg") or ["1"])[0].isdigit() else 1)
    elif kind == "deals":
        out = rankings.deals(country=country, language=language, collection=(query.get("bubble-id") or [None])[0])
    elif kind == "search":
        keyword = (query.get("k") or query.get("keywords") or [None])[0]
        me = (query.get("me") or [None])[0]
        node = None
        try:
            node = refs.resolve_node(url)
        except ValueError:
            pass
        page = int((query.get("page") or ["1"])[0]) if (query.get("page") or ["1"])[0].isdigit() else 1
        if me:
            out = sellers.products(me, country, language, page=page, query=keyword)
        elif keyword:
            out = search.search(keyword, country, language, page=page, category=(query.get("i") or [None])[0], node=node)
        elif node:
            out = search.category_products(node, country, language, page=page)
        else:
            raise fetch.AmazonBadRequest("search link carries no keyword, seller or browse node")
    else:
        raise fetch.AmazonBadRequest("unsupported Amazon link: use a product, search, category, best sellers, deals, seller or influencer page")
    return {"page_type": kind, "url": url, "country": country, **out}
