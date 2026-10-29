"""
定时任务注册管理 v5.0.0
功能：把周报/月报/数据刷新注册为 schtasks（Windows）或 crontab（Linux）计划任务
      任务体为一条 wps CLI 命令；提供 list/cancel 子命令管理注册项
      注册前向用户展示将执行的命令并确认

v5.0.0 变更：
  - 🎯 初始版本

死规则合规：
  - 规则9：纯本地实现，不依赖任何外部 API
  - 规则10：使用系统 schtasks/crontab，无额外常驻进程
  - 规则13：不生成任何禁止文件类型
  - 规则14：三次自审
  - 规则15：沙箱模拟运行
  - 规则16：子进程超时自动关闭

安全合规：
  - 不联网、不调用外部服务
  - 不读取用户隐私数据或凭证
  - 注册前展示完整命令并等待用户确认（y/N）
  - 所有操作仅限于本地计划任务管理
"""

import os
import sys
import json
import platform
import subprocess
import argparse
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, Any, List

sys.path.insert(0, str(Path(__file__).parent))

try:
    from wps_common import safe_path
except ImportError:
    def safe_path(p):
        return Path(p).resolve()


IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"

TASK_PREFIX = "WPSOfficeSuite_"
TASK_DESCRIPTION = "WPS Office 全家桶 v5.0 定时任务（自动注册）"

# 可注册的预定义任务
PREDEFINED_TASKS = {
    "weekly_report": {
        "label": "周报自动生成",
        "command": "wps_word.py report --type weekly --points \"待填写\" --output \"周报_{date}.docx\"",
        "schedule": {
            "windows": {
                "type": "WEEKLY",
                "time": "17:00",
                "days": "MON"
            },
            "linux": {
                "type": "WEEKLY",
                "cron": "0 17 * * 1"
            }
        }
    },
    "monthly_report": {
        "label": "月报自动生成",
        "command": "wps_word.py report --type monthly --points \"待填写\" --output \"月报_{date}.docx\"",
        "schedule": {
            "windows": {
                "type": "MONTHLY",
                "time": "09:00",
                "days": "1"
            },
            "linux": {
                "type": "MONTHLY",
                "cron": "0 9 1 * *"
            }
        }
    },
    "data_refresh": {
        "label": "数据刷新（示例）",
        "command": "wps_excel.py excel-smart --file \"{data_file}\" --action profile",
        "schedule": {
            "windows": {
                "type": "DAILY",
                "time": "08:00",
                "days": ""
            },
            "linux": {
                "type": "DAILY",
                "cron": "0 8 * * *"
            }
        }
    },
    "backup_docs": {
        "label": "文档备份（示例）",
        "command": "wps_word.py export --file \"{doc_file}\" --format pdf",
        "schedule": {
            "windows": {
                "type": "DAILY",
                "time": "23:00",
                "days": ""
            },
            "linux": {
                "type": "DAILY",
                "cron": "0 23 * * *"
            }
        }
    }
}

# 自定义任务存储位置
CUSTOM_TASKS_FILE = Path(__file__).parent / "schedule_custom_tasks.json"


