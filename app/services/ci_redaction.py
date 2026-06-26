"""CI trace redaction helpers."""


def masked_values_from_variables(variables: dict[str, object]) -> list[str]:
    values: list[str] = []
    for variable in variables.values():
        if not isinstance(variable, dict) or not variable.get("masked"):
            continue
        value = str(variable.get("value", ""))
        if value:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def redact_trace_text(text: str, variables: dict[str, object]) -> str:
    redacted = text
    for value in masked_values_from_variables(variables):
        redacted = redacted.replace(value, "[MASKED]")
    return redacted
