#!/usr/bin/env python3
"""List installed harness skills without model calls; read a token at the skill path.

Run against a synced scratch HOME first. CLI startup writes only inside the
selected HOME except Claude's temporary config directory. Claude uses normal discovery; --bare suppresses user skill lists.
"""

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import select
import shutil
import subprocess
import sys
import tempfile
import time


class Protocol:
    def __init__(self, command, env, cwd):
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=self.errors, env=env, cwd=cwd)
        self.buffer = b""

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, predicate):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                value = json.loads(line)
                if predicate(value):
                    return value
                continue
            if select.select([self.process.stdout], [], [], 1)[0]:
                data = os.read(self.process.stdout.fileno(), 65536)
                if not data:
                    raise RuntimeError("harness exited before its discovery response")
                self.buffer += data
        raise RuntimeError("harness discovery timed out")

    def close(self):
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
        self.errors.close()


@contextmanager
def temporary_claude_config(home):
    with tempfile.TemporaryDirectory(prefix="skills-proof-claude-") as directory:
        config = Path(directory)
        (config / "skills").symlink_to(home / ".claude/skills")
        yield config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--cwd", type=Path, required=True)
    parser.add_argument("--skill", default="standards")
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    home, cwd = args.home.resolve(), args.cwd.resolve()
    env = {**os.environ, "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
           "PI_CODING_AGENT_DIR": str(home / ".pi/agent"),
           "PI_OFFLINE": "1"}
    env.pop("CLAUDECODE", None)
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    expected = {item.name for item in (home / ".agents/skills").iterdir()
                if (item / "SKILL.md").is_file()}
    report = {}

    def record(harness, names, path):
        missing = expected - set(names)
        if missing:
            raise RuntimeError(f"{harness}: missing skills {sorted(missing)}")
        if args.token not in path.read_text():
            raise RuntimeError(f"{harness}: overlay token absent at {path}")
        report[harness] = {"listed_skills": sorted(names), "overlay_path": str(path), "token_visible": True}

    codex = Protocol(["codex", "app-server"], env, cwd)
    try:
        codex.send({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "skills-proof", "version": "1"},
            "capabilities": {"experimentalApi": True}}})
        initialized = codex.receive(lambda value: value.get("id") == 1)
        if "error" in initialized:
            raise RuntimeError(initialized["error"])
        codex.send({"method": "initialized", "params": {}})
        codex.send({"id": 2, "method": "skills/list", "params": {"cwds": [str(cwd)], "forceReload": True}})
        result = codex.receive(lambda value: value.get("id") == 2)
        skills = result["result"]["data"][0]["skills"]
        selected = next(skill for skill in skills if skill["name"] == args.skill)
        record("Codex", [skill["name"] for skill in skills], Path(selected["path"]))
    finally:
        codex.close()

    with temporary_claude_config(home) as config:
        claude = Protocol(["claude", "--settings", '{"disableAllHooks":true}', "--print",
                           "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
                           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
                           "--setting-sources", "user", "--no-session-persistence"], {**env, "CLAUDE_CONFIG_DIR": str(config)}, cwd)
        try:
            claude.send({"type": "control_request", "request_id": "init-proof",
                         "request": {"subtype": "initialize"}})
            result = claude.receive(lambda value: value.get("type") == "control_response")
            commands = result["response"]["response"]["commands"]
            # Claude's command metadata reports user scope, without a file path.
            # Its user skill path is the configured ~/.claude/skills directory.
            record("Claude Code", [command["name"] for command in commands],
                   home / ".claude/skills" / args.skill / "SKILL.md")
        finally:
            claude.close()

    module = Path(shutil.which("pi")).resolve().parents[1] / "core/resource-loader.js"
    javascript = f'''
import {{DefaultResourceLoader}} from {json.dumps(module.as_uri())};
const loader=new DefaultResourceLoader({{cwd:{json.dumps(str(cwd))},
agentDir:{json.dumps(str(home / '.pi/agent'))},noExtensions:true,noThemes:true,
noPromptTemplates:true,noContextFiles:true}});
await loader.reload();
console.log(JSON.stringify(loader.getSkills()));
'''
    result = subprocess.run(["node", "--input-type=module"], input=javascript, text=True,
                            env=env, cwd=cwd, capture_output=True, check=True, timeout=30)
    skills = json.loads(result.stdout)["skills"]
    selected = next(skill for skill in skills if skill["name"] == args.skill)
    record("Pi", [skill["name"] for skill in skills], Path(selected["filePath"]))
    print(json.dumps(report, indent=2))
    print("PASS: all three harnesses list built skills; their skill files contain the overlay token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
