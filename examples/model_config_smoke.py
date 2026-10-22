"""Opt-in, bounded real API checks; not full role or controller integration tests."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import time
from pathlib import Path

from adaptive_game_generation.models.base import utc_now
from adaptive_game_generation.providers import GatewayConfig, OpenAICompatibleTextProvider
from adaptive_game_generation.providers.openai_compatible import TEXT_ROLE_PREFIXES
from adaptive_game_generation.runtime.store import atomic_text

ROOT = Path(__file__).resolve().parents[1]
INSTRUCTION = ROOT / "benchmark_resources/FreedomIntelligence-gamecraft-bench-a433475/tasks/platformer-time-loop/instruction.md"
INSTRUCTION_SHA256 = "dcb85ce30a25d88a5e0a12b6a0f91a8783e896d6816cdcb88d6b8432e5c10969"
FOCUS = {
    "DESIGNER": "Identify one concrete gameplay requirement and its acceptance condition.",
    "ART_PLANNER": "Identify one required art asset and an allowed acquisition source.",
    "CODE_DEVELOPER": "Describe one implementation responsibility for the time-loop mechanic.",
    "PLAYER": "Describe one input sequence to test a required mechanic; do not claim it was executed.",
    "QA": "Describe evidence needed to accept a requirement; no gameplay evidence is supplied, so do not claim a pass.",
    "ART_SVG": "Describe one possible vector asset, respecting the asset-source rules. Do not create a file.",
    "SCHEDULER": "Identify one task constraint relevant to a future scheduler. Do not schedule or invoke any role.",
}


def write_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def build_prompt(prefix, instruction):
    return json.dumps({
        "protocol": "model-configuration-smoke.v1",
        "purpose": "Check the configured model transport and English JSON output, not full role execution.",
        "role_configuration": prefix,
        "task_id": "platformer-time-loop",
        "instruction": instruction,
        "request": FOCUS[prefix],
        "output_schema": {
            "role_configuration": "Copy the exact role_configuration string.",
            "source_excerpt": "One nonempty contiguous excerpt, at most 200 characters, copied exactly from instruction.",
            "observation": "One or two short English sentences answering the request using that excerpt.",
        },
        "constraints": [
            "Return exactly these three string fields in a single JSON object, with no Markdown fences.",
            "Do not generate a complete design or game. Do not claim any play, build, asset generation, or acceptance happened.",
        ],
    }, ensure_ascii=False)


def validate_response(text, prefix, instruction):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=unique)
    if not isinstance(value, dict) or set(value) != {"role_configuration", "source_excerpt", "observation"}:
        raise ValueError("Unexpected smoke response fields")
    if not all(isinstance(item, str) for item in value.values()) or value["role_configuration"] != prefix:
        raise ValueError("Unexpected smoke response types or role")
    if not 1 <= len(value["source_excerpt"]) <= 200 or value["source_excerpt"] not in instruction:
        raise ValueError("Excerpt is not grounded in the original instruction")
    if not value["observation"].strip() or re.search(r"[\u3400-\u9fff]", value["observation"]):
        raise ValueError("Missing English observation")
    return value


async def run(output, env_file, prefixes, max_output_tokens=1024, timeout=90.0):
    raw = INSTRUCTION.read_bytes()  # Read only the public instruction, not task-directory siblings.
    if hashlib.sha256(raw).hexdigest() != INSTRUCTION_SHA256:
        raise ValueError("Benchmark instruction checksum mismatch")
    instruction = raw.decode("utf-8")
    configs = {prefix: GatewayConfig.from_env(env_file, prefix=prefix) for prefix in prefixes}
    output.mkdir(parents=True, exist_ok=False)
    (output / "instruction.md").write_bytes(raw)
    report = {
        "protocol": "model-configuration-smoke.v1", "started_at": utc_now().isoformat(),
        "source": str(INSTRUCTION.relative_to(ROOT)).replace("\\", "/"),
        "instruction_sha256": INSTRUCTION_SHA256,
        "scope": "Small real text requests. No Executor invocation, controller implementation, game build, play, or image generation.",
        "requested_output_tokens_per_call": max_output_tokens, "timeout_seconds_per_call": timeout,
        "automatic_retries": 0, "image_calls": 0, "results": [],
    }
    write_json(output / "report.json", report)
    # Deliberately sequential: low quota pressure and no hidden concurrency.
    for prefix, config in configs.items():
        directory = output / prefix.lower()
        directory.mkdir()
        write_json(directory / "provider_config.json", config.public_settings())
        prompt = build_prompt(prefix, instruction)
        atomic_text(directory / "prompt.json", prompt + "\n")
        row = {
            "prefix": prefix, "configured_model": config.model,
            "configuration_id": config.configuration_id,
            "reserved_only": prefix in ("ART_SVG", "SCHEDULER"),
        }
        started = time.monotonic()
        try:
            async with OpenAICompatibleTextProvider(config, capture_directory=directory / "model_calls") as provider:
                async with asyncio.timeout(timeout):
                    reply = await provider.generate(
                        prompt, max_output_tokens=min(max_output_tokens, config.max_output_tokens), timeout=timeout,
                    )
            response = json.loads((directory / "model_calls/001/response.json").read_text(encoding="utf-8"))
            row.update(
                returned_model=response.get("model"),
                finish_reason=response["choices"][0]["finish_reason"],
                input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
                cached_input_tokens=reply.cached_input_tokens,
                reasoning_tokens=(response.get("usage", {}).get("completion_tokens_details") or {}).get("reasoning_tokens"),
            )
            if row["finish_reason"] != "stop":
                raise ValueError("Response did not finish normally")
            if reply.output_tokens > min(max_output_tokens, config.max_output_tokens):
                raise ValueError("Returned output usage exceeded the requested allowance")
            validate_response(reply.text, prefix, instruction)
            row["status"] = "passed"
        except Exception as exc:
            # The provider's private metadata has sanitized transport details.
            # Never print arbitrary exception text that might contain a URL or credential.
            row.update(status="failed", error_type=type(exc).__name__)
        row["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["results"].append(row)
        write_json(output / "report.json", report)
        print(json.dumps(row), flush=True)
    report["finished_at"] = utc_now().isoformat()
    report["passed"] = all(row["status"] == "passed" for row in report["results"])
    report["total_input_tokens"] = sum(row.get("input_tokens", 0) for row in report["results"])
    report["total_output_tokens"] = sum(row.get("output_tokens", 0) for row in report["results"])
    write_json(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Explicitly allow real API requests.")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--output", type=Path, required=True, help="New directory; existing results are never overwritten.")
    parser.add_argument("--roles", nargs="+", choices=TEXT_ROLE_PREFIXES, default=list(TEXT_ROLE_PREFIXES))
    args = parser.parse_args()
    if not args.live:
        parser.error("--live is required because these checks consume real model quota")
    if len(args.roles) != len(set(args.roles)):
        parser.error("--roles cannot contain duplicates")
    result = asyncio.run(run(args.output, args.env_file, args.roles))
    raise SystemExit(0 if result["passed"] else 1)
