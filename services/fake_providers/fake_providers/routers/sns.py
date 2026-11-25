"""DEV ONLY: serves the simulator's SNS signing certificate."""

from fastapi import APIRouter, Response

from fake_providers.sns import CERT_PATH, certificate_pem

router = APIRouter(tags=["fake-sns"])


@router.get(CERT_PATH, include_in_schema=False)
async def cert() -> Response:
    return Response(certificate_pem(), media_type="application/x-pem-file")
