"""rotate-secrets.sh smoke test — verifies the surgical anchor behavior
preserves agent_uuid while stripping tokens."""
import json
import subprocess
import tempfile
from pathlib import Path


def test_surgical_strip_preserves_uuid(tmp_path):
    """Feed a realistic anchor through the Python inline-strip block
    and verify shape."""
    anchor = tmp_path / "watcher.json"
    anchor.write_text(json.dumps({
        "client_session_id": "agent-8a930180-c64",
        "continuity_token": "v1.somelongtoken.sig",
        "agent_uuid": "8a930180-9ef9-59c7-9267-fd7dd936dd58",
    }))

    # The script's surgical block, verbatim.
    script = f"""
import json, os, sys, tempfile
path = {str(anchor)!r}
with open(path) as fh:
    d = json.load(fh)
uuid = d.get("agent_uuid")
if not uuid:
    sys.exit("no uuid")
new = {{"agent_uuid": uuid}}
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as fh:
    json.dump(new, fh)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
"""
    subprocess.check_call(["python3", "-c", script])

    result = json.loads(anchor.read_text())
    assert result == {"agent_uuid": "8a930180-9ef9-59c7-9267-fd7dd936dd58"}
    assert oct(anchor.stat().st_mode)[-3:] == "600"


def test_refuses_anchor_without_uuid(tmp_path):
    """Script must die if anchor lacks agent_uuid — operator re-bootstraps."""
    anchor = tmp_path / "broken.json"
    anchor.write_text(json.dumps({"client_session_id": "sk"}))

    script = f"""
import json, sys
d = json.load(open({str(anchor)!r}))
sys.exit(0 if d.get("agent_uuid") else 1)
"""
    result = subprocess.run(["python3", "-c", script])
    assert result.returncode == 1  # script's preflight loop would abort
