"""Build a Waifly upload archive without credentials or live database writes."""

import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
FILES = ("bot.py", "vacancy.py", "deepseek.py", "knowledge.md", "requirements.txt")


def main():
    source_path = ROOT / "vacancy.sqlite3"
    if not source_path.is_file():
        raise SystemExit("Не найдена vacancy.sqlite3; архив с текущими заявками не создан")
    output_path = ROOT / "dist" / "waifly-release.zip"
    output_path.parent.mkdir(exist_ok=True)

    with TemporaryDirectory() as directory:
        snapshot_path = Path(directory) / "vacancy.sqlite3"
        source = sqlite3.connect(source_path)
        snapshot = sqlite3.connect(snapshot_path)
        try:
            source.backup(snapshot)
            if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Проверка копии SQLite не прошла")
        finally:
            snapshot.close()
            source.close()

        with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
            for name in FILES:
                archive.write(ROOT / name, name)
            archive.write(snapshot_path, "vacancy.sqlite3")

    print(f"Архив создан: {output_path}")
    print("Секреты .env в архив не включены")


if __name__ == "__main__":
    main()
