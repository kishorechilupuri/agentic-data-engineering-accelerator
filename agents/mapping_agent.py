"""Stage 2 -- Source-to-Target Mapping (STTM) Agent.

Produces the mapping document that drives code generation, data quality and
tests. Grounded in both the declared schema and a profile of the data that
actually landed in ADLS Gen2.

Model tier: `deep` (Opus-class) -- cross-schema reasoning across three layers.
"""

import json

import config
import tools as tool_module
from agents.base_agent import (
    STATUS_DEGRADED,
    STATUS_OK,
    BaseAgent,
    StageResult,
    extract_json,
)
from agents.fallbacks import fallback_mapping
from agents.grounding import landing_brief, schema_brief, validate_table_names

VALID_LAYERS = set(config.LAYERS)


class SourceToTargetMappingAgent(BaseAgent):
    key = "sttm_mapper"
    prompt_name = "sttm_mapper"
    description = "Builds the L1/L2/L3 source-to-target mapping document."
    tool_names = (
        "list_source_tables",
        "get_table_schema",
        "profile_landing_dataset",
        "batch_tool",
    )

    def run(self, requirements: dict) -> StageResult:
        self.logger.info("Building source-to-target mapping.")

        # Ground the mapping in the real schemas and the real landed files.
        # A mapping built on invented columns produces code that cannot run.
        tables, _invented = validate_table_names(requirements.get("source_tables"))
        if not tables:
            tables = [
                table["table_name"]
                for table in tool_module.list_source_tables()["tables"]
            ]

        requirements_json = json.dumps(requirements, indent=2)[:4000]
        notes, mappings = [], []
        total_turns, all_calls = 0, []
        status = STATUS_OK

        # One request per source table (L1 + L2), then one for the gold layer.
        # Asking an 8B model for every layer of every table in a single reply
        # overruns its output budget and the JSON comes back truncated.
        for table in tables:
            produced, turns, calls, note = self._map_table(table, requirements_json)
            mappings.extend(produced)
            total_turns += turns
            all_calls.extend(calls)
            if note:
                notes.append(note)
                status = STATUS_DEGRADED

        produced, turns, calls, note = self._map_gold(
            tables, requirements, requirements_json
        )
        mappings.extend(produced)
        total_turns += turns
        all_calls.extend(calls)
        if note:
            notes.append(note)
            status = STATUS_DEGRADED

        turns, calls = total_turns, all_calls
        mapping = {"mappings": mappings}

        covered = {item["layer"] for item in mappings}
        missing = sorted(VALID_LAYERS - covered)
        if missing:
            self.logger.warning("Mapping is missing layers: %s", missing)
            supplement = [
                item for item in fallback_mapping(requirements)["mappings"]
                if item["layer"] in missing
            ]
            mappings.extend(supplement)
            mapping = {"mappings": mappings}
            notes.append(
                f"Layers {missing} were absent from the model's mapping and "
                "were supplied from the catalog-derived fallback."
            )
            status = STATUS_DEGRADED

        path = self.write("mapping/source_to_target_mapping.json", mapping)
        csv_path = self.write(
            "mapping/source_to_target_mapping.csv", _render_csv(mappings)
        )

        self.logger.info(
            "Mapping ready | objects=%d | layers=%s | status=%s",
            len(mappings),
            sorted({item["layer"] for item in mappings}),
            status,
        )
        return StageResult(
            agent=self.key,
            status=status,
            output=mapping,
            artifacts=[path, csv_path],
            notes=notes,
            turns=turns,
            tool_calls=calls,
        )

    # ------------------------------------------------------------------
    def _map_table(self, table: str, requirements_json: str):
        """Produce the L1 and L2 mapping objects for one source table."""
        schema = tool_module.get_table_schema(table)
        dataset = schema.get("landing_dataset")
        self.logger.info("Mapping %s (L1 + L2).", table)

        scope = (
            f"Produce exactly TWO mapping objects for the source table "
            f"`{table}`:\n"
            f"  1. layer L1: landing file -> {config.CATALOG}.bronze.{dataset}\n"
            f"  2. layer L2: {config.CATALOG}.bronze.{dataset} -> "
            f"{config.CATALOG}.silver.{dataset}"
        )
        system = self.build_system(
            requirements_json=requirements_json,
            catalog=config.CATALOG,
            scope=scope,
        )
        user = (
            "SOURCE SCHEMA (authoritative -- use these column names and types "
            "exactly, and no others):\n\n"
            f"{schema_brief([table])}\n\n"
            "LANDED FILE PROFILE (what ADF actually delivered):\n\n"
            f"{landing_brief([dataset])}\n\n"
            "Reply with the JSON object only."
        )

        text, _messages, turns, calls = self.converse(user, system)
        produced = self._clean(extract_json(text))
        produced = [item for item in produced if item["layer"] in ("L1", "L2")]

        missing = {"L1", "L2"} - {item["layer"] for item in produced}
        note = None
        if missing:
            supplement = [
                item for item in fallback_mapping()["mappings"]
                if item["layer"] in missing
                and str(item.get("target_object", "")).endswith(f".{dataset}")
            ]
            produced.extend(supplement)
            note = (
                f"Layers {sorted(missing)} for `{table}` were not returned "
                "usably by the model and were supplied from the "
                "catalog-derived fallback."
            )
            self.logger.warning(note)
        return produced, turns, calls, note

    def _map_gold(self, tables: list, requirements: dict, requirements_json: str):
        """Produce the L3 mapping object(s) for the curated layer."""
        self.logger.info("Mapping the gold layer (L3).")

        silver = [
            f"{config.CATALOG}.silver."
            f"{tool_module.get_table_schema(table).get('landing_dataset')}"
            for table in tables
        ]
        entities = [
            entity for entity in requirements.get("target_entities", [])
            if isinstance(entity, dict) and str(entity.get("layer")) == "L3"
        ]
        wanted = (
            ", ".join(f"`{entity.get('name')}`" for entity in entities)
            or "the curated entity the requirements describe"
        )

        scope = (
            "Produce the mapping object(s) for layer L3 only. Build "
            f"{wanted} by joining the silver tables listed below. Target "
            f"schema is `{config.CATALOG}.gold`. Load strategy is `overwrite`."
        )
        system = self.build_system(
            requirements_json=requirements_json,
            catalog=config.CATALOG,
            scope=scope,
        )
        user = (
            "SILVER TABLES AVAILABLE TO JOIN:\n"
            + "\n".join(f"  - {name}" for name in silver)
            + "\n\nTheir columns are the snake_case forms of these source "
            "schemas:\n\n"
            f"{schema_brief(tables)}\n\n"
            "Reply with the JSON object only."
        )

        text, _messages, turns, calls = self.converse(user, system)
        produced = [
            item for item in self._clean(extract_json(text))
            if item["layer"] == "L3"
        ]

        note = None
        if not produced:
            produced = [
                item for item in fallback_mapping()["mappings"]
                if item["layer"] == "L3"
            ]
            note = (
                "The gold (L3) mapping was not returned usably by the model "
                "and was supplied from the catalog-derived fallback."
            )
            self.logger.warning(note)
        return produced, turns, calls, note

    def _clean(self, mapping) -> list:
        """Keep only mapping objects that are structurally usable."""
        if not isinstance(mapping, dict):
            return []
        candidates = mapping.get("mappings") or mapping.get("items") or []
        if isinstance(candidates, dict):
            candidates = [candidates]

        cleaned = []
        for index, item in enumerate(candidates, 1):
            if not isinstance(item, dict):
                continue
            layer = str(item.get("layer", "")).upper()
            columns = item.get("columns")
            if layer not in VALID_LAYERS or not isinstance(columns, list) or not columns:
                self.logger.debug("Discarding unusable mapping object #%d", index)
                continue
            item["layer"] = layer
            item.setdefault("mapping_id", f"MAP-{layer}-{index:02d}")
            item.setdefault("load_strategy", _default_strategy(layer))
            item.setdefault("business_key", [])
            item["columns"] = [
                column for column in columns
                if isinstance(column, dict) and column.get("target_column")
            ]
            if item["columns"]:
                cleaned.append(item)
        return cleaned


def _default_strategy(layer: str) -> str:
    return {"L1": "append", "L2": "merge", "L3": "overwrite"}.get(layer, "append")


_CSV_HEADER = (
    "mapping_id,layer,source_object,target_object,load_strategy,"
    "business_key,source_column,target_column,target_type,transformation,nullable"
)


def _render_csv(mappings: list) -> str:
    """Flat CSV view -- the format data engineers actually review in."""
    rows = [_CSV_HEADER]
    for mapping in mappings:
        key = "|".join(str(item) for item in mapping.get("business_key", []))
        for column in mapping.get("columns", []):
            rows.append(
                ",".join(
                    _escape(value)
                    for value in (
                        mapping.get("mapping_id", ""),
                        mapping.get("layer", ""),
                        mapping.get("source_object", ""),
                        mapping.get("target_object", ""),
                        mapping.get("load_strategy", ""),
                        key,
                        column.get("source_column", ""),
                        column.get("target_column", ""),
                        column.get("target_type", ""),
                        column.get("transformation", ""),
                        str(column.get("nullable", True)),
                    )
                )
            )
    return "\n".join(rows) + "\n"


def _escape(value) -> str:
    text = str(value).replace('"', "'")
    return f'"{text}"' if "," in text else text
