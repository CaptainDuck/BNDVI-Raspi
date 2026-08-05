"""Put `hylocropter/` on the path.

The app's modules import each other flat (`import bndvi`, `import flights`)
because they sit beside `app.py` and are run as scripts, not as a package. So the
tests add that directory rather than the repo root.
"""

import sys
from pathlib import Path

HYLOCROPTER = Path(__file__).resolve().parent.parent
if str(HYLOCROPTER) not in sys.path:
    sys.path.insert(0, str(HYLOCROPTER))
