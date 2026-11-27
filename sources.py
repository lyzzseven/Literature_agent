"""Scholarly metadata search and retrieval of public full-text PDFs."""
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


def doi_id(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", unquote(value or ""), flags=re.I)
    return value.strip().rstrip(".,;").lower()


def json_request(url, *, payload=None, headers=None, timeout=15):
    request_headers = {"Accept": "application/json", "User-Agent": "ResearchLiteratureAgent/0.1"}
    request_headers.update(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    authorization = request_headers.pop("Authorization", None)
    request = Request(url, data=data, headers=request_headers)
    if authorization:
        request.add_unredirected_header("Authorization", authorization)
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def publication_record(title, *, doi="", url="", year=None, authors=None, venue="", source="", oa=None):
    doi = doi_id(doi)
    return {
        "title": title, "doi": doi, "url": url or ("https://doi.org/" + doi if doi else ""),
        "year": year, "authors": authors or [], "venue": venue,
        "search_sources": [source] if source else [], "open_access": oa or {},
        "pdf": "", "read_status": "metadata_only",
    }


def crossref(query, limit, since, until):
    filters = {}
    if since:
        filters["from-pub-date"] = f"{since}-01-01"
    if until:
        filters["until-pub-date"] = f"{until}-12-31"
    params = {"query.bibliographic": query, "rows": limit,
              "select": "DOI,title,author,published,container-title,URL"}
    if filters:
        params["filter"] = ",".join(f"{k}:{v}" for k, v in filters.items())
    items = json_request("https://api.crossref.org/works?" + urlencode(params))["message"]["items"]
    return [
        publication_record(
            (r.get("title") or [""])[0], doi=r.get("DOI", ""), url=r.get("URL", ""),
            year=r.get("published", {}).get("date-parts", [[None]])[0][0],
            authors=[a.get("name") or " ".join(filter(None, [a.get("given"), a.get("family")]))
                     for a in r.get("author", [])],
            venue=(r.get("container-title") or [""])[0], source="Crossref",
        )
        for r in items if r.get("title")
    ]


def openalex_api_key():
    key = os.getenv("OPENALEX_API_KEY", "").strip()
    if key:
        return key
    try:
        lines = Path(__file__).with_name(".env").read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return ""
    for line in lines:
        name, _, value = line.partition("=")
        if name.strip() == "OPENALEX_API_KEY":
            return value.strip().strip("\"'")
    return ""


def openalex(query, limit, since, until):
    params = {"search": query, "per_page": limit,
              "select": "id,doi,title,publication_year,authorships,primary_location,best_oa_location,open_access"}
    filters = []
    if since:
        filters.append(f"from_publication_date:{since}-01-01")
    if until:
        filters.append(f"to_publication_date:{until}-12-31")
    if filters:
        params["filter"] = ",".join(filters)
    key = openalex_api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else None
    items = json_request("https://api.openalex.org/works?" + urlencode(params), headers=headers)["results"]
    results = []
    for r in items:
        if not r.get("title"):
            continue
        location = r.get("best_oa_location") or {}
        primary = r.get("primary_location") or {}
        oa = {}
        if location.get("is_oa") and location.get("pdf_url"):
            oa = {"pdf_url": location["pdf_url"], "license": location.get("license"),
                  "version": location.get("version"), "provider": "OpenAlex"}
        results.append(publication_record(
            r["title"], doi=r.get("doi") or "", url=primary.get("landing_page_url") or r["id"],
            year=r.get("publication_year"),
            authors=[(a.get("author") or {}).get("display_name", "") for a in r.get("authorships", [])],
            venue=(primary.get("source") or {}).get("display_name", ""), source="OpenAlex", oa=oa,
        ))
    return results


def title_key(text):
    return re.sub(r"[\W_]+", "", text.casefold())


def merge_publication(records, record):
    for existing in records:
        same_doi = record["doi"] and record["doi"] == existing["doi"]
        same_title = (
            bool(record["title"] and existing["title"])
            and title_key(record["title"]) == title_key(existing["title"])
            and not (record["doi"] and existing["doi"] and record["doi"] != existing["doi"])
        )
        if same_doi or same_title:
            for k in ("doi", "url", "year", "authors", "venue", "open_access", "pdf"):
                if not existing[k]:
                    existing[k] = record[k]
            existing["search_sources"] = list(dict.fromkeys(existing["search_sources"] + record["search_sources"]))
            return existing
    records.append(record)
    return record


def search(queries, providers, limit, since, until, warn, *, request_log=None):
    from concurrent.futures import ThreadPoolExecutor
    if not queries or not providers:
        return []
    functions = {"crossref": crossref, "openalex": openalex}
    records = []
    jobs = [(query, provider) for query in queries for provider in providers]
    with ThreadPoolExecutor(max_workers=min(4, len(jobs))) as executor:
        tasks = [executor.submit(functions[p], q, limit, since, until) for q, p in jobs]
        for (query, provider), task in zip(jobs, tasks):
            entry = {"provider": provider, "query": query}
            try:
                hits = task.result()
                for record in hits:
                    merge_publication(records, record)
                entry.update(status="ok", hits=len(hits))
            except (OSError, ValueError, KeyError, TypeError) as error:
                reason = brief_error(error)
                if provider == "openalex" and isinstance(error, HTTPError):
                    if error.code in (401, 403):
                        reason += "，请检查 OpenAlex 密钥及激活状态"
                    elif error.code == 429:
                        reason += "，已达到 OpenAlex 请求或用量限制"
                warn(f"{provider} 检索失败（{query}）：{reason}")
                entry.update(status="error", error=reason)
            if request_log is not None:
                request_log.append(entry)
    return records


def brief_error(error):
    if isinstance(error, HTTPError):
        return f"HTTP {error.code}"
    if isinstance(error, URLError):
        return str(error.reason)
    return str(error)


def unpaywall(doi, email):
    endpoint = "https://api.unpaywall.org/v2/" + quote(doi, safe="/") + "?" + urlencode({"email": email})
    result = json_request(endpoint)
    location = result.get("best_oa_location") or {}
    if result.get("is_oa") and location.get("url_for_pdf"):
        return {"pdf_url": location["url_for_pdf"], "license": location.get("license"),
                "version": location.get("version"), "provider": "Unpaywall"}
    return {}


def check_public_https(url):
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme != "https" or not host or parts.username or parts.password or parts.port not in (443, None):
        raise ValueError("PDF 地址须为公开 HTTPS")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if any(not ipaddress.ip_address(address[4][0].split("%")[0]).is_global for address in addresses):
        raise ValueError("PDF 地址指向非公开网络")


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_pdf(url, destination, max_bytes=40_000_000):
    check_public_https(url)
    req = Request(url, headers={"Accept": "application/pdf", "User-Agent": "ResearchLiteratureAgent/0.1"})
    with build_opener(PublicRedirects()).open(req, timeout=20) as response:
        data = response.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("PDF 超过 40 MB")
    if not data.startswith(b"%PDF-"):
        raise ValueError("链接返回的内容不是 PDF")
    destination.write_bytes(data)
