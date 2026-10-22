"""Trusted MCP stdio client inside a disposable, network-disabled container.

Only the fixed inspection sequence below is dispatched. No model-supplied MCP
method, executable, filesystem root, script, or arbitrary input action is accepted.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

PROTOCOL = "2025-06-18"
SERVER = "/opt/godot-mcp/node_modules/godot-mcp-runtime/dist/index.js"
TOOLS = ("get_scene_tree", "run_project", "simulate_input", "take_screenshot",
         "get_ui_elements", "get_debug_output", "stop_project")
MAX_WIRE_BYTES = 24 * 1024 * 1024


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def tree_hash(path):
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*"), key=lambda p: tuple(s.casefold() for s in p.relative_to(path).parts)):
        if child.is_symlink():
            raise ValueError("Symlinks are forbidden")
        if child.is_file():
            digest.update(child.relative_to(path).as_posix().encode() + b"\0")
            digest.update(hashlib.sha256(child.read_bytes()).digest())
    return digest.hexdigest()


class Client:
    def __init__(self, output):
        self.output, self.process, self.counter = output, None, 0
        self.bytes = 0
        self.calls = []

    async def start(self, environment):
        self.stderr = (self.output / "server.stderr.txt").open("wb")
        self.transcript = (self.output / "transcript.jsonl").open("w", encoding="utf-8")
        self.process = await asyncio.create_subprocess_exec(
            "node", SERVER, env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=MAX_WIRE_BYTES)
        async def drain():
            size = 0
            while data := await self.process.stderr.read(65536):
                size += len(data)
                if size > 2 * 1024 * 1024:
                    self.process.kill()
                    raise ValueError("MCP stderr exceeded limit")
                self.stderr.write(data)
                self.stderr.flush()
        self.reader = asyncio.create_task(drain())
        info = await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {"elicitation": {}},
            "clientInfo": {"name": "gamecraft-inspector", "version": "1.0.0"}})
        if info.get("protocolVersion") != PROTOCOL or "tools" not in info.get("capabilities", {}):
            raise ValueError("MCP protocol or capability mismatch")
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        catalog = await self.request("tools/list", {})
        if not set(TOOLS).issubset({t["name"] for t in catalog.get("tools", [])}):
            raise ValueError("MCP server lacks required tools")
        write_json(self.output / "server.json", {"initialize": info, "catalog": catalog})

    def log(self, direction, message):
        row = json.dumps({"direction": direction, "message": message}, ensure_ascii=False) + "\n"
        self.bytes += len(row.encode())
        if self.bytes > MAX_WIRE_BYTES:
            raise ValueError("MCP transcript exceeded limit")
        self.transcript.write(row)
        self.transcript.flush()

    async def send(self, message):
        self.log("client", message)
        self.process.stdin.write((json.dumps(message) + "\n").encode())
        await self.process.stdin.drain()

    async def request(self, method, params):
        self.counter += 1
        identity = self.counter
        await self.send({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        async def receive():
            while True:
                line = await self.process.stdout.readline()
                if not line:
                    raise ValueError("MCP server closed its output")
                message = json.loads(line)
                self.log("server", message)
                if message.get("jsonrpc") != "2.0":
                    raise ValueError("Invalid MCP envelope")
                if "method" in message:
                    # Only the operator-authorized fixed launch may be confirmed.
                    # Unknown confirmations and all sampling/roots requests fail closed.
                    if "id" in message:
                        if message["method"] == "elicitation/create":
                            permitted = authorized_launch(message.get("params", {}), method, params)
                            await self.send({"jsonrpc": "2.0", "id": message["id"], "result": {
                                "action": "accept" if permitted else "decline", "content": {"confirm": permitted}}})
                        else:
                            await self.send({"jsonrpc": "2.0", "id": message["id"],
                                "error": {"code": -32601, "message": "Client capability is not enabled"}})
                    continue
                if message.get("id") != identity:
                    raise ValueError("Unexpected MCP response ID")
                if "error" in message:
                    raise ValueError("MCP request failed: " + json.dumps(message["error"]))
                return message["result"]
        return await asyncio.wait_for(receive(), 45)

    async def tool(self, name, arguments):
        if name not in TOOLS:
            raise ValueError("MCP tool is not allowed")
        self.calls.append({"name": name, "arguments": arguments})
        result = await self.request("tools/call", {"name": name, "arguments": arguments})
        self.calls[-1]["result"] = result
        return result

    async def close(self):
        if self.process:
            if self.process.stdin:
                self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except asyncio.TimeoutError:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 3)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            if hasattr(self, "reader"):
                await self.reader
        if hasattr(self, "stderr"):
            self.stderr.close()
            self.transcript.close()


def brief(result):
    # Full MCP envelopes, including image bytes, remain in transcript.jsonl.
    text = json.dumps({k: v for k, v in result.items() if k != "content"} | {
        "content": [c for c in result.get("content", []) if c.get("type") != "image"]}, ensure_ascii=False)
    return {"text": text[:60000], "truncated": len(text) > 60000, "is_error": result.get("isError", False)}


def authorized_launch(confirmation, method, params):
    schema = confirmation.get("requestedSchema", {})
    project = params.get("arguments", {}).get("projectPath", "")
    return (method == "tools/call" and params.get("name") == "run_project"
            and re.fullmatch(r"/tmp/mcp-[A-Za-z0-9_-]+/game", project) is not None
            and confirmation.get("message") == "Launching a Godot project executes arbitrary code in its autoloads and main scene. Proceed?"
            and schema.get("required") == ["confirm"]
            and set(schema.get("properties", {})) == {"confirm"}
            and schema["properties"]["confirm"].get("type") == "boolean")


async def inspect(job, output):
    if job.get("execution_authorized") is not True:
        raise ValueError("The host must authorize isolated preview execution")
    source = Path("/workspace")
    expected = job["project_sha256"]
    if tree_hash(source) != expected:
        raise ValueError("Project checksum mismatch")
    scene = job["scene_path"]
    if not re.fullmatch(r"[A-Za-z0-9_./ -]+\.tscn", scene) or any(p in ("", ".", "..") for p in scene.split("/")):
        raise ValueError("Invalid scene path")
    scenario = job.get("scenario")
    if scenario is not None and not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", scenario):
        raise ValueError("Invalid scenario")
    if type(job["frames"]) is not int or not 1 <= job["frames"] <= 180:
        raise ValueError("Invalid preview frame allowance")
    result = {"version": "godot-mcp-inspection.v1", "server_package": "godot-mcp-runtime@3.8.1",
        "project_sha256": expected, "scene_path": scene, "scenario": scenario,
        "status": "failed", "network": "none", "fresh_process": True, "fresh_user_data": True,
        "acceptance_evidence": False, "deterministic_replay": False, "observations": {}}
    client = Client(output)
    with tempfile.TemporaryDirectory(prefix="mcp-", dir="/tmp") as temp_name:
        temp = Path(temp_name)
        game = temp / "game"
        shutil.copytree(source, game, ignore=shutil.ignore_patterns(".godot"))
        env = {**os.environ, "HOME": str(temp / "home"), "XDG_DATA_HOME": str(temp / "data"),
               "XDG_CONFIG_HOME": str(temp / "config"), "XDG_CACHE_HOME": str(temp / "cache"),
               "GODOT_MCP_STRICT": "true",
               "LIBGL_ALWAYS_SOFTWARE": "1"}
        # A prior host environment must not change the pinned server's gates.
        env.pop("GODOT_MCP_DISABLE_SECURITY", None)
        env.pop("GODOT_MCP_DISABLE_ELICITATION", None)
        if scenario:
            env["GAMECRAFT_PREVIEW_SCENARIO"] = scenario
        for key in ("HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            Path(env[key]).mkdir()
        process = await asyncio.create_subprocess_exec("godot", "--headless", "--path", str(game), "--import",
            env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            log, _ = await asyncio.wait_for(process.communicate(), 60)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        (output / "import.txt").write_bytes(log)
        try:
            if process.returncode:
                raise ValueError("Preview import failed")
            await client.start(env)
            arguments = [("get_scene_tree", {"projectPath": str(game), "scenePath": scene, "maxDepth": 8}),
                         ("run_project", {"projectPath": str(game), "scene": scene}),
                         ("simulate_input", {"actions": [{"type": "wait", "frames": job["frames"]}]}),
                         ("take_screenshot", {"responseMode": "full", "timeout": 15000}),
                         ("get_ui_elements", {})]
            errors = []
            for name, args in arguments:
                reply = await client.tool(name, args)
                result["observations"][name] = brief(reply)
                if reply.get("isError"):
                    errors.append(name)
                if name == "take_screenshot" and not reply.get("isError"):
                    pictures = [c for c in reply.get("content", []) if c.get("type") == "image"]
                    if len(pictures) != 1 or pictures[0].get("mimeType") != "image/png":
                        raise ValueError("Expected one actual PNG MCP image")
                    data = base64.b64decode(pictures[0]["data"], validate=True)
                    if len(data) > 8 * 1024 * 1024:
                        raise ValueError("Preview image too large")
                    (output / "preview.png").write_bytes(data)
                    result["screenshot_sha256"] = hashlib.sha256(data).hexdigest()
            result["status"] = "failed" if errors else "passed"
            result["tool_errors"] = errors
        except Exception as exc:
            result["error"] = str(exc)[:4000]
        finally:
            if client.process:
                for name in ("get_debug_output", "stop_project"):
                    try:
                        reply = await client.tool(name, {"limit": 200} if name == "get_debug_output" else {})
                        result["observations"][name] = brief(reply)
                        if reply.get("isError"):
                            result["status"] = "failed"
                        if name == "get_debug_output" and re.search(r"SCRIPT ERROR:|Parse Error:|Shader compilation failed", json.dumps(reply)):
                            result["runtime_errors"] = True
                            result["status"] = "failed"
                    except Exception as exc:
                        result["status"] = "failed"
                        result["cleanup_error"] = str(exc)[:2000]
            await client.close()
            result["original_project_unchanged"] = tree_hash(source) == expected
            result["mcp_tool_calls"] = len(client.calls)
            if not result["original_project_unchanged"]:
                result["status"] = "failed"
            write_json(output / "result.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", default="/output/job.json")
    args = parser.parse_args()
    output = Path("/output/session")
    output.mkdir()
    job = json.loads(Path(args.job).read_text())
    asyncio.run(inspect(job, output))


if __name__ == "__main__":
    main()
