"""Isolated Chrome UI/UTF-8 acceptance; does not launch any public node."""
import asyncio
import base64
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from websockets.asyncio.client import connect


async def verify(url, output, fixture=False, snapshot=False):
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ask-my-coworkers-agent-browser-", ignore_cleanup_errors=True) as temporary:
        profile = Path(temporary)
        process = subprocess.Popen([
            "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe", "--headless=new", "--disable-gpu", "--no-sandbox",
            "--no-first-run", "--no-default-browser-check", "--remote-debugging-port=0",
            "--user-data-dir=" + str(profile), "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            for _ in range(100):
                if (profile / "DevToolsActivePort").exists():
                    break
                await asyncio.sleep(0.1)
            port = (profile / "DevToolsActivePort").read_text().splitlines()[0]
            with urlopen(f"http://127.0.0.1:{port}/json/list") as response:
                target = next(x for x in json.load(response) if x["type"] == "page")
            async with connect(target["webSocketDebuggerUrl"], max_size=8 * 1024 * 1024, proxy=None) as ws:
                sequence = 0
                errors = []
                async def call(method, params=None):
                    nonlocal sequence
                    sequence += 1
                    request_id = sequence
                    await ws.send(json.dumps({"id": request_id, "method": method, "params": params or {}}))
                    while True:
                        message = json.loads(await asyncio.wait_for(ws.recv(), timeout=15))
                        if message.get("method") == "Runtime.exceptionThrown":
                            errors.append(message["params"])
                        if message.get("id") == request_id:
                            if "error" in message:
                                raise AssertionError(message["error"])
                            return message.get("result", {})
                async def evaluate(expression):
                    result = await call("Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True})
                    if "exceptionDetails" in result:
                        raise AssertionError(result["exceptionDetails"])
                    return result.get("result", {}).get("value")
                async def wait(expression):
                    for _ in range(80):
                        if await evaluate(expression):
                            return
                        await asyncio.sleep(0.1)
                    raise AssertionError("UI condition not reached: " + expression)
                await call("Page.enable")
                await call("Runtime.enable")
                await call("Emulation.setDeviceMetricsOverride", {"width": 1365, "height": 960, "deviceScaleFactor": 1, "mobile": False})
                await call("Page.navigate", {"url": url})
                await wait("document.getElementById('spaceSelect') && !document.getElementById('spaceSelect').textContent.includes('正在读取')")
                assert await evaluate("document.title") == "Ask My Coworker's Agent"
                if snapshot:
                    await wait("document.getElementById('identity').textContent.length > 0")
                    await wait("document.getElementById('members').textContent.length > 0")
                    assert await evaluate("document.documentElement.scrollWidth <= innerWidth")
                    shot = await call("Page.captureScreenshot", {"format": "png"})
                    (output / "live-workspace.png").write_bytes(base64.b64decode(shot["data"]))
                    print(json.dumps({"live_space": await evaluate("document.getElementById('spaceStatus').textContent"), "identity": await evaluate("document.getElementById('identity').textContent"), "javascript_errors": errors}, ensure_ascii=False))
                    assert not errors
                    await call("Browser.close")
                    return
                if fixture:
                    await wait("document.querySelectorAll('#members button').length > 0")
                    await evaluate("document.querySelector('#members button').click()")
                    await wait("document.querySelectorAll('.bubble').length > 0")
                    before = await evaluate("document.getElementById('messages').innerText")
                    await evaluate("document.getElementById('question').value='统一界面发送的合成问题';document.getElementById('composer').requestSubmit()")
                    await wait("document.getElementById('messages').textContent.includes('统一界面发送的合成问题')")
                    await evaluate("document.getElementById('spaceSelect').selectedIndex=1;document.getElementById('spaceSelect').dispatchEvent(new Event('change'))")
                    await wait("document.querySelectorAll('#members button').length > 0")
                    await evaluate("document.querySelector('#members button').click()")
                    await wait("document.querySelectorAll('.bubble').length > 0 && !document.getElementById('messages').textContent.includes('统一界面发送的合成问题')")
                    assert before != await evaluate("document.getElementById('messages').innerText")
                    await evaluate("document.getElementById('pending').click()")
                    await wait("document.getElementById('approvalDialog').open")
                    await evaluate("document.querySelector('#approvals button').click()")
                    await wait("!document.querySelector('#approvals button')")
                    await evaluate("document.getElementById('approvalDialog').close();document.getElementById('invite').click()")
                    await wait("document.getElementById('codeDialog').open")
                    assert '合成邀请' in await evaluate("document.getElementById('code').value")
                    await evaluate("document.getElementById('codeDialog').close()")
                body = await evaluate("document.body.innerText")
                assert "我的对话" in body and "\ufffd" not in body
                await evaluate("document.getElementById('newSpace').click()")
                assert await evaluate("document.documentElement.scrollWidth <= innerWidth")
                await evaluate("document.getElementById('spaceName').focus()")
                await call("Input.insertText", {"text": "中文编码验收空间"})
                await evaluate("document.getElementById('create').click()")
                await wait("document.getElementById('code').value.includes('中文编码验收空间')")
                code = await evaluate("document.getElementById('code').value")
                assert "ASK-MY-COWORKERS-AGENT-ENTRY-BEGIN" in code and "\ufffd" not in code
                await wait("document.getElementById('spaceSelect').textContent.includes('中文编码验收空间')")
                await call("Browser.grantPermissions", {"origin": url.split('/#')[0], "permissions": ["clipboardReadWrite", "clipboardSanitizedWrite"]})
                await evaluate("document.getElementById('copy').scrollIntoView({block:'center'})")
                box = await evaluate("(()=>{const r=document.getElementById('copy').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}})()")
                await call("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, **box})
                await call("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, **box})
                await wait("document.getElementById('notice').textContent.includes('已复制')")
                clipboard = await evaluate("navigator.clipboard.readText()")
                assert clipboard.replace("\r\n", "\n") == code.replace("\r\n", "\n")
                downloads = profile / "downloads"
                downloads.mkdir()
                await call("Browser.setDownloadBehavior", {"behavior": "allow", "downloadPath": str(downloads)})
                await evaluate("document.getElementById('download').click()")
                for _ in range(50):
                    files = list(downloads.glob("*.txt"))
                    if files:
                        break
                    await asyncio.sleep(0.1)
                assert files and files[0].read_text(encoding="utf-8").replace("\r\n", "\n") == code.replace("\r\n", "\n")
                await evaluate("document.getElementById('codeDialog').close();window.scrollTo(0,0)")
                screenshot = await call("Page.captureScreenshot", {"format": "png"})
                (output / "hub-desktop.png").write_bytes(base64.b64decode(screenshot["data"]))
                await call("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True})
                await evaluate("window.scrollTo(0,0)")
                assert await evaluate("document.documentElement.scrollWidth <= innerWidth")
                screenshot = await call("Page.captureScreenshot", {"format": "png"})
                (output / "hub-mobile.png").write_bytes(base64.b64decode(screenshot["data"]))
                assert not errors, errors
                receipt = {"ui_loaded": True, "chinese_rendered": True, "unicode_replacement_characters": False,
                           "ui_generated_unique_code": True, "clipboard_unicode_text_preserved": True,
                           "downloaded_code_is_valid_utf8": True,
                           "desktop_and_mobile_no_horizontal_overflow": True, "javascript_errors": errors,
                           "unified_chat_send_switch_approve_invite": fixture, "network_registration": False, "screenshots": ["hub-desktop.png", "hub-mobile.png"]}
                (output / "browser-receipt.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps(receipt, ensure_ascii=False))
                await call("Browser.close")
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait(timeout=10)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    asyncio.run(verify(sys.argv[1], Path(sys.argv[2]), len(sys.argv) > 3 and sys.argv[3] == "fixture", len(sys.argv) > 3 and sys.argv[3] == "snapshot"))
