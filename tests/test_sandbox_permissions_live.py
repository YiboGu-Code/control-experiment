"""Opt-in POSIX UID/mode regression in a local Linux container; no online calls."""
import base64
import inspect
import io
import json
import os
from pathlib import Path
import subprocess

from PIL import Image
import pytest

from adaptive_game_generation.runtime.sandbox import OUTPUT_BOOTSTRAP, prepare_output_tree
from adaptive_game_generation.runtime.workspace import no_links, inspect_tree


@pytest.mark.live
@pytest.mark.skipif(os.getenv("GAMECRAFT_PERMISSION_ENGINE_TESTS") != "1",
                    reason="Opt in to local Docker POSIX permission reproduction")
def test_linux_foreign_uid_godot_import_and_host_cache_reuse(tmp_path):
    pixels = io.BytesIO()
    Image.new("RGBA", (8, 8), (30, 50, 70, 255)).save(pixels, "PNG")
    # Execute the actual permission implementation on a Linux tmpfs, not Windows
    # bind-mount emulation. No dependencies or container image changes are needed.
    source = "import os, stat, subprocess, json, base64\nfrom pathlib import Path\n"
    source += "class PermissionDenied(Exception): pass\nclass InvalidSubmission(Exception): pass\nclass SandboxFilesystemError(Exception): pass\n"
    source += "\n".join(inspect.getsource(fn) for fn in (no_links, inspect_tree, prepare_output_tree))
    source += f"\nBOOTSTRAP = {OUTPUT_BOOTSTRAP!r}\nPNG = {base64.b64encode(pixels.getvalue()).decode()!r}\n"
    source += r'''
root = Path('/output')
game = root / 'game'
(game / 'assets').mkdir(parents=True)
for directory in (game, game / 'assets'):
    directory.chmod(0o755)
(game / 'project.godot').write_text('config_version=5\n[application]\nrun/main_scene="res://Main.tscn"\n')
(game / 'Main.tscn').write_text('[gd_scene format=3]\n[node name="Main" type="Node2D"]\n')
(game / 'assets/hero.png').write_bytes(base64.b64decode(PNG))
# Existing host-owned import sidecars also need writes on the next import.
(game / 'assets/hero.png.import').write_text('')
(game / 'assets/hero.png.import').chmod(0o644)

def as_uid(uid, argv):
    def identity():
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    result = subprocess.run(argv, preexec_fn=identity, capture_output=True, text=True, timeout=60,
                            env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/tmp', 'XDG_CACHE_HOME': '/tmp/cache'})
    return {'returncode': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}

engine = ['godot', '--headless', '--path', '/output/game', '--import']
before = as_uid(65534, engine)
prepare_output_tree(root)
after = as_uid(65534, ['/bin/sh', '-c', BOOTSTRAP, 'gamecraft-output', *engine])
# A second, unrelated host UID must be able to update container-created caches.
cache = game / '.godot/imported'
reuse = as_uid(10001, ['python3', '-c', "from pathlib import Path; p=Path('/output/game/.godot/imported/host-check'); p.write_text('host write'); p.unlink()"])
prepare_output_tree(root)
again = as_uid(65534, ['/bin/sh', '-c', BOOTSTRAP, 'gamecraft-output', *engine])
literal = '$(touch /output/INTERPOLATED)'
forward = as_uid(65534, ['/bin/sh', '-c', BOOTSTRAP, 'gamecraft-output', 'python3', '-c',
    'import sys; print(sys.argv[1])', literal])
assert not (root / 'INTERPOLATED').exists()
print(json.dumps({'before': before, 'after': after, 'second_import': again, 'host_cache_write': reuse,
                  'literal_argument': forward, 'cache_mode': oct(stat.S_IMODE(cache.stat().st_mode))}))
'''
    # This is trusted test setup, with only setuid/setgid retained to reproduce a
    # host/container ownership mismatch. Godot always runs as unprivileged 65534.
    script = tmp_path / "probe.py"
    script.write_text(source, encoding="utf-8")
    image = os.getenv("GAMECRAFT_PERMISSION_IMAGE", "gamecraft-player:4.5.1")
    command = ["docker", "run", "--rm", "--pull=never", "--network=none", "--read-only", "--user", "0:0",
        "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID", "--security-opt=no-new-privileges",
        "--pids-limit", "128", "--memory", "1024m", "--cpus", "2",
        "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m,mode=1777",
        "--tmpfs", "/output:rw,nosuid,nodev,size=128m,mode=777",
        "--mount", f"type=bind,src={tmp_path},dst=/probe,readonly",
        "--entrypoint", "python3", image, "/probe/probe.py"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    if location := os.getenv("GAMECRAFT_PERMISSION_OUTPUT"):
        directory = Path(location)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "probe.py").write_text(source, encoding="utf-8")
        (directory / "process.json").write_text(json.dumps({"argv": command, "returncode": result.returncode,
            "stdout": result.stdout, "stderr": result.stderr}, indent=2), encoding="utf-8")
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert "Could not create directory" in data["before"]["stderr"]
    for name in ("after", "second_import"):
        assert data[name]["returncode"] == 0
        assert "ERROR:" not in data[name]["stderr"], data[name]
    assert data["host_cache_write"]["returncode"] == 0
    assert data["cache_mode"] == "0o777"
    assert data["literal_argument"]["stdout"].strip() == "$(touch /output/INTERPOLATED)"
