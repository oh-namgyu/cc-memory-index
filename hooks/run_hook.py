"""Plugin entry point: run the hook from the plugin checkout, without installing anything.

Like the hook itself it always exits 0, even when the package cannot be imported (a broken
checkout, or a python3 older than 3.9), so that it can never fail a tool call.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from cc_memory_index.hook import main
except Exception as e:  # noqa: BLE001
    sys.stderr.write(f"cc-memory-index hook: cannot load the package: {type(e).__name__}: {str(e)[:200]}\n")
    sys.exit(0)

sys.exit(main())
