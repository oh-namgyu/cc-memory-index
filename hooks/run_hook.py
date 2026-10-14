"""Plugin entry point: run the hook from the plugin checkout, without installing anything."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cc_memory_index.hook import main  # noqa: E402

sys.exit(main())
