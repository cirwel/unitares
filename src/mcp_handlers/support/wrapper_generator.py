"""
Typed Wrapper Generator for MCP Tools

Generates wrapper functions with explicit typed signatures from JSON schemas.
This allows FastMCP to infer correct schemas without kwargs wrapping.

Benefits:
- Claude.ai sends parameters directly (no kwargs wrapper needed)
- CLI's kwargs wrapping still works (dispatch_tool unwraps)
- Proper IDE/client autocomplete from typed signatures
- Schema metadata (descriptions, enums) preserved for MCP clients
"""

import inspect
import logging
from typing import Annotated, Any, Callable, Optional, Union

from pydantic import ConfigDict, Field
logger = logging.getLogger(__name__)

def create_typed_wrapper(
    tool_name: str,
    input_schema: dict,
    get_handler: Callable,
    inject_session: bool = False,
    session_extractor: Optional[Callable] = None,
) -> Callable:
    """
    Create a wrapper function with explicit typed parameters from JSON schema.
    
    Args:
        tool_name: Name of the tool being wrapped
        input_schema: JSON Schema defining the tool's parameters
        get_handler: Function that returns the actual handler (e.g., get_tool_wrapper)
        inject_session: Whether to inject session_id from context
        session_extractor: Function to extract session_id from context (ctx -> str)
    
    Returns:
        Async function with typed signature that FastMCP can introspect
    """
    properties = input_schema.get("properties", {})
    required = set(input_schema.get("required", []))

    # Build parameter info for signature, preserving schema metadata
    param_info = []
    for param_name, param_def in properties.items():
        param_type = _resolve_param_type(param_def)
        is_required = param_name in required
        description = param_def.get("description")
        enum_values = param_def.get("enum")
        param_info.append((param_name, param_type, is_required, description, enum_values))
    
    # Generate the wrapper dynamically
    if inject_session:
        wrapper = _create_session_wrapper(tool_name, param_info, get_handler, session_extractor)
    else:
        wrapper = _create_simple_wrapper(tool_name, param_info, get_handler)
    
    # Set function metadata for FastMCP introspection
    wrapper.__name__ = tool_name
    wrapper.__qualname__ = tool_name
    
    return wrapper

def enable_extra_argument_passthrough(tool: Any) -> bool:
    """Preserve unknown top-level MCP arguments through FastMCP validation.

    FastMCP builds an internal Pydantic argument model from a wrapper's
    signature and, by default, Pydantic ignores keys that are not named in that
    signature. UNITARES' dispatch middleware already preserves extra fields
    after its own Pydantic validation, but internal S22/R6 callers need those
    fields to survive FastMCP's earlier validation boundary first.

    This helper intentionally leaves the public/listed tool schema unchanged;
    it only swaps the registered FastMCP tool's internal argument model for a
    subclass with ``extra='allow'`` whose ``model_dump_one_level`` includes the
    retained extras.
    """
    fn_metadata = getattr(tool, "fn_metadata", None)
    arg_model = getattr(fn_metadata, "arg_model", None)
    if fn_metadata is None or arg_model is None:
        return False
    if getattr(arg_model, "__unitaires_extra_passthrough_enabled__", False):
        return False

    base_config = dict(getattr(arg_model, "model_config", {}) or {})

    class ExtraPassthroughArgModel(arg_model):  # type: ignore[misc, valid-type]
        __unitaires_extra_passthrough_enabled__ = True
        model_config = ConfigDict(**{**base_config, "extra": "allow"})

        def model_dump_one_level(self) -> dict[str, Any]:
            values = super().model_dump_one_level()
            extras = getattr(self, "__pydantic_extra__", None) or {}
            values.update(extras)
            return values

    ExtraPassthroughArgModel.__name__ = f"{arg_model.__name__}ExtraPassthrough"
    ExtraPassthroughArgModel.__qualname__ = ExtraPassthroughArgModel.__name__
    ExtraPassthroughArgModel.model_rebuild(force=True)
    fn_metadata.arg_model = ExtraPassthroughArgModel
    return True


