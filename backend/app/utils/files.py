import hashlib
import json
import os
import tempfile
from pathlib import Path


def sha256_file(path: Path) -> str:
  digest = hashlib.sha256()

  with path.open("rb") as source:
    for block in iter(lambda: source.read(1024 * 1024), b""):
      digest.update(block)

  return digest.hexdigest()


def write_json_atomic(path: Path,
                      payload: dict,
                      *,
                      allow_nan: bool = True,
                      durable: bool = False) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary_path = None

  try:
    with tempfile.NamedTemporaryFile(mode="w",
                                     encoding="utf-8",
                                     newline="\n",
                                     dir=path.parent,
                                     prefix=f".{path.name}.",
                                     suffix=".tmp",
                                     delete=False) as output:
      temporary_path = Path(output.name)
      json.dump(payload,
                output,
                ensure_ascii=False,
                indent=2,
                allow_nan=allow_nan)
      output.write("\n")

      if durable:
        output.flush()
        os.fsync(output.fileno())

    os.replace(temporary_path, path)
  finally:
    if temporary_path is not None:
      temporary_path.unlink(missing_ok=True)
