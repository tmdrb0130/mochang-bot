"""검색 주제·자료 종류·출처가 고르게 선택되도록 후보 순서를 정한다."""
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def source_key(url: str) -> str:
    """추적 파라미터는 제거하고 자료 식별자는 보존한다."""
    try:
        p = urlsplit((url or "").strip())
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
        return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urlencode(sorted(query)), ""))
    except ValueError:
        return (url or "").split("#")[0].rstrip("/")


def diverse_results(rows: list[dict], domain_limit: int = 2,
                    initial: list[dict] | None = None) -> list[dict]:
    """관련 후보 안에서 다양성을 우선하고 동률이면 기존 순위를 따른다."""
    from .research import domain
    pending, seen = [], set()
    hosts, kinds, queries = {}, {}, set()

    def remember(row):
        host = domain(row.get("url", ""))
        hosts[host] = hosts.get(host, 0) + 1
        kind = row.get("source_type") or "web"
        kinds[kind] = kinds.get(kind, 0) + 1
        if row.get("query"):
            queries.add(row["query"])

    for row in initial or []:
        seen.add(source_key(row.get("url", "")))
        remember(row)
    for row in rows:
        key = source_key(row.get("url", ""))
        if key and key not in seen:
            seen.add(key)
            pending.append(row)
    out = []
    while pending:
        eligible = [r for r in pending if r.get("no_fetch") or domain_limit <= 0
                    or hosts.get(domain(r.get("url", "")), 0) < domain_limit]
        if not eligible:
            break
        def score(r):
            host = domain(r.get("url", ""))
            kind = r.get("source_type") or "web"
            new_query = bool(r.get("query") and r["query"] not in queries)
            return (int(new_query) + int(kind not in kinds), -hosts.get(host, 0), -kinds.get(kind, 0))
        row = max(eligible, key=score)
        pending.remove(row)
        out.append(row)
        remember(row)
    return out