def _resolve_param_type(param_def: dict) -> Any:
    """Resolve a JSON Schema parameter definition to a Python type.

    Handles simple ``{"type": "string"}``, Pydantic-generated ``{"anyOf": [...]}``
    union schemas, and nested anyOf (e.g. ``Union[float, str, None]`` with
    constraints generates ``anyOf: [{anyOf: [{number}, {string}], ge: ...}, {null}]``).
    """
    any_of = param_def.get("anyOf")
    if any_of:
        types = []
        has_null = False
        for variant in any_of:
            vtype = variant.get("type")
            if vtype == "null":
                has_null = True
            elif "$ref" in variant:
                # Pydantic emits nested BaseModel fields as $ref entries. FastMCP
                # only needs the Python wrapper to accept a JSON object here; the
                # handler's Pydantic schema performs the nested validation later.
                types.append(dict)
            elif vtype:
                types.append(_json_type_to_python(vtype))
            elif variant.get("properties") is not None:
                types.append(dict)
            elif "anyOf" in variant:
                # Nested anyOf (Pydantic wraps constrained unions this way)
                inner = _resolve_param_type(variant)
                if inner is not None:
                    types.append(inner)
        if not types:
            base = str
        elif len(types) == 1:
            base = types[0]
        else:
            # Flatten: if an inner type is already a Union, collect its args
            flat = []
            for t in types:
                origin = getattr(t, "__origin__", None)
                if origin is Union:
                    flat.extend(t.__args__)
                else:
                    flat.append(t)
            # Deduplicate while preserving order
            seen = set()
            unique = []
            for t in flat:
                if t not in seen:
                    seen.add(t)
                    unique.append(t)
            if len(unique) == 1:
                base = unique[0]
            elif len(unique) == 2:
                base = Union[unique[0], unique[1]]  # type: ignore
            elif len(unique) == 3:
                base = Union[unique[0], unique[1], unique[2]]  # type: ignore
            else:
                base = unique[0]  # fallback
        if has_null:
            return Optional[base]  # type: ignore
        return base

    if "$ref" in param_def:
        return dict
    if param_def.get("properties") is not None:
        return dict

    return _json_type_to_python(param_def.get("type", "string"))

def _json_type_to_python(json_type: Any) -> Any:
    """Convert JSON Schema type to Python type annotation."""
    if isinstance(json_type, list):
        # Handle union types like ["number", "string", "null"]
        non_null = [t for t in json_type if t != "null"]
        has_null = "null" in json_type
        
        if len(non_null) > 1:
            # Multiple non-null types - create Union
            python_types = [_json_type_to_python(t) for t in non_null]
            # Build Union type dynamically
            if len(python_types) == 2:
                union_type = Union[python_types[0], python_types[1]]  # type: ignore
            elif len(python_types) == 3:
                union_type = Union[python_types[0], python_types[1], python_types[2]]  # type: ignore
            else:
                # Fallback: use first type if more than 3 (shouldn't happen in practice)
                union_type = python_types[0]
            
            if has_null:
                return Optional[union_type]  # type: ignore
            return union_type
        elif non_null:
            # Single non-null type, possibly with null
            base_type = _json_type_to_python(non_null[0])
            if has_null:
                return Optional[base_type]  # type: ignore
            return base_type
        else:
            # Only null (shouldn't happen in practice)
            return str
    
    type_map = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": Union[str, bool],  # Accept strings for boolean coercion (e.g., "true" → True)
        "array": list,
        "object": dict,
    }
    return type_map.get(json_type, str)

def _build_annotated_type(
    base_type: Any,
    is_required: bool,
    description: Optional[str] = None,
    enum_values: Optional[list] = None,
) -> Any:
    """Build an Annotated type that carries schema metadata through to FastMCP/Pydantic.

    Returns Annotated[type, Field(...)] when metadata exists, plain type otherwise.
    """
    field_kwargs: dict[str, Any] = {}
    if description:
        field_kwargs["description"] = description
    if enum_values:
        field_kwargs["json_schema_extra"] = {"enum": enum_values}

    if not field_kwargs:
        return Optional[base_type] if not is_required else base_type

    if is_required:
        return Annotated[base_type, Field(**field_kwargs)]
    else:
        return Annotated[Optional[base_type], Field(default=None, **field_kwargs)]

