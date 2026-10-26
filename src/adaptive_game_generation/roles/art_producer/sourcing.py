"""Audit library-first selection without inventing visual/semantic judgments."""
from ...runtime import InvalidSubmission


def validate_library_usage(asset, candidate):
    """Use explicit pack-category names as negative evidence, never visual approval."""
    if asset.layout is None or asset.layout.kind in ("ui", "background", "source_atlas"):
        return
    parts = {p.casefold().replace("_", "-").replace(" ", "-") for p in candidate.path.split("/")}
    if parts.intersection({"input-prompts", "input-icons", "controller-prompts"}):
        raise InvalidSubmission(f"Asset {asset.asset_id}: input-prompt/icon packs are UI glyphs, not actors, terrain or world props. "
            "Search for the actual gameplay object; use layout.kind=ui only when the asset's purpose really is an input hint")


def validate_fallback(asset, sources, observations, *, require_scoped=False):
    available = {s["library"] for s in sources["libraries"] if s["available"]}
    if not available:
        return  # The host, not the model, established that both mounts are absent.
    fallback = asset.generation_fallback
    prefix = f"Asset {asset.asset_id}: "
    if fallback is None:
        raise InvalidSubmission(prefix + "Library-first policy requires generation_fallback with a reason, "
            "actual search_ids covering every available library, and reviewed candidates before generating")
    searches = {s["search_id"]: s for s in observations}
    if set(fallback.search_ids) - searches.keys():
        raise InvalidSubmission(prefix + "Generation fallback references an unknown search_id")
    used = [searches[s] for s in fallback.search_ids]
    if require_scoped and any(asset.asset_id not in s.get("asset_ids", ()) for s in used):
        raise InvalidSubmission(prefix + "Fallback searches must explicitly include this asset_id in their search scope; unrelated discovery is not evidence of a missing asset")
    if available - {s["library"] for s in used}:
        raise InvalidSubmission(prefix + "Search every available library before choosing image generation")
    returned = {(a["library"], a["path"]) for s in used for a in s["assets"]}
    rejected = {(c.library, c.path) for c in fallback.rejected_candidates}
    if rejected - returned:
        raise InvalidSubmission(prefix + "Rejected library candidates must come from the cited searches")
    for search in used:
        candidates = {(a["library"], a["path"]) for a in search["assets"]}
        if candidates and not candidates.intersection(rejected):
            raise InvalidSubmission(prefix + "Explain why a returned candidate cannot satisfy this asset; "
                "prefer adapting the style to a usable library sprite")
        if not candidates and search["scan_truncated"]:
            raise InvalidSubmission(prefix + "An empty truncated scan does not establish a library fallback; refine the search")


def validate_repair(asset, prior):
    if prior is None or prior.normalization is None or prior.normalization.status == "materialized":
        return
    old = prior.request
    if old.layout and old.layout.kind in ("sprite", "sprite_sheet") and (
            asset.layout is None or asset.layout.kind not in ("sprite", "sprite_sheet")):
        raise InvalidSubmission(f"Asset {asset.asset_id}: a rejected sprite cannot become a UI/background to evade quality checks")
    # Renaming a file/frame or rewriting integration notes does not repair pixels.
    materialization = {"source", "source_path", "generation_prompt", "width", "height",
                       "transparent_background", "columns", "rows", "layout"}
    if asset.model_dump(include=materialization) == old.model_dump(include=materialization):
        raise InvalidSubmission(f"Asset {asset.asset_id}: unchanged rejected request; revise the prompt/layout or choose a library source")
