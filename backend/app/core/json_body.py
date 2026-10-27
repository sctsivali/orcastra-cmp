"""Read a route's JSON body in the route, so a 422 never echoes what the caller sent.

important: FastAPI's own 422 for a declared body lists each failing value back to the caller, and
it parses that body before any dependency (feature flag, login) runs. A secret one character too
long would come back in the response and in any proxy log on the way. Routes that accept secrets
read their body with this instead.
"""

from typing import Any, Dict, Type, TypeVar

from fastapi import HTTPException, Request
from pydantic import BaseModel, ValidationError

Body = TypeVar("Body", bound=BaseModel)


async def read_json_body(request: Request, model: Type[Body]) -> Body:
    try:
        return model.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=[
            {"type": error["type"], "loc": ["body", *error["loc"]], "msg": error["msg"]}
            for error in exc.errors(include_url=False, include_context=False, include_input=False)
        ]) from None


def body_spec(model: Type[BaseModel]) -> Dict[str, Any]:
    """openapi_extra for a route that reads its body with read_json_body, so the spec still shows it."""
    return {"requestBody": {"required": True, "content": {"application/json": {"schema": model.model_json_schema()}}}}
