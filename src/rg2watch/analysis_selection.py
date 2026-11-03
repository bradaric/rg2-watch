"""Select stored analyses without making a model change erase completed reads."""
from collections import Counter


def model_tie_key(model: str | None) -> tuple[bool, str]:
    """Sort named models lexically and keep the distinct NULL model last."""
    return model is None, model or ""


def preferred_models(rows, preferred_model: str) -> list[str | None]:
    """Preferred model followed by completed models in descending document coverage."""
    coverage = Counter(row["model"] for row in rows)
    return [preferred_model, *sorted(
        (model for model in coverage if model != preferred_model),
        key=lambda model: (-coverage[model], *model_tie_key(model)))]


def preferred_analyses(rows, preferred_model: str, prompt_prefix: str | None = None,
                       model_order: list[str | None] | None = None) -> dict:
    """Document id -> preferred model row, else the most widely used completed model row."""
    rows = [row for row in rows
            if prompt_prefix is None or row["prompt_version"].startswith(prompt_prefix)]
    order = model_order or preferred_models(rows, preferred_model)
    order = [*order, *sorted(
        {row["model"] for row in rows} - set(order), key=model_tie_key)]
    rank = {model: i for i, model in enumerate(order)}
    selected = {}
    for row in rows:
        current = selected.get(row["document_id"])
        if current is None or rank[row["model"]] < rank[current["model"]]:
            selected[row["document_id"]] = row
    return selected