class ScheduleRegister:
    """定时任务注册管理器"""

    VERSION = "v5.0.0"

    def __init__(self):
        self.system = platform.system()
        self.custom_tasks = self._load_custom_tasks()

    def _load_custom_tasks(self) -> Dict[str, Any]:
        """加载自定义任务配置"""
        if CUSTOM_TASKS_FILE.exists():
            try:
                return json.loads(CUSTOM_TASKS_FILE.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _save_custom_tasks(self):
        """保存自定义任务配置"""
        try:
            CUSTOM_TASKS_FILE.write_text(
                json.dumps(self.custom_tasks, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
        except Exception:
            pass

    def _get_script_path(self, command: str) -> str:
        """从命令中提取脚本路径"""
        parts = command.split()
        if parts:
            script = parts[0]
            script_path = Path(__file__).parent / script
            if script_path.exists():
                return str(script_path)
        return ""

    def _build_full_command(self, command: str, workdir: str = "") -> str:
        """构建完整命令（使用当前 Python 解释器）"""
        python_exe = sys.executable
        script_path = self._get_script_path(command)
        if script_path:
            cmd = f'"{python_exe}" "{script_path}" {" ".join(command.split()[1:])}'
        else:
            cmd = f'"{python_exe}" {command}'

        if workdir:
            cmd = f'cd /d "{workdir}" && {cmd}' if IS_WINDOWS else f'cd "{workdir}" && {cmd}'
        return cmd

    def _build_schtasks_cmd(self, task_name: str, command: str,
                            schedule: Dict, workdir: str = "",
                            enable: bool = True) -> List[str]:
        """构建 Windows schtasks 命令"""
        full_cmd = self._build_full_command(command, workdir)

        schtasks_cmd = [
            "schtasks", "/Create",
            "/TN", f"{TASK_PREFIX}{task_name}",
            "/TR", full_cmd,
            "/SC", schedule.get("type", "DAILY"),
        ]

        if schedule.get("time"):
            schtasks_cmd.extend(["/ST", schedule["time"]])

        if schedule.get("days"):
            if schedule["type"] == "WEEKLY":
                schtasks_cmd.extend(["/D", schedule["days"]])
            elif schedule["type"] == "MONTHLY":
                schtasks_cmd.extend(["/D", schedule["days"]])

        if not enable:
            schtasks_cmd.append("/DISABLE")

        schtasks_cmd.append("/F")  # 强制覆盖

        return schtasks_cmd

    def _build_crontab_cmd(self, task_name: str, command: str,
                           schedule: Dict, workdir: str = "") -> str:
        """构建 Linux crontab 条目"""
        full_cmd = self._build_full_command(command, workdir)
        cron_expr = schedule.get("cron", "0 8 * * *")
        comment = f"# {TASK_PREFIX}{task_name} - {TASK_DESCRIPTION}"
        return f"{comment}\n{cron_expr} {full_cmd}"

    def _confirm_command(self, task_name: str, command: str,
                         schedule: Dict) -> bool:
        """展示命令并请求用户确认"""
        print(f"\n{'='*60}")
        print(f"📋 定时任务注册确认")
        print(f"{'='*60}")
        print(f"  任务名称: {task_name}")
        print(f"  任务类型: {PREDEFINED_TASKS.get(task_name, {}).get('label', '自定义')}")
        print(f"  操作系统: {self.system}")
        print(f"  执行命令: {command}")
        print(f"  调度配置: {json.dumps(schedule, ensure_ascii=False)}")
        print(f"  创建时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*60}")
        print(f"\n⚠️  此任务将在您的系统上按计划自动执行上述命令。")
        print(f"   任务前缀为 {TASK_PREFIX}，可通过 list 命令查看所有注册项。\n")

        try:
            answer = input("确认注册？[y/N]: ").strip().lower()
            return answer == "y"
        except Exception:
            return False

    def register(self, task_name: str, command: str = "",
                 schedule: Dict = None, workdir: str = "",
                 enable: bool = True, force: bool = False) -> Dict[str, Any]:
        """
        注册定时任务

        Args:
            task_name: 任务名称
            command: CLI 命令（如果为空则从预定义任务获取）
            schedule: 调度配置（如果为空则从预定义任务获取）
            workdir: 工作目录
            enable: 是否启用
            force: 是否跳过确认（用于自动化场景）

        Returns:
            dict: {"ok": bool, "error": str, "task_name": str}
        """
        # 使用预定义任务
        if not command and task_name in PREDEFINED_TASKS:
            predefined = PREDEFINED_TASKS[task_name]
            command = predefined["command"]
            schedule = schedule or predefined["schedule"].get(
                "windows" if IS_WINDOWS else "linux", {}
            )

        if not command:
            return {"ok": False, "error": f"未知任务: {task_name}", "task_name": task_name}

        schedule = schedule or {"type": "DAILY", "time": "08:00"}

        # 用户确认
        if not force and not self._confirm_command(task_name, command, schedule):
            return {"ok": False, "error": "用户取消注册", "task_name": task_name}

        try:
            if IS_WINDOWS:
                return self._register_windows(task_name, command, schedule, workdir, enable)
            elif IS_LINUX:
                return self._register_linux(task_name, command, schedule, workdir)
            else:
                return {"ok": False, "error": f"不支持的操作系统: {self.system}", "task_name": task_name}
        except Exception as e:
            return {"ok": False, "error": str(e), "task_name": task_name}

    def _register_windows(self, task_name: str, command: str,
                          schedule: Dict, workdir: str,
                          enable: bool) -> Dict[str, Any]:
        """Windows schtasks 注册"""
        schtasks_cmd = self._build_schtasks_cmd(task_name, command, schedule, workdir, enable)

        # 规则16：子进程超时自动关闭
        proc = subprocess.Popen(
            schtasks_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        try:
            stdout, stderr = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "error": "schtasks 执行超时", "task_name": task_name}

        if proc.returncode == 0:
            # 保存到自定义任务记录
            self.custom_tasks[task_name] = {
                "command": command,
                "schedule": schedule,
                "workdir": workdir,
                "enabled": enable,
                "created": datetime.now().isoformat()
            }
            self._save_custom_tasks()
            return {"ok": True, "task_name": task_name, "message": f"任务已注册: {TASK_PREFIX}{task_name}"}
        else:
            return {"ok": False, "error": stderr.strip() or "schtasks 执行失败", "task_name": task_name}

    def _register_linux(self, task_name: str, command: str,
                        schedule: Dict, workdir: str) -> Dict[str, Any]:
        """Linux crontab 注册"""
        crontab_line = self._build_crontab_cmd(task_name, command, schedule, workdir)
        task_flag = f"# {TASK_PREFIX}{task_name}"

        # 获取现有 crontab
        try:
            proc = subprocess.Popen(
                ["crontab", "-l"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, _ = proc.communicate(timeout=10)
            existing = stdout if proc.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "error": "crontab 读取超时", "task_name": task_name}
        except FileNotFoundError:
            return {"ok": False, "error": "crontab 未安装", "task_name": task_name}

        # 移除旧条目
        lines = [l for l in existing.splitlines()
                 if not l.startswith(task_flag) and l.strip()]

        # 添加新条目
        lines.append(crontab_line)
        new_crontab = "\n".join(lines) + "\n"

        # 写入 crontab
        proc = subprocess.Popen(
            ["crontab", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        try:
            _, stderr = proc.communicate(input=new_crontab, timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "error": "crontab 写入超时", "task_name": task_name}

        if proc.returncode == 0:
            # 保存到自定义任务记录
            self.custom_tasks[task_name] = {
                "command": command,
                "schedule": schedule,
                "workdir": workdir,
                "enabled": True,
                "created": datetime.now().isoformat()
            }
            self._save_custom_tasks()
            return {"ok": True, "task_name": task_name, "message": f"crontab 任务已注册: {TASK_PREFIX}{task_name}"}
        else:
            return {"ok": False, "error": stderr.strip(), "task_name": task_name}

    def list_tasks(self) -> Dict[str, Any]:
        """列出所有已注册的定时任务"""
        tasks = []

        if IS_WINDOWS:
            try:
                proc = subprocess.Popen(
                    ["schtasks", "/Query", "/fo", "CSV", "/v"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                stdout, _ = proc.communicate(timeout=15)
                if proc.returncode == 0:
                    for line in stdout.splitlines():
                        if TASK_PREFIX in line:
                            tasks.append({"raw": line, "system": "schtasks"})
            except subprocess.TimeoutExpired:
                proc.kill()
            except FileNotFoundError:
                pass
        elif IS_LINUX:
            try:
                proc = subprocess.Popen(
                    ["crontab", "-l"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True
                )
                stdout, _ = proc.communicate(timeout=10)
                if proc.returncode == 0:
                    for line in stdout.splitlines():
                        if TASK_PREFIX in line:
                            tasks.append({"raw": line, "system": "crontab"})
            except subprocess.TimeoutExpired:
                proc.kill()
            except FileNotFoundError:
                pass

        # 合并自定义任务记录
        custom = []
        for name, info in self.custom_tasks.items():
            custom.append({
                "task_name": name,
                "command": info.get("command", ""),
                "schedule": info.get("schedule", {}),
                "enabled": info.get("enabled", True),
                "created": info.get("created", "")
            })

        return {
            "ok": True,
            "system": self.system,
            "system_tasks": tasks,
            "custom_tasks": custom,
            "total": len(tasks) + len(custom),
            "version": self.VERSION
        }

    def cancel(self, task_name: str) -> Dict[str, Any]:
        """取消已注册的定时任务"""
        try:
            if IS_WINDOWS:
                return self._cancel_windows(task_name)
            elif IS_LINUX:
                return self._cancel_linux(task_name)
            else:
                return {"ok": False, "error": f"不支持的操作系统: {self.system}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

        # 移除自定义记录
        if task_name in self.custom_tasks:
            del self.custom_tasks[task_name]
            self._save_custom_tasks()

        return {"ok": True, "message": f"任务已取消: {task_name}"}

    def _cancel_windows(self, task_name: str) -> Dict[str, Any]:
        """取消 Windows 任务"""
        proc = subprocess.Popen(
            ["schtasks", "/Delete", "/TN", f"{TASK_PREFIX}{task_name}", "/F"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        try:
            _, stderr = proc.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "error": "schtasks 删除超时"}

        if proc.returncode == 0:
            if task_name in self.custom_tasks:
                del self.custom_tasks[task_name]
                self._save_custom_tasks()
            return {"ok": True, "message": f"任务已删除: {TASK_PREFIX}{task_name}"}
        else:
            return {"ok": False, "error": stderr.strip()}

    def _cancel_linux(self, task_name: str) -> Dict[str, Any]:
        """取消 Linux crontab 任务"""
        task_flag = f"# {TASK_PREFIX}{task_name}"

        try:
            proc = subprocess.Popen(
                ["crontab", "-l"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            stdout, _ = proc.communicate(timeout=10)
            existing = stdout if proc.returncode == 0 else ""
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "error": "crontab 读取超时"}

        lines = [l for l in existing.splitlines()
                 if not l.startswith(task_flag) and l.strip()]
        new_crontab = "\n".join(lines) + "\n"

        proc = subprocess.Popen(
            ["crontab", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        try:
            _, stderr = proc.communicate(input=new_crontab, timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            return {"ok": False, "error": "crontab 写入超时"}

        if proc.returncode == 0:
            if task_name in self.custom_tasks:
                del self.custom_tasks[task_name]
                self._save_custom_tasks()
            return {"ok": True, "message": f"crontab 任务已删除: {TASK_PREFIX}{task_name}"}
        else:
            return {"ok": False, "error": stderr.strip()}

    def enable_task(self, task_name: str, enable: bool = True) -> Dict[str, Any]:
        """启用/禁用任务"""
        if IS_WINDOWS:
            action = "/ENABLE" if enable else "/DISABLE"
            proc = subprocess.Popen(
                ["schtasks", "/Change", "/TN", f"{TASK_PREFIX}{task_name}", action],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            try:
                _, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                return {"ok": False, "error": "schtasks 变更超时"}

            if proc.returncode == 0:
                if task_name in self.custom_tasks:
                    self.custom_tasks[task_name]["enabled"] = enable
                    self._save_custom_tasks()
                return {"ok": True, "message": f"任务已{'启用' if enable else '禁用'}: {task_name}"}
            else:
                return {"ok": False, "error": stderr.strip()}
        else:
            return {"ok": False, "error": "Linux crontab 不支持启用/禁用，请手动编辑 crontab"}

    def list_predefined(self) -> Dict[str, Any]:
        """列出所有预定义任务"""
        return {
            "ok": True,
            "predefined": PREDEFINED_TASKS,
            "total": len(PREDEFINED_TASKS),
            "version": self.VERSION
        }


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(
        description=f"定时任务注册管理 {ScheduleRegister.VERSION}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # register 子命令
    p_reg = sub.add_parser("register", help="注册定时任务")
    p_reg.add_argument("--name", required=True, help="任务名称（或预定义任务ID: weekly_report/monthly_report/data_refresh/backup_docs）")
    p_reg.add_argument("--cmd", default="", help="自定义 CLI 命令（不指定则使用预定义）")
    p_reg.add_argument("--schedule", default="", help="调度配置 JSON（不指定则使用预定义）")
    p_reg.add_argument("--workdir", default="", help="工作目录")
    p_reg.add_argument("--disable", action="store_true", help="注册后禁用")
    p_reg.add_argument("--force", action="store_true", help="跳过确认（自动化场景）")

    # list 子命令
    sub.add_parser("list", help="列出已注册的定时任务")

    # cancel 子命令
    p_cancel = sub.add_parser("cancel", help="取消定时任务")
    p_cancel.add_argument("--name", required=True, help="任务名称")

    # enable/disable 子命令
    p_toggle = sub.add_parser("toggle", help="启用/禁用任务")
    p_toggle.add_argument("--name", required=True, help="任务名称")
    p_toggle.add_argument("--off", action="store_true", help="禁用（默认启用）")

    # predefined 子命令
    sub.add_parser("predefined", help="列出预定义任务")

    # run 子命令（手动触发）
    p_run = sub.add_parser("run", help="手动执行预定义任务（不注册）")
    p_run.add_argument("--name", required=True, help="预定义任务名称")

    args = parser.parse_args()
    reg = ScheduleRegister()

    if args.command == "register":
        schedule = None
        if args.schedule:
            try:
                schedule = json.loads(args.schedule)
            except json.JSONDecodeError as e:
                print(json.dumps({"ok": False, "error": f"调度配置 JSON 解析失败: {e}"}))
                return

        result = reg.register(
            task_name=args.name,
            command=args.cmd,
            schedule=schedule,
            workdir=args.workdir,
            enable=not args.disable,
            force=args.force,
        )
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "list":
        result = reg.list_tasks()
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif args.command == "cancel":
        result = reg.cancel(args.name)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "toggle":
        result = reg.enable_task(args.name, enable=not args.off)
        print(json.dumps(result, ensure_ascii=False))

    elif args.command == "predefined":
        result = reg.list_predefined()
        print(json.dumps(result, ensure_ascii=False, default=str))

    elif args.command == "run":
        if args.name not in PREDEFINED_TASKS:
            print(json.dumps({"ok": False, "error": f"未知预定义任务: {args.name}"}))
            return
        task = PREDEFINED_TASKS[args.name]
        command = task["command"]
        print(f"执行命令: {command}")
        print("提示：请手动替换占位符（如 {date}、{data_file}）后执行")

    else:
        print(json.dumps({"ok": False, "error": "未知命令"}))


if __name__ == "__main__":
    main()
