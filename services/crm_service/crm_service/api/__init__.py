from fastapi import APIRouter

from crm_service.api import catalog, contacts, exports, imports, internal, leads, segments, suppressions

# `exports` (/api/v1/leads/exports) must be registered before `leads` (/api/v1/leads/{lead_id}).
routers: list[APIRouter] = [
    exports.router,
    leads.router,
    contacts.router,
    catalog.router,
    suppressions.router,
    segments.router,
    imports.router,
    internal.router,
]
