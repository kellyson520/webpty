# Vendored tomli 2.0.1 (MIT)

Pure-Python backport of the stdlib `tomllib` (Python 3.11+), vendored so
webpty keeps its "zero runtime deps" promise on Python 3.10 hosts
(`/usr/bin/python3` on Ubuntu 22.04 is 3.10 and has no `tomllib`).

Source: https://pypi.org/project/tomli/ — files copied verbatim from the
2.0.1 sdist (`src/tomli/{__init__,_parser,_re,_types}.py` + LICENSE).

Do not edit here; upgrade by replacing the whole `src/tomli/` directory
from a newer tomli release.
