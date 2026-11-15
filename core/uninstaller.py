"""在安装目录外卸载发布版，记录残留并提供经过所有权校验的重试。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid

OWNER_FILE = ".desktop_pet_uninstall.json"


def _is_relative_to(path, parent):
    return path == parent or parent in path.parents


def _safe_root(value):
    original = Path(value).absolute()
    root = original.resolve()
    if root != original or len(root.parts) < 3 or root == Path(root.anchor):
        raise RuntimeError("卸载目录无效，不能是根目录或目录链接。")
    protected = [Path.home()]
    for name in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(name):
            p = Path(os.environ[name]).resolve()
            protected.append(p)
            if name != "LOCALAPPDATA" and _is_relative_to(root, p):
                raise RuntimeError("不能卸载系统目录中的文件。")
    if any(root == p or root in p.parents for p in protected):
        raise RuntimeError("不能卸载系统目录或用户目录。")
    if (root / ".git").exists() or (root / "main.py").exists():
        raise RuntimeError("不能卸载开发项目目录。")
    return root


def is_release_install(root):
    root = Path(root).resolve()
    try:
        config = json.loads((root / "config/app_config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return (root / "发布清单.txt").is_file() and isinstance(config, dict) and bool(config.get("release_template"))


def _state_dir():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share")) / "MyDesktopPet"


def external_work_directory(root):
    root = Path(root).resolve()
    for value in (tempfile.gettempdir(), _state_dir() / "Temp", Path.home() / "AppData/Local/Temp"):
        candidate = Path(value).resolve()
        if not _is_relative_to(candidate, root):
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
    raise RuntimeError("找不到安装目录以外的卸载工作目录。")


def _read_json(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def pending_uninstall():
    record = _read_json(_state_dir() / "uninstall_failed.json")
    try:
        token = record["token"]
        if not isinstance(token, str) or uuid.UUID(hex=token).hex != token:
            return None
        root = _safe_root(record["target"])
        job = _read_json(_state_dir() / (token + ".json"))
        if job != {"target": str(root), "token": token} or _read_json(root / OWNER_FILE) != job:
            return None
        return job
    except (KeyError, ValueError, TypeError, RuntimeError, OSError):
        return None


def dismiss_uninstall_failure():
    (_state_dir() / "uninstall_failed.json").unlink(missing_ok=True)


def check_startup_uninstall(current_root):
    """在加载角色、配置向导等窗口之前提示；残缺安装也能进入重试。"""
    if not pending_uninstall():
        return False
    import tkinter as tk
    parent = tk.Tk()
    parent.withdraw()
    exiting = []
    try:
        prompt_failed_uninstall(parent, current_root, lambda: exiting.append(True))
    finally:
        parent.destroy()
    return bool(exiting)


def prompt_failed_uninstall(parent, current_root, on_exit):
    from tkinter import messagebox
    record = pending_uninstall()
    if not record:
        return
    target = Path(record["target"])
    if not messagebox.askyesno("上次卸载未完成", "上次卸载未完成，残留目录：\n{}\n\n是否重试删除？".format(target), parent=parent):
        dismiss_uninstall_failure()
        return
    same = target == Path(current_root).resolve()
    try:
        schedule_release_uninstall(target, os.getpid() if same else 0, retry=record)
    except (OSError, RuntimeError) as error:
        messagebox.showerror("无法重试卸载", str(error), parent=parent)
        return
    if same:
        try:
            os.chdir(external_work_directory(target))
        except OSError:
            pass
        on_exit()


def schedule_release_uninstall(root, process_id, retry=None):
    root = _safe_root(root)
    if retry is None:
        if not is_release_install(root):
            raise RuntimeError("为保护开发文件，仅可卸载正式发布版。")
        record = {"target": str(root), "token": uuid.uuid4().hex}
    else:
        record = pending_uninstall()
        if not record or record != retry or record["target"] != str(root):
            raise RuntimeError("卸载重试记录与目标目录不匹配。")
    state = _state_dir().resolve()
    if _is_relative_to(state, root):
        raise RuntimeError("卸载日志目录不能位于待删除目录中。")
    state.mkdir(parents=True, exist_ok=True)
    workdir = external_work_directory(root)
    script_path = workdir / ("my_desktop_pet_uninstall_" + record["token"] + ".ps1")
    job_path = state / (record["token"] + ".json")
    payload = json.dumps(record)
    job_path.write_text(payload, encoding="utf-8")
    (root / OWNER_FILE).write_text(payload, encoding="utf-8")
    script_path.write_text(_UNINSTALL_SCRIPT, encoding="utf-8-sig")
    powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    command = [str(powershell if powershell.is_file() else "powershell.exe"),
               "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
               "-File", str(script_path), "-DesktopPetPid", str(int(process_id)), "-JobPath", str(job_path)]
    try:
        subprocess.Popen(command, cwd=str(workdir), creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        script_path.unlink(missing_ok=True)
        raise
    return script_path


_UNINSTALL_SCRIPT = r'''param([int]$DesktopPetPid, [string]$JobPath)
$ErrorActionPreference = "Stop"
$job = Get-Content -LiteralPath $JobPath -Raw | ConvertFrom-Json
$Target = [IO.Path]::GetFullPath($job.target).TrimEnd('\')
$state = Split-Path -Parent $JobPath
$log = Join-Path $state 'uninstall.log'
$failed = Join-Path $state 'uninstall_failed.json'
$ownerPath = Join-Path $Target '.desktop_pet_uninstall.json'
function Log([string]$message) {
    Add-Content -LiteralPath $log -Value ((Get-Date -Format o) + ' ' + $message) -Encoding UTF8
}
function AssertOwnedTarget {
    $owner = Get-Content -LiteralPath $ownerPath -Raw | ConvertFrom-Json
    if ($owner.target -ne $Target -or $owner.token -ne $job.token) { throw 'Ownership mismatch' }
    # Reject linked ancestors too; never traverse junctions during deletion.
    $ancestor = Get-Item -LiteralPath $Target -Force
    while ($ancestor) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Target is a link' }
        $ancestor = $ancestor.Parent
    }
}
try {
    Set-Location -LiteralPath (Split-Path -Parent $PSCommandPath)
    AssertOwnedTarget
    Log ("BEGIN target=" + $Target + " cwd=" + (Get-Location).Path)
    if ($DesktopPetPid -gt 0) { Wait-Process -Id $DesktopPetPid -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    for ($attempt = 0; $attempt -lt 3; $attempt++) {
        AssertOwnedTarget
        $failures = [Collections.Generic.List[object]]::new()
        $dirs = [Collections.Generic.List[string]]::new()
        $pending = [Collections.Generic.Stack[string]]::new()
        $pending.Push($Target)
        while ($pending.Count -gt 0) {
            $folder = $pending.Pop()
            try {
                if ((Get-Item -LiteralPath $folder -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                    throw 'Directory changed to a link'
                }
                $items = @(Get-ChildItem -LiteralPath $folder -Force -ErrorAction Stop)
            }
            catch { $failures.Add(@{path=$folder; error=$_.Exception.Message}); continue }
            foreach ($item in $items) {
                if ($item.FullName -eq $ownerPath) { continue }
                if ($item.PSIsContainer -and -not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                    $dirs.Add($item.FullName)
                    $pending.Push($item.FullName)
                } else {
                    try {
                        if ($item.PSIsContainer) { [IO.Directory]::Delete($item.FullName) }
                        else { Remove-Item -LiteralPath $item.FullName -Force -ErrorAction Stop }
                        Log ("DELETED " + $item.FullName)
                    } catch {
                        $failures.Add(@{path=$item.FullName; error=$_.Exception.Message})
                        Log ("FAILED " + $item.FullName + " " + $_.Exception.Message)
                    }
                }
            }
        }
        foreach ($folder in ($dirs | Sort-Object Length -Descending)) {
            try { [IO.Directory]::Delete($folder); Log ("DELETED " + $folder) }
            catch { $failures.Add(@{path=$folder; error=$_.Exception.Message}) }
        }
        try {
            if (@(Get-ChildItem -LiteralPath $Target -Force | Where-Object FullName -ne $ownerPath).Count -eq 0) {
                Remove-Item -LiteralPath $ownerPath -Force
                try { [IO.Directory]::Delete($Target) }
                catch { $job | ConvertTo-Json | Set-Content -LiteralPath $ownerPath -Encoding UTF8; throw }
            }
        } catch { $failures.Add(@{path=$Target; error=$_.Exception.Message}) }
        if (-not (Test-Path -LiteralPath $Target)) { break }
        if ($attempt -lt 2) { Start-Sleep -Milliseconds 500 }
    }
    if (Test-Path -LiteralPath $Target) {
        @{target=$Target; token=$job.token; failures=@($failures.ToArray())} |
            ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $failed -Encoding UTF8
        Log ("INCOMPLETE " + ($failures.ToArray() | ConvertTo-Json -Compress -Depth 4))
    } else {
        $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Software\我的桌宠', $true)
        if ($key) {
            try {
                $installed = $key.GetValue('InstallPath')
                if ($installed -and [IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($installed)) -eq $Target) {
                    $key.DeleteValue('InstallPath', $false)
                }
            } finally { $key.Close() }
        }
        if (Test-Path -LiteralPath $failed) {
            $old = Get-Content -LiteralPath $failed -Raw | ConvertFrom-Json
            if ($old.token -eq $job.token) { Remove-Item -LiteralPath $failed -Force }
        }
        Remove-Item -LiteralPath $JobPath -Force
        Log ("SUCCESS " + $Target)
    }
} catch {
    $reason = $_.Exception.Message
    Log ("ERROR " + $reason)
    if (Test-Path -LiteralPath $Target) {
        @{target=$Target; token=$job.token; failures=@(@{path=$Target; error=$reason})} |
            ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $failed -Encoding UTF8
    }
    exit 1
}
finally { Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue }
'''
