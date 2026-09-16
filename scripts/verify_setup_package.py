"""Exercise the shipped executable without installation, registration or a tunnel."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen
from pathlib import Path


def main():
    executable = Path(sys.argv[1]).resolve()
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    checks = []
    with tempfile.TemporaryDirectory(prefix="ask-my-coworkers-agent-package-") as temporary:
        root = Path(temporary)
        data = root / "isolated-state"

        def run(*args, expected=0):
            completed = subprocess.run([str(executable), "--data-dir", str(data), *args],
                                       capture_output=True, text=True, encoding="utf-8", timeout=45,
                                       creationflags=creationflags)
            if completed.returncode != expected:
                raise AssertionError((args, completed.returncode, completed.stdout, completed.stderr))
            return json.loads(completed.stdout)

        requirements = run("describe")
        assert requirements["product"] == "Ask My Coworker's Agent"
        assert not data.exists()
        checks.append("executable_describes_contract_without_writing_state")
        assert run("status")["status"] == "not_configured"
        assert not data.exists()
        checks.append("unconfigured_status_is_read_only")
        value = requirements["template"]
        value["space_name"] = "Isolated package verification"
        value["card"] = {"role_name": "Fixture only", "description": "No real user data", "owner_claim": "Unverified fixture"}
        draft = root / "draft.json"
        draft.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        valid = run("validate", "--file", str(draft))
        assert valid["status"] == "valid" and not data.exists()
        checks.append("validation_does_not_install_or_connect")
        denied = run("apply", "--file", str(draft), "--confirmed-digest", "wrong", expected=1)
        assert denied["status"] == "error" and not data.exists()
        checks.append("changed_confirmation_rejected_before_write")
        applied = run("apply", "--file", str(draft), "--confirmed-digest", valid["config_digest"])
        assert applied["status"] == "configured"
        observed = run("status")
        assert observed["configuration"] == value
        assert observed["config_digest"] == valid["config_digest"]
        assert not observed["connected"] and not observed["automatic_answers_enabled"]
        assert not (data / "credentials.json").exists() and not (data / "session.dpapi").exists()
        checks.append("confirmed_configuration_roundtrips_without_network_identity")
        manual = root / "handbook.md"
        run("handbook", "--output", str(manual), "--package-dir", str(executable.parent))
        content = manual.read_text(encoding="utf-8")
        assert "必须询问当前用户" in content and "AMCA" not in content
        checks.append("packaged_handbook_generator_is_self_contained")
        guide = run("guide")
        assert guide["manual_version"] == 4 and "已有权限保持原样" in guide["manual"]
        checks.append("versioned_builtin_guide_is_utf8")
        hub_process = subprocess.Popen([str(executable), "ui", "--registry-dir", str(root / "hub"), "--no-open"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", creationflags=creationflags)
        hub_url = None
        try:
            line = hub_process.stdout.readline().strip()
            assert line.startswith("UI_URL="), line
            hub_url = line.removeprefix("UI_URL=")
            parsed = urlparse(hub_url)
            token = parse_qs(parsed.fragment)["token"][0]
            def hub_action(payload):
                request = Request(f"http://{parsed.netloc}/api/hub/action", data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-Hub-Token": token})
                with urlopen(request, timeout=10) as response:
                    return json.load(response)
            issued = hub_action({"action": "create", "space_name": "打包程序中文验证"})
            assert "本机程序位置：" + str(executable) in issued["code"]
            checks.append("configuration_code_uses_this_devices_running_executable")
            code_file = root / "entry.txt"
            code_file.write_text(issued["code"], encoding="utf-8")
            claimed = run("accept", "--file", str(code_file), "--label", "独立验证对话")
            assert claimed["status"] == "session_claimed" and not claimed["profile_reused"]
            assert run("accept", "--file", str(code_file), "--label", "重复领取")["status"] == "already_claimed"
            checks.append("packaged_ui_issues_single_use_entry_with_unique_local_target")
            data = Path(claimed["data_dir"])
            value = claimed["template"]
            value["card"] = {"role_name": "中文测试助手", "description": "仅用于独立测试", "owner_claim": "测试声明"}
            draft.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            valid = run("validate", "--file", str(draft))
            run("apply", "--file", str(draft), "--confirmed-digest", valid["config_digest"])
            assert run("status")["binding"]["session_bound"]
            issued_again = hub_action({"action": "create", "space_name": "第二个测试空间"})
            code_file.write_text(issued_again["code"], encoding="utf-8")
            again = run("accept", "--file", str(code_file), "--label", "下一段对话")
            assert again["profile_reused"] and again["template"]["card"] == value["card"]
            assert again["template"]["policy"]["disclosure"] == {"allow": [], "deny": []}
            assert again["data_dir"] != claimed["data_dir"]
            checks.append("packaged_second_space_reuses_persona_without_inheriting_grants")
        finally:
            if hub_url:
                hub_action({"action": "close"})
            hub_process.wait(timeout=15)
    receipt = {"product": "Ask My Coworker's Agent", "executable": str(executable),
               "sha256": hashlib.sha256(executable.read_bytes()).hexdigest(), "checks": checks,
               "installed": False, "real_registration_performed": False, "public_tunnel_started": False,
               "boundary": "Offline packaged CLI acceptance only; real new-conversation onboarding is user-tested."}
    output = executable.parents[2] / "build" / "setup-package-verification.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
