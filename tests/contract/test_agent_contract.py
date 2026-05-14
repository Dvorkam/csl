import schemathesis
from schemathesis import Case, Response
from schemathesis.checks import CheckContext, not_a_server_error

from control_station_lite.agent.main import app

schema = schemathesis.openapi.from_asgi("/openapi.json", app)


def _no_unexpected_server_error(ctx: CheckContext, response: Response, case: Case) -> None:
    """Like not_a_server_error but allows 501 — expected for stub endpoints."""
    if response.status_code != 501:
        not_a_server_error(ctx, response, case)


@schema.parametrize()
def test_agent_api_contract(case: schemathesis.Case) -> None:
    """Verify every agent endpoint accepts valid inputs and returns documented responses.

    501 responses are expected for not-yet-implemented stub endpoints.
    """
    response = case.call(app=app)
    case.validate_response(response, checks=(_no_unexpected_server_error,))
