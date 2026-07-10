"""Encabezados HTTP compartidos para las llamadas salientes a Sicar X, evitando
duplicar los mismos literales (User-Agent, Origin, Referer) en cada servicio."""

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
USER_AGENT_ADMIN_APP = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36 Edg/150.0.0.0"
)

STOREFRONT_ORIGIN = "https://ferreteriacharly.sicarx.shop"
ADMIN_APP_ORIGIN = "https://app.sicarx.com"

ACCEPT_JSON = "application/json, text/plain, */*"


def storefront_headers(token: str, content_type: str = None, branch_id=None) -> dict:
    """Encabezados para llamadas autenticadas contra el storefront"""
    headers = {
        "Accept": ACCEPT_JSON,
        "Authorization": token,
        "Origin": STOREFRONT_ORIGIN,
        "Referer": f"{STOREFRONT_ORIGIN}/",
        "User-Agent": USER_AGENT,
    }
    if content_type:
        headers["Content-Type"] = content_type
    if branch_id is not None:
        headers["x-branch-id"] = str(branch_id)
    return headers


def admin_app_headers(token: str) -> dict:
    """Encabezados para llamadas administrativas contra app.sicarx.com."""
    return {
        "Accept": ACCEPT_JSON,
        "Authorization": token,
        "Content-Type": "application/json;charset=UTF-8",
        "Origin": ADMIN_APP_ORIGIN,
        "Referer": f"{ADMIN_APP_ORIGIN}/",
        "User-Agent": USER_AGENT_ADMIN_APP,
    }


def graphql_bearer_headers(token: str) -> dict:
    """Encabezados para llamadas GraphQL administrativas sin Origin/Referer (api.sicarx.com)."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/graphql",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }


def bearer_json_headers(token: str) -> dict:
    """Encabezados para llamadas REST administrativas con Bearer token (api.sicarx.com)."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }