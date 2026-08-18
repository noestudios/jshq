"""Every schema sent through the strict structured-outputs path
(output_config.format = json_schema) may use only STRUCTURAL keywords. The API
400s — before the model ever runs — on JSON-Schema *validation* keywords: array
length bounds (minItems/maxItems other than 0 or 1) and integer/string/number
bounds (minimum, maximum, minLength, pattern, format, ...).

Synthesis hit this twice — minItems/maxItems on the rubric array, then
minimum/maximum on the rubric value — because propose() is a paid live call the
keyless suite never exercises. This pins EVERY module's output schema so a third
never ships. Range/length checks belong in each module's own validator, not the
model-facing schema.
"""

from jshq import jobparse, refine
from jshq.scoring import haiku, learned, synthesis
from jshq.scoring.criteria import load_criteria

# Unconditionally unsupported inside an output schema. (minItems/maxItems are
# allowed as 0 or 1 only — checked separately.)
FORBIDDEN = {
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "minLength",
    "maxLength",
    "pattern",
    "format",
    "uniqueItems",
    "minProperties",
    "maxProperties",
    "patternProperties",
}


def assert_output_schema_supported(schema, label):
    def walk(node, path):
        if isinstance(node, dict):
            bad = FORBIDDEN & node.keys()
            assert not bad, f"{path} carries unsupported keyword(s) {sorted(bad)}"
            for bound in ("minItems", "maxItems"):
                if bound in node:
                    assert node[bound] in (0, 1), f"{path}.{bound}={node[bound]} unsupported"
            for k, v in node.items():
                walk(v, f"{path}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]")

    walk(schema, label)


def test_all_structured_output_schemas_use_supported_keywords():
    assert_output_schema_supported(synthesis.SCHEMA, "synthesis.SCHEMA")
    assert_output_schema_supported(refine.SCHEMA, "refine.SCHEMA")
    assert_output_schema_supported(learned.SCHEMA, "learned.SCHEMA")
    assert_output_schema_supported(jobparse._LLM_SCHEMA, "jobparse._LLM_SCHEMA")
    # haiku's schema is built from the criteria doc — check both the default
    # shape and the one derived from the live (seeded) criteria.
    assert_output_schema_supported(haiku.build_schema(), "haiku.build_schema()")
    assert_output_schema_supported(
        haiku.build_schema(load_criteria()), "haiku.build_schema(criteria)"
    )
