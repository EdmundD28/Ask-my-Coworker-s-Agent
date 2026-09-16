"""Export the canonical manual, never a standalone entry credential."""
from pathlib import Path
from .agent_guide import GUIDE


def write_handbook(output, *, package_directory, invite_code=None):
    if invite_code:
        raise ValueError("请通过界面生成完整邀请码，不能用手册代替目标凭证")
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    package = str(Path(package_directory).resolve()).replace("'", "''")
    output.write_text("本文件是操作手册，不是配置码。先双击程序，在界面生成配置码或邀请码。\n"
                      + f"PowerShell 程序目录：'{package}'\n\n" + GUIDE, encoding="utf-8")
    return output
