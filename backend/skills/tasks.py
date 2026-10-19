"""
Tasks & notes skill.

Everything lives in a local JSON file (data/tasks.json) so there's no
database setup required. Swap this out for SQLite/Postgres later if
your task list grows large.
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "tasks.json"
DATA_FILE.parent.mkdir(exist_ok=True)


def _load() -> list[dict]:
    if not DATA_FILE.exists():
        return []
    return json.loads(DATA_FILE.read_text())


def _save(tasks: list[dict]) -> None:
    DATA_FILE.write_text(json.dumps(tasks, indent=2))


def list_tasks(include_done: bool = True) -> list[dict]:
    tasks = _load()
    if not include_done:
        tasks = [t for t in tasks if not t["done"]]
    return tasks


def add_task(title: str, due: str | None = None) -> dict:
    tasks = _load()
    task = {
        "id": str(uuid.uuid4())[:8],
        "title": title,
        "due": due,
        "done": False,
        "created_at": datetime.utcnow().isoformat(),
    }
    tasks.append(task)
    _save(tasks)
    return task


def complete_task(task_id: str) -> dict | None:
    tasks = _load()
    for t in tasks:
        if t["id"] == task_id:
            t["done"] = True
            _save(tasks)
            return t
    return None


def delete_task(task_id: str) -> bool:
    tasks = _load()
    new_tasks = [t for t in tasks if t["id"] != task_id]
    if len(new_tasks) == len(tasks):
        return False
    _save(new_tasks)
    return True
