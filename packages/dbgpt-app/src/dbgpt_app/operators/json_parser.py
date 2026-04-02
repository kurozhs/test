"""JSON Parser Operators for AWEL flows."""

import json
import logging
import re
from typing import Optional

from dbgpt.core import ModelOutput
from dbgpt.core.awel import MapOperator
from dbgpt.core.awel.flow import (
    TAGS_ORDER_HIGH,
    IOField,
    OperatorCategory,
    ViewMetadata,
)
from dbgpt.util.i18n_utils import _

logger = logging.getLogger(__name__)


def extract_json_from_text(text: str) -> Optional[dict]:
    """Extract JSON from text that may contain markdown code blocks or other content."""
    if not text:
        return None

    # Try to parse directly
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try to extract JSON from markdown code blocks
    json_patterns = [
        r'```json\s*([\s\S]*?)\s*```',
        r'```\s*([\s\S]*?)\s*```',
        r'\{[\s\S]*\}',
    ]

    for pattern in json_patterns:
        matches = re.findall(pattern, text, re.MULTILINE)
        for match in matches:
            try:
                return json.loads(match.strip())
            except json.JSONDecodeError:
                continue

    return None


class ModelOutputToSqlDict(MapOperator[ModelOutput, dict]):
    """Converts a model output to a SQL dictionary.

    Extracts JSON containing SQL from the LLM output, handling various formats
    including markdown code blocks.
    """

    metadata = ViewMetadata(
        label=_("LLM Output to SQL Dict"),
        name="model_output_to_sql_dict",
        description=_("Parse LLM output to extract SQL dictionary for database execution."),
        category=OperatorCategory.TYPE_CONVERTER,
        parameters=[],
        inputs=[IOField.build_from(_("Model Output"), "model_output", ModelOutput)],
        outputs=[IOField.build_from(_("SQL Dictionary"), "sql_dict", dict)],
        tags={"order": TAGS_ORDER_HIGH},
    )

    async def map(self, model_output: ModelOutput) -> dict:
        """Extract SQL dict from model output."""
        text = model_output.text if hasattr(model_output, 'text') else str(model_output)

        result = extract_json_from_text(text)

        if result and isinstance(result, dict):
            logger.info(f"Extracted SQL: {result.get('sql', 'N/A')}")
            return result

        # Fallback: return empty dict with thoughts
        logger.warning(f"Failed to parse JSON from LLM output: {text[:200]}...")
        return {
            "thoughts": text,
            "sql": None,
            "display_type": "text"
        }


class StringToSqlDict(MapOperator[str, dict]):
    """Converts a string to a SQL dictionary.

    Extracts JSON containing SQL from text, handling various formats.
    """

    metadata = ViewMetadata(
        label=_("String to SQL Dict"),
        name="string_to_sql_dict",
        description=_("Parse string to extract SQL dictionary for database execution."),
        category=OperatorCategory.TYPE_CONVERTER,
        parameters=[],
        inputs=[IOField.build_from(_("String"), "string", str)],
        outputs=[IOField.build_from(_("SQL Dictionary"), "sql_dict", dict)],
        tags={"order": TAGS_ORDER_HIGH},
    )

    async def map(self, text: str) -> dict:
        """Extract SQL dict from string."""
        result = extract_json_from_text(text)

        if result and isinstance(result, dict):
            logger.info(f"Extracted SQL: {result.get('sql', 'N/A')}")
            return result

        # Fallback
        logger.warning(f"Failed to parse JSON from string: {text[:200]}...")
        return {
            "thoughts": text,
            "sql": None,
            "display_type": "text"
        }