def _create_simple_wrapper(
    tool_name: str,
    param_info: list,
    get_handler: Callable,
) -> Callable:
    """Create wrapper for tools that don't need session injection.

    Args:
        tool_name: Name of the tool
        param_info: List of (name, type, is_required, description, enum_values) tuples
        get_handler: Function that returns the actual handler (e.g., get_tool_wrapper)
    """
    # Build proper signature with typed parameters, preserving schema metadata
    params = []
    for name, ptype, is_required, description, enum_values in param_info:
        annotated_type = _build_annotated_type(ptype, is_required, description, enum_values)
        if is_required:
            param = inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotated_type,
            )
        else:
            param = inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=annotated_type,
            )
        params.append(param)
    
    # Create and set the signature
    sig = inspect.Signature(params, return_annotation=dict)
    
    # The actual implementation collects kwargs
    async def typed_wrapper(**kwargs) -> dict:
        # Handle CLI's kwargs wrapping: {"kwargs": {"name": "..."}} -> {"name": "..."}
        if "kwargs" in kwargs:
            wrapped = kwargs.pop("kwargs")
            if isinstance(wrapped, str):
                import json
                try:
                    wrapped = json.loads(wrapped)
                except json.JSONDecodeError:
                    pass
            if isinstance(wrapped, dict):
                kwargs.update(wrapped)

        # Filter out None values for cleaner handler calls
        filtered = {k: v for k, v in kwargs.items() if v is not None}
        handler = get_handler(tool_name)
        return await handler(**filtered)

    typed_wrapper.__signature__ = sig
    typed_wrapper.__name__ = tool_name
    typed_wrapper.__qualname__ = tool_name

    return typed_wrapper

def _create_session_wrapper(
    tool_name: str,
    param_info: list,
    get_handler: Callable,
    session_extractor: Callable,
) -> Callable:
    """Create wrapper for tools that need session injection.

    Args:
        tool_name: Name of the tool
        param_info: List of (name, type, is_required, description, enum_values) tuples
        get_handler: Function that returns the actual handler (e.g., get_tool_wrapper)
        session_extractor: Function to extract session_id from context (ctx -> str)
    """
    from mcp.server.fastmcp import Context

    # Build signature: ctx first, then typed params
    params = [
        inspect.Parameter(
            "ctx",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Optional[Context],
        )
    ]

    for name, ptype, is_required, description, enum_values in param_info:
        annotated_type = _build_annotated_type(ptype, is_required, description, enum_values)
        if is_required:
            param = inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                annotation=annotated_type,
            )
        else:
            param = inspect.Parameter(
                name,
                inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=annotated_type,
            )
        params.append(param)
    
    sig = inspect.Signature(params, return_annotation=dict)
    
    async def typed_wrapper(*, ctx=None, **kwargs) -> dict:
        # Handle CLI's kwargs wrapping: {"kwargs": {"name": "..."}} -> {"name": "..."}
        if "kwargs" in kwargs:
            wrapped = kwargs.pop("kwargs")
            if isinstance(wrapped, str):
                import json
                try:
                    wrapped = json.loads(wrapped)
                except json.JSONDecodeError:
                    pass
            if isinstance(wrapped, dict):
                kwargs.update(wrapped)

        # Inject session if available and not already provided
        if session_extractor and ctx:
            session_id = session_extractor(ctx)
            if session_id and "client_session_id" not in kwargs:
                kwargs["client_session_id"] = session_id
                logger.debug(f"[TYPED_WRAPPER] {tool_name}: injected session_id={session_id}")

        # Filter out None values
        filtered = {k: v for k, v in kwargs.items() if v is not None}
        handler = get_handler(tool_name)
        return await handler(**filtered)

    typed_wrapper.__signature__ = sig
    typed_wrapper.__name__ = tool_name
    typed_wrapper.__qualname__ = tool_name
    # Sync __annotations__ with the synthesized signature. FastMCP's
    # find_context_parameter reads typing.get_type_hints (which walks
    # __annotations__, not __signature__) to decide whether to skip ctx
    # from the emitted tools/list inputSchema. Without this sync, ctx
    # leaks into the schema as a first-class argument even though the
    # signature annotation correctly types it as Optional[Context].
    typed_wrapper.__annotations__ = {
        p.name: p.annotation
        for p in sig.parameters.values()
        if p.annotation is not inspect.Parameter.empty
    }
    typed_wrapper.__annotations__["return"] = dict

    return typed_wrapper
