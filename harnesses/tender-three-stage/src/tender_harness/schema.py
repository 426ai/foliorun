def inline_schema(schema):
    """Inline local $defs so providers without $ref support get a closed flat schema."""
    defs = schema.get("$defs", {})
    def walk(v):
        if isinstance(v, list):
            return [walk(x) for x in v]
        if not isinstance(v, dict):
            return v
        if "$ref" in v:
            return walk(defs[v["$ref"].removeprefix("#/$defs/")])
        return {k: walk(x) for k, x in v.items() if k != "$defs"}
    return walk(schema)
