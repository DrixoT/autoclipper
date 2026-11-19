"""Tokens per metadata call (gate G7, a measurement tool, not the daemon). Usage: python tools/llm_cost.py <dir with prompt.txt and f1..f4.jpg> <luna|plus|haiku> <n>"""
import json, os, subprocess, sys, time
from pathlib import Path


def main() -> None:
    d, who, n = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    prompt = (d / "prompt.txt").read_text()
    frames = [str(d / f"f{i}.jpg") for i in (1, 2, 3, 4)]
    home = os.path.expanduser
    if who in ("luna", "plus"):
        codex_home = "~/.codex" if who == "luna" else os.environ.get("CODEX_HOME_2", "~/.codex")
        env = {**os.environ, "CODEX_HOME": home(codex_home)}
        argv = ["codex", "exec", "-m", "gpt-6-luna", "-c", "model_reasoning_effort=low", "-s", "read-only",
                "--skip-git-repo-check", "--ephemeral", "--json"] + [x for f in frames for x in ("-i", f)] + ["-"]
    else:
        env = dict(os.environ)
        argv = ["claude", "-p", "--model", "claude-haiku-5-5", "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
                "--setting-sources", "", "--system-prompt", "You write YouTube metadata as strict JSON.", "--output-format", "json"]
    for i in range(n):
        t = time.time()
        p = subprocess.run(argv, input=prompt, env=env, cwd=d, capture_output=True, text=True, timeout=180)
        dt = time.time() - t
        usage, text = None, None
        for line in p.stdout.splitlines():
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if isinstance(o, dict):
                if "usage" in o: usage = o["usage"]
                if o.get("type") == "item.completed" and o.get("item", {}).get("type") == "agent_message": text = o["item"].get("text")
                if "result" in o: text = o["result"]; usage = {**(o.get("usage") or {}), "total_cost_usd": o.get("total_cost_usd")}
        print(f"[{who} #{i + 1}] rc={p.returncode} {dt:.1f}s usage={json.dumps(usage)}")
        print("   reply:", (text or p.stdout[-300:] or p.stderr[-300:]).replace("\n", " ")[:330])


if __name__ == "__main__":
    main()
