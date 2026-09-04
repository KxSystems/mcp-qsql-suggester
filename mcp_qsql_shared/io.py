"""User-prompt formatting."""

from __future__ import annotations


def build_user_prompt(example: dict, output_schema: str | None = None) -> str:
    """Build the user prompt from an example, including table schemas if available.

    ``example`` is a dict rather than plain arguments because this function is
    byte-identical to the one that built the prompts the model was fine-tuned
    on, where the dict is a training record
    (``{"instruction": ..., "metadata": {"table_schemas": ...}, ...}``). That
    tree is maintainer-side and not part of this repo. The shape is therefore
    load-bearing: the prompt contract only holds as long as this renders the
    same string as it did at training time, so the server's only caller
    (``mcp_server.loop._build_messages``) fabricates the two keys it needs
    rather than this signature being flattened. Do not "tidy" it.
    """
    parts = [example["instruction"]]

    schemas = example.get("metadata", {}).get("table_schemas", {})
    if schemas:
        parts.append("\nTable schemas:")
        for tname, sdef in schemas.items():
            parts.append(f"  {tname}: {sdef}")

    if output_schema:
        parts.append(f"\nExpected result schema: {output_schema}")

    return "\n".join(parts)
