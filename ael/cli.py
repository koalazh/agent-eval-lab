from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import typer

from .agents import builtin_real_drivers, builtin_test_drivers, collector_status, probe_registry
from .cases import load_experiment
from .persistence import Repository
from .reports import matrix_report
from .runner import Runner

app = typer.Typer(add_completion=False, help="Agent Eval Lab 本地实验命令行工具。")


def _drivers() -> dict[str, Any]:
    result = {}
    result.update(builtin_real_drivers())
    result.update(builtin_test_drivers())
    return result


@app.command(help="检查数据库、已注册的 Agent CLI 和本地 OTel Collector。")
def doctor(root: Path = typer.Option(Path("."), "--root", help="AEL 项目根目录。")) -> None:
    repository = Repository(root)
    rows = probe_registry(repository)
    print("AEL 环境检查")
    print(f"数据库：正常（{repository.db_path}）")
    for row in rows:
        agent = row["agent"]
        capabilities = row["capabilities"]
        state = "可用" if capabilities["available"] else "不可用"
        print(
            f"{agent['id']}：{state}，版本={agent['detected_version']}，"
            f"controlled={capabilities['controlled_support']}"
        )
    collector = collector_status()
    collector_state = "可用" if collector["available"] else "未找到"
    print(f"OTel Collector：{collector_state}，host={collector['host']}，ports={collector['ports']}")


@app.command(help="探测并输出 Agent 能力矩阵。")
def agents(root: Path = typer.Option(Path("."), "--root", help="AEL 项目根目录。")) -> None:
    repository = Repository(root)
    rows = probe_registry(repository)
    print(json.dumps(rows, indent=2, sort_keys=True))


@app.command("run", help="执行一个实验定义并输出矩阵结果。")
def run_experiment(
    experiment: Path = typer.Argument(..., exists=True, readable=True, help="实验定义文件。"),
    root: Path = typer.Option(Path("."), "--root", help="AEL 项目根目录。"),
) -> None:
    spec = load_experiment(experiment)
    runner = Runner(Repository(root), _drivers())
    results = asyncio.run(runner.run_experiment(spec))
    print(json.dumps({"experiment": spec.id, "matrix": matrix_report(results), "runs": results}, indent=2, sort_keys=True))


@app.command("compare", help="比较两个已持久化的实验。")
def compare(
    experiment_a: str = typer.Argument(..., help="基线实验 ID。"),
    experiment_b: str = typer.Argument(..., help="候选实验 ID。"),
    root: Path = typer.Option(Path("."), "--root", help="AEL 项目根目录。"),
) -> None:
    from .comparison import compare_experiments

    report = compare_experiments(Repository(root), experiment_a, experiment_b)
    print(json.dumps(report, indent=2, sort_keys=True))


@app.command(help="启动本地 Web UI。")
def ui(
    root: Path = typer.Option(Path("."), "--root", help="AEL 项目根目录。"),
    host: str = typer.Option("127.0.0.1", "--host", help="监听地址。"),
    port: int = typer.Option(8711, "--port", help="监听端口。"),
) -> None:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(root), host=host, port=port)


if __name__ == "__main__":
    app()
