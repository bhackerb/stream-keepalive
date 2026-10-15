"""KDE Wayland placement via a temporary KWin script and a private D-Bus reply."""
import asyncio
import json
import shutil
import tempfile
import uuid
from pathlib import Path


def script_source(plans, service, dry_run=False, borderless=None):
    return """
    const plans = %s;
    const windows = workspace.windowList();
    const selected = plans.map(p => windows.filter(w => w.caption.includes(p.marker)));
    let result;
    if (selected.some(matches => matches.length !== 1) || new Set(selected.map(m => m[0])).size !== selected.length) {
        result = {success:false, message:'Streams must occupy distinct identifiable windows'};
    } else {
        if (!%s) selected.forEach((matches, i) => {
            const w = matches[0]; const p = plans[i];
            w.fullScreen = false; w.setMaximize(false, false);
            const borderless = %s;
            if (borderless !== null) w.noBorder = borderless;
            w.frameGeometry = {x:p.x, y:p.y, width:p.width, height:p.height};
        });
        result = {success:true, windows:selected.map((m, i) => ({
            marker:plans[i].marker, borderless:m[0].noBorder, geometry:{x:m[0].frameGeometry.x, y:m[0].frameGeometry.y,
            width:m[0].frameGeometry.width, height:m[0].frameGeometry.height}
        }))};
    }
    callDBus(%s, '/Result', 'org.streamkeeper.Layout', 'Report', JSON.stringify(result));
    """ % (json.dumps(plans), json.dumps(dry_run), json.dumps(borderless), json.dumps(service))


async def run_script(plans, dry_run=False, borderless=None):
    from dbus_next.aio import MessageBus
    from dbus_next.service import ServiceInterface, method
    command = shutil.which("qdbus6") or shutil.which("qdbus")
    if not command:
        raise ValueError("KDE Wayland placement requires qdbus6")
    future = asyncio.get_running_loop().create_future()
    class Result(ServiceInterface):
        def __init__(self):
            super().__init__("org.streamkeeper.Layout")

        @method()
        def Report(self, value: 's'):
            if not future.done():
                try:
                    future.set_result(json.loads(value))
                except Exception as exc:
                    future.set_exception(exc)
    service = "org.streamkeeper.Layout.r" + uuid.uuid4().hex
    bus = await MessageBus().connect()
    plugin = "streamkeeper-" + uuid.uuid4().hex
    async def qdbus(*args):
        proc = await asyncio.create_subprocess_exec(command, "org.kde.KWin", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 5)
        except BaseException:
            proc.kill(); await proc.wait(); raise
        if proc.returncode:
            raise ValueError("KWin scripting unavailable: " + stderr.decode()[:300])
        return stdout.decode().strip()
    loaded = False
    try:
        await bus.request_name(service)
        bus.export("/Result", Result())
        with tempfile.TemporaryDirectory(prefix="streamkeeper-kwin-") as directory:
            path = Path(directory) / "layout.js"
            path.write_text(script_source(plans, service, dry_run, borderless))
            script_id = await qdbus("/Scripting", "org.kde.kwin.Scripting.loadScript", str(path), plugin)
            if not script_id.isdigit():
                raise ValueError("KWin refused the temporary placement script")
            loaded = True
            await qdbus("/Scripting/Script" + script_id, "org.kde.kwin.Script.run")
            return await asyncio.wait_for(future, 5)
    finally:
        try:
            if loaded:
                await qdbus("/Scripting", "org.kde.kwin.Scripting.unloadScript", plugin)
        finally:
            bus.disconnect()


async def arrange_windows(streams, placements, borderless=None):
    tagged = []
    try:
        plans = []
        for placement in placements:
            page = streams[placement["stream_id"]].page
            title = await page.title()
            marker = "streamkeeper-" + uuid.uuid4().hex
            tagged.append((page, title))
            await page.evaluate("marker => document.title = marker", marker)
            plans.append({**placement, "marker": marker})
        # Give the compositor time to receive browser caption updates.
        await asyncio.sleep(0.2)
        result = await run_script(plans, dry_run=True)
        if not result["success"]:
            return result
        result = await run_script(plans, borderless=borderless)
        if not result["success"]:
            return result
        await asyncio.sleep(0.2)
        measured = await run_script(plans, dry_run=True)
        if not measured["success"]:
            return measured
        matches = all(all(abs(window["geometry"][key] - plan[key]) <= 2 for key in ("x", "y", "width", "height"))
                      for window, plan in zip(measured["windows"], plans))
        if borderless is not None:matches=matches and all(w.get('borderless') is borderless for w in measured['windows'])
        return {"success": matches, "message": "KWin placement verified" if matches else "KWin constrained one or more windows; requested layout was not achieved",
                "windows": measured["windows"]}
    finally:
        for page, title in tagged:
            try:
                await page.evaluate("title => document.title = title", title)
            except Exception:
                pass
